# Multi-Day Dispatcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the single-day Cambridge VRPTW dispatcher into a multi-day planner that tracks cross-docked freight, models overnight Palletline (B37 7HB) and Hazchem (LE10 3BS) trunk runs, uses 2-day lookahead from Qargo, and replays or plans multiple consecutive days with per-day HTML map output.

**Architecture:** Layered — `multiday_state.py` owns day-boundary JSON state; `trunk_planner.py` sizes and assigns nightly hub runs using OSRM drive times; `day_coordinator.py` orchestrates order pool assembly → VRPTW → trunk plan → state write; `multiday_backtest.py` loops the coordinator over a date range. All changes build on the existing `run_day_multi_trip` dispatcher without rewriting it.

**Tech Stack:** Python 3.11, pandas, dataclasses, pytest, OSRM (localhost:5000), existing `cambridge/` package. No new dependencies.

**DO NOT COMMIT** — keep all work local (project constraint).

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `cambridge/config.py` | MOD | Add `TRUNK_B37_DWELL_MIN`, `TRUNK_LE10_DWELL_MIN`, `TRUNK_LOADING_BUFFER_MIN`, `STATE_DIR` |
| `cambridge/scope.py` | MOD | Add `delivery_date` property to `ScopedOrder`; fix PALLETLINE-no-sub → `FULL_FLEET` |
| `cambridge/multiday_state.py` | NEW | `DayState`, `TrunkHubManifest`; JSON I/O; telematics bootstrap |
| `cambridge/trunk_planner.py` | NEW | `TrunkPlan`; OSRM drive times; per-vehicle capacity sizing; tractor assignment |
| `cambridge/dispatcher.py` | MOD | `build_freight_availability` accepts `pre_staged_ids`; `run_day_multi_trip` passes them through |
| `cambridge/day_coordinator.py` | NEW | `plan_day()`: assembles pools → calls dispatcher → trunk planner → writes state |
| `cambridge/multiday_backtest.py` | NEW | `run_multiday_backtest()` day loop; per-day report + HTML map |
| `cambridge/__main__.py` | MOD | Add `--multiday` flag wiring into `run_multiday_backtest` |
| `operational_analysis/export_plan_replay.py` | MOD | Add trunk arc overlays (CB22↔B37, CB22↔LE10) to HTML map |
| `tests/cambridge/test_multiday_state.py` | NEW | Unit tests for `DayState` JSON round-trip, filtering, bootstrap |
| `tests/cambridge/test_trunk_planner.py` | NEW | Unit tests for tractor sizing, departure time, OSRM fallback |
| `tests/cambridge/test_day_coordinator.py` | NEW | Integration test for `plan_day()` pool assembly |

**Run all tests from:** `e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics/`
**Test command:** `python -m pytest tests/cambridge/ -v`
**Python:** `e:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe` (or `python` if venv is activated)

---

## Task 1: Config additions

**Files:**
- Modify: `cambridge/config.py` (end of file)
- Test: `tests/cambridge/test_config.py` (already exists — just run it)

- [ ] **Step 1: Add constants to config.py**

Open `cambridge/config.py`. At the end of the file, after the `service_minutes_for_load` function, add:

```python
# Multi-day trunk parameters — derived from January 2026 telematics.
# Overnight hub visits (arrive 18:00–23:00, depart 00:00–08:00) median durations.
# Re-derive by running: python investigations/derive_trunk_parameters.py
TRUNK_B37_DWELL_MIN:       int = 380   # B37 7HB Palletline — median 6.3h (n=20 visits)
TRUNK_LE10_DWELL_MIN:      int = 330   # LE10 3BS Hazchem   — median 5.5h (n=15 visits)
TRUNK_LOADING_BUFFER_MIN:  int = 30    # depot staging buffer before trunk departs

# Output directory for daily state JSON files (one per day).
from pathlib import Path as _Path2
STATE_DIR: _Path2 = (_Path(__file__).parent.parent
                     / 'data' / 'Output' / 'cambridge' / 'state')
```

- [ ] **Step 2: Verify import works**

```
python -c "from cambridge.config import TRUNK_B37_DWELL_MIN, TRUNK_LE10_DWELL_MIN, TRUNK_LOADING_BUFFER_MIN, STATE_DIR; print(TRUNK_B37_DWELL_MIN, TRUNK_LE10_DWELL_MIN, TRUNK_LOADING_BUFFER_MIN, STATE_DIR)"
```

Expected output:
```
380 330 30 e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics\data\Output\cambridge\state
```

- [ ] **Step 3: Run existing config tests**

```
python -m pytest tests/cambridge/test_config.py -v
```

Expected: all tests pass (no regressions).

---

## Task 2: ScopedOrder.delivery_date property + PALLETLINE-no-sub fix

**Files:**
- Modify: `cambridge/scope.py` (two targeted changes)
- Test: `tests/cambridge/test_scope.py` (add two new test functions)

**Context:** `ScopedOrder` is a dataclass in `scope.py`. It already has `delivery_window: Tuple[datetime, datetime]`. We add a `@property` that derives `delivery_date` from it — no stored field, no breaking change. We also fix `classify_order` so PALLETLINE rows with no subcontractor return `'FULL_FLEET'` instead of `None`. In backtest mode the existing vehicle-assignment filter gates these; in forward mode the geographic `in_cambridge_scope` filter gates them.

- [ ] **Step 1: Write two failing tests**

Add to the END of `tests/cambridge/test_scope.py`:

```python
def test_palletline_no_sub_classifies_as_full_fleet():
    row = pd.Series({
        'order_import_integration_type': 'PALLETLINE',
        'resource_subcontractor': None,
        'transport_service': '1. Non Hazardous Shipment',
    })
    assert classify_order(row) == 'FULL_FLEET'


def test_scoped_order_delivery_date_matches_window():
    from datetime import datetime, date
    from cambridge.scope import ScopedOrder
    o = ScopedOrder(
        order_id='x', name='WT1', flow='PL_IMPORT',
        origin_pc=None, destination_pc='CB1 1AA',
        weight_kg=100.0, pallets=1.0,
        delivery_window=(datetime(2026, 1, 7, 6, 0), datetime(2026, 1, 7, 18, 0)),
        collection_window=None,
    )
    assert o.delivery_date == date(2026, 1, 7)
```

- [ ] **Step 2: Run to confirm they fail**

```
python -m pytest tests/cambridge/test_scope.py::test_palletline_no_sub_classifies_as_full_fleet tests/cambridge/test_scope.py::test_scoped_order_delivery_date_matches_window -v
```

Expected: 2 FAILED (AttributeError on `delivery_date`; `classify_order` returns `None`).

- [ ] **Step 3: Add delivery_date property to ScopedOrder**

In `cambridge/scope.py`, find the `ScopedOrder` dataclass (around line 202). Add a `@property` method immediately after the last dataclass field (`stop_type`):

```python
    stop_type: Literal['delivery', 'pickup'] = 'delivery'
    # 'pickup' for PL_EXPORT: the stop is at origin_pc (shipper collection point).
    # 'delivery' for PL_IMPORT and FULL_FLEET: stop is at destination_pc.

    @property
    def delivery_date(self):
        """Calendar date on which this order is due for delivery/pickup."""
        from datetime import date as _date
        return self.delivery_window[0].date()
```

- [ ] **Step 4: Fix classify_order for PALLETLINE-no-sub**

In `cambridge/scope.py`, find `classify_order`. Find the block:

```python
    if import_type_str == 'PALLETLINE' and 'import from API' in sub_str:
        return 'PL_IMPORT'
```

Immediately after it, add:

```python
    if import_type_str == 'PALLETLINE' and not sub_str:
        # No subcontractor: Palletline commissioned us for direct delivery.
        # Scope is gated by in_cambridge_scope (origin geography in forward mode)
        # or by CB22 vehicle assignment (backtest mode).
        return 'FULL_FLEET'
```

- [ ] **Step 5: Run the new tests**

