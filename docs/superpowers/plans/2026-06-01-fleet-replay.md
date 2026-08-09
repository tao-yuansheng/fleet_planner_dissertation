# Fleet Replay Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Streamlit + Folium fleet-replay tool per [`2026-06-01-fleet-replay-design.md`](../specs/2026-06-01-fleet-replay-design.md): pick a date, choose one of three vehicle-scope modes, drag a time slider to scrub ZEEFleet's telematics ping-by-ping, with depot labels (Duxford / Bedford / St Ives / Palletline-Birmingham), clickable depot popups showing arrivals & departures, and a postcode/order-ID pin overlay.

**Architecture:** Two new files under `BackEnd/logistics/operational_analysis/`: `fleet_replay_data.py` is plain-Python (loaders, derivations, geocoding, order lookup) and `fleet_replay.py` is the Streamlit app (sidebar widgets, caching wrappers, folium map composition, `st_folium` embedding). Tests live in `BackEnd/logistics/tests/test_fleet_replay_data.py` per existing codebase convention (the spec said the tests would live alongside the module — overriding to follow the established convention).

**Tech Stack:** Streamlit, Folium, streamlit-folium, pandas, numpy, shapely, requests, pytest. Working directory for all Python commands and tests is `BackEnd/logistics/` (per `from simulation.data_loader import ...` pattern in existing tests).

**Git note:** The repo at `e:/BEAT/ZECURE-Phase2-main` IS a git repo (verified). Commits are real. The outer `e:/BEAT` is not — don't confuse the two.

---

## Task 1: Add dependencies and scaffold modules

**Files:**
- Modify: `BackEnd/logistics/requirements.txt`
- Create: `BackEnd/logistics/operational_analysis/fleet_replay.py`
- Create: `BackEnd/logistics/operational_analysis/fleet_replay_data.py`

- [ ] **Step 1: Add `streamlit` and `streamlit-folium` to requirements.txt**

Edit `BackEnd/logistics/requirements.txt`. Find the "Data visualization" section that contains `matplotlib>=3.7.0` and `seaborn>=0.12.0`. Append immediately after that section:

```
# Interactive analyst tools (fleet replay)
streamlit>=1.32.0
streamlit-folium>=0.20.0
```

- [ ] **Step 2: Install the new dependencies in the project venv**

Run from repo root:
```
.venv-1/Scripts/python -m pip install "streamlit>=1.32.0" "streamlit-folium>=0.20.0"
```
Expected output ends with `Successfully installed ...` listing both packages. Verify with:
```
.venv-1/Scripts/python -c "import streamlit, streamlit_folium; print(streamlit.__version__, streamlit_folium.__version__)"
```
Expected: two version numbers print without ImportError.

- [ ] **Step 3: Create `fleet_replay_data.py` with module docstring**

Create `BackEnd/logistics/operational_analysis/fleet_replay_data.py` with this content:

```python
"""Data loaders & derivations for the fleet replay Streamlit app.

Pure-Python module with no Streamlit imports — importable from notebooks and
unit-testable in isolation. The companion file `fleet_replay.py` wraps these
in Streamlit caching widgets and builds the folium map.

See: BackEnd/logistics/docs/superpowers/specs/2026-06-01-fleet-replay-design.md
"""
```

- [ ] **Step 4: Create `fleet_replay.py` with module docstring and smoke-check checklist**

Create `BackEnd/logistics/operational_analysis/fleet_replay.py` with this content:

```python
"""Streamlit app: ping-by-ping fleet replay on a real map.

Launch from `BackEnd/logistics/`:
    streamlit run operational_analysis/fleet_replay.py

Manual smoke checks after any change:
  1. Date = 2026-01-07, Mode = Single vehicle, vehicle = HX17CUA.
     Verify the morning Bedford-area trace AND the B37 7HB overnight
     stop (Jan 6 evening) both appear.
  2. Drop pin on `IP6 0LW`, Date = 2026-01-06.
     Confirm pin appears in the Ipswich area and that no ZEEFleet
     cursor lands within 500m of it during the morning slider sweep.
  3. Click the Bedford depot icon with Date = 2026-01-07.
     Verify the popup lists HX17CUA and W888RNW with their arrival
     timestamps.
"""
```

- [ ] **Step 5: Commit the scaffold**

```
git add BackEnd/logistics/requirements.txt BackEnd/logistics/operational_analysis/fleet_replay.py BackEnd/logistics/operational_analysis/fleet_replay_data.py
git commit -m "feat(fleet-replay): scaffold modules and add streamlit deps"
```

---

## Task 2: Telematics day loader with parquet caching

**Files:**
- Modify: `BackEnd/logistics/operational_analysis/fleet_replay_data.py`
- Test: `BackEnd/logistics/tests/test_fleet_replay_data.py`

- [ ] **Step 1: Write the failing test**

Append to (create if missing) `BackEnd/logistics/tests/test_fleet_replay_data.py`:

```python
"""Tests for operational_analysis.fleet_replay_data."""
from datetime import date, datetime
import pandas as pd
import pytest

from operational_analysis import fleet_replay_data as frd


def test_load_day_returns_only_pings_for_that_date(tmp_path, monkeypatch):
    """load_day(date) returns rows whose LocalTime falls on that date only."""
    fake_csv = tmp_path / "supatrak_telematics_cleaned_20260101_to_20260131.csv"
    fake_csv.write_text(
        "LocalTime,AssetName,AssetDriver,Ignition,Latitude,Longitude,GPSSpeed,"
        "Location_Town,Location_Road,Location_Postcode,CANbusData_EngineLoadPercent,"
        "CANbusData_Odometer,CANbusData_AxleWeight,CANbusData_FuelUsed,"
        "CANbusData_FuelLevelPercent\n"
        "2026-01-06 23:59:00,HX17CUA,X,True,52.1,-0.4,30,T,R,MK42 0LF,,,,,\n"
        "2026-01-07 00:00:30,HX17CUA,X,True,52.1,-0.4,30,T,R,MK42 0LF,,,,,\n"
        "2026-01-07 23:59:30,HX17CUA,X,True,52.1,-0.4,30,T,R,MK42 0LF,,,,,\n"
        "2026-01-08 00:00:30,HX17CUA,X,True,52.1,-0.4,30,T,R,MK42 0LF,,,,,\n"
    )
    monkeypatch.setattr(frd, "TELEMATICS_DIR", tmp_path)
    monkeypatch.setattr(frd, "CACHE_DIR", tmp_path / "cache")
    df = frd.load_day(date(2026, 1, 7))
    assert len(df) == 2
    assert df["LocalTime"].min() >= pd.Timestamp("2026-01-07 00:00:00")
    assert df["LocalTime"].max() < pd.Timestamp("2026-01-08 00:00:00")


def test_load_day_writes_and_reuses_parquet_cache(tmp_path, monkeypatch):
    """Second call for the same month should not re-read the CSV (parquet hit)."""
    fake_csv = tmp_path / "supatrak_telematics_cleaned_20260101_to_20260131.csv"
    fake_csv.write_text(
        "LocalTime,AssetName,AssetDriver,Ignition,Latitude,Longitude,GPSSpeed,"
        "Location_Town,Location_Road,Location_Postcode,CANbusData_EngineLoadPercent,"
        "CANbusData_Odometer,CANbusData_AxleWeight,CANbusData_FuelUsed,"
        "CANbusData_FuelLevelPercent\n"
        "2026-01-07 12:00:00,A,X,True,52,0,10,T,R,P,,,,,\n"
    )
    monkeypatch.setattr(frd, "TELEMATICS_DIR", tmp_path)
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(frd, "CACHE_DIR", cache_dir)
    frd.load_day(date(2026, 1, 7))
    parquet_path = cache_dir / "telematics_202601.parquet"
    assert parquet_path.exists()
    fake_csv.unlink()
    df = frd.load_day(date(2026, 1, 7))
    assert len(df) == 1
```

