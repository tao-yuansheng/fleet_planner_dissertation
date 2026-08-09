# Backtest Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `run_backtest.py` — a standalone script that re-runs the dispatcher on a historical date and prints planned vs actual KM, fuel cost, order assignment rate, and on-time delivery rate side-by-side.

**Architecture:** Three new/modified files. `simulation/actuals_loader.py` is a pure data module (`load_actuals`) with no dispatcher dependency. `simulation/report.py` gets two new print functions. `run_backtest.py` wires them together: runs the dispatcher, computes planned fuel-only metrics, loads actuals, prints comparison, writes JSON.

**Tech Stack:** Python, pandas, existing `run_batch` / `load_datasets` / `data_loader` / `profitability_report_merged` infrastructure. No new dependencies.

---

## File Structure

| File | Status | Responsibility |
|------|--------|----------------|
| `simulation/actuals_loader.py` | **CREATE** | GPS-track KM, Jigsaw fuel, Qargo assignment + on-time rate |
| `simulation/report.py` | **MODIFY** | Add `print_backtest` and `print_backtest_summary` |
| `tests/test_actuals_loader.py` | **CREATE** | Unit tests for `load_actuals` and helpers |
| `run_backtest.py` | **CREATE** | Top-level script: dispatcher replay + comparison output |

---

## Context for Implementers

**Working directory:** `E:\BEAT\ZECURE-Phase2-main\BackEnd\logistics`

**Python:** `E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe`

**Run tests with:**
```
cd E:\BEAT\ZECURE-Phase2-main\BackEnd\logistics
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/ -v
```

**Key existing imports you will use:**

```python
# Haversine distance (reuse, don't reimplement):
from profitability_report.profitability_report_merged import _haversine_km, _load_cost_rates, _normalise_type_key, _rate_bundle

# Data loading (datasets):
from data_audit import load_datasets  # returns {'qargo', 'supatrak_telematics', 'supatrak_vehicles', 'jigsaw'}

# Order + vehicle builders (same as used by alns/lns):
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'simulation'))
from data_loader import build_vehicles, build_orders, last_known_positions, load_postcode_cache, save_postcode_cache

# Dispatcher:
from mcts_dispatcher import run_batch
# For ALNS: from alns import run_alns
# For LNS:  from lns import run_lns
# For Greedy: from greedy import run_greedy
```

**`load_datasets` returns DataFrames keyed by:**
- `'qargo'` — Qargo order export (many columns, not just the audit subset)
- `'supatrak_telematics'` — has `Latitude`, `Longitude`, `LocalTime`, `AssetName`
- `'supatrak_vehicles'` — has `AssetName`, `AssetType`, `typical_tonnes`, `CircuitName`
- `'jigsaw'` — has `transactionDateTime`, `companyProductName`, `unitPrice`, `totalCost` (or `quantity`)

**vehicle_cost_rates.json** (at `profitability_report/vehicle_cost_rates.json`):
- Per-type keys: `'tractor unit'`, `'lorry'`, `'rigid truck'`, `'mini truck'`, `'service van'`, `'default'`
- Per-type fields: `fuel_gbp_per_mile`, `driver_mileage_gbp_per_mile`
- `_normalise_type_key(asset_type)` lowercases and trims for lookup
- `_rate_bundle(cost_rates, key)` returns the rate dict, falling back to `'default'`

**RESOURCE_VEHICLE_COLS** (from `data_audit.py`):
```python
['resource_tractor', 'resource_rigid', 'resource_van', 'resource_drawbartrailer', 'resource_trailer']
```

**Qargo SLA columns:**
- `destination_end_timestamp_local` — actual delivery date (parsed to date by profitability report; may be datetime string in raw data)
- `destination_requested_start_timestamp_local` — delivery deadline

**KM_TO_MILES = 0.621371** (defined in `pdp_route.py`; redefine locally in each file that needs it)

---

## Task 1: `simulation/actuals_loader.py` — GPS-track KM + vehicle counts

**Files:**
- Create: `simulation/actuals_loader.py`
- Create: `tests/test_actuals_loader.py`

### Step 1.1: Write failing tests for GPS-track KM

- [ ] Create `tests/test_actuals_loader.py`:

```python
import math
import pandas as pd
import pytest
from simulation.actuals_loader import _gps_track_km


def test_gps_track_km_two_pings_single_vehicle():
    # Two pings 1 degree latitude apart ≈ 111.2 km
    telem_df = pd.DataFrame({
        'LocalTime':  ['2026-01-02 08:00', '2026-01-02 09:00'],
        'Latitude':   [51.0, 52.0],
        'Longitude':  [0.0,  0.0],
        'AssetName':  ['VEH001', 'VEH001'],
    })
    km, vehicles = _gps_track_km(telem_df, '2026-01-02')
    assert abs(km - 111.2) < 1.0
    assert vehicles == ['VEH001']


def test_gps_track_km_two_vehicles():
    telem_df = pd.DataFrame({
        'LocalTime': ['2026-01-02 08:00', '2026-01-02 09:00',
                      '2026-01-02 08:00', '2026-01-02 09:00'],
        'Latitude':  [51.0, 52.0, 51.0, 51.5],
        'Longitude': [0.0,  0.0,  0.0,  0.0],
        'AssetName': ['VEH001', 'VEH001', 'VEH002', 'VEH002'],
    })
    km, vehicles = _gps_track_km(telem_df, '2026-01-02')
    assert len(vehicles) == 2
    assert km > 100.0  # at least one full degree

def test_gps_track_km_empty_df():
    km, vehicles = _gps_track_km(pd.DataFrame(), '2026-01-02')
    assert km == 0.0
    assert vehicles == []


def test_gps_track_km_wrong_date():
    telem_df = pd.DataFrame({
        'LocalTime': ['2026-01-03 08:00', '2026-01-03 09:00'],
        'Latitude':  [51.0, 52.0],
        'Longitude': [0.0,  0.0],
        'AssetName': ['VEH001', 'VEH001'],
    })
    km, vehicles = _gps_track_km(telem_df, '2026-01-02')
    assert km == 0.0
    assert vehicles == []


def test_gps_track_km_single_ping_no_distance():
    # One ping per vehicle → no legs → zero KM but vehicle counted as active
    telem_df = pd.DataFrame({
        'LocalTime': ['2026-01-02 08:00'],
        'Latitude':  [51.0],
        'Longitude': [0.0],
        'AssetName': ['VEH001'],
    })
    km, vehicles = _gps_track_km(telem_df, '2026-01-02')
    assert km == 0.0
    assert 'VEH001' in vehicles
```

