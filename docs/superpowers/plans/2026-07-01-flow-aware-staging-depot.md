# Flow-Aware Staging-Depot Resolution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Out-of-territory freight stages at a real, resourced gateway depot (imports → CB22; far collections → nearest of CB22/Bedford) instead of the geographically-nearest anchor, which today mis-stages northern deliveries at the dockless Stoke ST4 satellite.

**Architecture:** One new pure helper `resolve_staging_depot` in `cambridge/scope.py` becomes the single place a postcode turns into a real depot (`assign_depot` stays the territory authority and still returns `OVERFLOW`). `freight_planner/legs.py` calls it so legs never emit `"OVERFLOW"` as a dispatchable `source_depot`. A one-line simplification in `freight_planner/tour_plan.py` makes the tour bucketer trust any real `source_depot` (not just FULL_FLEET). Consolidation (`resolve_cluster`) already trusts a real `source_depot`, so it needs no change.

**Tech Stack:** Python 3.12, pytest, pandas. Postcode→depot territory map in `cambridge/config.py`/`cambridge/scope.py`.

**Design spec:** `docs/superpowers/specs/2026-07-01-flow-aware-staging-depot-design.md`

---

## Conventions (read first)

**Run tests** from `BackEnd/logistics` with the project venv:

```bash
cd /e/BEAT/ZECURE-Phase2-main/BackEnd/logistics && PYTHONPATH=. PYTHONIOENCODING=utf-8 \
  /e/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe -m pytest <path> -q
```

**No git commits this session** (standing stakeholder constraint). The usual per-task
"commit" step is replaced by a **full-suite run** checkpoint. Write files only.

**Background gotcha:** OSRM (localhost:5000) has no client-side timeout; only the
end-to-end run (Task 5) touches it. Tasks 1–4 are pure/offline.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `cambridge/scope.py` | territory + staging resolution | **add** `GATEWAY_DEPOTS`, `_nearest_gateway`, `resolve_staging_depot` |
| `freight_planner/legs.py` | emit movement legs | **wire** resolver into `origin_depot`/`dest_depot` |
| `freight_planner/tour_plan.py` | tour bucketing | **extract** `_anchor_or_nearest`, drop FULL_FLEET-only condition |
| `tests/cambridge/test_scope.py` | scope unit tests | **add** resolver tests |
| `tests/freight_planner/test_legs_staging.py` | legs wiring | **create** |
| `tests/freight_planner/test_tour_plan.py` | bucketing | **add** helper test |
| `tests/freight_planner/test_tours.py` | consolidation | **add** regression guard |

---

## Task 1: `resolve_staging_depot` in `cambridge/scope.py`

**Files:**
- Modify: `cambridge/scope.py` (add after `assign_depot`, which ends at line 141)
- Test: `tests/cambridge/test_scope.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/cambridge/test_scope.py` (extend the existing import on line 4):

```python
from cambridge.scope import (
    classify_order, in_cambridge_scope, resolve_staging_depot, GATEWAY_DEPOTS,
)


def test_gateway_depots_are_the_capable_members_only():
    # ST4 satellite and the empty ST_IVES yard are NOT gateways
    assert GATEWAY_DEPOTS == ("CB22", "BEDFORD")


def test_in_territory_delivery_is_unchanged():
    # MK is Bedford territory -> stays Bedford even as a delivery anchor
    assert resolve_staging_depot("MK1 1AA", is_delivery_anchor=True) == "BEDFORD"


def test_in_territory_stoke_origin_is_unchanged():
    assert resolve_staging_depot("ST4 8HP", is_delivery_anchor=False) == "STOKE"


def test_overflow_delivery_goes_to_cb22_gateway():
    # EH48 (Scotland) is outside all territory -> import stages at CB22, not Stoke
    assert resolve_staging_depot("EH48 2HA", is_delivery_anchor=True) == "CB22"


def test_overflow_collection_uses_nearest_gateway():
    # far origin west of both gateways (Manchester) -> Bedford is closer than CB22
    assert resolve_staging_depot("M1 1AA", is_delivery_anchor=False,
                                 lat=53.48, lon=-2.24) == "BEDFORD"
    # far origin east (Ipswich-ish) -> CB22 closer
    assert resolve_staging_depot("IP1 1AA", is_delivery_anchor=False,
                                 lat=52.05, lon=1.15) == "CB22"


def test_overflow_collection_without_coords_defaults_cb22():
    assert resolve_staging_depot("M1 1AA", is_delivery_anchor=False) == "CB22"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... -m pytest tests/cambridge/test_scope.py -q`
