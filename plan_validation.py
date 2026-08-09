"""Temporal validity guard: a delivery cannot precede its pickup in time.

The freight ledger guarantees a delivery is only selected if its pickup is also
selected, but that is set-membership, not time. This checks the *final* plan's
timestamps: for any order with both a pickup and a delivery, the delivery must
not arrive before the pickup departs (it cannot carry freight that has not been
collected). With same-day crossdock disabled, multi-day crossdock is day-
separated, so this should be empty 鈥?it is a regression guard.
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

from freight_planner.legs import SAME_DAY_XDOCK_HANDOFF_MIN

VIOLATION_COLUMNS = [
    "order_id", "pickup_vehicle", "pickup_depart", "delivery_vehicle", "delivery_arrive",
    "required_ready_time", "violation_type",
]


def temporal_violations(selected: pd.DataFrame) -> pd.DataFrame:
    if selected is None or selected.empty:
        return pd.DataFrame(columns=VIOLATION_COLUMNS)

    pickups = selected[selected["leg_kind"] == "CUSTOMER_PICKUP"]
    deliveries = selected[selected["leg_kind"] == "CUSTOMER_DELIVERY"]
    if pickups.empty or deliveries.empty:
        return pd.DataFrame(columns=VIOLATION_COLUMNS)

    pick = (pickups.sort_values("planned_depart")
            .groupby("order_id")
            .agg(pickup_vehicle=("vehicle_id", "first"), pickup_depart=("planned_depart", "first")))
    dele = (deliveries.sort_values("planned_arrive")
            .groupby("order_id")
            .agg(delivery_vehicle=("vehicle_id", "first"), delivery_arrive=("planned_arrive", "first")))

    merged = pick.join(dele, how="inner").reset_index()
    p_dep = pd.to_datetime(merged["pickup_depart"], errors="coerce")
    d_arr = pd.to_datetime(merged["delivery_arrive"], errors="coerce")
    same_day = p_dep.dt.date == d_arr.dt.date
    required = p_dep.where(~same_day, p_dep + timedelta(minutes=SAME_DAY_XDOCK_HANDOFF_MIN))
    mask = p_dep.notna() & d_arr.notna() & (d_arr < required)
    violations = merged[mask].copy()
    if violations.empty:
        return pd.DataFrame(columns=VIOLATION_COLUMNS)
    violations["required_ready_time"] = required[mask].dt.strftime("%Y-%m-%d %H:%M:%S")
    violations["violation_type"] = [
        "SAME_DAY_HANDOFF_BUFFER" if sd else "DELIVERY_BEFORE_PICKUP"
        for sd in same_day[mask]
    ]
    return violations[VIOLATION_COLUMNS].reset_index(drop=True)