- [ ] Run to verify they fail:
```
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/test_actuals_loader.py -v
```
Expected: ImportError (`cannot import name '_gps_track_km'`)

### Step 1.2: Implement `_gps_track_km` in `simulation/actuals_loader.py`

- [ ] Create `simulation/actuals_loader.py`:

```python
"""Ground-truth metric extraction for backtest validation.

Extracts actual KM, fuel spend, and order SLA metrics from Supatrak telematics,
Jigsaw fuel cards, and Qargo order data for a given date.
"""
import math
import pandas as pd

# Qargo columns that hold the vehicle(s) assigned to an order (5 resource fields).
RESOURCE_VEHICLE_COLS = [
    'resource_tractor', 'resource_rigid', 'resource_van',
    'resource_drawbartrailer', 'resource_trailer',
]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _gps_track_km(telem_df: pd.DataFrame, date_str: str) -> tuple[float, list[str]]:
    """Sum consecutive Haversine distances per vehicle for date_str.

    Returns (total_fleet_km, list_of_active_asset_names).

    Trade-off: Haversine between successive pings undercounts curves (ping interval
    ~2-5 min; straight-line between pings misses bends). This is consistent and
    reproducible — the same method used in calibration elsewhere in this codebase.
    NOTE: OSRM is implemented in routing.py for route planning (via /table), but
    GPS track map-matching would require OSRM's /match endpoint, which is not
    currently configured. Haversine-between-pings is therefore the correct choice
    until /match is available.
    """
    if telem_df.empty or 'LocalTime' not in telem_df.columns:
        return 0.0, []

    df = telem_df.copy()
    df['_time'] = pd.to_datetime(df['LocalTime'], errors='coerce')
    df = df.dropna(subset=['_time', 'Latitude', 'Longitude', 'AssetName'])
    df = df[df['_time'].dt.date.astype(str) == date_str]

    if df.empty:
        return 0.0, []

    total_km = 0.0
    active_vehicles: list[str] = []
    for asset_name, grp in df.groupby('AssetName'):
        grp = grp.sort_values('_time')
        lats = grp['Latitude'].tolist()
        lons = grp['Longitude'].tolist()
        if len(lats) >= 2:
            total_km += sum(
                _haversine_km(lats[i], lons[i], lats[i + 1], lons[i + 1])
                for i in range(len(lats) - 1)
            )
        active_vehicles.append(str(asset_name))

    return total_km, active_vehicles
```

- [ ] Run tests to verify they pass:
```
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/test_actuals_loader.py -v
```
Expected: 5 PASS

---

## Task 2: `simulation/actuals_loader.py` — Jigsaw fuel + Qargo metrics + `load_actuals`

**Files:**
- Modify: `simulation/actuals_loader.py`
- Modify: `tests/test_actuals_loader.py`

### Step 2.1: Write failing tests for Jigsaw fuel, Qargo metrics, and `load_actuals`

- [ ] Append to `tests/test_actuals_loader.py`:

```python
from simulation.actuals_loader import _jigsaw_fuel_gbp, _qargo_actuals, load_actuals


# ── Jigsaw fuel ──────────────────────────────────────────────────────────────

def test_jigsaw_fuel_diesel_only():
    df = pd.DataFrame({
        'transactionDateTime': ['2026-01-02 10:00', '2026-01-02 11:00', '2026-01-02 12:00'],
        'companyProductName':  ['Diesel', 'Adblue', 'Diesel'],
        'totalCost':           [100.0,    20.0,      50.0],
        'unitPrice':           [104.3,    80.0,      104.3],
    })
    assert _jigsaw_fuel_gbp(df, '2026-01-02') == 150.0


def test_jigsaw_fuel_zero_price_excluded():
    df = pd.DataFrame({
        'transactionDateTime': ['2026-01-02 10:00', '2026-01-02 11:00'],
        'companyProductName':  ['Diesel', 'Diesel'],
        'totalCost':           [100.0,    50.0],
        'unitPrice':           [104.3,    0.0],
    })
    assert _jigsaw_fuel_gbp(df, '2026-01-02') == 100.0


def test_jigsaw_fuel_wrong_date():
    df = pd.DataFrame({
        'transactionDateTime': ['2026-01-03 10:00'],
        'companyProductName':  ['Diesel'],
        'totalCost':           [100.0],
        'unitPrice':           [104.3],
    })
    assert _jigsaw_fuel_gbp(df, '2026-01-02') == 0.0


def test_jigsaw_fuel_empty_df():
    assert _jigsaw_fuel_gbp(pd.DataFrame(), '2026-01-02') == 0.0


# ── Qargo actuals ─────────────────────────────────────────────────────────────

def test_qargo_actuals_assignment_rate():
    orders = {'ORD001': {}, 'ORD002': {}, 'ORD003': {}}
    qargo_df = pd.DataFrame({
        'order_id':           ['ORD001', 'ORD002', 'ORD003'],
        'resource_tractor':   ['AB12CDE', None,     None],
        'resource_rigid':     [None,      'XY99ZZZ', None],
        'resource_van':       [None,      None,      None],
        'resource_drawbartrailer': [None,  None,     None],
        'resource_trailer':   [None,      None,      None],
    })
    result = _qargo_actuals(qargo_df, orders)
    assert result['orders_assigned_actual'] == 2
    assert abs(result['assignment_rate_actual'] - 2/3) < 0.001


def test_qargo_actuals_on_time():
    orders = {'ORD001': {}, 'ORD002': {}}
    qargo_df = pd.DataFrame({
        'order_id':           ['ORD001', 'ORD002'],
        'resource_tractor':   ['AB12CDE', 'XY99ZZZ'],
        'resource_rigid':     [None,      None],
        'resource_van':       [None,      None],
        'resource_drawbartrailer': [None, None],
        'resource_trailer':   [None,      None],
        # ORD001 delivered on deadline day → on time
        # ORD002 delivered 1 day late
        'destination_end_timestamp_local':          ['2026-01-02', '2026-01-04'],
        'destination_requested_start_timestamp_local': ['2026-01-02', '2026-01-03'],
    })
    result = _qargo_actuals(qargo_df, orders)
    assert result['orders_on_time_actual'] == 1
    assert abs(result['on_time_rate_actual'] - 0.5) < 0.001


def test_qargo_actuals_empty_orders():
    result = _qargo_actuals(pd.DataFrame(), {})
    assert result['orders_assigned_actual'] == 0
    assert result['on_time_rate_actual'] == 0.0


# ── load_actuals integration ──────────────────────────────────────────────────

def test_load_actuals_returns_all_keys():
    telem_df = pd.DataFrame({
        'LocalTime': ['2026-01-02 08:00', '2026-01-02 09:00'],
        'Latitude':  [51.0, 52.0],
        'Longitude': [0.0, 0.0],
        'AssetName': ['VEH001', 'VEH001'],
    })
    vehicles_df = pd.DataFrame({
        'AssetName': ['VEH001'],
        'AssetType': ['Tractor Unit'],
    })
    jigsaw_df = pd.DataFrame({
        'transactionDateTime': ['2026-01-02 10:00'],
        'companyProductName':  ['Diesel'],
        'totalCost':           [200.0],
        'unitPrice':           [104.3],
    })
    orders = {'ORD001': {}}
    qargo_df = pd.DataFrame({
        'order_id':            ['ORD001'],
        'resource_tractor':    ['VEH001'],
        'resource_rigid':      [None],
        'resource_van':        [None],
        'resource_drawbartrailer': [None],
        'resource_trailer':    [None],
        'destination_end_timestamp_local':             ['2026-01-02'],
        'destination_requested_start_timestamp_local': ['2026-01-02'],
    })
    result = load_actuals('2026-01-02', telem_df, vehicles_df, jigsaw_df, qargo_df, orders)
    expected_keys = {
        'active_vehicles_total', 'active_artics', 'active_rigids',
        'actual_km', 'actual_fuel_gbp',
        'orders_assigned_actual', 'assignment_rate_actual',
        'orders_on_time_actual', 'on_time_rate_actual',
    }
    assert expected_keys == set(result.keys())
    assert result['active_artics'] == 1
    assert result['actual_fuel_gbp'] == 200.0
    assert result['orders_assigned_actual'] == 1
    assert result['on_time_rate_actual'] == 1.0
```

- [ ] Run to verify they fail:
```
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/test_actuals_loader.py -v
```
Expected: ImportError for `_jigsaw_fuel_gbp`, `_qargo_actuals`, `load_actuals`

### Step 2.2: Implement `_jigsaw_fuel_gbp`, `_qargo_actuals`, and `load_actuals`

- [ ] Append to `simulation/actuals_loader.py`:

```python
def _jigsaw_fuel_gbp(jigsaw_df: pd.DataFrame, date_str: str) -> float:
    """Sum diesel-only fuel spend from Jigsaw for the target date.

    Trade-off: Jigsaw records fleet-wide diesel spend, not per-order or
    per-vehicle (transaction is at the pump). Adblue and zero-price rows
    are excluded — same filter used in data_audit.py for fuel price calibration.
    Cost is taken from 'totalCost' column if present; falls back to
    unitPrice (pence) × quantity (litres) / 100 → GBP.
    """
    if jigsaw_df.empty or 'transactionDateTime' not in jigsaw_df.columns:
        return 0.0

    df = jigsaw_df.copy()
    df['_dt'] = pd.to_datetime(df['transactionDateTime'], errors='coerce')
    df = df[df['_dt'].dt.date.astype(str) == date_str]

    if 'companyProductName' in df.columns:
        df = df[df['companyProductName'] == 'Diesel']

    unit_price = pd.to_numeric(df.get('unitPrice', pd.Series(dtype=float)), errors='coerce')
    df = df[unit_price > 0]

    if df.empty:
        return 0.0

    if 'totalCost' in df.columns:
        total = pd.to_numeric(df['totalCost'], errors='coerce').dropna().sum()
        return round(float(total), 2)

    if 'quantity' in df.columns:
        qty = pd.to_numeric(df['quantity'], errors='coerce').fillna(0.0)
        price = pd.to_numeric(df['unitPrice'], errors='coerce').fillna(0.0)
        return round(float((price * qty / 100.0).sum()), 2)

    return 0.0


def _qargo_actuals(qargo_df: pd.DataFrame, orders: dict) -> dict:
    """Extract assignment rate and on-time rate for the orders in the dispatcher's input set.

    Assignment: any of the 5 resource columns is non-null → the order was carried by a vehicle.
    On-time: destination_end_timestamp_local <= destination_requested_start_timestamp_local
    at date granularity (Qargo exports the actual delivery as a date, not datetime —
    so hour-level SLA is not computable; day-level is).

    on_time_rate denominator is orders_assigned_actual (unassigned orders excluded —
    we can't say they were late if they were never dispatched in the first place).
    """
    order_ids = set(orders.keys())
    empty = {
        'orders_assigned_actual': 0,
        'assignment_rate_actual': 0.0,
        'orders_on_time_actual': 0,
        'on_time_rate_actual': 0.0,
    }
    if qargo_df.empty or not order_ids:
        return empty

    df = qargo_df[qargo_df['order_id'].astype(str).isin(order_ids)].copy()
    if df.empty:
        return empty

    present_cols = [c for c in RESOURCE_VEHICLE_COLS if c in df.columns]
    if present_cols:
        assigned_mask = df[present_cols].notna().any(axis=1)
    else:
        assigned_mask = pd.Series(False, index=df.index)

    orders_assigned = int(assigned_mask.sum())
    assignment_rate = orders_assigned / len(order_ids) if order_ids else 0.0

    on_time = 0
    actual_col   = 'destination_end_timestamp_local'
    deadline_col = 'destination_requested_start_timestamp_local'
    if actual_col in df.columns and deadline_col in df.columns:
        df_assigned  = df[assigned_mask].copy()
        actual_dt   = pd.to_datetime(df_assigned[actual_col],   errors='coerce')
        deadline_dt = pd.to_datetime(df_assigned[deadline_col], errors='coerce')
        on_time = int((actual_dt.notna() & deadline_dt.notna() & (actual_dt <= deadline_dt)).sum())

    on_time_rate = on_time / orders_assigned if orders_assigned else 0.0

    return {
        'orders_assigned_actual': orders_assigned,
        'assignment_rate_actual': round(assignment_rate, 3),
        'orders_on_time_actual':  on_time,
        'on_time_rate_actual':    round(on_time_rate, 3),
    }


def load_actuals(
    date_str: str,
    telem_df: pd.DataFrame,
    vehicles_df: pd.DataFrame,
    jigsaw_df: pd.DataFrame,
    qargo_df: pd.DataFrame,
    orders: dict,
) -> dict:
    """Extract ground-truth metrics for a single date.

    Parameters
    ----------
    date_str     : 'YYYY-MM-DD'
    telem_df     : Supatrak telematics (Latitude, Longitude, LocalTime, AssetName)
    vehicles_df  : Supatrak vehicle list (AssetName, AssetType) — for artic/rigid split
    jigsaw_df    : Jigsaw fuel transactions
    qargo_df     : Qargo order export (all columns, not the audit subset)
    orders       : {order_id: order_dict} from the dispatcher's input for this window

    Returns a flat dict with keys:
        active_vehicles_total, active_artics, active_rigids,
        actual_km, actual_fuel_gbp,
        orders_assigned_actual, assignment_rate_actual,
        orders_on_time_actual, on_time_rate_actual
    """
    actual_km, active_names = _gps_track_km(telem_df, date_str)

    active_artics = 0
    active_rigids = 0
    if (not vehicles_df.empty
            and 'AssetName' in vehicles_df.columns
            and 'AssetType' in vehicles_df.columns):
        veh_types = dict(zip(
            vehicles_df['AssetName'].astype(str),
            vehicles_df['AssetType'].astype(str),
        ))
        for name in active_names:
            if veh_types.get(name) == 'Tractor Unit':
                active_artics += 1
            else:
                active_rigids += 1

    fuel = _jigsaw_fuel_gbp(jigsaw_df, date_str)
    qargo_stats = _qargo_actuals(qargo_df, orders)

    if not active_names:
        print(f"  WARNING: no Supatrak pings for {date_str} — actual_km = 0")
    if fuel == 0.0:
        print(f"  WARNING: no Jigsaw diesel transactions for {date_str} — actual_fuel_gbp = 0")

    return {
        'active_vehicles_total': len(active_names),
        'active_artics':         active_artics,
        'active_rigids':         active_rigids,
        'actual_km':             round(actual_km, 1),
        'actual_fuel_gbp':       round(fuel, 2),
        **qargo_stats,
    }
```

- [ ] Run all tests to verify they pass:
```
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/test_actuals_loader.py -v
```
Expected: all 14 tests PASS

- [ ] Run full regression:
```
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/ -v
```
Expected: all existing tests still PASS

---

## Task 3: `simulation/report.py` — `print_backtest` and `print_backtest_summary`

**Files:**
- Modify: `simulation/report.py`
- Modify: `tests/test_actuals_loader.py` (add print function smoke tests)

### Step 3.1: Write failing tests for print functions

- [ ] Append to `tests/test_actuals_loader.py`:

```python
from simulation.report import print_backtest, print_backtest_summary


def _make_planned():
    return {
        'vehicles_total': 6, 'vehicles_artic': 4, 'vehicles_rigid': 2,
        'distance_km': 6402.4,
        'fuel_gbp': 2681.30, 'driver_allowance_gbp': 2862.10,
        'orders_in_window': 163, 'orders_assigned': 154,
        'assignment_rate': 0.945, 'on_time_rate': 1.0,
    }


def _make_actual():
    return {
        'active_vehicles_total': 7, 'active_artics': 3, 'active_rigids': 4,
        'actual_km': 5891.2, 'actual_fuel_gbp': 2540.40,
        'orders_assigned_actual': 142, 'assignment_rate_actual': 0.871,
        'orders_on_time_actual': 98, 'on_time_rate_actual': 0.690,
    }


def test_print_backtest_runs_without_error(capsys):
    print_backtest('2026-01-02', _make_planned(), _make_actual(), 'ALNS, 120s')
    out = capsys.readouterr().out
    assert 'BACKTEST' in out
    assert 'PLANNED' in out
    assert 'ACTUAL' in out
    assert 'Fuel delta' in out
    assert 'On-time delta' in out


def test_print_backtest_summary_runs_without_error(capsys):
    day = {'planned': _make_planned(), 'actual': _make_actual()}
    print_backtest_summary([day, day], budget=120, algorithm='ALNS')
    out = capsys.readouterr().out
    assert 'BACKTEST SUMMARY' in out
    assert '2 days' in out
```

- [ ] Run to verify they fail:
```
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/test_actuals_loader.py::test_print_backtest_runs_without_error tests/test_actuals_loader.py::test_print_backtest_summary_runs_without_error -v
```
Expected: ImportError for `print_backtest`, `print_backtest_summary`

### Step 3.2: Implement `print_backtest` and `print_backtest_summary` in `simulation/report.py`

- [ ] Append to `simulation/report.py`:

```python
def print_backtest(date_str: str, planned: dict, actual: dict, flags: str = '') -> None:
    """Print a single-date planned-vs-actual comparison block.

    planned keys: vehicles_total, vehicles_artic, vehicles_rigid, distance_km,
                  fuel_gbp, driver_allowance_gbp, orders_in_window, orders_assigned,
                  assignment_rate, on_time_rate
    actual keys:  active_vehicles_total, active_artics, active_rigids, actual_km,
                  actual_fuel_gbp, orders_assigned_actual, assignment_rate_actual,
                  orders_on_time_actual, on_time_rate_actual
    """
    W = 62
    header = f"  BACKTEST  {date_str}"
    if flags:
        header += f"   ({flags})"
    print(f"\n{'=' * W}")
    print(header)
    print(f"{'=' * W}")
    print(f"  {'':34}  {'PLANNED':>10}  {'ACTUAL':>10}")
    print(f"  {'-' * (W - 2)}")

    def _row(label, pv, av):
        p = f"{pv:>10}" if pv is not None else f"{'n/a':>10}"
        a = f"{av:>10}" if av is not None else f"{'n/a':>10}"
        print(f"  {label:<34}  {p}  {a}")

    _row('Vehicles used  (artic)',
         planned.get('vehicles_artic'), actual.get('active_artics'))
    _row('Vehicles used  (rigid)',
         planned.get('vehicles_rigid'), actual.get('active_rigids'))
    _row('Vehicles used  (total)',
         planned.get('vehicles_total'), actual.get('active_vehicles_total'))
    _row('Total distance KM',
         f"{planned.get('distance_km', 0.0):,.1f}",
         f"{actual.get('actual_km', 0.0):,.1f}")
    _row('Fuel cost GBP',
         f"{planned.get('fuel_gbp', 0.0):,.2f}",
         f"{actual.get('actual_fuel_gbp', 0.0):,.2f}")
    _row('Driver allowance GBP',
         f"{planned.get('driver_allowance_gbp', 0.0):,.2f}",
         None)
    _row('Orders in window',
         planned.get('orders_in_window'), planned.get('orders_in_window'))
    _row('Orders assigned',
         planned.get('orders_assigned'), actual.get('orders_assigned_actual'))
    _row('Assignment rate',
         f"{planned.get('assignment_rate', 0.0) * 100:.1f}%",
         f"{actual.get('assignment_rate_actual', 0.0) * 100:.1f}%")
    _row('On-time deliveries',
         planned.get('orders_assigned'), actual.get('orders_on_time_actual'))
    _row('On-time rate',
         f"{planned.get('on_time_rate', 1.0) * 100:.1f}%",
         f"{actual.get('on_time_rate_actual', 0.0) * 100:.1f}%")

    print(f"  {'-' * (W - 2)}")

    p_fuel = planned.get('fuel_gbp', 0.0)
    a_fuel = actual.get('actual_fuel_gbp', 0.0)
    if a_fuel > 0:
        fuel_delta = (p_fuel - a_fuel) / a_fuel * 100
        print(f"  Fuel delta (planned vs actual):    {fuel_delta:+.1f}%")
    else:
        print(f"  Fuel delta (planned vs actual):    n/a (no Jigsaw data)")

    p_ot = planned.get('on_time_rate', 1.0)
    a_ot = actual.get('on_time_rate_actual', 0.0)
    print(f"  On-time delta (planned vs actual): {(p_ot - a_ot) * 100:+.1f}pp")

    print(f"  {'-' * (W - 2)}")
    print(f"  [1] Planned dist = closed-loop OSRM road distance (km).")
    print(f"      Actual  dist = GPS-track Haversine sum (undercounts curves).")
    print(f"  [2] Planned fuel = fuel_gbp_per_mile[type] x miles (vehicle_cost_rates.json).")
    print(f"      Actual  fuel = Jigsaw diesel spend (excl. Adblue + zero-price rows).")
    print(f"  [3] Planned on-time = 100% by construction (dispatcher is feasibility-constrained).")
    print(f"      Actual  on-time = dest_end_timestamp <= deadline (date-level only).")
    print(f"{'=' * W}\n")


def print_backtest_summary(all_results: list, budget: int, algorithm: str) -> None:
    """Print aggregate summary across multiple backtest days.

    all_results: list of {'planned': dict, 'actual': dict} from each day.
    """
    days = len(all_results)
    if not days:
        return

    p_fuel      = sum(r['planned']['fuel_gbp']              for r in all_results)
    a_fuel      = sum(r['actual']['actual_fuel_gbp']         for r in all_results)
    p_km        = sum(r['planned']['distance_km']            for r in all_results)
    a_km        = sum(r['actual']['actual_km']               for r in all_results)
    p_assigned  = sum(r['planned']['orders_assigned']        for r in all_results)
    a_assigned  = sum(r['actual']['orders_assigned_actual']  for r in all_results)
    a_on_time   = sum(r['actual']['orders_on_time_actual']   for r in all_results)
    p_window    = sum(r['planned']['orders_in_window']       for r in all_results)

    fuel_delta  = (p_fuel - a_fuel) / a_fuel * 100 if a_fuel > 0 else None
    a_ot_rate   = a_on_time / a_assigned if a_assigned > 0 else 0.0
    p_rate      = p_assigned / p_window * 100 if p_window > 0 else 0.0
    a_rate      = a_assigned / p_window * 100 if p_window > 0 else 0.0

    W = 62
    print(f"\n{'=' * W}")
    print(f"  BACKTEST SUMMARY  ({days} days, {algorithm}, budget {budget}s)")
    print(f"{'=' * W}")
    print(f"  {'':34}  {'PLANNED':>10}  {'ACTUAL':>10}")
    print(f"  {'-' * (W - 2)}")
    print(f"  {'Total distance KM':<34}  {p_km:>10,.1f}  {a_km:>10,.1f}")
    print(f"  {'Total fuel GBP':<34}  {p_fuel:>10,.2f}  {a_fuel:>10,.2f}")
    print(f"  {'Orders in windows (total)':<34}  {p_window:>10}  {'n/a':>10}")
    print(f"  {'Orders assigned':<34}  {p_assigned:>10}  {a_assigned:>10}")
    print(f"  {'Assignment rate':<34}  {p_rate:>9.1f}%  {a_rate:>9.1f}%")
    print(f"  {'On-time deliveries (actual)':<34}  {'n/a':>10}  {a_on_time:>10}")
    print(f"  {'On-time rate':<34}  {'100.0%':>10}  {a_ot_rate * 100:>9.1f}%")
    print(f"  {'-' * (W - 2)}")
    if fuel_delta is not None:
        print(f"  Fuel delta (planned vs actual):    {fuel_delta:+.1f}%")
    print(f"{'=' * W}\n")
```

