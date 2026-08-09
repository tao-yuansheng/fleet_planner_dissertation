"""Per-vehicle *actual* activity for one operating day, from telematics.

Two distances are available per vehicle:
  * **odometer** — true driven km from the CANbus odometer (a UK fleet logs it in
    *miles*, so we convert). This is the ground truth and the default for validation.
  * **haversine** — sum of straight-line hops between consecutive GPS pings, used as a
    fallback when the odometer is missing. Dense in-motion pings (sub-km apart) already
    trace the road (measured odo/haversine ~1.05), so they stay raw; only long signal-gap
    JUMPS (> _GAP_KM, where the tracker lost signal mid-drive and the chord cuts under the
    winding road) are grossed up by the road-circuity factor _ROAD_CIRCUITY (2026-07-25).

Also exposes the fleet's actually-visited postcodes (where it STOPPED), reduced to
outward codes for robust matching against planned stops. Identity: telematics
``AssetName`` == planner ``vehicle_id`` (direct match).
"""
from __future__ import annotations

from datetime import date
from typing import Callable

import pandas as pd

from freight_planner.route_costs import haversine_km

_MILES_TO_KM = 1.609344


def _load_day(d: date) -> pd.DataFrame:
    # Lazy import so telematics deps/files load only on the real path, never when a
    # test injects its own ``loader``.
    from freight_planner.shared.fleet_replay_data import load_day
    return load_day(d)


# A consecutive-ping segment longer than this is a signal-gap straight-line jump: dense
# in-motion sampling is sub-km apart and already traces the road (measured odo/haversine
# ~1.05 on odometer vehicles, all densely pinged), but a gap where the tracker lost signal
# mid-drive draws a chord UNDER the true winding road. Only those gap jumps are grossed up
# by the road-circuity factor; dense hops stay raw so they are not double-counted.
_GAP_KM = 2.0
_ROAD_CIRCUITY = 1.3


def _haversine_km_one(g: pd.DataFrame) -> float:
    """Sum of haversine between one vehicle's consecutive (time-sorted) pings, with the
    road-circuity factor applied ONLY to long signal-gap jumps (> ``_GAP_KM``)."""
    lats = g["Latitude"].to_numpy()
    lons = g["Longitude"].to_numpy()
    total = 0.0
    for i in range(1, len(lats)):
        d = haversine_km(float(lats[i - 1]), float(lons[i - 1]), float(lats[i]), float(lons[i]))
        total += d * _ROAD_CIRCUITY if d > _GAP_KM else d
    return float(total)


def _odometer_km(g: pd.DataFrame) -> float | None:
    """True driven km from the CANbus odometer (miles) over the day, or None if the
    column is missing/unusable. ``g`` must be time-sorted."""
    if "CANbusData_Odometer" not in g.columns:
        return None
    o = pd.to_numeric(g["CANbusData_Odometer"], errors="coerce").dropna()
    o = o[o > 0]
    if len(o) < 2:
        return None
    delta_mi = float(o.iloc[-1] - o.iloc[0])
    if not (0.0 <= delta_mi < 2000.0):  # guard resets / bad reads
        return None
    return delta_mi * _MILES_TO_KM


def actual_km_by_vehicle(
    day: date,
    *,
    prefer_odometer: bool = False,
    loader: Callable[[date], pd.DataFrame] = _load_day,
) -> dict[str, float]:
    """Distance each vehicle actually drove on ``day``.

    ``prefer_odometer`` uses the true odometer distance (miles->km) per vehicle when
    available, falling back to the haversine-of-pings estimate otherwise.
    """
    df = loader(day)
    if df is None or df.empty:
        return {}
    df = df.dropna(subset=["Latitude", "Longitude"])
    out: dict[str, float] = {}
    for name, g in df.groupby("AssetName"):
        g = g.sort_values("LocalTime")
        km = _odometer_km(g) if prefer_odometer else None
        if km is None:
            km = _haversine_km_one(g)
        out[str(name)] = float(km)
    return out


_MIN_DUTY_MOVING_MPH = 2.0


def actual_duty_by_vehicle(
    day: date,
    *,
    loader: Callable[[date], pd.DataFrame] = _load_day,
) -> dict[str, float]:
    """On-duty hours per vehicle on ``day`` = span from first to last MOVING ping
    (GPSSpeed > 2 mph). This is the telematics equivalent of the plan's depot
    depart->return span, for the duty-hours validation axis."""
    df = loader(day)
    if df is None or df.empty or "GPSSpeed" not in df.columns:
        return {}
    out: dict[str, float] = {}
    for name, g in df.groupby("AssetName"):
        g = g.sort_values("LocalTime")
        spd = pd.to_numeric(g["GPSSpeed"], errors="coerce")
        moving = g[spd > _MIN_DUTY_MOVING_MPH]
        if len(moving) < 2:
            continue
        t = pd.to_datetime(moving["LocalTime"], errors="coerce").dropna()
        if len(t) < 2:
            continue
        hours = (t.iloc[-1] - t.iloc[0]) / pd.Timedelta(hours=1)
        if hours > 0:
            out[str(name)] = float(hours)
    return out


def actual_vehicle_days(
    day: date,
    *,
    min_km: float = 1.0,
    loader: Callable[[date], pd.DataFrame] = _load_day,
) -> set[str]:
    """Vehicles that actually moved (> ``min_km``) on ``day``."""
    return {v for v, k in actual_km_by_vehicle(day, loader=loader).items() if k >= min_km}


def normalize_pc(pc: str) -> str:
    """Reduce a postcode to its OUTWARD code (the part before the space) — the robust
    granularity common to planned (full) and telematics (often shorter) postcodes.
    'CB22 4PS' / 'CB22 4' / 'CB22' -> 'CB22'."""
    toks = str(pc or "").strip().upper().split()
    if not toks:
        return ""
    head = toks[0]
    # full postcode written with no space (e.g. 'CB224PS'): strip the 3-char inward code
    if len(toks) == 1 and len(head) >= 5 and head[-3].isdigit() and head[-2:].isalpha():
        return head[:-3]
    return head


def visited_postcodes(
    day: date,
    *,
    speed_kmh: float = 5.0,
    loader: Callable[[date], pd.DataFrame] = _load_day,
) -> set[str]:
    """Set of OUTWARD codes the fleet actually STOPPED in (GPSSpeed < ``speed_kmh``)
    on ``day``, from each ping's reverse-geocoded ``Location_Postcode``."""
    df = loader(day)
    if df is None or df.empty or "Location_Postcode" not in df.columns:
        return set()
    if "GPSSpeed" in df.columns:
        df = df[pd.to_numeric(df["GPSSpeed"], errors="coerce") < speed_kmh]
    out = {normalize_pc(p) for p in df["Location_Postcode"].dropna()}
    out.discard("")
    return out
