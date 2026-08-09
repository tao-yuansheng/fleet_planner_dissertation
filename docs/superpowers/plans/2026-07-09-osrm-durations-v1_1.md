# freight-planner v1.1 (OSRM durations + per-type calibration + depot timing + duty axis) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the daily evaluator OSRM road-type travel times calibrated per vehicle type (flag-gated, default off), emit the depot-to-depot clock, and validate it against telematics — without disturbing the experiment campaign.

**Architecture:** Part A is a standalone, reproducible calibration+validation script producing per-type duration factors. Part B swaps the daily evaluator's `drive_minutes(km)` for a per-segment, per-type `road_minutes(...)` gated by `config.USE_OSRM_DURATIONS` (off ⇒ byte-identical to today). Parts C and D are always-on, non-mutating: C threads the already-computed route start/end times into `route_stops.csv`; D adds a duty-hours axis to `viz_app --validate`.

**Tech Stack:** Python 3, pandas, numpy, pytest; self-hosted OSRM (`simulation.routing` router with cached truck-adjusted durations); existing freight_planner pipeline.

**Spec:** `docs/superpowers/specs/2026-07-09-osrm-durations-v1_1-design.md`

---

## STANDING RULES (read before executing)

- **NO git commits, ever.** Every task below ends with a **Checkpoint (no commit)** step: run the task's tests green and note the touched files in your working ledger. Never run `git add`/`git commit`.
- **All experiment code changes tracked/restorable.** Before editing a file the E-campaign depends on (`route_costs.py`, `routing_adapter.py`, `compatibility.py`, `alns.py`), confirm a copy exists under `freight_planner/experiments/code_snapshots/` or note the pre-edit state so it can be restored.
- **Flag-off must be bit-identical.** Task B6 is the gate: the solve fingerprint with `USE_OSRM_DURATIONS=False` must equal the pre-v1.1 baseline. If it diverges, STOP and fix before proceeding.
- **Run tests from** `e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics` (the repo root for `pytest`/imports). All `pytest` commands below assume that cwd.

---

## File Structure

**Create:**
- `freight_planner/speed_calibration.py` — Part A: road-class + type classifiers, observed-speed table, per-type factor calibration, CLI/artifacts.
- `tests/freight_planner/test_speed_calibration.py` — Part A unit tests.
- Artifacts written by the script: `freight_planner/data/calibration/speed_by_type_road.csv`, `freight_planner/data/calibration/speed_factors.json`.

**Modify:**
- `freight_planner/config.py` — Part B: `USE_OSRM_DURATIONS`, `FREIGHT_DURATION_FACTOR`, `OSRM_SCREEN_SPEED_KMH`, `duration_factor_for()`.
- `freight_planner/route_costs.py` — Part B: `road_minutes()` + `_min_cache`.
- `freight_planner/routing_adapter.py` — Part B: three call-site swaps in `evaluate_route`.
- `freight_planner/compatibility.py` — Part B: flag-aware screen speed.
- `freight_planner/alns.py` — Part C: `_route_times_from_solution`, `RouteSeedImprovement.route_times`.
- `freight_planner/manifest.py` — Part C: depot-row timing in `build_route_stops`.
- `freight_planner/reports.py` — Part C: thread `route_times`.
- `freight_planner/vehicle_actuals.py` — Part D: `actual_duty_by_vehicle`.
- `freight_planner/viz_app.py` — Part D: duty aggregation + `_build_validation` duty block + render row.

**Test files:** `tests/freight_planner/test_route_costs_road_minutes.py`, `…/test_routing_adapter_osrm.py`, `…/test_compatibility_screen_flag.py`, `…/test_manifest_depot_timing.py`, `…/test_alns_route_times.py`, `…/test_vehicle_actuals.py` (extend), `…/test_viz_app_validation.py` (extend), `…/test_flag_off_fingerprint.py`.

---

# PART A — Speed calibration & per-type validation

### Task A1: Road-class and vehicle-type classifiers

**Files:**
- Create: `freight_planner/speed_calibration.py`
- Test: `tests/freight_planner/test_speed_calibration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/freight_planner/test_speed_calibration.py
from freight_planner.speed_calibration import classify_road, resolve_vehicle_type


def test_classify_road_by_prefix():
    assert classify_road("M11") == "motorway"
    assert classify_road("A505") == "A_road"
    assert classify_road("B1049") == "B_road"
    assert classify_road("Moorfield Road") == "minor_urban"
    assert classify_road("") == "unknown"
    assert classify_road(None) == "unknown"


def test_resolve_vehicle_type():
    assert resolve_vehicle_type("Tractor Unit", "GCW", "Diesel") == "tractor"
    assert resolve_vehicle_type("Lorry", "GVW", "Diesel") == "rigid"
    assert resolve_vehicle_type("Rigid Truck", "GVW", "Diesel") == "rigid"
    assert resolve_vehicle_type("Service Van", "GVW", "Diesel") == "van"
    assert resolve_vehicle_type("Anything", "GVW", "Electric") == "EV"
    # fallback via metric when AssetType is unknown
    assert resolve_vehicle_type("", "GCW", "Diesel") == "tractor"
    assert resolve_vehicle_type("", "GVW", "Diesel") == "rigid"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/freight_planner/test_speed_calibration.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'freight_planner.speed_calibration'`.

- [ ] **Step 3: Write minimal implementation**

```python
# freight_planner/speed_calibration.py
"""v1.1 speed calibration & per-type validation (design 2026-07-09).

Produces (1) the observed by-type / by-road-class speed table (the "validate
speed by type" deliverable) and (2) the per-vehicle-type OSRM duration factors
consumed by config.FREIGHT_DURATION_FACTOR.

Calibration basis is STRUCTURAL: per-journey and per-road-class speeds measured
from the GPS telematics — never a fit to historical daily km/time totals (that
overfits forward/backtest mode and fails on unseen days).
"""
from __future__ import annotations

import re

MPH_TO_KMH = 1.609344


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/freight_planner/test_speed_calibration.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Checkpoint (no commit)** — tests green; note `speed_calibration.py`, `test_speed_calibration.py` created.

---

### Task A2: Observed speed table by (vehicle type × road class)

**Files:**
- Modify: `freight_planner/speed_calibration.py`
- Test: `tests/freight_planner/test_speed_calibration.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/freight_planner/test_speed_calibration.py
import pandas as pd
from freight_planner.speed_calibration import observed_speed_table


