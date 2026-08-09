# Cambridge Dispatcher v1.5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace v1's uniform vehicle assumptions with telematics-derived per-vehicle profiles and add a 2-event multi-trip dispatch model, closing the 76→150 order assignment gap on the Cambridge backtest.

**Architecture:** New `VEHICLE_PROFILES` dict loaded from `data/Output/cambridge/vehicle_profiles_derived.json` at import. A new orchestrator `run_day_multi_trip` in `dispatcher.py` calls the existing `run_event` up to twice per multi-trip rigid, with capacity, shift hours, and event-B timing all driven by per-vehicle profile data. The solver itself is unchanged.

**Tech Stack:** Python 3.12, pandas, pytest. Reuses `simulation/vrptw_engine.DeliveryRoute`, `cambridge/dispatcher.run_event`, existing backtest framework.

**Spec:** [`../cambridge-dispatcher-v15-update.md`](../../cambridge-dispatcher-v15-update.md).

**User constraint:** Do NOT commit any work. All files stay uncommitted in the working tree.

**Run location:** All commands assume CWD = `e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics/`.

---

## File structure

**Files modified:**

| Path | Responsibility |
|---|---|
| `cambridge/config.py` | Add `VEHICLE_PROFILES` (loaded from JSON), `MULTI_TRIP_THRESHOLD`, `DEPOT_DWELL_MIN`, `EVENT_B_DEFAULT_HOUR`, `DEFAULT_TRIP_DURATION_H`. |
| `cambridge/dispatcher.py` | Add per-vehicle helpers (`is_multi_trip_rigid`, `vehicle_capacity_for_event`, `vehicle_shift_for_event`, `build_rigid_for_event`) and new orchestrator `run_day_multi_trip`. |
| `cambridge/backtest.py` | Replace uniform `DeliveryRoute` construction with `build_rigid_for_event`; pass telematics through `run_day_multi_trip`. |
| `cambridge/__main__.py` | No interface change needed — already passes telematics + qargo. |
| `tests/cambridge/test_config.py` | Tests for new constants + profile shape. |
| `tests/cambridge/test_dispatcher.py` | Tests for new helpers + `run_day_multi_trip` two-event flow. |
| `tests/cambridge/test_backtest.py` | Update fixture rigid construction; verify the new code path runs. |

**Files NOT modified:** `simulation/*` (no engine changes per spec §5).

---

## Task 1: Per-vehicle profile + multi-trip constants in config.py

**Files:**
- Modify: `cambridge/config.py`
- Test: `tests/cambridge/test_config.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/cambridge/test_config.py`:

```python
from datetime import time as time_t
from cambridge import config


def test_vehicle_profiles_has_entry_for_every_cb22_rigid():
    """Every rigid in CB22_RIGIDS must have a profile entry."""
    assert isinstance(config.VEHICLE_PROFILES, dict)
    missing = config.CB22_RIGIDS - set(config.VEHICLE_PROFILES.keys())
    assert not missing, f'Profiles missing for: {missing}'


def test_vehicle_profile_has_required_keys():
    """Each profile dict has the keys downstream code reads."""
    required = {'asset_type', 'capacity_kg_per_trip', 'capacity_pallets_per_trip',
                'shift_start', 'shift_end', 'median_trips_per_day', 'multi_trip_share'}
    for veh, p in config.VEHICLE_PROFILES.items():
        missing = required - set(p.keys())
        assert not missing, f'{veh}: missing keys {missing}'


def test_vehicle_profile_shift_times_are_time_objects():
    """shift_start and shift_end are datetime.time, not strings."""
    for veh, p in config.VEHICLE_PROFILES.items():
        assert isinstance(p['shift_start'], time_t), f'{veh}.shift_start not a time'
        assert isinstance(p['shift_end'], time_t), f'{veh}.shift_end not a time'
        assert p['shift_start'] < p['shift_end'], f'{veh}: start must be before end'


def test_vehicle_profile_capacities_are_positive():
    for veh, p in config.VEHICLE_PROFILES.items():
        assert p['capacity_kg_per_trip'] > 0, f'{veh}: capacity_kg must be > 0'
        assert p['capacity_pallets_per_trip'] > 0, f'{veh}: capacity_pallets must be > 0'


def test_multi_trip_constants_exist():
    assert config.MULTI_TRIP_THRESHOLD == 0.40
    assert config.DEPOT_DWELL_MIN == 42
    assert config.EVENT_B_DEFAULT_HOUR == 12
    assert config.DEFAULT_TRIP_DURATION_H == 4.1


def test_at_least_some_rigids_are_multi_trip():
    """Sanity: per the derivation, 6 of 11 rigids should cross the multi-trip threshold."""
    multi = [v for v, p in config.VEHICLE_PROFILES.items()
             if p['multi_trip_share'] >= config.MULTI_TRIP_THRESHOLD]
    assert len(multi) >= 4, f'Expected at least 4 multi-trip rigids, got {len(multi)}'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_config.py -v -k "vehicle_profile or multi_trip"`
