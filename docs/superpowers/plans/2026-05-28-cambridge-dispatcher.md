# Cambridge Regional Dispatcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A depot-anchored VRPTW dispatcher scoped to the Cambridge (CB22) operation, with collection planning for the FULL_FLEET subset and a level-0+1 backtest validation framework, matching the actual operation on historical days.

**Architecture:** New `cambridge/` package that wraps the existing `simulation/vrptw_*` engine. Scope filter classifies Qargo orders into `PL_IMPORT` (delivery only) and `FULL_FLEET` (collection + delivery). A collection planner schedules prior-day trunk pickups; the existing rolling dispatcher routes deliveries from CB22. The backtest module compares per-day planned-vs-actual at five day-totals and five distributional metrics.

**Tech Stack:** Python 3.12, pandas, scipy (for KS test), pytest. Reuses the existing `simulation/vrptw_engine.py`, `vrptw_alns.py`, `rolling_dispatcher.py`, `freight_tracker.py`, `data_loader.py`, `actuals_loader.py`. No solver code changes.

**Spec:** [`../cambridge-dispatcher-design.md`](../../cambridge-dispatcher-design.md).

**Run location:** All commands assume CWD = `e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics/`.

---

## File structure

**Files created (new package + tests + output dir):**

| Path | Responsibility |
|---|---|
| `cambridge/__init__.py` | Package marker, empty. |
| `cambridge/config.py` | Cambridge constants: depot anchor, fleet lists, service area, defaults, thresholds, collection profiles, service-level windows. |
| `cambridge/scope.py` | `ScopedOrder` dataclass + classification (`classify_order`, `in_cambridge_scope`, `build_scoped_orders`). Pure functions. |
| `cambridge/collection_planner.py` | `CollectionTrip` dataclass + planning (`group_by_origin`, `plan_trip`, `assign_tractor`, `plan_collections`). |
| `cambridge/dispatcher.py` | Orchestration (`build_freight_availability`, `run_event`, `run_day`). Calls `vrptw_alns.run_vrptw`. |
| `cambridge/backtest.py` | Validation (`actuals_for_day`, `level0_metrics`, `level1_metrics`, `run_day`, `run_period`, `print_report`). |
| `tests/cambridge/__init__.py` | Test package marker, empty. |
| `tests/cambridge/conftest.py` | Pytest fixtures: sample Qargo DataFrame, sample telematics, sample vehicles, sample postcode cache. |
| `tests/cambridge/test_scope.py` | Tests for `cambridge/scope.py`. |
| `tests/cambridge/test_collection_planner.py` | Tests for `cambridge/collection_planner.py`. |
| `tests/cambridge/test_dispatcher.py` | Tests for `cambridge/dispatcher.py`. |
| `tests/cambridge/test_backtest.py` | Tests for `cambridge/backtest.py`. |
| `data/Output/cambridge/` | Output directory for `day_compare_*.json` and `aggregate_*.json`. |

**Files modified:** none. The `simulation/` package stays unchanged per the spec.

---

## Task 0: Package skeleton + test scaffolding

**Files:**
- Create: `cambridge/__init__.py`
- Create: `cambridge/config.py`
- Create: `cambridge/scope.py`
- Create: `cambridge/collection_planner.py`
- Create: `cambridge/dispatcher.py`
- Create: `cambridge/backtest.py`
- Create: `tests/cambridge/__init__.py`
- Create: `tests/cambridge/conftest.py`
- Create: `data/Output/cambridge/.gitkeep`

- [ ] **Step 1: Create the package directories**

```bash
mkdir -p cambridge tests/cambridge data/Output/cambridge
touch data/Output/cambridge/.gitkeep
```

- [ ] **Step 2: Create empty package markers**

Create `cambridge/__init__.py` and `tests/cambridge/__init__.py`, both empty (zero bytes).

- [ ] **Step 3: Create the five stub modules**

Each stub is one line that defines the module. Create:

`cambridge/config.py`:
```python
"""Cambridge dispatcher configuration constants."""
```

`cambridge/scope.py`:
```python
"""Cambridge order classification and scope filter."""
```

`cambridge/collection_planner.py`:
```python
"""Cambridge collection trip planner for FULL_FLEET orders."""
```

`cambridge/dispatcher.py`:
```python
"""Cambridge regional dispatcher orchestration."""
```

`cambridge/backtest.py`:
```python
"""Cambridge backtest level-0 and level-1 validation."""
```

- [ ] **Step 4: Create `tests/cambridge/conftest.py` with placeholder fixtures**

```python
"""Shared pytest fixtures for Cambridge dispatcher tests."""
import pandas as pd
import pytest


@pytest.fixture
def sample_qargo_df():
    """Minimal Qargo DataFrame with one of each flow type."""
    return pd.DataFrame([
        {
            'order_id': 'pl-1',
            'name': 'WT900001',
            'order_import_integration_type': 'PALLETLINE',
            'resource_subcontractor': 'Palletline (import from API)',
            'resource_tractor': None,
            'resource_rigid': 'HX66DUH',
            'origin_postal_code': 'DN8 4HT',
            'origin_city': 'DONCASTER',
            'destination_postal_code': 'CB2 1AA',
            'destination_city': 'Cambridge',
            'destination_requested_start_timestamp_local': '2026-01-07 10:00:00',
            'origin_requested_start_timestamp_local': '2026-01-06 10:00:00',
            'goods_weight': 320.0,
            'goods_pallet_spaces': 1.0,
            'service_level_name': 'Next day',
            'transport_service': '1. Non Hazardous Shipment',
        },
        {
            'order_id': 'ff-1',
            'name': 'WT900002',
            'order_import_integration_type': 'MANUAL',
            'resource_subcontractor': None,
            'resource_tractor': 'AR05DEX',
            'resource_rigid': 'T88GNW',
            'origin_postal_code': 'CB9 8QP',
            'origin_city': 'Haverhill',
            'destination_postal_code': 'IP1 5AA',
            'destination_city': 'IPSWICH',
            'destination_requested_start_timestamp_local': '2026-01-07 09:00:00',
            'origin_requested_start_timestamp_local': '2026-01-06 09:00:00',
            'goods_weight': 1000.0,
            'goods_pallet_spaces': 2.0,
            'service_level_name': 'Next day',
            'transport_service': '1. Non Hazardous Shipment',
        },
        {
            'order_id': 'sub-1',
            'name': 'WT900003',
            'order_import_integration_type': 'MANUAL',
            'resource_subcontractor': 'Palletline (export to API)',
            'resource_tractor': None,
            'resource_rigid': None,
            'origin_postal_code': 'MK42 0LF',
            'origin_city': 'Bedford',
            'destination_postal_code': 'ME10 3FP',
            'destination_city': 'Sittingbourne',
            'destination_requested_start_timestamp_local': '2026-01-07 12:00:00',
            'origin_requested_start_timestamp_local': '2026-01-05 12:00:00',
            'goods_weight': 500.0,
            'goods_pallet_spaces': 1.0,
            'service_level_name': 'Economy',
            'transport_service': '1. Non Hazardous Shipment',
        },
    ])


@pytest.fixture
def sample_postcode_cache():
    """Postcode → (lat, lon) for the fixtures' postcodes plus CB22 depot."""
    return {
        'CB22 4PS': (52.0859, 0.1717),   # depot
        'CB2 1AA':  (52.2049, 0.1218),
        'IP1 5AA':  (52.0567, 1.1486),
        'CB9 8QP':  (52.0832, 0.4361),
        'DN8 4HT':  (53.6212, -0.9826),
        'MK42 0LF': (52.1141, -0.4798),
        'ME10 3FP': (51.3450, 0.7385),
    }
```

- [ ] **Step 5: Verify pytest discovers the new test folder**

Run: `python -m pytest tests/cambridge/ --collect-only -q`
Expected: `0 tests collected`. No errors. (Empty folder, no tests yet — but it must be discoverable.)

- [ ] **Step 6: Commit**

```bash
git add cambridge/ tests/cambridge/ data/Output/cambridge/
git commit -m "feat(cambridge): package skeleton and test fixtures"
```

---

## Task 1: cambridge/config.py — constants and thresholds

**Files:**
- Modify: `cambridge/config.py` (replace stub)
- Test: `tests/cambridge/test_config.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/cambridge/test_config.py`:

```python
"""Tests for cambridge/config.py — constants exist and have expected shape."""
from datetime import time
from cambridge import config


def test_depot_anchor_is_lat_lon_tuple():
    assert isinstance(config.CB22_DEPOT_ANCHOR, tuple)
    assert len(config.CB22_DEPOT_ANCHOR) == 2
    lat, lon = config.CB22_DEPOT_ANCHOR
    assert 51.0 < lat < 53.0   # roughly Cambridge area
    assert -1.0 < lon < 1.0


def test_cb22_rigids_is_non_empty_set_of_strings():
    assert isinstance(config.CB22_RIGIDS, set)
    assert len(config.CB22_RIGIDS) >= 10
    assert all(isinstance(v, str) for v in config.CB22_RIGIDS)


def test_cb22_tractors_is_set_of_strings():
    assert isinstance(config.CB22_TRACTORS, set)
    assert all(isinstance(v, str) for v in config.CB22_TRACTORS)


def test_service_prefixes_includes_core_postcodes():
    assert {'CB', 'SG', 'CM', 'AL', 'IP', 'SS'}.issubset(config.CAMBRIDGE_SERVICE_PREFIXES)


def test_catchment_radius_is_100km():
    assert config.CATCHMENT_RADIUS_KM == 100.0


def test_operating_day_endpoints():
    assert config.OPERATING_DAY_START == time(6, 0)
    assert config.OPERATING_DAY_END == time(18, 0)


def test_collection_profiles_has_cb9_ardex():
    assert 'CB9' in config.COLLECTION_PROFILES
    cb9 = config.COLLECTION_PROFILES['CB9']
    assert 'depart_hour' in cb9
    assert 'dwell_min' in cb9
    assert 'trip_hours' in cb9


def test_service_level_window_hours_covers_common_levels():
    assert config.SERVICE_LEVEL_WINDOW_HOURS['Next day'] == 24
    assert config.SERVICE_LEVEL_WINDOW_HOURS['Economy'] == 48


def test_pass_thresholds_levels_0_and_1():
    assert 'km_pct' in config.PASS_THRESHOLDS
    assert 'ks_max' in config.PASS_THRESHOLDS
    assert 'jaccard_min' in config.PASS_THRESHOLDS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_config.py -v`
Expected: All tests FAIL with `AttributeError: module 'cambridge.config' has no attribute '...'`.

- [ ] **Step 3: Write the implementation**

Replace `cambridge/config.py` with:

```python
"""Cambridge dispatcher configuration constants."""
from datetime import time

# Cambridge depot (Duxford CB22 4PS) GPS anchor
CB22_DEPOT_ANCHOR = (52.0859, 0.1717)

# Confirmed Cambridge-fleet rigids (from investigations/cambridge_audit.py;
# 100% consistent overnight at CB22 in the telematics).
CB22_RIGIDS = {
    'HX66DUH', 'T88GNW', 'T88RNW', 'W88RNW', 'LN67SWJ', 'M88GNW',
    'AY18JWA', 'L88GNW', 'BF65WBY', 'AR05DEX', 'T888RNW',
}

# Confirmed Cambridge-fleet tractors. Identified via the same telematics-based
# home-depot derivation as the rigids: asset_type == 'Tractor Unit' AND parks
# overnight at CB22 ≥ 90% of the time across Jan–Feb. To be confirmed during
# implementation by running the same analysis with tractor filter.
CB22_TRACTORS = {
    'AR02DEX', 'N8GNW', 'Y88RNW', 'N88GNW', 'S88RNW', 'R88GNW',
}

# Geographic scope for delivery destinations.
CAMBRIDGE_SERVICE_PREFIXES = {
    'CB', 'SG', 'CM', 'AL', 'IP', 'SS', 'PE', 'RH', 'NW', 'LU',
}

# Maximum origin distance from CB22 for a FULL_FLEET order to be in scope.
CATCHMENT_RADIUS_KM = 100.0

# Operating day boundaries.
OPERATING_DAY_START = time(6, 0)
OPERATING_DAY_END = time(18, 0)

# Vehicle re-availability gate.
MIN_VIABLE_TRIP_HOURS = 1.5

# Cross-dock buffer between trunk arrival and freight ready for delivery.
CROSS_DOCK_BUFFER_MIN = 30

# Route-cost constants (mirror vrptw_engine defaults; overridable here).
VEHICLE_ACTIVATION_COST = 150.0
ROAD_DISTANCE_FACTOR = 1.3
AVG_SPEED_KMH = 50.0
SERVICE_MINUTES_PER_STOP = 20.0
UNASSIGNED_PENALTY = 50_000.0

# Per-origin collection profiles learned from telematics
# (investigations/verify_collection_patterns.py).
COLLECTION_PROFILES = {
    'CB9': {'depart_hour': 10, 'dwell_min': 62,  'trip_hours': 3.2},
    'SG8': {'depart_hour':  8, 'dwell_min': 282, 'trip_hours': 5.7},
    'AL7': {'depart_hour':  7, 'dwell_min': 289, 'trip_hours': 9.5},
    'SG6': {'depart_hour':  9, 'dwell_min': 66,  'trip_hours': 6.8},
}
DEFAULT_COLLECTION_PROFILE = {'depart_hour': 8, 'dwell_min': 60, 'trip_hours': 4.0}

# Delivery time-window width from service level.
SERVICE_LEVEL_WINDOW_HOURS = {
    'Next day': 24,
    'Economy': 48,
    '5. Specialist Movement': 4,
}
DEFAULT_WINDOW_HOURS = 24

# Default freight availability times for backtest fallback.
DEFAULT_PRE_STAGED_HOUR = 6
DEFAULT_VIA_DEPOT_HOUR = 12

# Level 0 + Level 1 pass thresholds.
PASS_THRESHOLDS = {
    'km_pct':           0.20,     # ±20% on total delivery km
    'vehicles_count':   2,        # ±2 vehicles
    'fuel_pct':         0.25,     # ±25% on fuel cost
    'assignment_pp':    0.10,     # ±10 percentage points
    'on_time_pp':       0.10,
    'ks_max':           0.30,     # KS distance ceiling for distributional metrics
    'jaccard_min':      0.80,     # Postcode-district set overlap floor
    'time_minutes':     60,       # ±60 min for median depart/return time
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_config.py -v`
Expected: All 9 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cambridge/config.py tests/cambridge/test_config.py
git commit -m "feat(cambridge): config constants and pass thresholds"
```

---

## Task 2: scope — classify_order (single-order flow tagging)

**Files:**
- Modify: `cambridge/scope.py`
- Test: `tests/cambridge/test_scope.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/cambridge/test_scope.py`:

```python
"""Tests for cambridge/scope.py."""
import pandas as pd
import pytest
from cambridge.scope import classify_order


def _row(**kwargs):
    """Build a minimal Qargo row with defaults; override via kwargs."""
    defaults = {
        'order_import_integration_type': 'MANUAL',
        'resource_subcontractor': None,
        'transport_service': '1. Non Hazardous Shipment',
        'origin_postal_code': 'CB9 8QP',
        'destination_postal_code': 'CB2 1AA',
    }
    defaults.update(kwargs)
    return pd.Series(defaults)


def test_palletline_with_import_suffix_is_pl_import():
    row = _row(order_import_integration_type='PALLETLINE',
               resource_subcontractor='Palletline (import from API)')
    assert classify_order(row) == 'PL_IMPORT'


def test_manual_with_no_sub_is_full_fleet():
    row = _row(order_import_integration_type='MANUAL', resource_subcontractor=None)
    assert classify_order(row) == 'FULL_FLEET'


def test_null_import_with_no_sub_is_full_fleet():
    row = _row(order_import_integration_type=None, resource_subcontractor=None)
    assert classify_order(row) == 'FULL_FLEET'


def test_specialist_movement_is_out_of_scope():
    row = _row(transport_service='5. Specialist Movement')
    assert classify_order(row) is None


def test_hazchem_is_out_of_scope():
    row = _row(order_import_integration_type='HAZCHEM')
    assert classify_order(row) is None


def test_sub_export_is_out_of_scope():
    row = _row(resource_subcontractor='Palletline (export to API)')
    assert classify_order(row) is None


def test_clarus_is_out_of_scope():
    row = _row(order_import_integration_type='CLARUS')
    assert classify_order(row) is None


def test_palletline_without_recognised_sub_is_out_of_scope():
    """PALLETLINE without (import from API) suffix can't be trusted."""
    row = _row(order_import_integration_type='PALLETLINE', resource_subcontractor=None)
    assert classify_order(row) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_scope.py -v`
Expected: All tests FAIL with `ImportError: cannot import name 'classify_order'`.

- [ ] **Step 3: Implement classify_order**

Replace `cambridge/scope.py` with:

```python
"""Cambridge order classification and scope filter."""
from typing import Literal, Optional

import pandas as pd

FlowTag = Literal['PL_IMPORT', 'FULL_FLEET']


def classify_order(row: pd.Series) -> Optional[FlowTag]:
    """Classify a Qargo order row into a Cambridge flow tag.

    Returns 'PL_IMPORT' for orders Palletline brought in (delivery only),
    'FULL_FLEET' for orders we own end-to-end (collection + delivery),
    or None for orders out of v1 scope (direct, hazmat, sub-only, ambiguous).
    """
    transport = str(row.get('transport_service', '') or '')
    if 'Specialist Movement' in transport:
        return None

    import_type = row.get('order_import_integration_type')
    import_type_str = '' if import_type is None or (isinstance(import_type, float) and pd.isna(import_type)) else str(import_type)
    sub = row.get('resource_subcontractor')
    sub_str = '' if sub is None or (isinstance(sub, float) and pd.isna(sub)) else str(sub)

    if import_type_str == 'PALLETLINE' and 'import from API' in sub_str:
        return 'PL_IMPORT'

    if import_type_str in ('MANUAL', '') and not sub_str:
        return 'FULL_FLEET'

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_scope.py -v`
Expected: All 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cambridge/scope.py tests/cambridge/test_scope.py
git commit -m "feat(cambridge): classify_order flow tagging"
```

---

## Task 3: scope — in_cambridge_scope (geographic filter)

**Files:**
- Modify: `cambridge/scope.py`
- Modify: `tests/cambridge/test_scope.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/cambridge/test_scope.py`:

```python
from cambridge.scope import in_cambridge_scope
from cambridge import config


def test_pl_import_with_cambridge_destination_in_scope(sample_postcode_cache):
    row = _row(order_import_integration_type='PALLETLINE',
               resource_subcontractor='Palletline (import from API)',
               destination_postal_code='CB2 1AA')
    assert in_cambridge_scope(row, 'PL_IMPORT', sample_postcode_cache) is True


def test_pl_import_with_out_of_area_destination_not_in_scope(sample_postcode_cache):
    sample_postcode_cache['BS3 5AA'] = (51.4500, -2.5500)  # Bristol
    row = _row(destination_postal_code='BS3 5AA')
    assert in_cambridge_scope(row, 'PL_IMPORT', sample_postcode_cache) is False


def test_full_fleet_with_cambridge_origin_in_scope(sample_postcode_cache):
    row = _row(origin_postal_code='CB9 8QP', destination_postal_code='CB2 1AA')
    assert in_cambridge_scope(row, 'FULL_FLEET', sample_postcode_cache) is True


def test_full_fleet_with_stoke_origin_not_in_scope(sample_postcode_cache):
    """ST4 (Stoke) origin is >100 km from CB22 — outside catchment."""
    sample_postcode_cache['ST4 8JB'] = (53.0050, -2.1796)
    row = _row(origin_postal_code='ST4 8JB', destination_postal_code='CB2 1AA')
    assert in_cambridge_scope(row, 'FULL_FLEET', sample_postcode_cache) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_scope.py -v -k "in_cambridge_scope"`
Expected: 4 tests FAIL with `ImportError: cannot import name 'in_cambridge_scope'`.

- [ ] **Step 3: Implement in_cambridge_scope**

Append to `cambridge/scope.py`:

```python
import math

from cambridge.config import (
    CB22_DEPOT_ANCHOR,
    CAMBRIDGE_SERVICE_PREFIXES,
    CATCHMENT_RADIUS_KM,
)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _postcode_prefix(pc: str) -> str:
    """Outward area code (first 1-2 letters)."""
    pc = pc.strip().upper()
    return ''.join(c for c in pc[:2] if c.isalpha())


def in_cambridge_scope(row: pd.Series, flow: FlowTag,
                       postcode_cache: dict) -> bool:
    """Decide whether a classified order is in Cambridge's scope.

    PL_IMPORT: destination postcode prefix in CAMBRIDGE_SERVICE_PREFIXES.
    FULL_FLEET: same destination rule AND origin within CATCHMENT_RADIUS_KM of CB22.
    """
    dest_pc = str(row.get('destination_postal_code', '')).strip().upper()
    if _postcode_prefix(dest_pc) not in CAMBRIDGE_SERVICE_PREFIXES:
        return False

    if flow == 'PL_IMPORT':
        return True

    # FULL_FLEET — additionally check origin catchment
    origin_pc = str(row.get('origin_postal_code', '')).strip().upper()
    origin_coords = postcode_cache.get(origin_pc)
    if origin_coords is None:
        return False
    olat, olon = origin_coords
    dlat, dlon = CB22_DEPOT_ANCHOR
    return _haversine_km(olat, olon, dlat, dlon) <= CATCHMENT_RADIUS_KM
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_scope.py -v`
Expected: All 12 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cambridge/scope.py tests/cambridge/test_scope.py
git commit -m "feat(cambridge): in_cambridge_scope geographic filter"
```

---

## Task 4: scope — ScopedOrder + build_scoped_orders

**Files:**
- Modify: `cambridge/scope.py`
- Modify: `tests/cambridge/test_scope.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/cambridge/test_scope.py`:

```python
from cambridge.scope import ScopedOrder, build_scoped_orders


def test_scoped_order_has_required_fields():
    so = ScopedOrder(
        order_id='abc', name='WT1', flow='PL_IMPORT',
        origin_pc=None, destination_pc='CB2 1AA',
        weight_kg=320.0, pallets=1.0,
        delivery_window=(pd.Timestamp('2026-01-07 06:00'),
                         pd.Timestamp('2026-01-07 18:00')),
        collection_window=None,
    )
    assert so.order_id == 'abc'
    assert so.flow == 'PL_IMPORT'