def test_observed_speed_table_moving_only_and_units():
    # 3 pings: one parked (ignored), two moving on a motorway at 50 mph
    df = pd.DataFrame({
        "AssetName": ["V1", "V1", "V1"],
        "Ignition": ["TRUE", "TRUE", "FALSE"],
        "GPSSpeed": [50.0, 50.0, 0.0],
        "Location_Road": ["M11", "M11", "M11"],
    })
    type_map = {"V1": "tractor"}
    tbl = observed_speed_table(df, type_map)
    row = tbl[(tbl["vehicle_type"] == "tractor") & (tbl["road_class"] == "motorway")].iloc[0]
    assert row["ping_count"] == 2                      # parked ping excluded
    assert abs(row["mean_kmh"] - 50.0 * 1.609344) < 1e-6   # mph -> km/h
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/freight_planner/test_speed_calibration.py::test_observed_speed_table_moving_only_and_units -q`
Expected: FAIL — `ImportError: cannot import name 'observed_speed_table'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to freight_planner/speed_calibration.py
import pandas as pd

MIN_MOVING_MPH = 2.0


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/freight_planner/test_speed_calibration.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Checkpoint (no commit).**

---

### Task A3: Journey extraction and per-type OSRM factor

**Files:**
- Modify: `freight_planner/speed_calibration.py`
- Test: `tests/freight_planner/test_speed_calibration.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/freight_planner/test_speed_calibration.py
from freight_planner.speed_calibration import build_journeys, per_type_factors


def test_build_journeys_segments_on_ignition():
    df = pd.DataFrame({
        "AssetName": ["V1"] * 5,
        "LocalTime": ["2026-01-01 08:00:00", "2026-01-01 08:30:00", "2026-01-01 09:00:00",
                      "2026-01-01 12:00:00", "2026-01-01 12:30:00"],
        "Ignition": ["TRUE", "TRUE", "FALSE", "TRUE", "TRUE"],
        "Latitude": [52.0, 52.1, 52.2, 52.2, 52.3],
        "Longitude": [0.0, 0.1, 0.2, 0.2, 0.3],
    })
    j = build_journeys(df)
    assert len(j) == 2                       # two ignition-on runs
    first = j.iloc[0]
    assert first["AssetName"] == "V1"
    assert abs(first["observed_h"] - 1.0) < 1e-6   # 08:00 -> 09:00 (ignition-on run incl. its last ping)


def test_per_type_factors_ratio():
    journeys = pd.DataFrame({
        "AssetName": ["V1", "V1"],
        "observed_h": [2.0, 2.0],
        "o_lat": [52.0, 52.0], "o_lon": [0.0, 0.0],
        "d_lat": [52.5, 52.5], "d_lon": [0.5, 0.5],
    })
    type_map = {"V1": "rigid"}
    # stub OSRM car free-flow: every journey predicted at 1.6 h -> factor = 2.0/1.6 = 1.25
    factors = per_type_factors(journeys, type_map, osrm_freeflow_h=lambda a, b, c, d: 1.6)
    assert abs(factors["rigid"] - 1.25) < 1e-6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/freight_planner/test_speed_calibration.py -k "journeys or factors" -q`
Expected: FAIL — `ImportError: cannot import name 'build_journeys'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to freight_planner/speed_calibration.py
def build_journeys(df: pd.DataFrame) -> pd.DataFrame:
    """One row per ignition-on run per vehicle: endpoints + observed duration (h).

    A journey is a maximal run of Ignition-true pings (time-sorted). observed_h is
    the span from the run's first to last ping.
    """
    rows = []
    for name, g in df.groupby("AssetName"):
        g = g.sort_values("LocalTime")
        ign = g["Ignition"].astype(str).str.lower().isin(["true", "1", "1.0"]).to_numpy()
        t = pd.to_datetime(g["LocalTime"], errors="coerce").to_numpy()
        lat = pd.to_numeric(g["Latitude"], errors="coerce").to_numpy()
        lon = pd.to_numeric(g["Longitude"], errors="coerce").to_numpy()
        i = 0
        n = len(ign)
        while i < n:
            if not ign[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and ign[j + 1]:
                j += 1
            if j > i:
                observed_h = (t[j] - t[i]) / pd.Timedelta(hours=1)
                if observed_h > 0:
                    rows.append({"AssetName": str(name), "o_lat": float(lat[i]), "o_lon": float(lon[i]),
                                 "d_lat": float(lat[j]), "d_lon": float(lon[j]),
                                 "observed_h": float(observed_h)})
            i = j + 1
    return pd.DataFrame(rows, columns=["AssetName", "o_lat", "o_lon", "d_lat", "d_lon", "observed_h"])


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/freight_planner/test_speed_calibration.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Checkpoint (no commit).**

---

### Task A4: CLI + artifact emission (`speed_by_type_road.csv`, `speed_factors.json`)

**Files:**
- Modify: `freight_planner/speed_calibration.py`
- Test: `tests/freight_planner/test_speed_calibration.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/freight_planner/test_speed_calibration.py
from freight_planner.speed_calibration import load_type_map


def test_load_type_map():
    vl = pd.DataFrame({
        "AssetName": ["V1", "V2"],
        "AssetType": ["Tractor Unit", "Service Van"],
        "metric": ["GCW", "GVW"],
        "fuel_type": ["Diesel", "Diesel"],
    })
    m = load_type_map(vl)
    assert m == {"V1": "tractor", "V2": "van"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/freight_planner/test_speed_calibration.py::test_load_type_map -q`
Expected: FAIL — `ImportError: cannot import name 'load_type_map'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to freight_planner/speed_calibration.py
import json
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]  # .../BackEnd/logistics
SUPATRAK = _BASE / "data" / "Input" / "supatrak"
OUT_DIR = Path(__file__).resolve().parent / "data" / "calibration"