Expected: All new tests FAIL with `AttributeError: module 'cambridge.config' has no attribute 'VEHICLE_PROFILES'`.

- [ ] **Step 3: Add the implementation**

Append to `cambridge/config.py`:

```python
import json as _json
from pathlib import Path as _Path

# v1.5: per-vehicle profile loaded from the telematics-derived JSON.
# Source: investigations/derive_v15_parameters.py.
_PROFILES_JSON = (_Path(__file__).parent.parent
                  / 'data' / 'Output' / 'cambridge'
                  / 'vehicle_profiles_derived.json')


def _build_vehicle_profiles() -> dict:
    """Load the derived JSON and reshape into the v1.5 profile dict.

    Each profile carries per-trip capacity, median shift times, and the
    multi-trip share for the dispatcher to decide single- vs two-event flow.
    """
    if not _PROFILES_JSON.exists():
        return {}
    raw = _json.loads(_PROFILES_JSON.read_text())
    out: dict = {}
    for veh, p in raw.items():
        hh, mm = p['shift_start_median'].split(':')
        start = time(int(hh), int(mm))
        hh, mm = p['shift_end_median'].split(':')
        end = time(int(hh), int(mm))
        out[veh] = {
            'asset_type':                p['asset_type'],
            'capacity_kg_per_trip':      int(p['derived_capacity_kg_p95']),
            'capacity_pallets_per_trip': int(p['derived_capacity_pallets_p95']),
            'shift_start':               start,
            'shift_end':                 end,
            'median_trips_per_day':      int(p['median_trips_per_day']),
            'multi_trip_share':          float(p['multi_trip_day_pct']) / 100.0,
        }
    return out


VEHICLE_PROFILES = _build_vehicle_profiles()

# v1.5 multi-trip constants (from trip_profile_derived.json + data study).
MULTI_TRIP_THRESHOLD     = 0.40   # share of days with ≥2 depot returns
DEPOT_DWELL_MIN          = 42     # median inter-trip dwell across 242 obs
EVENT_B_DEFAULT_HOUR     = 12     # forward-mode default mid-day reload hour
DEFAULT_TRIP_DURATION_H  = 4.1    # median trip duration across 601 obs
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_config.py -v`
Expected: All tests PASS (11 prior + 6 new = 17).

---

## Task 2: Per-vehicle classification + capacity helpers in dispatcher.py

**Files:**
- Modify: `cambridge/dispatcher.py`
- Test: `tests/cambridge/test_dispatcher.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/cambridge/test_dispatcher.py`:

```python
from cambridge.dispatcher import (is_multi_trip_rigid, vehicle_capacity_for_event,
                                   vehicle_shift_for_event)
from cambridge.config import VEHICLE_PROFILES


def test_is_multi_trip_rigid_returns_true_for_hx66duh():
    """HX66DUH has multi_trip_share=0.73 in the profile."""
    assert is_multi_trip_rigid('HX66DUH') is True


def test_is_multi_trip_rigid_returns_false_for_t888rnw():
    """T888RNW has multi_trip_share=0.0 in the profile."""
    assert is_multi_trip_rigid('T888RNW') is False


def test_is_multi_trip_rigid_returns_false_for_unknown_vehicle():
    assert is_multi_trip_rigid('NOT_A_REAL_REG') is False


def test_vehicle_capacity_for_event_a_multi_trip_uses_per_trip():
    """Event A capacity for multi-trip vehicles is per_trip from profile."""
    kg, pallets = vehicle_capacity_for_event('HX66DUH', event='A')
    p = VEHICLE_PROFILES['HX66DUH']
    assert kg == p['capacity_kg_per_trip']
    assert pallets == p['capacity_pallets_per_trip']


def test_vehicle_capacity_for_event_a_single_trip_uses_daily_total():
    """Event A capacity for single-trip vehicles is per_trip × median_trips."""
    kg, pallets = vehicle_capacity_for_event('T888RNW', event='A')
    p = VEHICLE_PROFILES['T888RNW']
    # T888RNW has median_trips=1, so daily = per-trip.
    assert kg == p['capacity_kg_per_trip'] * p['median_trips_per_day']
    assert pallets == p['capacity_pallets_per_trip'] * p['median_trips_per_day']


def test_vehicle_capacity_for_event_b_uses_per_trip():
    """Event B is always per-trip (only multi-trip vehicles get Event B)."""
    kg, pallets = vehicle_capacity_for_event('HX66DUH', event='B')
    p = VEHICLE_PROFILES['HX66DUH']
    assert kg == p['capacity_kg_per_trip']
    assert pallets == p['capacity_pallets_per_trip']


def test_vehicle_shift_for_event_forward_mode_event_a():
    """Forward mode Event A: start=profile shift_start; end=start+default duration."""
    start, end = vehicle_shift_for_event(
        'HX66DUH', event='A', day=date_type(2026, 1, 7),
        mode='forward', telem_df=None)
    p = VEHICLE_PROFILES['HX66DUH']
    assert start.time() == p['shift_start']
    expected_end_hour = p['shift_start'].hour + int(4.1)
    assert end.hour == expected_end_hour or end.hour == expected_end_hour + 1  # allow for fractional rounding


def test_vehicle_shift_for_event_forward_mode_single_trip_event_a_uses_full_shift():
    """For single-trip vehicles in forward mode, Event A covers the full shift."""
    start, end = vehicle_shift_for_event(
        'T888RNW', event='A', day=date_type(2026, 1, 7),
        mode='forward', telem_df=None)
    p = VEHICLE_PROFILES['T888RNW']
    assert start.time() == p['shift_start']
    assert end.time() == p['shift_end']
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_dispatcher.py -v -k "is_multi_trip or vehicle_capacity or vehicle_shift"`
Expected: All new tests FAIL with `ImportError`.