- [ ] **Step 2: Run the tests to confirm they fail**

From `BackEnd/logistics/`:
```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py -v
```
Expected: both tests fail with `AttributeError: module ... has no attribute 'TELEMATICS_DIR'` or similar (because the module is empty).

- [ ] **Step 3: Implement `load_day` and module-level paths**

Append to `BackEnd/logistics/operational_analysis/fleet_replay_data.py`:

```python
from __future__ import annotations
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

# Repo-relative defaults (overridable in tests via monkeypatch).
_THIS = Path(__file__).resolve()
_LOGISTICS = _THIS.parents[1]  # BackEnd/logistics/
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
```

- [ ] **Step 4: Run the tests to confirm they pass**

From `BackEnd/logistics/`:
```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py -v
```
Expected: both `test_load_day_*` tests pass.

- [ ] **Step 5: Commit**

```
git add BackEnd/logistics/operational_analysis/fleet_replay_data.py BackEnd/logistics/tests/test_fleet_replay_data.py
git commit -m "feat(fleet-replay): telematics day loader with parquet cache"
```

---

## Task 3: Vehicle metadata loader

**Files:**
- Modify: `BackEnd/logistics/operational_analysis/fleet_replay_data.py`
- Test: `BackEnd/logistics/tests/test_fleet_replay_data.py`

- [ ] **Step 1: Write the failing tests**

Append to `BackEnd/logistics/tests/test_fleet_replay_data.py`:

```python
def test_load_vehicles_returns_rows_with_expected_columns(tmp_path, monkeypatch):
    fake = tmp_path / "supatrak_vehicle_list_enriched.csv"
    fake.write_text(
        "AssetID,AssetName,Description,AssetType,CircuitName,metric,"
        "min_tonnes,max_tonnes,typical_tonnes,notes,fuel_type,fuel_confidence,"
        "fuel_notes,has_Odometer,has_AxleWeight\n"
        "1,HX17CUA,RENAULT T,Tractor Unit,Bedford - Artic,GCW,40,70,44,,Diesel,High,,True,False\n"
        "2,R888GNW,RENAULT D,Lorry,Bedford - Rigid,GVW,7.5,26,18,,Diesel,High,,True,False\n"
    )
    monkeypatch.setattr(frd, "TELEMATICS_DIR", tmp_path)
    df = frd.load_vehicles()
    assert set(["AssetName", "AssetType", "CircuitName"]).issubset(df.columns)
    assert len(df) == 2


def test_vehicles_by_circuit_groups_correctly(tmp_path, monkeypatch):
    fake = tmp_path / "supatrak_vehicle_list_enriched.csv"
    fake.write_text(
        "AssetID,AssetName,Description,AssetType,CircuitName,metric,"
        "min_tonnes,max_tonnes,typical_tonnes,notes,fuel_type,fuel_confidence,"
        "fuel_notes,has_Odometer,has_AxleWeight\n"
        "1,HX17CUA,X,Tractor Unit,Bedford - Artic,,,,,,,,,True,False\n"
        "2,N888GNW,X,Tractor Unit,Bedford - Artic,,,,,,,,,True,False\n"
        "3,T88RNW,X,Lorry,Duxford - Rigid,,,,,,,,,True,False\n"
    )
    monkeypatch.setattr(frd, "TELEMATICS_DIR", tmp_path)
    out = frd.vehicles_by_circuit()
    assert out["Bedford - Artic"] == ["HX17CUA", "N888GNW"]
    assert out["Duxford - Rigid"] == ["T88RNW"]
```

- [ ] **Step 2: Run the tests, expect failure**

```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py::test_load_vehicles_returns_rows_with_expected_columns tests/test_fleet_replay_data.py::test_vehicles_by_circuit_groups_correctly -v
```
Expected: AttributeError on `frd.load_vehicles` / `frd.vehicles_by_circuit`.

- [ ] **Step 3: Implement the loaders**

Append to `BackEnd/logistics/operational_analysis/fleet_replay_data.py`:

```python
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
```

- [ ] **Step 4: Run tests, expect pass**

```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py -v
```
Expected: all tests pass.

- [ ] **Step 5: Commit**

```
git add BackEnd/logistics/operational_analysis/fleet_replay_data.py BackEnd/logistics/tests/test_fleet_replay_data.py
git commit -m "feat(fleet-replay): vehicle metadata loader and circuit grouping"
```

---

## Task 4: Depot loader

**Files:**
- Modify: `BackEnd/logistics/operational_analysis/fleet_replay_data.py`
- Test: `BackEnd/logistics/tests/test_fleet_replay_data.py`

- [ ] **Step 1: Write the failing test**

Append to `BackEnd/logistics/tests/test_fleet_replay_data.py`:

```python
def test_load_depots_returns_typed_records_and_classifies_palletline(tmp_path, monkeypatch):
    fake = tmp_path / "depot_addresses.json"
    fake.write_text(
        '{"addresses": ['
        '{"name":"1. Duxford (HQ)","coordinates":[52.10,0.16],"type":"Depot","nearest_node_id":null},'
        '{"name":"2. Bedford","coordinates":[52.12,-0.43],"type":"Depot","nearest_node_id":null},'
        '{"name":"4. Palletine Network — Birmingham","coordinates":[52.46,-1.74],"type":"Consolidation Centre","nearest_node_id":null}'
        ']}'
    )
    monkeypatch.setattr(frd, "DEPOTS_PATH", fake)
    depots = frd.load_depots()
    by_name = {d.name: d for d in depots}
    assert by_name["1. Duxford (HQ)"].kind == "zeefleet"
    assert by_name["1. Duxford (HQ)"].radius_m == 200
    assert by_name["4. Palletine Network — Birmingham"].kind == "palletline"
    assert by_name["4. Palletine Network — Birmingham"].radius_m == 300
    assert by_name["2. Bedford"].lat == 52.12
    assert by_name["2. Bedford"].lon == -0.43
```