Expected: FAIL with `ImportError: cannot import name 'resolve_staging_depot'`.

- [ ] **Step 3: Implement the helper**

In `cambridge/scope.py`, immediately after `assign_depot` (after its final
`return 'OVERFLOW'` on line 141), add:

```python
# ── Staging-depot resolution ────────────────────────────────────────────────
# assign_depot() returns OVERFLOW for any postcode outside a depot territory.
# resolve_staging_depot() turns that OVERFLOW into a *real* gateway depot so
# freight never stages at a virtual location or the geographically-nearest anchor
# (which for anything northern is the dockless Stoke ST4 satellite). The gateways
# are the capable member depots that trunk the B37 Palletline hub; the ST4
# satellite and the (empty) ST_IVES yard are deliberately NOT eligible.
GATEWAY_DEPOTS: Tuple[str, ...] = ("CB22", "BEDFORD")


def _nearest_gateway(lat: float, lon: float) -> str:
    """Return the nearest capable gateway depot (CB22/BEDFORD) to a point."""
    anchors = {"CB22": CB22_DEPOT_ANCHOR, "BEDFORD": BEDFORD_DEPOT_ANCHOR}
    return min(anchors,
              key=lambda d: _haversine_km(anchors[d][0], anchors[d][1], lat, lon))


def resolve_staging_depot(pc: Optional[str], *, is_delivery_anchor: bool,
                          lat: Optional[float] = None,
                          lon: Optional[float] = None) -> str:
    """Map an (already-cleaned) postcode to a real staging depot.

    In-territory postcodes return their owning depot unchanged (incl. STOKE).
    OVERFLOW freight resolves to a capable gateway instead of a virtual/nearest
    anchor:
      * a delivery anchor (import last-mile needs a dock) -> CB22 (capability-primary);
      * a collection anchor -> the nearest gateway to the collection point.
    """
    depot = assign_depot(pc)
    if depot != 'OVERFLOW':
        return depot
    if is_delivery_anchor:
        return 'CB22'
    if lat is not None and lon is not None:
        return _nearest_gateway(lat, lon)
    return 'CB22'
```