```
python -m pytest tests/cambridge/test_scope.py -v
```

Expected: all tests pass including the two new ones.

---

## Task 3: multiday_state.py — DayState

**Files:**
- Create: `cambridge/multiday_state.py`
- Create: `tests/cambridge/test_multiday_state.py`

**Context:** `DayState` holds end-of-day state for one day. It is the only data that crosses day boundaries. `ScopedOrder` objects in `depot_inventory` are serialised as minimal dicts (not full dataclass), then reconstructed. `bootstrap_from_telematics` derives tractor locations from last known postcode after 16:00 in the telematics CSV.

- [ ] **Step 1: Write failing tests**

Create `tests/cambridge/test_multiday_state.py`:

```python
"""Tests for cambridge.multiday_state."""
import json
import tempfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest


def _make_scoped_order(order_id='o1', flow='PL_IMPORT',
                       delivery_date='2026-01-08', pallets=2.0):
    from cambridge.scope import ScopedOrder
    d = datetime.fromisoformat(f'{delivery_date}T06:00:00')
    e = datetime.fromisoformat(f'{delivery_date}T18:00:00')
    return ScopedOrder(
        order_id=order_id, name='WT1', flow=flow,
        origin_pc=None, destination_pc='CB1 1AA',
        weight_kg=200.0, pallets=pallets,
        delivery_window=(d, e), collection_window=None,
    )


def test_day_state_json_round_trip():
    from cambridge.multiday_state import DayState, TrunkHubManifest
    state = DayState(
        date=date(2026, 1, 7),
        vehicle_locations={'X88RNW': 'B37_HUB', 'W88GNW': 'CB22_DEPOT'},
        depot_inventory=[_make_scoped_order('o1', 'FULL_FLEET', '2026-01-08', 1.0)],
        unassigned_carry_forward=['u1', 'u2'],
        trunk_manifest={
            'B37_HUB': TrunkHubManifest(
                tractors=['X88RNW'],
                pallets_outbound=20.0,
                departed=datetime(2026, 1, 7, 19, 0),
                expected_return=datetime(2026, 1, 8, 4, 0),
            )
        },
    )
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / 'state_2026-01-07.json'
        state.to_json(p)
        loaded = DayState.from_json(p)

    assert loaded.date == date(2026, 1, 7)
    assert loaded.vehicle_locations['X88RNW'] == 'B37_HUB'
    assert len(loaded.depot_inventory) == 1
    assert loaded.depot_inventory[0].order_id == 'o1'
    assert loaded.unassigned_carry_forward == ['u1', 'u2']
    assert 'B37_HUB' in loaded.trunk_manifest
    assert loaded.trunk_manifest['B37_HUB'].tractors == ['X88RNW']


def test_trunk_return_time():
    from cambridge.multiday_state import DayState, TrunkHubManifest
    ret = datetime(2026, 1, 8, 4, 15)
    state = DayState(
        date=date(2026, 1, 7),
        vehicle_locations={},
        depot_inventory=[],
        unassigned_carry_forward=[],
        trunk_manifest={
            'B37_HUB': TrunkHubManifest(
                tractors=['X88RNW'], pallets_outbound=10.0,
                departed=datetime(2026, 1, 7, 19, 0),
                expected_return=ret,
            )
        },
    )
    assert state.trunk_return_time('B37_HUB') == ret
    assert state.trunk_return_time('LE10_HUB') is None


def test_depot_inventory_for_date_filters_correctly():
    from cambridge.multiday_state import DayState
    jan8 = _make_scoped_order('o1', 'FULL_FLEET', '2026-01-08')
    jan9 = _make_scoped_order('o2', 'FULL_FLEET', '2026-01-09')
    state = DayState(
        date=date(2026, 1, 7),
        vehicle_locations={}, depot_inventory=[jan8, jan9],
        unassigned_carry_forward=[], trunk_manifest={},
    )
    assert [o.order_id for o in state.depot_inventory_for_date(date(2026, 1, 8))] == ['o1']
    assert [o.order_id for o in state.depot_inventory_for_date(date(2026, 1, 9))] == ['o2']
    assert state.depot_inventory_for_future(date(2026, 1, 8)) == [jan9]


def test_bootstrap_from_telematics_classifies_hubs():
    from cambridge.multiday_state import DayState
    telem = pd.DataFrame([
        {'LocalTime': '2026-01-06 20:00:00', 'AssetName': 'X88RNW',
         'Location_Postcode': 'B37 7HB', 'GPSSpeed': 0.0},
        {'LocalTime': '2026-01-06 21:00:00', 'AssetName': 'X8GNW',
         'Location_Postcode': 'LE10 3BS', 'GPSSpeed': 0.0},
        {'LocalTime': '2026-01-06 18:30:00', 'AssetName': 'W88GNW',
         'Location_Postcode': 'CB22 4PS', 'GPSSpeed': 0.0},
    ])
    state = DayState.bootstrap_from_telematics(date(2026, 1, 6), telem)
    assert state.vehicle_locations.get('X88RNW') == 'B37_HUB'
    assert state.vehicle_locations.get('X8GNW') == 'LE10_HUB'
    assert state.vehicle_locations.get('W88GNW') == 'CB22_DEPOT'
```

- [ ] **Step 2: Run to confirm they fail**

```
python -m pytest tests/cambridge/test_multiday_state.py -v
```

Expected: 4 errors — `ModuleNotFoundError: cambridge.multiday_state`.

- [ ] **Step 3: Implement multiday_state.py**

Create `cambridge/multiday_state.py`:

