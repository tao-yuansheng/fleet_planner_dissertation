"""Milestone 2: the mutable planning state engine.

`PlannerState` owns the horizon's mutable resources: vehicle runtime state, the
freight ledger, and the selected / completed / rejected job sets. `apply_job`
is the single mutation entry point — it validates a job against vehicle capacity
and freight availability, applies the freight transition through the ledger, and
either records a `SelectedPlanRecord` (Milestone 1 schema) or a rejection with a
specific reason.

Scope note: time and travel advancement are intentionally coarse here. Real
route timelines, multi-stop load accumulation, and km are Milestone 4
(routing adapter). This milestone establishes the state structures and the
physical freight/capacity gates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from freight_planner.freight_ledger import FREIGHT_DELIVERED, FreightLedger
from freight_planner.plan_schema import SelectedPlanBuilder, SelectedPlanRecord

CUSTOMER_PICKUP = "CUSTOMER_PICKUP"
CUSTOMER_DELIVERY = "CUSTOMER_DELIVERY"
DIRECT_CUSTOMER_MOVE = "DIRECT_CUSTOMER_MOVE"


@dataclass
class VehicleRuntime:
    vehicle_id: str
    home_depot: str
    current_node: str
    available_from: str
    shift_end: str
    capacity_kg: float
    capacity_pallets: float
    vehicle_type: str
    assigned_job_ids: list[str] = field(default_factory=list)


@dataclass
class RejectedJob:
    job_id: str
    reason: str


@dataclass
class ApplyResult:
    ok: bool
    job_id: str
    reason: str
    record: SelectedPlanRecord | None = None


def _get(job: Any, key: str, default: Any = "") -> Any:
    if isinstance(job, dict):
        value = job.get(key, default)
    else:
        value = getattr(job, key, default)
    return default if value is None else value


class PlannerState:
    def __init__(self, vehicles: dict[str, VehicleRuntime], ledger: FreightLedger,
                 plan_id: str = "PLAN") -> None:
        self.vehicles = vehicles
        self.ledger = ledger
        self._builder = SelectedPlanBuilder(plan_id=plan_id)
        self.completed_job_ids: list[str] = []
        self.rejected: list[RejectedJob] = []

    @classmethod
    def from_frames(cls, vehicle_states: pd.DataFrame, freight_states: pd.DataFrame,
                    plan_id: str = "PLAN") -> "PlannerState":
        vehicles: dict[str, VehicleRuntime] = {}
        if vehicle_states is not None and not vehicle_states.empty:
            for row in vehicle_states.itertuples(index=False):
                vid = str(getattr(row, "vehicle_id", "") or "")
                if not vid:
                    continue
                vehicles[vid] = VehicleRuntime(
                    vehicle_id=vid,
                    home_depot=str(getattr(row, "home_depot", "") or ""),
                    current_node=str(getattr(row, "current_node", "") or ""),
                    available_from=str(getattr(row, "available_from", "") or ""),
                    shift_end=str(getattr(row, "shift_end", "") or ""),
                    capacity_kg=float(getattr(row, "capacity_kg", 0.0) or 0.0),
                    capacity_pallets=float(getattr(row, "capacity_pallets", 0.0) or 0.0),
                    vehicle_type=str(getattr(row, "vehicle_type", "") or ""),
                )
        ledger = FreightLedger.from_initial_states(freight_states)
        return cls(vehicles, ledger, plan_id=plan_id)

    @property
    def selected_records(self) -> list[SelectedPlanRecord]:
        return self._builder.records

    def _reject(self, job_id: str, reason: str) -> ApplyResult:
        self.rejected.append(RejectedJob(job_id=job_id, reason=reason))
        return ApplyResult(ok=False, job_id=job_id, reason=reason)

    def can_apply(self, job: Any, vehicle_id: str) -> tuple[bool, str]:
        """Non-mutating feasibility probe. Returns (ok, reason).

        Lets a planner try several vehicles for a job without committing state or
        recording spurious rejections. ``apply_job`` shares this logic.
        """
        job_id = str(_get(job, "job_id"))
        order_id = str(_get(job, "order_id"))
        leg_kind = str(_get(job, "leg_kind"))

        vehicle = self.vehicles.get(str(vehicle_id))
        if vehicle is None:
            return False, "UNKNOWN_VEHICLE"
        if job_id in self._builder.selected_job_ids():
            return False, "DUPLICATE_JOB"

        pallets = float(_get(job, "pallets", 0.0) or 0.0)
        weight_kg = float(_get(job, "weight_kg", 0.0) or 0.0)
        if pallets > vehicle.capacity_pallets or weight_kg > vehicle.capacity_kg:
            return False, "CAPACITY_EXCEEDED"

        source_depot = str(_get(job, "source_depot"))
        if leg_kind == CUSTOMER_PICKUP:
            if self.ledger.state_of(order_id) == FREIGHT_DELIVERED:
                return False, "FREIGHT_ALREADY_DELIVERED"
            return True, "OK"
        if leg_kind == CUSTOMER_DELIVERY:
            if not self.ledger.exists_at_depot(order_id, source_depot):
                return False, "DELIVERY_BEFORE_PICKUP"
            return True, "OK"
        if leg_kind == DIRECT_CUSTOMER_MOVE:
            unit = self.ledger.get(order_id)
            if unit is None or unit.state == FREIGHT_DELIVERED:
                return False, "DELIVERY_BEFORE_PICKUP"
            return True, "OK"
        return False, "UNSUPPORTED_LEG_KIND"

    def apply_job(self, job: Any, vehicle_id: str, *, assignment_reason: str = "SEED") -> ApplyResult:
        job_id = str(_get(job, "job_id"))
        order_id = str(_get(job, "order_id"))
        leg_kind = str(_get(job, "leg_kind"))

        ok, reason = self.can_apply(job, vehicle_id)
        if not ok:
            return self._reject(job_id, reason)

        vehicle = self.vehicles[str(vehicle_id)]
        source_depot = str(_get(job, "source_depot"))
        target_depot = str(_get(job, "target_depot"))
        state_before = self.ledger.state_of(order_id)
        if leg_kind == CUSTOMER_PICKUP:
            self.ledger.pickup_to_depot(order_id, target_depot)
        elif leg_kind == CUSTOMER_DELIVERY:
            self.ledger.deliver_from_depot(order_id, source_depot)
        else:  # DIRECT_CUSTOMER_MOVE (only remaining can_apply-approved kind)
            self.ledger.deliver_direct(order_id)

        state_after = self.ledger.state_of(order_id)
        service_date = str(_get(job, "service_date"))
        route_id = f"ROUTE:{vehicle.vehicle_id}:{service_date}"
        sequence = len(vehicle.assigned_job_ids) + 1
        # Freight terminates at depot or customer on completion of a single leg;
        # multi-stop on-board load accumulation is Milestone 4.
        record = self._builder.assign(
            route_id=route_id,
            vehicle_id=vehicle.vehicle_id,
            vehicle_home_depot=vehicle.home_depot,
            sequence=sequence,
            job=job,
            assignment_reason=assignment_reason,
            load_pallets_after=0.0,
            load_kg_after=0.0,
            freight_state_before=state_before,
            freight_state_after=state_after,
        )
        vehicle.assigned_job_ids.append(job_id)
        vehicle.current_node = record.destination_node or vehicle.current_node
        self.completed_job_ids.append(job_id)
        return ApplyResult(ok=True, job_id=job_id, reason=assignment_reason, record=record)

    def ledger_violations(self, candidates: pd.DataFrame):
        from freight_planner.plan_schema import plan_ledger_violations
        return plan_ledger_violations(self.selected_records, candidates)
