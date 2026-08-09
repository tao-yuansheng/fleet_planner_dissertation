from __future__ import annotations

from dataclasses import asdict, dataclass
from math import asin, cos, radians, sin, sqrt
from typing import Any

import numpy as np
import pandas as pd

from freight_planner.shared.config import AVG_SPEED_KMH, ROAD_DISTANCE_FACTOR
from freight_planner import geocode


@dataclass(frozen=True)
class VehicleJobCompatibilityRecord:
    vehicle_id: str
    job_id: str
    leg_id: str
    order_id: str
    vehicle_type: str
    vehicle_home_depot: str
    job_source_depot: str
    same_depot: bool
    cross_depot: bool
    capacity_ok: bool
    time_reachable: bool
    service_lat: float | None
    service_lon: float | None
    current_to_service_km: float | None
    estimated_drive_minutes: float | None
    compatibility_status: str

    def to_dict(self) -> dict:
        return asdict(self)


def _haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    dlat = radians(b_lat - a_lat)
    dlon = radians(b_lon - a_lon)
    x = sin(dlat / 2) ** 2 + cos(radians(a_lat)) * cos(radians(b_lat)) * sin(dlon / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(x))


def _coords(pc: str, postcode_cache: dict) -> tuple[float, float] | None:
    return geocode.coords(str(pc or ""), postcode_cache)


def _capacity_ok(vehicle: Any, job: Any) -> bool:
    return (
        float(vehicle.capacity_kg or 0.0) >= float(job.weight_kg or 0.0)
        and float(vehicle.capacity_pallets or 0.0) >= float(job.pallets or 0.0)
    )


def _screen_speed_kmh() -> float:
    """Reach-screen speed. Constant AVG_SPEED_KMH normally; a generous bound under
    USE_OSRM_DURATIONS so the screen never rejects a job the per-segment OSRM
    evaluator would accept (the evaluator is the real time authority; spec Part B,
    screen safety)."""
    from freight_planner import config
    if config.USE_OSRM_DURATIONS:
        return float(config.OSRM_SCREEN_SPEED_KMH)
    return float(AVG_SPEED_KMH)


def _time_reachable(vehicle: Any, job: Any, drive_minutes: float | None) -> bool:
    if drive_minutes is None:
        return False
    available = pd.to_datetime(getattr(vehicle, "available_from", ""), errors="coerce")
    latest = pd.to_datetime(getattr(job, "latest_finish", ""), errors="coerce")
    if pd.isna(available) or pd.isna(latest):
        return True
    return available + pd.Timedelta(minutes=drive_minutes) <= latest


def _job_coords(jobs: pd.DataFrame, postcode_cache: dict) -> pd.DataFrame:
    out = jobs.copy()
    coords = [_coords(pc, postcode_cache) for pc in out.get("service_pc", pd.Series(dtype=str))]
    out["service_lat"] = [c[0] if c else np.nan for c in coords]
    out["service_lon"] = [c[1] if c else np.nan for c in coords]
    return out


def _vectorized_haversine_km(df: pd.DataFrame) -> pd.Series:
    a_lat = np.radians(df["current_lat"].astype(float))
    a_lon = np.radians(df["current_lon"].astype(float))
    b_lat = np.radians(df["service_lat"].astype(float))
    b_lon = np.radians(df["service_lon"].astype(float))
    dlat = b_lat - a_lat
    dlon = b_lon - a_lon
    x = np.sin(dlat / 2) ** 2 + np.cos(a_lat) * np.cos(b_lat) * np.sin(dlon / 2) ** 2
    return pd.Series(2 * 6371.0 * np.arcsin(np.sqrt(x)), index=df.index)


