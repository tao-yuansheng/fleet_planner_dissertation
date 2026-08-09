# Cambridge Dispatcher v2.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add (a) time-of-day road-speed multiplier to OSRM-backed routing, and (b) cumulative driver-hours enforcement + output collapse across multi-event days. Aligns dispatcher with two physical realities ignored by v1.9.

**Architecture:** Two changes share the file `simulation/routing.py` + `simulation/vrptw_engine.py`; the cumulative budget + collapse change is isolated to `cambridge/dispatcher.py` + `cambridge/backtest.py`. Calibration artefact (`tod_multiplier.json`) is produced by a new investigations script.

**Tech Stack:** Python 3.11, pandas (telematics), pytest, OSRM (already running).

---

## Files to be created or modified

- **Create:** `investigations/derive_tod_multiplier.py`
- **Create:** `data/Output/cambridge/tod_multiplier.json` (output of the script above)
- **Modify:** `simulation/routing.py` — add `depart_time` to `Router`, `HaversineRouter.duration_h`, `OSRMRouter.duration_h`; add `set_tod_multiplier`/`get_tod_multiplier`/`load_tod_multiplier`
- **Modify:** `simulation/vrptw_engine.py:_walk_schedule` — pass `t` as `depart_time`
- **Modify:** `cambridge/dispatcher.py:run_day_multi_trip` — actual on-duty decrement + cumulative cap; `build_rigid_for_event` accepts `remaining_budget_h`
- **Modify:** `cambridge/backtest.py` — call `collapse_route_keys`, count unique base ids
- **Create:** `simulation/route_collapse.py` (or add to `vrptw_alns.py`) — helper that merges `<vid>_E\d+` keys back to `<vid>`
- **Tests:**
  - `tests/test_routing.py` — TOD multiplier paths
  - `tests/test_vrptw_engine.py` — `_walk_schedule` passes time
  - `tests/cambridge/test_dispatcher.py` — cumulative cap + collapse

---

## Task 1: TOD multiplier calibration script

**Files:**
- Create: `investigations/derive_tod_multiplier.py`
- Output: `data/Output/cambridge/tod_multiplier.json`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_routing.py`:
```python
def test_load_tod_multiplier_returns_24_floats(tmp_path):
    import json
    from routing import load_tod_multiplier
    p = tmp_path / 'mul.json'
    p.write_text(json.dumps([1.0]*24))
    out = load_tod_multiplier(str(p))
    assert len(out) == 24
    assert all(isinstance(x, float) for x in out)

def test_load_tod_multiplier_returns_None_when_missing(tmp_path):
    from routing import load_tod_multiplier
    assert load_tod_multiplier(str(tmp_path / 'nope.json')) is None
```

- [ ] **Step 2: Run tests, verify fail**

Run: `cd BackEnd/logistics && pytest tests/test_routing.py::test_load_tod_multiplier_returns_24_floats -v`
Expected: FAIL (`load_tod_multiplier` not defined).

- [ ] **Step 3: Implement `load_tod_multiplier` in `simulation/routing.py`**

```python
import json as _json
from pathlib import Path as _Path

TOD_MULTIPLIER_DEFAULT_PATH = _Path(__file__).resolve().parents[1] / 'data' / 'Output' / 'cambridge' / 'tod_multiplier.json'

def load_tod_multiplier(path=TOD_MULTIPLIER_DEFAULT_PATH):
    """Return a 24-length list of float multipliers, or None if file missing."""
    p = _Path(path)
    if not p.exists():
        return None
    data = _json.loads(p.read_text())
    if not isinstance(data, list) or len(data) != 24:
        return None
    return [float(x) for x in data]
```

- [ ] **Step 4: Tests pass.**

- [ ] **Step 5: Write the calibration script** `investigations/derive_tod_multiplier.py`:

```python
"""Derive a 24-bucket time-of-day duration multiplier from Supatrak.

Output: data/Output/cambridge/tod_multiplier.json, a 24-element list
indexed by hour-of-day. Multiplier scales the cached (already truck-factored)
OSRM duration; 1.0 = baseline, > 1.0 = slower than baseline.

Calibration window: Supatrak Jan-Feb 2026, our-fleet only.
"""
import json
from pathlib import Path
import pandas as pd
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'cambridge'))

