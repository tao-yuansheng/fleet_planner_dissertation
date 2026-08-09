"""Milestone 3: first feasible seed planner.

A simple, physically valid greedy plan built on the Milestone 2 state engine.
Not optimal — but every selected row is feasible through `planner_state`
(capacity + freight dependencies enforced) and every unselected runnable job
carries a specific reason.

Heuristic:
  1. consider only runnable candidate jobs (``hard_blocker == ""``);
  2. order by service_date, then dependency rank (pickups that produce future
     freight and direct moves first, crossdock deliveries after their pickup),
     then tightest latest_finish;
  3. for each job, prefer a same-depot OK vehicle, then cross-depot; within a
     bucket prefer the least-loaded vehicle, then nearest;
  4. apply one job at a time through `planner_state`;
  5. reject with an explicit reason when no feasible vehicle exists or freight
     is not available.

Vehicle feasibility leans on the compatibility screen (capacity + coarse
time-reachability). Real route sequencing, shift time, and multi-stop load are
Milestone 4.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from freight_planner.planner_state import PlannerState, RejectedJob
from freight_planner.plan_schema import SelectedPlanRecord, selected_plan_frame

# Reasons that are vehicle-independent: trying another vehicle will not help.
_VEHICLE_INDEPENDENT_REASONS = {"DELIVERY_BEFORE_PICKUP", "UNSUPPORTED_LEG_KIND", "FREIGHT_ALREADY_DELIVERED"}

_DEP_RANK = {
    "PRODUCES_DEPOT_FREIGHT": 0,  # crossdock pickup feeding a future delivery
    "NONE_DIRECT": 1,             # direct customer-to-customer move
    "PICKUP_TERMINAL": 2,         # export collection (terminal)
    "PRESTAGED_DELIVERY": 3,      # import delivery, freight already staged
    "REQUIRES_PRIOR_PICKUP": 4,   # crossdock delivery, after its pickup
}


@dataclass
class SeedResult:
    selected: list[SelectedPlanRecord]
    rejected: list[RejectedJob]
    state: PlannerState


def _priority_key(job) -> tuple:
    return (
        str(getattr(job, "service_date", "") or ""),
        _DEP_RANK.get(str(getattr(job, "dependency_type", "") or ""), 5),
        str(getattr(job, "latest_finish", "") or "~"),
        str(getattr(job, "job_id", "") or ""),
    )


def _ok_options_by_leg(compatibility: pd.DataFrame) -> dict[str, list[tuple[str, bool, float]]]:
    if compatibility is None or compatibility.empty:
        return {}
    ok = compatibility[compatibility["compatibility_status"].astype(str).eq("OK")]
    out: dict[str, list[tuple[str, bool, float]]] = {}
    for row in ok.itertuples(index=False):
        leg_id = str(getattr(row, "leg_id", "") or "")
        km = getattr(row, "current_to_service_km", None)
        km_val = float(km) if pd.notna(km) else float("inf")
        out.setdefault(leg_id, []).append((
            str(getattr(row, "vehicle_id", "") or ""),
            bool(getattr(row, "same_depot", False)),
            km_val,
        ))
    return out


def run_seed_plan(
    candidates: pd.DataFrame,
    vehicles: pd.DataFrame,
    compatibility: pd.DataFrame,
    freight_states: pd.DataFrame,
    plan_id: str = "SEED",
) -> SeedResult:
    state = PlannerState.from_frames(vehicles, freight_states, plan_id=plan_id)
    rejected: list[RejectedJob] = []
    if candidates is None or candidates.empty:
        return SeedResult(state.selected_records, rejected, state)

    runnable = candidates[candidates.get("hard_blocker", pd.Series(dtype=str)).fillna("").eq("")]
    options_by_leg = _ok_options_by_leg(compatibility)
    ordered = sorted(runnable.itertuples(index=False), key=_priority_key)

    def load_of(vehicle_id: str) -> int:
        runtime = state.vehicles.get(vehicle_id)
        return len(runtime.assigned_job_ids) if runtime is not None else 10 ** 9

    for job in ordered:
        leg_id = str(getattr(job, "leg_id", "") or "")
        job_id = str(getattr(job, "job_id", "") or "")
        options = options_by_leg.get(leg_id, [])
        if not options:
            rejected.append(RejectedJob(job_id=job_id, reason="NO_OK_VEHICLE_PAIR"))
            continue
        # same-depot first, then least-loaded, then nearest
        ranked = sorted(options, key=lambda o: (0 if o[1] else 1, load_of(o[0]), o[2]))

        assigned = False
        last_reason = "NO_OK_VEHICLE_PAIR"
        for vehicle_id, same_depot, _km in ranked:
            ok, reason = state.can_apply(job, vehicle_id)
            if ok:
                state.apply_job(
                    job, vehicle_id,
                    assignment_reason="SAME_DEPOT_SEED" if same_depot else "CROSS_DEPOT_SEED",
                )
                assigned = True
                break
            last_reason = reason
            if reason in _VEHICLE_INDEPENDENT_REASONS:
                break
        if not assigned:
            rejected.append(RejectedJob(job_id=job_id, reason=last_reason))

    return SeedResult(state.selected_records, rejected, state)


def seed_plan_frame(result: SeedResult) -> pd.DataFrame:
    return selected_plan_frame(result.selected)


def seed_rejection_frame(result: SeedResult) -> pd.DataFrame:
    rows = [{"job_id": r.job_id, "reason": r.reason} for r in result.rejected]
    return pd.DataFrame(rows, columns=["job_id", "reason"])