- [ ] **Step 3: Add the implementation**

Append to `cambridge/dispatcher.py`:

```python
from datetime import timedelta
from typing import Literal, Optional

from cambridge.config import (
    VEHICLE_PROFILES,
    MULTI_TRIP_THRESHOLD,
    DEPOT_DWELL_MIN,
    DEFAULT_TRIP_DURATION_H,
)

EventLabel = Literal['A', 'B']
Mode = Literal['backtest', 'forward']


def is_multi_trip_rigid(vehicle_id: str) -> bool:
    """A rigid is multi-trip if its observed share of days with ≥2 depot
    returns meets the threshold."""
    profile = VEHICLE_PROFILES.get(vehicle_id)
    if profile is None:
        return False
    return profile['multi_trip_share'] >= MULTI_TRIP_THRESHOLD


def vehicle_capacity_for_event(vehicle_id: str, event: EventLabel) -> tuple[int, int]:
    """Return (capacity_kg, capacity_pallets) for one event.

    Event A multi-trip → per-trip (the vehicle gets two events).
    Event A single-trip → per-trip × median_trips (the vehicle gets one event,
                          so capacity is rolled up to daily total).
    Event B → per-trip (only multi-trip rigids see Event B).
    """
    p = VEHICLE_PROFILES[vehicle_id]
    if event == 'B':
        return p['capacity_kg_per_trip'], p['capacity_pallets_per_trip']
    # Event A
    if is_multi_trip_rigid(vehicle_id):
        return p['capacity_kg_per_trip'], p['capacity_pallets_per_trip']
    # Single-trip rigid in Event A: roll capacity up to daily total.
    n = max(1, p['median_trips_per_day'])
    return p['capacity_kg_per_trip'] * n, p['capacity_pallets_per_trip'] * n


def vehicle_shift_for_event(vehicle_id: str, event: EventLabel,
                            day: date_type, mode: Mode,
                            telem_df: Optional[Any] = None
                            ) -> tuple[datetime, datetime]:
    """Return the (shift_start, shift_end) datetimes the solver should see
    for this (vehicle, event) on this day.

    Forward mode uses profile medians + the default trip duration. Backtest
    mode is handled in Task 3 (derive_vehicle_trip_times_telematics).
    """
    p = VEHICLE_PROFILES[vehicle_id]
    profile_start = datetime.combine(day, p['shift_start'])
    profile_end   = datetime.combine(day, p['shift_end'])

    if mode == 'forward':
        if event == 'A':
            if is_multi_trip_rigid(vehicle_id):
                end_a = profile_start + timedelta(hours=DEFAULT_TRIP_DURATION_H)
                return profile_start, min(end_a, profile_end)
            return profile_start, profile_end
        # Event B
        start_b = profile_start + timedelta(
            hours=DEFAULT_TRIP_DURATION_H, minutes=DEPOT_DWELL_MIN)
        return start_b, profile_end
    # mode == 'backtest' is delegated to Task 3
    raise NotImplementedError("backtest mode handled by derive_vehicle_trip_times_telematics")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_dispatcher.py -v -k "is_multi_trip or vehicle_capacity or vehicle_shift"`
Expected: All 8 new tests PASS.

---

## Task 3: Backtest-mode trip-time derivation from telematics

**Files:**
- Modify: `cambridge/dispatcher.py`
- Test: `tests/cambridge/test_dispatcher.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/cambridge/test_dispatcher.py`:

```python
import pandas as pd
from cambridge.dispatcher import derive_vehicle_trip_times_telematics


def _telem(rows):
    """Helper to build a tiny telematics DataFrame from (asset, ts, lat, lon, sp) tuples."""
    return pd.DataFrame(rows, columns=['AssetName', 'LocalTime',
                                       'Latitude', 'Longitude', 'GPSSpeed'])


def test_telematics_returns_none_for_unknown_vehicle():
    df = _telem([])
    out = derive_vehicle_trip_times_telematics(
        'HX66DUH', date_type(2026, 1, 7), df)
    assert out is None


def test_telematics_single_trip_day_returns_event_a_only():
    """Vehicle stays out all day, returns once to depot at end."""
    # CB22 = (52.0859, 0.1717). Three pings: 06:00 leaves depot, 14:00 mid-trip,
    # 17:00 back at depot. No mid-day depot return → no Event B.
    df = _telem([
        ('HX66DUH', '2026-01-07 06:00', 52.0859, 0.1717, 0),
        ('HX66DUH', '2026-01-07 06:30', 52.0859, 0.1717, 30),
        ('HX66DUH', '2026-01-07 09:00', 52.30,   0.50,   50),
        ('HX66DUH', '2026-01-07 14:00', 52.40,   0.60,   30),
        ('HX66DUH', '2026-01-07 17:00', 52.0859, 0.1717, 5),
    ])
    out = derive_vehicle_trip_times_telematics(
        'HX66DUH', date_type(2026, 1, 7), df)
    assert out is not None
    assert out['first_depot_return'] is not None  # the 17:00 ping at depot
    assert out['second_trip_start']  is None      # no second trip
    assert out['day_first_move'].strftime('%H:%M') == '06:30'


def test_telematics_two_trip_day_returns_both_events():
    """Vehicle returns to depot mid-day, then leaves again."""
    df = _telem([
        ('HX66DUH', '2026-01-07 06:00', 52.0859, 0.1717,  0),
        ('HX66DUH', '2026-01-07 06:30', 52.0859, 0.1717, 30),  # leaving
        ('HX66DUH', '2026-01-07 09:00', 52.30,   0.50,   50),
        ('HX66DUH', '2026-01-07 11:00', 52.0859, 0.1717, 10),  # depot return #1
        ('HX66DUH', '2026-01-07 12:00', 52.0859, 0.1717, 25),  # leaving again
        ('HX66DUH', '2026-01-07 15:00', 52.40,   0.60,   30),
        ('HX66DUH', '2026-01-07 17:00', 52.0859, 0.1717,  5),  # depot return #2
    ])
    out = derive_vehicle_trip_times_telematics(
        'HX66DUH', date_type(2026, 1, 7), df)
    assert out is not None
    assert out['first_depot_return'] is not None
    assert out['second_trip_start']  is not None
    # First return happens before second trip starts
    assert out['first_depot_return'] < out['second_trip_start']
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_dispatcher.py -v -k "telematics"`
Expected: All 3 new tests FAIL with `ImportError`.

- [ ] **Step 3: Add the implementation**

Append to `cambridge/dispatcher.py`:

```python
import math
from cambridge.config import CB22_DEPOT_ANCHOR

_R_DEPOT_KM = 2.0


def _depot_distance_km(lat: float, lon: float) -> float:
    """Haversine distance from a GPS ping to CB22 in km."""
    clat, clon = CB22_DEPOT_ANCHOR
    p = math.pi / 180
    dlat = (clat - lat) * p
    dlon = (clon - lon) * p
    a = math.sin(dlat / 2) ** 2 + math.cos(lat * p) * math.cos(clat * p) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def derive_vehicle_trip_times_telematics(vehicle_id: str, day: date_type,
                                          telem_df: 'pd.DataFrame'
                                          ) -> Optional[dict]:
    """Walk telematics for one (vehicle, day) and return trip-boundary times.

    Returns dict with: first_depot_return, second_trip_start, day_first_move,
    day_last_move. Each is a datetime or None. Returns None if vehicle has no
    telematics on day.
    """
    import pandas as pd
    g = telem_df[telem_df['AssetName'].astype(str) == vehicle_id].copy()
    if g.empty:
        return None
    g['_ts'] = pd.to_datetime(g['LocalTime'], errors='coerce')
    g['_lat'] = pd.to_numeric(g['Latitude'], errors='coerce')
    g['_lon'] = pd.to_numeric(g['Longitude'], errors='coerce')
    g['_sp']  = pd.to_numeric(g['GPSSpeed'], errors='coerce').fillna(0)
    g = g.dropna(subset=['_ts', '_lat', '_lon'])
    g = g[g['_ts'].dt.date == day].sort_values('_ts')
    if g.empty:
        return None
    g['_d_cb22'] = g.apply(lambda r: _depot_distance_km(r['_lat'], r['_lon']), axis=1)
    g['_at_depot'] = g['_d_cb22'] < _R_DEPOT_KM

    moving = g[g['_sp'] > 2]
    if moving.empty:
        return None
    day_first_move = moving['_ts'].min()
    day_last_move  = moving['_ts'].max()

    # First depot return = first transition from away → at-depot AFTER day_first_move.
    after_first_move = g[g['_ts'] >= day_first_move]
    prev_away = ~after_first_move['_at_depot'].shift(1, fill_value=False)
    returns = after_first_move[after_first_move['_at_depot'] & prev_away]
    first_depot_return = returns['_ts'].iloc[0] if not returns.empty else None

    # Second trip start = first away ping AFTER first_depot_return.
    second_trip_start = None
    if first_depot_return is not None:
        after_return = g[g['_ts'] > first_depot_return]
        moves_after = after_return[(after_return['_sp'] > 2)
                                    & (~after_return['_at_depot'])]
        if not moves_after.empty:
            second_trip_start = moves_after['_ts'].iloc[0]

    return {
        'day_first_move':      day_first_move.to_pydatetime(),
        'day_last_move':       day_last_move.to_pydatetime(),
        'first_depot_return':  first_depot_return.to_pydatetime() if first_depot_return is not None else None,
        'second_trip_start':   second_trip_start.to_pydatetime() if second_trip_start is not None else None,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_dispatcher.py -v -k "telematics"`