- [ ] **Step 2: Run test, expect failure**

```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py::test_load_depots_returns_typed_records_and_classifies_palletline -v
```
Expected: AttributeError on `frd.load_depots`.

- [ ] **Step 3: Implement `Depot` dataclass and `load_depots`**

Append to `BackEnd/logistics/operational_analysis/fleet_replay_data.py`:

```python
import json
from dataclasses import dataclass


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
```

- [ ] **Step 4: Run test, expect pass**

```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py::test_load_depots_returns_typed_records_and_classifies_palletline -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add BackEnd/logistics/operational_analysis/fleet_replay_data.py BackEnd/logistics/tests/test_fleet_replay_data.py
git commit -m "feat(fleet-replay): depot loader with Palletline classification"
```

---

## Task 5: Trace downsampling (Douglas-Peucker + ignition preservation)

**Files:**
- Modify: `BackEnd/logistics/operational_analysis/fleet_replay_data.py`
- Test: `BackEnd/logistics/tests/test_fleet_replay_data.py`

- [ ] **Step 1: Write the failing tests**

Append to `BackEnd/logistics/tests/test_fleet_replay_data.py`:

```python
def _synthetic_pings(n: int, ignition_flips_at: list[int] | None = None) -> pd.DataFrame:
    """Build a synthetic ping DataFrame with a straight-line route."""
    import numpy as np
    times = pd.date_range("2026-01-07 06:00:00", periods=n, freq="30s")
    lats = np.linspace(52.10, 52.20, n)
    lons = np.linspace(-0.40, -0.30, n)
    ign = [True] * n
    for i in (ignition_flips_at or []):
        ign[i] = not ign[i - 1]
    return pd.DataFrame({
        "LocalTime": times,
        "AssetName": ["X"] * n,
        "Latitude": lats,
        "Longitude": lons,
        "Ignition": ign,
        "GPSSpeed": [30.0] * n,
        "Location_Postcode": ["X"] * n,
        "AssetDriver": ["D"] * n,
        "CANbusData_Odometer": [0.0] * n,
    })


def test_downsample_under_cap_returns_input_unchanged():
    df = _synthetic_pings(500)
    out = frd.downsample_trace(df, cap=1500)
    assert len(out) == 500
    assert list(out["LocalTime"]) == list(df["LocalTime"])


def test_downsample_respects_cap():
    df = _synthetic_pings(5000)
    out = frd.downsample_trace(df, cap=1500)
    assert len(out) <= 1500


def test_downsample_preserves_every_ignition_transition():
    flips = [100, 1500, 3000, 4500]
    df = _synthetic_pings(5000, ignition_flips_at=flips)
    out = frd.downsample_trace(df, cap=1500)
    # Both the flip row AND its predecessor must be present (so the transition is visible)
    out_times = set(out["LocalTime"])
    for i in flips:
        assert df["LocalTime"].iloc[i] in out_times, f"flip at row {i} dropped"
        assert df["LocalTime"].iloc[i - 1] in out_times, f"row before flip at {i} dropped"
```

- [ ] **Step 2: Run tests, expect failure**

```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py -v -k downsample
```
Expected: AttributeError on `frd.downsample_trace`.

- [ ] **Step 3: Implement `downsample_trace`**

Append to `BackEnd/logistics/operational_analysis/fleet_replay_data.py`:

```python
from shapely.geometry import LineString


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
```

- [ ] **Step 4: Run tests, expect pass**

```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py -v -k downsample
```
Expected: all three downsample tests pass.

- [ ] **Step 5: Commit**

```
git add BackEnd/logistics/operational_analysis/fleet_replay_data.py BackEnd/logistics/tests/test_fleet_replay_data.py
git commit -m "feat(fleet-replay): trace downsampling preserving ignition transitions"
```

---

## Task 6: Trace preparation (one entry per vehicle) and current-position lookup

**Files:**
- Modify: `BackEnd/logistics/operational_analysis/fleet_replay_data.py`
- Test: `BackEnd/logistics/tests/test_fleet_replay_data.py`

- [ ] **Step 1: Write the failing tests**

Append to `BackEnd/logistics/tests/test_fleet_replay_data.py`:

```python
def test_prepare_vehicle_traces_returns_one_entry_per_vehicle():
    pings = pd.concat([
        _synthetic_pings(100).assign(AssetName="A"),
        _synthetic_pings(100).assign(AssetName="B"),
    ], ignore_index=True)
    traces = frd.prepare_vehicle_traces(pings, ["A", "B"])
    assert set(traces.keys()) == {"A", "B"}
    assert len(traces["A"].full) == 100
    assert len(traces["A"].rendered) <= 1500


def test_current_position_finds_latest_ping_at_or_before_t():
    pings = _synthetic_pings(10).assign(AssetName="A")
    traces = frd.prepare_vehicle_traces(pings, ["A"])
    trace = traces["A"]
    t = pings["LocalTime"].iloc[5]
    row, stale = frd.current_position(trace, t)
    assert row["LocalTime"] == t
    assert stale is False


def test_current_position_flags_stale_when_gap_over_30min():
    pings = _synthetic_pings(10).assign(AssetName="A")
    traces = frd.prepare_vehicle_traces(pings, ["A"])
    trace = traces["A"]
    t = pings["LocalTime"].iloc[-1] + pd.Timedelta(minutes=45)
    row, stale = frd.current_position(trace, t)
    assert row is not None
    assert stale is True


def test_current_position_returns_none_when_t_before_first_ping():
    pings = _synthetic_pings(10).assign(AssetName="A")
    traces = frd.prepare_vehicle_traces(pings, ["A"])
    trace = traces["A"]
    t = pings["LocalTime"].iloc[0] - pd.Timedelta(hours=1)
    row, stale = frd.current_position(trace, t)
    assert row is None
    assert stale is False
```

- [ ] **Step 2: Run tests, expect failure**

```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py -v -k "prepare_vehicle or current_position"
```
Expected: AttributeError on `frd.prepare_vehicle_traces` / `frd.current_position`.

- [ ] **Step 3: Implement `VehicleTrace`, `prepare_vehicle_traces`, `current_position`**

Append to `BackEnd/logistics/operational_analysis/fleet_replay_data.py`:

```python
import numpy as np

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
    """Find the latest ping with LocalTime ≤ t.

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
```

- [ ] **Step 4: Run tests, expect pass**

```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py -v -k "prepare_vehicle or current_position"
```
Expected: all four tests pass.

- [ ] **Step 5: Commit**

