"""Q8 decision: persist the verified physical leg onto the orders dataset.

The raw orders do not reliably say which leg our fleet did. `verify_legs.py`
resolves it from telematics into `freight_planner/data/verified_legs.csv`
({order_id -> leg, confidence, method}). This module joins that truth onto the
orders as explicit columns, so the physical movement of each order is known up
front instead of inferred at runtime.

The enriched columns are leg *verification* only. They classify which leg is
ours (responsibility), and must never be used to choose future vehicle/route
assignments — the historical `matched_vehicle` field is deliberately not carried
here to remove that temptation.
"""
from __future__ import annotations

import pandas as pd

ENRICHED_LEG_COLUMNS = ["verified_leg", "verified_confidence", "verified_method"]
LOAD_AUDIT_COLUMNS = ["goods_weight_reported", "goods_weight_correction_reason"]

# Explicit corrections accepted after review of the source order records.  The
# stable order UUID is the key: WT names are included only for human-readable
# provenance and must not be used as identifiers.
ORDER_WEIGHT_CORRECTIONS = {
    "305eb7c9-8829-48fc-82e4-65390774025c": {
        "name": "WT259833",
        "weight_kg": 5_991.360,
        "reason": "reported_in_grams_converted_to_kg",
    },
    "86ea1cd4-2385-4945-853d-e52fb4bc55f1": {
        "name": "WT271534",
        "weight_kg": 22_432.0,
        "reason": "corrected_from_order_documentation",
    },
    "9b6d1653-82e1-4811-971d-18c1b6c59c6a": {
        "name": "WT271550",
        "weight_kg": 22_432.0,
        "reason": "corrected_from_order_documentation",
    },
}

# Source column in verified_legs.csv -> enriched column name.
_SOURCE_TO_ENRICHED = {
    "leg": "verified_leg",
    "confidence": "verified_confidence",
    "method": "verified_method",
}


def apply_order_load_corrections(orders: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with audited effective weights and raw-value provenance.

    The raw order feed is never rewritten.  ``goods_weight_reported`` preserves
    its supplied value, while ``goods_weight`` is the effective value consumed
    by routing and capacity checks.  Zero/missing payload rows are deliberately
    retained unchanged: an order record still represents a physical movement
    even when its load fields are incomplete.
    """
    out = orders.copy()
    if "goods_weight" not in out.columns:
        out["goods_weight"] = 0.0
    if "goods_weight_reported" not in out.columns:
        out["goods_weight_reported"] = out["goods_weight"]
    out["goods_weight_correction_reason"] = ""

    if "order_id" not in out.columns:
        return out
    ids = out["order_id"].astype(str)
    for order_id, correction in ORDER_WEIGHT_CORRECTIONS.items():
        matched = ids.eq(order_id)
        if not matched.any():
            continue
        out.loc[matched, "goods_weight"] = float(correction["weight_kg"])
        out.loc[matched, "goods_weight_correction_reason"] = str(correction["reason"])
    return out


def build_enriched_orders(orders: pd.DataFrame, verified: pd.DataFrame) -> pd.DataFrame:
    """Return ``orders`` with the verified-leg columns joined on ``order_id``.

    Row count and order are preserved. Orders with no verified leg get blank
    strings (not NaN) so downstream string handling stays simple. Duplicate
    ``order_id`` rows in ``verified`` keep the first occurrence.
    """
    out = apply_order_load_corrections(orders)
    ids = out["order_id"].astype(str)

    if verified is None or verified.empty:
        for col in ENRICHED_LEG_COLUMNS:
            out[col] = ""
        return out

    dedup = verified.drop_duplicates(subset=["order_id"], keep="first")
    keyed = dedup.assign(_oid=dedup["order_id"].astype(str)).set_index("_oid")
    for source, enriched in _SOURCE_TO_ENRICHED.items():
        mapped = ids.map(keyed[source]) if source in keyed.columns else pd.Series([None] * len(out), index=out.index)
        out[enriched] = mapped.fillna("").astype(str)
    return out


def verified_leg_lookup(orders: pd.DataFrame) -> dict[str, str] | None:
    """Build an {order_id -> leg} lookup from an enriched orders frame.

    Returns None when the ``verified_leg`` column is absent, so callers can fall
    back to the runtime CSV lookup. Blank legs are omitted.
    """
    if "verified_leg" not in orders.columns:
        return None
    lookup: dict[str, str] = {}
    for order_id, leg in zip(orders["order_id"].astype(str), orders["verified_leg"]):
        text = "" if leg is None else str(leg).strip()
        if text:
            lookup[order_id] = text
    return lookup
