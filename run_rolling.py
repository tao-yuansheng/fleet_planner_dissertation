"""E6 DYNAMIC rolling dispatcher (spec 2026-07-10 §4.7a).

The modern loop the review architecture describes: a batch seed from the
overnight book, rolling re-optimization at the day's two anchor epochs
(00:00 midnight seed, 12:00 noon cut-off), INSERTION into in-flight routes'
open suffixes (commitment watermarks, never live telematics — the plan's own
clock is the position feed), micro insertion-passes (06:00-18:00) between
anchors for same-day arrivals, and freeze periods at stop granularity.

Commitment is the EXPIRY rule at trip level (a trip departing before the next
decision point + delta_R1 is launched and cannot be unlaunched) plus the
WATERMARK at stop level (stops the driver has begun, or is rolling toward, are
fact; the suffix stays open to insertion and re-sequencing, pinned to its
vehicle). In-flight trips are carried INSIDE the solution across epochs, so the
final epoch's improvement IS the merged plan — one emission through the
unchanged ``emit_outputs`` contract.

Slips follow the promise-fixed rule (§4.6): a missed collection consumes float,
the delivery date moves only when float is exhausted. Tonight's trunk is sized
at the 18:00 day close from the dock — the EXPORTS committed today (export-only,
2026-07-24; imports arrive via the invisible hub, never our trunk), never from
the noon solve. Strict whole-trip freezing survives as a config (--strict) for
the floor measurement.

TESTABILITY: the orchestration lives in ``run_dynamic_loop(cfg, ctx)`` with the
window builder and solver injected via ``LoopCtx`` — a scripted fake solver
drives the whole loop on a toy universe in milliseconds (test_dynamic_e2e.py),
so integration seams fail in tests, not in 15-minute smokes.
"""
from __future__ import annotations

import argparse
import json
import os
import time as _time
import warnings
from argparse import Namespace
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from freight_planner.alns import improve_existing_solution, insertion_pass
from freight_planner.alns import JobMeta as _JobMeta
from freight_planner.alns import VehicleMeta as _VehicleMeta
from freight_planner import config as _fp_cfg
from freight_planner.build_phase0 import _load_qargo, _parse_date
from freight_planner.demand import build_demand_records
from freight_planner.disturbance import disturbance_breakdown, imminence_weights
from freight_planner.epoch_state import (
    DELTA_MIN,
    RollingState,
    Watermarks,
    duty_after_freeze,
    epoch_grid,
)
from freight_planner.output_layout import flat_window_label, run_dirs, write_run_manifest
from freight_planner.paths import DEFAULT_ENRICHED, DEFAULT_OUT_DIR, DEFAULT_POSTCODE_CACHE
from freight_planner.progress import RunLog
from freight_planner.route_seed import _ok_options, _route_vehicle, make_route_job
from freight_planner.routing_adapter import DutyOverride, apply_avail_override, evaluate_day
from freight_planner.run_alns import (
    SolveResult,
    _env_toggles,
    build_window_inputs,
    emit_outputs,
    reoptimize_window,
    solve_window,
)
from freight_planner.shuttle import detect_shuttle_bins
from freight_planner.trunk import TrunkPlan
from freight_planner.visibility import (
    COLLECT_FLOWS,
    DELIVER_FLOWS,
    build_order_meta,
    shuttle_exempt_order_ids,
    visible_order_ids,
)

DAY_CLOSE_HOUR = 18   # trunk loading cutoff; micro_times' day_end_hour matches

_BASE_SOLVE_FIELDS = dict(
    date_basis="planning_window", responsibility_mode="forward_structural",
    time_budget=100000.0, no_improve=100000, restarts=1, log_every=200,
    router="osrm", sa_temp=0.005, sa_cooling=0.999, consolidate_tours=True,
    repair_every=20, regret_repair=False, day_flex=False,
)


# --------------------------------------------------------------------------
# helpers (unit-tested)
# --------------------------------------------------------------------------


def _assert_tour_record_day_caps(records: list) -> None:
    """Fail closed when emitted tour physics exceed a per-day hard cap."""
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for record in records or []:
        key = (
            str(getattr(record, "vehicle_id", "")),
            str(getattr(record, "service_date", ""))[:10],
        )
        if not all(key):
            continue
        day = grouped.setdefault(key, {"drive": 0.0, "times": [], "records": []})
        day["records"].append(record)
        day["drive"] += float(
            getattr(record, "planned_drive_minutes", 0.0) or 0.0
        )
        for field in ("planned_arrive", "planned_depart"):
            stamp = pd.to_datetime(
                getattr(record, field, None), errors="coerce"
            )
            if pd.notna(stamp):
                day["times"].append(stamp)

    drive_cap = 600.0
    duty_cap = float(_fp_cfg.TOUR_DAY_ELAPSED_CAP_MIN)
    for key, day in grouped.items():
        drive = float(day["drive"])
        if drive > drive_cap + 1e-6:
            detail = [
                {
                    "route_id": str(getattr(record, "route_id", "")),
                    "job_id": str(getattr(record, "job_id", "")),
                    "leg_kind": str(getattr(record, "leg_kind", "")),
                    "drive_min": round(float(getattr(record, "planned_drive_minutes", 0.0) or 0.0), 3),
                    "arrive": str(getattr(record, "planned_arrive", "")),
                }
                for record in day["records"]
            ]
            raise ValueError(
                f"tour day drive cap exceeded for {key[0]}/{key[1]}: "
                f"{drive:.3f} > {drive_cap:.0f} min; records={detail}"
            )
        times = list(day["times"])
        if len(times) >= 2:
            duty = (max(times) - min(times)).total_seconds() / 60.0
            if duty > duty_cap + 1e-6:
                raise ValueError(
                    f"tour day duty cap exceeded for {key[0]}/{key[1]}: "
                    f"{duty:.3f} > {duty_cap:.0f} min"
                )


def _assert_no_mixed_tour_daily_keys(solution: dict, tour_records: list) -> None:
    """Reject a vehicle-day present in both ordinary and fixed-tour plans."""
    daily_keys = {
        (str(key[0]), str(key[1])[:10])
        for key, trips in (solution or {}).items()
        if trips
    }
    tour_keys = {
        (
            str(getattr(record, "vehicle_id", "")),
            str(getattr(record, "service_date", ""))[:10],
        )
        for record in (tour_records or [])
    }
    collisions = sorted(key for key in daily_keys & tour_keys if all(key))
    if collisions:
        rendered = ", ".join(f"{vid}/{day}" for vid, day in collisions)
        raise ValueError(f"tour/daily vehicle-day collision: {rendered}")

def day_close_trunk(day_iso: str, next_day_iso: str, frozen_leg_ids_today: set,
                    candidate_df: pd.DataFrame, vehicle_df: pd.DataFrame,
                    tour_reserved: set, route_end_by_vid: dict | None = None):
    """Size TONIGHT's trunk from the dock (spec §4.6a): the EXPORTS the plan
    actually committed (froze) today. Export-only (2026-07-24) — network imports
    arrive via the invisible hub, never our trunk, so tomorrow's import manifest
    no longer sizes tonight. ``next_day_iso`` is kept only as the window upper
    bound handed to ``trunk_schedule``."""
    from freight_planner.shared.config import DEPOT_ANCHORS

    from freight_planner.route_costs import road_km
    from freight_planner.tour_plan import B37_LATLON, LE10_LATLON
    from freight_planner.trunk import TRUNK_DAY_DEPOTS, TRUNK_DEPOTS, draw_tractors, trunk_schedule

    rt = {(d, "B37_HUB"): 2.0 * road_km(*DEPOT_ANCHORS[d], *B37_LATLON)
          for d in (*TRUNK_DEPOTS, *TRUNK_DAY_DEPOTS) if d in DEPOT_ANCHORS}
    if "CB22" in DEPOT_ANCHORS:
        rt[("CB22", "LE10_HUB")] = 2.0 * road_km(*DEPOT_ANCHORS["CB22"], *LE10_LATLON)
    legs = candidate_df["leg_id"].astype(str)
    frame = candidate_df[legs.isin(set(frozen_leg_ids_today))]
    nights = [n for n in trunk_schedule(frame, day_iso, next_day_iso, rt)
              if str(n.night) == day_iso]
    plan = draw_tractors(nights, vehicle_df, set(tour_reserved or set()),
                         route_end_by_vid=route_end_by_vid)
    # plan.nights are the DRAWN nights (vehicles + feasible stamped, gap 5) —
    # returning the raw schedule here would emit a nameless trunk_schedule.csv.
    return plan.nights, plan


def redate_qargo(qargo_df: pd.DataFrame, slip_pool: dict, flow_of: dict) -> pd.DataFrame:
    """Shift slipped orders' collection day by their slip count. THE DELIVERY
    PROMISE IS FIXED (stakeholder rule 2026-07-10): a slip consumes collection
    float first; when collection lands ON the delivery day the same-day option
    set (DIRECT / same-day xdock) is the recovery mode; only exhausted float
    moves the delivery (an explicit promise break). Exports' destination leg is
    the network's and never moves.

    The pickup WINDOW anchor columns move with the slip (fix 2026-07-16): the
    collection window derives from the requested/actual timestamp columns
    (scope._pickup_anchor_timestamp), NOT origin_date — un-shifted they pinned a
    slipped order's window to the ORIGINAL day forever, so every retry was
    TIME_WINDOW-dead and nothing could ever serve late (zero SLIPPED outcomes;
    WT255059 starved 6 days). Requested and actual shift TOGETHER — the anchor's
    reschedule branch re-anchors to the actual's date whenever the two dates
    differ, so shifting only one re-pins to the stale other. A placeholder
    midnight stays a placeholder on the new date (expands to the new operating
    day downstream); windows only ever move LATER, so non-anticipation holds.
    A pushed FULL_FLEET delivery shifts its window anchors by the same push."""
    if not slip_pool:
        return qargo_df
    q = qargo_df.copy()
    oid = q["order_id"].astype(str)
    days = oid.map(slip_pool)
    mask = days.notna()
    if not mask.any():
        return qargo_df
    shift = pd.to_timedelta(days.fillna(0).astype(int), unit="D")

    def _shift_cols(cols, row_mask, delta):
        for col in cols:
            if col in q.columns:
                ts = pd.to_datetime(q[col], errors="coerce")
                q.loc[row_mask, col] = (ts + delta)[row_mask]

    od = pd.to_datetime(q["origin_date"], errors="coerce")
    new_od = od + shift
    q.loc[mask, "origin_date"] = new_od[mask].dt.strftime("%Y-%m-%d")
    _shift_cols(("origin_requested_start_timestamp_local", "origin_timestamp_local",
                 "origin_end_timestamp_local"), mask, shift)
    ff = mask & oid.map(flow_of).eq("FULL_FLEET")
    if ff.any():
        dd = pd.to_datetime(q["destination_date"], errors="coerce")
        pushed = new_od.where(new_od > dd, dd)
        q.loc[ff, "destination_date"] = pushed[ff].dt.strftime("%Y-%m-%d")
        push = (pushed - dd).fillna(pd.Timedelta(0))
        _shift_cols(("destination_requested_start_timestamp_local",
                     "destination_timestamp_local", "destination_end_timestamp_local"),
                    ff, push)
    return q


def merge_frozen_routing(route_totals: dict, route_times: dict) -> tuple[dict, dict]:
    """Rebuild day-level keys from per-trip keys (kept for analysis/tests; the
    dynamic loop's final improvement already carries consistent totals)."""
    by_day: dict[str, list[tuple[int, str]]] = {}
    for key in route_times:
        if "#T" in key:
            rid, t = key.rsplit("#T", 1)
            by_day.setdefault(rid, []).append((int(t), key))
    totals, times = dict(route_totals), dict(route_times)
    for rid, trips in by_day.items():
        trips.sort()
        totals[rid] = sum(float(route_totals.get(k, 0.0)) for _, k in trips)
        times[rid] = (route_times[trips[0][1]][0], route_times[trips[-1][1]][1])
    return totals, times


def dispatch_floor(prior: DutyOverride | str | None, lo: datetime, day: str,
                   profile_start: str | None = None):
    """A plan computed at t cannot dispatch before t + delta: today's fresh
    vehicles may not start earlier than the floor (and never earlier than their
    own shift)."""
    lo_iso = lo.isoformat(sep=" ")
    if prior is None:
        if profile_start and str(profile_start) >= lo_iso:
            return None
        return DutyOverride(start_iso=lo_iso)
    if isinstance(prior, DutyOverride):
        if str(prior.start_iso) >= lo_iso:
            return prior
        return DutyOverride(start_iso=lo_iso, drive_since_break0=prior.drive_since_break0,
                            drive_minutes_left=prior.drive_minutes_left)
    hh = datetime.strptime(f"{day} {prior}", "%Y-%m-%d %H:%M")
    return prior if hh >= lo else DutyOverride(start_iso=lo_iso)


def _tour_first_departs(tour_records: list) -> dict[str, datetime]:
    """Infer each tour's depot departure from its first emitted movement.

    Tour records begin at the first service/overnight event rather than with a
    depot-start row. ``planned_depart`` is therefore the *customer* departure,
    which can be many hours after the vehicle actually left. Back-calculate the
    movement start from arrival minus its inbound driving and statutory break;
    legacy/synthetic records without those fields retain the old timestamp
    fallback.
    """
    firsts: dict[str, datetime] = {}
    for r in tour_records:
        try:
            arrive_raw = getattr(r, "planned_arrive", "")
            depart_raw = getattr(r, "planned_depart", "")
            drive = float(getattr(r, "planned_drive_minutes", 0.0) or 0.0)
            break_min = float(getattr(r, "break_minutes_before", 0.0) or 0.0)
            if arrive_raw and (drive > 0.0 or break_min > 0.0):
                dep = datetime.fromisoformat(str(arrive_raw)) - timedelta(
                    minutes=drive + break_min
                )
            else:
                dep = datetime.fromisoformat(str(depart_raw or arrive_raw))
        except (TypeError, ValueError):
            continue
        rid = str(r.route_id)
        if rid not in firsts or dep < firsts[rid]:
            firsts[rid] = dep
    return firsts


# --------------------------------------------------------------------------
# dynamic-loop helpers (pure; unit-tested)
# --------------------------------------------------------------------------

def stop_timings(vrow, key: tuple[str, str], trips: list,
                 override=None) -> tuple[str, list[list[tuple[str, str]]]]:
    """(depot_depart_iso, per-trip [(arrive, depart), ...]) from a detail
    evaluation — the plan's own clock, used to advance watermarks. MUST apply
    the same availability override the solve ran under (trunk 10:00 start,
    dispatch floor), or the re-evaluation drifts from the accepted plan."""
    rv = apply_avail_override(_route_vehicle(vrow, key[1]), override, key[1])
    ev = evaluate_day(rv, trips, detail=True)
    per_trip = [[(s.arrive, s.depart) for s in tev.stops] for tev in ev.trip_evaluations]
    dep0 = ev.trip_evaluations[0].route_start if ev.trip_evaluations else rv.start_time
    return str(dep0), per_trip


def trip_timings(vrow, key: tuple[str, str], trips: list, override=None):
    """Per-trip ``(route_starts, route_ends, per_trip [[(arrive, depart), ...]])``:
    each trip's depot DEPARTURE and return ARRIVAL (the exact depot-drive bounds) plus
    its per-stop times. Same evaluator + override as ``stop_timings``, so the snapshot's
    depot legs match the accepted plan rather than being estimated downstream."""
    rv = apply_avail_override(_route_vehicle(vrow, key[1]), override, key[1])
    ev = evaluate_day(rv, trips, detail=True)
    starts = [tev.route_start for tev in ev.trip_evaluations]
    ends = [tev.route_end for tev in ev.trip_evaluations]
    per_trip = [[(s.arrive, s.depart) for s in tev.stops] for tev in ev.trip_evaluations]
    return starts, ends, per_trip


def plan_snapshot_rows(sol: dict, epoch_iso: str, kind: str, order_of_job: dict,
                       timings_fn, view=None) -> list[dict]:
    """One snapshot of the LIVE plan at a decision epoch: per (vehicle, trip, stop),
    exactly what would be dispatched to the driver at ``epoch_iso``. ``timings_fn(key,
    trips)`` returns ``(route_starts, route_ends, per_trip [[(arrive, depart), ...]])``
    — each trip's depot departure + return arrival and its per-stop times; the loop
    injects ``trip_timings`` (same override the solve ran under), tests a stub.
    ``view[key]`` (optional) is the watermark committed-stop count per trip; a stop is
    ``committed`` once its sequence falls inside that count (already locked to a driver).
    Read-only observation of ``sol`` — it never mutates the plan."""
    rows: list[dict] = []
    for key, trips in sol.items():
        vid, day = key
        tt = [trips] if trips and hasattr(trips[0], "job_id") else trips
        try:
            starts, ends, per_trip = timings_fn(key, tt)
        except Exception as e:
            # Degrade to blank times but NEVER silently: a vehicle-epoch with no
            # times reads as a vanished route on the board — that mask hid the
            # frozen-orders bug (2026-07-13) for days.
            warnings.warn(f"plan snapshot: timing evaluation failed for {key} "
                          f"at {epoch_iso}: {e!r} — stops emitted without times")
            starts, ends, per_trip = [], [], []
        counts = (view or {}).get(key)
        for ti, trip in enumerate(tt):
            times = per_trip[ti] if ti < len(per_trip) else []
            cc = counts[ti] if (counts and ti < len(counts)) else 0
            rstart = str(starts[ti]) if ti < len(starts) else ""
            rend = str(ends[ti]) if ti < len(ends) else ""
            for seq, j in enumerate(trip):
                arr, dep = times[seq] if seq < len(times) else ("", "")
                rows.append({
                    "epoch": epoch_iso, "epoch_kind": kind,
                    "vehicle_id": str(vid), "service_date": str(day),
                    "trip_index": ti + 1, "sequence": seq,
                    "job_id": str(j.job_id),
                    "order_id": str(order_of_job.get(j.job_id, "") or ""),
                    "leg_kind": str(getattr(j, "leg_kind", "") or ""),
                    "planned_arrive": str(arr or ""), "planned_depart": str(dep or ""),
                    "committed": int(seq < cc),
                    "route_start": rstart, "route_end": rend,
                })
    return rows