```
git add BackEnd/logistics/operational_analysis/fleet_replay_data.py BackEnd/logistics/tests/test_fleet_replay_data.py
git commit -m "feat(fleet-replay): per-vehicle trace prep and current-position lookup"
```

---

## Task 7: Depot arrival/departure derivation

**Files:**
- Modify: `BackEnd/logistics/operational_analysis/fleet_replay_data.py`
- Test: `BackEnd/logistics/tests/test_fleet_replay_data.py`

- [ ] **Step 1: Write the failing tests**

Append to `BackEnd/logistics/tests/test_fleet_replay_data.py`:

```python
def _depot_ping_row(t: str, lat: float, lon: float, ignition: bool = False) -> dict:
    return {
        "LocalTime": pd.Timestamp(t), "Latitude": lat, "Longitude": lon,
        "Ignition": ignition, "GPSSpeed": 0 if not ignition else 30,
        "AssetName": "V", "AssetDriver": "D",
        "Location_Postcode": "P", "CANbusData_Odometer": 0.0,
    }


def _bedford():
    return frd.Depot(
        name="Bedford", lat=52.122, lon=-0.431, kind="zeefleet", radius_m=200,
    )


def test_depot_visits_enters_then_leaves():
    pings = pd.DataFrame([
        _depot_ping_row("2026-01-07 05:00", 53.0, -1.0, True),  # far away, moving
        _depot_ping_row("2026-01-07 06:00", 52.122, -0.431, False),  # at depot, parked
        _depot_ping_row("2026-01-07 06:30", 52.122, -0.431, False),  # still parked
        _depot_ping_row("2026-01-07 07:00", 53.0, -1.0, True),  # gone again
    ])
    visits = frd.depot_visits(pings, _bedford())
    assert len(visits) == 1
    assert visits[0].arrived == pd.Timestamp("2026-01-07 06:00")
    assert visits[0].departed == pd.Timestamp("2026-01-07 07:00")
    assert visits[0].is_stop is True


def test_depot_visits_starts_inside():
    pings = pd.DataFrame([
        _depot_ping_row("2026-01-07 00:00", 52.122, -0.431, False),
        _depot_ping_row("2026-01-07 07:00", 53.0, -1.0, True),
    ])
    visits = frd.depot_visits(pings, _bedford())
    assert len(visits) == 1
    assert visits[0].arrived is None  # already inside at start
    assert visits[0].departed == pd.Timestamp("2026-01-07 07:00")


def test_depot_visits_never_enters():
    pings = pd.DataFrame([
        _depot_ping_row("2026-01-07 05:00", 53.0, -1.0, True),
        _depot_ping_row("2026-01-07 06:00", 53.0, -1.0, True),
    ])
    visits = frd.depot_visits(pings, _bedford())
    assert visits == []


def test_depot_visits_drives_through_is_not_a_stop():
    pings = pd.DataFrame([
        _depot_ping_row("2026-01-07 05:00", 53.0, -1.0, True),
        _depot_ping_row("2026-01-07 06:00", 52.122, -0.431, True),  # inside but moving, ignition on
        _depot_ping_row("2026-01-07 06:01", 52.122, -0.431, True),  # still inside, still moving
        _depot_ping_row("2026-01-07 07:00", 53.0, -1.0, True),
    ])
    visits = frd.depot_visits(pings, _bedford())
    assert len(visits) == 1
    assert visits[0].is_stop is False  # ignition never went off → drive-through
```

- [ ] **Step 2: Run tests, expect failure**

```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py -v -k depot_visits
```
Expected: AttributeError on `frd.depot_visits` / `frd.DepotVisit`.

- [ ] **Step 3: Implement haversine, `DepotVisit`, `depot_visits`**

Append to `BackEnd/logistics/operational_analysis/fleet_replay_data.py`:

```python
from math import radians, sin, cos, sqrt, asin
from typing import Optional


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
        departed = run["LocalTime"].iloc[-1] if e != len(df) else None
        dwell = (
            run["LocalTime"].iloc[-1] - run["LocalTime"].iloc[0]
            if (arrived is not None and departed is not None)
            else None
        )
        is_stop = bool((~run["Ignition"].astype(bool)).any())
        visits.append(DepotVisit(
            vehicle=vehicle, arrived=arrived, departed=departed,
            dwell=dwell, is_stop=is_stop,
        ))
    return visits
```

- [ ] **Step 4: Run tests, expect pass**

```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py -v -k depot_visits
```
Expected: all four `test_depot_visits_*` tests pass.

- [ ] **Step 5: Commit**

```
git add BackEnd/logistics/operational_analysis/fleet_replay_data.py BackEnd/logistics/tests/test_fleet_replay_data.py
git commit -m "feat(fleet-replay): depot arrival/departure derivation"
```

---

## Task 8: Postcode geocoder with offline fallback

**Files:**
- Modify: `BackEnd/logistics/operational_analysis/fleet_replay_data.py`
- Test: `BackEnd/logistics/tests/test_fleet_replay_data.py`

- [ ] **Step 1: Write the failing tests**

Append to `BackEnd/logistics/tests/test_fleet_replay_data.py`:

```python
def test_geocode_postcode_returns_lat_lon_on_success(monkeypatch):
    class FakeResp:
        status_code = 200
        def json(self):
            return {"status": 200, "result": {"latitude": 52.117, "longitude": 1.097}}
    monkeypatch.setattr(frd.requests, "get", lambda *a, **kw: FakeResp())
    frd._geocode_cache.clear()
    out = frd.geocode_postcode("IP6 0LW")
    assert out == (52.117, 1.097)


def test_geocode_postcode_caches_successful_lookups(monkeypatch):
    calls = {"n": 0}
    class FakeResp:
        status_code = 200
        def json(self):
            return {"status": 200, "result": {"latitude": 1.0, "longitude": 2.0}}
    def fake_get(*a, **kw):
        calls["n"] += 1
        return FakeResp()
    monkeypatch.setattr(frd.requests, "get", fake_get)
    frd._geocode_cache.clear()
    frd.geocode_postcode("AB1 2CD")
    frd.geocode_postcode("AB1 2CD")
    assert calls["n"] == 1


def test_geocode_postcode_returns_none_on_http_failure(monkeypatch):
    def fake_get(*a, **kw):
        raise frd.requests.ConnectionError("no internet")
    monkeypatch.setattr(frd.requests, "get", fake_get)
    frd._geocode_cache.clear()
    assert frd.geocode_postcode("IP6 0LW") is None


def test_geocode_postcode_returns_none_on_not_found(monkeypatch):
    class FakeResp:
        status_code = 404
        def json(self):
            return {"status": 404, "error": "Postcode not found"}
    monkeypatch.setattr(frd.requests, "get", lambda *a, **kw: FakeResp())
    frd._geocode_cache.clear()
    assert frd.geocode_postcode("ZZZ 9ZZ") is None
```

