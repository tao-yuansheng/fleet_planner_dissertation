"""Build selected-plan rows from a *final* set of routes.

Records must be emitted once the route order is settled, not incrementally during
insertion — best-position insertion reorders a route, which would otherwise leave
earlier rows with stale sequence/arrival/km. Both the greedy seed and ALNS share
this so their outputs are consistent and final-state-true.
"""
from __future__ import annotations

import os

from freight_planner.plan_schema import SelectedPlanBuilder, SelectedPlanRecord
from freight_planner.routing_adapter import RouteJob, evaluate_day

_FREIGHT_STATE_AFTER = {
    "CUSTOMER_PICKUP": "AT_DEPOT",
    "HUB_DROP": "WITH_NETWORK",  # handed to the Palletline/Hazchem network at the hub
}


def _as_trips(value) -> list[list[RouteJob]]:
    if not value:
        return []
    first = value[0]
    if isinstance(first, RouteJob):
        return [list(value)]
    return [list(trip) for trip in value if trip]


def build_plan_records(
    routes: dict,                 # (vehicle_id, day) -> list[RouteJob] or list[list[RouteJob]]
    candidate_by_job: dict,       # job_id -> candidate mapping (dict/Series/namedtuple)
    route_vehicle,                # (vehicle_id, day) -> RouteVehicle
    home_depot_of,                # vehicle_id -> str
    reason_for,                   # (candidate, home_depot) -> str
    plan_id: str,
) -> list[SelectedPlanRecord]:
    builder = SelectedPlanBuilder(plan_id=plan_id)
    for (vid, day), route_value in routes.items():
        trips = _as_trips(route_value)
        if not trips:
            continue
        day_eval = evaluate_day(route_vehicle(vid, day), trips)
        if not day_eval.feasible:
            # An infeasible day has no stop timings, so its jobs would produce no
            # records — silently losing freight from the plan (B16). Refuse loudly:
            # whatever put this day into the final solution is the bug to fix.
            jids = [j.job_id for trip in trips for j in trip]
            if os.environ.get("FP_DEBUG_INFEASIBLE"):
                rv = route_vehicle(vid, day)
                print(f"[FP_DEBUG_INFEASIBLE] {vid} {day} reason={day_eval.failure_reason} "
                      f"veh_start={rv.start_time} shift_end={rv.shift_end}")
                for t_i, tev in enumerate(day_eval.trip_evaluations, start=1):
                    print(f"  trip {t_i}: feasible={tev.feasible} reason={tev.failure_reason} "
                          f"start={tev.route_start} end={tev.route_end}")
                    for s in tev.stops:
                        print(f"    {s.job_id:48} arr={s.arrive} dep={s.depart} "
                              f"wait={s.wait_minutes:.0f} brk={s.break_minutes_before:.0f}")
                for t_i, trip in enumerate(trips, start=1):
                    for j in trip:
                        print(f"  jobdef t{t_i} {j.job_id:46} kind={j.leg_kind} "
                              f"es={j.earliest_start} lf={j.latest_finish}")
            raise ValueError(
                f"cannot emit plan records for {vid} {day}: day evaluates infeasible "
                f"({day_eval.failure_reason}); jobs that would be dropped: {jids}"
            )
        home = home_depot_of(vid)
        for trip_index, (seq, trip_eval) in enumerate(zip(trips, day_eval.trip_evaluations), start=1):
            stop_by_job = {s.job_id: s for s in trip_eval.stops}
            for position, rjob in enumerate(seq, start=1):
                candidate = candidate_by_job.get(rjob.job_id)
                stop = stop_by_job.get(rjob.job_id)
                if candidate is None or stop is None:
                    missing = "candidate row" if candidate is None else "stop timing"
                    raise ValueError(
                        f"job {rjob.job_id} on {vid} {day} trip {trip_index} has no "
                        f"{missing}; refusing to silently drop it from the plan"
                    )
                # The route key is the physical vehicle-day.  Candidate frames are
                # re-dated as unserved work ages, so a later frame can contain a
                # stale service_date for a job whose route was already committed.
                # Never let that mutable planning attribute corrupt emitted dates.
                if isinstance(candidate, dict):
                    candidate = dict(candidate)
                    candidate["service_date"] = str(day)
                builder.assign(
                    route_id=f"ROUTE:{vid}:{day}",
                    vehicle_id=vid,
                    vehicle_home_depot=home,
                    sequence=position,
                    trip_index=trip_index,
                    job=candidate,
                    assignment_reason=reason_for(candidate, home),
                    planned_arrive=stop.arrive,
                    planned_depart=stop.depart,
                    planned_km=stop.leg_km,
                    planned_drive_minutes=stop.drive_minutes,
                    load_pallets_after=stop.load_pallets_after,
                    load_kg_after=stop.load_kg_after,
                    freight_state_after=_FREIGHT_STATE_AFTER.get(rjob.leg_kind, "DELIVERED"),
                    break_minutes_before=float(getattr(stop, "break_minutes_before", 0.0) or 0.0),
                )
    return builder.records