def trunk_snapshot_rows(nights, epoch_iso: str) -> list[dict]:
    """Snapshot rows for the day-close trunk decision (2026-07-21 user request):
    trunk trips were invisible to plan_snapshots.csv, so plan-vs-telematics
    momentum figures missed the 21:00 night runs entirely. One row per trip;
    vehicles from the draw (TRUNK-SHORTFALL when the pool ran short). Times from
    the trunk constants: night trunks depart TRUNK_DEPART_HOUR, day trunks
    (Stoke) run from the 18:00 close; hub arrive = depart + one-way drive,
    hub depart adds TRUNK_HUB_DWELL_MIN, route_end = the return leg."""
    from freight_planner.route_costs import drive_minutes
    from freight_planner.shared.config import TRUNK_DEPART_HOUR, TRUNK_HUB_DWELL_MIN
    rows: list[dict] = []
    for n in nights:
        trips = max(1, int(getattr(n, "trips", 0) or 0))
        one_way_min = drive_minutes(float(getattr(n, "km", 0.0) or 0.0) / trips / 2.0)
        depart_h = DAY_CLOSE_HOUR if getattr(n, "day_trunk", False) else int(TRUNK_DEPART_HOUR)
        dep = datetime.fromisoformat(f"{str(n.night)[:10]} 00:00:00") + timedelta(hours=depart_h)
        hub_arr = dep + timedelta(minutes=one_way_min)
        hub_dep = hub_arr + timedelta(minutes=int(TRUNK_HUB_DWELL_MIN))
        back = hub_dep + timedelta(minutes=one_way_min)
        vehicles = tuple(getattr(n, "vehicles", ()) or ())
        for i in range(int(getattr(n, "trips", 0) or 0)):
            fmt = "%Y-%m-%d %H:%M:%S"
            rows.append({
                "epoch": epoch_iso, "epoch_kind": "close",
                "vehicle_id": str(vehicles[i]) if i < len(vehicles) else "TRUNK-SHORTFALL",
                "service_date": str(n.night)[:10],
                "trip_index": i + 1, "sequence": 0,
                "job_id": f"TRUNK:{n.depot}:{str(n.night)[:10]}#{i + 1}",
                "order_id": "", "leg_kind": "TRUNK",
                "planned_arrive": hub_arr.strftime(fmt),
                "planned_depart": hub_dep.strftime(fmt),
                "committed": 1,
                "route_start": dep.strftime(fmt), "route_end": back.strftime(fmt),
            })
    return rows


def advance_watermarks(wm: Watermarks, inflight: dict, timings: dict, now: datetime) -> dict:
    """Advance every in-flight trip's watermark to ``now`` and return the ALNS
    view {(vid, day) -> tuple(committed count per trip)}. A trip with no usable
    timing info is treated as FULLY committed — never re-plannable — because
    the safe failure mode is refusing to touch work we cannot place in time."""
    view: dict[tuple[str, str], tuple] = {}
    for key, trips in inflight.items():
        dep0, per_trip = timings.get(key, ("", []))
        counts = []
        for t_idx in range(len(trips)):
            k3 = (key[0], key[1], t_idx + 1)
            stops = per_trip[t_idx] if t_idx < len(per_trip) else []
            if not stops or not dep0:
                cur = max(int(wm.marks.get(k3, 0)), len(trips[t_idx]))
                wm.marks[k3] = cur
                counts.append(cur)
                continue
            if t_idx > 0 and t_idx - 1 < len(per_trip) and per_trip[t_idx - 1]:
                prev_ret = per_trip[t_idx - 1][-1][1]
            else:
                prev_ret = dep0
            counts.append(wm.advance(k3, prev_ret, stops, now))
        view[key] = tuple(counts)
    return view


def expire_commit(sol: dict, route_times: dict, day_iso: str,
                  horizon: datetime) -> dict:
    """The expiry rule: today's trips departing before ``horizon`` (the next
    decision point + delta_R1) are launched — return {key -> [trips]} newly
    committed (callers merge into the in-flight set)."""
    out: dict = {}
    for (vid, d), trips in sol.items():
        if str(d)[:10] != day_iso:
            continue
        tt = [trips] if trips and hasattr(trips[0], "job_id") else list(trips)
        chosen = []
        for k, trip in enumerate(tt, start=1):
            val = route_times.get(f"ROUTE:{vid}:{d}#T{k}") or (
                route_times.get(f"ROUTE:{vid}:{d}") if len(tt) == 1 else None)
            if not val:
                continue
            try:
                dep = datetime.fromisoformat(str(val[0]))
            except (TypeError, ValueError):
                continue
            if dep < horizon:
                chosen.append(trip)
        if chosen:
            out[(str(vid), str(d))] = chosen
    return out


def live_departures(sol: dict, day_iso: str, timings_fn) -> dict:
    """route_times-shaped ``{"ROUTE:vid:day#Tk": (depart_iso,)}`` recomputed from
    the LIVE plan for today's keys. The last anchor's route_times cannot contain
    a trip a micro pass inserted, so without this live clock ``expire_commit``
    never sees such a trip's departure: it never launches, never gains watermark
    protection, and a later pass may re-time, move, or drop it — the board shows
    a frozen order jumping or vanishing. ``timings_fn(key, trips)`` must evaluate
    under the override the key's plan was BUILT with (never this epoch's dispatch
    floor); an evaluation failure skips the key — it just stays unlaunched."""
    out: dict = {}
    for key, trips in sol.items():
        vid, d = key
        if str(d)[:10] != day_iso:
            continue
        tt = [trips] if trips and hasattr(trips[0], "job_id") else trips
        try:
            starts, _ends, _per = timings_fn(key, tt)
        except Exception:
            continue
        for ti, st in enumerate(starts, start=1):
            if st:
                out[f"ROUTE:{vid}:{d}#T{ti}"] = (str(st),)
    return out


def suffix_locks(inflight: dict, view: dict) -> dict:
    """{job_id -> (vid, day)}: every stop of an in-flight trip is pinned to its
    vehicle — committed stops via the pinned set, suffix stops via this lock
    (onboard freight cannot change trucks; v1 defers cross-vehicle moves)."""
    locks: dict[str, tuple[str, str]] = {}
    for key, trips in inflight.items():
        for trip in trips:
            for j in trip:
                locks[j.job_id] = key
    return locks


def micro_times(anchors: list[datetime], every_min: int, day_end_hour: int = 18,
                day_start_hour: int = 6) -> list[datetime]:
    """Insertion-pass heartbeat: every ``every_min`` minutes strictly between
    decision points, within the collection booking day [06:00, 18:00). The
    ``day_start_hour`` floor (06:00) suppresses the midnight-seed window's early
    passes — with the 00:00 seed the first micro fires at 06:00, not 00:30, since
    intraday collections only start being revealed once the booking day opens.
    The 18:00 ``day_end_hour`` is the day close (no micro re-plans after it)."""
    if every_min <= 0:
        return []
    out: list[datetime] = []
    for i, a in enumerate(anchors):
        nxt = anchors[i + 1] if i + 1 < len(anchors) else None
        end = datetime(a.year, a.month, a.day, day_end_hour)
        if nxt is not None and nxt.date() == a.date():
            end = min(end, nxt)
        floor = datetime(a.year, a.month, a.day, day_start_hour)
        m = a + timedelta(minutes=every_min)
        while m < end:
            if m >= floor:
                out.append(m)
            m += timedelta(minutes=every_min)
    return sorted(set(out))


_MICRO_INSERTABLE_KINDS = {"CUSTOMER_PICKUP", "DIRECT_CUSTOMER_MOVE"}


def new_arrival_meta(order_ids: set, inputs0, day_iso: str, floor_iso: str | None = None,
                     gated: dict | None = None) -> dict:
    """JobMeta for micro-pass arrivals, built from the window-level frames (row
    content is reveal-independent). Today's COLLECTION-SIDE legs only (user
    rule 2026-07-10 / review Fix 4): a depot-loaded delivery leg means freight
    on the vehicle from departure — an anchor-planned decision, never a
    mid-day patch. Hard-blocked rows skipped.

    ``floor_iso`` (the micro's dispatch floor = now + delta) is stamped as each
    job's ``earliest_start`` — kept as the LATER of it and any real pickup window
    (user rule 2026-07-11). A micro arrival cannot be SERVED before the floor,
    which already post-dates the order's creation, so the router (which enforces
    earliest_start) never schedules the collection before it exists — the
    per-stop non-anticipation guarantee, in planning AND at emission. This is
    what stops an afternoon-booked collection landing on a morning sweep."""
    cf = inputs0.candidate_df
    rows = cf[cf["order_id"].astype(str).isin(order_ids)
              & cf["service_date"].astype(str).str.startswith(day_iso)
              & cf["leg_kind"].astype(str).isin(_MICRO_INSERTABLE_KINDS)
              & cf.get("hard_blocker", pd.Series(dtype=str)).fillna("").eq("")]
    coords = {}
    ok = inputs0.compatibility_df
    ok = ok[ok["compatibility_status"].astype(str).eq("OK")]
    elig: dict[str, list[str]] = {}
    for r in ok.itertuples(index=False):
        leg = str(getattr(r, "leg_id", ""))
        elig.setdefault(leg, []).append(str(getattr(r, "vehicle_id", "")))
        lat, lon = getattr(r, "service_lat", None), getattr(r, "service_lon", None)
        if leg not in coords and pd.notna(lat) and pd.notna(lon):
            coords[leg] = (float(lat), float(lon))
    from freight_planner.tours import is_tour_only
    meta: dict[str, _JobMeta] = {}
    for row in rows.itertuples(index=False):
        rj = make_route_job(row, coords)
        if rj is None:
            continue
        leg = str(getattr(row, "leg_id", ""))
        # Tour-only gate (2026-07-20, WT267756): the seed's far/tour-only split
        # (tour_plan B14) never guarded this one-shot insert, so a 972-km Wales
        # DIRECT was crammed into a van's daily route mid-morning. Mirror the
        # seed's classification (tractor-speed carry vs the driving cap, two-point
        # legs on the full depot->origin->dest->depot carry, PRODUCES_DEPOT_FREIGHT
        # exempt) — far jobs belong to the tour paths (seed / attach / commission).
        if str(getattr(row, "dependency_type", "")) != "PRODUCES_DEPOT_FREIGHT":
            o_lat = o_lon = None
            if str(getattr(row, "leg_kind", "")) in ("DIRECT_CUSTOMER_MOVE", "HUB_DROP"):
                ol, oo = getattr(row, "origin_lat", None), getattr(row, "origin_lon", None)
                if ol is not None and oo is not None and pd.notna(ol) and pd.notna(oo):
                    o_lat, o_lon = float(ol), float(oo)
            c = coords.get(leg)
            if c is not None and is_tour_only(
                    c[0], c[1], origin_lat=o_lat, origin_lon=o_lon,
                    pallets=float(getattr(row, "pallets", 0.0) or 0.0),
                    depot=str(getattr(row, "source_depot", ""))):
                # surfaced (jid -> order) so the micro can hand it to the tour
                # path (attach/commission) as a LAST resort, never dropped silently
                if gated is not None:
                    gated[rj.job_id] = str(getattr(row, "order_id", ""))
                continue
        es = str(getattr(row, "earliest_start", "") or "")
        if floor_iso and (not es or es < floor_iso):
            rj = replace(rj, earliest_start=floor_iso)   # can't be served before now+delta
            es = floor_iso
        if floor_iso:
            # departure floor rides ON the job: a trip carrying it may not start
            # driving before now+delta — the vehicle waits at the depot
            # (2026-07-16: lets a returned vehicle take a floored 2nd trip
            # instead of forcing a fresh activation). A LATER column-carried
            # floor (collocated depot-delivery readiness) survives: keep the max.
            df0 = str(getattr(rj, "depart_floor", "") or "")
            rj = replace(rj, depart_floor=max(df0, floor_iso) if df0 else floor_iso)
        cand = {k: getattr(row, k, "") for k in row._fields}
        cand["earliest_start"] = es
        meta[rj.job_id] = _JobMeta(rjob=rj, day=day_iso,
                                   eligible_vehicles=elig.get(leg, []),
                                   candidate=cand)
    return meta


def _classify_failed(due_iso: str, next_anchor: datetime) -> str:
    """Split a micro-pass insertion FAILURE into its disposition (thesis results
    wiring 2026-07-24). A micro pass is insertion-only: it never re-plans, so an
    arrival it could not place is not automatically lost — the NEXT re-planning
    anchor (the midday re-opt) may still route it, PROVIDED the job is still
    serviceable then.

    Rule: compare the arrival's due / window-end against the next re-planning
    anchor (``next_decision_after(now)`` — the 18:00 close never re-plans, so
    that call already skips it and returns the real next anchor).
      * due >= next_anchor  -> "deferred": a future anchor can still plan it.
      * due <  next_anchor  -> "slipped":  its serviceable window closes before
                               any anchor can act, so it can never be served
                               in-window.
    ``due_iso`` is the row's ``latest_finish`` (window end), falling back to its
    ``service_date`` — the same due signal the tour-freeze / candidate paths read
    (see line ~1120). A full timestamp is compared directly; a date-only value
    (YYYY-MM-DD) is treated as serviceable through the END of that day. An empty
    or unparseable due is treated as still-serviceable ("deferred"): the
    conservative choice, since a job is only called slipped on POSITIVE evidence
    its window has already closed."""
    if not due_iso:
        return "deferred"
    s = due_iso.strip()
    # A bare date (no time component) is serviceable through the END of that day,
    # not midnight — else a same-day due looks already-closed against a noon
    # anchor. `datetime.fromisoformat` happily parses "YYYY-MM-DD" to midnight, so
    # detect the date-only shape ourselves (len 10, no 'T'/space separator).
    if len(s) == 10 and "T" not in s and " " not in s:
        try:
            due = datetime.combine(date.fromisoformat(s), dtime(23, 59, 59))
        except ValueError:
            return "deferred"
    else:
        try:
            due = datetime.fromisoformat(s)
        except ValueError:
            return "deferred"
    return "deferred" if due >= next_anchor else "slipped"


def _assert_no_dups(sol: dict, where: str) -> None:
    """Debug tripwire (FP_DEBUG_INFEASIBLE): duplicated job ids inside one
    vehicle-day mean some stage double-placed a stop — raise AT CREATION with
    the stage named, instead of dying later at record emission."""
    if not os.environ.get("FP_DEBUG_INFEASIBLE"):
        return
    for key, trips in sol.items():
        tt = [trips] if trips and hasattr(trips[0], "job_id") else trips
        seen: dict[str, int] = {}
        for t in tt:
            for j in t:
                seen[j.job_id] = seen.get(j.job_id, 0) + 1
        dups = {k: v for k, v in seen.items() if v > 1}
        if dups:
            raise AssertionError(f"[DUP@{where}] {key}: {dups}")


def apply_commit_ctx(base: dict, ctx: dict) -> dict:
    """Committed keys' evaluation views are authoritative INCLUDING ABSENCE
    (structural review Fix 2): a day committed under no override must not
    inherit one fabricated by a later solve. Apply the pin two-way — set the
    pinned value, or DELETE the key when the pinned view is 'no override'."""
    out = dict(base)
    for k, v in ctx.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


_COLLECT_STOP_KINDS = {"customer_pickup", "direct_customer_move"}
_PICKUP_LEG_KINDS = {"CUSTOMER_PICKUP", "DIRECT_CUSTOMER_MOVE"}
# a COLLECTION runs the freight onto the vehicle (HUB_DROP = collect then hand to
# the network at the hub). Marking an order collected requires one of THESE legs to
# commit — not its paired delivery (matches the freeze / expiry sites).
_COLLECTION_LEG_KINDS = {"CUSTOMER_PICKUP", "DIRECT_CUSTOMER_MOVE", "HUB_DROP"}


def _collection_satisfying_job(j) -> bool:
    """True when this JOB's presence in the plan discharges its order's collection.
    Collection kinds always do. A collocated depot-delivery (2026-07-17) is its
    order's ONLY leg — the freight loads at the fleet's own yard at departure — so
    that delivery IS the serve event; an ordinary delivery still is not (Bug A: a
    paired delivery must never mark its rejected pickup collected). The signature
    is the :DIR job-id tail, NOT depot_bound presence: under DEPOT_PINNING every
    delivery carries a bound, and keying on it flooded the collection ledger with
    every import (A1 re-key)."""
    kind = str(getattr(j, "leg_kind", "")).upper()
    if kind in _COLLECTION_LEG_KINDS:
        return True
    if kind != "CUSTOMER_DELIVERY":
        return False
    tail = str(getattr(j, "job_id", "")).rsplit(":", 1)[-1]
    return tail.startswith("DIR")


def collection_days_in_plan(records, carryin_ids: set = frozenset()) -> dict[str, str]:
    """Orders whose COLLECTION leg (pickup / direct / hub-drop) is present in the
    EMITTED plan records. The finalize reconciles the ledger against this — an
    order is served only if its pickup actually survived into the plan, not merely
    if it was placed at some epoch then pruned at the iterations=0 finalize.

    Record order_ids carry the FREIGHT id for split parts ('uuid#S1' — record
    minting substitutes it so the FreightLedger gates per part); normalize to the
    parent, or every split order demotes NOT_IN_PLAN at the finalize despite both
    parts being planned, launched and emitted (Scenario C: seven 30-34-pal FTLs,
    2026-07-16).

    A collocated depot-delivery (2026-07-17) also counts: records don't carry
    depot_bound, but the reclassification keeps the :DIR leg id on the delivery —
    that signature is unique to depot-loaded directs (daily AND tour-side), and an
    ordinary :D/:XD delivery still never marks its order collected."""
    out: dict[str, str] = {}
    for r in records:
        kind = str(getattr(r, "leg_kind", "")).upper()
        tail = str(getattr(r, "leg_id", "")).rsplit(":", 1)[-1]
        oid = str(getattr(r, "order_id", "") or "").split("#", 1)[0]
        if kind in _COLLECTION_LEG_KINDS or (
                kind == "CUSTOMER_DELIVERY" and tail.startswith("DIR")) or (
                kind == "CUSTOMER_DELIVERY" and oid in carryin_ids):
            if oid:
                stamp = pd.to_datetime(getattr(r, "planned_arrive", None), errors="coerce")
                day = (stamp.strftime("%Y-%m-%d") if pd.notna(stamp)
                       else str(getattr(r, "service_date", "") or "")[:10])
                out.setdefault(oid, "")
                if day and (not out[oid] or day < out[oid]):
                    out[oid] = day
    return out


def collection_orders_in_plan(records, carryin_ids: set = frozenset()) -> set:
    """Backward-compatible ID view of :func:`collection_days_in_plan`."""
    return set(collection_days_in_plan(records, carryin_ids))


