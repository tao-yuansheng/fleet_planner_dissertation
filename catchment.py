"""B15: per-vehicle service-area radii learned from history.

Each vehicle's catchment = P95 of the haversine distances from its home-depot
anchor to the customer postcodes of orders it actually served (qargo
resource_* columns). Thin histories fall back to the fleet-wide per-type P95;
everything is floored. The radius feeds a SOFT ranking penalty
(vehicle_cost.out_of_area_penalty_km) — no hard gate, coverage cannot drop.

Deployment caveat: calibrating from the planning window's own month is a
fleet-behavior prior, not per-order hindsight; a live deployment would feed
trailing months instead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from freight_planner.shared.config import ALL_RIGIDS, ALL_TRACTORS, DEPOT_ANCHORS, VEHICLE_DEPOT_MAP
from freight_planner.shared.scope import classify_order
from freight_planner import geocode
from freight_planner.config import (
    CATCHMENT_MIN_SAMPLES,
    CATCHMENT_PERCENTILE,
    CATCHMENT_RADIUS_FLOOR_KM,
)
from freight_planner.route_costs import haversine_km

_RESOURCE_COLS = ("resource_rigid", "resource_tractor", "resource_van")


def _home_anchor(reg: str) -> tuple[float, float] | None:
    depot = VEHICLE_DEPOT_MAP.get(reg)
    return DEPOT_ANCHORS.get(depot) if depot else None


def _fleet_type(reg: str) -> str:
    if reg in ALL_TRACTORS:
        return "tractor"
    if reg in ALL_RIGIDS:
        return "rigid"
    return "van"


def _regs(value) -> list[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return []
    return [p.strip().replace(" ", "").upper() for p in text.split(",") if p.strip()]


def _service_pcs(row, flow) -> list[str]:
    o = str(row.get("origin_postal_code") or "").strip().upper()
    d = str(row.get("destination_postal_code") or "").strip().upper()
    if flow in ("PL_EXPORT", "LOCAL_COLLECT"):
        return [o] if o else []
    if flow in ("PL_IMPORT", "LOCAL_DELIVER"):
        return [d] if d else []
    return [p for p in (o, d) if p]


def job_distance_km(home_lat: float, home_lon: float, job) -> float:
    """Straight-line km from a vehicle's home to a job's farthest endpoint.

    Two-point moves (DIRECT/HUB_DROP with origin coords) use the MAX of the
    collection and delivery distances — a near delivery with a far collection
    is still out-of-area work. Straight-line on purpose: the radii were
    calibrated on the same metric."""
    d = haversine_km(home_lat, home_lon, float(job.lat), float(job.lon))
    o_lat = getattr(job, "origin_lat", None)
    o_lon = getattr(job, "origin_lon", None)
    if o_lat is not None and o_lon is not None:
        d = max(d, haversine_km(home_lat, home_lon, float(o_lat), float(o_lon)))
    return d


def build_vehicle_catchment(
    qargo_df: pd.DataFrame,
    postcode_cache: dict,
    type_of: dict[str, str] | None = None,
    fleet_types: dict[str, str] | None = None,
) -> dict[str, float]:
    """vehicle reg -> catchment radius km (P95 own history, type fallback, floored).

    Every reg in ``fleet_types`` is guaranteed a radius (own P95 -> type
    fallback -> floor), so no fleet vehicle escapes the out-of-area penalty by
    having no qargo history — zero-history vehicles are exactly the ones the
    optimizer would otherwise dump long work on. Non-fleet regs with no depot
    mapping are still skipped entirely (no radius -> no penalty).
    ``type_of`` overrides the fleet-set type lookup (testing seam)."""
    samples: dict[str, list[float]] = {}
    # Perf: iterrows() constructs a Series+Index per row, the slowest pandas
    # row-iteration path; classify_order/_service_pcs only ever call
    # row.get(key), which a plain dict from to_dict("records") satisfies
    # identically — this is a pure iteration-speed change, not a behavior one.
    for row in qargo_df.to_dict("records"):
        if str(row.get("status") or "").upper() == "CANCELLED":
            continue
        flow = classify_order(row)
        pcs = _service_pcs(row, flow)
        if not pcs:
            continue
        for col in _RESOURCE_COLS:
            for reg in _regs(row.get(col)):
                anchor = _home_anchor(reg)
                if anchor is None:
                    continue
                for pc in pcs:
                    ll = geocode.coords(pc, postcode_cache)
                    if not ll:
                        continue
                    samples.setdefault(reg, []).append(
                        haversine_km(anchor[0], anchor[1], ll[0], ll[1]))

    kind = type_of.get if type_of else lambda reg, default=None: _fleet_type(reg)
    by_type: dict[str, list[float]] = {}
    for reg, arr in samples.items():
        by_type.setdefault(kind(reg) or "van", []).extend(arr)
    type_radius = {
        t: float(np.percentile(np.array(arr), CATCHMENT_PERCENTILE))
        for t, arr in by_type.items() if arr
    }

    radii: dict[str, float] = {}
    for reg, arr in samples.items():
        if len(arr) >= CATCHMENT_MIN_SAMPLES:
            r = float(np.percentile(np.array(arr), CATCHMENT_PERCENTILE))
        else:
            r = type_radius.get(kind(reg) or "van", 0.0)
        radii[reg] = max(CATCHMENT_RADIUS_FLOOR_KM, r)

    # Backfill fleet vehicles with ZERO qargo history: type fallback, floored.
    # type_radius keys come from kind()/_fleet_type() -> lowercase, normalize to match.
    for reg, vtype in (fleet_types or {}).items():
        if reg not in radii:
            radii[reg] = max(CATCHMENT_RADIUS_FLOOR_KM,
                             type_radius.get(str(vtype or "").strip().lower(), 0.0))
    return radii