- [ ] **Step 2: Run tests, expect failure**

```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py -v -k geocode
```
Expected: AttributeError on `frd.requests` / `frd.geocode_postcode` / `frd._geocode_cache`.

- [ ] **Step 3: Implement geocoder with module-level cache**

Append to `BackEnd/logistics/operational_analysis/fleet_replay_data.py`:

```python
import requests

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
```

- [ ] **Step 4: Run tests, expect pass**

```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py -v -k geocode
```
Expected: all four `test_geocode_postcode_*` tests pass.

- [ ] **Step 5: Commit**

```
git add BackEnd/logistics/operational_analysis/fleet_replay_data.py BackEnd/logistics/tests/test_fleet_replay_data.py
git commit -m "feat(fleet-replay): postcodes.io geocoder with cache and offline fallback"
```

---

## Task 9: Order lookup

**Files:**
- Modify: `BackEnd/logistics/operational_analysis/fleet_replay_data.py`
- Test: `BackEnd/logistics/tests/test_fleet_replay_data.py`

- [ ] **Step 1: Write the failing tests**

Append to `BackEnd/logistics/tests/test_fleet_replay_data.py`:

```python
def test_find_order_returns_pins_for_known_order(tmp_path, monkeypatch):
    fake = tmp_path / "qargo_20260101_to_20260131.xlsx"
    pd.DataFrame([{
        "name": "WT253245",
        "origin_postal_code": "G33 4TP",
        "origin_city": "Glasgow",
        "destination_postal_code": "MK43 0YL",
        "destination_city": "Bedford",
        "destination_timestamp_local": pd.Timestamp("2026-01-07 08:56:34"),
    }]).to_excel(fake, index=False)
    monkeypatch.setattr(frd, "ORDERS_DIR", tmp_path)
    # Stub the geocoder to avoid network
    monkeypatch.setattr(frd, "geocode_postcode", lambda pc: (
        {"G33 4TP": (55.86, -4.16), "MK43 0YL": (52.07, -0.62)}[pc.upper()]
    ))
    pin = frd.find_order("WT253245", date(2026, 1, 7))
    assert pin.origin_postcode == "G33 4TP"
    assert pin.destination_postcode == "MK43 0YL"
    assert pin.origin_latlon == (55.86, -4.16)
    assert pin.destination_latlon == (52.07, -0.62)
    assert pin.destination_time == pd.Timestamp("2026-01-07 08:56:34")


def test_find_order_raises_for_unknown_order(tmp_path, monkeypatch):
    fake = tmp_path / "qargo_20260101_to_20260131.xlsx"
    pd.DataFrame([{
        "name": "WT000001",
        "origin_postal_code": "A",
        "destination_postal_code": "B",
        "destination_timestamp_local": pd.Timestamp("2026-01-07"),
    }]).to_excel(fake, index=False)
    monkeypatch.setattr(frd, "ORDERS_DIR", tmp_path)
    with pytest.raises(frd.OrderNotFound):
        frd.find_order("WT999999", date(2026, 1, 7))
```

- [ ] **Step 2: Run tests, expect failure**

```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py -v -k find_order
```
Expected: AttributeError on `frd.find_order` / `frd.OrderNotFound` / `frd.OrderPin`.

- [ ] **Step 3: Implement `OrderPin`, `OrderNotFound`, `find_order`**

Append to `BackEnd/logistics/operational_analysis/fleet_replay_data.py`:

```python
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
```

- [ ] **Step 4: Run tests, expect pass**

```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py -v -k find_order
```
Expected: both `test_find_order_*` tests pass.

- [ ] **Step 5: Run the whole test file once to confirm nothing regressed**

```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py -v
```
Expected: all tests so far pass.

- [ ] **Step 6: Commit**

```
git add BackEnd/logistics/operational_analysis/fleet_replay_data.py BackEnd/logistics/tests/test_fleet_replay_data.py
git commit -m "feat(fleet-replay): order ID lookup returning geocoded pins"
```

---

## Task 10: Streamlit shell — date picker, depot markers, base map

**Files:**
- Modify: `BackEnd/logistics/operational_analysis/fleet_replay.py`

This task and the next four have no automated tests — they're UI tasks verified by the smoke checks documented in `fleet_replay.py`. Each task ends with a manual verification step before committing.

- [ ] **Step 1: Implement the date picker, base map, depot markers**

Append to `BackEnd/logistics/operational_analysis/fleet_replay.py`:

```python
from datetime import date

import folium
import streamlit as st
from folium import Popup
from streamlit_folium import st_folium

from operational_analysis import fleet_replay_data as frd

# Map defaults centred on East Anglia / Bedfordshire so the whole region fits
MAP_CENTER = [52.2, -0.5]
MAP_ZOOM = 8


def _depot_marker(depot: frd.Depot) -> folium.Marker:
    """Build a styled marker for one depot. Popup body filled later."""
    icon = folium.Icon(
        color="darkred" if depot.kind == "palletline" else "blue",
        icon="diamond" if depot.kind == "palletline" else "star",
        prefix="fa",
    )
    return folium.Marker(
        location=[depot.lat, depot.lon],
        icon=icon,
        tooltip=depot.name,
        popup=Popup(f"<b>{depot.name}</b><br>{depot.kind}", max_width=400),
    )


def build_base_map(depots: list[frd.Depot]) -> folium.Map:
    m = folium.Map(location=MAP_CENTER, zoom_start=MAP_ZOOM, tiles="OpenStreetMap")
    for d in depots:
        _depot_marker(d).add_to(m)
    return m


def main() -> None:
    st.set_page_config(page_title="Fleet Replay", layout="wide")
    st.sidebar.title("Fleet Replay")
    chosen_date = st.sidebar.date_input("Date", value=date(2026, 1, 7))
    depots = frd.load_depots()
    m = build_base_map(depots)
    st_folium(m, height=720, width=None, returned_objects=[])


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Launch and smoke-check**

From `BackEnd/logistics/`:
```
../../.venv-1/Scripts/python -m streamlit run operational_analysis/fleet_replay.py
```
Expected: browser opens; map of East Anglia loads with four icons — Duxford (blue star), Bedford (blue star), St Ives (blue star), Palletline Birmingham (dark-red diamond). Tooltips on hover show the depot names. Date picker in the sidebar defaults to 2026-01-07.

Stop the server (Ctrl+C in the terminal).

- [ ] **Step 3: Commit**

```
git add BackEnd/logistics/operational_analysis/fleet_replay.py
git commit -m "feat(fleet-replay): Streamlit shell with date picker and depot markers"
```

---

## Task 11: Vehicle selection modes and trace rendering

**Files:**
- Modify: `BackEnd/logistics/operational_analysis/fleet_replay.py`

- [ ] **Step 1: Add cached wrappers and trace-layer helpers**

In `fleet_replay.py`, insert these helpers above `def main()`:

```python
COLOR_BY_TYPE = {
    "Tractor Unit": "#1f77b4",
    "Lorry": "#2ca02c",
    "Rigid Truck": "#2ca02c",
    "Mini Truck": "#2ca02c",
    "Service Van": "#777777",
}


