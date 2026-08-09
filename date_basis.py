from __future__ import annotations

from datetime import date

import pandas as pd

SERVICE_DATE = "service_date"
DEMAND_TOUCH = "demand_touch"
MANIFEST_COMPAT = "manifest_compat"
PLANNING_WINDOW = "planning_window"

VALID_BASIS = {SERVICE_DATE, DEMAND_TOUCH, MANIFEST_COMPAT, PLANNING_WINDOW}
CUSTOMER_OR_ACCOUNTING_LEGS = [
    "CUSTOMER_PICKUP",
    "CUSTOMER_DELIVERY",
    "DIRECT_CUSTOMER_MOVE",
    "HUB_DROP",
    "ACCOUNTING_ONLY",
]


def _date_series(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, errors="coerce").dt.date


def filter_demand_by_basis(demand: pd.DataFrame, start: date, end: date, basis: str) -> pd.DataFrame:
    if basis not in VALID_BASIS:
        raise ValueError(f"Unknown date basis: {basis}")
    if demand.empty:
        return demand.copy()
    if basis in {DEMAND_TOUCH, MANIFEST_COMPAT, PLANNING_WINDOW}:
        collect = _date_series(demand.get("collect_date", pd.Series(dtype=str)))
        deliver = _date_series(demand.get("deliver_date", pd.Series(dtype=str)))
        mask = ((collect >= start) & (collect <= end)) | ((deliver >= start) & (deliver <= end))
        return demand[mask.fillna(False)].copy()
    # SERVICE_DATE is leg-level; keep all demand that has at least one filtered leg elsewhere.
    return demand.copy()


def filter_legs_by_basis(legs: pd.DataFrame, start: date, end: date, basis: str) -> pd.DataFrame:
    if basis not in VALID_BASIS:
        raise ValueError(f"Unknown date basis: {basis}")
    if legs.empty:
        return legs.copy()
    service = _date_series(legs.get("service_date", pd.Series(dtype=str)))
    if basis == SERVICE_DATE:
        mask = (service >= start) & (service <= end)
        return legs[mask.fillna(False)].copy()
    if basis == DEMAND_TOUCH:
        return legs.copy()
    customer_or_accounting = legs["leg_kind"].isin(CUSTOMER_OR_ACCOUNTING_LEGS)
    in_window = (customer_or_accounting & (service >= start) & (service <= end)).fillna(False)
    if basis == PLANNING_WINDOW:
        # Retain every customer/accounting leg of any order that has at least one
        # customer/accounting leg in-window, so a multi-day order admitted on one
        # endpoint keeps its partner leg — a future delivery tied to an in-window
        # pickup, or the pickup behind an in-window delivery — instead of being
        # clipped at the window edge into an orphan. Trunk legs stay excluded
        # (reported separately); orders with no in-window leg are not pulled in.
        in_window_orders = set(legs.loc[in_window, "order_id"].astype(str))
        keep = customer_or_accounting & legs["order_id"].astype(str).isin(in_window_orders)
        return legs[keep].copy()
    # MANIFEST_COMPAT approximates the current plan manifest denominator: customer-facing
    # rows whose service date sits in-window, plus out-of-scope/accounting rows already
    # emitted for in-window orders. Trunk legs are excluded because the manifest reports
    # customer/order service rows and trunk routes separately on the map.
    return legs[in_window].copy()


def align_demand_to_legs(demand: pd.DataFrame, legs: pd.DataFrame) -> pd.DataFrame:
    if demand.empty or legs.empty or "order_id" not in legs.columns:
        return demand.iloc[0:0].copy() if not demand.empty else demand.copy()
    ids = set(legs["order_id"].astype(str))
    return demand[demand["order_id"].astype(str).isin(ids)].copy()