from cambridge.config import CB22_RIGIDS, CB22_TRACTORS

OUR_FLEET = set(CB22_RIGIDS) | set(CB22_TRACTORS)
SUPATRAK_FILES = [
    ROOT / 'data' / 'Input' / 'supatrak' / 'supatrak_telematics_cleaned_20260101_to_20260131.csv',
    ROOT / 'data' / 'Input' / 'supatrak' / 'supatrak_telematics_cleaned_20260201_to_20260228.csv',
]
OUT_PATH = ROOT / 'data' / 'Output' / 'cambridge' / 'tod_multiplier.json'


def main():
    frames = []
    for fp in SUPATRAK_FILES:
        if not fp.exists():
            print(f'skip missing {fp}')
            continue
        frames.append(pd.read_csv(fp, low_memory=False))
    df = pd.concat(frames, ignore_index=True)
    df['AssetName'] = df['AssetName'].astype(str)
    df = df[df['AssetName'].isin(OUR_FLEET)]
    df['Ignition'] = df['Ignition'].astype(str).str.lower()
    df = df[df['Ignition'] == 'on']
    df['GPSSpeed'] = pd.to_numeric(df['GPSSpeed'], errors='coerce')
    df = df[df['GPSSpeed'] > 5]
    df['LocalTime'] = pd.to_datetime(df['LocalTime'], errors='coerce')
    df = df.dropna(subset=['LocalTime', 'GPSSpeed'])
    df['hour'] = df['LocalTime'].dt.hour

    hourly = df.groupby('hour')['GPSSpeed'].agg(['mean', 'count']).reindex(range(24))
    print(hourly.to_string())
    overall_mean = hourly['mean'].mean()
    multiplier = (overall_mean / hourly['mean']).fillna(1.0)
    multiplier = multiplier.where(hourly['count'] >= 100, 1.0)  # low-confidence → 1.0
    print(f'overall_mean GPSSpeed = {overall_mean:.2f} km/h')
    print(f'multiplier range: {multiplier.min():.3f} .. {multiplier.max():.3f}')

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps([round(float(x), 3) for x in multiplier.tolist()], indent=2))
    print(f'wrote {OUT_PATH}')


if __name__ == '__main__':
    main()
```

- [ ] **Step 6: Run the script.** `cd BackEnd/logistics && python investigations/derive_tod_multiplier.py`. Verify output file has 24 entries and the range is sensible (0.85-1.25).

- [ ] **Step 7: Commit (do NOT execute git commit — user instruction: "do not commit any work !!! stay local").** Skip this step. Move on.

---

## Task 2: `Router.duration_h(... depart_time)` plumbing

**Files:**
- Modify: `simulation/routing.py`
- Test: `tests/test_routing.py`

- [ ] **Step 1: Write the failing test**

```python
def test_haversine_router_ignores_depart_time():
    from datetime import datetime, timezone
    from routing import HaversineRouter
    r = HaversineRouter()
    h0 = r.duration_h(52.1, 0.2, 52.3, 0.4)
    h1 = r.duration_h(52.1, 0.2, 52.3, 0.4, depart_time=datetime(2026, 1, 7, 8, tzinfo=timezone.utc))
    assert h0 == h1

def test_osrm_router_scales_by_tod_when_set():
    from datetime import datetime, timezone
    from routing import OSRMRouter, pair_key, set_tod_multiplier
    matrix = {pair_key(52.0, 0.0, 52.5, 0.5): (50.0, 1.0)}
    r = OSRMRouter(matrix=matrix)
    set_tod_multiplier([1.0]*24)
    base = r.duration_h(52.0, 0.0, 52.5, 0.5, depart_time=datetime(2026, 1, 7, 14, tzinfo=timezone.utc))
    assert abs(base - 1.0) < 1e-9
    mult = [1.0]*24
    mult[8] = 1.20
    set_tod_multiplier(mult)
    peak = r.duration_h(52.0, 0.0, 52.5, 0.5, depart_time=datetime(2026, 1, 7, 8, tzinfo=timezone.utc))
    assert abs(peak - 1.20) < 1e-9
    set_tod_multiplier(None)  # cleanup