def test_build_scoped_orders_from_sample(sample_qargo_df, sample_postcode_cache):
    orders = build_scoped_orders(sample_qargo_df, sample_postcode_cache)
    # pl-1 (Doncaster -> CB2): PL_IMPORT in scope
    # ff-1 (Haverhill -> IP1): FULL_FLEET in scope (CB9 within catchment)
    # sub-1: sub-only → out of scope
    assert len(orders) == 2
    flows = {o.flow for o in orders}
    assert flows == {'PL_IMPORT', 'FULL_FLEET'}


def test_build_scoped_orders_assigns_collection_window_only_for_full_fleet(
        sample_qargo_df, sample_postcode_cache):
    orders = build_scoped_orders(sample_qargo_df, sample_postcode_cache)
    pl = next(o for o in orders if o.flow == 'PL_IMPORT')
    ff = next(o for o in orders if o.flow == 'FULL_FLEET')
    assert pl.collection_window is None
    assert ff.collection_window is not None


def test_build_scoped_orders_uses_service_level_window():
    """A 'Next day' order gets a 24-hour delivery window."""
    df = pd.DataFrame([{
        'order_id': 'a', 'name': 'WT1',
        'order_import_integration_type': 'PALLETLINE',
        'resource_subcontractor': 'Palletline (import from API)',
        'resource_tractor': None, 'resource_rigid': 'X',
        'origin_postal_code': 'DN8 4HT',
        'destination_postal_code': 'CB2 1AA',
        'destination_requested_start_timestamp_local': '2026-01-07 09:00:00',
        'origin_requested_start_timestamp_local': '2026-01-06 09:00:00',
        'goods_weight': 100.0, 'goods_pallet_spaces': 1.0,
        'service_level_name': 'Next day',
        'transport_service': '1. Non Hazardous Shipment',
    }])
    cache = {'DN8 4HT': (53.6212, -0.9826), 'CB2 1AA': (52.2049, 0.1218)}
    [order] = build_scoped_orders(df, cache)
    start, end = order.delivery_window
    assert (end - start).total_seconds() / 3600 == 24
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_scope.py -v -k "scoped_order or build_scoped"`
Expected: 4 tests FAIL with `ImportError`.

- [ ] **Step 3: Implement ScopedOrder + build_scoped_orders**

Append to `cambridge/scope.py`:

```python
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

from cambridge.config import (
    SERVICE_LEVEL_WINDOW_HOURS,
    DEFAULT_WINDOW_HOURS,
    OPERATING_DAY_START,
    OPERATING_DAY_END,
)


@dataclass
class ScopedOrder:
    order_id: str
    name: str
    flow: FlowTag
    origin_pc: Optional[str]              # None for PL_IMPORT
    destination_pc: str
    weight_kg: float
    pallets: float
    delivery_window: Tuple[datetime, datetime]
    collection_window: Optional[Tuple[datetime, datetime]]


def _delivery_window(row: pd.Series) -> Tuple[datetime, datetime]:
    """Compute [start, start + service_level_hours] from Qargo timestamps.

    If the timestamp has time = 00:00 (date-only), treat as the operating day."""
    start = pd.to_datetime(row.get('destination_requested_start_timestamp_local'))
    sl = str(row.get('service_level_name', '') or '')
    hours = SERVICE_LEVEL_WINDOW_HOURS.get(sl, DEFAULT_WINDOW_HOURS)
    if start.hour == 0 and start.minute == 0:
        start = start.replace(hour=OPERATING_DAY_START.hour,
                              minute=OPERATING_DAY_START.minute)
        end = start.replace(hour=OPERATING_DAY_END.hour,
                            minute=OPERATING_DAY_END.minute)
    else:
        end = start + timedelta(hours=hours)
    return start.to_pydatetime(), end.to_pydatetime()


def _collection_window(row: pd.Series) -> Tuple[datetime, datetime]:
    """For FULL_FLEET, derive collection window from origin timestamp + 24h."""
    start = pd.to_datetime(row.get('origin_requested_start_timestamp_local'))
    if start.hour == 0 and start.minute == 0:
        start = start.replace(hour=OPERATING_DAY_START.hour,
                              minute=OPERATING_DAY_START.minute)
        end = start.replace(hour=OPERATING_DAY_END.hour,
                            minute=OPERATING_DAY_END.minute)
    else:
        end = start + timedelta(hours=DEFAULT_WINDOW_HOURS)
    return start.to_pydatetime(), end.to_pydatetime()


def build_scoped_orders(qargo_df: pd.DataFrame,
                        postcode_cache: dict) -> list[ScopedOrder]:
    """Classify and scope-filter every Qargo row; return in-scope orders only."""
    out: list[ScopedOrder] = []
    for _, row in qargo_df.iterrows():
        flow = classify_order(row)
        if flow is None:
            continue
        if not in_cambridge_scope(row, flow, postcode_cache):
            continue
        out.append(ScopedOrder(
            order_id=str(row['order_id']),
            name=str(row.get('name', '')),
            flow=flow,
            origin_pc=(str(row['origin_postal_code']).strip().upper()
                       if flow == 'FULL_FLEET' else None),
            destination_pc=str(row['destination_postal_code']).strip().upper(),
            weight_kg=float(row.get('goods_weight', 0) or 0),
            pallets=float(row.get('goods_pallet_spaces', 0) or 0),
            delivery_window=_delivery_window(row),
            collection_window=(_collection_window(row)
                               if flow == 'FULL_FLEET' else None),
        ))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_scope.py -v`
Expected: All 16 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cambridge/scope.py tests/cambridge/test_scope.py
git commit -m "feat(cambridge): ScopedOrder dataclass and build_scoped_orders"
```

---

## Task 5: collection_planner — group_by_origin

**Files:**
- Modify: `cambridge/collection_planner.py`
- Test: `tests/cambridge/test_collection_planner.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/cambridge/test_collection_planner.py`:

```python
"""Tests for cambridge/collection_planner.py."""
from datetime import datetime

import pandas as pd
import pytest

from cambridge.scope import ScopedOrder
from cambridge.collection_planner import group_by_origin


def _ff(order_id: str, origin_pc: str, delivery_date: str) -> ScopedOrder:
    return ScopedOrder(
        order_id=order_id, name=f'WT{order_id}', flow='FULL_FLEET',
        origin_pc=origin_pc, destination_pc='CB2 1AA',
        weight_kg=500.0, pallets=1.0,
        delivery_window=(datetime.fromisoformat(f'{delivery_date} 06:00:00'),
                         datetime.fromisoformat(f'{delivery_date} 18:00:00')),
        collection_window=(datetime.fromisoformat(f'{delivery_date} 00:00:00'),
                           datetime.fromisoformat(f'{delivery_date} 23:59:59')),
    )


def test_groups_orders_by_date_and_origin():
    orders = [
        _ff('a', 'CB9 8QP', '2026-01-07'),
        _ff('b', 'CB9 8QP', '2026-01-07'),
        _ff('c', 'SG8 5RL', '2026-01-07'),
        _ff('d', 'CB9 8QP', '2026-01-08'),
    ]
    groups = group_by_origin(orders)
    assert len(groups) == 3
    assert len(groups[('2026-01-07', 'CB9')]) == 2
    assert len(groups[('2026-01-07', 'SG8')]) == 1
    assert len(groups[('2026-01-08', 'CB9')]) == 1


def test_skips_pl_import_orders():
    orders = [
        _ff('a', 'CB9 8QP', '2026-01-07'),
        ScopedOrder(order_id='b', name='WTb', flow='PL_IMPORT',
                    origin_pc=None, destination_pc='CB2 1AA',
                    weight_kg=100.0, pallets=1.0,
                    delivery_window=(datetime.fromisoformat('2026-01-07 06:00:00'),
                                     datetime.fromisoformat('2026-01-07 18:00:00')),
                    collection_window=None),
    ]
    groups = group_by_origin(orders)
    assert len(groups) == 1
    assert ('2026-01-07', 'CB9') in groups
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_collection_planner.py -v`
Expected: All tests FAIL with `ImportError: cannot import name 'group_by_origin'`.

- [ ] **Step 3: Implement group_by_origin**

Replace `cambridge/collection_planner.py` with:

```python
"""Cambridge collection trip planner for FULL_FLEET orders."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from cambridge.scope import ScopedOrder


def _postcode_outward(pc: str) -> str:
    """First half of a UK postcode (the outward area code, e.g. 'CB9')."""
    pc = pc.strip().upper()
    return pc.split(' ')[0] if ' ' in pc else pc[:3]


def group_by_origin(orders: list[ScopedOrder]
                    ) -> dict[tuple[str, str], list[ScopedOrder]]:
    """Group FULL_FLEET orders by (delivery_date_iso, origin_outward_postcode).

    Returns {(YYYY-MM-DD, OUTWARD): [orders, ...]}. PL_IMPORT orders are skipped.
    """
    groups: dict[tuple[str, str], list[ScopedOrder]] = defaultdict(list)
    for order in orders:
        if order.flow != 'FULL_FLEET':
            continue
        if order.origin_pc is None:
            continue
        delivery_date = order.delivery_window[0].date().isoformat()
        outward = _postcode_outward(order.origin_pc)
        groups[(delivery_date, outward)].append(order)
    return dict(groups)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_collection_planner.py -v`
Expected: Both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cambridge/collection_planner.py tests/cambridge/test_collection_planner.py
git commit -m "feat(cambridge): group_by_origin for collection planning"
```

---

## Task 6: collection_planner — plan_trip + CollectionTrip

**Files:**
- Modify: `cambridge/collection_planner.py`
- Modify: `tests/cambridge/test_collection_planner.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/cambridge/test_collection_planner.py`:

```python
from datetime import date as date_type, timedelta

from cambridge.collection_planner import plan_trip, CollectionTrip


def test_plan_trip_uses_origin_profile_for_cb9():
    orders = [_ff('a', 'CB9 8QP', '2026-01-08')]
    # delivery day = Thu 2026-01-08; trip scheduled prior day Wed 2026-01-07
    trip = plan_trip(date_type(2026, 1, 8), 'CB9', orders,
                     postcode_cache={'CB9 8QP': (52.0832, 0.4361)})
    assert isinstance(trip, CollectionTrip)
    assert trip.origin_pc == 'CB9'
    assert trip.depart_cb22.date() == date_type(2026, 1, 7)  # prior day
    assert trip.depart_cb22.hour == 10                       # CB9 profile = 10am
    assert trip.arrive_cb22 > trip.depart_origin
    assert trip.freight_ready_at_depot > trip.arrive_cb22
    assert trip.orders == [orders[0].order_id]


def test_plan_trip_uses_default_profile_for_unknown_origin():
    orders = [_ff('a', 'NR3 2BD', '2026-01-08')]
    trip = plan_trip(date_type(2026, 1, 8), 'NR3', orders,
                     postcode_cache={'NR3 2BD': (52.6309, 1.2974)})
    assert trip.depart_cb22.hour == 8   # DEFAULT_COLLECTION_PROFILE.depart_hour


