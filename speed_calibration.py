"""v1.1 speed calibration & per-type validation (design 2026-07-09).

Produces (1) the observed by-type / by-road-class speed table (the "validate
speed by type" deliverable) and (2) the per-vehicle-type OSRM duration factors
consumed by config.FREIGHT_DURATION_FACTOR.

Calibration basis is STRUCTURAL: per-journey and per-road-class speeds measured
from the GPS telematics — never a fit to historical daily km/time totals (that
overfits forward/backtest mode and fails on unseen days).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

MPH_TO_KMH = 1.609344
MIN_MOVING_MPH = 2.0

_BASE = Path(__file__).resolve().parents[1]  # .../BackEnd/logistics
SUPATRAK = _BASE / "data" / "Input" / "supatrak"
OUT_DIR = Path(__file__).resolve().parent / "data" / "calibration"


def classify_road(name) -> str:
    if not isinstance(name, str) or not name.strip():
        return "unknown"
    t = name.strip().upper()
    if re.match(r"^M\d", t) or "MOTORWAY" in t:
        return "motorway"
    if re.match(r"^A\d", t):
        return "A_road"
    if re.match(r"^B\d", t):
        return "B_road"
    return "minor_urban"


def resolve_vehicle_type(asset_type, metric, fuel_type) -> str:
    at = str(asset_type or "").lower()
    fuel = str(fuel_type or "").lower()
    m = str(metric or "").upper()
    if "electric" in fuel:
        return "EV"
    if "tractor" in at or "artic" in at:
        return "tractor"
    if "van" in at:
        return "van"
    if any(k in at for k in ("lorry", "rigid", "box", "curtain", "truck")):
        return "rigid"
    return "tractor" if m == "GCW" else "rigid"


def observed_speed_table(df: pd.DataFrame, type_map: dict) -> pd.DataFrame:
    """Mean/median moving speed (km/h) and ping count by (vehicle_type, road_class).

    Moving pings only (Ignition true, GPSSpeed > MIN_MOVING_MPH). GPSSpeed is mph.
    """
    d = df.copy()
    d["GPSSpeed"] = pd.to_numeric(d["GPSSpeed"], errors="coerce")
    ign = d["Ignition"].astype(str).str.lower().isin(["true", "1", "1.0"])
    d = d[ign & (d["GPSSpeed"] > MIN_MOVING_MPH)].copy()
    d["vehicle_type"] = d["AssetName"].astype(str).map(type_map).fillna("unknown")
    d["road_class"] = d["Location_Road"].map(classify_road)
    d["kmh"] = d["GPSSpeed"] * MPH_TO_KMH
    g = d.groupby(["vehicle_type", "road_class"])["kmh"]
    out = g.agg(mean_kmh="mean", median_kmh="median", ping_count="count").reset_index()
    return out


def _hav_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(a))


def build_hops(df: pd.DataFrame, min_km: float = 1.5, max_gap_min: float = 8.0,
               min_speed_mph: float = MIN_MOVING_MPH) -> pd.DataFrame:
    """Point-to-point MOVING hops for per-type speed calibration.

    A hop is a pair of consecutive pings (time-sorted) where BOTH endpoints are
    moving (GPSSpeed > min_speed_mph), the time gap is small (<= max_gap_min, so it
    is one continuous drive, not a hop spanning a stop) and the straight-line
    distance is >= min_km (so OSRM's point-to-point prediction is meaningful and GPS
    noise is small). observed_h is then pure driving time over a genuine single leg
    — the correct unit for a per-leg duration factor (service/idle time excluded).
    """
    rows = []
    for name, g in df.groupby("AssetName"):
        g = g.sort_values("LocalTime")
        t = pd.to_datetime(g["LocalTime"], errors="coerce").to_numpy()
        spd = pd.to_numeric(g["GPSSpeed"], errors="coerce").to_numpy()
        lat = pd.to_numeric(g["Latitude"], errors="coerce").to_numpy()
        lon = pd.to_numeric(g["Longitude"], errors="coerce").to_numpy()
        road = g["Location_Road"].to_numpy() if "Location_Road" in g.columns else None
        for i in range(len(g) - 1):
            if not (spd[i] > min_speed_mph and spd[i + 1] > min_speed_mph):
                continue
            dt_min = (t[i + 1] - t[i]) / pd.Timedelta(minutes=1)
            if not (0 < dt_min <= max_gap_min):
                continue
            dist = _hav_km(lat[i], lon[i], lat[i + 1], lon[i + 1])
            if not (dist >= min_km):
                continue
            rows.append({"AssetName": str(name), "o_lat": float(lat[i]), "o_lon": float(lon[i]),
                         "d_lat": float(lat[i + 1]), "d_lon": float(lon[i + 1]),
                         "observed_h": float(dt_min / 60.0),
                         "road_class": classify_road(road[i]) if road is not None else "unknown"})
    return pd.DataFrame(rows, columns=["AssetName", "o_lat", "o_lon", "d_lat", "d_lon",
                                       "observed_h", "road_class"])


def per_type_factors(journeys: pd.DataFrame, type_map: dict, osrm_freeflow_h) -> dict:
    """factor[type] = sum(observed_h) / sum(osrm_freeflow_h) over that type's journeys.

    ``osrm_freeflow_h(o_lat, o_lon, d_lat, d_lon) -> hours`` is the OSRM CAR free-flow
    time (i.e. the router duration with TRUCK_DURATION_FACTOR divided back out).
    Journeys with no route / non-positive prediction are skipped.
    """
    obs: dict[str, float] = {}
    pred: dict[str, float] = {}
    for r in journeys.itertuples(index=False):
        vt = type_map.get(str(r.AssetName), "unknown")
        try:
            ph = float(osrm_freeflow_h(r.o_lat, r.o_lon, r.d_lat, r.d_lon))
        except Exception:
            continue
        if ph <= 0:
            continue
        obs[vt] = obs.get(vt, 0.0) + float(r.observed_h)
        pred[vt] = pred.get(vt, 0.0) + ph
    return {vt: round(obs[vt] / pred[vt], 4) for vt in obs if pred.get(vt, 0.0) > 0}


def per_type_class_factors(hops: pd.DataFrame, type_map: dict, osrm_freeflow_h) -> dict:
    """factor[(vehicle_type, road_class)] = sum(observed_h) / sum(OSRM car free-flow h),
    grouped by vehicle type x the hop's road class (~1.0 means OSRM already matches the truck)."""
    obs: dict = {}
    pred: dict = {}
    for r in hops.itertuples(index=False):
        key = (type_map.get(str(r.AssetName), "unknown"), str(getattr(r, "road_class", "unknown")))
        try:
            ph = float(osrm_freeflow_h(r.o_lat, r.o_lon, r.d_lat, r.d_lon))
        except Exception:
            continue
        if ph <= 0:
            continue
        obs[key] = obs.get(key, 0.0) + float(r.observed_h)
        pred[key] = pred.get(key, 0.0) + ph
    return {k: round(obs[k] / pred[k], 3) for k in obs if pred.get(k, 0.0) > 0}