def test_osrm_router_unchanged_when_no_depart_time():
    from routing import OSRMRouter, pair_key, set_tod_multiplier
    matrix = {pair_key(52.0, 0.0, 52.5, 0.5): (50.0, 1.0)}
    r = OSRMRouter(matrix=matrix)
    mult = [1.0]*24
    mult[8] = 1.20
    set_tod_multiplier(mult)
    h = r.duration_h(52.0, 0.0, 52.5, 0.5)  # no depart_time
    assert abs(h - 1.0) < 1e-9
    set_tod_multiplier(None)
```

- [ ] **Step 2: Run tests, verify fail.**

- [ ] **Step 3: Implement.**

In `simulation/routing.py`:
- Add to module: `_tod_multiplier: list | None = None`, `def set_tod_multiplier(vec): global _tod_multiplier; _tod_multiplier = vec`, `def get_tod_multiplier(): return _tod_multiplier`.
- Update `Router.Protocol`: `def duration_h(self, lat1, lon1, lat2, lon2, depart_time=None): ...`
- `HaversineRouter.duration_h(self, lat1, lon1, lat2, lon2, depart_time=None)` — keep computing `distance_km / avg_speed_kmh`, ignore `depart_time`.
- `OSRMRouter.duration_h(self, lat1, lon1, lat2, lon2, depart_time=None)` — get baseline via existing `_lookup(... idx=1)`; if `depart_time is not None` and `get_tod_multiplier() is not None`, multiply by `_tod_multiplier[depart_time.hour]`. Same hour-index for naive or tz-aware datetimes.

- [ ] **Step 4: Run all routing tests, verify pass.**

---

## Task 3: `_walk_schedule` passes the clock as `depart_time`

**Files:**
- Modify: `simulation/vrptw_engine.py:_walk_schedule`
- Test: `tests/test_vrptw_engine.py`

- [ ] **Step 1: Failing test.** Add to `tests/test_vrptw_engine.py`:

```python
def test_walk_schedule_passes_depart_time_to_router(monkeypatch):
    from datetime import datetime, timezone, timedelta
    import simulation.vrptw_engine as eng
    from simulation.vrptw_engine import DeliveryRoute, DeliveryStop, _walk_schedule

    calls = []
    class FakeRouter:
        def distance_km(self, a, b, c, d): return 50.0
        def duration_h(self, a, b, c, d, depart_time=None):
            calls.append(depart_time)
            return 1.0
    monkeypatch.setattr(eng, '_get_router', lambda: FakeRouter())

    T0 = datetime(2026, 1, 7, 7, 0, tzinfo=timezone.utc)
    route = DeliveryRoute(
        vehicle_id='V', depot_lat=52.0, depot_lon=0.0,
        shift_start=T0, shift_end=T0 + timedelta(hours=11),
        capacity_kg=10000, capacity_pallets=20, asset_type='Lorry',
        stops=[DeliveryStop(order_id='A', lat=52.2, lon=0.2,
                            weight_kg=100, pallets=1,
                            window_end=None, service_h=0.5)],
    )
    _walk_schedule(route)
    assert all(d is not None for d in calls)
    assert calls[0].hour == 7   # depart depot at shift_start
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement** — in `_walk_schedule`, change the two `router.duration_h(...)` calls to pass `depart_time=t`:

```python
leg_h = router.duration_h(prev_lat, prev_lon, stop.lat, stop.lon, depart_time=t)
...
return_h = router.duration_h(prev_lat, prev_lon, route.depot_lat, route.depot_lon, depart_time=t)
```

- [ ] **Step 4: Tests pass; also run the full test_vrptw_engine.py suite to verify no regression.**