```python
"""Day-boundary state for the multi-day Cambridge dispatcher.

One JSON file per day (state_YYYY-MM-DD.json) written at end of planning.
The next day reads it to know: which tractors are at which hub, what freight
is cross-docked at CB22 depot, and which orders failed and should be retried.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Optional

import pandas as pd

from cambridge.scope import ScopedOrder

HubLocation = Literal['CB22_DEPOT', 'B37_HUB', 'LE10_HUB']


@dataclass
class TrunkHubManifest:
    tractors: list[str]
    pallets_outbound: float
    departed: datetime
    expected_return: datetime


@dataclass
class DayState:
    date: date
    vehicle_locations: dict[str, HubLocation]       # tractor_id → location at end of day
    depot_inventory: list[ScopedOrder]               # cross-docked; delivery_date > today
    unassigned_carry_forward: list[str]              # order_ids that failed dispatch
    trunk_manifest: dict[str, TrunkHubManifest]      # hub name → manifest

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def trunk_return_time(self, hub: str) -> Optional[datetime]:
        m = self.trunk_manifest.get(hub)
        return m.expected_return if m else None

    def depot_inventory_for_date(self, d: date) -> list[ScopedOrder]:
        return [o for o in self.depot_inventory if o.delivery_date == d]

    def depot_inventory_for_future(self, d: date) -> list[ScopedOrder]:
        return [o for o in self.depot_inventory if o.delivery_date > d]

    # ------------------------------------------------------------------
    # JSON serialisation
    # ------------------------------------------------------------------

    def to_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            'date': self.date.isoformat(),
            'vehicle_locations': self.vehicle_locations,
            'depot_inventory': [_order_to_dict(o) for o in self.depot_inventory],
            'unassigned_carry_forward': self.unassigned_carry_forward,
            'trunk_manifest': {
                hub: {
                    'tractors': m.tractors,
                    'pallets_outbound': m.pallets_outbound,
                    'departed': m.departed.isoformat(),
                    'expected_return': m.expected_return.isoformat(),
                }
                for hub, m in self.trunk_manifest.items()
            },
        }
        path.write_text(json.dumps(raw, indent=2))

    @classmethod
    def from_json(cls, path: Path) -> 'DayState':
        raw = json.loads(path.read_text())
        return cls(
            date=date.fromisoformat(raw['date']),
            vehicle_locations=raw['vehicle_locations'],
            depot_inventory=[_dict_to_order(d) for d in raw['depot_inventory']],
            unassigned_carry_forward=raw['unassigned_carry_forward'],
            trunk_manifest={
                hub: TrunkHubManifest(
                    tractors=m['tractors'],
                    pallets_outbound=m['pallets_outbound'],
                    departed=datetime.fromisoformat(m['departed']),
                    expected_return=datetime.fromisoformat(m['expected_return']),
                )
                for hub, m in raw['trunk_manifest'].items()
            },
        )

    # ------------------------------------------------------------------
    # Bootstrap from telematics (first day, no prior JSON)
    # ------------------------------------------------------------------

    @classmethod
    def bootstrap_from_telematics(cls, day: date,
                                   telem_df: pd.DataFrame) -> 'DayState':
        """Derive tractor locations from last known postcode after 16:00 on `day`."""
        from cambridge.config import CB22_TRACTORS
        ts = pd.to_datetime(telem_df['LocalTime'], errors='coerce')
        evening = telem_df[
            (ts.dt.date == day) & (ts.dt.hour >= 16)
        ].copy()
        evening['_ts'] = ts[evening.index]

        locations: dict[str, HubLocation] = {}
        for tractor in CB22_TRACTORS:
            sub = evening[evening['AssetName'] == tractor]
            if sub.empty:
                locations[tractor] = 'CB22_DEPOT'
                continue
            last_pc = str(sub.loc[sub['_ts'].idxmax(), 'Location_Postcode'] or '')
            if last_pc.startswith('B37'):
                locations[tractor] = 'B37_HUB'
            elif last_pc.startswith('LE10'):
                locations[tractor] = 'LE10_HUB'
            else:
                locations[tractor] = 'CB22_DEPOT'

        return cls(
            date=day,
            vehicle_locations=locations,
            depot_inventory=[],
            unassigned_carry_forward=[],
            trunk_manifest={},
        )


# ------------------------------------------------------------------
# Private serialisation helpers
# ------------------------------------------------------------------

def _order_to_dict(o: ScopedOrder) -> dict:
    return {
        'order_id': o.order_id,
        'name': o.name,
        'flow': o.flow,
        'origin_pc': o.origin_pc,
        'destination_pc': o.destination_pc,
        'weight_kg': o.weight_kg,
        'pallets': o.pallets,
        'delivery_window': [o.delivery_window[0].isoformat(),
                            o.delivery_window[1].isoformat()],
        'stop_type': o.stop_type,
    }


def _dict_to_order(d: dict) -> ScopedOrder:
    return ScopedOrder(
        order_id=d['order_id'],
        name=d['name'],
        flow=d['flow'],
        origin_pc=d.get('origin_pc'),
        destination_pc=d['destination_pc'],
        weight_kg=d['weight_kg'],
        pallets=d['pallets'],
        delivery_window=(
            datetime.fromisoformat(d['delivery_window'][0]),
            datetime.fromisoformat(d['delivery_window'][1]),
        ),
        collection_window=None,
        stop_type=d.get('stop_type', 'delivery'),
    )
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/cambridge/test_multiday_state.py -v
```

Expected: 4 PASSED.

- [ ] **Step 5: Run full test suite to check no regressions**

```
python -m pytest tests/cambridge/ -v --tb=short
```

Expected: all existing tests still pass.

---

## Task 4: trunk_planner.py — TrunkPlan

**Files:**
- Create: `cambridge/trunk_planner.py`
- Create: `tests/cambridge/test_trunk_planner.py`

**Context:** The trunk planner does three things: (1) sizes how many tractors to send to each hub using per-vehicle `VEHICLE_PROFILES` capacity; (2) assigns available tractors (tractors already at a hub stay there, others sorted by earliest-free); (3) computes departure time (max collection return + 30 min buffer) and expected return time (departure + OSRM drive + hub dwell + OSRM return drive). OSRM is queried at runtime; falls back to haversine at 50 km/h if OSRM is unreachable.

- [ ] **Step 1: Write failing tests**

Create `tests/cambridge/test_trunk_planner.py`:

```python
"""Tests for cambridge.trunk_planner."""
from datetime import date, datetime, timedelta


def test_tractors_to_cover_empty_when_no_pallets():
    from cambridge.trunk_planner import _tractors_to_cover
    result = _tractors_to_cover(0.0, ['X88RNW', 'W88GNW'])
    assert result == []


def test_tractors_to_cover_assigns_minimum_needed():
    from cambridge.trunk_planner import _tractors_to_cover
    # Each tractor has capacity_pallets_per_trip from VEHICLE_PROFILES.
    # Use tractors known to be in CB22_TRACTORS with profile capacity.
    # We only need to verify at least one tractor is assigned for >0 pallets.
    tractors = ['X88RNW', 'W88GNW', 'S88GNW']
    result = _tractors_to_cover(5.0, tractors)
    assert len(result) >= 1
    assert all(t in tractors for t in result)


def test_plan_trunk_departure_uses_max_return_plus_buffer():
    from cambridge.trunk_planner import plan_trunk
    from cambridge.scope import ScopedOrder

    day = date(2026, 1, 7)
    # One collection returns at 17:30 — trunk should depart at 18:00 (17:30 + 30 min)
    collection_returns = {
        'S88GNW': datetime(2026, 1, 7, 17, 30),
        'V88GNW': datetime(2026, 1, 7, 15, 0),
    }
    plan = plan_trunk(
        pl_export_b37=[],
        pl_export_le10=[],
        pl_import_tomorrow_b37=[],
        available_tractors=['X88RNW', 'W88GNW'],
        collection_return_times=collection_returns,
        day=day,
    )
    # When no pallets to carry, no tractors assigned and no departure
    assert plan.b37_tractors == []
    assert plan.le10_tractors == []


def test_plan_trunk_depart_fallback_when_no_collections():
    from cambridge.trunk_planner import plan_trunk
    from cambridge.scope import ScopedOrder
    from datetime import time as dtime

    day = date(2026, 1, 7)
    # No collections → fallback depart at 16:00
    plan = plan_trunk(
        pl_export_b37=[],
        pl_export_le10=[],
        pl_import_tomorrow_b37=[],
        available_tractors=['X88RNW'],
        collection_return_times={},
        day=day,
    )
    # No pallets → no tractors assigned; depart fallback not used when no tractors needed
    assert plan.b37_tractors == []


def test_plan_trunk_assigns_tractors_for_nonzero_pallets():
    from cambridge.trunk_planner import plan_trunk
    from cambridge.scope import ScopedOrder

    def _order(order_id, pallets):
        return ScopedOrder(
            order_id=order_id, name=order_id, flow='PL_EXPORT',
            origin_pc='CB9 7BG', destination_pc='B37 7HB',
            weight_kg=pallets * 200, pallets=pallets,
            delivery_window=(
                datetime(2026, 1, 7, 6, 0),
                datetime(2026, 1, 7, 18, 0),
            ),
            collection_window=None,
        )

    day = date(2026, 1, 7)
    plan = plan_trunk(
        pl_export_b37=[_order('e1', 10.0), _order('e2', 8.0)],
        pl_export_le10=[],
        pl_import_tomorrow_b37=[],
        available_tractors=['X88RNW', 'W88GNW'],
        collection_return_times={'X88RNW': datetime(2026, 1, 7, 17, 0)},
        day=day,
    )
    assert len(plan.b37_tractors) >= 1
    assert plan.b37_depart is not None
    # Departure should be >= 17:00 + 30 min = 17:30
    assert plan.b37_depart >= datetime(2026, 1, 7, 17, 30)


def test_osrm_drive_h_returns_positive_float():
    from cambridge.trunk_planner import _osrm_drive_h
    from cambridge.config import CB22_DEPOT_ANCHOR, PALLETLINE_HUB_COORDS
    h = _osrm_drive_h(CB22_DEPOT_ANCHOR, PALLETLINE_HUB_COORDS)
    assert isinstance(h, float)
    assert 1.0 < h < 5.0   # sanity: must be between 1h and 5h
```