- [ ] Run tests to verify they pass:
```
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/test_actuals_loader.py -v
```
Expected: all 16 tests PASS

> **Fix needed:** The `_row` call for 'Orders in window' has a messy expression. Replace that line with:
> ```python
> _row('Orders in window', planned.get('orders_in_window'), planned.get('orders_in_window'))
> ```
> (actual has the same order count in the window — the window is fixed by the dispatcher input)

- [ ] Run full regression:
```
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/ -v
```
Expected: all tests PASS

> **Note:** The `print_backtest` implementation above already has the correct `_row` call for 'Orders in window' — no further fix needed.

---

## Task 4: `run_backtest.py` — top-level script

**Files:**
- Create: `run_backtest.py`

### Step 4.1: Write the script

- [ ] Create `run_backtest.py`:

```python
"""
Backtest validator for the ZEEFLEET dispatcher.

Re-runs the dispatcher on a historical date's Qargo orders and compares the
planned output (KM, fuel cost, assignment rate) against ground truth from
Supatrak telematics (actual KM), Jigsaw fuel cards (actual diesel spend),
and Qargo resource fields (actual assignment + on-time rate).

Usage:
    python run_backtest.py --date 2026-01-02
    python run_backtest.py --date 2026-01-02 --alns --budget 60
    python run_backtest.py --date 2026-01-02 --days 5 --alns --budget 60
    python run_backtest.py --date 2026-01-02 --routing osrm --alns --budget 120
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from data_audit import load_datasets

BASE_DIR   = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / 'data' / 'Output'
KM_TO_MILES = 0.621371

RATES_JSON_PATH = str(BASE_DIR / 'profitability_report' / 'vehicle_cost_rates.json')

sys.path.insert(0, str(BASE_DIR / 'simulation'))


def _first_date_in_qargo(qargo_df: pd.DataFrame) -> str:
    ts = pd.to_datetime(qargo_df['origin_requested_start_timestamp_local'], errors='coerce').dropna()
    if ts.empty:
        raise ValueError("No valid timestamps in Qargo data")
    return ts.min().date().isoformat()


def _filter_orders_for_date(qargo_df: pd.DataFrame, date_str: str) -> pd.DataFrame:
    """Return all Qargo rows whose origin timestamp falls on date_str (full day)."""
    ts_col = 'origin_requested_start_timestamp_local'
    qargo_df[ts_col] = pd.to_datetime(qargo_df[ts_col], errors='coerce')
    return qargo_df[qargo_df[ts_col].dt.date.astype(str) == date_str].copy()


def _planned_metrics(result: dict, vehicles: list, cost_rates: dict) -> dict:
    """Extract planned-side metrics from a dispatcher result.

    Computes fuel-only planned cost (fuel_gbp_per_mile x miles per vehicle type)
    separately from driver mileage allowance, so fuel_gbp is directly comparable
    to Jigsaw actual diesel spend.

    Note: on_time_rate is 1.0 by construction — the dispatcher only places orders
    it deems feasible within their deadline (feasible_deadlines check in pdp_route.py).
    """
    from profitability_report.profitability_report_merged import (
        _load_cost_rates, _normalise_type_key, _rate_bundle,
    )

    routes      = result.get('routes', {})
    assignments = result.get('assignments', [])
    meta        = result.get('meta', {})

    veh_map = {v['vehicle_id']: v for v in vehicles}

    vehicles_artic = 0
    vehicles_rigid = 0
    fuel_gbp = 0.0
    driver_allowance_gbp = 0.0

    for vid, route in routes.items():
        asset_type = veh_map.get(vid, {}).get('asset_type', 'Lorry')
        type_key   = _normalise_type_key(asset_type)
        rates      = _rate_bundle(cost_rates, type_key)
        km         = route.get('total_distance_km', 0.0)
        miles      = km * KM_TO_MILES
        fuel_gbp             += rates['fuel_gbp_per_mile']            * miles
        driver_allowance_gbp += rates['driver_mileage_gbp_per_mile']  * miles
        if asset_type == 'Tractor Unit':
            vehicles_artic += 1
        else:
            vehicles_rigid += 1

    total_km      = sum(r.get('total_distance_km', 0.0) for r in routes.values())
    n_assigned    = len(assignments)
    n_unassigned  = meta.get('orders_unassigned', 0)
    n_in_window   = n_assigned + n_unassigned

    return {
        'vehicles_total':        len(routes),
        'vehicles_artic':        vehicles_artic,
        'vehicles_rigid':        vehicles_rigid,
        'distance_km':           round(total_km, 1),
        'fuel_gbp':              round(fuel_gbp, 2),
        'driver_allowance_gbp':  round(driver_allowance_gbp, 2),
        'orders_in_window':      n_in_window,
        'orders_assigned':       n_assigned,
        'assignment_rate':       round(n_assigned / n_in_window, 3) if n_in_window else 0.0,
        'on_time_rate':          1.0,
    }


def _run_one_date(date_str: str, qargo_df: pd.DataFrame, telem_df: pd.DataFrame,
                  vehicles_df: pd.DataFrame, jigsaw_df: pd.DataFrame,
                  args, cost_rates: dict) -> dict | None:
    """Run dispatcher + load actuals for one date. Returns {'planned': ..., 'actual': ...} or None."""
    from data_loader import (build_vehicles, build_orders,
                             last_known_positions, load_postcode_cache, save_postcode_cache)
    # simulation/ is already on sys.path (inserted at module level); import directly
    from actuals_loader import load_actuals
    from report import print_backtest

    day_df = _filter_orders_for_date(qargo_df.copy(), date_str)
    if day_df.empty:
        print(f"  No Qargo orders found for {date_str} — skipping")
        return None

    print(f"\n  Date: {date_str}  ({len(day_df)} orders in window)")

    # Geocode and build order dicts
    cache = load_postcode_cache()
    orders_list = build_orders(day_df, cache)
    save_postcode_cache(cache)
    if not orders_list:
        print(f"  No dispatchable orders after geocoding — skipping {date_str}")
        return None

    # Build vehicles list
    last_pos   = last_known_positions(telem_df)
    vehicles   = build_vehicles(vehicles_df, last_pos)
    if not vehicles:
        print(f"  No vehicles available — skipping {date_str}")
        return None

    orders_dict = {o['order_id']: o for o in orders_list}

    # Install OSRM router if requested
    if args.routing == 'osrm':
        try:
            from routing import install_osrm_router
            install_osrm_router(orders_list, vehicles, osrm_url=args.osrm_url)
            print(f"  Routing: OSRM ({args.osrm_url})")
        except Exception as exc:
            print(f"  Routing: OSRM unavailable ({exc}) — falling back to Haversine")
    else:
        print(f"  Routing: Haversine")

    # Run dispatcher
    start_time = datetime.strptime(f"{date_str} 00:00", '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc)
    batch_input = {
        'orders':                orders_list,
        'vehicles':              vehicles,
        'committed_assignments': [],
        'config': {
            'time_budget_seconds': args.budget,
            'horizon_hours':       24,
            'exploration_constant': 1.414,
            'sp_ucb_d': 0.1,
        },
    }

    if args.alns:
        from alns import run_alns
        print(f"  Running ALNS ({args.budget}s)...")
        result = run_alns(orders_list, vehicles, time_budget=args.budget)
        algo = 'ALNS'
    elif args.lns:
        from lns import run_lns
        print(f"  Running LNS ({args.budget}s)...")
        result = run_lns(orders_list, vehicles, time_budget=args.budget)
        algo = 'LNS'
    elif args.greedy:
        from greedy import run_greedy
        print(f"  Running Greedy...")
        result = run_greedy(orders_list, vehicles)
        algo = 'Greedy'
    else:
        from mcts_dispatcher import run_batch
        print(f"  Running MCTS ({args.budget}s)...")
        result = run_batch(batch_input)
        algo = 'MCTS'

    # Planned metrics
    planned = _planned_metrics(result, vehicles, cost_rates)

    # Actual metrics
    actual = load_actuals(date_str, telem_df, vehicles_df, jigsaw_df, qargo_df, orders_dict)

    # Print comparison
    flags = f"{algo}, {args.budget}s budget, {args.routing} routing"
    print_backtest(date_str, planned, actual, flags)

    # Write JSON
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        'date':    date_str,
        'flags':   {'algorithm': algo, 'budget_seconds': args.budget, 'routing': args.routing},
        'planned': planned,
        'actual':  actual,
        'notes': {
            'km_method': (
                'Actual = GPS-track Haversine sum between sorted pings per vehicle; '
                'undercounts curves slightly. Planned = closed-loop OSRM road distance.'
            ),
            'fuel_comparability': (
                'Planned fuel = fuel_gbp_per_mile[type] x miles from vehicle_cost_rates.json. '
                'Actual = Jigsaw diesel-only spend. Directly comparable.'
            ),
            'driver_allowance': (
                'Shown for reference; not in Jigsaw data and excluded from fuel delta.'
            ),
            'sla': (
                'on_time = destination_end_timestamp_local <= '
                'destination_requested_start_timestamp_local at date granularity. '
                'Planned on-time = 100% by construction (feasibility-constrained dispatcher).'
            ),
        },
    }
    out_path = OUTPUT_DIR / f'backtest_{date_str}.json'
    with open(out_path, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    print(f"  Saved: {out_path}")

    return {'planned': planned, 'actual': actual}


def main() -> None:
    parser = argparse.ArgumentParser(
        description='ZEEFLEET dispatcher backtest — planned vs actual',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--date',
                        help='Start date (YYYY-MM-DD). Default: first date in dataset.')
    parser.add_argument('--days', type=int, default=1,
                        help='Number of consecutive dates to backtest (default: 1).')
    parser.add_argument('--budget', type=int, default=120,
                        help='Dispatcher time budget in seconds (default: 120).')
    parser.add_argument('--alns',   action='store_true', help='Use ALNS dispatcher.')
    parser.add_argument('--lns',    action='store_true', help='Use LNS dispatcher.')
    parser.add_argument('--greedy', action='store_true', help='Use greedy baseline.')
    parser.add_argument('--routing', choices=['osrm', 'haversine'], default='haversine',
                        help='Distance model (default: haversine).')
    parser.add_argument('--osrm-url', default='http://localhost:5000',
                        help='OSRM server URL (default: http://localhost:5000).')
    args = parser.parse_args()

    print(f"\nZEEFLEET Dispatcher Backtest")
    print(f"Run at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("Loading datasets...")

    datasets    = load_datasets(str(BASE_DIR))
    qargo_df    = datasets['qargo']
    telem_df    = datasets['supatrak_telematics']
    vehicles_df = datasets['supatrak_vehicles']
    jigsaw_df   = datasets['jigsaw']

    if qargo_df.empty:
        print("ERROR: No Qargo data loaded.")
        sys.exit(1)

    print(f"  Qargo: {len(qargo_df):,} rows  |  Telematics: {len(telem_df):,} rows  "
          f"|  Vehicles: {len(vehicles_df):,}  |  Jigsaw: {len(jigsaw_df):,} rows")

    start_date = args.date or _first_date_in_qargo(qargo_df)

    from profitability_report.profitability_report_merged import _load_cost_rates
    cost_rates = _load_cost_rates(RATES_JSON_PATH)

    all_results = []
    for i in range(args.days):
        date_str = (datetime.strptime(start_date, '%Y-%m-%d') + timedelta(days=i)).strftime('%Y-%m-%d')
        day_result = _run_one_date(date_str, qargo_df, telem_df, vehicles_df, jigsaw_df, args, cost_rates)
        if day_result:
            all_results.append(day_result)

    if args.days > 1 and len(all_results) > 1:
        from report import print_backtest_summary
        algo = 'ALNS' if args.alns else ('LNS' if args.lns else ('Greedy' if args.greedy else 'MCTS'))
        print_backtest_summary(all_results, budget=args.budget, algorithm=algo)


if __name__ == '__main__':
    main()
```