def collected_orders_today(current_sol: dict, day_iso: str, order_of_job: dict,
                           collect_ids: set, carryin_ids: set = frozenset()) -> set:
    """Collect orders whose COLLECTION leg committed on ``day_iso``. A collection
    order is served only when its pickup actually runs; marking it collected because
    merely its paired DELIVERY committed (whose pickup may have been rejected) is the
    ledger over-report that let orphan deliveries pass as ON_TIME (Bug A)."""
    out: set = set()
    for (vid, d2), trips in current_sol.items():
        if str(d2)[:10] != day_iso:
            continue
        for t in trips:
            for j in t:
                oid = order_of_job.get(j.job_id, "")
                if (_collection_satisfying_job(j)
                        or (oid in carryin_ids
                            and str(getattr(j, "leg_kind", "")).upper() == "CUSTOMER_DELIVERY")):
                    if oid in collect_ids:
                        out.add(oid)
    return out


def tour_planned_orders(tour_records) -> set:
    """Orders with any leg on a planned tour (frozen or not). Double-plan fix
    Door A (2026-07-20, WT267756): the seed's tour had the order but the tour
    hadn't frozen, so the 03:30 micro saw it 'unserved and visible' and inserted
    a second copy into a van's daily route — the micro must skip these."""
    return {str(getattr(r, "order_id", "")) for r in tour_records} - {""}


def _drain_staged_deliveries(window_staged: dict, delivered_committed: set) -> None:
    """Drop staged within-window FF freight whose delivery has EVER committed.

    Draining on the ACCUMULATED delivered-committed set (not on a static planned
    delivery day) is the decision-audit #4 fix: the old backstop also popped a
    collected order whose tight planned delivery day merely passed WITHOUT a committed
    delivery, so that order — already served-on-collection — vanished from visibility
    and was never re-planned while the ledger still reported it ON_TIME. Keeping it
    staged leaves it visible so the next solve re-attempts its delivery; it drains only
    once its delivery actually commits, so ``window_staged`` still cannot leak."""
    for oid in list(window_staged):
        if oid in delivered_committed:
            window_staged.pop(oid, None)


def inflight_orders_for_day(inflight: dict, order_of_job: dict, day_iso: str,
                            grace_days: int = 1) -> set:
    """Orders with an in-flight trip dated today or within ``grace_days`` before
    today.

    The candidate-row metadata for an already-committed trip is still needed the
    day after it completes: this epoch's build, and later the end-of-run
    0-iteration re-price, look injected jobs back up against THIS epoch's
    candidate_df, and a job with no matching row there silently drops out of the
    final solution -- even though the physical trip is still sitting, untouched,
    in ``inflight`` (it is never pruned there). A plain ``k[1] >= day_iso`` cutoff
    (the original 2026-07-13 state-hygiene fix) drops that protection the exact
    day after a trip's OWN day passes, and for an order with no delivery leg to
    track (LOCAL_COLLECT) there is nothing else to catch it (2026-07-29:
    W0_baseline run, split LOCAL_COLLECT orders 476bf923/f6b76b34 and same-day
    FULL_FLEET order 5658f248 all lost this way -- the general shape of the same
    gap ``committed_visibility_protection``'s ``delivered_committed`` closes for
    the delivery-tracked case). Bounded at ``grace_days`` (not unbounded) keeps
    the original fix's guarantee that a long-finished order's rows eventually
    stop being rebuilt every epoch."""
    floor_day = (date.fromisoformat(day_iso) - timedelta(days=grace_days)).isoformat()
    return {order_of_job.get(j.job_id, "")
            for k, trips in inflight.items() if str(k[1])[:10] >= floor_day
            for t in trips for j in t} - {""}


def committed_visibility_protection(inflight_orders: set, window_staged: dict,
                                    delivered_committed: set) -> set:
    """Orders whose committed work must never be excluded from ``vis``, regardless
    of calendar day.

    Three overlapping mechanisms hand a committed order's visibility off to each
    other across its lifecycle: ``window_staged`` covers it from collection until
    its delivery commits; ``inflight_orders`` covers it only while its trip's OWN
    day is still >= today (the state-hygiene fix, 2026-07-13, that stops a finished
    trip's order being re-offered forever); ``delivered_committed`` is set the
    moment the delivery commits and never shrinks. ``_drain_staged_deliveries``
    drops ``window_staged`` protection at EXACTLY the moment ``delivered_committed``
    becomes true — the same epoch ``inflight_orders`` still (correctly) covers the
    order, since the delivering trip is dated today. The NEXT day, that trip is
    "yesterday" and drops out of ``inflight_orders`` too, so BOTH mechanisms end
    their coverage on the same order at the same transition with nothing left to
    carry it — real committed, driver-promised legs silently vanished from every
    later candidate frame this way (2026-07-29, W0_baseline run, order a77a75df:
    watermark-committed pickup + delivery legs across two vehicles, never dropped
    by any drop/superseded/orphan mechanism, just never offered as a candidate
    again). ``delivered_committed`` already uniquely identifies exactly the orders
    that need to keep being protected once ``window_staged`` hands off, so folding
    it into the protected set here closes the gap without touching either
    mechanism's own drain/expiry timing."""
    return set(inflight_orders) | set(window_staged) | set(delivered_committed)


def delivered_orders_in_plan(current_sol: dict, order_of_job: dict) -> set:
    """Orders whose DELIVERY has committed in the live plan — an XDOCK
    ``CUSTOMER_DELIVERY`` leg, or a ``:DIR`` direct (which discharges both legs).
    Drains within-window staged freight (``window_staged``) once its delivery
    actually lands, so a collected FF order stops being re-offered as prestaged."""
    out: set = set()
    for trips in current_sol.values():
        for t in trips:
            for j in t:
                kind = str(getattr(j, "leg_kind", "")).upper()
                jid = str(getattr(j, "job_id", ""))
                if kind == "CUSTOMER_DELIVERY" or jid.rsplit(":", 1)[-1].startswith("DIR"):
                    oid = order_of_job.get(jid, "")
                    if oid:
                        out.add(str(oid).split("#", 1)[0])
    return out


# A frozen tour record whose freight ends in one of these states carried the order
# to the end of OUR responsibility (delivered / handed to the network / staged).
_TOUR_DONE_STATES = {"DELIVERED", "WITH_NETWORK", "AT_DEPOT"}


def tour_served_order_ids(tour_records) -> set:
    """Orders a FROZEN (committed) tour carried to their end state. The finalize
    reconcile must union these into the plan-served set: a depot-loaded direct
    emits as CUSTOMER_DELIVERY (TOUR_DEPOT_DIRECT_AS_DELIVERY), so the
    collection-leg scan alone cannot see it — such orders closed the window
    UNSERVED while the tour subsystem had delivered them (WT255892, 2026-07-16)."""
    out: set = set()
    for r in tour_records or []:
        oid = str(getattr(r, "order_id", "") or "").split("#", 1)[0]
        if oid and str(getattr(r, "freight_state_after", "")).upper() in _TOUR_DONE_STATES:
            out.add(oid)
    return out


def credit_frozen_tour_record(state: RollingState, orders_done: set, r) -> None:
    """Ledger crediting for ONE record of a tour that just froze (committed).

    DELIVERED marks the order served OUTRIGHT: keying on leg_kind alone misses
    reclassified depot-loaded directs (they emit as CUSTOMER_DELIVERY), which
    left such orders slipping at every day-close despite the delivery."""
    oid = str(getattr(r, "order_id", "") or "").split("#", 1)[0]
    st = str(getattr(r, "freight_state_after", "")).upper()
    if (oid and str(getattr(r, "job_id", "")).startswith("__DIRECT_COLLECT__:")):
        day = str(getattr(r, "service_date", ""))[:10]
        if not state.collected_day.get(oid) or day < state.collected_day[oid]:
            state.collected_day[oid] = day
        if oid in state.served:
            if not state.served_day.get(oid) or day < state.served_day[oid]:
                state.served_day[oid] = day
        else:
            state.note_served({oid}, day)
        return
    if not oid or st not in _TOUR_DONE_STATES:
        return
    orders_done.add(oid)
    day = str(getattr(r, "service_date", ""))[:10]
    if str(getattr(r, "leg_kind", "")).upper() in _COLLECTION_LEG_KINDS:
        state.collected_day.setdefault(oid, day)
    if st == "DELIVERED":
        state.collected_day.setdefault(oid, day)
        state.note_served({oid}, day)


def serviceable_collect_ids(flow_of: dict, win_orders: set, legs_df: pd.DataFrame,
                            window_start: str | None = None) -> set:
    """Collections the fleet can physically serve: a COLLECT_FLOWS order with a
    real pickup leg. Orders whose only leg is ACCOUNTING_ONLY (network- or
    subcontractor-handled — recorded for billing, never routed by us) are NOT
    fleet collection work and must not inflate the served-universe, the slip
    pool, or the ledger (smoke-9: 100 of 103 'UNSERVED' were accounting-only
    phantoms that can never be served, so the pool could never drain).

    Also out of the universe (2026-07-16):
    * MASSIVE orders — every customer leg exceeds the single-vehicle ceiling
      (MASSIVE_UNSUPPORTED): no fleet vehicle can ever carry them, so they must
      not count as unserved collections. They stay visible as blocked rows. A
      split order with a runnable part stays in.
    * Pre-window-collected orders — every pickup dated before the window, with
      an in-window delivery: the freight is already at a depot (prestaged), so
      the remaining work is the DELIVERY; collection-centric tracking would
      slip them daily despite a served delivery."""
    base = {o for o in flow_of if flow_of[o] in COLLECT_FLOWS} & set(win_orders)
    if legs_df is None or legs_df.empty or "leg_kind" not in legs_df.columns:
        return base
    lk = legs_df["leg_kind"].astype(str)
    oid = legs_df["order_id"].astype(str)
    pickup = set(legs_df.loc[lk.isin(_PICKUP_LEG_KINDS), "order_id"].astype(str))
    # Collocated depot-deliveries (2026-07-17) stay IN the universe: the DIRECT was
    # reclassified at emission, so the order has no pickup-kind leg — but it is 100%
    # in-window fleet work, and dropping it made a lost order fail SILENTLY (the
    # probe ledger shrank 453->432 when 21 such orders left the population). Keyed
    # on the :DIR leg-id identity, NOT depot_bound presence — under DEPOT_PINNING
    # every delivery is bound, and the bound-key flooded the ledger with imports.
    bound: set = set()
    if "leg_id" in legs_df.columns:
        tail = legs_df["leg_id"].astype(str).str.rsplit(":", n=1).str[-1]
        bound = set(oid[lk.eq("CUSTOMER_DELIVERY") & tail.str.startswith("DIR")])
    out = base & (pickup | bound)
    customer_kinds = {"CUSTOMER_PICKUP", "CUSTOMER_DELIVERY", "DIRECT_CUSTOMER_MOVE", "HUB_DROP"}
    if "planner_status" in legs_df.columns:
        cust = legs_df[lk.isin(customer_kinds)]
        if not cust.empty:
            all_massive = cust.groupby(cust["order_id"].astype(str))["planner_status"].agg(
                lambda s: set(str(x) for x in s) == {"MASSIVE_UNSUPPORTED"})
            out -= set(all_massive[all_massive].index)
    if window_start and "service_date" in legs_df.columns:
        out -= carryin_delivery_ids(legs_df, window_start)
    return out


def carryin_delivery_ids(legs_df: pd.DataFrame, window_start: str) -> set:
    """Orders COLLECTED before the window with an in-window DELIVERY (the
    prestaged carry-ins). Their remaining fleet work is the delivery, so they
    are excluded from collection-centric tracking — and (fix D, 2026-07-22)
    tracked separately with the DELIVERY as the service-satisfying leg, so a
    late staged delivery (the slack-run Fort William case) is no longer
    invisible to the service ledger."""
    if legs_df is None or legs_df.empty or "leg_kind" not in legs_df.columns             or "service_date" not in legs_df.columns:
        return set()
    lk = legs_df["leg_kind"].astype(str)
    oid = legs_df["order_id"].astype(str)
    sd = legs_df["service_date"].astype(str).str[:10]
    is_pick = lk.eq("CUSTOMER_PICKUP")
    pick_pre = set(oid[is_pick & (sd != "") & (sd < window_start)])
    pick_in = set(oid[is_pick & (sd >= window_start)])
    del_in = set(oid[lk.eq("CUSTOMER_DELIVERY") & (sd >= window_start)])
    return (pick_pre - pick_in) & del_in


def audit_non_anticipation(plan_dir: Path, qargo0: pd.DataFrame, runlog) -> int:
    """Fail-loud guard: NO collection may be ARRIVED AT before its order was
    created (user rule 2026-07-11). If an order was not booked yet, at that
    instant it does not exist in the universe, so sending a vehicle to its site
    is future knowledge — data leakage. The audited moment is therefore the
    drive-up, ``planned_arrive``, NOT the pickup (``planned_depart``): a vehicle
    that arrives early and merely WAITS at the curb is still parked at a site for
    freight that does not exist, which is the leak we are hunting. A 1-second
    tolerance absorbs whole-second route-timing rounding against the microsecond
    ``timestamp_created`` (else a pickup floored to a whole-second creation looks
    a few ms early). Returns the violation count; raises when
    ``FP_STRICT_CAUSALITY`` is set (CI/gate use)."""
    rs_path = plan_dir / "route_stops.csv"
    if not rs_path.exists():
        return 0
    rs = pd.read_csv(rs_path)
    if rs.empty or "stop_type" not in rs.columns:
        return 0
    created = (pd.to_datetime(qargo0.set_index(qargo0["order_id"].astype(str))["timestamp_created"],
                              errors="coerce", utc=True).dt.tz_localize(None))
    coll = rs[rs["stop_type"].astype(str).isin(_COLLECT_STOP_KINDS)].copy()
    coll["created"] = coll["order_id"].astype(str).map(created)
    # format="mixed": arrive/depart mix whole-second (wait-stops timed off the
    # creation-floored earliest_start) and sub-second values; a single inferred
    # format NaT's the mismatches.
    arrive = pd.to_datetime(coll["planned_arrive"], errors="coerce", format="mixed")
    coll["arrive"] = arrive
    bad = coll[coll["created"].notna() & arrive.notna()
               & (arrive < coll["created"] - pd.Timedelta(seconds=1))]
    n = int(len(bad))
    if n:
        top = bad["service_pc"].value_counts().head(6).to_dict() if "service_pc" in bad else {}
        runlog.log(f"!! NON-ANTICIPATION: {n} collections arrived at before their order was "
                   f"created (should be 0). top postcodes: {top}")
        if os.environ.get("FP_STRICT_CAUSALITY"):
            ex = bad.iloc[0]
            raise ValueError(
                f"non-anticipation violation: order {ex['order_id']} at {ex.get('service_pc','')} "
                f"arrived {ex['arrive']} but created {ex['created']} (+{n-1} more)")
    else:
        runlog.log("non-anticipation audit: 0 violations (every collection arrived at after booking)")
    return n


def audit_route_backdating(plan_dir: Path, tour_created_at: dict, placement: dict,
                           runlog) -> int:
    """A4 route-level guard (RULES.md gap 1, closed 2026-07-14): NO emitted stop
    may be planned in the PAST of the decision that created it. The order-level
    audit above is blind to this class — i5000's TOUR:Y888AUK:2026-01-15 was
    created at the Jan-16 00:00 midnight seed yet emitted "Jan-15 13:06" work, and passed,
    because every ORDER existed when placed. Two checks:

      * tour rows: every ``service_date`` >= the creating anchor's DAY
        (``tour_created_at``: route_id -> epoch ISO, stamped at each seed). Tours
        are born only at 00:00 midnight seeds (rule B7), so day-level is exact.
      * daily rows: ``planned_arrive`` >= the epoch that FIRST placed the job
        (``placement``: job_id -> {epoch,...}), 1-second rounding tolerance.

    Routes/legs with no provenance record are UNTRACED — logged, never counted
    as violations (the trace is best-effort for synthetic finalize artifacts).
    Returns the violation count; raises under ``FP_STRICT_CAUSALITY``."""
    rs_path = plan_dir / "route_stops.csv"
    if not rs_path.exists():
        return 0
    rs = pd.read_csv(rs_path)
    if rs.empty or "route_id" not in rs.columns:
        return 0
    is_tour = (rs["is_tour"].astype(str).str.lower().isin(("true", "1", "1.0"))
               if "is_tour" in rs.columns
               else rs["route_id"].astype(str).str.startswith("TOUR:"))

    n_tour = 0
    untraced_tours: set = set()
    tours = rs[is_tour]
    if not tours.empty:
        created_day = tours["route_id"].astype(str).map(
            {k: str(v)[:10] for k, v in (tour_created_at or {}).items()})
        untraced_tours = set(tours.loc[created_day.isna(), "route_id"].astype(str))
        bad = tours[created_day.notna()
                    & (tours["service_date"].astype(str).str[:10] < created_day)]
        n_tour = int(len(bad))
        if n_tour:
            rids = bad["route_id"].astype(str).value_counts().to_dict()
            runlog.log(f"!! ROUTE BACKDATING: {n_tour} tour stop rows dated before their "
                       f"creating seed (should be 0): {rids}")

    n_daily = 0
    place_epoch = {(str(k)[4:] if str(k).startswith("JOB:") else str(k)): str(v.get("epoch", ""))
                   for k, v in (placement or {}).items() if isinstance(v, dict)}
    daily = rs[~is_tour & rs["leg_id"].astype(str).ne("") & rs["leg_id"].notna()].copy()
    if not daily.empty:
        ep = pd.to_datetime(daily["leg_id"].astype(str).map(place_epoch), errors="coerce")
        arrive = pd.to_datetime(daily["planned_arrive"], errors="coerce", format="mixed")
        bad = daily[ep.notna() & arrive.notna()
                    & (arrive < ep - pd.Timedelta(seconds=1))]
        n_daily = int(len(bad))
        if n_daily:
            runlog.log(f"!! ROUTE BACKDATING: {n_daily} daily stops timed before the epoch "
                       f"that placed them (should be 0): "
                       f"{bad['leg_id'].astype(str).head(6).tolist()}")

    n = n_tour + n_daily
    if untraced_tours:
        runlog.log(f"route-backdating audit: {len(untraced_tours)} tour route(s) without a "
                   f"creation record (untraced, not counted): {sorted(untraced_tours)[:4]}")
    if n and os.environ.get("FP_STRICT_CAUSALITY"):
        raise ValueError(f"route backdating: {n_tour} tour rows + {n_daily} daily stops "
                         f"planned before their deciding epoch")
    if not n:
        runlog.log("route-backdating audit: 0 violations (no stop precedes its deciding epoch)")
    return n