def vehicle_job_compatibility_frame(
    jobs: pd.DataFrame,
    vehicles: pd.DataFrame,
    postcode_cache: dict,
) -> pd.DataFrame:
    columns = [
        "vehicle_id", "job_id", "leg_id", "order_id", "vehicle_type",
        "vehicle_home_depot", "job_source_depot", "same_depot", "cross_depot",
        "capacity_ok", "time_reachable", "service_lat", "service_lon",
        "current_to_service_km", "estimated_drive_minutes", "compatibility_status",
    ]
    if jobs.empty or vehicles.empty:
        return pd.DataFrame(columns=columns)

    runnable = jobs[jobs["hard_blocker"].fillna("").eq("")].copy()
    if runnable.empty:
        return pd.DataFrame(columns=columns)
    runnable = _job_coords(runnable, postcode_cache)

    job_cols = [
        "job_id", "leg_id", "order_id", "source_depot", "service_lat", "service_lon",
        "weight_kg", "pallets", "latest_finish",
    ]
    vehicle_cols = [
        "vehicle_id", "vehicle_type", "home_depot", "current_lat", "current_lon",
        "capacity_kg", "capacity_pallets", "available_from",
    ]
    left = runnable[job_cols].assign(_join_key=1)
    right = vehicles[vehicle_cols].assign(_join_key=1)
    merged = left.merge(right, on="_join_key", how="inner").drop(columns=["_join_key"])

    has_coords = merged["service_lat"].notna() & merged["service_lon"].notna()
    merged["current_to_service_km"] = np.nan
    if has_coords.any():
        merged.loc[has_coords, "current_to_service_km"] = (
            _vectorized_haversine_km(merged.loc[has_coords]) * ROAD_DISTANCE_FACTOR
        )
    merged["estimated_drive_minutes"] = (merged["current_to_service_km"] / _screen_speed_kmh()) * 60.0
    merged["capacity_ok"] = (
        (merged["capacity_kg"].astype(float) >= merged["weight_kg"].astype(float))
        & (merged["capacity_pallets"].astype(float) >= merged["pallets"].astype(float))
    )

    available = pd.to_datetime(merged["available_from"], errors="coerce")
    latest = pd.to_datetime(merged["latest_finish"], errors="coerce")
    arrival = available + pd.to_timedelta(merged["estimated_drive_minutes"], unit="m")
    merged["time_reachable"] = has_coords & (available.isna() | latest.isna() | (arrival <= latest))
    merged["same_depot"] = merged["home_depot"].astype(str).eq(merged["source_depot"].astype(str))
    merged["cross_depot"] = merged["source_depot"].fillna("").astype(str).ne("") & ~merged["same_depot"]

    status = np.where(~merged["capacity_ok"], "CAPACITY", "OK")
    status = np.where(~has_coords, "BAD_GEOCODE", status)
    status = np.where(has_coords & merged["capacity_ok"] & ~merged["time_reachable"], "TIME_REACH", status)
    merged["compatibility_status"] = status

    out = pd.DataFrame({
        "vehicle_id": merged["vehicle_id"].astype(str),
        "job_id": merged["job_id"].astype(str),
        "leg_id": merged["leg_id"].astype(str),
        "order_id": merged["order_id"].astype(str),
        "vehicle_type": merged["vehicle_type"].astype(str),
        "vehicle_home_depot": merged["home_depot"].astype(str),
        "job_source_depot": merged["source_depot"].astype(str),
        "same_depot": merged["same_depot"].astype(bool),
        "cross_depot": merged["cross_depot"].astype(bool),
        "capacity_ok": merged["capacity_ok"].astype(bool),
        "time_reachable": merged["time_reachable"].astype(bool),
        "service_lat": merged["service_lat"],
        "service_lon": merged["service_lon"],
        "current_to_service_km": merged["current_to_service_km"],
        "estimated_drive_minutes": merged["estimated_drive_minutes"],
        "compatibility_status": merged["compatibility_status"].astype(str),
    })
    return out[columns]


def build_vehicle_job_compatibility(
    jobs: pd.DataFrame,
    vehicles: pd.DataFrame,
    postcode_cache: dict,
) -> list[VehicleJobCompatibilityRecord]:
    frame = vehicle_job_compatibility_frame(jobs, vehicles, postcode_cache)
    return [VehicleJobCompatibilityRecord(**row) for row in frame.to_dict("records")]