### Step 4.2: Smoke-test the script on a single date

- [ ] Run on the first available date (no OSRM required):
```
cd E:\BEAT\ZECURE-Phase2-main\BackEnd\logistics
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe run_backtest.py --alns --budget 30
```
Expected output (structure only — numbers will vary):
```
ZEEFLEET Dispatcher Backtest
...
  BACKTEST  YYYY-MM-DD   (ALNS, 30s budget, haversine routing)
  ============================================================
                                    PLANNED       ACTUAL
  ...
  Fuel delta (planned vs actual):   +X.X%
  On-time delta (planned vs actual): +XX.Xpp
  ============================================================
  Saved: ...backtest_YYYY-MM-DD.json
```

- [ ] Verify the JSON file was written to `data/Output/backtest_YYYY-MM-DD.json` and contains all expected keys.

### Step 4.3: Run multi-day backtest

- [ ] Run 3 consecutive dates to verify the summary block prints:
```
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe run_backtest.py --alns --budget 30 --days 3
```
Expected: 3 individual comparison blocks followed by a `BACKTEST SUMMARY (3 days, ...)` block.

### Step 4.4: Run full test suite

- [ ] Verify no regressions:
```
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/ -v
```
Expected: all tests PASS

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Covered by |
|-----------------|------------|
| `run_backtest.py` script with `--date`, `--days`, `--alns/lns/greedy`, `--routing`, `--budget` | Task 4 |
| `simulation/actuals_loader.py` with `load_actuals` | Tasks 1 + 2 |
| GPS-track KM (Haversine between pings) | Task 1 |
| Jigsaw fuel (diesel-only, `>0`) | Task 2 |
| Qargo assignment rate (resource columns) | Task 2 |
| On-time rate (`dest_end <= deadline`, date-level) | Task 2 |
| `print_backtest` side-by-side output | Task 3 |
| `print_backtest_summary` aggregate | Task 3 |
| Fuel delta % shown | Task 3 |
| On-time delta pp shown | Task 3 |
| JSON written to `data/Output/backtest_YYYY-MM-DD.json` | Task 4 |
| Planned fuel = fuel-only (not total cost) | Task 4 `_planned_metrics` |
| Trade-off notes in code comments | Tasks 1, 2, 4 |
| Tests for all actuals helpers | Tasks 1, 2, 3 |
| Error/empty handling (warnings, no crash) | Task 2 `load_actuals` |
| OSRM note clarified (table vs match endpoint) | Task 1 `_gps_track_km` docstring |

**No placeholders found.** All code is complete.
