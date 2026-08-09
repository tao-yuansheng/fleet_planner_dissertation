"""Telematics-verified leg overlay for flow classification.

The Palletline/Hazchem import/export tags are information-flow signals, not
freight-direction. `freight_planner/data/verified_legs.csv` (built from telematics
ground truth) records, per order, the leg our fleet physically did:
COLLECTION / DELIVERY / FULL_FLEET. This module overlays that truth onto the
stale tag-based flow so the dispatcher and manifest service the correct leg.

Direction mapping (the network/hub still comes from import_type/subcontractor):
    DELIVERY   -> PL_IMPORT  (inbound; we deliver)
    COLLECTION -> PL_EXPORT  (we collect; outbound)
    FULL_FLEET -> FULL_FLEET (unchanged; also corrects 'hidden' full-fleet)

`classify_order` itself is deliberately left untouched (raw tag logic) so the
verified-legs CSV can be regenerated without circularity; this overlay is applied
at the two points where the pipeline turns a row into a flow.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

from freight_planner.shared.paths import LOGISTICS_ROOT

_CSV_PATH = LOGISTICS_ROOT / "freight_planner" / "data" / "verified_legs.csv"

# leg -> corrected flow direction for NETWORK orders (+ hidden full-fleet)
_LEG_TO_FLOW = {
    "DELIVERY": "PL_IMPORT",
    "COLLECTION": "PL_EXPORT",
    "FULL_FLEET": "FULL_FLEET",
}
# leg -> flow for PARTIAL-FLEET orders (raw flow None, non-network): single LOCAL
# leg with no hub trunk. Brings the previously-unclassifiable orders into scope.
_PARTIAL_LEG_TO_FLOW = {
    "DELIVERY": "LOCAL_DELIVER",
    "COLLECTION": "LOCAL_COLLECT",
    "FULL_FLEET": "FULL_FLEET",
}

_cache: Optional[dict] = None


def load_verified_legs(path=None) -> dict:
    """Return cached {order_id: leg}. Empty dict if the CSV is absent (the
    pipeline then falls back to the raw tag-based flow with no error)."""
    global _cache
    if path is None and _cache is not None:
        return _cache
    p = Path(path) if path is not None else _CSV_PATH
    out: dict = {}
    if p.exists():
        with open(p, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                oid, leg = r.get("order_id"), r.get("leg")
                if oid and leg:
                    out[oid] = leg
    if path is None:
        _cache = out
    return out


def verified_leg(order_id, lookup: Optional[dict] = None) -> Optional[str]:
    lk = lookup if lookup is not None else load_verified_legs()
    return lk.get(str(order_id or "")) if order_id is not None else None


def corrected_flow(row, raw_flow, lookup: Optional[dict] = None):
    """Override the raw tag-based flow with the telematics-verified leg direction.

    Network orders (raw PL_IMPORT/PL_EXPORT) are flipped to match the verified
    leg; a verified FULL_FLEET promotes a mis-tagged network order to full-fleet.
    Orders with no verified leg, and (in Stage 1) partial-fleet rows whose raw
    flow is None, are returned unchanged.
    """
    leg = verified_leg(row.get("order_id"), lookup)
    if leg is None or leg == "UNVERIFIED":
        # no usable verified leg (incl. structural-single with unknown direction) -> keep raw
        return raw_flow
    if leg == "FULL_FLEET":
        return "FULL_FLEET"
    # Single physical leg (COLLECTION / DELIVERY). Choose the network form
    # (PL_*) or the non-network local form (LOCAL_*) by the order's network type:
    if raw_flow in ("PL_IMPORT", "PL_EXPORT"):
        return _LEG_TO_FLOW[leg]                       # network direction flip
    if raw_flow == "FULL_FLEET":
        # Full-fleet demoted to one leg: Palletline/Hazchem direct -> network PL_*,
        # structural (MANUAL/CLARUS) -> non-network LOCAL_*.
        it = str(row.get("order_import_integration_type") or "").upper()
        if it in ("PALLETLINE", "HAZCHEM"):
            return _LEG_TO_FLOW[leg]
        return _PARTIAL_LEG_TO_FLOW[leg]
    if raw_flow is None:
        # Partial-fleet (non-network, previously unclassifiable) -> single LOCAL leg.
        return _PARTIAL_LEG_TO_FLOW[leg]
    return raw_flow
