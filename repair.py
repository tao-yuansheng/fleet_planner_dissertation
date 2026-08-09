# ============================================================================
# REDUNDANT (2026-07-19): DEAD MODULE — not imported anywhere in the live
# pipeline and not a __main__ entry point. The live repair logic lives inside
# alns.py (destroy/regret-k repair) and the stranded-backhaul repair in
# run_alns.py/tours.py. Retained for reference only; safe to remove.
# See experiments/REDUNDANT_FILES.md.
# ============================================================================
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pandas as pd

from freight_planner.alns import (
    _as_trips,
    _build_vehicle_meta,
    _flatten,
    _records_from_solution,
    _route_vehicle,
)
from freight_planner.planner_state import RejectedJob
from freight_planner.route_seed import _job_coords, _ok_options, _reorder, make_route_job, same_order_handoff_conflict
from freight_planner.routing_adapter import evaluate_day, try_insert_job

REPAIRABLE_REASONS = {"SHIFT", "DRIVING_CAP", "TIME_WINDOW", "NO_FEASIBLE_ROUTE"}


@dataclass
class RepairResult:
    solution: dict
    selected: list
    remaining_rejected: list[RejectedJob]
    inserted_jobs: int
    attempted_jobs: int


def repair_unassigned_jobs(
    solution: dict,
    rejected: list[RejectedJob],
    candidates: pd.DataFrame,
    vehicles: pd.DataFrame,
    compatibility: pd.DataFrame,
    *,
    plan_id: str = "REPAIR",
    excluded_vehicle_days: set[tuple[str, str]] | None = None,
) -> RepairResult:
    excluded = excluded_vehicle_days or set()
    routes = {key: _as_trips(value) for key, value in solution.items()}
    vehicle_meta = _build_vehicle_meta(vehicles)
    candidate_by_job = {str(r.get("job_id", "")): r for r in candidates.to_dict("records")}
    coords = _job_coords(compatibility)
    options = _ok_options(compatibility)
    selected_leg_ids = {
        str(candidate_by_job.get(job.job_id, {}).get("leg_id", ""))
        for trips in routes.values()
        for job in _flatten(trips)
    }

    rv_cache = {}

    def rv(vid: str, day: str):
        cached = rv_cache.get((vid, day))
        if cached is None:
            cached = _route_vehicle(vehicle_meta[vid], day)
            rv_cache[(vid, day)] = cached
        return cached

    def day_km(vid: str, day: str, trips: list[list]):
        return 0.0 if not trips else evaluate_day(rv(vid, day), trips).total_km

    repairable: list[tuple[RejectedJob, dict]] = []
    remaining: list[RejectedJob] = []
    for rj in rejected or []:
        cand = candidate_by_job.get(str(rj.job_id), {})
        if not cand or str(rj.reason) not in REPAIRABLE_REASONS:
            remaining.append(rj)
            continue
        if str(cand.get("hard_blocker", "") or ""):
            remaining.append(rj)
            continue
        if str(cand.get("dependency_type", "") or "") == "REQUIRES_PRIOR_PICKUP":
            predecessor = str(cand.get("predecessor_leg_id", "") or "")
            if predecessor and predecessor not in selected_leg_ids:
                remaining.append(rj)
                continue
        repairable.append((rj, cand))

    repairable.sort(key=lambda item: (
        str(item[1].get("service_date", "")),
        str(item[1].get("latest_finish", "") or "~"),
        -float(item[1].get("pallets", 0.0) or 0.0),
        str(item[0].job_id),
    ))

    inserted = 0
    attempted = 0
    for rj, cand in repairable:
        attempted += 1
        rjob = make_route_job(SimpleNamespace(**cand), coords)
        if rjob is None:
            remaining.append(rj)
            continue
        day = str(cand.get("service_date", "") or "")
        leg_id = str(cand.get("leg_id", "") or "")
        opts = options.get(leg_id, [])
        eligible = [v for v, same in opts if same] + [v for v, same in opts if not same]
        best = None
        for vid in eligible:
            if vid not in vehicle_meta or (vid, day) in excluded:
                continue
            current_trips = routes.get((vid, day), [])
            base_km = day_km(vid, day, current_trips)

            for idx, trip in enumerate(current_trips):
                if same_order_handoff_conflict(trip, rjob):
                    continue
                trip_ev = try_insert_job(rv(vid, day), trip, rjob, "best")
                if not trip_ev.feasible:
                    continue
                candidate_trips = [list(t) for t in current_trips]
                candidate_trips[idx] = _reorder(trip + [rjob], trip_ev)
                day_ev = evaluate_day(rv(vid, day), candidate_trips)
                if not day_ev.feasible:
                    continue
                delta = day_ev.total_km - base_km
                if best is None or delta < best[0]:
                    best = (delta, (vid, day), candidate_trips)

            # no trip-count cap (user rule 2026-07-16): feasibility bounds the day
            candidate_trips = [list(t) for t in current_trips] + [[rjob]]
            day_ev = evaluate_day(rv(vid, day), candidate_trips)
            if day_ev.feasible:
                delta = day_ev.total_km - base_km
                if best is None or delta < best[0]:
                    best = (delta, (vid, day), candidate_trips)

        if best is None:
            remaining.append(rj)
            continue
        _, key, new_trips = best
        routes[key] = new_trips
        inserted += 1
        selected_leg_ids.add(leg_id)

    job_meta = {}
    for (_vid, day), trips in routes.items():
        for rjob in _flatten(trips):
            cand = candidate_by_job.get(rjob.job_id, {})
            job_meta[rjob.job_id] = SimpleNamespace(rjob=rjob, day=day, candidate=cand)
    selected = _records_from_solution(
        routes,
        {jid: SimpleNamespace(candidate=meta.candidate) for jid, meta in job_meta.items()},
        vehicle_meta,
        plan_id,
    )
    return RepairResult(
        solution=routes,
        selected=selected,
        remaining_rejected=remaining,
        inserted_jobs=inserted,
        attempted_jobs=attempted,
    )