def stop_provenance(route_stops: pd.DataFrame, created: dict,
                    placement: dict, emit_start: dict) -> pd.DataFrame:
    """Per collection-stop provenance for tracing non-anticipation (user rule
    2026-07-11: trace every placed stop back to the epoch that made it).

    Joins the FINAL ``route_stops`` with, for each collection stop:
      * ``created`` — the order's booking time (order_id -> iso), so
        ``anticipation_gap_min`` = created - ARRIVE time (minutes; >0 = arrived
        before it was booked, the leak). The drive-up is ``planned_arrive``,
        matching ``audit_non_anticipation`` — a vehicle parked at a site before
        the order exists is the leak, even if it waits to pick up (``served`` /
        ``planned_depart`` is kept as a context column);
      * the epoch / kind / floor that FIRST placed the job — from ``placement``
        (job_id -> {epoch, kind, floor}), keyed here by leg_id;
      * ``emit_start`` — the vehicle start_time emission used to TIME the route
        (route_id -> iso). If this is the raw morning availability while the job
        was placed under a mid-day floor, emission re-timed the route earlier
        than it was planned — the timing-basis class of leak.
    """
    if route_stops is None or route_stops.empty or "stop_type" not in route_stops.columns:
        return pd.DataFrame()
    coll = route_stops[route_stops["stop_type"].astype(str).isin(_COLLECT_STOP_KINDS)].copy()
    if coll.empty:
        return coll
    cser = pd.to_datetime(pd.Series(created), errors="coerce", utc=True).dt.tz_localize(None)
    place_by_leg = {(k[4:] if str(k).startswith("JOB:") else str(k)): v
                    for k, v in (placement or {}).items()}
    leg = coll["leg_id"].astype(str)
    coll["created"] = coll["order_id"].astype(str).map(cser.to_dict())
    coll["arrive"] = pd.to_datetime(coll["planned_arrive"], errors="coerce", format="mixed")
    served = (pd.to_datetime(coll["planned_depart"], errors="coerce", format="mixed")
              if "planned_depart" in coll.columns else coll["arrive"]).fillna(coll["arrive"])
    coll["served"] = served
    # gap measured at the DRIVE-UP (arrive): >0 = arrived before booking, the leak
    # (user rule 2026-07-11). ``served`` is kept as a context column.
    coll["anticipation_gap_min"] = (coll["created"] - coll["arrive"]).dt.total_seconds() / 60.0
    coll["first_epoch"] = leg.map(lambda l: (place_by_leg.get(l) or {}).get("epoch", ""))
    coll["first_kind"] = leg.map(lambda l: (place_by_leg.get(l) or {}).get("kind", ""))
    coll["floor_at_place"] = leg.map(lambda l: (place_by_leg.get(l) or {}).get("floor", ""))
    coll["emit_start"] = coll["route_id"].astype(str).map(emit_start or {}).fillna("")
    return coll


def emit_stop_provenance(plan_dir: Path, reports_dir: Path, qargo0: pd.DataFrame,
                         current_sol: dict, fin_over: dict, vrows0: dict,
                         placement: dict, runlog) -> None:
    """Write the non-anticipation trace artifacts (user rule 2026-07-11).

    ``reports/stop_provenance.csv`` — every collection stop with its creation
    time, placement provenance and emission start_time. ``non_anticipation_detail
    .csv`` — the violating subset, worst-gap first. The emission start_time is
    recomputed here EXACTLY as ``build_plan_records`` times each route (same
    ``fin_over`` override + ``_route_vehicle``), so a route emitted from the raw
    morning while its late work was planned under a mid-day floor is visible as
    ``emit_start`` < ``floor_at_place`` — the timing-basis leak, caught in the
    open. Best-effort: never breaks the run."""
    try:
        rs_path = plan_dir / "route_stops.csv"
        if not rs_path.exists():
            return
        rs = pd.read_csv(rs_path)
        created = qargo0.set_index(qargo0["order_id"].astype(str))["timestamp_created"].astype(str).to_dict()
        emit_start: dict[str, str] = {}
        for (vid, d2) in current_sol:
            vrow = vrows0.get(str(vid))
            if vrow is None:
                continue
            day_iso = str(d2)
            rv = apply_avail_override(_route_vehicle(vrow, day_iso),
                                      (fin_over or {}).get((vid, day_iso)), day_iso)
            emit_start[f"ROUTE:{vid}:{day_iso}"] = str(getattr(rv, "start_time", ""))
        prov = stop_provenance(rs, created, placement, emit_start)
        if prov.empty:
            return
        prov.to_csv(reports_dir / "stop_provenance.csv", index=False)
        # detail = the violating subset, same 1-second tolerance as
        # audit_non_anticipation (gap is created - arrive in minutes; 1 s absorbs
        # whole-second timing rounding) so the count and the CSV never disagree.
        viol = prov[prov["anticipation_gap_min"] > 1.0 / 60.0].sort_values(
            "anticipation_gap_min", ascending=False)
        if viol.empty:
            return
        viol.to_csv(reports_dir / "non_anticipation_detail.csv", index=False)
        by_kind = viol["first_kind"].value_counts().to_dict()
        runlog.log(f"NA-TRACE: {len(viol)} violators by first-placement kind={by_kind}; "
                   f"detail -> {reports_dir / 'non_anticipation_detail.csv'}")
        for _, v in viol.head(10).iterrows():
            runlog.log(f"  NA {v['order_id']} {v.get('service_pc','')} "
                       f"created={v['created']} arrive={v['planned_arrive']} "
                       f"gap={float(v['anticipation_gap_min']):.0f}m "
                       f"first={v['first_kind']}@{v['first_epoch']} "
                       f"floor={v['floor_at_place']} emit_start={v['emit_start']} "
                       f"route={v['route_id']} seq={v.get('sequence','')}")
    except Exception as exc:                  # diagnostics must never break emission
        runlog.log(f"NA-TRACE skipped ({type(exc).__name__}: {exc})")


