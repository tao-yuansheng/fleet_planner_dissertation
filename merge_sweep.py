"""Zero-cost same-address merge sweep (K1 component 2).

Post-ALNS, collapse same-day same-postcode split visits when the merge is
feasible and net-km >= 0. Operational realism (one truck per dock), NOT a km
saver -- the replay proved these merges are km-neutral. Never degrades
feasibility, coverage, or km.
Spec: docs/superpowers/specs/2026-07-03-shuttle-carveout-design.md
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from freight_planner.route_seed import _reorder, same_order_handoff_conflict
from freight_planner.routing_adapter import evaluate_day, try_insert_job

CUSTOMER_PICKUP = "CUSTOMER_PICKUP"
CUSTOMER_DELIVERY = "CUSTOMER_DELIVERY"


@dataclass
class MergeSweepResult:
    """Outcome of one sweep pass.

    ``km_delta`` sign convention: negative = km saved (it accumulates ``-net``
    over applied merges). ``rollbacks`` means "merge abandoned at day
    revalidation" (the MERGED_DAY_INFEASIBLE census bucket) -- nothing was
    ever committed, so no state is unwound; the field name is kept for API
    stability.
    """
    applied: int = 0
    candidates: int = 0
    rollbacks: int = 0
    km_delta: float = 0.0
    census: Counter = field(default_factory=Counter)


def _day_km(vehicle, trips) -> float:
    ev = evaluate_day(vehicle, trips)
    return ev.total_km if ev.feasible else float("inf")


def _all_job_ids(solution: dict) -> Counter:
    ids: Counter = Counter()
    for trips in solution.values():
        for trip in trips:
            for job in trip:
                ids[job.job_id] += 1
    return ids


def apply_zero_cost_merges(solution: dict, job_meta: dict, vehicle_meta: dict,
                            route_vehicle, excluded: set, pinned: frozenset,
                            watermarks: dict | None = None, commit_floor=None,
                            now=None,
                            ) -> MergeSweepResult:
    """One greedy pass. Mutates ``solution`` in place.

    ``route_vehicle`` is alns._route_vehicle (vehicle_meta entry + day ->
    RouteVehicle); injected to avoid a circular import.

    ``excluded`` is the tour-reserved (vehicle_id, day) key set: those days
    may neither host a merge nor donate a guest job. ``pinned`` is the
    shuttle job-id set: pinned jobs never move (merging INTO a trip that
    carries pinned jobs is allowed -- that's top-up).

    Rolling context (WT255131 fix, 2026-07-14 — the sweep was the ONE insertion
    path without the E6 guards, so a noon top-up landed inside a departed trip
    and retimed served stops): ``watermarks`` {(vid, day) -> committed stop
    counts per trip} makes a merge insert only AFTER the committed watermark;
    ``commit_floor`` rejects any merge whose changed/added stops arrive before
    now + delta_R1 (checked with alns.floor_ok on the merged host day). All
    three default None = static behavior, byte-identical.
    """
    before_ids = _all_job_ids(solution)

    res = MergeSweepResult()
    # index customer stops: (day, pc) -> list[(key, trip_idx, job)]
    groups: dict = {}
    for key, trips in solution.items():
        vid, day = key
        for ti, trip in enumerate(trips):
            for job in trip:
                meta = job_meta.get(job.job_id)
                if meta is None:
                    continue
                pc = str(meta.candidate.get("service_pc", "") or "").upper().strip()
                if not pc:
                    continue
                groups.setdefault((day, pc), []).append((key, ti, job))

    for (day, pc), members in sorted(groups.items()):
        vids = {key[0] for key, _ti, _j in members}
        if len(vids) < 2:
            continue
        by_trip = Counter((key, ti) for key, ti, _j in members)
        (host_key, host_ti), _n = by_trip.most_common(1)[0]
        host_vid, _hday = host_key
        hmeta_v = vehicle_meta.get(host_vid)
        if hmeta_v is None or host_key in excluded:
            continue
        member_ids = {j.job_id for _k, _t, j in members}
        for key, ti, job in list(members):
            if key == host_key:
                continue
            res.candidates += 1
            # Structural invariant: daily solutions shouldn't contain
            # tour-reserved keys at all. This is insurance -- silently pulling
            # a job off a reserved day would NOT be caught by the
            # job-conservation assert (the job would still exist, elsewhere).
            if key in excluded:
                res.census["EXCLUDED"] += 1
                continue
            # groups were built only from jobs with meta present and job_meta
            # is never mutated here, so the lookup cannot miss.
            meta = job_meta[job.job_id]
            if job.job_id in pinned:
                res.census["PINNED"] += 1
                continue
            kind = str(meta.candidate.get("leg_kind", "") or "")
            depot_field = "target_depot" if kind == CUSTOMER_PICKUP else "source_depot"
            jd = str(meta.candidate.get(depot_field, "") or "")
            if kind in (CUSTOMER_PICKUP, CUSTOMER_DELIVERY) and jd \
                    and jd != str(hmeta_v.home_depot):
                res.census["DEPOT_MISMATCH"] += 1
                continue
            if host_vid not in set(meta.eligible_vehicles):
                res.census["OK_SET_EXCLUDED"] += 1
                continue
            gmeta_v = vehicle_meta.get(key[0])
            if gmeta_v is None:
                res.census["NO_VEHICLE_META"] += 1
                continue
            host_trips = solution.get(host_key)
            if host_trips is None or host_ti >= len(host_trips):
                res.census["HOST_GONE"] += 1
                continue
            host_trip = host_trips[host_ti]
            # Cross-group staleness: an earlier group's merge may have dropped
            # an emptied trip and shifted this vehicle's trip indices, so
            # host_ti can now point at an unrelated trip. The real host trip
            # must still carry at least one stop of THIS (day, pc) group.
            if not any(j.job_id in member_ids for j in host_trip):
                res.census["HOST_GONE"] += 1
                continue
            if same_order_handoff_conflict(host_trip, job):
                res.census["HANDOFF_CONFLICT"] += 1
                continue
            hveh = route_vehicle(hmeta_v, day)
            host_wm = (watermarks or {}).get(host_key, ())
            host_minpos = int(host_wm[host_ti]) if host_ti < len(host_wm) else 0
            trip_ev = try_insert_job(hveh, host_trip, job, "best",
                                     min_position=host_minpos)
            if not trip_ev.feasible:
                res.census[f"TRIP_{trip_ev.failure_reason or 'NO_POSITION'}"] += 1
                continue

            new_host = [list(t) for t in host_trips]
            new_host[host_ti] = _reorder(host_trip + [job], trip_ev)
            if commit_floor is not None:
                # the merged HOST day must clear the E6 floor exactly like an
                # ALNS insertion would: no new/uncommitted stop before
                # now + delta_R1, nothing appended behind a departed prefix.
                from freight_planner.alns import floor_ok
                merged_ev = evaluate_day(hveh, new_host)
                if not merged_ev.feasible or not floor_ok(
                        merged_ev, host_wm, commit_floor, now=now):
                    res.census["FLOOR"] += 1
                    continue
            base_host = _day_km(hveh, host_trips)
            after_host = _day_km(hveh, new_host)

            gveh = route_vehicle(gmeta_v, day)
            guest_trips = solution.get(key, [])
            new_guest = [[j for j in t if j.job_id != job.job_id] for t in guest_trips]
            new_guest = [t for t in new_guest if t]
            base_guest = _day_km(gveh, guest_trips)
            after_guest = _day_km(gveh, new_guest) if new_guest else 0.0

            if base_host == float("inf") or base_guest == float("inf"):
                # A day that is ALREADY infeasible before the merge is an
                # upstream bug, not a sweep rollback -- keep it loudly visible.
                res.census["BASE_INFEASIBLE"] += 1
                continue
            if after_host == float("inf") or after_guest == float("inf"):
                # Merge abandoned at day revalidation (trip-level insert was
                # feasible but the full day is not). Nothing was committed.
                res.rollbacks += 1
                res.census["MERGED_DAY_INFEASIBLE"] += 1
                continue

            net = (base_guest - after_guest) - (after_host - base_host)
            # -1e-6 epsilon: this is an optics pass, so a merge whose float
            # noise lands a hair below exactly-zero must not be spuriously
            # skipped.
            if net < -1e-6:
                res.census["NET_NEGATIVE"] += 1
                continue

            solution[host_key] = new_host
            if new_guest:
                solution[key] = new_guest
            else:
                solution.pop(key, None)
            res.applied += 1
            res.km_delta -= net  # negative = km saved (see MergeSweepResult)
            res.census["APPLIED"] += 1

    after_ids = _all_job_ids(solution)
    assert before_ids == after_ids, (
        "merge_sweep lost or duplicated jobs: "
        f"missing={list((before_ids - after_ids).elements())} "
        f"extra={list((after_ids - before_ids).elements())}"
    )
    return res