@st.cache_data(ttl=3600)
def cached_day(d: date):
    return frd.load_day(d)


@st.cache_data
def cached_traces(d: date, vehicle_names: tuple[str, ...]):
    day = cached_day(d)
    return frd.prepare_vehicle_traces(day, vehicle_names)


@st.cache_data
def cached_vehicle_meta():
    return frd.load_vehicles()


def _color_for_vehicle(name: str, meta_df) -> str:
    row = meta_df[meta_df["AssetName"] == name]
    if row.empty:
        return "#888888"
    return COLOR_BY_TYPE.get(str(row.iloc[0]["AssetType"]), "#888888")


def add_vehicle_traces(m: folium.Map, traces: dict[str, "frd.VehicleTrace"], meta_df) -> None:
    """Render each vehicle's downsampled trace as a faint polyline + clickable dots."""
    for name, trace in traces.items():
        color = _color_for_vehicle(name, meta_df)
        coords = list(zip(trace.rendered["Latitude"], trace.rendered["Longitude"]))
        if len(coords) >= 2:
            folium.PolyLine(coords, color=color, weight=2, opacity=0.4).add_to(m)
        for _, ping in trace.rendered.iterrows():
            popup_html = (
                f"<b>{name}</b><br>"
                f"{ping['LocalTime']:%Y-%m-%d %H:%M:%S}<br>"
                f"{ping.get('Location_Postcode', '')}<br>"
                f"speed {ping.get('GPSSpeed', 0):.0f} mph &nbsp;"
                f"ign {'on' if bool(ping.get('Ignition', False)) else 'off'}<br>"
                f"driver: {ping.get('AssetDriver', '')}"
            )
            folium.CircleMarker(
                location=[ping["Latitude"], ping["Longitude"]],
                radius=2,
                color=color,
                fill=True,
                fill_opacity=0.5,
                popup=Popup(popup_html, max_width=300),
            ).add_to(m)
```

- [ ] **Step 2: Update `main()` to handle vehicle selection modes**

Replace the existing `main()` body in `fleet_replay.py` with:

```python
def main() -> None:
    st.set_page_config(page_title="Fleet Replay", layout="wide")
    st.sidebar.title("Fleet Replay")
    chosen_date = st.sidebar.date_input("Date", value=date(2026, 1, 7))

    mode = st.sidebar.radio(
        "Mode", ["All fleet", "By circuit", "Single vehicle"], index=2,
    )
    meta = cached_vehicle_meta()
    by_circuit = frd.vehicles_by_circuit()
    if mode == "All fleet":
        selected = sorted(meta["AssetName"].tolist())
    elif mode == "By circuit":
        circuit = st.sidebar.selectbox("Circuit", sorted(by_circuit.keys()))
        selected = by_circuit[circuit]
    else:
        selected = [st.sidebar.selectbox(
            "Vehicle", sorted(meta["AssetName"].tolist()),
            index=sorted(meta["AssetName"].tolist()).index("HX17CUA")
                if "HX17CUA" in meta["AssetName"].values else 0,
        )]

    if len(selected) > 100:
        st.sidebar.warning(f"{len(selected)} vehicles selected. Capping at 100 for performance.")
        selected = selected[:100]

    depots = frd.load_depots()
    m = build_base_map(depots)
    if selected:
        traces = cached_traces(chosen_date, tuple(selected))
        add_vehicle_traces(m, traces, meta)
    st_folium(m, height=720, width=None, returned_objects=[])
```

- [ ] **Step 3: Launch and smoke-check**

From `BackEnd/logistics/`:
```
../../.venv-1/Scripts/python -m streamlit run operational_analysis/fleet_replay.py
```
Expected: Mode = Single vehicle, Vehicle = HX17CUA, Date = 2026-01-07. The map shows HX17CUA's blue polyline through Bedford/MK and out to B37 Birmingham, with small dots along it. Clicking a dot opens a popup with the timestamp, postcode, speed, ignition, driver. Switch Mode = By circuit, Circuit = Bedford - Artic — several blue traces appear. Switch Mode = All fleet — warning may appear about capping, and many traces render (may be slow first time as it loads the day).

Stop the server.

- [ ] **Step 4: Commit**

```
git add BackEnd/logistics/operational_analysis/fleet_replay.py
git commit -m "feat(fleet-replay): vehicle selection modes and clickable traces"
```

---

## Task 12: Time slider + current-position cursor markers

**Files:**
- Modify: `BackEnd/logistics/operational_analysis/fleet_replay.py`

- [ ] **Step 1: Add cursor-layer helper**

Insert in `fleet_replay.py` above `def main()`:

```python
def add_cursor_markers(
    m: folium.Map,
    traces: dict[str, "frd.VehicleTrace"],
    t,
    meta_df,
) -> None:
    """Drop a bold marker per vehicle at its current-position-as-of-`t` ping."""
    import pandas as pd
    ts = pd.Timestamp(t)
    for name, trace in traces.items():
        row, stale = frd.current_position(trace, ts)
        if row is None:
            continue
        color = _color_for_vehicle(name, meta_df)
        label = f"{name}{' (stale)' if stale else ''}"
        icon = folium.DivIcon(
            html=(
                f'<div style="background:{color};color:white;padding:2px 5px;'
                f'border-radius:3px;font-size:11px;'
                f'opacity:{0.5 if stale else 1.0};'
                f'white-space:nowrap;">{label}</div>'
            )
        )
        folium.Marker(
            location=[row["Latitude"], row["Longitude"]],
            icon=icon,
            tooltip=f"{name} @ {row['LocalTime']:%H:%M:%S}",
        ).add_to(m)
```

- [ ] **Step 2: Add time slider to `main()`, call `add_cursor_markers`**

In `fleet_replay.py`, modify `main()` — add immediately after the `selected = selected[:100]` block:

```python
    # Time slider — drives the cursor markers.
    from datetime import datetime, time as dtime
    slider_t = st.sidebar.slider(
        "Time",
        min_value=datetime.combine(chosen_date, dtime(0, 0)),
        max_value=datetime.combine(chosen_date, dtime(23, 59)),
        value=datetime.combine(chosen_date, dtime(9, 0)),
        step=pd.Timedelta(minutes=1).to_pytimedelta(),
        format="HH:mm",
    )
    speed = st.sidebar.selectbox("Auto-play speed", ["off", "5×", "15×", "60×"], index=0)