def target_service_day(qargo0: pd.DataFrame, flow_of: dict,
                       deliver_flows: set = DELIVER_FLOWS,
                       window_start: str | None = None) -> dict:
    """Per-order date of OUR service leg, used by the loop's staleness/expiry
    test. A DELIVERY-flow order (we deliver; the NETWORK collected it, days
    earlier) is targeted by its DELIVERY date; every other order by its
    origin/collection date.

    Judging staleness by ``origin_date`` for imports was a dynamic-only
    regression: the network's collection predates our delivery day, so the
    expiry filter marked every import stale and stripped it from the visible set
    before the seed — dropping ALL import deliveries. The static planner had no
    such filter, which is why deliveries used to seed correctly.

    The same disease one flow over (2026-07-16): a FULL_FLEET order COLLECTED
    before the window opened, delivered inside it, is already at a depot (the
    prestaged-delivery machinery unblocks its D leg) — but the origin-date
    target expired it at the FIRST anchor, before any seed could place the
    delivery. With ``window_start`` given, such orders target their delivery
    date. Fully-historic orders (delivery also pre-window) keep the origin date
    and expire as before."""
    oid = qargo0["order_id"].astype(str)
    origin = pd.to_datetime(qargo0["origin_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    dest = pd.to_datetime(qargo0.get("destination_date"), errors="coerce").dt.strftime("%Y-%m-%d")
    out: dict = {}
    for o, od, dd in zip(oid, origin, dest):
        if flow_of.get(o) in deliver_flows and isinstance(dd, str) and dd:
            out[o] = dd
        elif (window_start and flow_of.get(o) == "FULL_FLEET"
              and isinstance(dd, str) and dd and isinstance(od, str) and od
              and od < window_start <= dd):
            out[o] = dd                      # pre-window collected: our leg is the delivery
        else:
            out[o] = od
    return out


def dispatch_times(sol: dict, route_times: dict) -> dict:
    """job_id -> its trip's depot-departure ISO, read from the live plan's route
    times. Feeds ``imminence_weights`` so the disturbance penalty weights a
    soon-to-dispatch job more than a far-future one (spec 2026-07-11 §5)."""
    out: dict = {}
    for (vid, day), trips in sol.items():
        tt = [trips] if trips and hasattr(trips[0], "job_id") else trips
        for t_idx, trip in enumerate(tt):
            val = (route_times.get(f"ROUTE:{vid}:{day}#T{t_idx + 1}")
                   or (route_times.get(f"ROUTE:{vid}:{day}") if len(tt) == 1 else None))
            dep = str(val[0]) if val else ""
            for j in trip:
                out[j.job_id] = dep
    return out


def _vehicle_meta_map(vehicle_df: pd.DataFrame) -> dict:
    out = {}
    for r in vehicle_df.itertuples(index=False):
        out[str(r.vehicle_id)] = _VehicleMeta(
            vehicle_id=str(r.vehicle_id), home_depot=str(getattr(r, "home_depot", "")),
            lat=float(getattr(r, "current_lat", 0.0) or 0.0),
            lon=float(getattr(r, "current_lon", 0.0) or 0.0),
            available_from=str(getattr(r, "available_from", "") or ""),
            shift_end=str(getattr(r, "shift_end", "") or ""),
            capacity_pallets=float(getattr(r, "capacity_pallets", 0.0) or 0.0),
            capacity_kg=float(getattr(r, "capacity_kg", 0.0) or 0.0),
            vehicle_type=str(getattr(r, "vehicle_type", "") or ""),
            catchment_km=float(getattr(r, "catchment_km", 0.0) or 0.0),
        )
    return out


# --------------------------------------------------------------------------
# the orchestration loop (testable core; see test_dynamic_e2e.py)
# --------------------------------------------------------------------------

@dataclass
class LoopCfg:
    start: date
    end: date
    epoch_times: tuple
    delta_r1_min: int = 90
    micro_every_min: int = 60
    strict: bool = False
    trace: bool = False
    reports_dir: Path | None = None
    beta: float = 0.0                         # dynamic v2 stability weight (Phase 3 CLI)


@dataclass
class LoopCtx:
    """Window-level context + injectable stages. Tests script ``solve_fn`` /
    ``build_fn`` to drive the whole orchestration on a toy universe."""
    base: dict
    inputs0: object
    qargo0: pd.DataFrame
    flow_of: dict
    meta0: pd.DataFrame
    exempt: set
    target0: dict
    win_orders: set
    collect_ids: set
    vrows0: dict
    vmeta0: dict
    leg_of_job: dict
    order_of_job: dict
    # pre-window-collected orders tracked by their DELIVERY (fix D, 2026-07-22)
    carryin_ids: set = frozenset()
    build_fn: object = build_window_inputs
    solve_fn: object = solve_window
    reopt_fn: object = reoptimize_window
    trunk_fn: object = day_close_trunk


_TOUR_ATTACH_FAR = {"NO_FEASIBLE_ROUTE", "NO_FEASIBLE_TOUR", "NO_OK_VEHICLE_PAIR",
                    # micro-originated (2026-07-20): geometry-gated arrivals and
                    # regular-insertion failures — anchor solves never emit these
                    "TOUR_ONLY", "MICRO_INSERT_FAILED"}


def _tour_attach_candidates(order_ids: set, inputs0, day_iso: str):
    """Build (Candidate list, meta-by-order) for today's unassigned far orders — one
    collection leg each, coords from the compatibility frame (spec 2026-07-12)."""
    from freight_planner.shared.config import DEPOT_ANCHORS
    from freight_planner.route_costs import road_km
    from freight_planner.tour_attach import Candidate
    from freight_planner.tours import nearest_depot
    cf = inputs0.candidate_df
    rows = cf[cf["order_id"].astype(str).isin(order_ids)
              & cf["leg_kind"].astype(str).isin(_COLLECTION_LEG_KINDS)
              & cf.get("hard_blocker", pd.Series(dtype=str)).fillna("").eq("")]
    ok = inputs0.compatibility_df
    ok = ok[ok["compatibility_status"].astype(str).eq("OK")]
    coords: dict = {}
    for r in ok.itertuples(index=False):
        leg = str(getattr(r, "leg_id", ""))
        lat, lon = getattr(r, "service_lat", None), getattr(r, "service_lon", None)
        if leg not in coords and pd.notna(lat) and pd.notna(lon):
            coords[leg] = (float(lat), float(lon))
    cands: list = []
    meta_by_order: dict = {}
    seen: set = set()
    for row in rows.itertuples(index=False):
        oid = str(getattr(row, "order_id", ""))
        if oid in seen:
            continue
        rj = make_route_job(row, coords)
        if rj is None:
            continue
        seen.add(oid)
        due = str(getattr(row, "latest_finish", "") or getattr(row, "service_date", "") or day_iso)[:10]
        # A multi-day DIRECT row is dated/windowed by its DELIVERY promise, but
        # physically starts with a collection.  In the dynamic pipeline that
        # collection becomes possible on the booking day (creation_floor), not
        # at the next day's destination-window opening.  Using earliest_start
        # here forced WT269897's whole direct carry onto its delivery day.
        ready_value = (
            getattr(row, "creation_floor", "")
            if str(rj.leg_kind) == "DIRECT_CUSTOMER_MOVE" else ""
        )
        try:
            if pd.isna(ready_value):
                ready_value = ""
        except (TypeError, ValueError):
            pass
        ready = str(ready_value or getattr(row, "earliest_start", "")
                    or getattr(row, "service_date", "") or day_iso)
        dep, _ = nearest_depot(rj.lat, rj.lon)
        dlat, dlon = DEPOT_ANCHORS.get(dep, DEPOT_ANCHORS["CB22"])
        cands.append(Candidate(oid, rj, due, ready,
                               standalone_km=2.0 * road_km(dlat, dlon, rj.lat, rj.lon),
                               target_depot=str(getattr(row, "target_depot", "") or ""),
                               depot_bound=str(getattr(row, "depot_bound", "") or "")))
        service_pc = str(getattr(row, "service_pc", ""))
        origin_pc = str(getattr(row, "origin_pc", "") or "")
        meta_by_order[oid] = {
            "leg_id": str(getattr(row, "leg_id", "")),
            "order_id": oid,
            "preferred_start_node": (
                origin_pc if str(rj.leg_kind) == "DIRECT_CUSTOMER_MOVE" and origin_pc
                else service_pc
            ),
            "preferred_end_node": service_pc,
        }
    return cands, meta_by_order


def _tour_commission(cands, meta_by_order, now, wm, day_iso, current_sol, vrows0,
                     state, merged_tours, merged_tour_records, frozen_tour_rids,
                     orders_done, micro_log, tour_created_at=None) -> None:
    """Fresh-tour commissioning (1b, 2026-07-16): for far orders attach could not
    ride, pick an idle capable vehicle, evaluate a one-order tour from its REAL
    home with day-1 starting at the dispatch floor, and commit the dispatch NOW —
    frozen immediately (a phoned driver is a commitment), credited via the tour
    path, its span reserved so no later anchor double-books the vehicle."""
    from freight_planner.routing_adapter import RouteVehicle
    from freight_planner.tour_attach import commission_intraday
    from freight_planner.tour_plan import TourAssignment
    busy = {str(k[0]) for k in current_sol if str(k[1])[:10] == day_iso}
    busy |= {str(v) for (v, d) in state.reserved if str(d)[:10] == day_iso}
    busy |= {str(ta.vehicle_id) for ta in merged_tours}   # any tour vehicle: conservative
    idle: list = []
    for vid, r in vrows0.items():
        if str(vid) in busy:
            continue
        try:
            idle.append(RouteVehicle(
                vehicle_id=str(vid), start_node=str(getattr(r, "home_depot", "")),
                start_lat=float(getattr(r, "current_lat")), start_lon=float(getattr(r, "current_lon")),
                start_time=f"{day_iso} {_fp_cfg.TOUR_DAY_START_HOUR:02d}:00:00",
                capacity_pallets=float(getattr(r, "capacity_pallets", 0.0)),
                capacity_kg=float(getattr(r, "capacity_kg", 0.0)),
                vehicle_type=str(getattr(r, "vehicle_type", "")),
                home_depot=str(getattr(r, "home_depot", "")),
                home_lat=float(getattr(r, "current_lat")), home_lon=float(getattr(r, "current_lon"))))
        except (TypeError, ValueError):
            continue
    if not idle:
        return
    day0 = datetime.fromisoformat(f"{day_iso} 00:00:00") + timedelta(
        hours=int(_fp_cfg.TOUR_DAY_START_HOUR))
    floor_minute = max(0.0, (wm.floor(now) - day0).total_seconds() / 60.0)
    for c in commission_intraday(cands, idle, day_iso, floor_minute, meta_by_order):
        # Multi-day guard (structural review, 2026-08-03): the `busy` set above
        # only checks TODAY's current_sol slice, since it is computed before the
        # tour's actual length is known. A tour whose evaluated span reaches a
        # later day can collide with ordinary daily work already sitting in
        # current_sol for that later date (assigned by an earlier anchor) — the
        # slower --travel-slack stress runs make multi-day tours common enough
        # to hit this (T888GNW/2026-02-17, W0_R2_slack_speed_minus_30pct_s0).
        # Re-check every day the tour will actually span before committing;
        # skip the candidate rather than let _assert_no_mixed_tour_daily_keys
        # crash the run downstream on a collision that was already knowable.
        base = date.fromisoformat(c.start_date)
        span_days = {(base + timedelta(days=k)).isoformat() for k in range(int(c.evaluation.days))}
        vehicle_daily_days = {str(key[1])[:10] for key, trips in current_sol.items()
                              if str(key[0]) == c.vehicle_id and trips}
        if span_days & vehicle_daily_days:
            micro_log.append({"epoch": now.isoformat(sep=" "), "kind": "tour_commission_skipped",
                              "order_id": c.order_id, "vehicle_id": c.vehicle_id,
                              "reason": "multi-day span collides with existing daily plan"})
            continue
        rid = f"TOUR:{c.vehicle_id}:{c.start_date}"
        if tour_created_at is not None:
            # the backdating audit traces every tour to the epoch that decided it;
            # commissioned tours are minted here, outside the anchor stamping loop
            tour_created_at[rid] = now.isoformat(sep=" ")
        merged_tours.append(TourAssignment(c.vehicle_id, c.start_date, c.evaluation.days,
                                           c.jobs, c.evaluation,
                                           depot=str(getattr(vrows0[c.vehicle_id], "home_depot", ""))))
        merged_tour_records.extend(c.records)
        frozen_tour_rids.add(rid)
        for r in c.records:
            credit_frozen_tour_record(state, orders_done, r)
        for k in range(int(c.evaluation.days)):
            state.reserved.add((c.vehicle_id, (base + timedelta(days=k)).isoformat()))
        micro_log.append({"epoch": now.isoformat(sep=" "), "kind": "tour_commission",
                          "order_id": c.order_id, "tour_id": rid,
                          "days": int(c.evaluation.days),
                          "km": round(float(c.evaluation.total_km), 1)})


def _tour_attach_step(now, wm, result, merged_tours, merged_tour_records,
                      inputs0, order_of_job, day_iso, state, micro_log,
                      current_sol=None, vrows0=None, frozen_tour_rids=None,
                      orders_done=None, tour_created_at=None) -> None:
    """Intraday tour attachment (spec 2026-07-12): free-ride today's unassigned far
    orders onto in-flight tours' mutable tails. Mutates ``merged_tour_records`` and the
    ledger state in place; logs each ride. The committed head is never touched (the
    frontier split reuses ``committed_stop_count``), and no tour gains a day.
    Orders no tail can take fall through to FRESH-TOUR COMMISSIONING (1b) when the
    loop context is supplied and ``TOUR_COMMISSION_ENABLED``."""
    from freight_planner.tour_attach import (
        apply_attachments, attach_intraday, build_inflight_tour)
    from freight_planner.tour_plan import _proto_vehicle
    rej = list(getattr(result.imp, "remaining_rejected", []) or []) + list(
        getattr(getattr(result, "seed", None), "rejected", []) or [])
    order_ids = {order_of_job.get(str(getattr(rj, "job_id", "")))
                 for rj in rej if str(getattr(rj, "reason", "")) in _TOUR_ATTACH_FAR}
    order_ids = {o for o in order_ids
                 if o and o not in state.served and o not in state.collected_day}
    # Double-plan fix (2026-07-20, WT269519/WT269520): the rejected list above is
    # the SOLVER's — stale when a post-ALNS pass (merge sweep / micro) has since
    # inserted the order into the live daily plan. At one 12:00 warm anchor both
    # orders were planned on N88RNW AND commissioned onto fresh one-stop "tours".
    # An order with a job in current_sol must neither attach nor commission.
    if current_sol:
        _in_plan = {order_of_job.get(str(getattr(j, "job_id", "")))
                    for trips in current_sol.values() for t in trips for j in t}
        order_ids -= _in_plan
    if not order_ids:
        return
    cands, meta_by_order = _tour_attach_candidates(order_ids, inputs0, day_iso)
    if not cands:
        return
    atts: list = []
    if _fp_cfg.TOUR_ATTACH_ENABLED:
        recs_by_route: dict = {}
        for r in merged_tour_records:
            recs_by_route.setdefault(str(r.route_id), []).append(r)
        tours: list = []
        due_by_job: dict = {}
        for ta in merged_tours:
            rid = f"TOUR:{ta.vehicle_id}:{ta.start_date}"
            it = build_inflight_tour(ta, recs_by_route.get(rid, []),
                                     _proto_vehicle(getattr(ta, "depot", "") or "CB22", str(ta.start_date)))
            if it is not None:
                tours.append(it)
            base = date.fromisoformat(str(ta.start_date)[:10])
            for s in ta.evaluation.stops:        # a stop may not move to a later day than planned
                due_by_job.setdefault(s.job_id, (base + timedelta(days=int(s.day_index))).isoformat())
        if tours:
            atts = attach_intraday(cands, tours, wm.floor(now), due_by_job)
    if atts:
        merged_tour_records[:] = apply_attachments(atts, merged_tour_records, meta_by_order)
        for att in atts:
            oid = att.order_id
            state.collected_day.setdefault(oid, att.resume_date.isoformat())
            state.note_served({oid}, day=att.resume_date.isoformat())
            micro_log.append({"epoch": now.isoformat(sep=" "), "kind": "tour_attach",
                              "order_id": oid, "tour_id": att.tour_id,
                              "added_km": round(float(att.added_km), 1)})
    if (_fp_cfg.TOUR_COMMISSION_ENABLED and current_sol is not None and vrows0
            and frozen_tour_rids is not None and orders_done is not None):
        attached = {a.order_id for a in atts}
        remaining = [c for c in cands if c.order_id not in attached]
        if remaining:
            _tour_commission(remaining, meta_by_order, now, wm, day_iso, current_sol,
                             vrows0, state, merged_tours, merged_tour_records,
                             frozen_tour_rids, orders_done, micro_log,
                             tour_created_at=tour_created_at)


def run_dynamic_loop(cfg: LoopCfg, ctx: LoopCtx, runlog) -> dict:
    start, end, epoch_times = cfg.start, cfg.end, cfg.epoch_times
    base = ctx.base
    inputs0, qargo0 = ctx.inputs0, ctx.qargo0
    flow_of, meta0, exempt = ctx.flow_of, ctx.meta0, ctx.exempt
    target0, win_orders, collect_ids = ctx.target0, ctx.win_orders, ctx.collect_ids
    vrows0, vmeta0 = ctx.vrows0, ctx.vmeta0
    leg_of_job, order_of_job = ctx.leg_of_job, ctx.order_of_job

    state = RollingState(window_start=start, window_end=end)
    wm = Watermarks(delta_r1_min=cfg.delta_r1_min, strict=cfg.strict)
    inflight: dict = {}                       # (vid, day) -> committed trips (open suffixes)
    timings: dict = {}                        # (vid, day) -> (depot_depart, per-trip stop times)
    inflight_ctx: dict = {}                   # (vid, day) -> avail override IN FORCE AT COMMIT
                                              # (crash 6: the evaluation context a day was
                                              # built under is part of its commitment)
    orders_done: set[str] = set()             # tour-served + expired (never re-enter candidates)
    merged_tours: list = []
    merged_tour_records: list = []
    merged_trunk_nights: list = []
    trunk_shortfalls: list = []
    frozen_tour_rids: set[str] = set()
    last_tour_records: list = []          # the last SEED's tour records/assignments —
    last_seed_tours: list = []            # warm anchors return none; freezing must keep
                                          # working between seeds (Fix 9)
    micro_log: list[dict] = []
    micro_failed_today: set = set()      # orders only micros saw and failed — label the
                                          # ledger honestly instead of UNKNOWN (2026-07-16)
    current_sol: dict = {}
    last_solve = None
    last_inputs = None
    last_overrides: dict = {}
    churn_rows: list[dict] = []
    # Whole-day ALNS work (audit #8, 2026-07-26): each anchor epoch's improve_existing_
    # solution call does REAL search (seed at 03:00, warm-reopt at 12:00, ...), but only the
    # LAST anchor's stats survived to `last_solve`, and main()'s finalize then discarded even
    # those behind a fresh iterations=0 re-price -- the console summary always read "accepted
    # moves: 0" regardless of how much real work the day's epochs did. Accumulate across every
    # anchor so the caller can report the whole day, not just the final no-op re-price.
    total_accepted_moves = 0
    total_inserted_jobs = 0
    total_iterations_run = 0
    first_km_before: float | None = None
    # same bug class (2026-07-28): cost_before/served_before need the identical fix --
    # the finalize re-price is a 0-iteration no-op, so ITS cost_before trivially equals
    # its cost_after (and served_before its served_after). Keep the day's true starting
    # cost/coverage from the FIRST anchor epoch, same as km_before above.
    first_cost_before: float | None = None
    first_served_before: int | None = None
    # Within-window multi-day FF delivery (2026-07-20): a FULL_FLEET order collected on
    # day N must have its delivery planned on day N+1 IN this window, not deferred to the
    # next window's handover. FULL_FLEET is a COLLECT_FLOW, so collection marks it
    # served and drops it from vis (line ~1481) — stranding the delivery. Track collected-
    # but-undelivered FF orders + the depot their freight rests at; each epoch thread them
    # as extra_staged (-> delivery-only PRESTAGED_DELIVERY candidate) and keep them visible
    # until their delivery commits (drained via delivered_orders_in_plan). Bounded by the
    # collected-undelivered FF count and drains on delivery, so it cannot run vis away.
    window_staged: dict[str, tuple[str, str]] = {}
    # Orders whose delivery has EVER committed (inflight is transient — a delivery that
    # executed then left inflight would otherwise stop matching, which is why the old
    # drain fell back to a static delivery-day backstop that also lost never-committed
    # freight, audit #4). Accumulates across days; window_staged drains only on this.
    delivered_committed: set = set()
    stage_depot_of: dict[str, str] = {}   # order -> depot its XDOCK freight rests at (pickup source)
    deliver_day_of: dict[str, str] = {}   # order -> its XDOCK delivery service day (multi-day gate)
    if inputs0.candidate_df is not None and not inputs0.candidate_df.empty:
        _cf = inputs0.candidate_df
        for _r in _cf[_cf["leg_kind"].astype(str).eq("CUSTOMER_PICKUP")].itertuples(index=False):
            _oid = str(getattr(_r, "order_id", "")).split("#", 1)[0]
            if not _oid or _oid in stage_depot_of:
                continue
            for _c in ("source_depot", "target_depot"):
                _d = str(getattr(_r, _c, "") or "")
                if _d and _d.lower() != "nan":
                    stage_depot_of[_oid] = _d
                    break
        for _r in _cf[_cf["leg_kind"].astype(str).eq("CUSTOMER_DELIVERY")].itertuples(index=False):
            _oid = str(getattr(_r, "order_id", "")).split("#", 1)[0]
            _sd = str(getattr(_r, "service_date", ""))[:10]
            if _oid and _sd and _oid not in deliver_day_of:
                deliver_day_of[_oid] = _sd
    snapshot_rows: list[dict] = []       # per-epoch plan snapshots (spec 2026-07-12)
    prev_uncommitted: dict[str, tuple[str, str]] = {}
    dispatch_floor_ov: dict = {}         # (vid, day) -> depot-departure floor for IDLE
                                         # vehicles at THIS epoch (Fix 2b wiring 2026-07-13):
                                         # a plan made now cannot dispatch a not-yet-departed
                                         # truck to have left before the floor. Recomputed each
                                         # epoch; applied to the micro solve, the warm anchor,
                                         # AND the snapshot emission so all three agree.
    micro_ctx: dict = {}                 # (vid, day) -> the availability override a MICRO
                                         # insertion accepted the key's plan under (its build
                                         # context). Launching that trip must carry THIS ctx
                                         # into inflight_ctx — last_overrides is the anchor's
                                         # view and re-times the trip from the wrong baseline.
                                         # Cleared at every anchor (fresh solve, fresh ctx).

    placement: dict[str, dict] = {}           # job_id -> FIRST placement provenance
                                              # (epoch/kind/floor/key/pos) for the
                                              # non-anticipation trace (user rule 2026-07-11)
    tour_created_at: dict[str, str] = {}      # tour route_id -> the seed epoch that (last)
                                              # wrote it — the deciding epoch for the A4
                                              # route-backdating audit (gap 1). Frozen tours
                                              # keep the stamp of the seed they froze from.
    ever_committed_legs: set[str] = set()     # leg_id -> EVER watermark-committed (locked to
                                              # a driver) at any epoch, across the whole day.
                                              # Fed to drop_superseded_option_legs so the final
                                              # DIRECT/XDOCK backstop never silently reassigns an
                                              # already-promised job (2026-07-28, see ledger.py).
    # job_id -> (option_set, option_group), for every DIRECT/XDOCK option leg in the
    # window. Fed to insertion_pass so a MICRO arrival can never pick up the rival
    # group of an option_set already resolved by an EARLIER epoch (2026-07-28,
    # R888GNW/2026-02-02: the same freight ended up with both a committed DIRECT
    # collect leg and a brand-new XDOCK pickup, since they are different job_ids and
    # the exact-job_id `served_jobs` filter below does not see the relationship).
    option_index: dict[str, tuple[str, str]] = {}
    if {"leg_id", "option_set", "option_group"} <= set(inputs0.candidate_df.columns):
        _cdf = inputs0.candidate_df
        option_index = {
            f"JOB:{lid}": (oset, ogrp)
            for lid, oset, ogrp in zip(_cdf["leg_id"].astype(str),
                                       _cdf["option_set"].astype(str),
                                       _cdf["option_group"].astype(str))
            if oset
        }

    def _snapshot(sol, epoch_iso, kind, wm_view):
        # persist the live plan for the day being dispatched — what the drivers would
        # be sent at this epoch. Reuses stop_timings with the SAME override the solve
        # ran under (day_iso / view from the enclosing loop iteration).
        today = {k: v for k, v in sol.items() if str(k[1])[:10] == day_iso}
        _rows = plan_snapshot_rows(
            today, epoch_iso, kind, order_of_job,
            lambda key, tt: trip_timings(vrows0[key[0]], key, tt, override=_commit_ctx(key)),
            view=wm_view)
        snapshot_rows.extend(_rows)
        for _row in _rows:
            if _row.get("committed"):
                _jid = str(_row.get("job_id", ""))
                ever_committed_legs.add(_jid[4:] if _jid.startswith("JOB:") else _jid)

    def _track(sol: dict, epoch_iso: str, kind: str, floor_dt) -> None:
        fl = f"{floor_dt:%Y-%m-%d %H:%M}" if floor_dt is not None else ""
        for (vid, d2), trips in sol.items():
            tt = [trips] if trips and hasattr(trips[0], "job_id") else trips
            for ti, trip in enumerate(tt, start=1):
                for pos, j in enumerate(trip, start=1):
                    placement.setdefault(j.job_id, {
                        "epoch": epoch_iso, "kind": kind, "floor": fl,
                        "vid": str(vid), "day": str(d2), "trip": ti, "pos": pos})

    def _commit_ctx(key):
        # committed vehicles: the exact override the day was built under (never re-floored,
        # their departure is history). Idle vehicles: THIS epoch's dispatch floor so the
        # snapshot re-times to a reachable departure, not the 06:00 profile / a back-calc past.
        if key in inflight_ctx:
            return inflight_ctx[key]
        return dispatch_floor_ov.get(key) or (last_overrides or {}).get(key)

    def _built_ctx(key):
        # the override a key's CURRENT plan was accepted under — inflight ctx, else the
        # micro floor captured when an insertion touched it, else the last anchor's view.
        # NEVER this epoch's dispatch floor: that floor governs planning NEW trips, and
        # re-timing an already-planned imminent departure with it breaks the evaluation
        # (the trip departs INSIDE the floor precisely when it is about to launch).
        if key in inflight_ctx:
            return inflight_ctx[key]
        return micro_ctx.get(key) or (last_overrides or {}).get(key)

    def _freeze_due_tours(horizon) -> None:
        # Tours freeze the moment their first departure enters the commit horizon —
        # at ANY decision, from the last seed's records (Fix 9, 2026-01-14: the
        # anchor-only check made every honest tour miss its window; only the
        # Fix-8 backdating bug ever froze one, because "yesterday" beat any horizon).
        firsts = _tour_first_departs(last_tour_records)
        newly_frozen = {rid for rid, dep in firsts.items()
                        if dep < horizon and rid not in frozen_tour_rids}
        if not newly_frozen:
            return
        frozen_tour_rids.update(newly_frozen)
        recs = [r for r in last_tour_records if str(r.route_id) in newly_frozen]
        merged_tour_records.extend(recs)
        for r in recs:
            credit_frozen_tour_record(state, orders_done, r)
        for ta in last_seed_tours:
            rid = f"TOUR:{ta.vehicle_id}:{ta.start_date}"
            if rid in newly_frozen:
                merged_tours.append(ta)

    def _leg_of(jid: str) -> str:
        # Derive the leg from the job id itself (structurally "JOB:<leg>") so
        # synthetic jobs absent from the window candidate frame can never
        # silently fall out of an exclusion or the trunk dock list.
        got = leg_of_job.get(jid)
        if got:
            return got
        s = str(jid)
        return s[4:] if s.startswith("JOB:") else s

    anchors = epoch_grid(start, end, times=epoch_times)
    micros = micro_times(anchors, cfg.micro_every_min)
    # Fix 6 (user rule 2026-07-10): the day close is a real 18:00 moment AFTER
    # the day's last micro-pass — tonight's ONE consolidated trunk list keeps
    # absorbing committed collections until the loading cutoff (vehicles are
    # back by the 18:00 shift end; 18:00->departure is the loading window).
    closes = [datetime.combine(d, dtime(DAY_CLOSE_HOUR, 0))
              for d in sorted({a.date() for a in anchors})]
    decisions = sorted([(a, "anchor") for a in anchors]
                       + [(m, "micro") for m in micros]
                       + [(c, "close") for c in closes])

    def next_decision_after(t: datetime) -> datetime:
        # the close never re-plans: horizons look through it to the next
        # PLAN-CHANGING decision, so the day's last micro launches everything
        # still departing today.
        for d, kind in decisions:
            if d > t and kind != "close":
                return d
        return datetime.combine(end + timedelta(days=1), epoch_times[0])

    for di, (now, kind) in enumerate(decisions):
        day = now.date()
        day_iso = day.isoformat()
        horizon = next_decision_after(now) + timedelta(minutes=cfg.delta_r1_min)
        view = advance_watermarks(wm, inflight, timings, now)
        locks = suffix_locks(inflight, view)
        floor = wm.floor(now)

        # Fix 2b wiring (2026-07-13): floor every IDLE (not-yet-departed) vehicle's depot
        # departure at the commit floor, ONCE, so the micro solve, the warm anchor, and the
        # snapshot emission all agree a fresh trip cannot leave before now + delta_R1.
        # In-flight vehicles are governed by their committed context + floor_ok — skipped.
        dispatch_floor_ov.clear()
        for _vid, _vrow in vrows0.items():
            _dk = (_vid, day_iso)
            if _dk in inflight:
                continue
            _prof = str(getattr(_vrow, "available_from", "") or "")[11:19] or f"{_fp_cfg.FLEET_DAY_START_HOUR:02d}:00:00"
            _fl = dispatch_floor(None, floor, day_iso, profile_start=f"{day_iso} {_prof}")
            if _fl is not None:
                dispatch_floor_ov[_dk] = _fl

        if kind == "micro":
            # ---- R2: one-shot insertion of arrivals since the last decision ----
            did_insert = False
            vis_now = visible_order_ids(meta0, now, exempt)
            # Door C (val1, WT267756): merged_tour_records holds only FROZEN
            # tours — the seed's fresh tours live in last_tour_records until
            # they freeze, and the 03:30 micro re-commissioned a second tour
            # for an order the 00:00 midnight seed had already tour-planned. Union both.
            _on_tours = (tour_planned_orders(merged_tour_records)
                         | tour_planned_orders(last_tour_records))
            fresh = {o for o in vis_now
                     if o not in state.served and o not in orders_done
                     and o not in _on_tours          # already rides a planned tour
                     and o in win_orders}
            served_jobs = {j.job_id for trips in current_sol.values()
                           for t in ([trips] if trips and hasattr(trips[0], "job_id") else trips)
                           for j in t}
            gated: dict[str, str] = {}
            nm = {jid: m for jid, m in
                  new_arrival_meta(fresh, inputs0, day_iso, floor.isoformat(sep=" "),
                                   gated=gated).items()
                  if jid not in served_jobs}
            fail: list[str] = []
            if nm and current_sol:
                excl = {k for k in current_sol
                        if cfg.strict and k in inflight}
                # Frozen tours are outside ``current_sol`` but consume their
                # vehicle for every day in the tour span. Anchor solves receive
                # these keys through ``external_reserved`` below; micro-passes
                # must enforce the same reservation or a newly booked order can
                # open a simultaneous daily route on the touring vehicle.
                excl |= {
                    (str(ta.vehicle_id),
                     (date.fromisoformat(str(ta.start_date))
                      + timedelta(days=k)).isoformat())
                    for ta in [*merged_tours, *last_seed_tours]
                    for k in range(int(ta.days))
                }
                merged_av = apply_commit_ctx(last_overrides or {}, inflight_ctx)
                merged_av = {**merged_av, **dispatch_floor_ov}   # Fix 2b: floor idle vehicles at the micro
                _micro_t0 = _time.monotonic()                    # per-micro-pass wall-clock (thesis results)
                sol2, ins, fail = insertion_pass(
                    current_sol, nm, vmeta0, excluded=excl,
                    avail_overrides=merged_av or None,
                    watermarks=view, commit_floor=floor, now=now, locked_keys=locks,
                    option_index=option_index or None)
                _micro_wall_s = round(_time.monotonic() - _micro_t0, 3)
                if ins:
                    before = current_sol
                    current_sol = sol2
                    did_insert = True
                    _assert_no_dups(current_sol, f"micro-insert@{now:%a %H:%M}")
                    _assert_no_mixed_tour_daily_keys(
                        current_sol,
                        [*merged_tour_records, *last_tour_records],
                    )
                    _track(current_sol, now.isoformat(sep=" "), "micro", floor)
                    # record each touched key's BUILD context: when its trip later
                    # launches it must re-time under the view THIS acceptance ran
                    # with — last_overrides is the anchor's and re-times it wrong.
                    for k2, v2 in current_sol.items():
                        if v2 != before.get(k2):
                            micro_ctx[k2] = merged_av.get(k2)
                    # collected == a COLLECTION leg was inserted (not merely a paired
                    # delivery of a collect-flow order): else an order whose delivery
                    # micro-inserts but whose pickup never runs is falsely ON_TIME (Bug A).
                    kind_of = {j.job_id: str(getattr(j, "leg_kind", "")).upper()
                               for tr in current_sol.values() for t in tr for j in t}
                    for jid in ins:
                        oid = order_of_job.get(jid)
                        if (oid and flow_of.get(oid) in COLLECT_FLOWS
                                and kind_of.get(jid, "") in _COLLECTION_LEG_KINDS):
                            state.collected_day.setdefault(oid, day_iso)
                # Split failed into its dispositions (thesis results 2026-07-24):
                # the midday re-opt can still serve a late arrival whose window has
                # not yet closed (deferred_to_midday); one past its serviceable
                # window can never be served in-window (slipped). Classified over
                # the SAME `fail` list the count is taken from -> the two always sum
                # to `failed`. `next_decision_after` skips the non-planning close.
                _next_anchor = next_decision_after(now)
                _deferred = _slipped = 0
                for _jid in fail:
                    _cand = getattr(nm.get(_jid), "candidate", {}) or {}
                    _due = str(_cand.get("latest_finish", "")
                               or _cand.get("service_date", "") or "")
                    if _classify_failed(_due, _next_anchor) == "deferred":
                        _deferred += 1
                    else:
                        _slipped += 1
                micro_log.append({"at": now.isoformat(sep=" "), "arrivals": len(nm),
                                  "inserted": len(ins), "failed": len(fail),
                                  "deferred_to_midday": _deferred, "slipped": _slipped,
                                  "wall_s": _micro_wall_s})
            # ---- last resort: micro tour path (2026-07-20 user rule) ----
            # A micro may open a fresh TOUR only for arrivals no regular route
            # can take: regular insertion ran FIRST (fail = tried and failed);
            # gated = tour-only geometry the daily fleet may never serve. Attach
            # to an in-flight tour's tail is tried before commissioning, and the
            # Door-B guard inside the step keeps anything already in the live
            # plan from double-serving. Orders it serves are credited (frozen
            # tour path), so the honest-fail labeling below sees only leftovers.
            if (fail or gated) and (_fp_cfg.TOUR_ATTACH_ENABLED
                                    or _fp_cfg.TOUR_COMMISSION_ENABLED):
                shim = SimpleNamespace(imp=SimpleNamespace(remaining_rejected=(
                    [SimpleNamespace(job_id=str(j), reason="MICRO_INSERT_FAILED") for j in fail]
                    + [SimpleNamespace(job_id=j, reason="TOUR_ONLY") for j in gated])),
                    seed=None)
                _tour_attach_step(now, wm, shim, merged_tours, merged_tour_records,
                                  inputs0, order_of_job, day_iso, state, micro_log,
                                  current_sol=current_sol, vrows0=vrows0,
                                  frozen_tour_rids=frozen_tour_rids, orders_done=orders_done,
                                  tour_created_at=tour_created_at)
                _assert_tour_record_day_caps(merged_tour_records)
                _assert_tour_record_day_caps(last_tour_records)
                _assert_no_mixed_tour_daily_keys(
                    current_sol,
                    [*merged_tour_records, *last_tour_records],
                )
            for jid in fail:
                o = order_of_job.get(str(jid))
                if o and o not in state.served and o not in orders_done:
                    micro_failed_today.add(o)
            # expiry heartbeat: launched trips move into the in-flight set. Runs
            # BEFORE the snapshot so a trip crossing into the launch window this
            # epoch is snapshotted under its committed context — not re-floored
            # into an infeasible (null-time) evaluation. Departures are priced
            # off the LIVE plan (live overlay wins): the anchor's route_times
            # cannot contain a micro-inserted trip, and without the overlay such
            # a trip never launches and never gains watermark protection.
            if last_solve is not None:
                live = live_departures(
                    current_sol, day_iso,
                    lambda key, tt: trip_timings(vrows0[key[0]], key, tt,
                                                 override=_built_ctx(key)))
                newly = expire_commit(current_sol,
                                      {**last_solve.imp.route_times, **live},
                                      day_iso, horizon)
                for key, trips in newly.items():
                    # REPLACEMENT semantics: current_sol is authoritative — a
                    # key's in-flight set is exactly its departed trips (their
                    # committed prefixes are watermark-protected, and suffix
                    # insertions since the last decision ride along). Appending
                    # copies here is how trips get duplicated.
                    inflight[key] = [list(t) for t in trips]
                    if key not in inflight_ctx:
                        inflight_ctx[key] = micro_ctx.get(key) or (last_overrides or {}).get(key)
                    timings[key] = stop_timings(vrows0[key[0]], key, inflight[key],
                                                override=_commit_ctx(key))
                _assert_no_dups(inflight, f"micro-expire@{now:%a %H:%M}")
            _freeze_due_tours(horizon)
            if did_insert:
                # refresh the watermark view AFTER the heartbeat so a trip that
                # just LAUNCHED is snapshotted committed=1 at its insertion epoch
                # (user rule 2026-01-14, WT255029: inserted at the 90-min mark =
                # dispatched NOW — the board must say so immediately, not one
                # snapshot later).
                view = advance_watermarks(wm, inflight, timings, now)
                _snapshot(current_sol, now.isoformat(sep=" "), "micro", view)
            continue

        if kind == "close":
            # ---- 18:00 day close: dock-based trunk + pool roll (Fix 6) ----
            # ONE consolidated list per night, closed at the loading cutoff:
            # everything committed today — anchor-planned AND micro-inserted
            # all afternoon — is at the dock by the 18:00 shift end.
            committed_today_jobs = {j.job_id for (vid, d2), trips in current_sol.items()
                                    if str(d2)[:10] == day_iso for t in trips for j in t}
            committed_today_legs = {_leg_of(j) for j in committed_today_jobs}
            committed_today_legs |= {str(r.leg_id) for r in merged_tour_records
                                     if str(r.service_date)[:10] == day_iso}
            committed_today_legs.discard("")
            close_reserved = {(str(ta.vehicle_id),
                               (date.fromisoformat(str(ta.start_date)) + timedelta(days=k)).isoformat())
                              for ta in merged_tours for k in range(int(ta.days))}
            next_iso = (day + timedelta(days=1)).isoformat()
            # per-tractor same-day committed route-end, so the trunk draw can skip a
            # tractor still out past the prep cutoff (audit #8). Fail-open: a vehicle
            # missing here is simply un-gated. Anchor route_times only (a micro-inserted
            # late trip un-gates rather than fabricates a shortfall).
            route_ends_today: dict = {}
            if last_solve is not None:
                for _k, _v in last_solve.imp.route_times.items():
                    _parts = str(_k).split(":")
                    if len(_parts) < 3 or _parts[0] != "ROUTE" or not _v or len(_v) < 2:
                        continue
                    if _parts[2].split("#", 1)[0] != day_iso:
                        continue
                    _vid, _end = _parts[1], str(_v[1] or "")
                    if _end and _end > route_ends_today.get(_vid, ""):
                        route_ends_today[_vid] = _end
            tonight, tplan = ctx.trunk_fn(day_iso, next_iso, committed_today_legs,
                                          inputs0.candidate_df, inputs0.vehicle_df,
                                          close_reserved, route_ends_today)
            merged_trunk_nights.extend(tonight)
            snapshot_rows.extend(trunk_snapshot_rows(tonight, now.isoformat(sep=" ")))
            trunk_shortfalls.extend(tplan.shortfalls)
            for (vid, nd), hhmm in (tplan.avail_overrides or {}).items():
                state.avail_overrides.setdefault((vid, nd), hhmm)
            runlog.log(f"day-close trunk {day_iso}: {sum(int(n.trips) for n in tonight)} trips / "
                       f"{sum(float(n.km) for n in tonight):,.0f} km; shortfalls={len(tplan.shortfalls)}")
            reasons: dict[str, str] = {}
            if last_solve is not None:
                for rj in (list(last_solve.seed.rejected or [])
                           + list(last_solve.imp.remaining_rejected or [])):
                    oid = order_of_job.get(str(rj.job_id))
                    if oid:
                        reasons.setdefault(oid, str(rj.reason))
            for oid in micro_failed_today:       # booked after the last anchor: only
                reasons.setdefault(oid, "MICRO_INSERT_FAILED")   # micros ever saw it
            micro_failed_today.clear()
            # served == its COLLECTION leg committed (not merely a paired delivery):
            # otherwise an order whose pickup was rejected but whose delivery ran is
            # falsely marked ON_TIME, and its orphan delivery hides in the plan (Bug A).
            collected_today = collected_orders_today(current_sol, day_iso, order_of_job, collect_ids,
                                                     getattr(ctx, "carryin_ids", frozenset()))
            for oid in collected_today:
                state.collected_day.setdefault(oid, day_iso)
            state.note_served(collected_today, day=day_iso)
            # Stage a MULTI-DAY FULL_FLEET order the day its collection commits: its
            # delivery is OUR remaining leg on a LATER day and must be planned there
            # (prestaged), not lost to the served-and-dropped path. Gate on the delivery
            # day being AFTER today (deliver_day_of) — NOT on a tentative :D already in the
            # plan (a next-day :D the seed placed is uncommitted and would be lost when its
            # :C is clamped, so those orders MUST still be staged). Same-day FF (delivered
            # via DIRECT today) has deliver_day == today and is not staged.
            _newly = 0
            for oid in collected_today:
                if (flow_of.get(oid) == "FULL_FLEET" and stage_depot_of.get(oid)
                        and deliver_day_of.get(oid, "") > day_iso):
                    if oid not in window_staged:
                        _newly += 1
                    window_staged[oid] = (stage_depot_of[oid], f"{day_iso}T18:00:00")
            runlog.log(f"[within-window] {day_iso} close: collected_today={len(collected_today)} "
                       f"FF-staged +{_newly} (pending-delivery total={len(window_staged)})")
            vis_close = visible_order_ids(meta0, now, exempt) | set(state.slip_pool)
            targeted = {o for o in collect_ids
                        if target0.get(o) == day_iso and o not in state.collected_day}
            targeted |= {o for o in state.slip_pool if o not in state.collected_day}
            state.roll_day_end(day=day, targeted_today=targeted, visible_today=vis_close,
                               reasons=reasons)
            continue

        # ---------------- anchor epoch: full restricted solve ----------------
        t0 = _time.monotonic()
        # Today's (or later) in-flight keys, plus a 1-day grace window so a trip that
        # completed YESTERDAY still carries its candidate metadata into today's build
        # (2026-07-29 fix, see inflight_orders_for_day) -- a PAST-grace committed trip
        # is finished, so re-offering its already-served orders every day beyond that
        # is what makes `visible` run away (441->2301 over a week) and inflates the
        # seed's rejects + unassigned_jobs ~7x (state-hygiene fix 2026-07-13).
        inflight_orders = inflight_orders_for_day(inflight, order_of_job, day_iso)
        # A collected order is done (collection-centric responsibility, see
        # reconcile_to_plan) — the micro already excludes state.served; the seed must too,
        # or served orders re-enter the candidate frame forever. EXCEPTION: a FULL_FLEET
        # order collected this window but not yet delivered stays visible (its delivery is
        # our remaining leg). Drain the moment the delivery is COMMITTED (in-flight — a
        # tentative future :D in current_sol does NOT count, else we'd stop staging an order
        # whose delivery is still uncommitted and lose it). ``delivered_committed`` itself
        # (monotonic, never shrinks) then keeps protecting visibility once window_staged
        # hands off -- see committed_visibility_protection (2026-07-29 fix: window_staged
        # drains the SAME epoch inflight_orders' day filter can also stop covering the
        # order, leaving a one-day gap where nothing protects it).
        delivered_committed |= delivered_orders_in_plan(inflight, order_of_job)
        _drain_staged_deliveries(window_staged, delivered_committed)
        done_or_served = orders_done | set(state.served)
        vis = visible_order_ids(meta0, now, exempt)
        vis |= set(state.slip_pool)
        protected = committed_visibility_protection(inflight_orders, window_staged, delivered_committed)
        vis -= (done_or_served - protected)
        # in-flight stops need their candidate rows for ALNS job metadata even
        # when the order itself is complete — but only for THIS day's live trips
        # (inflight_orders); delivered_committed/window_staged carry the same need
        # across the day boundary the plain day filter would otherwise drop.
        vis |= protected
        expired = {o for o in vis
                   if (target0.get(o) or "9999") < day_iso
                   and o not in state.slip_pool and o not in state.collected_day
                   and o not in inflight_orders}
        vis -= expired
        orders_done |= expired
        _dbg_oid = os.environ.get("FP_DEBUG_ORDER")
        if _dbg_oid:
            runlog.log(
                f"!! ORDER TRACE {_dbg_oid} @ {now.isoformat(sep=' ')}: "
                f"vis={_dbg_oid in vis} served={_dbg_oid in state.served} "
                f"collected_day={_dbg_oid in state.collected_day} "
                f"slip_pool={_dbg_oid in state.slip_pool} "
                f"orders_done={_dbg_oid in orders_done} expired_now={_dbg_oid in expired} "
                f"inflight_orders={_dbg_oid in inflight_orders} "
                f"window_staged={_dbg_oid in window_staged} "
                f"delivered_committed={_dbg_oid in delivered_committed} "
                f"target0={target0.get(_dbg_oid)} deliver_day_of={deliver_day_of.get(_dbg_oid)} "
                f"stage_depot_of={stage_depot_of.get(_dbg_oid)}")

        overrides = dict(dispatch_floor_ov)       # Fix 2b: idle-vehicle departure floors (shared)
        overrides.update(state.avail_overrides)   # trunk next-day 10:00 etc.
        overrides = apply_commit_ctx(overrides, inflight_ctx)

        seed_exclude = {_leg_of(j.job_id) for trips in inflight.values()
                        for t in trips for j in t}
        seed_exclude |= {str(r.leg_id) for r in merged_tour_records if str(r.leg_id)}
        seed_exclude.discard("")

        tour_reserved = {(str(ta.vehicle_id),
                          (date.fromisoformat(str(ta.start_date)) + timedelta(days=k)).isoformat())
                         for ta in [*merged_tours, *last_seed_tours]
                         for k in range(int(ta.days))}

        # SEED-ONLY constraints: an in-flight vehicle's remaining day starts
        # where its committed trips end (duty carried); a day they exhaust is
        # seed-untouchable. ALNS is NOT so constrained — it works the same
        # trips via watermarked suffixes (smoke crash 3: without this, the
        # seed double-books the vehicle from 07:00 and injection chains an
        # impossible trip behind the in-flight one).
        seed_over: dict = {}
        seed_res: set = set()
        for key, trips in inflight.items():
            rvp = apply_avail_override(_route_vehicle(vrows0[key[0]], key[1]),
                                       _commit_ctx(key), key[1])
            ov = duty_after_freeze(rvp, trips)
            if ov is None:
                seed_res.add(key)
            else:
                seed_over[key] = ov

        # Fix 7 (user rule 2026-07-10): a departed trip is immutable — every
        # job on it (including un-begun delivery suffix stops: the freight is
        # already on the truck) is pinned. Suffixes stay open ONLY to
        # insertion of collection-side work.
        inflight_pinned = {j.job_id for trips in inflight.values()
                           for t in trips for j in t}

        # Dynamic v2: the day's 00:00 anchor constructs the full plan. Later
        # anchors improve the COMPLETE live incumbent in place. Watermarks pin
        # committed prefixes; the remaining suffix stays destroyable by ALNS.
        # Do not discard that suffix into the stochastic rejected pool: with a
        # finite repair cadence, previously covered jobs were otherwise sampled
        # only probabilistically and could disappear at noon despite remaining
        # feasible (2026-08-01 warm-coverage fix).
        warm = (now.time() != epoch_times[0]) and bool(current_sol)
        live_snapshot = ({k: [list(t) for t in v] for k, v in current_sol.items()}
                         if warm else None)
        # imminence weights over the live plan: a change to a soon-to-dispatch
        # job costs more disturbance than a change to a far-future one.
        dist_w = (imminence_weights(dispatch_times(current_sol, last_solve.imp.route_times), now)
                  if (warm and last_solve is not None) else None)

        ns = Namespace(**base,
                       visible_order_ids=vis,
                       extra_staged=(dict(window_staged) or None),  # within-window collected
                                                  # FF freight: deliver it (prestaged), don't
                                                  # defer to next window
                       trunk_from=day_iso,
                       min_service_day=day_iso,   # A4 day clamp: candidates dated
                                                  # before this epoch's day are not
                                                  # plannable (committed legs exempt)
                       max_service_day=end.isoformat(),  # later ordinary delivery
                                                  # transfers through handover
                       extra_pinned_job_ids=inflight_pinned,
                       qargo_frame=redate_qargo(qargo0, state.slip_pool, flow_of),
                       slip_priority=state.slip_priority_map(),
                       seed_exclude_leg_ids=seed_exclude,
                       # Noon improves the complete incumbent. A new day's
                       # midnight solve is a fresh construction and carries
                       # only genuinely committed/in-flight work; injecting
                       # yesterday's tentative future plan there would seed
                       # duplicate assignments.
                       inject_routes={k: [list(t) for t in v]
                                      for k, v in (current_sol if warm else inflight).items()},
                       watermarks=view,
                       commit_floor=floor,
                       now=now,                 # return-leg guard: no suffix insert onto a departed prefix
                       locked_keys=locks,
                       external_reserved=set(tour_reserved) | (
                           {k for k in inflight} if cfg.strict else set()),
                       seed_avail_overrides=seed_over,
                       seed_external_reserved=seed_res,
                       extra_avail_overrides=overrides,
                       beta=(cfg.beta if warm else 0.0),
                       reference_routes=live_snapshot,
                       disturbance_weight=dist_w)
        if cfg.trace and cfg.reports_dir is not None:
            os.environ["FP_ALNS_TRACE"] = str(cfg.reports_dir / f"trace_ep{di + 1:02d}_{now:%a_%H%M}.csv")
        runlog.log(f"== {'warm-reopt' if warm else 'seed'} {now:%Y-%m-%d %H:%M} visible={len(vis)} "
                   f"inflight_keys={len(inflight)} locks={len(locks)} pool={len(state.slip_pool)} "
                   f"floor={floor:%H:%M}")
        if window_staged:
            _ws_vis = len(set(window_staged) & vis)
            runlog.log(f"[within-window] extra_staged={len(window_staged)} threaded; "
                       f"{_ws_vis} of them in visible set")
        inputs = ctx.build_fn(ns, start, end, runlog)
        result = (ctx.reopt_fn if warm else ctx.solve_fn)(ns, start, inputs, runlog)
        imp, seed = result.imp, result.seed
        total_accepted_moves += int(getattr(imp, "accepted_moves", 0) or 0)
        total_inserted_jobs += int(getattr(imp, "inserted_jobs", 0) or 0)
        total_iterations_run += int(getattr(imp, "iterations_run", 0) or 0)
        if first_km_before is None:
            first_km_before = float(getattr(imp, "km_before", 0.0) or 0.0)
            first_cost_before = float(getattr(imp, "cost_before", 0.0) or 0.0)
            first_served_before = int(getattr(imp, "served_before", 0) or 0)
        current_sol = {k: [list(t) for t in ([v] if v and hasattr(v[0], "job_id") else v)]
                       for k, v in imp.solution.items()}
        if warm and live_snapshot is not None:
            def _covered_orders(sol):
                return {
                    str(order_of_job.get(str(j.job_id), str(j.job_id)))
                    for trips in sol.values()
                    for trip in trips
                    for j in trip
                }
            lost_orders = _covered_orders(live_snapshot) - _covered_orders(current_sol)
            if lost_orders:
                sample = ", ".join(sorted(lost_orders)[:10])
                raise RuntimeError(
                    "warm re-optimization lost previously covered orders: "
                    f"{sample}" + (f" (+{len(lost_orders) - 10} more)" if len(lost_orders) > 10 else "")
                )
        _assert_no_dups(current_sol, f"anchor-solve@{now:%a %H:%M}")
        _track(current_sol, now.isoformat(sep=" "), "warm" if warm else "seed", floor)
        _snapshot(current_sol, now.isoformat(sep=" "), "warm" if warm else "seed", view)
        last_solve, last_inputs = result, inputs
        last_overrides = dict(result.combined_avail_overrides or {})
        micro_ctx.clear()      # fresh solve, fresh build ctx — morning floors are stale now

        # ---- freeze whole tours whose first departure precedes the horizon ----
        if result.tour_records:
            last_tour_records = list(result.tour_records)
            last_seed_tours = list(seed.tours)
            for tr in result.tour_records:
                # this seed (re)decided every tour it emitted; frozen rids are
                # excluded from re-seeding so their original stamp survives
                tour_created_at[str(tr.route_id)] = now.isoformat(sep=" ")
        _freeze_due_tours(horizon)
        _assert_tour_record_day_caps(merged_tour_records)
        _assert_tour_record_day_caps(last_tour_records)
        _assert_no_mixed_tour_daily_keys(
            current_sol,
            [*merged_tour_records, *last_tour_records],
        )

        # ---- expiry: launched daily trips become in-flight ----
        newly = expire_commit(current_sol, imp.route_times, day_iso, horizon)
        for key, trips in newly.items():
            inflight[key] = list(trips)       # anchor re-solve owns the day's shape
            if key not in inflight_ctx:
                inflight_ctx[key] = (last_overrides or {}).get(key)
            timings[key] = stop_timings(vrows0[key[0]], key, inflight[key],
                                        override=_commit_ctx(key))
            for t in trips:
                for j in t:
                    oid = order_of_job.get(j.job_id)
                    if (oid and flow_of.get(oid) in COLLECT_FLOWS
                            and _collection_satisfying_job(j)):
                        state.collected_day.setdefault(oid, day_iso)

        # ---- intraday tour attachment + fresh-tour commissioning (far orders) ----
        if _fp_cfg.TOUR_ATTACH_ENABLED or _fp_cfg.TOUR_COMMISSION_ENABLED:
            _tour_attach_step(now, wm, result, merged_tours, merged_tour_records,
                              inputs0, order_of_job, now.date().isoformat(), state, micro_log,
                              current_sol=current_sol, vrows0=vrows0,
                              frozen_tour_rids=frozen_tour_rids, orders_done=orders_done,
                              tour_created_at=tour_created_at)
            _assert_tour_record_day_caps(merged_tour_records)
            _assert_no_mixed_tour_daily_keys(
                current_sol,
                [*merged_tour_records, *last_tour_records],
            )

        # ---- churn: uncommitted assignment stability between anchors ----
        cur: dict[str, tuple[str, str]] = {}
        for key, trips in current_sol.items():
            if key in inflight:
                continue
            for t in trips:
                for j in t:
                    cur[str(j.job_id)] = key
        common = set(cur) & set(prev_uncommitted)
        moved = sum(1 for jid in common if cur[jid] != prev_uncommitted[jid])
        # Disturbance anchor (2026-07-17): the OBJECTIVE's own score for this warm
        # re-opt vs its warm-start reference, with the same imminence weights the
        # solver received — at beta=0 this is the free-reshuffle baseline a future
        # beta sweep is tuned against. Locked/pinned jobs cannot move, so the
        # whole-plan score IS the uncommitted disturbance.
        dist = (disturbance_breakdown(current_sol, live_snapshot, weight=dist_w)
                if (warm and live_snapshot) else None)
        _wc = float(dist["weighted_comparable"]) if dist else 0.0
        churn_rows.append({"epoch": now.isoformat(sep=" "),
                           "kind": "warm" if warm else "seed",
                           "uncommitted_jobs": len(cur),
                           "comparable": len(common), "moved": moved,
                           "churn_pct": round(100.0 * moved / len(common), 1) if common else 0.0,
                           "resequenced": dist["resequenced"] if dist else "",
                           "disturbance_score": round(float(dist["score"]), 2) if dist else "",
                           "weighted_comparable": round(_wc, 2) if dist else "",
                           "disturbance_pct": (round(100.0 * float(dist["score"]) / _wc, 1)
                                               if dist and _wc else (0.0 if dist else "")),
                           "beta": float(cfg.beta) if warm else 0.0})
        if dist is not None:
            runlog.log(f"disturbance: {float(dist['score']):.1f} over weighted base {_wc:.1f} "
                       f"({dist['reassigned']} reassigned, {dist['resequenced']} resequenced "
                       f"of {dist['comparable']} comparable) beta={float(cfg.beta):g}")
        prev_uncommitted = cur

        state.manifest.append({
            "epoch": now.isoformat(sep=" "), "visible_orders": len(vis),
            "inflight_keys": len(inflight), "slip_pool": len(state.slip_pool),
            "frozen_tours_total": len(frozen_tour_rids),
            "wall_s": round(_time.monotonic() - t0, 1),
            "alns_km_after": round(float(imp.km_after), 1),
        })

    final_view = advance_watermarks(wm, inflight, timings,
                                    datetime.combine(end + timedelta(days=1), epoch_times[0]))
    return {
        "state": state, "wm": wm, "inflight": inflight, "timings": timings,
        "current_sol": current_sol, "last_solve": last_solve, "last_inputs": last_inputs,
        "last_overrides": last_overrides, "final_view": final_view,
        "merged_tours": merged_tours, "merged_tour_records": merged_tour_records,
        "merged_trunk_nights": merged_trunk_nights, "trunk_shortfalls": trunk_shortfalls,
        "micro_log": micro_log, "churn_rows": churn_rows, "snapshot_rows": snapshot_rows,
        "frozen_tour_rids": frozen_tour_rids, "orders_done": orders_done,
        "inflight_ctx": inflight_ctx, "placement": placement,
        "tour_created_at": tour_created_at, "ever_committed_legs": ever_committed_legs,
        "total_accepted_moves": total_accepted_moves,
        "total_inserted_jobs": total_inserted_jobs,
        "total_iterations_run": total_iterations_run,
        "first_km_before": first_km_before,
        "first_cost_before": first_cost_before,
        "first_served_before": first_served_before,
    }


def _emit_timeline(base_dir: Path, runlog, delta: int = 90) -> bool:
    """Auto-drop the evolving-plan gantt dashboard at ``<run>/timeline.html``.
    Resolved at call time so tests can patch the builder; a viz failure warns
    loudly and never kills a finished run."""
    from freight_planner import viz_timeline_build
    try:
        out = viz_timeline_build.write_dashboard(Path(base_dir),
                                                 Path(base_dir) / "timeline.html",
                                                 delta=delta)
        runlog.log(f"timeline: dashboard -> {out}")
        return True
    except Exception as e:  # noqa: BLE001 — derived view; the plan outputs are intact
        runlog.log(f"timeline FAILED (plan outputs are intact): {type(e).__name__}: {e}")
        return False


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _parse_rolling_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="E6 dynamic rolling dispatcher (spec 4.7a).")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    # Orders input is FIXED to the combined Jan+Feb enriched parquet (user rule
    # 2026-07-22: "we will only be reading off the combined file" — monthly files
    # are booking-month universes and silently miss cross-month dues; a CLI
    # override invited exactly that mistake). Reproducing a pre-2026-07-22 run
    # (monthly-file catchment calibration) requires editing paths.DEFAULT_ENRICHED.
    parser.add_argument("--postcode-cache", default=str(DEFAULT_POSTCODE_CACHE))
    parser.add_argument("--osrm-cache", default=None,
                        help="optional per-run OSRM matrix cache path")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--handover-in", default=None)
    parser.add_argument("--iterations", type=int, default=10000, help="ALNS iterations per ANCHOR epoch")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--delta-min", type=int, default=DELTA_MIN,
                    # VESTIGIAL: accepted for CLI compatibility; the live commit lag
                    # is --delta-r1-min everywhere (see epoch_state.DELTA_MIN note).
                        help="VESTIGIAL — kept for CLI compatibility; the live commit lag is --delta-r1-min")
    parser.add_argument("--delta-r1-min", type=int, default=90,
                        help="suffix-commitment lag: solve wall + driver notification + contingency")
    parser.add_argument("--micro-every-min", type=int, default=_fp_cfg.MICRO_EVERY_MIN,
                        help="insertion-only micro-pass cadence between anchors; 0 disables "
                             f"(default: config.MICRO_EVERY_MIN = {_fp_cfg.MICRO_EVERY_MIN}; "
                             "pre-2026-07-14 replays used 60)")
    parser.add_argument("--epochs", default="00:00,12:00",
                        help="anchor epoch times per operating day (midnight seed, noon re-opt)")
    parser.add_argument("--converge-pct", type=float, default=None,
                        help="per-anchor ALNS convergence gate: stop when best improves < this %% "
                             "over --converge-window iterations (default: config.ALNS_CONVERGE_PCT; "
                             "0 = fixed budget, for provenance replays)")
    parser.add_argument("--converge-window", type=int, default=None,
                        help="iterations per convergence check (default: config.ALNS_CONVERGE_WINDOW)")
    parser.add_argument("--converge-min-iters", type=int, default=None,
                        help="never stop an anchor before this many iterations "
                             "(default: config.ALNS_CONVERGE_MIN_ITERS)")
    parser.add_argument("--strict", action="store_true",
                        help="degenerate config: whole-trip freezing, no suffix insertion, "
                             "no micro-passes — the floor measurement")
    parser.add_argument("--trace", action="store_true",
                        help="write FP_ALNS_TRACE per anchor (trace_ep*.csv)")
    parser.add_argument("--beta", type=float, default=0.0,
                        help="dynamic-v2 stability weight: warm-start objective is "
                             "cost + beta*disturbance (0 = pure cost; the regression gate)")
    parser.add_argument("--vehicle-day-cost", action=argparse.BooleanOptionalAction, default=None,
                        help="per-vehicle-day driver activation cost in the objective "
                             "(guaranteed-shift floor + overtime; default: config, ON since 2026-07-15). "
                             "--no-vehicle-day-cost = the fuel-only ablation")
    parser.add_argument("--guaranteed-shift-hours", type=float, default=None,
                        help="paid minimum shift hours = floor of the driver-day cost "
                             f"(default: config.GUARANTEED_SHIFT_HOURS = {_fp_cfg.GUARANTEED_SHIFT_HOURS})")
    parser.add_argument("--overtime-cost", action=argparse.BooleanOptionalAction, default=None,
                        help="overtime + late-ramp surcharges in the driver-day cost (payroll OT "
                             "beyond the paid floor + unsocial premium ramping past 19:00; default: "
                             "config, ON since 2026-07-16). --no-overtime-cost = the pre-fairness "
                             "straight-time ablation")
    parser.add_argument("--tour-depot-direct-as-delivery",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="plan a DIRECT collected at its anchor depot as a depot-loaded delivery so "
                             "same-destination far orders consolidate onto one tour (default: config, ON "
                             "since 2026-07-15). --no-tour-depot-direct-as-delivery = the pre-fix split")
    parser.add_argument("--daily-depot-direct-as-delivery",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="emit a same-day DIRECT collected AT its source depot as a depot-loaded "
                             "delivery so same-origin orders co-load (default: config, ON since "
                             "2026-07-17). --no-daily-depot-direct-as-delivery = legacy atomic arcs")
    parser.add_argument("--daily-depot-direct-radius-km", type=float, default=None,
                        help="collocation radius (km) for the daily depot-direct rule (default: config, 2.0)")
    parser.add_argument("--collocated-staging-min", type=float, default=None,
                        help="minutes from collection-window open to the freight being loadable at the "
                             "dock — the reclassified leg's departure floor (default: config, 30)")
    parser.add_argument("--depot-pinning",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="serve every pickup/delivery only with vehicles homed at its freight's "
                             "depot (default: config, ON since 2026-07-17). --no-depot-pinning = "
                             "legacy free assignment (the cross-depot teleport ablation)")
    parser.add_argument("--readiness-lag-min", type=float, default=None,
                        help="EXPERIMENT (A2): floor PL_IMPORT delivery departures to 06:00 + M minutes, "
                             "modeling import freight that lands by day-trunk not the 04:30 night trunk "
                             "(default: config, 0 = off). Bounds the headline's sensitivity to the "
                             "unobservable per-order depot-arrival time; floors ONLY import deliveries")
    parser.add_argument("--hard-time-windows", dest="hard_time_windows",
                        action="store_true", default=False,
                        help="ablation (2026-07-18): hard cutoff on every stated delivery deadline instead "
                             "of the default soft earliness/tardiness penalty (hard-VRPTW comparison arm)")
    parser.add_argument("--tardiness-coef", type=float, default=None,
                        help="GBP per (minute late)^2 for the soft delivery-window penalty (default: config)")
    parser.add_argument("--earliness-coef", type=float, default=None,
                        help="GBP per minute early for the soft delivery-window penalty (default: config)")
    parser.add_argument("--tour-osrm-durations",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="time tours with OSRM per-road-type durations (default: config, ON); "
                             "--no-tour-osrm-durations reverts to the flat 50/80 km/h model")
    parser.add_argument("--travel-slack", type=float, default=None,
                        help="robustness multiplier on ALL planned travel times (default: config, 1.0 = "
                             "calibrated average-reality). Speed -15%% == 1/0.85 ~= 1.176")
    args = parser.parse_args(argv)
    if args.strict:
        args.micro_every_min = 0
    return args


