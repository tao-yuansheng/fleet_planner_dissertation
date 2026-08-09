from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

import pandas as pd

from freight_planner.dayflex import day_flex_min as compute_day_flex_min

CUSTOMER_LEG_KINDS = {"CUSTOMER_PICKUP", "CUSTOMER_DELIVERY", "DIRECT_CUSTOMER_MOVE", "HUB_DROP"}


def _opt_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class CandidateJobRecord:
    job_id: str
    leg_id: str
    freight_id: str
    order_id: str
    order_name: str
    flow: str
    hub: str
    leg_kind: str
    service_date: str
    service_pc: str
    preferred_start_node: str
    preferred_end_node: str
    source_depot: str
    target_depot: str
    option_set: str
    option_group: str
    origin_lat: float | None
    origin_lon: float | None
    ready_state: str
    result_state: str
    predecessor_leg_id: str
    successor_leg_id: str
    dependency_type: str
    earliest_start: str
    latest_finish: str
    freight_ready_time: str
    pallets: float
    weight_kg: float
    allowed_vehicle_types: str
    feasible_vehicle_count: int
    same_depot_feasible_vehicle_count: int
    hard_blocker: str
    origin_pc: str = ""   # DIRECT/HUB-DROP collection postcode (two-point legs)
    # K2 v1: earliest allowed service day for depot-controlled FF deliveries
    # ("" = pinned to service_date, today's behavior). See dayflex.day_flex_min.
    day_flex_min: str = ""
    depart_floor: str = ""  # collocated depot-delivery: trip may not DEPART before this
    depot_bound: str = ""   # collocated depot-delivery: serving vehicle's required home depot
    # Soft delivery window (2026-07-18): the customer's TIGHT window, carried through
    # to the RouteJob's window_open/deadline for the earliness/tardiness penalty.
    raw_window_start: str = ""
    raw_window_end: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _allowed_types(row: pd.Series, vehicles: pd.DataFrame) -> list[str]:
    required_kg = float(row.get("weight_kg") or 0.0)
    required_pallets = float(row.get("pallets") or 0.0)
    feasible = vehicles[
        (vehicles["capacity_kg"] >= required_kg) &
        (vehicles["capacity_pallets"] >= required_pallets)
    ]
    types = sorted(str(v) for v in feasible["vehicle_type"].dropna().unique())
    return types


def _vehicle_counts(row: pd.Series, vehicles: pd.DataFrame) -> tuple[int, int]:
    required_kg = float(row.get("weight_kg") or 0.0)
    required_pallets = float(row.get("pallets") or 0.0)
    feasible = vehicles[
        (vehicles["capacity_kg"] >= required_kg) &
        (vehicles["capacity_pallets"] >= required_pallets)
    ]
    depot = str(row.get("source_depot") or "")
    same_depot = feasible[feasible["home_depot"].astype(str).eq(depot)] if depot else feasible.iloc[0:0]
    return int(len(feasible)), int(len(same_depot))


def _hard_blocker(row: pd.Series, feasible_count: int, planning_start: date | None = None) -> str:
    if planning_start is not None:
        service_date = pd.to_datetime(row.get("service_date"), errors="coerce")
        if pd.notna(service_date) and service_date.date() < planning_start:
            return "BEFORE_PLANNING_START"
    status = str(row.get("planner_status") or "")
    if status != "DISPATCHABLE":
        return status
    if feasible_count <= 0:
        return "NO_CAPABLE_VEHICLE"
    if not bool(row.get("geocode_ok", True)):
        return "BAD_GEOCODE"
    if not str(row.get("effective_window_start") or "") or not str(row.get("effective_window_end") or ""):
        return "MISSING_WINDOW"
    return ""


def _dependency_maps(customer: pd.DataFrame, planning_start: date | None = None) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    predecessor: dict[str, str] = {}
    successor: dict[str, str] = {}
    dep_type: dict[str, str] = {}
    if customer.empty:
        return predecessor, successor, dep_type
    start_iso = planning_start.isoformat() if planning_start is not None else None
    group_col = "freight_id" if "freight_id" in customer.columns else "order_id"
    for _freight_id, grp in customer.groupby(group_col, dropna=False):
        pickups = grp[grp["leg_kind"].eq("CUSTOMER_PICKUP")]
        deliveries = grp[grp["leg_kind"].eq("CUSTOMER_DELIVERY")]
        direct = grp[grp["leg_kind"].eq("DIRECT_CUSTOMER_MOVE")]
        hub_drops = grp[grp["leg_kind"].eq("HUB_DROP")]
        if not direct.empty:
            leg_id = str(direct.iloc[0].leg_id)
            dep_type[leg_id] = "NONE_DIRECT"
        for row in hub_drops.itertuples(index=False):
            # a hub-drop is a terminal collection (freight leaves at the hub)
            dep_type[str(row.leg_id)] = "PICKUP_TERMINAL"
        if pickups.empty and not deliveries.empty:
            for row in deliveries.itertuples(index=False):
                dep_type[str(row.leg_id)] = "PRESTAGED_DELIVERY"
            continue
        if deliveries.empty and not pickups.empty:
            for row in pickups.itertuples(index=False):
                dep_type[str(row.leg_id)] = "PICKUP_TERMINAL"
            continue
        if pickups.empty or deliveries.empty:
            continue
        pickup = pickups.sort_values(["service_date", "leg_id"]).iloc[0]
        delivery = deliveries.sort_values(["service_date", "leg_id"]).iloc[0]
        pickup_leg = str(pickup.leg_id)
        delivery_leg = str(delivery.leg_id)
        # If the pickup is BEFORE the planning window opened, the freight was already
        # collected and is in our network at window start. Its in-window delivery is a
        # prestaged delivery, NOT dependent on the (never-planned) pre-window pickup —
        # otherwise the delivery is orphaned (run-to-run state gap).
        if start_iso is not None and str(pickup.service_date)[:10] < start_iso:
            dep_type[pickup_leg] = "PRODUCES_DEPOT_FREIGHT"  # pre-window; hard-blocked, no successor
            dep_type[delivery_leg] = "PRESTAGED_DELIVERY"
            continue
        successor[pickup_leg] = delivery_leg
        predecessor[delivery_leg] = pickup_leg
        dep_type[pickup_leg] = "PRODUCES_DEPOT_FREIGHT"
        dep_type[delivery_leg] = "REQUIRES_PRIOR_PICKUP"
    return predecessor, successor, dep_type