---

## Task 4: Load TOD multiplier at dispatcher init

**Files:**
- Modify: `cambridge/dispatcher.py` (top of `run_day_multi_trip`)
- Test: `tests/cambridge/test_dispatcher.py`

- [ ] **Step 1: Failing test.**

```python
def test_run_day_multi_trip_installs_tod_when_file_present(monkeypatch, tmp_path):
    # Create a fake tod_multiplier.json at default path; monkeypatch the path.
    import json, simulation.routing as routing_mod
    monkeypatch.setattr(routing_mod, 'TOD_MULTIPLIER_DEFAULT_PATH', tmp_path / 'tod.json')
    (tmp_path / 'tod.json').write_text(json.dumps([1.0]*24))
    # No orders => early return path. Just ensure no crash and multiplier set.
    from cambridge.dispatcher import run_day_multi_trip
    from datetime import date
    out = run_day_multi_trip(day=date(2026, 1, 7), orders=[], trips=[],
                              postcode_cache={}, mode='forward',
                              solver_budget_s=1.0)
    assert routing_mod.get_tod_multiplier() == [1.0]*24
    routing_mod.set_tod_multiplier(None)
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement.** At the top of `run_day_multi_trip` (before the early-return for empty orders), add:

```python
from routing import load_tod_multiplier, set_tod_multiplier
mult = load_tod_multiplier()
if mult is not None:
    set_tod_multiplier(mult)
```

- [ ] **Step 4: Tests pass.**

---

## Task 5: Actual on-duty decrement in `run_day_multi_trip`

**Files:**
- Modify: `cambridge/dispatcher.py:run_day_multi_trip` lines ~593-600
- Test: `tests/cambridge/test_dispatcher.py`

- [ ] **Step 1: Failing test.** This test uses the production solver but verifies the budget bookkeeping. Create a synthetic scenario where Event A consumes ~10h and assert Event B's window is clipped to ~3h:

```python
def test_run_day_multi_trip_clips_event_b_to_cumulative_cap(monkeypatch):
    """If Event A uses 10h on-duty, Event B's shift_end must be clipped
    so total cumulative on-duty cannot exceed 13h."""
    # Strategy: monkeypatch event_out so Event A reports on_duty_minutes=600,
    # then assert the second-event rigid passed to run_event has shift_end
    # <= shift_start + 3 hours.
    import cambridge.dispatcher as cdisp
    captured_event_b_rigids = []

    original_run_event = cdisp.run_event
    def fake_run_event(inp, solver_budget_s):
        # Record the rigids passed in, then call the real solver.
        captured_event_b_rigids.append(list(inp.available_rigids))
        return original_run_event(inp, solver_budget_s)
    # ... (full test body in implementation)
```

(Full test code in the implementer's notes — use existing fixtures in `test_dispatcher.py`.)

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement.** Replace lines 593-600 of `run_day_multi_trip`:

```python
# v2.0: decrement by ACTUAL on-duty consumed (was fixed DEFAULT_TRIP_DURATION_H).
prior_on_duty_by_vid = {}  # vid -> cumulative on-duty hours used so far today
for vid, route in event_out.routes.items():
    if not isinstance(route, dict):
        continue
    used_h = (route.get('on_duty_minutes') or 0) / 60.0
    prior_on_duty_by_vid[vid] = prior_on_duty_by_vid.get(vid, 0.0) + used_h
    consumed = used_h + (DEPOT_DWELL_MIN / 60.0)
    new_budget = shift_remaining_h.get(vid, 0) - consumed
    # Also enforce cumulative 13h cap from vrptw_engine.MAX_ON_DUTY_HOURS:
    from vrptw_engine import MAX_ON_DUTY_HOURS
    cap_remaining = MAX_ON_DUTY_HOURS - prior_on_duty_by_vid[vid]
    new_budget = min(new_budget, cap_remaining)
    if new_budget <= 0:
        shift_remaining_h.pop(vid, None)
    else:
        shift_remaining_h[vid] = new_budget