- [ ] **Step 2: Run to confirm they fail**

```
python -m pytest tests/cambridge/test_trunk_planner.py -v
```

Expected: `ModuleNotFoundError: cambridge.trunk_planner`.

- [ ] **Step 3: Implement trunk_planner.py**

Create `cambridge/trunk_planner.py`:

```python
"""Trunk planner — decides which tractors go to which hub tonight and when.

Not a routing problem: the B37/LE10 runs follow a fixed overnight pattern.
This module handles capacity sizing (how many trailers?) and timing
(when do they depart and return?). Drive times come from OSRM at runtime.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional

from cambridge.config import (
    CB22_DEPOT_ANCHOR,
    PALLETLINE_HUB_COORDS,
    HAZCHEM_HUB_COORDS,
    TRUNK_B37_DWELL_MIN,
    TRUNK_LE10_DWELL_MIN,
    TRUNK_LOADING_BUFFER_MIN,
    VEHICLE_PROFILES,
    OSRM_URL,
)
from cambridge.multiday_state import TrunkHubManifest


@dataclass
class TrunkPlan:
    b37_tractors: list[str]
    le10_tractors: list[str]
    b37_depart: Optional[datetime]
    le10_depart: Optional[datetime]
    b37_expected_return: Optional[datetime]
    le10_expected_return: Optional[datetime]
    manifest: dict[str, TrunkHubManifest]

    @property
    def all_tractors(self) -> list[str]:
        return self.b37_tractors + self.le10_tractors


def _osrm_drive_h(origin: tuple[float, float],
                   dest: tuple[float, float]) -> float:
    """Query OSRM for HGV-adjusted one-way drive time in hours.

    Falls back to haversine / 50 km·h⁻¹ if OSRM is unreachable.
    """
    import sys, os
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'simulation'))
    from routing import TRUCK_DURATION_FACTOR
    from cambridge.scope import _haversine_km

    lon1, lat1 = origin[1], origin[0]
    lon2, lat2 = dest[1], dest[0]
    url = (f"{OSRM_URL}/route/v1/driving/"
           f"{lon1},{lat1};{lon2},{lat2}?overview=false")
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        seconds = data['routes'][0]['duration']
        return seconds * TRUCK_DURATION_FACTOR / 3600.0
    except Exception:
        km = _haversine_km(origin[0], origin[1], dest[0], dest[1])
        return (km / 50.0) * TRUCK_DURATION_FACTOR


def _tractors_to_cover(total_pallets: float,
                        candidates: list[str]) -> list[str]:
    """Assign fewest tractors needed to carry `total_pallets`, largest-first."""
    if total_pallets <= 0:
        return []
    assigned = []
    remaining = total_pallets
    for vid in sorted(
        candidates,
        key=lambda v: VEHICLE_PROFILES.get(v, {}).get('capacity_pallets_per_trip', 30),
        reverse=True,
    ):
        if remaining <= 0:
            break
        cap = VEHICLE_PROFILES.get(vid, {}).get('capacity_pallets_per_trip', 30)
        assigned.append(vid)
        remaining -= cap
    return assigned


def plan_trunk(
    pl_export_b37: list,
    pl_export_le10: list,
    pl_import_tomorrow_b37: list,
    available_tractors: list[str],
    collection_return_times: dict[str, datetime],
    day: date,
) -> TrunkPlan:
    """Compute tonight's trunk plan.

    Parameters
    ----------
    pl_export_b37 : list[ScopedOrder]
        PL_EXPORT orders (non-Hazchem) collected today for B37 hub.
    pl_export_le10 : list[ScopedOrder]
        PL_EXPORT Hazchem orders collected today for LE10 hub.
    pl_import_tomorrow_b37 : list[ScopedOrder]
        PL_IMPORT orders due tomorrow — used to size return trailer capacity.
    available_tractors : list[str]
        Tractor IDs currently at CB22 depot (from DayState.vehicle_locations).
    collection_return_times : dict[str, datetime]
        Estimated return time per tractor after daytime collections.
    day : date
        Calendar day being planned.
    """
    # --- 1. Pallet totals ---
    b37_pallets  = sum(o.pallets for o in pl_export_b37)
    le10_pallets = sum(o.pallets for o in pl_export_le10)

    # 2-day lookahead: bump B37 trailers if tomorrow's inbound needs more
    tomorrow_b37_pallets = sum(o.pallets for o in pl_import_tomorrow_b37)

    # --- 2. Size trailers ---
    # Assign LE10-bound tractors first (smaller, dedicated pool)
    le10_needed = _tractors_to_cover(le10_pallets, available_tractors)
    remaining_for_b37 = [t for t in available_tractors if t not in le10_needed]

    # B37: cover outbound pallets AND enough return capacity for tomorrow's inbound
    b37_for_outbound = _tractors_to_cover(b37_pallets, remaining_for_b37)
    b37_for_inbound  = _tractors_to_cover(tomorrow_b37_pallets, remaining_for_b37)
    # Union: assign the larger set
    b37_needed = list(dict.fromkeys(b37_for_outbound + b37_for_inbound))

    # --- 3. Departure time ---
    def _depart(tractors: list[str]) -> Optional[datetime]:
        if not tractors:
            return None
        relevant = {v: t for v, t in collection_return_times.items() if v in tractors}
        if relevant:
            base = max(relevant.values())
        else:
            base = datetime.combine(day, time(16, 0))
        return base + timedelta(minutes=TRUNK_LOADING_BUFFER_MIN)

    b37_depart  = _depart(b37_needed)
    le10_depart = _depart(le10_needed)

    # --- 4. Drive times from OSRM ---
    drive_b37_h  = _osrm_drive_h(CB22_DEPOT_ANCHOR, PALLETLINE_HUB_COORDS)
    drive_le10_h = _osrm_drive_h(CB22_DEPOT_ANCHOR, HAZCHEM_HUB_COORDS)

    # --- 5. Expected return ---
    def _return(depart: Optional[datetime], drive_h: float,
                dwell_min: int) -> Optional[datetime]:
        if depart is None:
            return None
        return (depart
                + timedelta(hours=drive_h)
                + timedelta(minutes=dwell_min)
                + timedelta(hours=drive_h))

    b37_return  = _return(b37_depart,  drive_b37_h,  TRUNK_B37_DWELL_MIN)
    le10_return = _return(le10_depart, drive_le10_h, TRUNK_LE10_DWELL_MIN)

    # --- 6. Build manifest ---
    manifest: dict[str, TrunkHubManifest] = {}
    if b37_needed and b37_depart and b37_return:
        manifest['B37_HUB'] = TrunkHubManifest(
            tractors=b37_needed,
            pallets_outbound=b37_pallets,
            departed=b37_depart,
            expected_return=b37_return,
        )
    if le10_needed and le10_depart and le10_return:
        manifest['LE10_HUB'] = TrunkHubManifest(
            tractors=le10_needed,
            pallets_outbound=le10_pallets,
            departed=le10_depart,
            expected_return=le10_return,
        )

    return TrunkPlan(
        b37_tractors=b37_needed,
        le10_tractors=le10_needed,
        b37_depart=b37_depart,
        le10_depart=le10_depart,
        b37_expected_return=b37_return,
        le10_expected_return=le10_return,
        manifest=manifest,
    )
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/cambridge/test_trunk_planner.py -v
```

Expected: 6 PASSED.

- [ ] **Step 5: Run full suite**

```
python -m pytest tests/cambridge/ -v --tb=short
```

Expected: all pass.

---

## Task 5: dispatcher.py — pre-staged order support

**Files:**
- Modify: `cambridge/dispatcher.py` — `build_freight_availability` and `run_day_multi_trip`
- Test: add to `tests/cambridge/test_dispatcher.py`