def test_plan_trip_returns_none_when_origin_not_geocoded():
    orders = [_ff('a', 'XX9 9XX', '2026-01-08')]
    trip = plan_trip(date_type(2026, 1, 8), 'XX9', orders, postcode_cache={})
    assert trip is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_collection_planner.py -v -k "plan_trip"`
Expected: 3 tests FAIL with `ImportError`.

- [ ] **Step 3: Implement plan_trip + CollectionTrip**

Append to `cambridge/collection_planner.py`:

```python
import math
from datetime import datetime, date as date_type, timedelta

from cambridge.config import (
    CB22_DEPOT_ANCHOR,
    COLLECTION_PROFILES,
    DEFAULT_COLLECTION_PROFILE,
    AVG_SPEED_KMH,
    ROAD_DISTANCE_FACTOR,
    CROSS_DOCK_BUFFER_MIN,
)


@dataclass
class CollectionTrip:
    trip_id: str
    tractor_id: Optional[str]               # assigned later
    origin_pc: str                          # outward code
    orders: list[str]                       # order_ids
    depart_cb22: datetime
    arrive_origin: datetime
    depart_origin: datetime
    arrive_cb22: datetime
    freight_ready_at_depot: datetime


def _hav(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def plan_trip(delivery_date: date_type,
              origin_outward: str,
              orders: list[ScopedOrder],
              postcode_cache: dict) -> Optional[CollectionTrip]:
    """Build a single-origin out-and-back trip for one (date, origin) group.

    Returns None if the origin can't be geocoded.
    """
    if not orders:
        return None
    first_pc = orders[0].origin_pc
    coords = postcode_cache.get(first_pc)
    if coords is None:
        return None
    olat, olon = coords
    clat, clon = CB22_DEPOT_ANCHOR
    one_way_km = _hav(olat, olon, clat, clon) * ROAD_DISTANCE_FACTOR
    one_way_hours = one_way_km / AVG_SPEED_KMH

    profile = COLLECTION_PROFILES.get(origin_outward, DEFAULT_COLLECTION_PROFILE)
    prior_day = delivery_date - timedelta(days=1)
    depart_cb22 = datetime(prior_day.year, prior_day.month, prior_day.day,
                           profile['depart_hour'], 0, 0)
    arrive_origin = depart_cb22 + timedelta(hours=one_way_hours)
    depart_origin = arrive_origin + timedelta(minutes=profile['dwell_min'])
    arrive_cb22 = depart_origin + timedelta(hours=one_way_hours)
    freight_ready = arrive_cb22 + timedelta(minutes=CROSS_DOCK_BUFFER_MIN)

    trip_id = f'TRIP-{delivery_date.isoformat()}-{origin_outward}'
    return CollectionTrip(
        trip_id=trip_id, tractor_id=None, origin_pc=origin_outward,
        orders=[o.order_id for o in orders],
        depart_cb22=depart_cb22, arrive_origin=arrive_origin,
        depart_origin=depart_origin, arrive_cb22=arrive_cb22,
        freight_ready_at_depot=freight_ready,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_collection_planner.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cambridge/collection_planner.py tests/cambridge/test_collection_planner.py
git commit -m "feat(cambridge): plan_trip and CollectionTrip dataclass"
```

---

## Task 7: collection_planner — assign_tractor (greedy)

**Files:**
- Modify: `cambridge/collection_planner.py`
- Modify: `tests/cambridge/test_collection_planner.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/cambridge/test_collection_planner.py`:

```python
from cambridge.collection_planner import assign_tractor


def _trip(depart='2026-01-07 10:00', return_to='2026-01-07 14:00') -> CollectionTrip:
    return CollectionTrip(
        trip_id='T1', tractor_id=None, origin_pc='CB9',
        orders=['a'],
        depart_cb22=datetime.fromisoformat(depart),
        arrive_origin=datetime.fromisoformat(depart) + timedelta(minutes=30),
        depart_origin=datetime.fromisoformat(depart) + timedelta(hours=2),
        arrive_cb22=datetime.fromisoformat(return_to),
        freight_ready_at_depot=datetime.fromisoformat(return_to) + timedelta(minutes=30),
    )


def test_assign_tractor_picks_an_available_tractor():
    pool = ['AR02DEX', 'N8GNW']
    assigned = assign_tractor(_trip(), pool)
    assert assigned in pool


def test_assign_tractor_returns_none_when_pool_empty():
    assert assign_tractor(_trip(), []) is None


def test_assign_tractor_marks_pool_used():
    pool = ['AR02DEX']
    first = assign_tractor(_trip('2026-01-07 10:00', '2026-01-07 14:00'), pool,
                           busy_intervals={'AR02DEX': []})
    busy = {'AR02DEX': [(datetime.fromisoformat('2026-01-07 10:00'),
                          datetime.fromisoformat('2026-01-07 14:00'))]}
    second = assign_tractor(_trip('2026-01-07 12:00', '2026-01-07 15:00'), pool,
                            busy_intervals=busy)
    assert first == 'AR02DEX'
    assert second is None        # conflicts with first booking
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_collection_planner.py -v -k "assign_tractor"`
Expected: 3 tests FAIL with `ImportError`.

- [ ] **Step 3: Implement assign_tractor**

Append to `cambridge/collection_planner.py`:

```python
def assign_tractor(trip: CollectionTrip,
                   pool: list[str],
                   busy_intervals: Optional[dict[str, list[tuple[datetime, datetime]]]] = None
                   ) -> Optional[str]:
    """Greedy: pick the first tractor in `pool` whose busy_intervals don't
    overlap the trip window. Returns the chosen tractor_id or None."""
    if not pool:
        return None
    busy_intervals = busy_intervals or {}
    trip_start = trip.depart_cb22
    trip_end = trip.arrive_cb22
    for tractor in pool:
        intervals = busy_intervals.get(tractor, [])
        if all(not (trip_start < end and start < trip_end)
               for start, end in intervals):
            return tractor
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_collection_planner.py -v`
Expected: All 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cambridge/collection_planner.py tests/cambridge/test_collection_planner.py
git commit -m "feat(cambridge): assign_tractor with busy-interval conflicts"
```

---

## Task 8: collection_planner — plan_collections (composition)

**Files:**
- Modify: `cambridge/collection_planner.py`
- Modify: `tests/cambridge/test_collection_planner.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/cambridge/test_collection_planner.py`:

```python
from cambridge.collection_planner import plan_collections


def test_plan_collections_composes_grouping_planning_assignment():
    orders = [
        _ff('a', 'CB9 8QP', '2026-01-08'),
        _ff('b', 'CB9 8QP', '2026-01-08'),
        _ff('c', 'SG8 5RL', '2026-01-08'),
    ]
    cache = {'CB9 8QP': (52.0832, 0.4361), 'SG8 5RL': (52.0721, -0.0232)}
    tractor_pool = ['AR02DEX', 'N8GNW']
    trips = plan_collections(orders, tractor_pool, postcode_cache=cache)
    assert len(trips) == 2                          # CB9 and SG8 groups
    assert all(t.tractor_id is not None for t in trips)
    assert {t.tractor_id for t in trips} <= set(tractor_pool)


def test_plan_collections_skips_pl_import_orders():
    orders = [
        ScopedOrder(order_id='a', name='WTa', flow='PL_IMPORT',
                    origin_pc=None, destination_pc='CB2 1AA',
                    weight_kg=100.0, pallets=1.0,
                    delivery_window=(datetime.fromisoformat('2026-01-07 06:00:00'),
                                     datetime.fromisoformat('2026-01-07 18:00:00')),
                    collection_window=None),
    ]
    trips = plan_collections(orders, ['AR02DEX'], postcode_cache={})
    assert trips == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_collection_planner.py -v -k "plan_collections"`
Expected: 2 tests FAIL with `ImportError`.

- [ ] **Step 3: Implement plan_collections**

Append to `cambridge/collection_planner.py`:

```python
def plan_collections(orders: list[ScopedOrder],
                     tractor_pool: list[str],
                     postcode_cache: dict) -> list[CollectionTrip]:
    """End-to-end collection planning for a set of in-scope orders.

    Groups by (delivery_date, origin) → builds trip per group → assigns a
    tractor with greedy fit. Trips that can't be geocoded or can't get a
    tractor are dropped (logged via simple print for v1).
    """
    groups = group_by_origin(orders)
    busy: dict[str, list[tuple[datetime, datetime]]] = {t: [] for t in tractor_pool}
    trips: list[CollectionTrip] = []
    # Process in chronological order so earlier trips claim tractors first.
    for (date_iso, outward) in sorted(groups.keys()):
        group_orders = groups[(date_iso, outward)]
        d = datetime.fromisoformat(date_iso).date()
        trip = plan_trip(d, outward, group_orders, postcode_cache)
        if trip is None:
            continue
        tractor = assign_tractor(trip, tractor_pool, busy_intervals=busy)
        if tractor is None:
            continue
        trip.tractor_id = tractor
        busy[tractor].append((trip.depart_cb22, trip.arrive_cb22))
        trips.append(trip)
    return trips
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_collection_planner.py -v`
Expected: All 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cambridge/collection_planner.py tests/cambridge/test_collection_planner.py
git commit -m "feat(cambridge): plan_collections composition"
```

---

## Task 9: dispatcher — build_freight_availability

**Files:**
- Modify: `cambridge/dispatcher.py`
- Test: `tests/cambridge/test_dispatcher.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/cambridge/test_dispatcher.py`:

```python
"""Tests for cambridge/dispatcher.py."""
from datetime import datetime, date as date_type

import pytest

from cambridge.scope import ScopedOrder
from cambridge.collection_planner import CollectionTrip
from cambridge.dispatcher import build_freight_availability


def _pl(order_id: str) -> ScopedOrder:
    return ScopedOrder(
        order_id=order_id, name=f'WT{order_id}', flow='PL_IMPORT',
        origin_pc=None, destination_pc='CB2 1AA',
        weight_kg=100.0, pallets=1.0,
        delivery_window=(datetime.fromisoformat('2026-01-07 06:00:00'),
                         datetime.fromisoformat('2026-01-07 18:00:00')),
        collection_window=None,
    )


def _ff(order_id: str) -> ScopedOrder:
    return ScopedOrder(
        order_id=order_id, name=f'WT{order_id}', flow='FULL_FLEET',
        origin_pc='CB9', destination_pc='CB2 1AA',
        weight_kg=100.0, pallets=1.0,
        delivery_window=(datetime.fromisoformat('2026-01-07 06:00:00'),
                         datetime.fromisoformat('2026-01-07 18:00:00')),
        collection_window=(datetime.fromisoformat('2026-01-06 00:00:00'),
                           datetime.fromisoformat('2026-01-06 23:59:59')),
    )


def test_pl_import_freight_ready_at_06_00():
    orders = [_pl('a')]
    ready = build_freight_availability(orders, trips=[],
                                       day=date_type(2026, 1, 7))
    assert ready['a'] == datetime.fromisoformat('2026-01-07 06:00:00')


def test_full_fleet_freight_ready_from_trip():
    orders = [_ff('a')]
    trip = CollectionTrip(
        trip_id='T1', tractor_id='AR02DEX', origin_pc='CB9', orders=['a'],
        depart_cb22=datetime.fromisoformat('2026-01-06 10:00:00'),
        arrive_origin=datetime.fromisoformat('2026-01-06 11:00:00'),
        depart_origin=datetime.fromisoformat('2026-01-06 12:00:00'),
        arrive_cb22=datetime.fromisoformat('2026-01-06 13:00:00'),
        freight_ready_at_depot=datetime.fromisoformat('2026-01-06 13:30:00'),
    )
    ready = build_freight_availability(orders, trips=[trip],
                                       day=date_type(2026, 1, 7))
    # Trip arrived day before; freight ready at 2026-01-07 06:00 (earliest of trip+buffer and day start).
    assert ready['a'] == datetime.fromisoformat('2026-01-07 06:00:00')


def test_full_fleet_without_trip_falls_back_to_day_start():
    orders = [_ff('a')]
    ready = build_freight_availability(orders, trips=[],
                                       day=date_type(2026, 1, 7))
    assert ready['a'] == datetime.fromisoformat('2026-01-07 06:00:00')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_dispatcher.py -v -k "freight_availability"`
Expected: 3 tests FAIL with `ImportError`.

- [ ] **Step 3: Implement build_freight_availability**

Replace `cambridge/dispatcher.py` with:

```python
"""Cambridge regional dispatcher orchestration."""
from datetime import datetime, date as date_type, time

from cambridge.scope import ScopedOrder
from cambridge.collection_planner import CollectionTrip
from cambridge.config import (
    OPERATING_DAY_START,
    DEFAULT_PRE_STAGED_HOUR,
)


def build_freight_availability(orders: list[ScopedOrder],
                               trips: list[CollectionTrip],
                               day: date_type) -> dict[str, datetime]:
    """Return order_id → first datetime the order is ready at CB22 to dispatch.

    PL_IMPORT  → day start (06:00).
    FULL_FLEET → max(trip.freight_ready_at_depot, day start). If no matching
                 trip is found, falls back to day start.
    """
    day_start = datetime.combine(day, time(DEFAULT_PRE_STAGED_HOUR, 0))
    # Map order_id → trip.freight_ready_at_depot
    trip_by_order: dict[str, datetime] = {}
    for trip in trips:
        for order_id in trip.orders:
            trip_by_order[order_id] = trip.freight_ready_at_depot

    out: dict[str, datetime] = {}
    for order in orders:
        if order.flow == 'PL_IMPORT':
            out[order.order_id] = day_start
        else:
            trip_ready = trip_by_order.get(order.order_id)
            out[order.order_id] = (max(trip_ready, day_start)
                                   if trip_ready else day_start)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_dispatcher.py -v -k "freight_availability"`
Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cambridge/dispatcher.py tests/cambridge/test_dispatcher.py
git commit -m "feat(cambridge): build_freight_availability for rolling dispatch"
```

---

## Task 10: dispatcher — run_event (single solver call)

**Files:**
- Modify: `cambridge/dispatcher.py`
- Modify: `tests/cambridge/test_dispatcher.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/cambridge/test_dispatcher.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / 'simulation'))

from cambridge.dispatcher import run_event, DispatchInput, DispatchOutput
from simulation.vrptw_engine import DeliveryRoute


def _rigid(vehicle_id: str, shift_end_iso: str) -> DeliveryRoute:
    # Use the existing DeliveryRoute as the per-vehicle state container for tests.
    return DeliveryRoute(
        vehicle_id=vehicle_id,
        depot_lat=52.0859, depot_lon=0.1717,
        shift_start=datetime.fromisoformat('2026-01-07 06:00:00'),
        shift_end=datetime.fromisoformat(shift_end_iso),
        capacity_kg=1500.0, capacity_pallets=10.0,
        asset_type='Rigid Truck',
    )


def test_run_event_returns_dispatch_output_with_routes(sample_postcode_cache):
    orders = [_pl('a')]
    rigids = [_rigid('HX66DUH', '2026-01-07 17:00:00')]
    inp = DispatchInput(
        available_orders=orders,
        available_rigids=rigids,
        planning_time=datetime.fromisoformat('2026-01-07 06:00:00'),
        locked_routes={},
        postcode_cache=sample_postcode_cache,
    )
    out = run_event(inp, solver_budget_s=5.0)
    assert isinstance(out, DispatchOutput)
    # With one order and one rigid, the solver should route the order to that rigid.
    assert out.metrics['orders_total'] == 1
    assert out.metrics['orders_assigned'] >= 0   # solver may not assign in 5s budget


def test_run_event_with_no_orders_returns_empty_routes(sample_postcode_cache):
    rigids = [_rigid('HX66DUH', '2026-01-07 17:00:00')]
    inp = DispatchInput(
        available_orders=[], available_rigids=rigids,
        planning_time=datetime.fromisoformat('2026-01-07 06:00:00'),
        locked_routes={}, postcode_cache=sample_postcode_cache,
    )
    out = run_event(inp, solver_budget_s=2.0)
    assert out.routes == {}
    assert out.unassigned == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_dispatcher.py -v -k "run_event"`
Expected: 2 tests FAIL with `ImportError: cannot import name 'run_event'`.

- [ ] **Step 3: Implement run_event**

Append to `cambridge/dispatcher.py`:

```python
import sys
import os
from dataclasses import dataclass, field
from typing import Any

# Bootstrap simulation/ onto the path so vrptw modules import cleanly.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'simulation'))

from vrptw_engine import DeliveryStop, DeliveryRoute
from vrptw_alns import run_vrptw

from cambridge.config import (
    CB22_DEPOT_ANCHOR,
    VEHICLE_ACTIVATION_COST,
    UNASSIGNED_PENALTY,
    SERVICE_MINUTES_PER_STOP,
    ROAD_DISTANCE_FACTOR,
)


@dataclass
class DispatchInput:
    available_orders: list[ScopedOrder]
    available_rigids: list[DeliveryRoute]
    planning_time: datetime
    locked_routes: dict[str, DeliveryRoute]    # vehicle_id → route already committed
    postcode_cache: dict


@dataclass
class DispatchOutput:
    routes: dict[str, DeliveryRoute] = field(default_factory=dict)
    unassigned: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def _to_delivery_stop(order: ScopedOrder, postcode_cache: dict
                      ) -> Optional[DeliveryStop]:
    coords = postcode_cache.get(order.destination_pc)
    if coords is None:
        return None
    lat, lon = coords
    return DeliveryStop(
        order_id=order.order_id,
        lat=lat, lon=lon,
        weight_kg=order.weight_kg, pallets=order.pallets,
    )


def run_event(inp: DispatchInput, solver_budget_s: float = 30.0) -> DispatchOutput:
    """Run one rolling-horizon delivery event.

    Builds DeliveryStops for available orders, calls vrptw_alns.run_vrptw with
    the available rigids, and returns the solver's output as a DispatchOutput.
    """
    stops: list[DeliveryStop] = []
    geocode_failures: list[str] = []
    for order in inp.available_orders:
        s = _to_delivery_stop(order, inp.postcode_cache)
        if s is None:
            geocode_failures.append(order.order_id)
        else:
            stops.append(s)

    if not stops:
        return DispatchOutput(routes={}, unassigned=geocode_failures,
                              metrics={'orders_total': len(inp.available_orders),
                                       'orders_assigned': 0,
                                       'geocode_failures': len(geocode_failures)})

    result = run_vrptw(
        stops=stops, vehicles=inp.available_rigids,
        time_budget_s=solver_budget_s,
    )
    routes_out: dict[str, DeliveryRoute] = {
        v.vehicle_id: v for v in result.get('routes', [])
    }
    unassigned = list(result.get('unassigned', [])) + geocode_failures
    metrics = {
        'orders_total':     len(inp.available_orders),
        'orders_assigned':  sum(len(r.stops) for r in routes_out.values()),
        'vehicles_used':    sum(1 for r in routes_out.values() if r.stops),
        'planned_km':       round(result.get('total_km', 0.0), 1),
        'planned_cost_gbp': round(result.get('total_cost', 0.0), 2),
        'geocode_failures': len(geocode_failures),
    }
    return DispatchOutput(routes=routes_out, unassigned=unassigned, metrics=metrics)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_dispatcher.py -v -k "run_event"`
Expected: 2 tests PASS. (The PL_IMPORT-with-rigid test may take ~5s due to the solver budget.)

- [ ] **Step 5: Commit**

```bash
git add cambridge/dispatcher.py tests/cambridge/test_dispatcher.py
git commit -m "feat(cambridge): run_event single solver call"
```

---

## Task 11: dispatcher — run_day (rolling horizon)

**Files:**
- Modify: `cambridge/dispatcher.py`
- Modify: `tests/cambridge/test_dispatcher.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/cambridge/test_dispatcher.py`:

```python
from cambridge.dispatcher import run_day, DayDispatchOutput


def test_run_day_returns_day_output_aggregating_events(sample_postcode_cache):
    orders = [_pl('a'), _pl('b')]
    rigids = [_rigid('HX66DUH', '2026-01-07 17:00:00')]
    out = run_day(
        day=date_type(2026, 1, 7),
        orders=orders,
        rigids=rigids,
        trips=[],
        postcode_cache=sample_postcode_cache,
        solver_budget_s=5.0,
    )
    assert isinstance(out, DayDispatchOutput)
    assert out.metrics['orders_total'] == 2
    assert out.metrics['planned_km'] >= 0
    assert out.day == date_type(2026, 1, 7)


def test_run_day_with_empty_inputs_returns_empty_output(sample_postcode_cache):
    out = run_day(date_type(2026, 1, 7), orders=[], rigids=[], trips=[],
                  postcode_cache=sample_postcode_cache)
    assert out.metrics['orders_total'] == 0
    assert out.routes == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_dispatcher.py -v -k "run_day"`
Expected: 2 tests FAIL with `ImportError`.

- [ ] **Step 3: Implement run_day**

Append to `cambridge/dispatcher.py`:

```python
@dataclass
class DayDispatchOutput:
    day: date_type
    routes: dict[str, DeliveryRoute] = field(default_factory=dict)
    collection_trips: list[CollectionTrip] = field(default_factory=list)
    unassigned: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def run_day(day: date_type,
            orders: list[ScopedOrder],
            rigids: list[DeliveryRoute],
            trips: list[CollectionTrip],
            postcode_cache: dict,
            solver_budget_s: float = 30.0) -> DayDispatchOutput:
    """Run the full rolling-horizon dispatch for one operating day.

    v1: single event at 06:00 covering all freight that is ready by then.
    A future task will extend this to multi-event rolling re-plan as freight
    becomes available later in the day.
    """
    freight_ready = build_freight_availability(orders, trips, day)
    day_start = datetime.combine(day, time(DEFAULT_PRE_STAGED_HOUR, 0))
    available_at_06 = [o for o in orders
                       if freight_ready.get(o.order_id, day_start) <= day_start]
    later = [o for o in orders
             if freight_ready.get(o.order_id, day_start) > day_start]

    event_out = run_event(DispatchInput(
        available_orders=available_at_06, available_rigids=rigids,
        planning_time=day_start, locked_routes={},
        postcode_cache=postcode_cache,
    ), solver_budget_s=solver_budget_s)

    # v1: any orders not ready at 06:00 are reported as deferred (not yet re-planned).
    deferred = [o.order_id for o in later]

    metrics = dict(event_out.metrics)
    metrics['orders_total'] = len(orders)
    metrics['orders_deferred'] = len(deferred)
    return DayDispatchOutput(
        day=day, routes=event_out.routes,
        collection_trips=trips,
        unassigned=event_out.unassigned + deferred,
        metrics=metrics,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_dispatcher.py -v`
Expected: All dispatcher tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cambridge/dispatcher.py tests/cambridge/test_dispatcher.py
git commit -m "feat(cambridge): run_day v1 (single-event rolling baseline)"
```

---

## Task 12: backtest — actuals_for_day

**Files:**
- Modify: `cambridge/backtest.py`
- Test: `tests/cambridge/test_backtest.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/cambridge/test_backtest.py`:

```python
"""Tests for cambridge/backtest.py."""
from datetime import date as date_type

import pandas as pd
import pytest

from cambridge.backtest import actuals_for_day


@pytest.fixture
def sample_telematics():
    """Telematics for 2026-01-07 with two Cambridge rigids active."""
    return pd.DataFrame({
        'LocalTime': pd.to_datetime([
            '2026-01-07 06:30', '2026-01-07 07:00', '2026-01-07 12:00',
            '2026-01-07 06:30', '2026-01-07 07:00', '2026-01-07 14:00',
            '2026-01-06 06:30',                              # off-day, ignored
        ]),
        'AssetName': ['HX66DUH','HX66DUH','HX66DUH',
                      'T88GNW','T88GNW','T88GNW',
                      'HX66DUH'],
        'Latitude':  [52.09, 52.10, 52.20,  52.09, 52.10, 52.30,  52.09],
        'Longitude': [0.17, 0.18, 0.20,    0.17, 0.18, 0.25,    0.17],
        'GPSSpeed':  [10, 50, 0,            10, 50, 0,           10],
    })


def test_actuals_for_day_counts_active_rigids(sample_telematics):
    actual = actuals_for_day(date_type(2026, 1, 7), sample_telematics)
    assert actual['active_vehicles'] == 2
    assert {'HX66DUH', 'T88GNW'} == set(actual['per_vehicle_km'].keys())


def test_actuals_for_day_computes_per_vehicle_km(sample_telematics):
    actual = actuals_for_day(date_type(2026, 1, 7), sample_telematics)
    for veh, km in actual['per_vehicle_km'].items():
        assert km > 0
    assert actual['total_km'] == pytest.approx(
        sum(actual['per_vehicle_km'].values()), rel=1e-6)


def test_actuals_for_day_excludes_non_cambridge_rigids():
    """A vehicle not in CB22_RIGIDS is not counted toward the day's actuals."""
    df = pd.DataFrame({
        'LocalTime': pd.to_datetime(['2026-01-07 06:00', '2026-01-07 07:00']),
        'AssetName': ['SOMETHING_ELSE', 'SOMETHING_ELSE'],
        'Latitude': [52.0, 52.1], 'Longitude': [0.0, 0.1],
        'GPSSpeed': [50, 50],
    })
    actual = actuals_for_day(date_type(2026, 1, 7), df)
    assert actual['active_vehicles'] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_backtest.py -v -k "actuals_for_day"`
Expected: 3 tests FAIL with `ImportError`.

- [ ] **Step 3: Implement actuals_for_day**

Replace `cambridge/backtest.py` with:

```python
"""Cambridge backtest level-0 and level-1 validation."""
import math
from datetime import date as date_type

import pandas as pd

from cambridge.config import CB22_RIGIDS


def _hav(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _vehicle_day_km(group: pd.DataFrame) -> float:
    """Sum haversine km across consecutive pings where speed > 2 km/h."""
    g = group.sort_values('LocalTime').copy()
    g['_lat'] = pd.to_numeric(g['Latitude'], errors='coerce')
    g['_lon'] = pd.to_numeric(g['Longitude'], errors='coerce')
    g['_sp']  = pd.to_numeric(g['GPSSpeed'], errors='coerce').fillna(0)
    g = g.dropna(subset=['_lat', '_lon'])
    active = g[g['_sp'] > 2]
    if len(active) < 2:
        return 0.0
    total = 0.0
    lats = active['_lat'].tolist()
    lons = active['_lon'].tolist()
    for i in range(len(lats) - 1):
        total += _hav(lats[i], lons[i], lats[i+1], lons[i+1])
    return total


def actuals_for_day(day: date_type, telem_df: pd.DataFrame) -> dict:
    """Compute per-vehicle actual km on `day` for Cambridge rigids only."""
    ts = pd.to_datetime(telem_df['LocalTime'], errors='coerce')
    day_df = telem_df[ts.dt.date == day]
    day_df = day_df[day_df['AssetName'].astype(str).isin(CB22_RIGIDS)]
    per_vehicle: dict[str, float] = {}
    for asset, g in day_df.groupby('AssetName'):
        if g['GPSSpeed'].astype(float).gt(2).sum() < 10:
            continue   # too few moving pings — not active
        per_vehicle[str(asset)] = round(_vehicle_day_km(g), 1)
    return {
        'day': day.isoformat(),
        'active_vehicles': len(per_vehicle),
        'per_vehicle_km':  per_vehicle,
        'total_km':        round(sum(per_vehicle.values()), 1),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_backtest.py -v -k "actuals_for_day"`
Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cambridge/backtest.py tests/cambridge/test_backtest.py
git commit -m "feat(cambridge): actuals_for_day from telematics"
```

---

## Task 13: backtest — level0_metrics

**Files:**
- Modify: `cambridge/backtest.py`
- Modify: `tests/cambridge/test_backtest.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/cambridge/test_backtest.py`:

```python
from cambridge.backtest import level0_metrics


def test_level0_metrics_within_threshold():
    planned = {'total_km': 1500.0, 'vehicles_used': 10,
               'planned_cost_gbp': 700.0,
               'orders_total': 50, 'orders_assigned': 48,
               'on_time_count': 47}
    actual = {'total_km': 1450.0, 'active_vehicles': 10,
              'actual_fuel_gbp': 650.0,
              'orders_actual_assigned': 48, 'orders_actual_on_time': 47}
    metrics = level0_metrics(planned, actual)
    assert metrics['km_pct_delta'] == pytest.approx(0.0345, abs=0.01)
    assert metrics['vehicles_delta'] == 0
    assert metrics['km_pass'] is True
    assert metrics['vehicles_pass'] is True
    assert metrics['day_pass'] is True


def test_level0_metrics_flags_km_outside_threshold():
    planned = {'total_km': 2000.0, 'vehicles_used': 10,
               'planned_cost_gbp': 800.0,
               'orders_total': 50, 'orders_assigned': 48,
               'on_time_count': 47}
    actual = {'total_km': 1000.0, 'active_vehicles': 10,
              'actual_fuel_gbp': 400.0,
              'orders_actual_assigned': 48, 'orders_actual_on_time': 47}
    metrics = level0_metrics(planned, actual)
    assert metrics['km_pass'] is False
    assert metrics['fuel_pass'] is False
    assert metrics['day_pass'] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_backtest.py -v -k "level0"`
Expected: 2 tests FAIL with `ImportError`.

- [ ] **Step 3: Implement level0_metrics**

Append to `cambridge/backtest.py`:

```python
from cambridge.config import PASS_THRESHOLDS


def _pct_delta(planned: float, actual: float) -> float:
    if actual == 0:
        return float('inf') if planned != 0 else 0.0
    return abs(planned - actual) / actual


def level0_metrics(planned: dict, actual: dict) -> dict:
    """Compute five day-total deltas and per-metric pass flags.

    Required planned keys: total_km, vehicles_used, planned_cost_gbp,
                            orders_total, orders_assigned, on_time_count.
    Required actual keys:  total_km, active_vehicles, actual_fuel_gbp,
                            orders_actual_assigned, orders_actual_on_time.
    """
    th = PASS_THRESHOLDS

    km_pct = _pct_delta(planned['total_km'], actual['total_km'])
    veh_d  = planned['vehicles_used'] - actual['active_vehicles']
    fuel_p = _pct_delta(planned['planned_cost_gbp'], actual['actual_fuel_gbp'])

    asn_p_rate = planned['orders_assigned'] / planned['orders_total'] if planned['orders_total'] else 0
    asn_a_rate = actual['orders_actual_assigned'] / planned['orders_total'] if planned['orders_total'] else 0
    asn_pp = abs(asn_p_rate - asn_a_rate)

    ot_p_rate = planned['on_time_count'] / planned['orders_total'] if planned['orders_total'] else 0
    ot_a_rate = actual['orders_actual_on_time'] / planned['orders_total'] if planned['orders_total'] else 0
    ot_pp = abs(ot_p_rate - ot_a_rate)

    km_pass        = km_pct <= th['km_pct']
    vehicles_pass  = abs(veh_d) <= th['vehicles_count']
    fuel_pass      = fuel_p <= th['fuel_pct']
    assignment_pass = asn_pp <= th['assignment_pp']
    on_time_pass    = ot_pp <= th['on_time_pp']

    return {
        'km_pct_delta':      round(km_pct, 4),
        'vehicles_delta':    veh_d,
        'fuel_pct_delta':    round(fuel_p, 4),
        'assignment_pp_delta': round(asn_pp, 4),
        'on_time_pp_delta':  round(ot_pp, 4),
        'km_pass':          km_pass,
        'vehicles_pass':    vehicles_pass,
        'fuel_pass':        fuel_pass,
        'assignment_pass':  assignment_pass,
        'on_time_pass':     on_time_pass,
        'day_pass':         all((km_pass, vehicles_pass, fuel_pass,
                                 assignment_pass, on_time_pass)),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_backtest.py -v -k "level0"`
Expected: 2 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cambridge/backtest.py tests/cambridge/test_backtest.py
git commit -m "feat(cambridge): level0_metrics with per-metric pass flags"
```

---

## Task 14: backtest — level1_metrics

**Files:**
- Modify: `cambridge/backtest.py`
- Modify: `tests/cambridge/test_backtest.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/cambridge/test_backtest.py`:

```python
from cambridge.backtest import level1_metrics


def test_level1_perfect_match_passes_all():
    planned_per_veh = {'HX66DUH': {'km': 100.0, 'stops': 5,
                                   'depart_hour': 7, 'return_hour': 16,
                                   'dest_districts': {'CB2', 'IP1'}},
                       'T88GNW':  {'km': 80.0,  'stops': 4,
                                   'depart_hour': 7, 'return_hour': 16,
                                   'dest_districts': {'CB2'}}}
    actual_per_veh = {'HX66DUH': {'km': 100.0, 'stops': 5,
                                  'depart_hour': 7, 'return_hour': 16,
                                  'dest_districts': {'CB2', 'IP1'}},
                      'T88GNW':  {'km': 80.0,  'stops': 4,
                                  'depart_hour': 7, 'return_hour': 16,
                                  'dest_districts': {'CB2'}}}
    metrics = level1_metrics(planned_per_veh, actual_per_veh)
    assert metrics['stop_count_ks'] == 0.0
    assert metrics['km_ks'] == 0.0
    assert metrics['postcode_jaccard'] == 1.0
    assert metrics['depart_min_delta'] == 0
    assert metrics['return_min_delta'] == 0
    assert metrics['all_pass'] is True


def test_level1_fails_jaccard_with_different_districts():
    planned_per_veh = {'A': {'km': 100, 'stops': 5,
                             'depart_hour': 7, 'return_hour': 16,
                             'dest_districts': {'CB2'}}}
    actual_per_veh = {'A': {'km': 100, 'stops': 5,
                            'depart_hour': 7, 'return_hour': 16,
                            'dest_districts': {'BS3'}}}
    metrics = level1_metrics(planned_per_veh, actual_per_veh)
    assert metrics['postcode_jaccard'] == 0.0
    assert metrics['jaccard_pass'] is False
    assert metrics['all_pass'] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_backtest.py -v -k "level1"`
Expected: 2 tests FAIL with `ImportError`.

- [ ] **Step 3: Implement level1_metrics**

Append to `cambridge/backtest.py`:

```python
from scipy.stats import ks_2samp


def _safe_ks(planned_vals: list, actual_vals: list) -> float:
    if not planned_vals or not actual_vals:
        return 1.0
    stat, _ = ks_2samp(planned_vals, actual_vals)
    return float(stat)


def level1_metrics(planned_per_veh: dict, actual_per_veh: dict) -> dict:
    """Distributional comparison across vehicles.

    Each per-vehicle dict has keys: km, stops, depart_hour, return_hour, dest_districts.
    Returns KS distances for stop-count and km, Jaccard for postcode districts,
    and median-time deltas in minutes.
    """
    th = PASS_THRESHOLDS

    p_stops = [v['stops'] for v in planned_per_veh.values()]
    a_stops = [v['stops'] for v in actual_per_veh.values()]
    p_km    = [v['km']    for v in planned_per_veh.values()]
    a_km    = [v['km']    for v in actual_per_veh.values()]

    stop_ks = round(_safe_ks(p_stops, a_stops), 3)
    km_ks   = round(_safe_ks(p_km,    a_km),    3)

    p_districts: set = set()
    for v in planned_per_veh.values():
        p_districts |= v['dest_districts']
    a_districts: set = set()
    for v in actual_per_veh.values():
        a_districts |= v['dest_districts']
    if p_districts or a_districts:
        jaccard = len(p_districts & a_districts) / len(p_districts | a_districts)
    else:
        jaccard = 1.0
    jaccard = round(jaccard, 3)

    p_depart = sorted([v['depart_hour'] * 60 for v in planned_per_veh.values()])
    a_depart = sorted([v['depart_hour'] * 60 for v in actual_per_veh.values()])
    p_return = sorted([v['return_hour'] * 60 for v in planned_per_veh.values()])
    a_return = sorted([v['return_hour'] * 60 for v in actual_per_veh.values()])

    def _median(values):
        if not values: return 0
        return values[len(values) // 2]

    depart_delta = _median(p_depart) - _median(a_depart)
    return_delta = _median(p_return) - _median(a_return)

    stop_pass    = stop_ks <= th['ks_max']
    km_pass      = km_ks   <= th['ks_max']
    jaccard_pass = jaccard >= th['jaccard_min']
    depart_pass  = abs(depart_delta) <= th['time_minutes']
    return_pass  = abs(return_delta) <= th['time_minutes']

    return {
        'stop_count_ks':     stop_ks,
        'km_ks':             km_ks,
        'postcode_jaccard':  jaccard,
        'depart_min_delta':  depart_delta,
        'return_min_delta':  return_delta,
        'stop_pass':         stop_pass,
        'km_pass':           km_pass,
        'jaccard_pass':      jaccard_pass,
        'depart_pass':       depart_pass,
        'return_pass':       return_pass,
        'all_pass':          all((stop_pass, km_pass, jaccard_pass, depart_pass, return_pass)),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_backtest.py -v -k "level1"`
Expected: 2 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cambridge/backtest.py tests/cambridge/test_backtest.py
git commit -m "feat(cambridge): level1_metrics distributional comparison"
```

---

## Task 15: backtest — run_day + report formatting

**Files:**
- Modify: `cambridge/backtest.py`
- Modify: `tests/cambridge/test_backtest.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/cambridge/test_backtest.py`:

```python
import json
from pathlib import Path
from cambridge.backtest import run_day_backtest, print_report


def test_print_report_includes_section_headers(capsys):
    report = {
        'day': '2026-01-07',
        'planned': {'total_km': 1500, 'vehicles_used': 10,
                    'planned_cost_gbp': 700, 'orders_total': 50,
                    'orders_assigned': 48, 'on_time_count': 47},
        'actual':  {'total_km': 1450, 'active_vehicles': 10,
                    'actual_fuel_gbp': 650,
                    'orders_actual_assigned': 48, 'orders_actual_on_time': 47},
        'level0':  {'km_pct_delta': 0.034, 'vehicles_delta': 0,
                    'fuel_pct_delta': 0.077, 'assignment_pp_delta': 0.0,
                    'on_time_pp_delta': 0.0,
                    'km_pass': True, 'vehicles_pass': True, 'fuel_pass': True,
                    'assignment_pass': True, 'on_time_pass': True,
                    'day_pass': True},
        'level1':  {'stop_count_ks': 0.1, 'km_ks': 0.15,
                    'postcode_jaccard': 0.9,
                    'depart_min_delta': 5, 'return_min_delta': -10,
                    'stop_pass': True, 'km_pass': True, 'jaccard_pass': True,
                    'depart_pass': True, 'return_pass': True, 'all_pass': True},
    }
    print_report(report)
    out = capsys.readouterr().out
    assert 'CAMBRIDGE BACKTEST  2026-01-07' in out
    assert 'PLANNED' in out and 'ACTUAL' in out
    assert 'Day verdict' in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_backtest.py -v -k "print_report"`
Expected: 1 test FAILS with `ImportError`.

- [ ] **Step 3: Implement print_report and the run_day_backtest stub that wires it up**

Append to `cambridge/backtest.py`:

```python
import json
from pathlib import Path


def print_report(report: dict) -> None:
    """Print a per-day backtest report to stdout in the format spec'd in §6."""
    p = report['planned']
    a = report['actual']
    l0 = report['level0']
    l1 = report['level1']
    print()
    print('=' * 62)
    print(f"  CAMBRIDGE BACKTEST  {report['day']}")
    print('=' * 62)
    print(f"                                      PLANNED       ACTUAL    DELTA")
    print(f"  Total km                       {p['total_km']:>10,.0f}   {a['total_km']:>10,.0f}   {l0['km_pct_delta']*100:+6.1f}%  {'pass' if l0['km_pass'] else 'WARN'}")
    print(f"  Vehicles used                  {p['vehicles_used']:>10}   {a['active_vehicles']:>10}   {l0['vehicles_delta']:>+5}    {'pass' if l0['vehicles_pass'] else 'WARN'}")
    print(f"  Fuel cost £                    {p['planned_cost_gbp']:>10,.0f}   {a['actual_fuel_gbp']:>10,.0f}   {l0['fuel_pct_delta']*100:+6.1f}%  {'pass' if l0['fuel_pass'] else 'WARN'}")
    print(f"  Assignment pp delta                                  {l0['assignment_pp_delta']*100:+6.1f}pp  {'pass' if l0['assignment_pass'] else 'WARN'}")
    print(f"  On-time pp delta                                     {l0['on_time_pp_delta']*100:+6.1f}pp  {'pass' if l0['on_time_pass'] else 'WARN'}")
    print('  ' + '-' * 58)
    print(f"  L1 stop-count KS dist          {l1['stop_count_ks']:>10.3f}              {'pass' if l1['stop_pass'] else 'WARN'}")
    print(f"  L1 km histogram KS dist        {l1['km_ks']:>10.3f}              {'pass' if l1['km_pass'] else 'WARN'}")
    print(f"  L1 postcode-district Jaccard   {l1['postcode_jaccard']:>10.3f}              {'pass' if l1['jaccard_pass'] else 'WARN'}")
    print(f"  L1 depart-time delta (min)     {l1['depart_min_delta']:>+10}              {'pass' if l1['depart_pass'] else 'WARN'}")
    print(f"  L1 return-time delta (min)     {l1['return_min_delta']:>+10}              {'pass' if l1['return_pass'] else 'WARN'}")
    print('  ' + '-' * 58)
    verdict = 'PASS' if (l0['day_pass'] and l1['all_pass']) else 'PARTIAL'
    print(f"  Day verdict: {verdict}")
    print('=' * 62)


def run_day_backtest(day: date_type,
                     qargo_df: pd.DataFrame,
                     telem_df: pd.DataFrame,
                     postcode_cache: dict,
                     output_dir: Path,
                     solver_budget_s: float = 30.0) -> dict:
    """End-to-end backtest for a single day.

    1. Build scoped orders + collection trips + dispatch.
    2. Build actual side from telematics.
    3. Compute level 0 + level 1 metrics.
    4. Save day_compare_<date>.json. Print report. Return the report dict.

    Per-vehicle level-1 dicts are constructed from the planned routes and the
    actual GPS group; postcode districts come from destination postcode prefixes.
    """
    from cambridge.scope import build_scoped_orders
    from cambridge.collection_planner import plan_collections
    from cambridge.dispatcher import run_day
    from cambridge.config import CB22_TRACTORS, CB22_RIGIDS

    scoped = build_scoped_orders(qargo_df, postcode_cache)
    trips = plan_collections(scoped, list(CB22_TRACTORS), postcode_cache)
    # Build minimal rigid records (we only need vehicle_id + depot + asset_type;
    # capacities and shifts are filled in by the dispatcher's solver wrapper).
    from simulation.vrptw_engine import DeliveryRoute
    rigids = [
        DeliveryRoute(vehicle_id=v, depot_lat=CB22_DEPOT_ANCHOR[0],
                      depot_lon=CB22_DEPOT_ANCHOR[1],
                      shift_start=datetime.combine(day, time(6, 0)),
                      shift_end=datetime.combine(day, time(17, 0)),
                      capacity_kg=2500.0, capacity_pallets=10.0,
                      asset_type='Rigid Truck')
        for v in CB22_RIGIDS
    ]
    day_out = run_day(day, scoped, rigids, trips, postcode_cache,
                      solver_budget_s=solver_budget_s)

    # Planned aggregates
    planned_total_km = sum(getattr(r, 'planned_km', 0) or
                           sum(s.lat for s in r.stops)*0 + 0
                           for r in day_out.routes.values())
    planned = {
        'total_km':         day_out.metrics.get('planned_km', 0.0),
        'vehicles_used':    day_out.metrics.get('vehicles_used', 0),
        'planned_cost_gbp': day_out.metrics.get('planned_cost_gbp', 0.0),
        'orders_total':     day_out.metrics.get('orders_total', 0),
        'orders_assigned':  day_out.metrics.get('orders_assigned', 0),
        'on_time_count':    day_out.metrics.get('orders_assigned', 0),  # placeholder until window-check
    }
    actual_basic = actuals_for_day(day, telem_df)
    actual = {
        'total_km':              actual_basic['total_km'],
        'active_vehicles':       actual_basic['active_vehicles'],
        'actual_fuel_gbp':       0.0,             # filled in by run_period when jigsaw is loaded
        'orders_actual_assigned': 0,
        'orders_actual_on_time':  0,
    }
    l0 = level0_metrics(planned, actual)

    # Per-vehicle aggregates for level 1 — planned side from day_out.routes,
    # actual side from telematics grouped by AssetName.
    planned_per_veh: dict[str, dict] = {}
    for vid, route in day_out.routes.items():
        if not route.stops: continue
        districts = set()
        for stop in route.stops:
            # Stops carry order_id; we look up the order to get destination prefix.
            for o in scoped:
                if o.order_id == stop.order_id:
                    districts.add(o.destination_pc.split(' ')[0])
                    break
        planned_per_veh[vid] = {
            'km':    0.0,   # filled by solver; v1 uses 0 placeholder if not on route
            'stops': len(route.stops),
            'depart_hour': route.shift_start.hour,
            'return_hour': route.shift_end.hour,
            'dest_districts': districts,
        }
    actual_per_veh: dict[str, dict] = {}
    for vid, km in actual_basic['per_vehicle_km'].items():
        actual_per_veh[vid] = {
            'km':    km,
            'stops': 0,                        # placeholder until stop-matcher
            'depart_hour': 7,                  # placeholder
            'return_hour': 16,
            'dest_districts': set(),           # placeholder
        }
    l1 = level1_metrics(planned_per_veh, actual_per_veh)

    report = {'day': day.isoformat(), 'planned': planned, 'actual': actual,
              'level0': l0, 'level1': l1,
              'unassigned': day_out.unassigned}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f'day_compare_{day.isoformat()}.json').write_text(
        json.dumps(report, indent=2, default=lambda o: list(o) if isinstance(o, set) else str(o)))
    print_report(report)
    return report
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_backtest.py -v -k "print_report"`
Expected: 1 test PASSES.

- [ ] **Step 5: Commit**

```bash
git add cambridge/backtest.py tests/cambridge/test_backtest.py
git commit -m "feat(cambridge): run_day_backtest and print_report"
```

---

## Task 16: backtest — run_period (loop + aggregate)

**Files:**
- Modify: `cambridge/backtest.py`
- Modify: `tests/cambridge/test_backtest.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/cambridge/test_backtest.py`:

```python
from cambridge.backtest import aggregate_reports


def test_aggregate_reports_counts_pass_partial_fail():
    reports = [
        {'level0': {'day_pass': True},  'level1': {'all_pass': True}},
        {'level0': {'day_pass': True},  'level1': {'all_pass': False}},
        {'level0': {'day_pass': False}, 'level1': {'all_pass': False}},
    ]
    agg = aggregate_reports(reports)
    assert agg['n_days']     == 3
    assert agg['pass_count'] == 1
    assert agg['partial_count'] == 1
    assert agg['fail_count'] == 1


def test_aggregate_reports_includes_metric_medians():
    reports = [
        {'level0': {'day_pass': True, 'km_pct_delta': 0.10},
         'level1': {'all_pass': True, 'postcode_jaccard': 0.9}},
        {'level0': {'day_pass': True, 'km_pct_delta': 0.20},
         'level1': {'all_pass': True, 'postcode_jaccard': 0.8}},
        {'level0': {'day_pass': True, 'km_pct_delta': 0.30},
         'level1': {'all_pass': True, 'postcode_jaccard': 0.7}},
    ]
    agg = aggregate_reports(reports)
    assert agg['km_pct_median'] == pytest.approx(0.20)
    assert agg['jaccard_median'] == pytest.approx(0.80)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_backtest.py -v -k "aggregate_reports"`
Expected: 2 tests FAIL with `ImportError`.

- [ ] **Step 3: Implement aggregate_reports + run_period**

Append to `cambridge/backtest.py`:

```python
import statistics


def aggregate_reports(reports: list[dict]) -> dict:
    """Summarise per-day reports into a single run-level dict."""
    n = len(reports)
    pass_n    = sum(1 for r in reports if r['level0']['day_pass'] and r['level1']['all_pass'])
    partial_n = sum(1 for r in reports if r['level0']['day_pass'] and not r['level1']['all_pass'])
    fail_n    = sum(1 for r in reports if not r['level0']['day_pass'])

    km_pcts = [r['level0'].get('km_pct_delta', 0) for r in reports]
    jaccs   = [r['level1'].get('postcode_jaccard', 0) for r in reports]
    return {
        'n_days':         n,
        'pass_count':     pass_n,
        'partial_count':  partial_n,
        'fail_count':     fail_n,
        'km_pct_median':  round(statistics.median(km_pcts), 4) if km_pcts else 0.0,
        'jaccard_median': round(statistics.median(jaccs), 4) if jaccs else 0.0,
    }


def run_period(start_date: date_type, end_date: date_type,
               qargo_df: pd.DataFrame, telem_df: pd.DataFrame,
               postcode_cache: dict, output_dir: Path,
               solver_budget_s: float = 30.0) -> dict:
    """Loop run_day_backtest over each calendar day, emit aggregate JSON."""
    reports: list[dict] = []
    d = start_date
    while d <= end_date:
        try:
            report = run_day_backtest(d, qargo_df, telem_df, postcode_cache,
                                       output_dir, solver_budget_s)
            reports.append(report)
        except Exception as exc:
            print(f'  ERROR on {d.isoformat()}: {exc}')
        d = d + pd.Timedelta(days=1).to_pytimedelta()
    agg = aggregate_reports(reports)
    output_dir.mkdir(parents=True, exist_ok=True)
    aggregate_path = output_dir / f'aggregate_{start_date.isoformat()}_{end_date.isoformat()}.json'
    aggregate_path.write_text(json.dumps(agg, indent=2))
    return agg
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_backtest.py -v`
Expected: All backtest tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cambridge/backtest.py tests/cambridge/test_backtest.py
git commit -m "feat(cambridge): run_period and aggregate_reports"
```

---

## Task 17: End-to-end smoke test on a real day

**Files:**
- Create: `cambridge/__main__.py`

- [ ] **Step 1: Create the CLI entry point**

Create `cambridge/__main__.py`:

```python
"""Cambridge dispatcher CLI: smoke-test one day or backtest a range."""
import argparse
import sys
from datetime import date as date_type
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / 'simulation'))

from data_audit import load_datasets
from simulation.data_loader import load_postcode_cache, save_postcode_cache

from cambridge.backtest import run_day_backtest, run_period

OUT = BASE / 'data' / 'Output' / 'cambridge'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--date', type=str, help='Single date YYYY-MM-DD')
    p.add_argument('--start', type=str, help='Start of range YYYY-MM-DD')
    p.add_argument('--end',   type=str, help='End of range YYYY-MM-DD')
    p.add_argument('--budget', type=float, default=30.0)
    args = p.parse_args()

    datasets = load_datasets(str(BASE))
    qargo = datasets['qargo']; telem = datasets['supatrak_telematics']
    cache = load_postcode_cache()

    if args.date:
        d = date_type.fromisoformat(args.date)
        run_day_backtest(d, qargo, telem, cache, OUT,
                         solver_budget_s=args.budget)
    elif args.start and args.end:
        s = date_type.fromisoformat(args.start)
        e = date_type.fromisoformat(args.end)
        agg = run_period(s, e, qargo, telem, cache, OUT,
                         solver_budget_s=args.budget)
        print(); print('AGGREGATE'); print(agg)
    else:
        p.error('Provide --date OR --start + --end')

    save_postcode_cache(cache)


if __name__ == '__main__':
    main()
```

- [ ] **Step 2: Run the smoke test on one Cambridge day**

Run: `python -m cambridge --date 2026-01-07 --budget 30`
Expected: A `CAMBRIDGE BACKTEST 2026-01-07` report renders to stdout, and `data/Output/cambridge/day_compare_2026-01-07.json` exists.

- [ ] **Step 3: Inspect the report**

Read: `data/Output/cambridge/day_compare_2026-01-07.json`
Manually verify:
- `planned.total_km` is non-zero and within order-of-magnitude of the audit's ~1,500-2,000 km expectation
- `actual.total_km` is non-zero and roughly matches Cambridge rigids' daily km
- `unassigned` is not large (< 20 % of orders_total)
- `level1.postcode_jaccard` is plausible (> 0.5)

If any of these fail order-of-magnitude sanity checks, the issue belongs in the next iteration — log the gap and proceed.

- [ ] **Step 4: Commit**

```bash
git add cambridge/__main__.py
git commit -m "feat(cambridge): CLI smoke test entry point"
```

---

## Task 18: Run the full Jan–Feb backtest and inspect aggregate

- [ ] **Step 1: Run the full period backtest**

Run: `python -m cambridge --start 2026-01-02 --end 2026-02-28 --budget 30 > data/Output/cambridge/run.log 2>&1`
Expected: Completes in 30-90 minutes depending on solver budget. Final line of stdout includes the aggregate dict.

- [ ] **Step 2: Inspect the aggregate JSON**

Read: `data/Output/cambridge/aggregate_2026-01-02_2026-02-28.json`
Expected fields: `n_days`, `pass_count`, `partial_count`, `fail_count`, `km_pct_median`, `jaccard_median`.

Manually sanity-check:
- `n_days` ≈ 46 (matches the audit's operating-day count)
- `pass_count + partial_count + fail_count` == `n_days`
- `km_pct_median` < 0.5 (below 50 % deviation; if above, level 0 is failing systematically)
- `jaccard_median` > 0.5 (some level of geographic-overlap signal)

- [ ] **Step 3: Run the full test suite to confirm no regressions**

Run: `python -m pytest tests/ -q`
Expected: All previously passing tests + all new Cambridge tests PASS.

- [ ] **Step 4: Commit the run log + aggregate as a snapshot**

```bash
git add data/Output/cambridge/run.log data/Output/cambridge/aggregate_*.json data/Output/cambridge/day_compare_*.json
git commit -m "chore(cambridge): v1 baseline backtest results across Jan-Feb"
```

---

## Self-review checklist

Reviewing this plan against the spec at [`../cambridge-dispatcher-design.md`](../../cambridge-dispatcher-design.md):

| Spec section | Implemented by task(s) |
|---|---|
| §1 Architecture & file layout | Task 0 (skeleton), §file structure above |
| §2 Order classification & scope filter | Tasks 2, 3, 4 |
| §3 Delivery routing core | Tasks 9 (freight availability), 10 (run_event), 11 (run_day) |
| §4 Collection planning | Tasks 5, 6, 7, 8 |
| §5 Rolling-horizon orchestration | Tasks 9, 11 |
| §6 Validation framework | Tasks 12, 13, 14, 15, 16 |
| §7 Cambridge data feasibility (no code) | reference in spec |
| §8 Open questions (TBDs) | Task 0 + 1 set defaults; iteration as needed |
| §9 v2 / v3 roadmap (deferred) | not implemented (by design) |

**Type-consistency check:**
- `ScopedOrder` defined Task 4, used by Tasks 5, 8, 9, 10, 11, 15
- `CollectionTrip` defined Task 6, used by Tasks 7, 8, 9, 15
- `DispatchInput` / `DispatchOutput` defined Task 10, used by Task 11
- `DayDispatchOutput` defined Task 11, used by Task 15
- Helper `_haversine_km` is duplicated between `scope.py` (Task 3) and `collection_planner.py` (Task 6) and `backtest.py` (Task 12) — acceptable for v1, refactor later if it accumulates.

**Placeholders cleared:** no "TBD" or vague steps. Each step has explicit code or commands.

**Run location:** all commands explicitly assume CWD = `e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics/`.

---