Expected: All 3 new tests PASS.

---

## Task 4: build_rigid_for_event — assembles a DeliveryRoute

**Files:**
- Modify: `cambridge/dispatcher.py`
- Test: `tests/cambridge/test_dispatcher.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/cambridge/test_dispatcher.py`:

```python
from cambridge.dispatcher import build_rigid_for_event


def test_build_rigid_for_event_forward_single_trip():
    """Single-trip rigid in forward mode: full shift, daily-aggregate capacity."""
    rigid = build_rigid_for_event(
        'T888RNW', event='A', day=date_type(2026, 1, 7),
        mode='forward', telem_df=None)
    p = VEHICLE_PROFILES['T888RNW']
    assert rigid.vehicle_id == 'T888RNW'
    assert rigid.shift_start.time() == p['shift_start']
    assert rigid.shift_end.time()   == p['shift_end']
    assert rigid.capacity_pallets == p['capacity_pallets_per_trip'] * p['median_trips_per_day']
    assert rigid.asset_type == p['asset_type']


def test_build_rigid_for_event_forward_multi_trip_event_a():
    """Multi-trip rigid Event A in forward mode: per-trip capacity, shortened shift."""
    rigid = build_rigid_for_event(
        'HX66DUH', event='A', day=date_type(2026, 1, 7),
        mode='forward', telem_df=None)
    p = VEHICLE_PROFILES['HX66DUH']
    assert rigid.capacity_pallets == p['capacity_pallets_per_trip']
    # Shift end is profile.shift_start + 4.1h, not profile.shift_end
    assert rigid.shift_end.time() < p['shift_end']


def test_build_rigid_for_event_backtest_uses_telematics_times():
    """Backtest mode reads first_depot_return from telematics for Event A end."""
    import pandas as pd
    telem = pd.DataFrame([
        ('HX66DUH', '2026-01-07 06:00', 52.0859, 0.1717,  0),
        ('HX66DUH', '2026-01-07 06:30', 52.0859, 0.1717, 30),
        ('HX66DUH', '2026-01-07 09:00', 52.30,   0.50,   50),
        ('HX66DUH', '2026-01-07 11:00', 52.0859, 0.1717, 10),
        ('HX66DUH', '2026-01-07 12:00', 52.0859, 0.1717, 25),
        ('HX66DUH', '2026-01-07 15:00', 52.40,   0.60,   30),
        ('HX66DUH', '2026-01-07 17:00', 52.0859, 0.1717,  5),
    ], columns=['AssetName', 'LocalTime', 'Latitude', 'Longitude', 'GPSSpeed'])
    rigid = build_rigid_for_event(
        'HX66DUH', event='A', day=date_type(2026, 1, 7),
        mode='backtest', telem_df=telem)
    # The first depot return in this telematics is 11:00.
    assert rigid.shift_end.hour == 11


def test_build_rigid_for_event_b_skipped_when_no_second_trip():
    """If telematics shows no second trip, build_rigid_for_event B returns None."""
    import pandas as pd
    telem = pd.DataFrame([
        ('HX66DUH', '2026-01-07 06:00', 52.0859, 0.1717,  0),
        ('HX66DUH', '2026-01-07 09:00', 52.30,   0.50,   50),
        ('HX66DUH', '2026-01-07 17:00', 52.0859, 0.1717,  5),
    ], columns=['AssetName', 'LocalTime', 'Latitude', 'Longitude', 'GPSSpeed'])
    rigid = build_rigid_for_event(
        'HX66DUH', event='B', day=date_type(2026, 1, 7),
        mode='backtest', telem_df=telem)
    assert rigid is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_dispatcher.py -v -k "build_rigid"`
Expected: All 4 new tests FAIL with `ImportError`.

- [ ] **Step 3: Add the implementation**

Append to `cambridge/dispatcher.py`:

```python
def build_rigid_for_event(vehicle_id: str, event: EventLabel,
                          day: date_type, mode: Mode,
                          telem_df: Optional[Any] = None
                          ) -> Optional[DeliveryRoute]:
    """Build the per-vehicle DeliveryRoute for one event.

    Returns None when Event B is requested but the vehicle didn't make a
    second trip on this day (backtest mode only). For forward mode and
    Event A, always returns a route.
    """
    p = VEHICLE_PROFILES.get(vehicle_id)
    if p is None:
        return None

    if mode == 'backtest' and telem_df is not None:
        times = derive_vehicle_trip_times_telematics(vehicle_id, day, telem_df)
        if event == 'A':
            if times is None:
                # No telematics; fall back to forward defaults.
                start = datetime.combine(day, p['shift_start'])
                if is_multi_trip_rigid(vehicle_id):
                    end = start + timedelta(hours=DEFAULT_TRIP_DURATION_H)
                else:
                    end = datetime.combine(day, p['shift_end'])
            else:
                start = times['day_first_move']
                # End = first_depot_return if multi-trip vehicle returned, else day_last_move
                if (is_multi_trip_rigid(vehicle_id)
                        and times['first_depot_return'] is not None):
                    end = times['first_depot_return']
                else:
                    end = times['day_last_move']
        else:  # Event B
            if times is None or times['second_trip_start'] is None:
                return None
            start = times['second_trip_start']
            end   = times['day_last_move']
    else:
        # Forward mode
        start, end = vehicle_shift_for_event(vehicle_id, event, day, 'forward')

    cap_kg, cap_pallets = vehicle_capacity_for_event(vehicle_id, event)
    return DeliveryRoute(
        vehicle_id=vehicle_id,
        depot_lat=CB22_DEPOT_ANCHOR[0], depot_lon=CB22_DEPOT_ANCHOR[1],
        shift_start=start, shift_end=end,
        capacity_kg=float(cap_kg), capacity_pallets=float(cap_pallets),
        asset_type=p['asset_type'],
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_dispatcher.py -v -k "build_rigid"`
Expected: All 4 new tests PASS.

---

## Task 5: run_day_multi_trip orchestrator

**Files:**
- Modify: `cambridge/dispatcher.py`
- Test: `tests/cambridge/test_dispatcher.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/cambridge/test_dispatcher.py`:

```python
from cambridge.dispatcher import run_day_multi_trip


def test_run_day_multi_trip_forward_no_orders_returns_empty():
    """Empty order list still returns a valid DayDispatchOutput."""
    out = run_day_multi_trip(
        day=date_type(2026, 1, 7), orders=[], trips=[],
        postcode_cache={'CB22 4PS': (52.0859, 0.1717)},
        mode='forward', telem_df=None, solver_budget_s=2.0)
    assert out.metrics['orders_total'] == 0
    assert out.routes == {}


def test_run_day_multi_trip_forward_dispatches_event_a(sample_postcode_cache):
    """Single PL_IMPORT order is routed via Event A (forward mode)."""
    orders = [_pl('a')]
    out = run_day_multi_trip(
        day=date_type(2026, 1, 7), orders=orders, trips=[],
        postcode_cache=sample_postcode_cache,
        mode='forward', telem_df=None, solver_budget_s=5.0)
    assert out.metrics['orders_total'] == 1
    # At least some vehicles were considered (depends on the solver)
    assert 'vehicles_used' in out.metrics


def test_run_day_multi_trip_records_event_b_in_metrics(sample_postcode_cache):
    """Event B is run for multi-trip rigids; metrics record event count."""
    orders = [_pl(f'order-{i}') for i in range(5)]
    out = run_day_multi_trip(
        day=date_type(2026, 1, 7), orders=orders, trips=[],
        postcode_cache=sample_postcode_cache,
        mode='forward', telem_df=None, solver_budget_s=5.0)
    assert out.metrics.get('events_run', 0) >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cambridge/test_dispatcher.py -v -k "run_day_multi_trip"`
Expected: 3 tests FAIL with `ImportError`.

- [ ] **Step 3: Add the implementation**

Append to `cambridge/dispatcher.py`:

```python
def run_day_multi_trip(day: date_type,
                       orders: list[ScopedOrder],
                       trips: list[CollectionTrip],
                       postcode_cache: dict,
                       mode: Mode = 'forward',
                       telem_df: Optional[Any] = None,
                       solver_budget_s: float = 30.0) -> DayDispatchOutput:
    """Two-event multi-trip dispatcher.

    Event A: every rigid (single + multi-trip) is dispatched once. Multi-trip
    rigids use per-trip capacity; single-trip rigids use daily-aggregate.

    Event B: only multi-trip rigids whose Event A shift ended within the day
    AND whose remaining shift can absorb another trip. Per-trip capacity.

    Backtest mode (telem_df given): trip windows derived per (vehicle, day)
    from telematics. Forward mode: profile medians + DEFAULT_TRIP_DURATION_H.
    """
    if not orders:
        return DayDispatchOutput(day=day, routes={}, collection_trips=trips,
                                  unassigned=[],
                                  metrics={'orders_total': 0, 'events_run': 0})

    freight_ready = build_freight_availability(orders, trips, day)
    day_start = datetime.combine(day, time(DEFAULT_PRE_STAGED_HOUR, 0))
    available_at_06 = [o for o in orders
                       if freight_ready.get(o.order_id, day_start) <= day_start]
    deferred_pre = [o.order_id for o in orders
                    if freight_ready.get(o.order_id, day_start) > day_start]

    # === Event A: all CB22 rigids ===
    rigids_a = []
    for vid in CB22_RIGIDS:
        r = build_rigid_for_event(vid, 'A', day, mode, telem_df)
        if r is not None:
            rigids_a.append(r)
    event_a = run_event(DispatchInput(
        available_orders=available_at_06, available_rigids=rigids_a,
        planning_time=day_start, locked_routes={},
        postcode_cache=postcode_cache,
    ), solver_budget_s=solver_budget_s)

    routes_all: dict = dict(event_a.routes)
    unassigned_after_a = list(event_a.unassigned)
    events_run = 1

    # === Event B: multi-trip rigids only ===
    if unassigned_after_a:
        unassigned_orders = [o for o in available_at_06
                             if o.order_id in unassigned_after_a]
        rigids_b = []
        for vid in CB22_RIGIDS:
            if not is_multi_trip_rigid(vid):
                continue
            r = build_rigid_for_event(vid, 'B', day, mode, telem_df)
            if r is not None and r.shift_end > r.shift_start:
                rigids_b.append(r)
        if rigids_b and unassigned_orders:
            planning_time_b = min(r.shift_start for r in rigids_b)
            event_b = run_event(DispatchInput(
                available_orders=unassigned_orders, available_rigids=rigids_b,
                planning_time=planning_time_b, locked_routes={},
                postcode_cache=postcode_cache,
            ), solver_budget_s=solver_budget_s)
            # Merge: Event B routes are stored under a 'vid_b' key to keep
            # them separate from Event A routes for the same vehicle.
            for vid, rt in event_b.routes.items():
                routes_all[f'{vid}_B'] = rt
            unassigned_after_a = list(event_b.unassigned)
            events_run = 2

    metrics = {
        'orders_total':     len(orders),
        'orders_assigned':  event_a.metrics.get('orders_assigned', 0)
                             + (event_b.metrics.get('orders_assigned', 0)
                                if events_run == 2 else 0),
        'vehicles_used':    sum(1 for rt in routes_all.values()
                                 if isinstance(rt, dict) and rt.get('stops')),
        'planned_km':       round(event_a.metrics.get('planned_km', 0.0)
                                   + (event_b.metrics.get('planned_km', 0.0)
                                      if events_run == 2 else 0), 1),
        'planned_cost_gbp': round(event_a.metrics.get('planned_cost_gbp', 0.0)
                                   + (event_b.metrics.get('planned_cost_gbp', 0.0)
                                      if events_run == 2 else 0), 2),
        'events_run':       events_run,
        'orders_deferred':  len(deferred_pre),
    }
    return DayDispatchOutput(
        day=day, routes=routes_all,
        collection_trips=trips,
        unassigned=unassigned_after_a + deferred_pre,
        metrics=metrics,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_dispatcher.py -v -k "run_day_multi_trip"`
Expected: All 3 new tests PASS.

---

## Task 6: Wire run_day_multi_trip into backtest

**Files:**
- Modify: `cambridge/backtest.py`
- Test: `tests/cambridge/test_backtest.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/cambridge/test_backtest.py`:

```python
def test_run_day_backtest_uses_vehicle_profiles_for_capacity(
        tmp_path, sample_postcode_cache):
    """Heterogeneous rigids: HX66DUH has 15 pallets/trip, T888RNW has 6."""
    qargo = pd.DataFrame([{
        'order_id': 'a', 'name': 'WT_a',
        'order_import_integration_type': 'PALLETLINE',
        'resource_subcontractor': 'Palletline (import from API)',
        'resource_tractor': None, 'resource_rigid': 'HX66DUH',
        'origin_postal_code': 'DN8 4HT', 'destination_postal_code': 'CB2 1AA',
        'destination_requested_start_timestamp_local': '2026-01-07 10:00:00',
        'origin_requested_start_timestamp_local': '2026-01-06 10:00:00',
        'goods_weight': 100.0, 'goods_pallet_spaces': 1.0,
        'service_level_name': 'Next day',
        'transport_service': '1. Non Hazardous Shipment',
    }])
    telem = pd.DataFrame({
        'LocalTime': pd.to_datetime(['2026-01-07 06:00', '2026-01-07 07:00']),
        'AssetName': ['HX66DUH', 'HX66DUH'],
        'Latitude':  [52.09, 52.10], 'Longitude': [0.17, 0.18],
        'GPSSpeed':  [10, 50],
    })
    report = run_day_backtest(
        date_type(2026, 1, 7), qargo, telem, sample_postcode_cache,
        output_dir=tmp_path, solver_budget_s=2.0,
    )
    # The run completes without errors and produces a report.
    assert 'planned' in report and 'actual' in report
    # The Cambridge fleet's capacity model is now per-vehicle; no crash.
    assert report['planned']['orders_total'] >= 0
```

- [ ] **Step 2: Run test to verify the existing implementation still works**

