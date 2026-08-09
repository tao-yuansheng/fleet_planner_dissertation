"""Milestone 1: the selected-plan schema.

A selected plan is the planner's *output*, distinct from the candidate jobs that
are its input. This module defines one stable plan row, a builder that enforces
the only hard invariant available at schema level (a job is assigned at most
once), and a thin bridge to the existing freight ledger so a selected plan can
be checked for delivery-before-pickup violations.

Routing, timing, load, and freight-state fields exist in the schema but are
populated by later milestones (state engine + routing adapter); here they
default to empty/zero so a plan can be constructed from a bare candidate job.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from freight_planner.route_costs import drive_minutes
from freight_planner.ledger import LedgerViolation, validate_selected_jobs


class DuplicateAssignmentError(Exception):
    """Raised when the same job_id is assigned to a plan more than once."""


@dataclass(frozen=True)
class SelectedPlanRecord:
    plan_id: str
    route_id: str
    trip_id: str
    vehicle_id: str
    vehicle_home_depot: str
    service_date: str
    sequence: int
    trip_index: int
    job_id: str
    leg_id: str
    order_id: str
    leg_kind: str
    origin_node: str
    destination_node: str
    planned_arrive: str
    planned_depart: str
    planned_km: float
    planned_drive_minutes: float
    load_pallets_after: float
    load_kg_after: float
    freight_state_before: str
    freight_state_after: str
    assignment_reason: str
    break_minutes_before: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


SELECTED_PLAN_COLUMNS = [f.name for f in SelectedPlanRecord.__dataclass_fields__.values()]


def _ts_seconds(value) -> str:
    """Planned timestamps normalized to whole-second '%Y-%m-%d %H:%M:%S'.

    Emission used to mix sub-second ALNS floats with whole-second wait-floored
    stamps; any naive pd.to_datetime on the CSVs silently NaT'd whichever format
    it didn't infer (the 'missing planned_arrive' ghost, 2026-07-21 — 80% of a
    run mis-parsed). One format at the mint ends the class of bug. Blanks and
    unparseable values pass through unchanged."""
    s = str(value or "")
    if not s:
        return s
    ts = pd.to_datetime(s, errors="coerce", format="mixed")
    return s if pd.isna(ts) else ts.strftime("%Y-%m-%d %H:%M:%S")


def _get(job: Any, key: str, default: str = "") -> Any:
    if isinstance(job, dict):
        value = job.get(key, default)
    else:
        value = getattr(job, key, default)
    return default if value is None else value


@dataclass
class SelectedPlanBuilder:
    """Accumulates plan rows, rejecting any job assigned twice."""

    plan_id: str
    records: list[SelectedPlanRecord] = field(default_factory=list)
    _assigned_job_ids: set[str] = field(default_factory=set)

    def assign(
        self,
        *,
        route_id: str,
        vehicle_id: str,
        vehicle_home_depot: str,
        sequence: int,
        job: Any,
        assignment_reason: str,
        trip_index: int = 0,
        planned_arrive: str = "",
        planned_depart: str = "",
        planned_km: float = 0.0,
        planned_drive_minutes: float = 0.0,
        load_pallets_after: float = 0.0,
        load_kg_after: float = 0.0,
        freight_state_before: str = "",
        freight_state_after: str = "",
        break_minutes_before: float = 0.0,
    ) -> SelectedPlanRecord:
        job_id = str(_get(job, "job_id"))
        if job_id in self._assigned_job_ids:
            raise DuplicateAssignmentError(
                f"job_id {job_id!r} already assigned in plan {self.plan_id!r}"
            )
        record = SelectedPlanRecord(
            plan_id=self.plan_id,
            route_id=str(route_id),
            trip_id=f"{route_id}#T{int(trip_index)}",
            vehicle_id=str(vehicle_id),
            vehicle_home_depot=str(vehicle_home_depot),
            service_date=str(_get(job, "service_date")),
            sequence=int(sequence),
            trip_index=int(trip_index),
            job_id=job_id,
            leg_id=str(_get(job, "leg_id")),
            order_id=str(_get(job, "order_id")),
            leg_kind=str(_get(job, "leg_kind")),
            origin_node=str(_get(job, "preferred_start_node")),
            destination_node=str(_get(job, "preferred_end_node")),
            planned_arrive=_ts_seconds(planned_arrive),
            planned_depart=_ts_seconds(planned_depart),
            planned_km=float(planned_km),
            planned_drive_minutes=float(planned_drive_minutes),
            load_pallets_after=float(load_pallets_after),
            load_kg_after=float(load_kg_after),
            freight_state_before=str(freight_state_before),
            freight_state_after=str(freight_state_after),
            assignment_reason=str(assignment_reason),
            break_minutes_before=float(break_minutes_before),
        )
        self.records.append(record)
        self._assigned_job_ids.add(job_id)
        return record

    def selected_leg_ids(self) -> set[str]:
        return {r.leg_id for r in self.records}

    def selected_job_ids(self) -> set[str]:
        return set(self._assigned_job_ids)


def selected_plan_frame(records: list[SelectedPlanRecord]) -> pd.DataFrame:
    rows = [r.to_dict() for r in records]
    return pd.DataFrame(rows, columns=SELECTED_PLAN_COLUMNS)


def plan_ledger_violations(
    records: list[SelectedPlanRecord],
    candidates: pd.DataFrame,
) -> list[LedgerViolation]:
    """Check a selected plan against the freight ledger.

    Reuses the candidate dependency metadata: a selected delivery whose pickup
    predecessor is not also selected is a delivery-before-pickup violation.
    """
    selected_leg_ids = {r.leg_id for r in records}
    return validate_selected_jobs(candidates, selected_leg_ids)


def selected_plan_export_frame(
    records: list[SelectedPlanRecord],
    route_totals: dict | None = None,
    route_drive_totals: dict | None = None,
) -> pd.DataFrame:
    """Selected-plan CSV view with synthetic depot-return movements.

    The core selected plan remains one row per assigned job for ledger/KPI logic.
    This export view adds non-job `DEPOT_RETURN` rows per trip so the CSV also
    reconciles with route-level km without inflating assigned job counts.
    """
    df = selected_plan_frame(records)
    if not route_totals or df.empty:
        return df

    rows = df.to_dict("records")
    group_cols = ["route_id", "trip_index"] if "trip_index" in df.columns else ["route_id"]
    route_group_counts = df.groupby("route_id")["trip_index"].nunique().to_dict() if "trip_index" in df.columns else {}
    by_trip = df.groupby(group_cols, sort=False)
    for key, grp in by_trip:
        if isinstance(key, tuple) and len(key) > 1:
            route_id, trip_index = str(key[0]), int(key[1] or 0)
        elif isinstance(key, tuple):
            route_id, trip_index = str(key[0]), 0
        else:
            route_id, trip_index = str(key), 0
        trip_key = f"{route_id}#T{trip_index}"
        leg_km = float(grp["planned_km"].astype(float).sum())
        leg_drive = float(grp["planned_drive_minutes"].astype(float).sum())
        if trip_key in route_totals:
            total_km = float(route_totals[trip_key])
        elif route_id in route_totals and (not route_group_counts or int(route_group_counts.get(route_id, 1)) == 1):
            total_km = float(route_totals[route_id])
        else:
            total_km = leg_km
        residual = max(0.0, total_km - leg_km)
        if residual <= 1e-6:
            continue
        last = grp.sort_values("sequence").iloc[-1].to_dict()
        if trip_key in (route_drive_totals or {}):
            total_drive = float(route_drive_totals[trip_key])
        elif route_id in (route_drive_totals or {}) and (
                not route_group_counts or int(route_group_counts.get(route_id, 1)) == 1):
            total_drive = float(route_drive_totals[route_id])
        else:
            total_drive = None
        residual_drive = (
            max(0.0, total_drive - leg_drive)
            if total_drive is not None
            else residual * ((leg_drive / leg_km) if (leg_km > 0 and leg_drive > 0) else 1.2)
        )
        rows.append({
            "plan_id": last.get("plan_id", ""),
            "route_id": route_id,
            "trip_id": f"{route_id}#T{trip_index}",
            "vehicle_id": last.get("vehicle_id", ""),
            "vehicle_home_depot": last.get("vehicle_home_depot", ""),
            "service_date": last.get("service_date", ""),
            "sequence": int(last.get("sequence", 0) or 0) + 1,
            "trip_index": trip_index,
            "job_id": f"__RETURN__:{route_id}:T{trip_index}",
            "leg_id": "",
            "order_id": "",
            "leg_kind": "DEPOT_RETURN",
            "origin_node": last.get("destination_node", ""),
            "destination_node": "DEPOT",
            "planned_arrive": "",
            "planned_depart": "",
            "planned_km": residual,
            "planned_drive_minutes": residual_drive,
            "load_pallets_after": 0.0,
            "load_kg_after": 0.0,
            "freight_state_before": "",
            "freight_state_after": "",
            "assignment_reason": "DEPOT_RETURN_ACCOUNTING",
            "break_minutes_before": 0.0,
        })
    return pd.DataFrame(rows, columns=SELECTED_PLAN_COLUMNS)