def _apply_vehicle_day_cost_flags(args) -> None:
    """Map the driver-day-cost CLI flags onto the config knobs the objective reads
    at call-time (vehicle_cost.driver_day_cost). ``--vehicle-day-cost`` /
    ``--no-vehicle-day-cost`` force it on/off; absent = keep the config default."""
    if getattr(args, "vehicle_day_cost", None) is not None:
        _fp_cfg.VEHICLE_DAY_COST_ENABLED = bool(args.vehicle_day_cost)
    if getattr(args, "guaranteed_shift_hours", None) is not None:
        _fp_cfg.GUARANTEED_SHIFT_HOURS = float(args.guaranteed_shift_hours)
    if getattr(args, "overtime_cost", None) is not None:
        _fp_cfg.OVERTIME_COST_ENABLED = bool(args.overtime_cost)
    if getattr(args, "tour_depot_direct_as_delivery", None) is not None:
        _fp_cfg.TOUR_DEPOT_DIRECT_AS_DELIVERY = bool(args.tour_depot_direct_as_delivery)
    if getattr(args, "daily_depot_direct_as_delivery", None) is not None:
        _fp_cfg.DAILY_DEPOT_DIRECT_AS_DELIVERY = bool(args.daily_depot_direct_as_delivery)
    if getattr(args, "daily_depot_direct_radius_km", None) is not None:
        _fp_cfg.DAILY_ORIGIN_AT_DEPOT_RADIUS_KM = float(args.daily_depot_direct_radius_km)
    if getattr(args, "collocated_staging_min", None) is not None:
        _fp_cfg.COLLOCATED_STAGING_MIN = float(args.collocated_staging_min)
    if getattr(args, "depot_pinning", None) is not None:
        _fp_cfg.DEPOT_PINNING = bool(args.depot_pinning)
    if getattr(args, "readiness_lag_min", None) is not None:
        _fp_cfg.READINESS_LAG_MIN = float(args.readiness_lag_min)
    if getattr(args, "hard_time_windows", False):
        _fp_cfg.SOFT_DELIVERY_WINDOWS = False
    if getattr(args, "tardiness_coef", None) is not None:
        _fp_cfg.TARDINESS_COEF = float(args.tardiness_coef)
    if getattr(args, "earliness_coef", None) is not None:
        _fp_cfg.EARLINESS_COEF = float(args.earliness_coef)
    if getattr(args, "tour_osrm_durations", None) is not None:
        _fp_cfg.TOUR_OSRM_DURATIONS = bool(args.tour_osrm_durations)
    if getattr(args, "travel_slack", None) is not None:
        _fp_cfg.TRAVEL_TIME_SLACK = float(args.travel_slack)