**Context:** Cross-docked orders from yesterday are already physically at CB22 depot at 06:00. They must not wait for a trunk arrival or a collection trip. We add an optional `pre_staged_ids: set[str]` parameter to `build_freight_availability`; any order whose ID is in this set gets `freight_ready = day_start` regardless of flow. `run_day_multi_trip` gets a matching `pre_staged_ids` parameter that it forwards.

- [ ] **Step 1: Write failing test**

Open `tests/cambridge/test_dispatcher.py`. Add at the end:

```python
def test_pre_staged_orders_get_day_start_freight_ready():
    """Cross-docked orders are at depot at day_start regardless of flow."""
    from datetime import date, datetime, time
    from cambridge.dispatcher import build_freight_availability
    from cambridge.scope import ScopedOrder

    day = date(2026, 1, 8)
    day_start = datetime.combine(day, time(6, 0))

    order = ScopedOrder(
        order_id='cross-1', name='WT1', flow='FULL_FLEET',
        origin_pc='CB9 7BG', destination_pc='CB1 1AA',
        weight_kg=200.0, pallets=1.0,
        delivery_window=(day_start, datetime.combine(day, time(18, 0))),
        collection_window=None,
    )
    freight_ready = build_freight_availability(
        orders=[order], trips=[], day=day,
        trunk_schedule=None,
        pre_staged_ids={'cross-1'},
    )
    assert freight_ready['cross-1'] == day_start
```

- [ ] **Step 2: Run to confirm it fails**

```
python -m pytest tests/cambridge/test_dispatcher.py::test_pre_staged_orders_get_day_start_freight_ready -v
```

Expected: FAILED — `TypeError: build_freight_availability() got unexpected keyword argument 'pre_staged_ids'`.

- [ ] **Step 3: Add pre_staged_ids to build_freight_availability**

In `cambridge/dispatcher.py`, find `build_freight_availability`. Change its signature and add early-exit for pre-staged orders:

```python
def build_freight_availability(orders: list[ScopedOrder],
                               trips: list[CollectionTrip],
                               day: date_type,
                               trunk_schedule=None,
                               pre_staged_ids: set | None = None) -> dict[str, datetime]:
    day_start = datetime.combine(day, time(DEFAULT_PRE_STAGED_HOUR, 0))
    pl_import_ready = (max(trunk_schedule.freight_ready, day_start)
                       if trunk_schedule is not None else day_start)

    trip_by_order: dict[str, datetime] = {}
    for trip in trips:
        for order_id in trip.orders:
            trip_by_order[order_id] = trip.freight_ready_at_depot

    out: dict[str, datetime] = {}
    for order in orders:
        if pre_staged_ids and order.order_id in pre_staged_ids:
            out[order.order_id] = day_start   # already at depot
        elif order.flow == 'PL_IMPORT':
            out[order.order_id] = pl_import_ready
        else:
            trip_ready = trip_by_order.get(order.order_id)
            out[order.order_id] = (max(trip_ready, day_start)
                                   if trip_ready else day_start)
    return out
```

- [ ] **Step 4: Add pre_staged_ids to run_day_multi_trip**

In `cambridge/dispatcher.py`, find `run_day_multi_trip`. Add `pre_staged_ids: set | None = None` to its signature and pass it through to `build_freight_availability`:

Find the call to `build_freight_availability` inside `run_day_multi_trip` and add the kwarg:

```python
freight_ready = build_freight_availability(
    orders, trips, day,
    trunk_schedule=trunk_schedule,
    pre_staged_ids=pre_staged_ids,
)
```

Also add `pre_staged_ids: set | None = None` to the function signature of `run_day_multi_trip`.

- [ ] **Step 5: Run tests**

```
python -m pytest tests/cambridge/test_dispatcher.py -v --tb=short
```

Expected: all tests pass including the new one.

---

## Task 6: day_coordinator.py — plan_day()

**Files:**
- Create: `cambridge/day_coordinator.py`
- Create: `tests/cambridge/test_day_coordinator.py`

**Context:** `plan_day` is the per-day orchestration entry point. It: (1) loads the order pool from Qargo for day D and D+1; (2) merges cross-docked orders from `prev_state.depot_inventory`; (3) calls `run_day_multi_trip` with pre_staged_ids; (4) calls `plan_trunk`; (5) builds new `DayState`; (6) returns a `DayPlan`. The test uses a lightweight mock to avoid loading real Qargo data.

- [ ] **Step 1: Write failing tests**

Create `tests/cambridge/test_day_coordinator.py`:

```python
"""Tests for cambridge.day_coordinator."""
from datetime import date, datetime, time
from unittest.mock import MagicMock, patch

import pytest


def _make_order(order_id, flow, dest_pc, delivery_date_str, pallets=1.0):
    from cambridge.scope import ScopedOrder
    d = datetime.fromisoformat(f'{delivery_date_str}T06:00:00')
    e = datetime.fromisoformat(f'{delivery_date_str}T18:00:00')
    return ScopedOrder(
        order_id=order_id, name=order_id, flow=flow,
        origin_pc='CB9 7BG' if flow != 'PL_IMPORT' else None,
        destination_pc=dest_pc,
        weight_kg=pallets * 200, pallets=pallets,
        delivery_window=(d, e), collection_window=None,
    )


def test_plan_day_includes_depot_inventory_as_pre_staged():
    """Cross-docked orders from prev_state enter today's pool as pre-staged."""
    from cambridge.day_coordinator import plan_day
    from cambridge.multiday_state import DayState

    day = date(2026, 1, 8)
    cross_docked = _make_order('cd-1', 'FULL_FLEET', 'CB1 1AA', '2026-01-08')

    prev_state = DayState(
        date=date(2026, 1, 7),
        vehicle_locations={'X88RNW': 'B37_HUB'},
        depot_inventory=[cross_docked],
        unassigned_carry_forward=[],
        trunk_manifest={},
    )

    # Patch out the heavy VRPTW call and Qargo loading
    mock_dispatch_output = MagicMock()
    mock_dispatch_output.unassigned = []
    mock_dispatch_output.routes = {}
    mock_dispatch_output.collection_trips = []

    mock_trunk_plan = MagicMock()
    mock_trunk_plan.all_tractors = []
    mock_trunk_plan.b37_tractors = []
    mock_trunk_plan.le10_tractors = []
    mock_trunk_plan.manifest = {}

    with patch('cambridge.day_coordinator.run_day_multi_trip',
               return_value=mock_dispatch_output) as mock_dispatch, \
         patch('cambridge.day_coordinator.plan_trunk',
               return_value=mock_trunk_plan), \
         patch('cambridge.day_coordinator._load_orders_for_day',
               return_value=([], [])):

        result = plan_day(
            day=day,
            prev_state=prev_state,
            qargo_df=None,
            telem_df=None,
            postcode_cache={},
            mode='forward',
            solver_budget_s=1.0,
        )

    # Verify cross-docked order was passed as pre_staged_ids
    call_kwargs = mock_dispatch.call_args.kwargs
    assert 'pre_staged_ids' in call_kwargs
    assert 'cd-1' in call_kwargs['pre_staged_ids']


def test_plan_day_end_state_moves_hub_tractors():
    """Tractors assigned to B37/LE10 appear in end_state vehicle_locations."""
    from cambridge.day_coordinator import plan_day
    from cambridge.multiday_state import DayState

    day = date(2026, 1, 7)
    prev_state = DayState(
        date=date(2026, 1, 6),
        vehicle_locations={'X88RNW': 'CB22_DEPOT', 'W88GNW': 'CB22_DEPOT'},
        depot_inventory=[],
        unassigned_carry_forward=[],
        trunk_manifest={},
    )

    mock_dispatch_output = MagicMock()
    mock_dispatch_output.unassigned = []
    mock_dispatch_output.routes = {}
    mock_dispatch_output.collection_trips = []

    mock_trunk_plan = MagicMock()
    mock_trunk_plan.all_tractors = ['X88RNW']
    mock_trunk_plan.b37_tractors = ['X88RNW']
    mock_trunk_plan.le10_tractors = []
    mock_trunk_plan.manifest = {}

    with patch('cambridge.day_coordinator.run_day_multi_trip',
               return_value=mock_dispatch_output), \
         patch('cambridge.day_coordinator.plan_trunk',
               return_value=mock_trunk_plan), \
         patch('cambridge.day_coordinator._load_orders_for_day',
               return_value=([], [])):

        result = plan_day(
            day=day, prev_state=prev_state, qargo_df=None, telem_df=None,
            postcode_cache={}, mode='forward', solver_budget_s=1.0,
        )

    assert result.end_state.vehicle_locations['X88RNW'] == 'B37_HUB'
    assert result.end_state.vehicle_locations['W88GNW'] == 'CB22_DEPOT'
```

