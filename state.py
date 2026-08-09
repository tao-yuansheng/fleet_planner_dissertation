from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class FreightStateRecord:
    freight_id: str
    order_id: str
    order_name: str
    initial_state: str
    initial_node: str
    initial_depot: str
    ready_time: str
    source: str
    dispatchable_leg_count: int
    trunk_leg_count: int
    accounting_status: str

    def to_dict(self) -> dict:
        return asdict(self)


def _pre_window_collected(legs_df: pd.DataFrame, planning_start: date | None) -> bool:
    """True if this freight's pickup/direct collection happened before the window opened —
    i.e. it is already in our network at window start (run-to-run state gap)."""
    if planning_start is None or legs_df.empty or "service_date" not in legs_df.columns:
        return False
    start_iso = planning_start.isoformat()
    collect = legs_df[legs_df.get("leg_kind", "").isin(["CUSTOMER_PICKUP", "DIRECT_CUSTOMER_MOVE"])]
    if collect.empty:
        return False
    earliest = min(str(v)[:10] for v in collect["service_date"] if str(v))
    return bool(earliest) and earliest < start_iso


def build_initial_freight_states(
    demand: pd.DataFrame, legs: pd.DataFrame, planning_start: date | None = None,
    staged_overrides: dict[str, tuple[str, str]] | None = None,
) -> list[FreightStateRecord]:
    if demand.empty:
        return []
    by_order = {str(r.order_id): r for r in demand.itertuples(index=False)}
    leg_groups = legs.groupby("order_id") if not legs.empty else []
    legs_by_order = {str(order_id): grp.copy() for order_id, grp in leg_groups}

    out: list[FreightStateRecord] = []
    for order_id, row in by_order.items():
        grp = legs_by_order.get(order_id, pd.DataFrame())
        freight_ids = [order_id]
        if not grp.empty and "freight_id" in grp.columns:
            ids = [str(v) for v in grp["freight_id"].dropna().unique() if str(v)]
            freight_ids = ids or freight_ids

        shape = str(row.responsibility_shape)
        for freight_id in freight_ids:
            fgrp = grp
            if not grp.empty and "freight_id" in grp.columns:
                fgrp = grp[grp["freight_id"].astype(str).eq(str(freight_id))]
            dispatchable_count = int(fgrp[fgrp.get("dispatchable", False) == True].shape[0]) if not fgrp.empty else 0
            trunk_count = int(fgrp[fgrp.get("leg_kind", "").isin(["INBOUND_TRUNK", "OUTBOUND_TRUNK"])].shape[0]) if not fgrp.empty else 0
            statuses = set(str(x) for x in fgrp.get("planner_status", []) if str(x)) if not fgrp.empty else set()
            accounting_status = "DISPATCHABLE" if dispatchable_count else (next(iter(statuses)) if statuses else "NO_LEGS")

            pickup = fgrp[fgrp.get("leg_kind", "") == "CUSTOMER_PICKUP"] if not fgrp.empty else pd.DataFrame()
            direct = fgrp[fgrp.get("leg_kind", "") == "DIRECT_CUSTOMER_MOVE"] if not fgrp.empty else pd.DataFrame()
            delivery = fgrp[fgrp.get("leg_kind", "") == "CUSTOMER_DELIVERY"] if not fgrp.empty else pd.DataFrame()

            if str(row.exclusion_reason):
                initial_state = str(row.exclusion_reason)
                initial_node = "MANUAL_OR_OUT_OF_SCOPE"
                initial_depot = ""
                ready_time = ""
            elif shape in {"NETWORK_IMPORT", "DELIVERY_ONLY"}:
                initial_state = "AT_DEPOT_OR_HUB_PENDING"
                initial_depot = str(delivery.iloc[0].source_depot) if not delivery.empty else ""
                initial_node = initial_depot or "DEPOT"
                ready_time = str(delivery.iloc[0].freight_ready_time) if not delivery.empty else ""
            elif shape in {"NETWORK_EXPORT", "PICKUP_ONLY"}:
                initial_state = "AT_CUSTOMER_ORIGIN"
                initial_depot = str(pickup.iloc[0].source_depot) if not pickup.empty else ""
                initial_node = str(pickup.iloc[0].service_pc) if not pickup.empty else str(row.origin_pc)
                ready_time = str(pickup.iloc[0].freight_ready_time) if not pickup.empty else str(row.collect_timestamp)
            elif shape == "FULL_END_TO_END":
                if not delivery.empty and _pre_window_collected(fgrp, planning_start):
                    # Collected before the window opened → already at a depot in our
                    # network, ready for its in-window delivery (not at customer origin).
                    # Ready since collection (pre-window), so it can be delivered any time
                    # in the window — carry the collection ready_time, not the delivery's.
                    collect_leg = pickup if not pickup.empty else direct
                    initial_state = "AT_DEPOT_OR_HUB_PENDING"
                    initial_depot = str(delivery.iloc[0].source_depot)
                    initial_node = initial_depot or "DEPOT"
                    ready_time = str(collect_leg.iloc[0].freight_ready_time) if not collect_leg.empty else str(delivery.iloc[0].freight_ready_time)
                elif not pickup.empty:
                    initial_state = "AT_CUSTOMER_ORIGIN"
                    initial_depot = str(pickup.iloc[0].source_depot)
                    initial_node = str(pickup.iloc[0].service_pc)
                    ready_time = str(pickup.iloc[0].freight_ready_time)
                elif not direct.empty:
                    initial_state = "AT_CUSTOMER_ORIGIN"
                    initial_depot = str(direct.iloc[0].source_depot)
                    initial_node = str(row.origin_pc)
                    ready_time = str(row.collect_timestamp)
                elif not delivery.empty:
                    initial_state = "AT_DEPOT_OR_HUB_PENDING"
                    initial_depot = str(delivery.iloc[0].source_depot)
                    initial_node = initial_depot or "DEPOT"
                    ready_time = str(delivery.iloc[0].freight_ready_time)
                else:
                    initial_state = "MANUAL_HANDLING"
                    initial_node = "MANUAL"
                    initial_depot = ""
                    ready_time = ""
            else:
                initial_state = "MANUAL_HANDLING"
                initial_node = "MANUAL"
                initial_depot = ""
                ready_time = ""

            if staged_overrides and order_id in staged_overrides:
                ov_depot, ov_ready = staged_overrides[order_id]
                initial_state = "AT_DEPOT_OR_HUB_PENDING"
                initial_depot = str(ov_depot)
                initial_node = str(ov_depot) or "DEPOT"
                ready_time = str(ov_ready) or ready_time

            out.append(FreightStateRecord(
                freight_id=freight_id,
                order_id=order_id,
                order_name=str(row.order_name),
                initial_state=initial_state,
                initial_node=initial_node,
                initial_depot=initial_depot,
                ready_time=ready_time,
                source=str(row.responsibility_source),
                dispatchable_leg_count=dispatchable_count,
                trunk_leg_count=trunk_count,
                accounting_status=accounting_status,
            ))
    return out