def build_candidate_jobs(
    legs: pd.DataFrame,
    vehicle_states: pd.DataFrame,
    planning_start: date | None = None,
) -> list[CandidateJobRecord]:
    if legs.empty:
        return []
    candidates: list[CandidateJobRecord] = []
    customer = legs[legs["leg_kind"].isin(CUSTOMER_LEG_KINDS)].copy()
    predecessor, successor, dep_type = _dependency_maps(customer, planning_start)
    for _, row in customer.iterrows():
        leg_id = str(row.get("leg_id") or "")
        feasible_count, same_depot_count = _vehicle_counts(row, vehicle_states)
        allowed = _allowed_types(row, vehicle_states)
        blocker = _hard_blocker(row, feasible_count, planning_start)
        if str(row.get("planner_status") or "") != "DISPATCHABLE" and not blocker:
            blocker = str(row.get("planner_status") or "")
        if blocker and blocker not in {"NO_CAPABLE_VEHICLE", "MISSING_WINDOW"}:
            # Keep non-dispatchable customer rows visible in the candidate table,
            # but the optimizer will only consume rows with hard_blocker == "".
            allowed = []
        candidates.append(CandidateJobRecord(
            job_id=f"JOB:{leg_id}",
            leg_id=leg_id,
            freight_id=str(row.get("freight_id") or row.get("order_id") or ""),
            order_id=str(row.get("order_id") or ""),
            order_name=str(row.get("order_name") or ""),
            flow=str(row.get("flow") or ""),
            hub=str(row.get("hub") or ""),
            leg_kind=str(row.get("leg_kind") or ""),
            service_date=str(row.get("service_date") or ""),
            service_pc=str(row.get("service_pc") or ""),
            preferred_start_node=str(row.get("origin_node") or ""),
            preferred_end_node=str(row.get("destination_node") or ""),
            source_depot=str(row.get("source_depot") or ""),
            target_depot=str(row.get("target_depot") or ""),
            option_set=str(row.get("option_set") or ""),
            option_group=str(row.get("option_group") or ""),
            origin_lat=_opt_float(row.get("origin_lat")),
            origin_lon=_opt_float(row.get("origin_lon")),
            origin_pc=str(row.get("origin_pc") or ""),
            ready_state=str(row.get("ready_state") or ""),
            result_state=str(row.get("result_state") or ""),
            predecessor_leg_id=predecessor.get(leg_id, ""),
            successor_leg_id=successor.get(leg_id, ""),
            dependency_type=dep_type.get(leg_id, "NONE"),
            earliest_start=str(row.get("effective_window_start") or ""),
            latest_finish=str(row.get("effective_window_end") or ""),
            freight_ready_time=str(row.get("freight_ready_time") or ""),
            pallets=float(row.get("pallets") or 0.0),
            weight_kg=float(row.get("weight_kg") or 0.0),
            allowed_vehicle_types=",".join(allowed),
            feasible_vehicle_count=feasible_count,
            same_depot_feasible_vehicle_count=same_depot_count,
            hard_blocker=blocker,
            day_flex_min=compute_day_flex_min(
                flow=str(row.get("flow") or ""),
                leg_kind=str(row.get("leg_kind") or ""),
                dependency_type=dep_type.get(leg_id, "NONE"),
                service_date=str(row.get("service_date") or ""),
                freight_ready_time=str(row.get("freight_ready_time") or ""),
                raw_window_start=str(row.get("raw_window_start") or ""),
                planning_start=planning_start,
            ),
            depart_floor=str(row.get("depart_floor") or ""),
            depot_bound=str(row.get("depot_bound") or ""),
            raw_window_start=str(row.get("raw_window_start") or ""),
            raw_window_end=str(row.get("raw_window_end") or ""),
        ))
    return candidates


def candidate_jobs_frame(
    legs: pd.DataFrame,
    vehicle_states: pd.DataFrame,
    planning_start: date | None = None,
) -> pd.DataFrame:
    return pd.DataFrame([record.to_dict() for record in build_candidate_jobs(legs, vehicle_states, planning_start)])