def _solver_base(args, start_s: str, end_s: str, out_dir_s: str) -> dict:
    """The base kwargs every anchor/micro Namespace is built from. Includes the
    convergence-gate overrides so the CLI reaches run_alns's getattr reads."""
    return dict(_BASE_SOLVE_FIELDS, start=start_s, end=end_s, qargo=str(DEFAULT_ENRICHED),
                postcode_cache=args.postcode_cache, osrm_cache=args.osrm_cache,
                out_dir=out_dir_s,
                handover_in=args.handover_in, iterations=args.iterations, seed=args.seed,
                converge_pct=args.converge_pct, converge_window=args.converge_window,
                converge_min_iters=args.converge_min_iters,
                collect_creation_floor=True)   # E6: no collection before its order existed


def main(argv: list[str] | None = None) -> int:
    args = _parse_rolling_args(argv)
    _apply_vehicle_day_cost_flags(args)
    epoch_times = tuple(datetime.strptime(t.strip(), "%H:%M").time()
                        for t in str(args.epochs).split(",") if t.strip())

    start, end = _parse_date(args.start), _parse_date(args.end)
    out_dir = Path(args.out_dir) / f"{start:%Y-%m}"
    window = flat_window_label(start, end, "forward_structural", "planning_window")
    base_dir, plan_dir, reports_dir = run_dirs(out_dir, window)
    write_run_manifest(out_dir, window, {
        "runner": "run_rolling_dynamic", "window": window, "start": str(start), "end": str(end),
        "handover_in": str(args.handover_in) if args.handover_in else None,
        "qargo": str(DEFAULT_ENRICHED), "osrm_cache": str(args.osrm_cache) if args.osrm_cache else None,
        "iterations_per_epoch": args.iterations,
        "seed": args.seed, "delta_min": args.delta_min, "delta_r1_min": args.delta_r1_min,
        "micro_every_min": args.micro_every_min, "epochs": str(args.epochs),
        "converge_pct": args.converge_pct, "converge_window": args.converge_window,
        "converge_min_iters": args.converge_min_iters,
        "travel_slack": float(_fp_cfg.TRAVEL_TIME_SLACK),
        "strict": bool(args.strict), "env_toggles": _env_toggles(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    runlog = RunLog(reports_dir / "alns_progress.log")
    runlog.log(f"DYNAMIC window {start}..{end} iters/anchor={args.iterations} seed={args.seed} "
               f"delta={args.delta_min}m deltaR1={args.delta_r1_min}m micro={args.micro_every_min}m "
               f"strict={args.strict}")

    base = _solver_base(args, str(start), str(end), str(out_dir))

    # ---- window-level context, built once ----
    inputs0 = build_window_inputs(Namespace(**base), start, end, runlog)
    qargo0 = inputs0.qargo_df
    demand0 = pd.DataFrame([r.to_dict() for r in build_demand_records(qargo0, start, end)])
    fcol = "corrected_flow" if "corrected_flow" in demand0.columns else "flow"
    flow_of = dict(zip(demand0["order_id"].astype(str), demand0[fcol].astype(str)))
    meta0 = build_order_meta(qargo0, demand0)
    runnable = inputs0.candidate_df[
        inputs0.candidate_df.get("hard_blocker", pd.Series(dtype=str)).fillna("").eq("")]
    bins = detect_shuttle_bins(runnable, _ok_options(inputs0.compatibility_df), inputs0.vehicle_df)
    exempt = shuttle_exempt_order_ids(bins, inputs0.candidate_df)
    runlog.log(f"standing shuttle exemption: {len(exempt)} orders across {len(bins)} bins")
    # staleness date is OUR service day: delivery date for delivery-flow orders
    # AND for pre-window-collected FULL_FLEET orders (freight already at a depot),
    # origin date for collections (else imports expire on their network-collection date).
    target0 = target_service_day(qargo0, flow_of, window_start=start.isoformat())
    win_orders = set(inputs0.legs_df["order_id"].astype(str)) if not inputs0.legs_df.empty else set()
    carryin_ids = {o for o in carryin_delivery_ids(inputs0.legs_df, start.isoformat())
                   if flow_of.get(o) in COLLECT_FLOWS} & win_orders
    collect_ids = serviceable_collect_ids(flow_of, win_orders, inputs0.legs_df,
                                          window_start=start.isoformat()) | carryin_ids
    vrows0 = {str(r.vehicle_id): r for r in inputs0.vehicle_df.itertuples(index=False)}
    vmeta0 = _vehicle_meta_map(inputs0.vehicle_df)
    leg_of_job = dict(zip("JOB:" + inputs0.candidate_df["leg_id"].astype(str),
                          inputs0.candidate_df["leg_id"].astype(str)))
    order_of_job = dict(zip("JOB:" + inputs0.candidate_df["leg_id"].astype(str),
                            inputs0.candidate_df["order_id"].astype(str)))

    cfg = LoopCfg(start=start, end=end, epoch_times=epoch_times,
                  delta_r1_min=args.delta_r1_min, micro_every_min=args.micro_every_min,
                  strict=bool(args.strict), trace=bool(args.trace), reports_dir=reports_dir,
                  beta=float(args.beta))
    ctx = LoopCtx(base=base, inputs0=inputs0, qargo0=qargo0, flow_of=flow_of, meta0=meta0,
                  exempt=exempt, target0=target0, win_orders=win_orders, carryin_ids=carryin_ids,
                  collect_ids=collect_ids, vrows0=vrows0, vmeta0=vmeta0,
                  leg_of_job=leg_of_job, order_of_job=order_of_job)
    r = run_dynamic_loop(cfg, ctx, runlog)
    state = r["state"]
    current_sol, inflight = r["current_sol"], r["inflight"]
    last_solve, last_overrides = r["last_solve"], r["last_overrides"]
    merged_tours, merged_tour_records = r["merged_tours"], r["merged_tour_records"]
    merged_trunk_nights, trunk_shortfalls = r["merged_trunk_nights"], r["trunk_shortfalls"]
    micro_log, churn_rows = r["micro_log"], r["churn_rows"]

    # ---------------- finalize: mint the merged plan's records ----------------
    os.environ.pop("FP_ALNS_TRACE", None)
    final_inputs = r["last_inputs"] if last_solve is not None else inputs0
    final_view = r["final_view"]
    fin_over = apply_commit_ctx(last_overrides or {}, r["inflight_ctx"])
    imp_final = improve_existing_solution(
        current_sol, final_inputs.candidate_df, final_inputs.vehicle_df,
        final_inputs.compatibility_df, iterations=0,
        rejected=list(getattr(last_solve.imp, "remaining_rejected", []) or []) if last_solve else [],
        avail_overrides=(fin_over or None),
        watermarks=final_view, locked_keys=suffix_locks(inflight, final_view),
        run_merge_sweep=False,
    )
    # reconcile the ledger against the EMITTED plan: an order is served only if its
    # collection leg survived into the final plan (imp_final + tours). Anything
    # counted served during the loop but pruned at this iterations=0 finalize — or
    # ever only delivery-marked — is demoted to unserved, so the ledger matches the
    # plan by construction (closes the delivery-conflation AND finalize-pruning gaps).
    plan_collected = collection_days_in_plan(
        list(imp_final.selected) + list(merged_tour_records),
        carryin_ids=carryin_ids,
    )
    for oid in tour_served_order_ids(merged_tour_records):
        plan_collected.setdefault(oid, state.collected_day.get(oid, ""))
    state.reconcile_to_plan(plan_collected, collect_ids)
    unserved_rejected = [SimpleNamespace(job_id=f"ORDER:{oid}", reason=reason)
                         for oid, reason in sorted(state.unserved.items())]
    # audit #8 (2026-07-26): imp_final is a 0-iteration re-price of the day's fully-evolved
    # solution, so imp_final.km_before == imp_final.km_after and accepted_moves is trivially
    # 0 -- the console summary always read "0 saved" / "accepted moves: 0" no matter how much
    # real ALNS work the day's seed + warm-reopt anchors did. km_after (the final committed
    # km) is still correct from imp_final; km_before/accepted_moves/inserted_jobs come from
    # the whole-day totals run_dynamic_loop accumulated across every anchor epoch.
    # Same bug class, fixed 2026-07-28: a 0-iteration re-price ALSO makes
    # imp_final.cost_before == cost_after and served_before == served_after trivially (it
    # re-evaluates the SAME final solution twice) -- validation_metrics.json's seed_cost was
    # reading identical to alns_cost on every run. cost_before/served_before now come from the
    # same first-epoch snapshot as km_before; cost_after/served_after stay from imp_final (a
    # fresh, correct re-evaluation of the actual final solution, same reasoning as km_after).
    _day_km_before = r.get("first_km_before")
    _day_cost_before = r.get("first_cost_before")
    _day_served_before = r.get("first_served_before")
    merged_imp = SimpleNamespace(
        selected=list(imp_final.selected),
        route_totals=dict(imp_final.route_totals), route_times=dict(imp_final.route_times),
        remaining_rejected=list(imp_final.remaining_rejected) + unserved_rejected,
        km_before=float(_day_km_before if _day_km_before is not None else imp_final.km_before),
        km_after=float(imp_final.km_after),
        cost_before=float(_day_cost_before if _day_cost_before is not None else imp_final.cost_before),
        cost_after=float(imp_final.cost_after),
        served_before=int(_day_served_before if _day_served_before is not None else imp_final.served_before),
        served_after=int(imp_final.served_after),
        accepted_moves=int(r.get("total_accepted_moves", 0) or 0),
        inserted_jobs=int(r.get("total_inserted_jobs", 0) or 0),
        iterations_run=int(r.get("total_iterations_run", 0) or 0),
        stop_reason="whole-day-total",   # sum across every anchor epoch, not one epoch's reason
        # §6.4 objective cost terms per (vehicle,day) from the final solve — carried
        # through the merge so the emit stage can write the cost decomposition.
        cost_decomposition=list(getattr(imp_final, "cost_decomposition", []) or []),
        # §6.3b seed-plan veh-days + km per type (consolidation / type-shift baseline).
        seed_by_type=dict(getattr(imp_final, "seed_by_type", {}) or {}),
    )
    merged_seed = SimpleNamespace(
        tours=merged_tours, tour_records=merged_tour_records,
        daily=SimpleNamespace(shuttle_job_ids=set()),
        rejected=[], routes={},
    )
    merged_trunk = None
    if merged_trunk_nights:
        merged_trunk = TrunkPlan(
            nights=merged_trunk_nights, draws={}, avail_overrides={},
            shortfalls=trunk_shortfalls,
            total_km=sum(float(n.km) for n in merged_trunk_nights),
            total_trips=sum(int(n.trips) for n in merged_trunk_nights),
        )
    tour_km = sum(float(ta.evaluation.total_km) for ta in merged_tours)
    merged = SolveResult(seed=merged_seed, imp=merged_imp, trunk_plan=merged_trunk,
                         tour_records=merged_tour_records, tour_km=tour_km,
                         combined_avail_overrides={})
    # each surviving job's OWN per-job dispatch floor (the epoch that FIRST placed
    # it), keyed by leg_id like candidate_by_leg -- closes the gap a vehicle-level
    # avail_override can't: a stop protected by a LATER floor than its vehicle-day's
    # own first-launch floor (route-backdating fix, 2026-07-28, see route_seed.py).
    def _pad_secs(iso: str) -> str:
        return iso if len(iso) > 16 else f"{iso}:00"          # "...HH:MM" -> "...HH:MM:00"
    fin_job_floors = {(k[4:] if str(k).startswith("JOB:") else str(k)): _pad_secs(str(v.get("floor", "")))
                      for k, v in (r["placement"] or {}).items()
                      if isinstance(v, dict) and str(v.get("floor", ""))}
    rc, _option_conflicts = emit_outputs(Namespace(**base), start, end, inputs0, merged, plan_dir, reports_dir, runlog,
                      final_avail_overrides=fin_over, final_job_floors=fin_job_floors,
                      ever_committed_legs=r["ever_committed_legs"])
    _na_viol = audit_non_anticipation(plan_dir, qargo0, runlog)
    emit_stop_provenance(plan_dir, reports_dir, qargo0, current_sol, fin_over,
                         vrows0, r["placement"], runlog)

    ledger = state.ledger_frame({o: target0.get(o, "") for o in collect_ids})
    ledger.to_csv(plan_dir / "service_ledger.csv", index=False)
    # headline slip visibility (user rule 2026-07-16): how many served LATE, and how
    # late — SLIPPED(n) rows are the orders that pre-1a died UNSERVED instead
    _slipped = ledger[ledger["outcome"].astype(str).str.startswith("SLIPPED")]
    # audit #10 (2026-07-26): this log line carried no denominator, so "0 unserved" (or any
    # count) read as if it covered the whole in-universe order set. `ledger` is COLLECTION
    # orders only (FULL_FLEET/PL_EXPORT/LOCAL_COLLECT) — a SUBSET of the KPI's in-universe
    # total (01_service_summary.md already states this; the console/log line did not).
    runlog.log(f"service (of {len(ledger)} collection orders — see 01_service_summary.md "
               f"for full scope): {int((ledger['outcome'] == 'ON_TIME').sum())} on-time, "
               f"{len(_slipped)} slipped (avg {float(_slipped['days_late'].mean()) if len(_slipped) else 0.0:.1f}d late), "
               f"{int((ledger['outcome'] == 'UNSERVED').sum())} unserved")
    svc = ["# Service summary", "",
           f"Window {start} .. {end} - one row per in-universe COLLECTION order (an order we "
           f"collect: FULL_FLEET / PL_EXPORT / LOCAL_COLLECT). Delivery-only orders (network "
           f"import) carry no collection here; their service is in 02_kpi_summary "
           f"(within-window completeness + delivery timeliness). So this count is a SUBSET of "
           f"the KPI's in-universe total, not a different answer to the same question.", "",
           f"- collection orders (this report's denominator): {len(ledger)}"]
    svc += [f"- {k}: {v}" for k, v in ledger["outcome"].value_counts().items()]
    if len(_slipped):
        svc += ["", "## Served LATE (slipped)", ""]
        svc += [f"- {r.order_id}: {int(r.days_late)}d late (target {r.target_day})"
                for r in _slipped.itertuples(index=False)]
    _uns = ledger[ledger["outcome"] == "UNSERVED"]
    if len(_uns):
        svc += ["", "## Unserved", ""]
        svc += [f"- {r.order_id}: target {r.target_day}, tried {int(r.days_late)}d, "
                f"reason {r.reason or 'UNKNOWN'}" for r in _uns.itertuples(index=False)]
    (plan_dir / "01_service_summary.md").write_text(chr(10).join(svc) + chr(10), encoding="utf-8")
    pd.DataFrame(churn_rows).to_csv(reports_dir / "churn.csv", index=False)
    _kpi_p = plan_dir / "02_kpi_summary.md"
    if churn_rows and _kpi_p.exists():
        # plan-stability section (2026-07-17): churn + the objective's own
        # disturbance score per warm re-opt — the anchor a beta sweep reads
        from freight_planner.reports import plan_stability_md
        _kpi_p.write_text(_kpi_p.read_text(encoding="utf-8").rstrip() + "\n\n"
                          + plan_stability_md(churn_rows, float(args.beta)),
                          encoding="utf-8")
    pd.DataFrame(micro_log).to_csv(reports_dir / "micro_passes.csv", index=False)
    pd.DataFrame(r.get("snapshot_rows") or []).to_csv(reports_dir / "plan_snapshots.csv", index=False)
    (plan_dir / "rolling_manifest.json").write_text(
        json.dumps({"anchors": state.manifest, "micro_passes": micro_log,
                    "ledger_summary": ledger["outcome"].value_counts().to_dict(),
                    "slipped_total": int(len(_slipped)),
                    "slipped_days_late_avg": (round(float(_slipped["days_late"].mean()), 2)
                                              if len(_slipped) else 0.0),
                    "shuttle_exempt": sorted(exempt),
                    "delta_min": args.delta_min, "delta_r1_min": args.delta_r1_min,
                    "micro_every_min": args.micro_every_min, "strict": bool(args.strict),
                    "iterations_per_epoch": args.iterations},
                   indent=2), encoding="utf-8")
    # the gantt board is a forensic surface too (it caught WT255038), so it is
    # built BEFORE the strict audit can raise
    _emit_timeline(base_dir, runlog, delta=args.delta_r1_min)
    # runs LAST among the finalize steps: a strict-mode raise must never abort the
    # forensic outputs (snapshots/provenance/ledger are exactly what diagnoses it)
    _bd_viol = audit_route_backdating(plan_dir, r.get("tour_created_at") or {}, r["placement"], runlog)
    # §6.1: fold the three dynamic audits into feasibility_audit.csv / report 09 so all
    # four correctness-audit families read 0 in one structured place, not just the log.
    # option_conflicts (2026-07-28) previously reached only the "!! OPTION CONFLICT" runlog
    # line inside emit_outputs -- a real campaign run could hit a nonzero count and it would
    # never show up in a §6.1 table built straight from feasibility_audit.csv.
    from freight_planner.feasibility_audit import augment_with_dynamic_audits
    augment_with_dynamic_audits(plan_dir, _na_viol, _bd_viol, _option_conflicts)
    runlog.log(f"DYNAMIC DONE: ledger {ledger['outcome'].value_counts().to_dict()}")
    print(f"rolling ledger: {ledger['outcome'].value_counts().to_dict()}")
    print(f"  run dir: {base_dir}  (plan_full.csv, timeline.html, runsheets.html + log at root; csv/, reports/)")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