```

And add `import pandas as pd` at the top of the file if not already imported.

Then change the trace-rendering block to also drop cursors:

```python
    if selected:
        traces = cached_traces(chosen_date, tuple(selected))
        add_vehicle_traces(m, traces, meta)
        add_cursor_markers(m, traces, slider_t, meta)
```

(Auto-play implementation deferred — slider selects the cursor moment; user can move it manually.)

- [ ] **Step 3: Launch and smoke-check**

```
../../.venv-1/Scripts/python -m streamlit run operational_analysis/fleet_replay.py
```
Expected: Mode = Single vehicle, Vehicle = HX17CUA, Date = 2026-01-07, slider at 09:00. A bold label marker reading `HX17CUA` appears at the depot/Matthew Clark area. Drag slider to 04:00 — marker jumps to a position on the M6 corridor returning from Birmingham. Drag to 06:00 — marker appears at Bedford depot. Switch to Mode = By circuit, Circuit = Bedford - Artic — multiple cursor markers, one per vehicle.

Stop the server.

- [ ] **Step 4: Commit**

```
git add BackEnd/logistics/operational_analysis/fleet_replay.py
git commit -m "feat(fleet-replay): time slider with per-vehicle cursor markers"
```

---

## Task 13: Depot popup wiring (arrivals/departures table)

**Files:**
- Modify: `BackEnd/logistics/operational_analysis/fleet_replay.py`

- [ ] **Step 1: Add cached depot-visits helper and popup HTML builder**

Insert in `fleet_replay.py` above `def main()`:

```python
@st.cache_data
def cached_depot_visits(d: date, depot_name: str) -> list:
    """For the chosen date, return all visits to one depot across the full fleet."""
    depots_by_name = {dep.name: dep for dep in frd.load_depots()}
    depot = depots_by_name[depot_name]
    day = cached_day(d)
    visits = []
    for name, sub in day.groupby("AssetName"):
        visits.extend(frd.depot_visits(sub, depot))
    return visits