(`Tuple`/`Optional` are already imported on line 6; `CB22_DEPOT_ANCHOR`/
`BEDFORD_DEPOT_ANCHOR` on lines 78-79; `_haversine_km` is defined on line 362 —
forward reference is fine because it resolves at call time.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... -m pytest tests/cambridge/test_scope.py -q`
Expected: PASS (all scope tests, new and existing).

- [ ] **Step 5: Checkpoint** — no commit. Proceed to Task 2.

---

## Task 2: Wire the resolver into `freight_planner/legs.py`

**Files:**
- Modify: `freight_planner/legs.py:9-14` (import), `freight_planner/legs.py:273-274` (depot computation)
- Test: `tests/freight_planner/test_legs_staging.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/freight_planner/test_legs_staging.py`:

```python
from __future__ import annotations

import pandas as pd

from freight_planner.demand import DemandRecord
from freight_planner.legs import build_movement_leg_records

CACHE = {
    "EH48 2HA": (55.90, -3.60),   # Bathgate, Scotland (outside all territory)
    "MK1 1AA": (52.04, -0.75),    # Milton Keynes (Bedford territory)
    "ST4 8HP": (52.97, -2.17),    # Stoke yard (Stoke territory)
    "CB9 8QP": (52.08, 0.44),     # Haverhill (CB22 territory)
    "M1 1AA": (53.48, -2.24),     # Manchester (outside territory, west)
    "B37 7HB": (52.46, -1.73),    # Palletline hub
}


def _demand(flow, origin_pc, dest_pc, order_id="O1"):
    return DemandRecord(
        order_id=order_id, order_name="WT", status="OK",
        raw_flow=flow, corrected_flow=flow,
        responsibility_shape="FULL_END_TO_END", responsibility_source="rule",
        exclusion_reason="", origin_pc=origin_pc, destination_pc=dest_pc,
        collect_date="2026-01-08", deliver_date="2026-01-08",
        collect_timestamp="2026-01-08 08:00:00", deliver_timestamp="2026-01-08 15:00:00",
        pallets=2.0, weight_kg=200.0, historical_resources="T1",
        resource_subcontractor="", network=flow,
    )


def _qargo(order_id, origin_pc, dest_pc):
    return pd.DataFrame([{
        "order_id": order_id, "name": "WT",
        "origin_postal_code": origin_pc, "destination_postal_code": dest_pc,
        "origin_timestamp_local": "2026-01-08 08:00:00",
        "origin_requested_start_timestamp_local": "2026-01-08 08:00:00",
        "destination_timestamp_local": "2026-01-08 15:00:00",
        "destination_requested_start_timestamp_local": "2026-01-08 15:00:00",
        "destination_time_window_value": "",
        "goods_pallet_spaces": 2.0, "goods_weight": 200.0,
        "resource_subcontractor": "", "order_import_integration_type": "",
    }])


def _source_of(legs, leg_kind):
    return next(l.source_depot for l in legs if l.leg_kind == leg_kind)


def test_scotland_import_stages_at_cb22_not_overflow():
    d = _demand("PL_IMPORT", "B37 7HB", "EH48 2HA")
    legs = build_movement_leg_records(_qargo("O1", "B37 7HB", "EH48 2HA"), [d], CACHE)
    assert _source_of(legs, "CUSTOMER_DELIVERY") == "CB22"


def test_in_territory_import_is_unchanged_bedford():
    d = _demand("PL_IMPORT", "B37 7HB", "MK1 1AA")
    legs = build_movement_leg_records(_qargo("O2", "B37 7HB", "MK1 1AA"), [d], CACHE)
    assert _source_of(legs, "CUSTOMER_DELIVERY") == "BEDFORD"


def test_stoke_origin_fullfleet_is_unchanged():
    d = _demand("FULL_FLEET", "ST4 8HP", "M1 1AA")
    legs = build_movement_leg_records(_qargo("O3", "ST4 8HP", "M1 1AA"), [d], CACHE)
    # FULL_FLEET source_depot is the origin depot; an ST origin stays STOKE
    assert _source_of(legs, "CUSTOMER_PICKUP") == "STOKE"


def test_overflow_collection_uses_nearest_gateway():
    # PL_EXPORT collected in Manchester (outside territory, west) -> nearest gateway BEDFORD
    d = _demand("PL_EXPORT", "M1 1AA", "CB9 8QP")
    legs = build_movement_leg_records(_qargo("O4", "M1 1AA", "CB9 8QP"), [d], CACHE)
    assert _source_of(legs, "CUSTOMER_PICKUP") == "BEDFORD"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... -m pytest tests/freight_planner/test_legs_staging.py -q`
Expected: FAIL — `test_scotland_import_...` gets `"OVERFLOW"`, not `"CB22"`;
`test_overflow_collection_...` gets `"OVERFLOW"`, not `"BEDFORD"`.

- [ ] **Step 3: Add the import**

In `freight_planner/legs.py`, extend the `cambridge.scope` import (lines 9-14) to
include `resolve_staging_depot`:

```python
from cambridge.scope import (
    _collection_window,
    _delivery_window_policy,
    _pl_export_window,
    assign_depot,
    resolve_staging_depot,
)
```

- [ ] **Step 4: Wire the resolver into the depot computation**

In `freight_planner/legs.py`, replace lines 273-274:

```python
        origin_depot = depot_for_pc(origin_pc)
        dest_depot = depot_for_pc(dest_pc)
```

with:

```python
        o_lat, o_lon = latlon(origin_pc, postcode_cache)
        origin_depot = resolve_staging_depot(origin_pc, is_delivery_anchor=False,
                                             lat=o_lat, lon=o_lon)
        dest_depot = resolve_staging_depot(dest_pc, is_delivery_anchor=True)
```

(`latlon` is the module helper on line 80. The inner `o_lat, o_lon = latlon(...)`
recomputations already present in the PL_EXPORT / FULL_FLEET branches are harmless —
they reassign the same cached value — leave them.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `... -m pytest tests/freight_planner/test_legs_staging.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Remove the now-dead `depot_for_pc` (and its only-user import)**

`depot_for_pc` (legs.py:84-85) was the only caller of both itself and the
`assign_depot` import; the wiring in Step 4 replaced its two call sites. Confirm
nothing else uses it:

```bash
cd /e/BEAT/ZECURE-Phase2-main/BackEnd/logistics && grep -rn "depot_for_pc" --include=*.py .
```
Expected: only the definition at `freight_planner/legs.py:84`.

If so, atomically (a) delete the `depot_for_pc` function (lines 84-85), and (b) drop
`assign_depot` from the `cambridge.scope` import so it reads:

```python
from cambridge.scope import (
    _collection_window,
    _delivery_window_policy,
    _pl_export_window,
    resolve_staging_depot,
)
```
(If the grep shows any other file references `depot_for_pc`, skip this step and leave
both in place.)

- [ ] **Step 7: Full-suite checkpoint**

Run: `... -m pytest tests/freight_planner tests/cambridge -q`
Expected: PASS (no regressions — confirms `assign_depot` was unused in `legs.py`).
No commit.

---

## Task 3: Trust any real `source_depot` in `freight_planner/tour_plan.py`

**Files:**
- Modify: `freight_planner/tour_plan.py` (add helper near line 100; edit call site 223-227)
- Test: `tests/freight_planner/test_tour_plan.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/freight_planner/test_tour_plan.py`:

```python
from freight_planner.tour_plan import _anchor_or_nearest


def test_anchor_or_nearest_trusts_real_source_depot():
    # a non-FULL_FLEET import staged at CB22 anchors at CB22, NOT the nearest anchor
    # to a far-north delivery (which would be the STOKE satellite)
    assert _anchor_or_nearest("CB22", GLASGOW[0], GLASGOW[1]) == "CB22"


def test_anchor_or_nearest_falls_back_for_non_anchor():
    # an unknown/virtual source depot still falls back to the nearest anchor
    assert _anchor_or_nearest("OVERFLOW", GLASGOW[0], GLASGOW[1]) == "STOKE"
```

(`GLASGOW = (55.86, -4.25)` is already defined at the top of this test file.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... -m pytest tests/freight_planner/test_tour_plan.py -q`
Expected: FAIL with `ImportError: cannot import name '_anchor_or_nearest'`.

- [ ] **Step 3: Add the helper**

In `freight_planner/tour_plan.py`, add after `_centroid_proto` (near line 100, before
`run_multiday_seed_plan`). `nearest_depot` (imported line 46) and `DEPOT_ANCHORS`
(imported line 24) are already available:

```python
def _anchor_or_nearest(src: str, lat: float, lon: float) -> str:
    """Trust a real staging depot; fall back to the nearest anchor only when the
    source depot is not a known anchor (should not happen once legs resolve every
    OVERFLOW to a real gateway)."""
    return src if src in DEPOT_ANCHORS else nearest_depot(lat, lon)[0]
```

- [ ] **Step 4: Simplify the call site**

In `freight_planner/tour_plan.py`, replace lines 223-227:

```python
        src = str(_g(row, "source_depot", ""))
        if str(_g(row, "flow", "")) == "FULL_FLEET" and src in DEPOT_ANCHORS:
            depot = src
        else:
            depot = nearest_depot(c[0], c[1])[0]
```

with:

```python
        src = str(_g(row, "source_depot", ""))
        depot = _anchor_or_nearest(src, c[0], c[1])
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `... -m pytest tests/freight_planner/test_tour_plan.py -q`
Expected: PASS.

- [ ] **Step 6: Full-suite checkpoint**

Run: `... -m pytest tests/freight_planner -q`
Expected: PASS. No commit.

---

## Task 4: Regression guard for the consolidated Scotland outcome

**Files:**
- Test: `tests/freight_planner/test_tours.py`

This guards the integrated contract the whole fix relies on: once legs emit a real
`source_depot`, `resolve_cluster` anchors the Scotland cluster at CB22 (most pallets)
with a genuine Stoke load-stop for the one ST-staged order. `resolve_cluster` is
unchanged, so this test **passes on first run** — it is a regression guard, not a
red-green driver.

- [ ] **Step 1: Add the guard test**

Add to `tests/freight_planner/test_tours.py` (it already imports `RouteJob`,
`resolve_cluster`, `DEPOT_LOAD`, `RouteVehicle`, and defines `_job`):

```python
def _proto_real(depot):
    from cambridge.config import DEPOT_ANCHORS
    lat, lon = DEPOT_ANCHORS[depot]
    return RouteVehicle(
        vehicle_id=f"P:{depot}", start_node=depot, start_lat=lat, start_lon=lon,
        start_time="2026-01-12 06:00:00", capacity_pallets=26.0, capacity_kg=24000.0,
        vehicle_type="tractor", home_depot=depot, home_lat=lat, home_lon=lon,
    )


def test_scotland_import_and_ff_anchor_cb22_with_stoke_loadstop():
    # EH48 import staged at CB22 (post-fix), ML6 full-fleet staged at STOKE, both
    # delivered in Scotland -> ONE tour anchored CB22 + a Stoke load-stop for ML6.
    eh48 = _job("eh48", 55.90, -3.60, pallets=6.0, kg=5000.0)   # CB22 (more pallets)
    ml6 = _job("ml6", 55.87, -3.97, pallets=2.0, kg=1500.0)     # STOKE
    src = {"eh48": "CB22", "ml6": "STOKE"}.get
    out = resolve_cluster([eh48, ml6], src, due_by_job=None, proto_for=_proto_real)

    assert len(out) == 1
    depot, ordered, ev = out[0]
    assert depot == "CB22"                                       # primary = most pallets
    assert ev.feasible
    load_nodes = {j.node for j in ordered if j.leg_kind == DEPOT_LOAD}
    assert load_nodes == {"STOKE"}                              # one load-stop, at Stoke
```

- [ ] **Step 2: Run the test**

Run: `... -m pytest tests/freight_planner/test_tours.py -q`
Expected: PASS.

- [ ] **Step 3: Full suite**

Run: `... -m pytest -q`
Expected: PASS (whole suite, ~290 tests). No commit.

---

## Task 5: End-to-end validation on 2026-01-12…17

**Files:**
- Run: `freight_planner/run_alns.py` (no code change)
- Update: `freight_planner/QUEST_LOG.md`, memory note

- [ ] **Step 1: Run the pipeline for the window**

```bash
cd /e/BEAT/ZECURE-Phase2-main/BackEnd/logistics && PYTHONPATH=. PYTHONIOENCODING=utf-8 \
  PYTHONDONTWRITEBYTECODE=1 /e/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe \
  -m freight_planner.run_alns --start 2026-01-12 --end 2026-01-17 --time-budget 90
```
Expected: completes; prints an assignment/coverage summary and writes a run folder
under the default out-dir with a `plan/` subfolder.

- [ ] **Step 2: Assert coverage did not regress**

Read the printed assignment rate (and/or the run's coverage report). Expected:
**≥ 99.3%** (the pre-change baseline). If it dropped, STOP and investigate — do not
proceed to the memory update.

- [ ] **Step 3: Confirm EH48 no longer stages at Stoke**

The Scotland import order id is `fbcb92a2` (Bathgate EH48). In the run's `plan/`
folder:
```bash
grep -n "fbcb92a2" <run>/plan/route_stops.csv | head
```
Expected: its delivery rides a tour whose anchor/vehicle is CB22-based (not a
STOKE-anchored tour); the tour that serves it now shows a Stoke `depot_load` stop
only for the genuinely ST-staged order (ML6, `0f36bf4b`), not for EH48.

- [ ] **Step 4: Record the result**

Update `freight_planner/QUEST_LOG.md` with a dated entry: what shipped (flow-aware
staging via `resolve_staging_depot`), the coverage/km deltas measured in Steps 2-3,
and that Stoke territory was deliberately left unchanged (telematics showed Stoke is
an ST-origin collection arm, not a delivery gateway).

Update the memory note `coverage-is-tour-capacity-bound.md` (or add a new
`flow-aware-staging-depot.md`) capturing: the root cause (OVERFLOW→nearest picked the
dockless ST4 satellite), the fix (delivery→CB22, collection→nearest gateway), and the
deferred CB22-for-everything fallback.

- [ ] **Step 5: Regenerate the trip viz (optional, if reviewing visually)**

Per standing preference, `trip_app` only — skip the folium maps:
```bash
cd /e/BEAT/ZECURE-Phase2-main/BackEnd/logistics && PYTHONPATH=. \
  /e/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe -m freight_planner.viz_app --plan-dir <run>/plan
```

- [ ] **Step 6: Report to stakeholder** — coverage delta, km delta, EH48 outcome,
      whether the Scotland tour now anchors CB22 with a Stoke load-stop. No commit.

---

## Notes for the executor

- **Do not widen Stoke territory** and **do not** change `resolve_cluster`/`_depot_of` —
  both are deliberately out of scope (see spec). The fix is exactly the three code
  edits above (scope.py, legs.py, tour_plan.py).
- If Task 5 coverage regresses or far-origin OVERFLOW work concentrates on one gateway
  and strands (mass `NO_FEASIBLE_TOUR`), the spec's deferred fallback is to collapse the
  collection-side rule to `CB22` as well — raise this with the stakeholder rather than
  self-deciding.
```