def load_type_map(vehicle_list: pd.DataFrame) -> dict:
    out = {}
    for r in vehicle_list.itertuples(index=False):
        out[str(getattr(r, "AssetName", ""))] = resolve_vehicle_type(
            getattr(r, "AssetType", ""), getattr(r, "metric", ""), getattr(r, "fuel_type", ""))
    return out


def _osrm_freeflow_h_factory():
    """Real OSRM car free-flow duration: router truck-hours / TRUCK_DURATION_FACTOR."""
    from freight_planner.route_costs import install_osrm_router, get_router
    from simulation.routing import TRUCK_DURATION_FACTOR
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

    journeys = build_journeys(df)
    factors = per_type_factors(journeys, type_map, _osrm_freeflow_h_factory())
    payload = {"factors": factors, "n_journeys": int(len(journeys)),
               "months": list(months), "basis": "per-journey observed/OSRM-freeflow ratio, per type"}
    (OUT_DIR / "speed_factors.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("factors:", factors)
    print("observed table:\n", table.to_string(index=False))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/freight_planner/test_speed_calibration.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5 (manual, requires OSRM server running): produce the artifacts**

Run: `python -m freight_planner.speed_calibration`
Expected: prints per-type factors (roughly tractor ≈ rigid ≈ 1.2–1.3, van < 1.1) and writes `freight_planner/data/calibration/speed_by_type_road.csv` + `speed_factors.json`. If OSRM is down, factors come back empty/partial — record that and rerun when the server is up. Paste the resulting factors into `config.FREIGHT_DURATION_FACTOR` in Task B1.

- [ ] **Step 6: Checkpoint (no commit).**

---

# PART B — OSRM travel-time model (flag-gated, per-type)

### Task B1: Config flag, per-type factors, screen speed

**Files:**
- Modify: `freight_planner/config.py`
- Test: `tests/freight_planner/test_route_costs_road_minutes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/freight_planner/test_route_costs_road_minutes.py
from freight_planner import config


def test_duration_factor_defaults_neutral():
    # default factors reproduce today's OSRM x 1.24 behaviour until calibrated
    assert config.USE_OSRM_DURATIONS is False
    assert config.duration_factor_for("tractor") == config.FREIGHT_DURATION_FACTOR["tractor"]
    # unknown types fall back to the tractor factor, never KeyError
    assert config.duration_factor_for("something_odd") == config.FREIGHT_DURATION_FACTOR["tractor"]
    assert config.OSRM_SCREEN_SPEED_KMH >= 90.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/freight_planner/test_route_costs_road_minutes.py::test_duration_factor_defaults_neutral -q`
Expected: FAIL — `AttributeError: module 'freight_planner.config' has no attribute 'USE_OSRM_DURATIONS'`.

- [ ] **Step 3: Write minimal implementation** (append to `freight_planner/config.py`)

```python
# --- OSRM travel-time model (v1.1, spec 2026-07-09) -------------------------
# When True, the DAILY evaluator times each leg by OSRM road-type duration x a
# per-vehicle-type factor instead of km / AVG_SPEED_KMH. Default OFF so the
# reference config and pending experiments keep the constant-speed model and
# the solve fingerprint stays bit-identical. Product/live deploys set this True.
USE_OSRM_DURATIONS: bool = False

# Per-type multiplier on OSRM CAR free-flow duration. Defaults = TRUCK_DURATION_FACTOR
# (1.24) so flag-on-before-calibration reproduces today's OSRM behaviour; replace
# with the values emitted by freight_planner.speed_calibration once calibrated.
FREIGHT_DURATION_FACTOR: dict = {"tractor": 1.24, "rigid": 1.24, "van": 1.24, "EV": 1.24}

# Generous reach-screen speed used ONLY when USE_OSRM_DURATIONS is on: a permissive
# upper bound so the screen never rejects a job the per-segment OSRM evaluator would
# accept (the evaluator is the real time authority). See spec Part B "screen safety".
OSRM_SCREEN_SPEED_KMH: float = 100.0


def duration_factor_for(vehicle_type: str) -> float:
    return FREIGHT_DURATION_FACTOR.get(str(vehicle_type).lower(), FREIGHT_DURATION_FACTOR["tractor"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/freight_planner/test_route_costs_road_minutes.py::test_duration_factor_defaults_neutral -q`
Expected: PASS.

- [ ] **Step 5: Checkpoint (no commit).**

---

### Task B2: `road_minutes()` in route_costs

**Files:**
- Modify: `freight_planner/route_costs.py`
- Test: `tests/freight_planner/test_route_costs_road_minutes.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/freight_planner/test_route_costs_road_minutes.py
import importlib
from freight_planner import route_costs, config


def teardown_function():
    route_costs.reset_router()
    config.USE_OSRM_DURATIONS = False


def test_road_minutes_flag_off_equals_drive_minutes():
    route_costs.reset_router()
    config.USE_OSRM_DURATIONS = False
    km = route_costs.road_km(52.0, 0.0, 52.5, 0.5)
    assert route_costs.road_minutes(52.0, 0.0, 52.5, 0.5, "tractor") == route_costs.drive_minutes(km)


class _StubOSRM:
    # 0.5 h between the points, regardless of coords
    def distance_km(self, a, b, c, d): return 40.0
    def duration_h(self, a, b, c, d, depart_time=None): return 0.5


def test_road_minutes_flag_on_uses_duration_and_type_factor(monkeypatch):
    config.USE_OSRM_DURATIONS = True
    monkeypatch.setitem(config.FREIGHT_DURATION_FACTOR, "van", 1.0)
    monkeypatch.setattr("simulation.routing.TRUCK_DURATION_FACTOR", 1.24, raising=False)
    route_costs.set_router(_StubOSRM())
    # minutes = duration_h / TRUCK_DURATION_FACTOR * factor[van] * 60 = 0.5/1.24*1.0*60
    assert abs(route_costs.road_minutes(52.0, 0.0, 52.5, 0.5, "van") - (0.5 / 1.24 * 60.0)) < 1e-6


def test_road_minutes_flag_on_falls_back_when_router_has_no_duration():
    config.USE_OSRM_DURATIONS = True

    class _DistOnly:
        def distance_km(self, a, b, c, d): return 40.0
    route_costs.set_router(_DistOnly())
    km = route_costs.road_km(52.0, 0.0, 52.5, 0.5)
    assert route_costs.road_minutes(52.0, 0.0, 52.5, 0.5, "tractor") == route_costs.drive_minutes(km)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/freight_planner/test_route_costs_road_minutes.py -k road_minutes -q`
Expected: FAIL — `AttributeError: module 'freight_planner.route_costs' has no attribute 'road_minutes'`.

- [ ] **Step 3: Write minimal implementation**

In `freight_planner/route_costs.py`, add a duration memo next to `_km_cache` (line ~45) and clear it in `set_router`/`reset_router` (lines ~52 and ~59):

```python
# add near _km_cache
_min_cache: dict[tuple, float] = {}
```

Update `set_router` and `reset_router` to also clear it:

```python
def set_router(router: RoadRouter | None) -> None:
    global _active_router
    _active_router = router
    _km_cache.clear()
    _min_cache.clear()


def reset_router() -> None:
    global _active_router
    _active_router = None
    _km_cache.clear()
    _min_cache.clear()
```

Add `road_minutes` after `drive_minutes` (line ~141):

```python
def road_minutes(a_lat: float, a_lon: float, b_lat: float, b_lon: float, vehicle_type: str) -> float:
    """Drive minutes for one leg.

    With config.USE_OSRM_DURATIONS and an OSRM router installed, use the router's
    truck-adjusted duration (car x TRUCK_DURATION_FACTOR), divide that factor back
    out to recover CAR free-flow, then apply the per-vehicle-type factor. Otherwise
    (default, tests, offline) this is byte-identical to drive_minutes(road_km(...)).
    """
    from freight_planner import config
    router = _active_router
    if config.USE_OSRM_DURATIONS and router is not None and hasattr(router, "duration_h"):
        key = (a_lat, a_lon, b_lat, b_lon, str(vehicle_type).lower())
        cached = _min_cache.get(key)
        if cached is not None:
            return cached
        from simulation.routing import TRUCK_DURATION_FACTOR
        hours = router.duration_h(a_lat, a_lon, b_lat, b_lon)
        minutes = hours / TRUCK_DURATION_FACTOR * config.duration_factor_for(vehicle_type) * 60.0
        _min_cache[key] = minutes
        return minutes
    return drive_minutes(road_km(a_lat, a_lon, b_lat, b_lon))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/freight_planner/test_route_costs_road_minutes.py -q`
Expected: PASS (all).

- [ ] **Step 5: Checkpoint (no commit).**

---

### Task B3: Swap `evaluate_route` to per-segment `road_minutes`

**Files:**
- Modify: `freight_planner/routing_adapter.py` (import line ~31; call sites ~210 and ~274; two-point leg ~206–210)
- Test: `tests/freight_planner/test_routing_adapter_osrm.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/freight_planner/test_routing_adapter_osrm.py
from freight_planner import route_costs, config
from freight_planner import routing_adapter as ra


def teardown_function():
    route_costs.reset_router()
    config.USE_OSRM_DURATIONS = False


def _vehicle():
    # Build the minimal RouteVehicle the evaluator needs; reuse the test helper
    # already used by the routing_adapter suite.
    from tests.freight_planner.helpers_routing import make_route_vehicle  # existing helper
    return make_route_vehicle(start_lat=52.0, start_lon=0.0, home_lat=52.0, home_lon=0.0,
                              start_time="2026-01-05 06:00", shift_end="2026-01-05 20:00",
                              vehicle_type="tractor")


def _job():
    from tests.freight_planner.helpers_routing import make_route_job
    return make_route_job(job_id="J1", lat=52.5, lon=0.5, pallets=2, kg=1000,
                          leg_kind="CUSTOMER_DELIVERY")


class _StubOSRM:
    def distance_km(self, a, b, c, d): return 40.0
    def duration_h(self, a, b, c, d, depart_time=None): return 2.0   # deliberately slow


def test_evaluate_route_uses_osrm_duration_when_flag_on(monkeypatch):
    config.USE_OSRM_DURATIONS = True
    monkeypatch.setitem(config.FREIGHT_DURATION_FACTOR, "tractor", 1.24)
    monkeypatch.setattr("simulation.routing.TRUCK_DURATION_FACTOR", 1.24, raising=False)
    route_costs.set_router(_StubOSRM())
    ev = ra.evaluate_route(_vehicle(), [_job()])
    # 2.0 h out + 2.0 h back = 240 min drive (vs 40km/50*60*2 = 96 min under constant speed)
    assert ev.feasible
    assert abs(ev.total_drive_minutes - 240.0) < 1e-6
```

> If `tests/freight_planner/helpers_routing.py` does not expose `make_route_vehicle`/`make_route_job`, construct the `RouteVehicle`/`RouteJob` dataclasses directly (import from `freight_planner.routing_adapter`) with the same fields the existing `test_routing_adapter*.py` tests use — check one of those tests for the exact constructor.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/freight_planner/test_routing_adapter_osrm.py -q`
Expected: FAIL — `ev.total_drive_minutes == 96.0` (still constant-speed), assertion fails.

- [ ] **Step 3: Write minimal implementation**

In `freight_planner/routing_adapter.py`, extend the import (line ~31):

```python
from freight_planner.route_costs import drive_minutes, road_km, road_minutes, service_minutes, statutory_breaks
```

Replace the forward-leg timing (the two-point vs normal `leg_km`/`dm` block, lines ~206–210):

```python
        if (job.leg_kind in _TWO_POINT_KINDS
                and job.origin_lat is not None and job.origin_lon is not None):
            leg_km = (road_km(prev_lat, prev_lon, job.origin_lat, job.origin_lon)
                      + road_km(job.origin_lat, job.origin_lon, job.lat, job.lon))
            dm = (road_minutes(prev_lat, prev_lon, job.origin_lat, job.origin_lon, vehicle.vehicle_type)
                  + road_minutes(job.origin_lat, job.origin_lon, job.lat, job.lon, vehicle.vehicle_type))
        else:
            leg_km = road_km(prev_lat, prev_lon, job.lat, job.lon)
            dm = road_minutes(prev_lat, prev_lon, job.lat, job.lon, vehicle.vehicle_type)
```

(Delete the old `dm = drive_minutes(leg_km)` line at ~210 — `dm` is now set per-branch above.)

Replace the return-to-depot leg (lines ~273–274):

```python
        back_km = road_km(prev_lat, prev_lon, vehicle.home_lat, vehicle.home_lon)
        back_dm = road_minutes(prev_lat, prev_lon, vehicle.home_lat, vehicle.home_lon, vehicle.vehicle_type)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/freight_planner/test_routing_adapter_osrm.py -q`
Expected: PASS.

- [ ] **Step 5: Run the whole routing_adapter suite (flag defaults off → unchanged)**

Run: `pytest tests/freight_planner/ -k routing_adapter -q`
Expected: PASS (no regressions; flag off ⇒ `road_minutes` == `drive_minutes(road_km)`).

- [ ] **Step 6: Checkpoint (no commit).**

---

### Task B4: Flag-aware compatibility screen

**Files:**
- Modify: `freight_planner/compatibility.py:121`
- Test: `tests/freight_planner/test_compatibility_screen_flag.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/freight_planner/test_compatibility_screen_flag.py
import numpy as np, pandas as pd
from freight_planner import config
from freight_planner.compatibility import _screen_speed_kmh   # tiny helper we add


def teardown_function():
    config.USE_OSRM_DURATIONS = False


def test_screen_speed_switches_with_flag():
    from cambridge.config import AVG_SPEED_KMH
    config.USE_OSRM_DURATIONS = False
    assert _screen_speed_kmh() == AVG_SPEED_KMH
    config.USE_OSRM_DURATIONS = True
    assert _screen_speed_kmh() == config.OSRM_SCREEN_SPEED_KMH
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/freight_planner/test_compatibility_screen_flag.py -q`
Expected: FAIL — `ImportError: cannot import name '_screen_speed_kmh'`.

- [ ] **Step 3: Write minimal implementation**

In `freight_planner/compatibility.py`, add the helper (near the top, after imports):

```python
def _screen_speed_kmh() -> float:
    """Reach-screen speed. Constant AVG_SPEED_KMH normally; a generous bound under
    USE_OSRM_DURATIONS so the screen never rejects a job the per-segment OSRM
    evaluator would accept (spec Part B, screen safety)."""
    from freight_planner import config
    if config.USE_OSRM_DURATIONS:
        return float(config.OSRM_SCREEN_SPEED_KMH)
    return float(AVG_SPEED_KMH)
```

Change line 121 from:

```python
    merged["estimated_drive_minutes"] = (merged["current_to_service_km"] / AVG_SPEED_KMH) * 60.0
```

to:

```python
    merged["estimated_drive_minutes"] = (merged["current_to_service_km"] / _screen_speed_kmh()) * 60.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/freight_planner/test_compatibility_screen_flag.py -q` then `pytest tests/freight_planner/ -k compatibility -q`
Expected: PASS (flag off ⇒ identical to before).

- [ ] **Step 5: Checkpoint (no commit).**

---

### Task B5: Flag-off bit-identical fingerprint gate

**Files:**
- Test: `tests/freight_planner/test_flag_off_fingerprint.py`

- [ ] **Step 1: Write the test** (this is the experiment-safety gate)

```python
# tests/freight_planner/test_flag_off_fingerprint.py
"""With USE_OSRM_DURATIONS off, road_minutes must be byte-identical to the old
drive_minutes(road_km(...)) across a grid of legs and vehicle types — the guarantee
that pending experiments are unaffected. (A full solve-fingerprint run is done
manually in Step 3.)"""
import itertools
from freight_planner import route_costs, config


def test_flag_off_road_minutes_identical_grid():
    route_costs.reset_router()          # offline haversine model, as experiments run reference config
    config.USE_OSRM_DURATIONS = False
    pts = [(52.0, 0.0), (52.5, 0.5), (53.1, -1.2), (51.4, 0.9)]
    for (a, b), (c, d) in itertools.product(pts, pts):
        for vt in ("tractor", "rigid", "van"):
            assert route_costs.road_minutes(a, b, c, d, vt) == route_costs.drive_minutes(route_costs.road_km(a, b, c, d))
```

- [ ] **Step 2: Run it**

Run: `pytest tests/freight_planner/test_flag_off_fingerprint.py -q`
Expected: PASS.

- [ ] **Step 3: Manual solve-fingerprint check (do once, before enabling the flag anywhere)**

Run an illustrative window through the normal seed→ALNS path with `USE_OSRM_DURATIONS=False` and compare the plan fingerprint (selected job set + per-route km) to a pre-v1.1 run of the same window. Use the existing fingerprint/determinism harness the E-campaign uses (`experiments/` fingerprint tooling). Expected: **identical**. If not, STOP — a call site changed behaviour with the flag off; diff `road_minutes` vs `drive_minutes` usage.

- [ ] **Step 4: Checkpoint (no commit).**

---

# PART C — Depot-timing emission (always-on)

### Task C1: `_route_times_from_solution` + `RouteSeedImprovement.route_times`

**Files:**
- Modify: `freight_planner/alns.py` (dataclass ~360; new fn near `_route_totals_from_solution` ~1191; return in `improve_existing_solution` ~1311)
- Test: `tests/freight_planner/test_alns_route_times.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/freight_planner/test_alns_route_times.py
from freight_planner.alns import RouteSeedImprovement


def test_route_seed_improvement_has_route_times():
    imp = RouteSeedImprovement(selected=[], km_before=0, km_after=0,
                               served_before=0, served_after=0, accepted_moves=0)
    assert imp.route_times == {}          # new field, defaults empty
```

> After Step 3 wiring, add a second test that runs `improve_existing_solution` on the smallest fixture already used by `tests/freight_planner/test_alns*.py` and asserts every `ROUTE:*` key in `route_totals` also appears in `route_times` with a `(start_iso, end_iso)` tuple. Copy the fixture setup from an existing alns test.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/freight_planner/test_alns_route_times.py -q`
Expected: FAIL — `TypeError: __init__() got no attribute route_times` / `AttributeError`.

- [ ] **Step 3: Write minimal implementation**

Add the field to `RouteSeedImprovement` (after `route_totals`, line ~367):

```python
    route_totals: dict = field(default_factory=dict)
    route_times: dict = field(default_factory=dict)
```

Add `_route_times_from_solution` next to `_route_totals_from_solution` (~1211). It mirrors it exactly, capturing the clock instead of km:

```python
def _route_times_from_solution(
    solution: dict,
    vehicle_meta: dict[str, VehicleMeta],
    avail_overrides: dict[tuple[str, str], str] | None = None,
) -> dict[str, tuple[str, str]]:
    rt_cache: dict[tuple[str, str], RouteVehicle] = {}
    route_times: dict[str, tuple[str, str]] = {}
    for (vid, day), trips in solution.items():
        trips = _as_trips(trips)
        if not trips or vid not in vehicle_meta:
            continue
        rv = rt_cache.get((vid, day))
        if rv is None:
            rv = _rv_ov(vehicle_meta[vid], day, avail_overrides)
            rt_cache[(vid, day)] = rv
        day_ev = evaluate_day(rv, trips)
        route_id = f"ROUTE:{vid}:{day}"
        route_times[route_id] = (day_ev.day_start, day_ev.day_end)
        for trip_index, trip_ev in enumerate(day_ev.trip_evaluations, start=1):
            route_times[f"{route_id}#T{trip_index}"] = (trip_ev.route_start, trip_ev.route_end)
    return route_times
```

Populate it at the `improve_existing_solution` return (right after the `route_totals = _route_totals_from_solution(...)` line ~1310):

```python
    route_totals = _route_totals_from_solution(improvement.solution, vehicle_meta, avail_overrides)
    route_times = _route_times_from_solution(improvement.solution, vehicle_meta, avail_overrides)
    return RouteSeedImprovement(
        selected=selected,
        km_before=improvement.km_before,
        km_after=improvement.km_after,
        served_before=improvement.served_before,
        served_after=improvement.served_after,
        accepted_moves=improvement.accepted_moves,
        route_totals=route_totals,
        route_times=route_times,
        solution=improvement.solution,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/freight_planner/test_alns_route_times.py -q`
Expected: PASS. Then add & run the second test (keys match `route_totals`).

- [ ] **Step 5: Checkpoint (no commit).**

---

### Task C2: Depot-row timing in `build_route_stops`

**Files:**
- Modify: `freight_planner/manifest.py` (`build_route_stops` signature ~268–275; `depot_start` row ~344; `depot_return` row ~395)
- Test: `tests/freight_planner/test_manifest_depot_timing.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/freight_planner/test_manifest_depot_timing.py
import pandas as pd
from freight_planner.manifest import build_route_stops


def _one_route_selected():
    # Minimal selected_df for a single daily route with one delivery stop.
    return pd.DataFrame([{
        "route_id": "ROUTE:V1:2026-01-05", "trip_index": 1, "vehicle_id": "V1",
        "vehicle_home_depot": "CB22", "service_date": "2026-01-05", "sequence": 1,
        "leg_id": "L1", "order_id": "O1", "leg_kind": "CUSTOMER_DELIVERY",
        "destination_node": "AB1 2CD", "origin_node": "",
        "planned_arrive": "2026-01-05 09:00", "planned_depart": "2026-01-05 09:20",
        "planned_km": 30.0, "break_minutes_before": 0.0,
        "load_pallets_after": 0.0, "load_kg_after": 0.0,
    }])


def test_depot_rows_get_times_from_route_times():
    sel = _one_route_selected()
    veh = pd.DataFrame([{"vehicle_id": "V1", "vehicle_type": "tractor",
                         "current_lat": 52.0, "current_lon": 0.0}])
    route_times = {"ROUTE:V1:2026-01-05#T1": ("2026-01-05 08:30", "2026-01-05 10:00")}
    out = build_route_stops(sel, candidate_df=pd.DataFrame(), compatibility_df=pd.DataFrame(),
                            vehicle_df=veh, route_totals=None, route_times=route_times)
    ds = out[out["stop_type"] == "depot_start"].iloc[0]
    dr = out[out["stop_type"] == "depot_return"].iloc[0]
    assert ds["planned_depart"] == "2026-01-05 08:30"
    assert dr["planned_arrive"] == "2026-01-05 10:00"


def test_depot_rows_blank_when_no_route_times():
    sel = _one_route_selected()
    veh = pd.DataFrame([{"vehicle_id": "V1", "vehicle_type": "tractor",
                         "current_lat": 52.0, "current_lon": 0.0}])
    out = build_route_stops(sel, candidate_df=pd.DataFrame(), compatibility_df=pd.DataFrame(),
                            vehicle_df=veh, route_totals=None, route_times=None)
    assert out[out["stop_type"] == "depot_start"].iloc[0]["planned_depart"] == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/freight_planner/test_manifest_depot_timing.py -q`
Expected: FAIL — `build_route_stops() got an unexpected keyword argument 'route_times'`.

- [ ] **Step 3: Write minimal implementation**

Add the parameter to `build_route_stops` (after `route_totals: dict | None = None,` ~273):

```python
    route_totals: dict | None = None,
    route_times: dict | None = None,
```

Set the depot_start time. Replace the `depot_start` emit (line ~344):

```python
        _rt = (route_times or {}).get(f"{route_id}#T{trip_index}")
        _row(sequence=0, stop_type="depot_start", node=home, lat=vlat, lon=vlon,
             planned_depart=(_rt[0] if _rt else ""))
```

Set the depot_return time. Replace the `depot_return` emit (line ~395):

```python
        _row(sequence=last_seq + 1, stop_type="depot_return", node=home, lat=vlat, lon=vlon,
             service_date=return_date, leg_km=max(0.0, ret_km),
             planned_arrive=(_rt[1] if _rt else ""))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/freight_planner/test_manifest_depot_timing.py -q` then `pytest tests/freight_planner/ -k manifest -q`
Expected: PASS (existing manifest tests unaffected — `route_times` defaults None ⇒ blank, as today).

- [ ] **Step 5: Checkpoint (no commit).**

---

### Task C3: Thread `route_times` through `write_reports`

**Files:**
- Modify: `freight_planner/reports.py` (`write_reports` signature ~45; `build_route_stops` call ~60)
- Modify: the CLI caller of `write_reports` (find with the grep in Step 1)
- Test: covered by C2 + an integration assertion

- [ ] **Step 1: Locate the caller**

Run: `grep -rn "write_reports(" e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics/freight_planner`
Expected: the plan CLI (e.g. `plan_week.py` / the run entry) calls `write_reports(...)` with `route_totals=improvement.route_totals`. Note the file/line.

- [ ] **Step 2: Write the change (signature + pass-through)**

In `reports.py`, add the parameter (after `route_totals: dict | None = None,` ~45):

```python
    route_totals: dict | None = None,
    route_times: dict | None = None,
```

Update the `build_route_stops` call (line ~60):

```python
    build_route_stops(selected_df, candidate_df, compatibility_df, vehicle_df, route_totals,
                       route_times=route_times, tour_return_dates=tour_return_dates).to_csv(
        out_dir / "route_stops.csv", index=False)
```

In the CLI caller located in Step 1, pass the improvement's times alongside its totals:

```python
    write_reports(..., route_totals=improvement.route_totals,
                  route_times=improvement.route_times, ...)
```

- [ ] **Step 3: Run the reports/integration tests**

Run: `pytest tests/freight_planner/ -k "reports or route_stops" -q`
Expected: PASS. If an existing end-to-end test writes `route_stops.csv`, extend it to assert `depot_start.planned_depart` is non-empty for at least one daily route.

- [ ] **Step 4: Checkpoint (no commit).**

---

# PART D — Duty-hours validation axis (always-on)

### Task D1: `actual_duty_by_vehicle` in vehicle_actuals

**Files:**
- Modify: `freight_planner/vehicle_actuals.py`
- Test: `tests/freight_planner/test_vehicle_actuals.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/freight_planner/test_vehicle_actuals.py
import pandas as pd
from datetime import date
from freight_planner import vehicle_actuals


def test_actual_duty_by_vehicle_first_to_last_moving():
    df = pd.DataFrame({
        "AssetName": ["V1", "V1", "V1", "V1"],
        "LocalTime": ["2026-01-05 06:00:00", "2026-01-05 07:00:00",
                      "2026-01-05 15:00:00", "2026-01-05 15:30:00"],
        "GPSSpeed": [0.0, 30.0, 25.0, 0.0],   # mph; moving = >2
        "Latitude": [52.0, 52.1, 52.5, 52.5],
        "Longitude": [0.0, 0.1, 0.5, 0.5],
    })
    duty = vehicle_actuals.actual_duty_by_vehicle(date(2026, 1, 5), loader=lambda d: df)
    # first moving 07:00 -> last moving 15:00 = 8.0 h
    assert abs(duty["V1"] - 8.0) < 1e-6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/freight_planner/test_vehicle_actuals.py::test_actual_duty_by_vehicle_first_to_last_moving -q`
Expected: FAIL — `AttributeError: module 'freight_planner.vehicle_actuals' has no attribute 'actual_duty_by_vehicle'`.

- [ ] **Step 3: Write minimal implementation** (add after `actual_km_by_vehicle`, ~line 79)

```python
_MIN_DUTY_MOVING_MPH = 2.0


def actual_duty_by_vehicle(
    day: date,
    *,
    loader: Callable[[date], pd.DataFrame] = _load_day,
) -> dict[str, float]:
    """On-duty hours per vehicle on ``day`` = span from first to last MOVING ping
    (GPSSpeed > 2 mph). This is the telematics equivalent of the plan's depot
    depart→return span, for the duty-hours validation axis."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/freight_planner/test_vehicle_actuals.py -q`
Expected: PASS.

- [ ] **Step 5: Checkpoint (no commit).**

---

### Task D2: Planned duty + duty block in `_build_validation`

**Files:**
- Modify: `freight_planner/viz_app.py` (`_build_validation` ~200; add a module helper for planned duty)
- Test: `tests/freight_planner/test_viz_app_validation.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/freight_planner/test_viz_app_validation.py
import pandas as pd
from freight_planner.viz_app import _planned_duty_hours


def test_planned_duty_hours_from_depot_rows():
    df = pd.DataFrame([
        {"vehicle_id": "V1", "service_date": "2026-01-05", "stop_type": "depot_start",
         "planned_depart": "2026-01-05 06:30", "planned_arrive": ""},
        {"vehicle_id": "V1", "service_date": "2026-01-05", "stop_type": "depot_return",
         "planned_depart": "", "planned_arrive": "2026-01-05 15:45"},
    ])
    duty = _planned_duty_hours(df)
    assert abs(duty["V1"] - 9.25) < 1e-6      # 06:30 -> 15:45
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/freight_planner/test_viz_app_validation.py::test_planned_duty_hours_from_depot_rows -q`
Expected: FAIL — `ImportError: cannot import name '_planned_duty_hours'`.

- [ ] **Step 3: Write minimal implementation**

Add the helper to `freight_planner/viz_app.py` (near `_build_validation`):

```python
def _planned_duty_hours(df: pd.DataFrame) -> dict:
    """Planned on-duty hours per vehicle over the frame's days: last depot_return
    arrive − first depot_start depart, summed across that vehicle's days."""
    if df.empty or "stop_type" not in df.columns:
        return {}
    starts = df[df["stop_type"] == "depot_start"]
    ends = df[df["stop_type"] == "depot_return"]
    out: dict[str, float] = {}
    key = ["vehicle_id", "service_date"] if "service_date" in df.columns else ["vehicle_id"]
    s = (starts.assign(_t=pd.to_datetime(starts["planned_depart"], errors="coerce"))
         .dropna(subset=["_t"]).groupby(key)["_t"].min())
    e = (ends.assign(_t=pd.to_datetime(ends["planned_arrive"], errors="coerce"))
         .dropna(subset=["_t"]).groupby(key)["_t"].max())
    for k in s.index.intersection(e.index):
        vid = str(k[0] if isinstance(k, tuple) else k)
        hours = (e[k] - s[k]) / pd.Timedelta(hours=1)
        if hours > 0:
            out[vid] = out.get(vid, 0.0) + float(hours)
    return out
```

Extend `_build_validation` to accept actual duty and emit a duty block. Change its signature (line ~200) to add `actuals_duty: dict | None = None`, and add to the returned `fleet` dict (after `"no_telematics": no_tel,` ~238):

```python
    planned_duty = _planned_duty_hours(df)
    ad = actuals_duty or {}
    matched = [v for v in planned_duty if v in ad]
    fleet_planned_duty = round(sum(planned_duty[v] for v in matched), 1) if matched else None
    fleet_actual_duty = round(sum(ad[v] for v in matched), 1) if matched else None
```

and insert into the `"fleet": { ... }` dict:

```python
            "no_telematics": no_tel,
            "planned_duty_h": fleet_planned_duty,
            "actual_duty_h": fleet_actual_duty,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/freight_planner/test_viz_app_validation.py -q`
Expected: PASS (existing validation tests still pass — `actuals_duty` defaults None ⇒ duty fields None).

- [ ] **Step 5: Checkpoint (no commit).**

---

### Task D3: Wire duty aggregation + render the axis

**Files:**
- Modify: `freight_planner/viz_app.py` (actuals loop ~374–390; `_build_validation` call ~549; render block ~888)
- Test: manual viz smoke (render is a JS template)

- [ ] **Step 1: Aggregate actual duty alongside actual km**

In the `with_actuals` loop (after `per_day_actuals[vday] = day_km`, ~381), accumulate duty over window days. First initialise `actuals_duty: dict = {}` next to `actuals: dict = {}` (~356). Then inside the loop, within the `win_lo <= vday <= win_hi` block (after the km accumulation ~390):

```python
            try:
                day_duty = vehicle_actuals.actual_duty_by_vehicle(d)
            except Exception:
                day_duty = {}
            for v, h in day_duty.items():
                actuals_duty[v] = actuals_duty.get(v, 0.0) + float(h)
```

- [ ] **Step 2: Pass it into `_build_validation`** (call site ~549):

```python
        validation = _build_validation(win_df, day_label, actuals, fleet_active, val_metrics,
                                        run_meta, actuals_duty=actuals_duty)
```

- [ ] **Step 3: Render the duty row** — in the validation scorecard JS (after the Vehicle-days `R(...)` line ~888), add:

```javascript
    + R('Vehicle-days <span class=muted>plan/actual</span>', `${f.planned_veh_days} / ${f.actual_veh_days}`)
    + ((f.planned_duty_h!=null||f.actual_duty_h!=null)
        ? R('Duty hours <span class=muted>plan/actual</span>',
            `${f.planned_duty_h!=null?f.planned_duty_h.toLocaleString():'—'} / ${f.actual_duty_h!=null?f.actual_duty_h.toLocaleString():'—'}`)
        : '');
```

(Keep the trailing `;` placement consistent — this replaces the existing `+ R('Vehicle-days ...')` line, folding the duty row in after it.)

- [ ] **Step 4: Manual smoke**

Rebuild a validation viz for an existing window and confirm the scorecard shows "Duty hours plan/actual". Use the standard command (viz regeneration is `viz_app.py`, per standing rule):

Run: `python -m freight_planner.viz_app --validate <plan_dir>` (use a real Jan window plan dir)
Expected: HTML scorecard includes the duty-hours row with two numbers; no JS errors in the page.

- [ ] **Step 5: Checkpoint (no commit).**

---

## Final Verification

- [ ] **Full suite:** `pytest tests/freight_planner/ -q` → all green.
- [ ] **Flag-off gate:** `pytest tests/freight_planner/test_flag_off_fingerprint.py -q` green **and** the manual solve-fingerprint (Task B5 Step 3) identical to pre-v1.1.
- [ ] **Artifacts:** `speed_by_type_road.csv` reproduces the spec §2 table within tolerance; `speed_factors.json` present; `FREIGHT_DURATION_FACTOR` updated from it.
- [ ] **Flag-on smoke:** set `USE_OSRM_DURATIONS=True`, run one Jan + one Feb window; coverage not lower than flag-off; `route_stops.csv` depot rows carry times; duty axis renders.
- [ ] **Restore point noted** for every modified experiment-critical file (`route_costs.py`, `routing_adapter.py`, `compatibility.py`, `alns.py`).

## Self-Review notes (author)

- **Spec coverage:** Part A → A1–A4; Part B → B1–B5; Part C → C1–C3; Part D → D1–D3. Screen-safety (spec §6) → B4. Experiment safety (spec §12) → B5 + standing rules.
- **Type consistency:** `road_minutes(a,b,c,d,vehicle_type)` signature identical across B2/B3/B5; `route_times` keys `f"{route_id}#T{trip_index}"` identical in C1 (producer) and C2 (consumer), matching existing `route_totals` keys; `_planned_duty_hours` / `actual_duty_by_vehicle` return `dict[str,float]` consumed in D2/D3; `duration_factor_for` used in B1/B2.
- **No git commits:** every task ends in a Checkpoint, not a commit (standing rule).