def load_type_map(vehicle_list: pd.DataFrame) -> dict:
    out = {}
    for r in vehicle_list.itertuples(index=False):
        out[str(getattr(r, "AssetName", ""))] = resolve_vehicle_type(
            getattr(r, "AssetType", ""), getattr(r, "metric", ""), getattr(r, "fuel_type", ""))
    return out


def _osrm_freeflow_h_factory():
    """Real OSRM car free-flow duration: router truck-hours / TRUCK_DURATION_FACTOR."""
    from freight_planner.route_costs import get_router, install_osrm_router
    from freight_planner.shared.routing import TRUCK_DURATION_FACTOR
    if get_router() is None:
        install_osrm_router()
    router = get_router()

    def _f(o_lat, o_lon, d_lat, d_lon):
        h = router.duration_h(o_lat, o_lon, d_lat, d_lon)  # truck hours (car x 1.24)
        return h / TRUCK_DURATION_FACTOR
    return _f


def main(months=("20260101_to_20260131", "20260201_to_20260228")) -> None:
    vl = pd.read_csv(SUPATRAK / "supatrak_vehicle_list_enriched.csv")
    type_map = load_type_map(vl)
    frames = [pd.read_csv(SUPATRAK / f"supatrak_telematics_cleaned_{m}.csv",
                          usecols=["AssetName", "Ignition", "GPSSpeed", "Location_Road",
                                   "LocalTime", "Latitude", "Longitude"]) for m in months]
    df = pd.concat(frames, ignore_index=True)

    table = observed_speed_table(df, type_map)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUT_DIR / "speed_by_type_road.csv", index=False)

    hops = build_hops(df)
    factors = per_type_factors(hops, type_map, _osrm_freeflow_h_factory())
    payload = {"factors": factors, "n_hops": int(len(hops)),
               "months": list(months),
               "basis": "point-to-point moving-hop observed/OSRM-freeflow ratio, per type"}
    (OUT_DIR / "speed_factors.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("factors:", factors)
    print("observed table:\n", table.to_string(index=False))


if __name__ == "__main__":
    main()