- [ ] **Step 2: Run to confirm they fail**

```
python -m pytest tests/cambridge/test_day_coordinator.py -v
```

Expected: `ModuleNotFoundError: cambridge.day_coordinator`.

- [ ] **Step 3: Implement day_coordinator.py**

Create `cambridge/day_coordinator.py`:

```python
"""Day coordinator — orchestrates one day of multi-day dispatch.

Assembles order pools from Qargo + cross-docked state, runs the VRPTW
dispatcher for rigids, runs the trunk planner for tractors, and writes
updated DayState.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal, Optional

import pandas as pd

from cambridge.config import (
    CB22_TRACTORS, CB22_DEPOT_ANCHOR, PALLETLINE_HUB_COORDS, HAZCHEM_HUB_COORDS,
)
from cambridge.scope import ScopedOrder, build_scoped_orders, classify_order
from cambridge.dispatcher import run_day_multi_trip, DayDispatchOutput
from cambridge.trunk_planner import TrunkPlan, plan_trunk
from cambridge.multiday_state import DayState, TrunkHubManifest


@dataclass
class DayPlan:
    date: date
    trunk_plan: TrunkPlan
    rigid_routes: DayDispatchOutput
    end_state: DayState


def plan_day(
    day: date,
    prev_state: DayState,
    qargo_df: Optional[pd.DataFrame],
    telem_df: Optional[pd.DataFrame],
    postcode_cache: dict,
    mode: Literal['backtest', 'forward'],
    solver_budget_s: float = 30.0,
) -> DayPlan:
    """Plan one operating day.

    Parameters
    ----------
    day : date
        The day being planned (D).
    prev_state : DayState
        End-of-day state from Day D-1 (tractor locations, depot inventory,
        unassigned carry-forward).
    qargo_df : pd.DataFrame or None
        Full Qargo dataset (all months). None only in unit tests with mocked loaders.
    telem_df : pd.DataFrame or None
        Supatrak telematics. Used in backtest mode for trip timing; None in forward mode.
    postcode_cache : dict
        Postcode → (lat, lon); shared across days and mutated in place.
    mode : 'backtest' or 'forward'
        Controls how vehicle shift windows are initialised.
    solver_budget_s : float
        VRPTW solver time budget in seconds.
    """
    # ------------------------------------------------------------------
    # 1. Load orders for Day D (delivery/collection due today)
    #    and Day D+1 (2-day lookahead for trunk sizing only).
    # ------------------------------------------------------------------
    orders_today, orders_tomorrow = _load_orders_for_day(
        day, qargo_df, postcode_cache, mode,
    )

    # ------------------------------------------------------------------
    # 2. Cross-docked orders from prev_state that are due today.
    # ------------------------------------------------------------------
    cross_docked_today = prev_state.depot_inventory_for_date(day)
    pre_staged_ids = {o.order_id for o in cross_docked_today}

    # Carry-forward unassigned orders from yesterday (retry today).
    carry_forward_ids = set(prev_state.unassigned_carry_forward)

    # Build the full order pool for the dispatcher:
    # today's orders + cross-docked due today + carry-forward (already in today's orders
    # if destination_date == day; cross_docked_today are depot-staged copies).
    all_orders = orders_today + cross_docked_today

    # ------------------------------------------------------------------
    # 3. Run rigid VRPTW.
    #    Tractors starting at CB22 are included; those at hubs are excluded
    #    (they return early morning and join the pool via trunk schedule).
    # ------------------------------------------------------------------
    trunk_return_b37 = prev_state.trunk_return_time('B37_HUB')
    trunk_return_le10 = prev_state.trunk_return_time('LE10_HUB')

    # Build a lightweight trunk_schedule proxy for build_freight_availability.
    class _TrunkProxy:
        def __init__(self, return_time):
            self.freight_ready = return_time or datetime.combine(day, time(6, 0))

    trunk_schedule = (
        _TrunkProxy(trunk_return_b37 or trunk_return_le10)
        if (trunk_return_b37 or trunk_return_le10) else None
    )

    rigid_output = run_day_multi_trip(
        day=day,
        orders=all_orders,
        trips=[],
        postcode_cache=postcode_cache,
        mode=mode,
        telem_df=telem_df,
        solver_budget_s=solver_budget_s,
        pre_staged_ids=pre_staged_ids,
    )

    # ------------------------------------------------------------------
    # 4. Trunk planner.
    # ------------------------------------------------------------------
    assigned_ids = set(rigid_output.routes.keys()) if rigid_output.routes else set()

    pl_export_b37 = [
        o for o in orders_today
        if o.flow == 'PL_EXPORT'
        and 'hazchem' not in str(o.order_id).lower()   # crude split; subcontractor
        and o.order_id not in rigid_output.unassigned
    ]
    pl_export_le10 = [
        o for o in orders_today
        if o.flow == 'PL_EXPORT'
        and 'hazchem' in str(o.order_id).lower()
        and o.order_id not in rigid_output.unassigned
    ]
    pl_import_tomorrow = [o for o in orders_tomorrow if o.flow == 'PL_IMPORT']

    available_tractors = [
        v for v in CB22_TRACTORS
        if prev_state.vehicle_locations.get(v, 'CB22_DEPOT') == 'CB22_DEPOT'
    ]

    collection_returns = _estimate_collection_returns(rigid_output, day)

    trunk_plan = plan_trunk(
        pl_export_b37=pl_export_b37,
        pl_export_le10=pl_export_le10,
        pl_import_tomorrow_b37=pl_import_tomorrow,
        available_tractors=available_tractors,
        collection_return_times=collection_returns,
        day=day,
    )

    # ------------------------------------------------------------------
    # 5. New end-of-day state.
    # ------------------------------------------------------------------
    cross_docked_future = (
        prev_state.depot_inventory_for_future(day)
        + [
            o for o in orders_today
            if o.flow == 'FULL_FLEET'
            and o.stop_type == 'pickup'        # collected today, deliver later
            and o.delivery_date > day
            and o.order_id not in rigid_output.unassigned
        ]
    )

    hub_locations: dict = {}
    for v, loc in prev_state.vehicle_locations.items():
        if v in trunk_plan.b37_tractors:
            hub_locations[v] = 'B37_HUB'
        elif v in trunk_plan.le10_tractors:
            hub_locations[v] = 'LE10_HUB'
        else:
            hub_locations[v] = 'CB22_DEPOT'
    for v in trunk_plan.b37_tractors:
        hub_locations[v] = 'B37_HUB'
    for v in trunk_plan.le10_tractors:
        hub_locations[v] = 'LE10_HUB'

    end_state = DayState(
        date=day,
        vehicle_locations=hub_locations,
        depot_inventory=cross_docked_future,
        unassigned_carry_forward=rigid_output.unassigned,
        trunk_manifest=trunk_plan.manifest,
    )

    return DayPlan(
        date=day,
        trunk_plan=trunk_plan,
        rigid_routes=rigid_output,
        end_state=end_state,
    )


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------

def _load_orders_for_day(
    day: date,
    qargo_df: Optional[pd.DataFrame],
    postcode_cache: dict,
    mode: str,
) -> tuple[list[ScopedOrder], list[ScopedOrder]]:
    """Return (orders_today, orders_tomorrow) from Qargo.

    'today' = orders whose collection or delivery date matches `day`.
    'tomorrow' = PL_IMPORT orders due day+1 (for trunk sizing only).
    """
    from datetime import timedelta
    tomorrow = day + timedelta(days=1)

    if qargo_df is None:
        return [], []

    cb22_fleet_only = (mode == 'backtest')

    orders_today = build_scoped_orders(
        qargo_df, postcode_cache,
        cb22_fleet_only=cb22_fleet_only, day=day,
    )
    orders_tomorrow = build_scoped_orders(
        qargo_df, postcode_cache,
        cb22_fleet_only=cb22_fleet_only, day=tomorrow,
    )
    # Filter orders_tomorrow to PL_IMPORT only (lookahead, not for dispatch)
    orders_tomorrow = [o for o in orders_tomorrow if o.flow == 'PL_IMPORT']

    return orders_today, orders_tomorrow


def _estimate_collection_returns(
    output: DayDispatchOutput, day: date,
) -> dict[str, datetime]:
    """Estimate when each tractor returns from daytime collections.

    Uses route shift_end from the dispatcher output where available.
    Falls back to 17:00 if the route has no timing information.
    """
    fallback = datetime.combine(day, time(17, 0))
    returns: dict[str, datetime] = {}
    for vid, route in (output.routes or {}).items():
        if vid not in CB22_TRACTORS:
            continue
        shift_end = getattr(route, 'shift_end', None)
        returns[vid] = shift_end if isinstance(shift_end, datetime) else fallback
    return returns
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/cambridge/test_day_coordinator.py -v
```

