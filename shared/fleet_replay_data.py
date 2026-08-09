"""Data loaders & derivations for the fleet replay Streamlit app.

Pure-Python module with no Streamlit imports — importable from notebooks and
unit-testable in isolation. The companion file `fleet_replay.py` wraps these
in Streamlit caching widgets and builds the folium map.

See: BackEnd/logistics/docs/superpowers/specs/2026-06-01-fleet-replay-design.md
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import radians, sin, cos, sqrt, asin
from pathlib import Path
from typing import Iterable, Optional
import json

import numpy as np
import pandas as pd
import requests
from shapely.geometry import LineString

# Repo-relative defaults (overridable in tests via monkeypatch).
from freight_planner.shared.paths import LOGISTICS_ROOT as _LOGISTICS  # BackEnd/logistics/ (2026-07-13 separation)
TELEMATICS_DIR: Path = _LOGISTICS / "data" / "Input" / "supatrak"
ORDERS_DIR: Path = _LOGISTICS / "data" / "Input" / "orders"
DEPOTS_PATH: Path = _LOGISTICS / "depot_data" / "depot_addresses.json"
CACHE_DIR: Path = _LOGISTICS / ".cache"


def _month_csv_filename(d: date) -> str:
    """The telematics CSV filename covering the month of `d`."""
    first = d.replace(day=1)
    # End-of-month day count
    next_month = (first + timedelta(days=32)).replace(day=1)
    last = next_month - timedelta(days=1)
    return (
        f"supatrak_telematics_cleaned_"
        f"{first:%Y%m%d}_to_{last:%Y%m%d}.csv"
    )


def _load_month(d: date) -> pd.DataFrame:
    """Load (and cache as parquet) all pings for the month containing `d`."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    parquet_path = CACHE_DIR / f"telematics_{d:%Y%m}.parquet"
    if parquet_path.exists():
        return pd.read_parquet(parquet_path)
    csv_path = TELEMATICS_DIR / _month_csv_filename(d)
    df = pd.read_csv(csv_path, parse_dates=["LocalTime"])
    df.to_parquet(parquet_path, index=False)
    return df


def load_day(d: date) -> pd.DataFrame:
    """Return all telematics pings whose LocalTime falls on date `d` (local).

    Reads the monthly CSV once per session (parquet-cached), then filters.
    """
    df = _load_month(d)
    start = pd.Timestamp(datetime.combine(d, datetime.min.time()))
    end = start + pd.Timedelta(days=1)
    return df[(df["LocalTime"] >= start) & (df["LocalTime"] < end)].copy()


def load_vehicles() -> pd.DataFrame:
    """Return the enriched vehicle list (one row per asset)."""
    path = TELEMATICS_DIR / "supatrak_vehicle_list_enriched.csv"
    return pd.read_csv(path)


def vehicles_by_circuit() -> dict[str, list[str]]:
    """Return {circuit_name: [AssetName, ...]} sorted by AssetName within each circuit."""
    df = load_vehicles()
    out: dict[str, list[str]] = {}
    for circuit, group in df.groupby("CircuitName", dropna=False):
        out[circuit] = sorted(group["AssetName"].tolist())
    return out


@dataclass(frozen=True)
class Depot:
    """A labelled location on the map: ZEEFleet depot or Palletline hub."""
    name: str
    lat: float
    lon: float
    kind: str          # "zeefleet" or "palletline"
    radius_m: int      # geo-fence radius for arrival/departure detection


def load_depots() -> list[Depot]:
    """Read depot_addresses.json and classify each entry as zeefleet vs palletline."""
    raw = json.loads(DEPOTS_PATH.read_text(encoding="utf-8"))
    out: list[Depot] = []
    for entry in raw["addresses"]:
        name = entry["name"]
        is_pl = "palletine" in name.lower() or "palletline" in name.lower()
        kind = "palletline" if is_pl else "zeefleet"
        out.append(Depot(
            name=name,
            lat=float(entry["coordinates"][0]),
            lon=float(entry["coordinates"][1]),
            kind=kind,
            radius_m=300 if is_pl else 200,
        ))
    return out