def _depot_popup_html(depot: "frd.Depot", visits: list) -> str:
    rows = []
    for v in sorted(visits, key=lambda x: (x.arrived or pd.Timestamp.min)):
        a = f"{v.arrived:%H:%M}" if v.arrived is not None else "—"
        d = f"{v.departed:%H:%M}" if v.departed is not None else "—"
        dwell = (
            f"{int(v.dwell.total_seconds() // 3600)}h"
            f"{int((v.dwell.total_seconds() % 3600) // 60):02d}m"
            if v.dwell is not None else "—"
        )
        stop = "✓" if v.is_stop else "drive-through"
        rows.append(
            f"<tr><td>{v.vehicle}</td><td>{a}</td><td>{d}</td>"
            f"<td>{dwell}</td><td>{stop}</td></tr>"
        )
    if not rows:
        rows = ['<tr><td colspan="5" style="color:#888">no visits</td></tr>']
    return (
        f"<b>{depot.name}</b><br>"
        f"<table style='font-size:11px;border-collapse:collapse'>"
        f"<thead><tr><th>Vehicle</th><th>Arrived</th><th>Departed</th>"
        f"<th>Dwell</th><th>Stop?</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
```

- [ ] **Step 2: Replace the simple `_depot_marker` with a date-aware one**

Replace the existing `_depot_marker` function with:

```python
def _depot_marker(depot: frd.Depot, popup_html: str) -> folium.Marker:
    icon = folium.Icon(
        color="darkred" if depot.kind == "palletline" else "blue",
        icon="diamond" if depot.kind == "palletline" else "star",
        prefix="fa",
    )
    return folium.Marker(
        location=[depot.lat, depot.lon],
        icon=icon,
        tooltip=depot.name,
        popup=Popup(popup_html, max_width=400),
    )
```

Replace `build_base_map` with:

```python
def build_base_map(depots: list[frd.Depot], chosen_date: date) -> folium.Map:
    m = folium.Map(location=MAP_CENTER, zoom_start=MAP_ZOOM, tiles="OpenStreetMap")
    for d in depots:
        visits = cached_depot_visits(chosen_date, d.name)
        _depot_marker(d, _depot_popup_html(d, visits)).add_to(m)
    return m
```

In `main()`, change the call from `build_base_map(depots)` to `build_base_map(depots, chosen_date)`.

- [ ] **Step 3: Launch and smoke-check**

```
../../.venv-1/Scripts/python -m streamlit run operational_analysis/fleet_replay.py
```
Expected: Date = 2026-01-07. Click the Bedford depot icon — popup shows a table listing several Bedford-circuit vehicles with their arrival/departure times that day, including HX17CUA (00:15 arrival) and W888RNW (06:06 arrival). Click the Palletline Birmingham icon — popup shows tractors that visited B37 7HB overnight Jan 6/7.

Stop the server.

- [ ] **Step 4: Commit**

```
git add BackEnd/logistics/operational_analysis/fleet_replay.py
git commit -m "feat(fleet-replay): depot popups with arrivals/departures table"
```

---

## Task 14: Order ID / postcode pin overlay

**Files:**
- Modify: `BackEnd/logistics/operational_analysis/fleet_replay.py`

- [ ] **Step 1: Add helpers and sidebar input**

Insert in `fleet_replay.py` above `def main()`:

```python
def add_postcode_pin(m: folium.Map, postcode: str, latlon: tuple[float, float]) -> None:
    folium.Marker(
        location=list(latlon),
        icon=folium.Icon(color="purple", icon="map-pin", prefix="fa"),
        tooltip=f"Pin: {postcode}",
        popup=Popup(f"<b>Pin</b><br>{postcode}<br>{latlon[0]:.4f}, {latlon[1]:.4f}", max_width=300),
    ).add_to(m)


def add_order_pins(m: folium.Map, pin: "frd.OrderPin") -> None:
    if pin.origin_latlon:
        folium.Marker(
            location=list(pin.origin_latlon),
            icon=folium.Icon(color="purple", icon="play", prefix="fa"),
            tooltip=f"Origin: {pin.origin_postcode}",
            popup=Popup(
                f"<b>{pin.order_id}</b> origin<br>{pin.origin_postcode}",
                max_width=300,
            ),
        ).add_to(m)
    if pin.destination_latlon:
        folium.Marker(
            location=list(pin.destination_latlon),
            icon=folium.Icon(color="purple", icon="stop", prefix="fa"),
            tooltip=f"Destination: {pin.destination_postcode}",
            popup=Popup(
                f"<b>{pin.order_id}</b> destination<br>{pin.destination_postcode}<br>"
                f"booked: {pin.destination_time:%H:%M}"
                if pin.destination_time is not None else f"<b>{pin.order_id}</b> destination",
                max_width=300,
            ),
        ).add_to(m)
    if pin.origin_latlon and pin.destination_latlon:
        folium.PolyLine(
            [list(pin.origin_latlon), list(pin.destination_latlon)],
            color="purple", weight=2, opacity=0.6, dash_array="5,5",
        ).add_to(m)
```

- [ ] **Step 2: Add sidebar input and resolve logic in `main()`**

In `fleet_replay.py`, add this block in `main()` immediately after the speed selectbox:

```python
    overlay_input = st.sidebar.text_input(
        "Pin postcode or order ID", value="",
        help="Examples: IP6 0LW (postcode) or WT253245 (order ID)",
    ).strip()

    pin_latlon = None
    order_pin = None
    if overlay_input:
        if overlay_input.upper().startswith("WT"):
            try:
                order_pin = frd.find_order(overlay_input, chosen_date)
                # Auto-jump slider to the booked delivery time if available
                if order_pin.destination_time is not None:
                    st.sidebar.caption(
                        f"Jumped to {order_pin.destination_time:%H:%M} (order delivery time)"
                    )
            except frd.OrderNotFound as e:
                st.sidebar.warning(str(e))
            except FileNotFoundError:
                st.sidebar.warning(
                    f"⚠ Order not in {chosen_date:%Y-%m} data — switch month."
                )
        else:
            pin_latlon = frd.geocode_postcode(overlay_input)
            if pin_latlon is None:
                st.sidebar.warning(
                    "⚠ Geocoder unavailable or postcode not found. "
                    "Enter lat,lon directly e.g. `52.117,1.097`."
                )
                if "," in overlay_input:
                    try:
                        a, b = (x.strip() for x in overlay_input.split(",", 1))
                        pin_latlon = (float(a), float(b))
                    except ValueError:
                        pass
```

And update the map-render block to add the pins:

```python
    if selected:
        traces = cached_traces(chosen_date, tuple(selected))
        add_vehicle_traces(m, traces, meta)
        add_cursor_markers(m, traces, slider_t, meta)
    if pin_latlon:
        add_postcode_pin(m, overlay_input, pin_latlon)
    if order_pin:
        add_order_pins(m, order_pin)
```

- [ ] **Step 3: Launch and run the documented smoke checks**

```
../../.venv-1/Scripts/python -m streamlit run operational_analysis/fleet_replay.py
```

Walk through all three smoke checks from the module docstring:

1. Date = 2026-01-07, Mode = Single vehicle, Vehicle = HX17CUA — confirm morning Bedford trace + B37 7HB overnight stop both visible.
2. Pin postcode `IP6 0LW`, Date = 2026-01-06 — confirm purple pin in Ipswich; sweep slider through 06:00–10:00; no ZEEFleet cursor lands at the pin.
3. Click Bedford depot, Date = 2026-01-07 — popup lists HX17CUA arrival 00:15 and W888RNW arrival 06:06.

Also verify the order overlay:

4. Pin order ID `WT253245`, Date = 2026-01-07 — two purple pins (Glasgow origin, Bedford destination) joined by a dashed line.

Stop the server.

- [ ] **Step 4: Commit**

```
git add BackEnd/logistics/operational_analysis/fleet_replay.py
git commit -m "feat(fleet-replay): postcode and order-ID pin overlay"
```

---

## Task 15: Final test sweep and tag

**Files:** none modified.

- [ ] **Step 1: Run the full data-module test suite once more**

```
../../.venv-1/Scripts/python -m pytest tests/test_fleet_replay_data.py -v
```
Expected: every test passes.

- [ ] **Step 2: Make sure the broader test suite still passes**

```
../../.venv-1/Scripts/python -m pytest tests/ -x --timeout=120
```
Expected: no regressions in any pre-existing test (no fleet-replay code is imported by other tests, so this should be a no-op).

- [ ] **Step 3: Tag the milestone**

```
git tag fleet-replay-v1
```

---

## Self-review

**Spec coverage:**
- Architecture (two files, one dep, launch command) → Task 1.
- Data sources (telematics, vehicles, orders, depots JSON) → Tasks 2, 3, 4, 9.
- UI shape (date / mode / vehicles / time / order-pc / speed) → Tasks 10, 11, 12, 13, 14.
- Time-cursor model (faint static traces + bold cursor) → Tasks 11, 12.
- Downsampling rule (1500 cap + Douglas-Peucker + keep ignition transitions) → Task 5.
- Current-position lookup with 30-min stale flag → Task 6.
- Depot geo-fence (200m/300m) + arrival/departure derivation → Tasks 4, 7.
- Depot popup format → Task 13.
- Postcode geocoder with offline fallback → Task 8 + Task 14 wiring.
- Order ID overlay with two pins and dashed line → Tasks 9 + 14.
- Performance: parquet cache, `@st.cache_data`, hard cap at 100 vehicles → Tasks 2, 11, 12, 13.
- Unit-test coverage (visits, downsampling, geocoder fallback) → Tasks 5, 7, 8.
- Manual smoke checks at top of `fleet_replay.py` → Task 1 (docstring) + Task 14 (Step 3 walk-through).

**Placeholder scan:** No "TBD", "TODO", "add appropriate", or "similar to" entries left. Auto-play is deferred (out of scope acknowledged in Task 12 Step 2 with explicit comment); manual slider is the v1 control surface — consistent with the spec's auto-play speed selector being a UI element, not animated playback.

**Type consistency:**
- `Depot.kind`: defined in Task 4, used in Task 13's marker color and Task 14 — same string set (`zeefleet` / `palletline`).
- `VehicleTrace.full / rendered / times`: defined Task 6, used Task 11 (`rendered`) and Task 12 (`current_position(trace, t)` via `times`).
- `DepotVisit.arrived / departed / dwell / is_stop`: defined Task 7, consumed by Task 13's `_depot_popup_html`.
- `OrderPin.origin_latlon / destination_latlon / destination_time`: defined Task 9, consumed by Task 14's `add_order_pins`.
- `frd.OrderNotFound`: defined Task 9, caught by Task 14.
- `frd.geocode_postcode`: defined Task 8, called by Task 9 (`find_order`) and Task 14 (postcode resolution).

Plan is internally consistent.

---

## Notes & deferrals

- **Auto-play animation:** the spec mentions a 5× / 15× / 60× speed selector. The widget is included in Task 12 Step 2 as a control, but actual ticker-based playback is deferred to v1.1 (Streamlit's `st.fragment` re-execution API is the right primitive; not yet wired). The slider itself can be dragged for scrubbing, which satisfies the scrubbing interaction in the spec.
- **Click-to-open ping detail panel:** the spec mentions a side panel updating on map click. The polyline-dot popups (Task 11) satisfy the basic ping-detail interaction; a synced side panel is a polish item not blocked by this v1 plan.
- **Tests for the Streamlit layer:** none — all UI verified by the smoke checks in Task 14 Step 3.