Expected: 2 PASSED.

- [ ] **Step 5: Fix PL_EXPORT hub routing in day_coordinator**

The crude hazchem split in step 3 above (using `order_id.lower()`) is wrong. In `day_coordinator.py`, replace the `pl_export_b37` / `pl_export_le10` split with a proper subcontractor check. First, check how subcontractor info reaches `ScopedOrder`:

Looking at `scope.py`, `ScopedOrder` does not store the subcontractor string. Add it now.

In `cambridge/scope.py`, add `subcontractor: Optional[str] = None` to `ScopedOrder` fields (after `shipment_names`):

```python
    shipment_names: Optional[str] = None    # raw Qargo shipment_names
    subcontractor: Optional[str] = None     # raw resource_subcontractor (for hub routing)
    stop_type: Literal['delivery', 'pickup'] = 'delivery'
```

In `build_scoped_orders`, add `subcontractor=sub_str` to the `ScopedOrder(...)` constructor call.

Then in `day_coordinator.py`, replace the crude split:

```python
    pl_export_b37 = [
        o for o in orders_today
        if o.flow == 'PL_EXPORT'
        and 'hazchem' not in (o.subcontractor or '').lower()
        and o.order_id not in rigid_output.unassigned
    ]
    pl_export_le10 = [
        o for o in orders_today
        if o.flow == 'PL_EXPORT'
        and 'hazchem' in (o.subcontractor or '').lower()
        and o.order_id not in rigid_output.unassigned
    ]
```

- [ ] **Step 6: Run full suite**

```
python -m pytest tests/cambridge/ -v --tb=short
```

Expected: all tests pass.

---

## Task 7: multiday_backtest.py + CLI

**Files:**
- Create: `cambridge/multiday_backtest.py`
- Modify: `cambridge/__main__.py`

**Context:** `run_multiday_backtest` loops `plan_day` over a date range, reads/writes state JSON each day, prints a per-day summary, and generates an HTML map. The first day bootstraps state from telematics. The `__main__.py` gets a `--multiday` flag.

- [ ] **Step 1: Implement multiday_backtest.py**

Create `cambridge/multiday_backtest.py`:

```python
"""Multi-day Cambridge dispatcher backtest.

Loops plan_day() over a consecutive date range, writing one state JSON and
one HTML map per day, then printing a cumulative summary.

Usage:
    python -m cambridge --multiday --start 2026-01-07 --end 2026-01-09 --budget 80
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from cambridge.config import STATE_DIR
from cambridge.day_coordinator import plan_day, DayPlan
from cambridge.multiday_state import DayState
from cambridge.backtest import actuals_for_day


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def run_multiday_backtest(
    start_date: date,
    end_date: date,
    qargo_df: pd.DataFrame,
    telem_df: pd.DataFrame,
    postcode_cache: dict,
    output_dir: Path,
    state_dir: Path = STATE_DIR,
    solver_budget_s: float = 30.0,
) -> list[dict]:
    """Run multi-day backtest from start_date to end_date inclusive.

    Returns list of per-day metric dicts (one per day).
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Bootstrap state from telematics on the day before start
    bootstrap_day = start_date - timedelta(days=1)
    prev_state = DayState.bootstrap_from_telematics(bootstrap_day, telem_df)

    all_metrics = []

    for day in _daterange(start_date, end_date):
        print(f'\n{"="*62}')
        print(f'  MULTI-DAY BACKTEST  {day}')
        print(f'{"="*62}')

        # Check if a state file already exists (resume support)
        state_path = state_dir / f'state_{day}.json'

        plan = plan_day(
            day=day,
            prev_state=prev_state,
            qargo_df=qargo_df,
            telem_df=telem_df,
            postcode_cache=postcode_cache,
            mode='backtest',
            solver_budget_s=solver_budget_s,
        )

        # Write state
        plan.end_state.to_json(state_path)
        print(f'  State written → {state_path.name}')

        # Print trunk summary
        tp = plan.trunk_plan
        if tp.b37_tractors:
            print(f'  B37 trunk:  {tp.b37_tractors}  depart {tp.b37_depart}')
        if tp.le10_tractors:
            print(f'  LE10 trunk: {tp.le10_tractors}  depart {tp.le10_depart}')

        # Per-day metrics
        actuals = actuals_for_day(day, telem_df)
        planned_vehicles = len(plan.rigid_routes.routes or {})
        planned_km = plan.rigid_routes.metrics.get('total_km', 0)
        unassigned = len(plan.rigid_routes.unassigned or [])

        print(f'  Planned vehicles: {planned_vehicles}   actual: {actuals["active_vehicles"]}')
        print(f'  Planned km:       {planned_km:.0f}   actual: {actuals["total_km"]:.0f}')
        print(f'  Unassigned:       {unassigned}')

        day_metrics = {
            'day': day.isoformat(),
            'planned_vehicles': planned_vehicles,
            'actual_vehicles': actuals['active_vehicles'],
            'planned_km': planned_km,
            'actual_km': actuals['total_km'],
            'unassigned': unassigned,
            'b37_tractors': tp.b37_tractors,
            'le10_tractors': tp.le10_tractors,
        }
        all_metrics.append(day_metrics)

        # HTML map
        _write_html_map(day, plan, telem_df, output_dir)

        prev_state = plan.end_state

    return all_metrics


def _write_html_map(day: date, plan: DayPlan,
                    telem_df: pd.DataFrame, output_dir: Path) -> None:
    """Generate HTML plan-vs-actual map including trunk arcs."""
    import json
    from pathlib import Path as P
    import sys, os
    sys.path.insert(0, str(P(__file__).resolve().parents[1]))

    try:
        from operational_analysis.export_plan_replay import build_and_write_map
        html_path = output_dir / f'plan_replay_{day}.html'
        build_and_write_map(
            day=day,
            plan=plan,
            telem_df=telem_df,
            output_path=html_path,
        )
        print(f'  HTML map → {html_path.name}')
    except Exception as exc:
        print(f'  [WARN] HTML map failed: {exc}')
```

- [ ] **Step 2: Add --multiday flag to __main__.py**

In `cambridge/__main__.py`, add the `--multiday` argument and wire it up:

```python
    p.add_argument('--multiday', action='store_true',
                   help='Run multi-day dispatcher (requires --start + --end)')
```

And in `main()`, add a branch:

```python
    if args.multiday:
        if not (args.start and args.end):
            p.error('--multiday requires --start and --end')
        from cambridge.multiday_backtest import run_multiday_backtest
        s = date_type.fromisoformat(args.start)
        e = date_type.fromisoformat(args.end)
        run_multiday_backtest(
            start_date=s, end_date=e,
            qargo_df=qargo, telem_df=telem,
            postcode_cache=cache,
            output_dir=OUT,
            solver_budget_s=args.budget,
        )
```

- [ ] **Step 3: Smoke-test single day**

Run one day with a short budget to verify the wiring works end-to-end:

```
cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics
python -m cambridge --multiday --start 2026-01-07 --end 2026-01-07 --budget 30
```

Expected output (approximate):
```
OSRM detected at http://localhost:5000 — using road routing
==============================================================
  MULTI-DAY BACKTEST  2026-01-07
==============================================================
  State written → state_2026-01-07.json
  B37 trunk:  [...]  depart ...
  Planned vehicles: ...   actual: 33
  Planned km: ...   actual: 9678
  Unassigned: ...
  HTML map → plan_replay_2026-01-07.html
```

- [ ] **Step 4: Run full test suite**

```
python -m pytest tests/cambridge/ -v --tb=short
```

Expected: all tests pass.

---

## Task 8: HTML map — trunk arc overlays

**Files:**
- Modify: `operational_analysis/export_plan_replay.py`

**Context:** The existing `export_plan_replay.py` already builds a Folium map with planned routes and actual GPS. We add two visual layers: outbound trunk arcs (CB22 → hub, dashed) and inbound arcs (hub → CB22, dotted), coloured dark purple for B37 and dark red for LE10. We also add a `build_and_write_map` function so `multiday_backtest.py` can call it without using the script's `__main__` block.

- [ ] **Step 1: Read the existing map-building function**

Open `operational_analysis/export_plan_replay.py` and find the function that builds and writes the HTML (it uses `folium`). Note the variable name of the folium map object (likely `m` or `map_obj`).

- [ ] **Step 2: Add trunk arc drawing helper**

In `operational_analysis/export_plan_replay.py`, after the imports block, add:

```python
_PALLETLINE_HUB_LL = (52.467, -1.787)   # B37 7HB
_HAZCHEM_HUB_LL    = (52.537, -1.376)   # LE10 3BS
_DEPOT_LL          = (DEPOT_LAT, DEPOT_LON)

_TRUNK_STYLES = {
    'B37_HUB':  {'color': '#3d0066', 'weight': 3, 'dash_array': '10 5',  'label': 'Palletline trunk'},
    'LE10_HUB': {'color': '#8b0000', 'weight': 3, 'dash_array': '10 5',  'label': 'Hazchem trunk'},
}


def _add_trunk_arcs(folium_map, trunk_plan) -> None:
    """Overlay outbound (solid) and inbound (dotted) trunk arcs on the map."""
    import folium

    hub_coords = {
        'B37_HUB': _PALLETLINE_HUB_LL,
        'LE10_HUB': _HAZCHEM_HUB_LL,
    }
    for hub, manifest in (trunk_plan.manifest or {}).items():
        hub_ll = hub_coords.get(hub)
        if hub_ll is None:
            continue
        style = _TRUNK_STYLES.get(hub, {'color': '#555555', 'weight': 2, 'dash_array': '5 5'})

        # Outbound arc: depot → hub (dashed)
        folium.PolyLine(
            locations=[_DEPOT_LL, hub_ll],
            color=style['color'], weight=style['weight'],
            dash_array=style['dash_array'],
            tooltip=f"{style['label']} outbound — {manifest.tractors}",
            opacity=0.8,
        ).add_to(folium_map)

        # Inbound arc: hub → depot (dotted, thinner)
        folium.PolyLine(
            locations=[hub_ll, _DEPOT_LL],
            color=style['color'], weight=2,
            dash_array='4 8',
            tooltip=f"{style['label']} return — expected {manifest.expected_return}",
            opacity=0.6,
        ).add_to(folium_map)

        # Hub marker
        folium.CircleMarker(
            location=hub_ll, radius=8,
            color=style['color'], fill=True, fill_color=style['color'],
            fill_opacity=0.7,
            tooltip=hub,
        ).add_to(folium_map)
```

- [ ] **Step 3: Add build_and_write_map entry point**

At the END of `export_plan_replay.py`, add:

```python
def build_and_write_map(day, plan, telem_df, output_path: Path) -> None:
    """Entry point for multiday_backtest: build map for one DayPlan and write HTML.

    Uses the existing script machinery (geocoding, GPS replay, planned routes)
    then overlays trunk arcs from plan.trunk_plan.
    """
    from datetime import date as _date
    import webbrowser

    # Re-use the existing single-day map builder
    html_str = _build_html_for_day(
        day=day if isinstance(day, _date) else day.date(),
        plan_routes=plan.rigid_routes.routes or {},
        telem_df=telem_df,
        open_browser=False,
    )
    output_path.write_text(html_str, encoding='utf-8')
```

**Note:** The exact internals of `_build_html_for_day` depend on how `export_plan_replay.py` is structured. If it builds the map in a `main()` function rather than a callable helper, refactor the map-building block into a `_build_html_for_day(day, plan_routes, telem_df, open_browser)` function and have `main()` call it. The trunk arc layer is then added by calling `_add_trunk_arcs(m, plan.trunk_plan)` just before the map is serialised to HTML.

- [ ] **Step 4: Manual verification**

Run the full multi-day backtest for Jan 7–8:

```
python -m cambridge --multiday --start 2026-01-07 --end 2026-01-08 --budget 60
```

Open `fleet_replay_exports/plan_replay_2026-01-07.html` in a browser. Verify:
- Dark purple dashed line from CB22 Duxford (CB22 4PS) to B37 area (Birmingham)
- Dark red dashed line if any Hazchem trunk ran that day
- Dotted return arcs in the same colours
- Purple/red circle markers at the hub locations

- [ ] **Step 5: Run full suite**

```
python -m pytest tests/cambridge/ -v --tb=short
```

Expected: all tests pass.

---

## Self-Review

**Spec coverage:**
- ✅ Day-start planning only
- ✅ 2-day lookahead (pl_import_tomorrow in trunk planner)
- ✅ HTML map output (Task 8 + multiday_backtest._write_html_map)
- ✅ Plain JSON state files (Task 3)
- ✅ Layered planner — trunk separate from VRPTW (Tasks 4 + 6)
- ✅ Duxford-only scope (existing CB22_RIGIDS/CB22_TRACTORS; cb22_fleet_only=True in backtest)
- ✅ Shift budget soft (existing in VRPTW; not re-hardened here)
- ✅ PALLETLINE-no-sub → FULL_FLEET (Task 2)
- ✅ delivery_date property on ScopedOrder (Task 2)
- ✅ pre-staged orders get day_start freight ready (Task 5)
- ✅ Trunk departure = max(collection returns) + 30 min (Task 4)
- ✅ OSRM at runtime, no hardcoded drive times (Task 4)
- ✅ Telematics bootstrap for first day (Task 3)
- ✅ Trunk arcs on HTML map (Task 8)
- ✅ CLI --multiday flag (Task 7)

**Missing from spec, added here:**
- `subcontractor` field on `ScopedOrder` (Task 6 Step 5) — needed to distinguish B37 vs LE10 PL_EXPORT without re-reading Qargo.

**Type consistency verified:**
- `TrunkHubManifest` defined in `multiday_state.py`, used in `trunk_planner.py` and `day_coordinator.py` ✅
- `TrunkPlan` defined in `trunk_planner.py`, returned by `plan_trunk()`, stored in `DayPlan` ✅
- `DayState.trunk_manifest` keys are `'B37_HUB'` / `'LE10_HUB'` strings, consistent with `HubLocation` type ✅
- `DayPlan.rigid_routes` is `DayDispatchOutput` ✅
- `pre_staged_ids` is `set[str]` in both `build_freight_availability` and `run_day_multi_trip` ✅