```

- [ ] **Step 4: Tests pass; run full Cambridge dispatcher test suite.**

---

## Task 6: Output collapse helper

**Files:**
- Create: `simulation/route_collapse.py`
- Modify: `cambridge/backtest.py` (call collapse before JSON dump + report)
- Test: `tests/test_route_collapse.py` (new)

- [ ] **Step 1: Failing test.** New file `tests/test_route_collapse.py`:

```python
def test_collapse_merges_event_keys():
    from simulation.route_collapse import collapse_routes
    routes_in = {
        'V1':    {'stops': [{'order_id': 'A', 'arrival_iso': '2026-01-07T08:00:00'}],
                  'total_distance_km': 100.0, 'on_duty_minutes': 360,
                  'driving_minutes': 180, 'driver_gbp': 120.0,
                  'activation_gbp': 500.0, 'fuel_gbp': 30.0,
                  'estimated_cost_gbp': 650.0, 'lateness_minutes': 0,
                  'shift_start_iso': '2026-01-07T06:00:00',
                  'return_time_iso': '2026-01-07T12:00:00'},
        'V1_E2': {'stops': [{'order_id': 'B', 'arrival_iso': '2026-01-07T14:00:00'}],
                  'total_distance_km': 50.0, 'on_duty_minutes': 240,
                  'driving_minutes': 120, 'driver_gbp': 80.0,
                  'activation_gbp': 500.0, 'fuel_gbp': 15.0,
                  'estimated_cost_gbp': 595.0, 'lateness_minutes': 0,
                  'shift_start_iso': '2026-01-07T13:00:00',
                  'return_time_iso': '2026-01-07T17:00:00'},
    }
    out = collapse_routes(routes_in)
    assert set(out.keys()) == {'V1'}
    v1 = out['V1']
    assert [s['order_id'] for s in v1['stops']] == ['A', 'B']
    assert v1['total_distance_km'] == 150.0
    assert v1['on_duty_minutes'] == 600
    assert v1['driving_minutes'] == 300
    assert v1['driver_gbp'] == 200.0
    assert v1['activation_gbp'] == 1000.0
    assert v1['fuel_gbp'] == 45.0
    assert v1['estimated_cost_gbp'] == 1245.0
    assert v1['shift_start_iso'] == '2026-01-07T06:00:00'
    assert v1['return_time_iso'] == '2026-01-07T17:00:00'
    assert v1['events_combined'] == ['', '_E2']


def test_collapse_passthrough_when_no_event_suffix():
    from simulation.route_collapse import collapse_routes
    routes_in = {'V1': {'stops': [], 'total_distance_km': 10.0}}
    out = collapse_routes(routes_in)
    assert out == routes_in
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement** `simulation/route_collapse.py`:

```python
"""Collapse rolling-horizon `<vid>_E\\d+` routing slots into one logical record.

The Cambridge dispatcher emits one route per (vehicle, event); when a vehicle
runs Events A + B it surfaces twice (e.g. `M88GNW` and `M88GNW_E2`). This
helper merges them into a single `<vid>` record so downstream metrics and the
per-vehicle plan reflect the physical fleet.
"""
import re
from typing import Any

_SUFFIX_RE = re.compile(r'^(?P<base>.+?)(?P<suffix>_E\d+)?$')


def _base_and_suffix(key: str) -> tuple[str, str]:
    m = _SUFFIX_RE.match(key)
    return m.group('base'), (m.group('suffix') or '')


_SUM_KEYS = ('total_distance_km', 'on_duty_minutes', 'driving_minutes',
             'driver_gbp', 'activation_gbp', 'fuel_gbp',
             'estimated_cost_gbp', 'lateness_minutes')


def collapse_routes(routes: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict whose keys are base vehicle ids; merges any
    `<vid>_E\\d+` slots into the base vid. Non-dict values pass through."""
    groups: dict[str, list[tuple[str, dict]]] = {}
    for key, route in routes.items():
        if not isinstance(route, dict):
            groups.setdefault(key, []).append((key, route))
            continue
        base, suffix = _base_and_suffix(key)
        groups.setdefault(base, []).append((suffix, route))

    out: dict[str, Any] = {}
    for base, members in groups.items():
        if len(members) == 1 and members[0][0] == '':
            out[base] = members[0][1]
            continue
        if any(not isinstance(r, dict) for _, r in members):
            out[base] = members[0][1]
            continue
        # Merge.
        ordered = sorted(members, key=lambda sr: sr[0])  # '' < '_E2' < '_E3'
        suffixes = [s for s, _ in ordered]
        first = ordered[0][1]
        merged = dict(first)  # shallow copy
        for k in _SUM_KEYS:
            merged[k] = sum((m.get(k) or 0) for _, m in ordered)
        # Stops: concat ordered by arrival_iso.
        all_stops = []
        for _, m in ordered:
            all_stops.extend(m.get('stops') or [])
        all_stops.sort(key=lambda s: s.get('arrival_iso') or '')
        merged['stops'] = all_stops
        # Shift bookends.
        merged['shift_start_iso'] = min((m.get('shift_start_iso') for _, m in ordered if m.get('shift_start_iso')), default=None)
        merged['return_time_iso'] = max((m.get('return_time_iso') for _, m in ordered if m.get('return_time_iso')), default=None)
        merged['events_combined'] = suffixes
        out[base] = merged
    return out
```

- [ ] **Step 4: Tests pass.**

- [ ] **Step 5: Wire collapse into `cambridge/backtest.py`.** After `day_out = run_day_multi_trip(...)` and before the report aggregation:

```python
from simulation.route_collapse import collapse_routes
day_out.routes = collapse_routes(day_out.routes)
```

Also recompute `metrics['vehicles_used']` on the collapsed dict:

```python
day_out.metrics['vehicles_used'] = sum(
    1 for rt in day_out.routes.values()
    if isinstance(rt, dict) and rt.get('stops'))
```

- [ ] **Step 6: Run the full test suite. Verify all green.**

---

## Task 7: End-to-end verification

- [ ] **Step 1: Run `python investigations/derive_tod_multiplier.py`** if not already done — produce the artefact.

- [ ] **Step 2: Re-run Jan-7 OSRM with v2.0:**

```bash
cd BackEnd/logistics
CAMBRIDGE_OSRM=1 python -m cambridge --date 2026-01-07 --budget 60
```

Expected:
- Vehicles-used in report = unique base-id count (no `_E2` doubles).
- Per-vehicle `on_duty_minutes` ≤ 780.
- `tod_multiplier.json` is loaded (visible because timing changes across hours).

- [ ] **Step 3: Inspect** `data/Output/cambridge/vehicle_plan_2026-01-07.json`:
- No keys ending `_E\d+`.
- Each vehicle's `events_combined` shows what merged.
- Cumulative `on_duty_minutes` ≤ 780.

- [ ] **Step 4: Re-run 5-day window** `cambridge --start 2026-01-07 --end 2026-01-11` and confirm Jan-8 (heavy day) behaviour: total fleet on-duty distribution and vehicles-used count should both be more realistic than v1.9.

- [ ] **Step 5: Update Cambridge `README.md`** to reflect v2.0 (header, A30/A32 closed, Limitations 5/10 closed). Do NOT commit (per user instruction "stay local").

---

## Notes for implementer

- User instruction (durable): **do not commit any work**. Skip every `git add` / `git commit` step. Stay local.
- User instruction (durable): **do not mention "ZEEFLEET"** in any doc, code, or comment; substitute "our fleet".
- User instruction (durable): **use real data**; the calibration script reads actual Supatrak files in `data/Input/supatrak/`.
- TOD multiplier file is allowed to be absent — code must be no-op in that case.
- Backtest mode (`mode='backtest'`) uses telematics shift bounds; cumulative cap clip still applies via `shift_remaining_h`, but `build_rigid_for_event` does not need new args for backtest because telematics already gives a tight end time. The decrement-by-actual logic in Task 5 covers both modes.