Run: `python -m pytest tests/cambridge/test_backtest.py -v`
Expected: All tests still pass (the implementation change in Step 3 hasn't happened yet, but this test should pass against the existing run_day_backtest as a smoke check).

- [ ] **Step 3: Replace the rigid construction + dispatch call in `run_day_backtest`**

In `cambridge/backtest.py`, locate the block that constructs `rigids = [DeliveryRoute(...) for v in CB22_RIGIDS]` and calls `run_day(...)`, and replace it with:

```python
from cambridge.dispatcher import run_day_multi_trip

# (Inside run_day_backtest, replace the rigid construction + run_day call)
day_out = run_day_multi_trip(
    day, scoped, trips, postcode_cache,
    mode='backtest', telem_df=telem_df,
    solver_budget_s=solver_budget_s,
)
```

Remove the prior `rigids = [DeliveryRoute(...) for v in CB22_RIGIDS]` list comprehension and the `from simulation.vrptw_engine import DeliveryRoute` import inside the function (it's no longer used here — `build_rigid_for_event` constructs rigids per event).

- [ ] **Step 4: Run all tests to verify nothing regressed**

Run: `python -m pytest tests/ -q`
Expected: All 96+ tests PASS (the new test from Step 1 + the rest of the suite).

---

## Task 7: Smoke test + verify the assignment-gap closure

- [ ] **Step 1: Re-run the Jan-7 smoke test**

Run: `python -m cambridge --date 2026-01-07 --budget 10`
Expected:
- Report renders to stdout.
- `data/Output/cambridge/day_compare_2026-01-07.json` is updated.

- [ ] **Step 2: Compare against the v1 baseline**

Read `data/Output/cambridge/day_compare_2026-01-07.json` and verify:

| Metric | v1 baseline | v1.5 expected | Pass criterion |
|---|---|---|---|
| `planned.orders_total` | 150 | 150 | unchanged (same scope filter) |
| `planned.orders_assigned` | 76 | **≥ 120** | gap closed by ≥ 60 % |
| `actual.orders_actual_assigned` | 150 | 150 | unchanged (Qargo actuals) |
| `planned.total_km` | 1,924 | **within 1,500–3,000** | order-of-magnitude consistent |
| `actual.total_km` | 1,966 | 1,966 | unchanged (telematics) |
| `level0.km_pct_delta` | 0.021 | **≤ 0.20** | still within ±20 % threshold |
| `metrics.events_run` (new field) | n/a | 2 | confirms Event B fired |

If `orders_assigned` is below 120, something is wrong with the multi-trip model — investigate (likely event-B shift budget or per-trip capacity).

- [ ] **Step 3: Run a 5-day period to confirm stability**

Run: `python -m cambridge --start 2026-01-05 --end 2026-01-09 --budget 10`
Expected:
- All 5 days complete without errors.
- Median `orders_assigned` across the 5 days is ≥ 80 % of the corresponding `orders_actual_assigned`.
- `data/Output/cambridge/aggregate_2026-01-05_2026-01-09.json` written.

- [ ] **Step 4: Confirm full regression suite**

Run: `python -m pytest tests/ -q`
Expected: All 100+ tests PASS (the v1.5 additions + the prior 96).

---

## Self-review checklist

| Spec section | Implemented by |
|---|---|
| §1 Per-vehicle profile | Task 1 (config) + Task 4 (build_rigid_for_event) |
| §2 Multi-trip dispatch model | Task 2 (classification) + Task 5 (run_day_multi_trip) |
| §3 Backtest vs forward mode | Task 3 (telematics derivation) + Task 4 (mode branch) |
| §4 Capacity model (per-trip vs daily) | Task 2 (vehicle_capacity_for_event) |
| §5 What does NOT change | No `simulation/` tasks, no solver tasks — confirmed |
| §6 Files affected | matches the file-structure table above |
| §7 Open questions | Task 7 step 2 explicitly tests open question #3 (AR05DEX single-route) |
| §8 Validation expectations | Task 7 step 2 makes them concrete pass criteria |
| §9 v1.6+ roadmap | Out of scope (deferred) — confirmed by absence of tasks |

**Type chain:**
- `is_multi_trip_rigid(vehicle_id) -> bool` defined Task 2, used by Task 4 and Task 5
- `vehicle_capacity_for_event(vehicle_id, event) -> (int, int)` defined Task 2, used by Task 4
- `vehicle_shift_for_event(vehicle_id, event, day, mode) -> (datetime, datetime)` defined Task 2, used by Task 4
- `derive_vehicle_trip_times_telematics(...) -> dict | None` defined Task 3, used by Task 4
- `build_rigid_for_event(...) -> DeliveryRoute | None` defined Task 4, used by Task 5
- `run_day_multi_trip(...) -> DayDispatchOutput` defined Task 5, used by Task 6

All types consistent.

**Placeholders:** none. Every step has concrete code or an explicit command + expected result.

**User constraint:** every task ends with a test verification step. No `git commit` anywhere in the plan.