def downsample_trace(df: pd.DataFrame, cap: int = 1500) -> pd.DataFrame:
    """Return a time-sorted subset of `df` with at most `cap` rows.

    Strategy:
      1. Always retain every row that is an Ignition state change (so stops
         and starts are never invisible) AND its immediate predecessor.
      2. For the remaining "non-transition" pings, apply Douglas-Peucker
         simplification on the (lon, lat) polyline with an adaptive tolerance:
         start small, grow until the union (transitions ∪ DP-kept) ≤ cap.
      3. If DP can't bring the count down even at tolerance = 0.1°, fall back
         to uniform sampling of the non-must-keep rows.
    Sorted by LocalTime in the output; index is reset.
    """
    df = df.sort_values("LocalTime").reset_index(drop=True)
    if len(df) <= cap:
        return df

    ign = df["Ignition"].astype(bool)
    flip = ign != ign.shift(1)
    must_keep_mask = flip | flip.shift(-1, fill_value=False)
    must_keep_mask.iloc[0] = True
    must_keep_mask.iloc[-1] = True
    must_keep_idx = df.index[must_keep_mask]

    line = LineString(list(zip(df["Longitude"], df["Latitude"])))
    tolerance = 0.0001
    while tolerance <= 0.1:
        simplified = line.simplify(tolerance, preserve_topology=False)
        kept_coords = set(
            (round(x, 7), round(y, 7)) for x, y in simplified.coords
        )
        geom_keep = df.apply(
            lambda r: (round(r["Longitude"], 7), round(r["Latitude"], 7)) in kept_coords,
            axis=1,
        )
        combined = must_keep_idx.union(df.index[geom_keep])
        if len(combined) <= cap:
            return df.loc[combined].sort_index().reset_index(drop=True)
        tolerance *= 1.5

    # Fallback: uniform sample of the non-must-keep rows
    other_idx = df.index.difference(must_keep_idx)
    remainder = cap - len(must_keep_idx)
    if remainder <= 0:
        return df.loc[must_keep_idx].reset_index(drop=True)
    step = max(1, len(other_idx) // remainder)
    uniform_idx = other_idx[::step]
    return df.loc[must_keep_idx.union(uniform_idx)].sort_index().reset_index(drop=True)


STALE_THRESHOLD = pd.Timedelta(minutes=30)


@dataclass
class VehicleTrace:
    """All retained pings for one vehicle on one date.

    `full` is the complete time-sorted DataFrame (used for current-position
    lookups so the cursor is always exact).
    `rendered` is the downsampled subset (≤ cap rows) used for the map polyline
    and the per-point clickable markers.
    `times` is the numpy datetime64 array of `full['LocalTime']` values,
    pre-extracted for fast np.searchsorted in `current_position`.
    """
    name: str
    full: pd.DataFrame
    rendered: pd.DataFrame
    times: np.ndarray


def prepare_vehicle_traces(
    day_pings: pd.DataFrame,
    vehicle_names: Iterable[str],
    cap: int = 1500,
) -> dict[str, VehicleTrace]:
    """Build a VehicleTrace per requested vehicle from one day's pings."""
    out: dict[str, VehicleTrace] = {}
    for name in vehicle_names:
        sub = day_pings[day_pings["AssetName"] == name].sort_values("LocalTime").reset_index(drop=True)
        if sub.empty:
            continue
        rendered = downsample_trace(sub, cap=cap)
        out[name] = VehicleTrace(
            name=name,
            full=sub,
            rendered=rendered,
            times=sub["LocalTime"].values,
        )
    return out


def current_position(
    trace: VehicleTrace,
    t: pd.Timestamp,
) -> tuple[pd.Series | None, bool]:
    """Find the latest ping with LocalTime <= t.

    Returns (row, is_stale). is_stale is True when (t - row.LocalTime) > 30 min.
    Returns (None, False) if t is before the vehicle's first ping of the day.
    """
    t64 = np.datetime64(pd.Timestamp(t))
    idx = int(np.searchsorted(trace.times, t64, side="right") - 1)
    if idx < 0:
        return None, False
    row = trace.full.iloc[idx]
    is_stale = (pd.Timestamp(t) - row["LocalTime"]) > STALE_THRESHOLD
    return row, is_stale


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two (lat,lon) pairs."""
    R = 6_371_000.0
    la1, la2 = radians(lat1), radians(lat2)
    dlat = la2 - la1
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(la1) * cos(la2) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))


@dataclass
class DepotVisit:
    """One contiguous in-fence visit by a vehicle to a depot."""
    vehicle: str
    arrived: Optional[pd.Timestamp]   # None if vehicle was inside at start of day
    departed: Optional[pd.Timestamp]  # None if vehicle was still inside at end of day
    dwell: Optional[pd.Timedelta]
    is_stop: bool                      # True if ignition was off at any inside ping


def depot_visits(
    pings: pd.DataFrame,
    depot: Depot,
) -> list[DepotVisit]:
    """Detect arrival/departure runs for one vehicle at one depot.

    `pings` must be one vehicle's pings, time-sorted (will be sorted defensively).
    """
    if pings.empty:
        return []
    df = pings.sort_values("LocalTime").reset_index(drop=True)
    lats = df["Latitude"].to_numpy()
    lons = df["Longitude"].to_numpy()
    inside = np.array([
        _haversine_m(depot.lat, depot.lon, la, lo) <= depot.radius_m
        for la, lo in zip(lats, lons)
    ])
    visits: list[DepotVisit] = []
    if not inside.any():
        return visits
    # Identify runs
    transitions = np.diff(inside.astype(int))
    starts = list(np.where(transitions == 1)[0] + 1)
    ends = list(np.where(transitions == -1)[0] + 1)  # exclusive end
    if inside[0]:
        starts = [0] + starts
    if inside[-1]:
        ends = ends + [len(df)]
    vehicle = df["AssetName"].iloc[0] if "AssetName" in df.columns else "?"
    for s, e in zip(starts, ends):
        run = df.iloc[s:e]
        arrived = run["LocalTime"].iloc[0] if s != 0 else None
        # departed = timestamp of first ping outside the fence (index e), or None if still inside at end
        departed = df["LocalTime"].iloc[e] if e != len(df) else None
        dwell = (
            departed - arrived
            if (arrived is not None and departed is not None)
            else None
        )
        is_stop = bool((~run["Ignition"].astype(bool)).any())
        visits.append(DepotVisit(
            vehicle=vehicle, arrived=arrived, departed=departed,
            dwell=dwell, is_stop=is_stop,
        ))
    return visits


_geocode_cache: dict[str, tuple[float, float] | None] = {}
POSTCODES_IO_URL = "https://api.postcodes.io/postcodes/{}"


def geocode_postcode(postcode: str) -> tuple[float, float] | None:
    """Resolve a UK postcode to (lat, lon) via postcodes.io. None on any failure.

    Caches results (including misses) for the lifetime of the process.
    Caller is responsible for surfacing a user-visible warning when None.
    """
    key = postcode.strip().upper()
    if key in _geocode_cache:
        return _geocode_cache[key]
    try:
        r = requests.get(POSTCODES_IO_URL.format(key), timeout=5)
    except requests.RequestException:
        _geocode_cache[key] = None
        return None
    if r.status_code != 200:
        _geocode_cache[key] = None
        return None
    body = r.json()
    if body.get("status") != 200 or not body.get("result"):
        _geocode_cache[key] = None
        return None
    result = body["result"]
    out = (float(result["latitude"]), float(result["longitude"]))
    _geocode_cache[key] = out
    return out


class OrderNotFound(Exception):
    """Raised when an order ID isn't found in the month's qargo file."""


@dataclass
class OrderPin:
    """Geocoded origin + destination for an order ID, for the map pin overlay."""
    order_id: str
    origin_postcode: str
    destination_postcode: str
    origin_latlon: tuple[float, float] | None
    destination_latlon: tuple[float, float] | None
    destination_time: pd.Timestamp | None


def _order_filename(d: date) -> str:
    """The qargo Excel filename covering the month of `d`."""
    first = d.replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    last = next_month - timedelta(days=1)
    return f"qargo_{first:%Y%m%d}_to_{last:%Y%m%d}.xlsx"


def build_animation_df(
    day_pings: pd.DataFrame,
    vehicle_names: Iterable[str],
    step_minutes: int = 5,
) -> pd.DataFrame:
    """Build a tidy DataFrame of vehicle positions at regular time steps.

    Returns one row per (time_step × vehicle) with columns:
      time_label, AssetName, Latitude, Longitude, ping_time,
      GPSSpeed, Ignition, AssetDriver, Location_Postcode, stale.

    Suitable for plotly.express.scatter_mapbox(animation_frame="time_label").
    Vehicles with no ping at or before a given step are omitted.
    """
    traces = prepare_vehicle_traces(day_pings, list(vehicle_names))
    _EMPTY_COLS = [
        "time_label", "AssetName", "Latitude", "Longitude",
        "ping_time", "GPSSpeed", "Ignition", "AssetDriver",
        "Location_Postcode", "stale",
    ]
    if not traces:
        return pd.DataFrame(columns=_EMPTY_COLS)

    day_start = pd.Timestamp(day_pings["LocalTime"].iloc[0]).normalize()
    steps = pd.date_range(
        start=day_start,
        periods=24 * 60 // step_minutes,
        freq=f"{step_minutes}min",
    )
    rows = []
    for t in steps:
        label = t.strftime("%H:%M")
        for name, trace in traces.items():
            row, stale = current_position(trace, t)
            if row is None:
                continue
            rows.append({
                "time_label": label,
                "AssetName": name,
                "Latitude": float(row["Latitude"]),
                "Longitude": float(row["Longitude"]),
                "ping_time": str(row["LocalTime"]),
                "GPSSpeed": float(row.get("GPSSpeed") or 0),
                "Ignition": bool(row.get("Ignition", False)),
                "AssetDriver": str(row.get("AssetDriver") or ""),
                "Location_Postcode": str(row.get("Location_Postcode") or ""),
                "stale": stale,
            })
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=_EMPTY_COLS)


def find_order(order_id: str, d: date) -> OrderPin:
    """Look up an order by name in the qargo file for the month of `d`.

    Returns geocoded OrderPin. Raises OrderNotFound if the ID is missing.
    Origin/destination latlons may be None if their postcode fails to geocode.
    """
    path = ORDERS_DIR / _order_filename(d)
    df = pd.read_excel(path)
    matches = df[df["name"] == order_id]
    if matches.empty:
        raise OrderNotFound(f"Order {order_id} not in {path.name}")
    row = matches.iloc[0]
    op = str(row["origin_postal_code"])
    dp = str(row["destination_postal_code"])
    return OrderPin(
        order_id=order_id,
        origin_postcode=op,
        destination_postcode=dp,
        origin_latlon=geocode_postcode(op),
        destination_latlon=geocode_postcode(dp),
        destination_time=pd.Timestamp(row["destination_timestamp_local"])
            if pd.notna(row.get("destination_timestamp_local")) else None,
    )
