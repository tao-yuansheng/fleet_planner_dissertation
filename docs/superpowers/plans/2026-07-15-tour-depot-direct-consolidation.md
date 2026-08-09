# Tour consolidation: depot-loaded directs as deliveries — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:executing-plans (inline) to implement
> this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. **Standing rule: NO git commits**
> (`e:\BEAT` is not a git repo) — every "Checkpoint" step means *run the stated tests and confirm
> green*, not commit. Run all `python`/`pytest` from `e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics`
> (the working dir resets — prepend `cd /e/BEAT/ZECURE-Phase2-main/BackEnd/logistics &&`).

**Goal:** Make depot-loaded far orders that share a destination consolidate onto one tour, by
reclassifying a DIRECT whose collection origin is at its anchor depot into a `CUSTOMER_DELIVERY`
for tour planning.

**Architecture:** A single input transform in the tour-candidate loop of
`tour_plan.run_multiday_seed_plan`. Downstream (`build_tours`, `evaluate_tour`, `resolve_cluster`,
emission) is unchanged — it already consolidates deliveries and already emits `stop_type` from
`rjob.leg_kind`. Gated by a config flag (default on) with a CLI ablation. The tour-evaluation core
is not touched.

**Tech Stack:** Python (frozen dataclass `RouteJob`, `dataclasses.replace`), pytest, argparse
`BooleanOptionalAction`. Design: `docs/superpowers/specs/2026-07-15-tour-depot-direct-consolidation-design.md`.

---

## File Structure

- Modify `freight_planner/config.py` — add the `TOUR_DEPOT_DIRECT_AS_DELIVERY` flag.
- Modify `freight_planner/tour_plan.py` — import `_origin_at_depot` + `config` module; add the
  `_as_depot_delivery` helper; add the `depot_direct_as_delivery` param to `run_multiday_seed_plan`
  and apply the helper in the tour-candidate loop.
- Modify `freight_planner/run_rolling.py` and `freight_planner/run_alns.py` — add the
  `--tour-depot-direct-as-delivery` CLI (BooleanOptionalAction) and set the config global in the
  existing `_apply_vehicle_day_cost_flags` helper (rename its call site is unnecessary — just extend it).
- Test: `tests/freight_planner/test_tour_plan.py` (helper + regression), plus the run-CLI tests
  alongside the existing vehicle-day-cost CLI tests.
- Docs: `freight_planner/PIPELINE.md`, `freight_planner/README_DYNAMIC.md`, and the
  `tour-consolidation-gap` memory.

---

### Task 1: The reclassification helper

**Files:**
- Modify: `freight_planner/tour_plan.py` (imports near line 49-62; helper near the other module
  helpers, e.g. after `_anchor_or_nearest` ~line 135)
- Test: `tests/freight_planner/test_tour_plan.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/freight_planner/test_tour_plan.py`:

```python
from freight_planner.shared.config import DEPOT_ANCHORS
from freight_planner.routing_adapter import RouteJob
from freight_planner.tour_plan import _as_depot_delivery


def _direct(origin_lat, origin_lon):
    # a DIRECT move delivering to Hull (HU6 7QD), collected at `origin`
    return RouteJob(job_id="J", leg_kind="DIRECT_CUSTOMER_MOVE", node="HU6 7QD",
                    lat=53.769, lon=-0.3334, pallets=6.0, kg=1838.0,
                    origin_lat=origin_lat, origin_lon=origin_lon, order_id="J")


def test_depot_origin_direct_becomes_delivery():
    anchor = DEPOT_ANCHORS["CB22"]
    out = _as_depot_delivery(_direct(52.0966, 0.1591), anchor, enabled=True)  # CB22 4PS ~1.5 km
    assert out.leg_kind == "CUSTOMER_DELIVERY"
    assert out.origin_lat is None and out.origin_lon is None
    assert out.lat == 53.769 and out.pallets == 6.0        # everything else preserved


def test_far_origin_direct_is_left_a_direct():
    anchor = DEPOT_ANCHORS["CB22"]
    out = _as_depot_delivery(_direct(53.0, -2.5), anchor, enabled=True)  # ~250 km origin
    assert out.leg_kind == "DIRECT_CUSTOMER_MOVE" and out.origin_lat == 53.0


def test_disabled_flag_leaves_the_direct_unchanged():
    anchor = DEPOT_ANCHORS["CB22"]
    out = _as_depot_delivery(_direct(52.0966, 0.1591), anchor, enabled=False)
    assert out.leg_kind == "DIRECT_CUSTOMER_MOVE"


def test_non_direct_is_returned_unchanged():
    anchor = DEPOT_ANCHORS["CB22"]
    d = RouteJob(job_id="D", leg_kind="CUSTOMER_DELIVERY", node="HU6 7QD", lat=53.769, lon=-0.3334,
                 pallets=6.0, kg=1838.0, order_id="D")
    assert _as_depot_delivery(d, anchor, enabled=True) is d
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `python -m pytest tests/freight_planner/test_tour_plan.py -k depot_origin_direct -q`
Expected: FAIL — `ImportError: cannot import name '_as_depot_delivery'`.

- [ ] **Step 3: Add the import**

In `freight_planner/tour_plan.py`, add `_origin_at_depot` to the existing `from freight_planner.tours import (...)` block (keep alphabetical-ish ordering):

```python
from freight_planner.tours import (
    _DAY_DRIVE_CAP_MIN,
    _origin_at_depot,
    DEPOT_LOAD,
    ...
)
```

- [ ] **Step 4: Implement the helper**

Add near the other small helpers in `freight_planner/tour_plan.py` (e.g. just after
`_anchor_or_nearest`):

```python
def _as_depot_delivery(rjob: RouteJob, anchor_xy, enabled: bool) -> RouteJob:
    """A DIRECT move whose collection origin is AT its anchor depot is functionally a
    depot-loaded delivery: the collect happens where the tour already starts, so the atomic
    collect->deliver pairing serves no purpose and blocks same-destination consolidation
    (two directs evaluate as two round trips -> infeasible). Reclassify it to a
    CUSTOMER_DELIVERY so it batches like a delivery. Non-depot-origin directs (a real
    backtrack collection) and non-directs are returned unchanged."""
    if not enabled or rjob.leg_kind != DIRECT_CUSTOMER_MOVE or anchor_xy is None:
        return rjob
    if not _origin_at_depot(rjob, {"_anchor": anchor_xy}):   # check the ANCHOR depot specifically
        return rjob
    return _dc_replace(rjob, leg_kind=CUSTOMER_DELIVERY, origin_lat=None, origin_lon=None)
```

- [ ] **Step 5: Run the tests, verify they pass**

Run: `python -m pytest tests/freight_planner/test_tour_plan.py -k "depot_origin_direct or far_origin_direct or disabled_flag or non_direct" -q`
Expected: PASS (4 passed).

- [ ] **Step 6: Checkpoint** — `python -m pytest tests/freight_planner/test_tour_plan.py -q` → all green.

---

### Task 2: Config flag + wire into the tour-candidate loop

**Files:**
- Modify: `freight_planner/config.py`
- Modify: `freight_planner/tour_plan.py` (module import; `run_multiday_seed_plan` signature ~line 200;
  the tour-candidate loop ~line 268-282)
- Test: `tests/freight_planner/test_tour_plan.py`

- [ ] **Step 1: Write the failing regression test**

Add to `tests/freight_planner/test_tour_plan.py`:

```python
from freight_planner.tours import build_tours
from freight_planner.tour_plan import _centroid_proto, _as_depot_delivery
from freight_planner.shared.config import DEPOT_ANCHORS
from freight_planner.routing_adapter import RouteJob


def _hull_direct(jid, pallets, kg):
    return RouteJob(job_id=jid, leg_kind="DIRECT_CUSTOMER_MOVE", node="HU6 7QD",
                    lat=53.769, lon=-0.3334, pallets=pallets, kg=kg,
                    origin_lat=52.0966, origin_lon=0.1591, order_id=jid)


def test_three_depot_directs_consolidate_to_one_tour_when_enabled():
    anchor = DEPOT_ANCHORS["CB22"]
    raw = [_hull_direct("A", 6, 1838.1), _hull_direct("B", 1, 322.7), _hull_direct("C", 2, 572.28)]
    jobs = [_as_depot_delivery(j, anchor, enabled=True) for j in raw]
    due = {j.job_id: "2026-01-15" for j in jobs}
    tours = build_tours(jobs, _centroid_proto("2026-01-15"), due_by_job=due)
    assert len(tours) == 1
    assert {j.job_id for j in tours[0].jobs} == {"A", "B", "C"}


def test_three_depot_directs_stay_split_when_disabled():
    anchor = DEPOT_ANCHORS["CB22"]
    raw = [_hull_direct("A", 6, 1838.1), _hull_direct("B", 1, 322.7), _hull_direct("C", 2, 572.28)]
    jobs = [_as_depot_delivery(j, anchor, enabled=False) for j in raw]
    due = {j.job_id: "2026-01-15" for j in jobs}
    tours = build_tours(jobs, _centroid_proto("2026-01-15"), due_by_job=due)
    assert len(tours) == 3          # today's behaviour: one tour per direct
```

- [ ] **Step 2: Run, verify it fails**

Run: `python -m pytest tests/freight_planner/test_tour_plan.py -k "consolidate_to_one_tour" -q`
Expected: FAIL — the enabled case returns 3 tours until the helper is wired (it IS wired here via
the test itself, so this should actually PASS from Task 1's helper). If it PASSES, that confirms the
helper works; proceed. The wiring into `run_multiday_seed_plan` is validated at Task 4 (integration).

> Note: these two tests exercise the helper + `build_tours` directly (fast, deterministic). They are
> the core regression guard. The end-to-end wiring through `run_multiday_seed_plan` is covered by the
> Task 4 integration run, because constructing the full seed DataFrame inputs in a unit test is
> disproportionate.

- [ ] **Step 3: Add the config flag**

In `freight_planner/config.py`, near the other tour flags (`TOUR_COHESION_KM` etc.):

```python
TOUR_DEPOT_DIRECT_AS_DELIVERY: bool = True   # a DIRECT collected AT its anchor depot is planned as a
                                             # depot-loaded delivery, so same-destination far orders
                                             # consolidate onto one tour (2026-07-15). --no-... ablates.
```

- [ ] **Step 4: Wire the flag into `run_multiday_seed_plan`**

In `freight_planner/tour_plan.py`:

Add the module import near the top (after line 26's `from freight_planner.config import (...)`):
```python
from freight_planner import config as _fp_config
```

Add the parameter to `run_multiday_seed_plan` (after `trunk_from: str | None = None,`):
```python
    trunk_from: str | None = None,
    depot_direct_as_delivery: bool | None = None,
) -> MultidaySeedResult:
    if depot_direct_as_delivery is None:
        depot_direct_as_delivery = _fp_config.TOUR_DEPOT_DIRECT_AS_DELIVERY
```
(Place the two `if` lines at the very top of the body, before the empty-candidates early return.)

In the tour-candidate loop, apply the helper right after `depot` is computed and before the job is
bucketed (~line 280-281):
```python
        depot = _anchor_or_nearest(src, c[0], c[1])
        rjob = _as_depot_delivery(rjob, DEPOT_ANCHORS.get(depot), depot_direct_as_delivery)
        buckets.setdefault(depot, []).append(rjob)
```

- [ ] **Step 5: Run the regression tests, verify pass**

Run: `python -m pytest tests/freight_planner/test_tour_plan.py -q`
Expected: PASS (all, including the two consolidation tests).

- [ ] **Step 6: Checkpoint** — `python -m pytest tests/freight_planner/ -q` → all green (no regressions
  in the seed/tour tests from the new param).

---

### Task 3: CLI ablation on `run_rolling` and `run_alns`

**Files:**
- Modify: `freight_planner/run_rolling.py` (argparse ~line 1465; `_apply_vehicle_day_cost_flags` ~1478)
- Modify: `freight_planner/run_alns.py` (argparse ~line 757 helper; the parser)
- Test: the file holding the existing vehicle-day-cost CLI tests (search: `test_run_rolling_cli`)

- [ ] **Step 1: Write the failing CLI test**

Find the test that asserts the vehicle-day-cost CLI sets config (e.g.
`test_run_rolling_cli_no_flag_forces_fuel_only_ablation`) and add alongside it:

```python
def test_run_rolling_cli_no_tour_direct_flag_sets_config_false(monkeypatch):
    from freight_planner import config as cfg
    from freight_planner import run_rolling
    monkeypatch.setattr(cfg, "TOUR_DEPOT_DIRECT_AS_DELIVERY", True)
    args = run_rolling._build_parser().parse_args(
        ["--start", "2026-01-12", "--end", "2026-01-13", "--no-tour-depot-direct-as-delivery"])
    run_rolling._apply_vehicle_day_cost_flags(args)
    assert cfg.TOUR_DEPOT_DIRECT_AS_DELIVERY is False


def test_run_rolling_cli_absent_tour_direct_flag_keeps_config_default(monkeypatch):
    from freight_planner import config as cfg
    from freight_planner import run_rolling
    monkeypatch.setattr(cfg, "TOUR_DEPOT_DIRECT_AS_DELIVERY", True)
    args = run_rolling._build_parser().parse_args(["--start", "2026-01-12", "--end", "2026-01-13"])
    run_rolling._apply_vehicle_day_cost_flags(args)
    assert cfg.TOUR_DEPOT_DIRECT_AS_DELIVERY is True
```

> If the parser is built inline in `main` rather than a `_build_parser()` helper, use the same access
> pattern the existing vehicle-day-cost CLI test uses (match it exactly).

- [ ] **Step 2: Run, verify fail**

Run: `python -m pytest tests/freight_planner/ -k "tour_direct_flag" -q`
Expected: FAIL — unrecognized argument `--no-tour-depot-direct-as-delivery`.

- [ ] **Step 3: Add the CLI argument (both runners)**

In `freight_planner/run_rolling.py` and `freight_planner/run_alns.py`, next to the
`--vehicle-day-cost` argument:

```python
    parser.add_argument("--tour-depot-direct-as-delivery",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="plan a DIRECT collected at its anchor depot as a depot-loaded "
                             "delivery so same-destination far orders consolidate (default: config; "
                             "--no-... reproduces the pre-fix behaviour)")
```

- [ ] **Step 4: Apply the flag to config**

Extend `_apply_vehicle_day_cost_flags(args)` in BOTH `run_rolling.py` and `run_alns.py` (it already
imports config as `_fp_cfg`):

```python
    if getattr(args, "tour_depot_direct_as_delivery", None) is not None:
        _fp_cfg.TOUR_DEPOT_DIRECT_AS_DELIVERY = bool(args.tour_depot_direct_as_delivery)
```

- [ ] **Step 5: Run the CLI tests, verify pass**

Run: `python -m pytest tests/freight_planner/ -k "tour_direct_flag" -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Checkpoint** — `python -m pytest tests/freight_planner/ -q` + `node --test
  tests/freight_planner/maplogic.test.cjs` → all green.

---

### Task 4: Integration validation (the Hull case, both flag states)

**Files:** none (verification only). Uses a fast STATIC seed run (`run_alns`), which calls the same
`run_multiday_seed_plan` the dynamic pipeline uses, so it exercises the fix end-to-end (batching →
ledger → emission) without a full month.

- [ ] **Step 1: Run the 12-18 Jan window with the fix ON (default), into a scratch out-dir**

Run:
```
python -m freight_planner.run_alns --start 2026-01-12 --end 2026-01-18 \
  --qargo freight_planner/data/enriched_orders_2026-01.parquet \
  --out-dir freight_planner/_val_tour_on
```

- [ ] **Step 2: Assert the three Hull orders now share ONE tour + coverage not worse**

Run (adjust the plan path to the produced window dir):
```
python - <<'PY'
import pandas as pd, glob
rs = pd.read_csv(glob.glob("freight_planner/_val_tour_on/**/route_stops.csv", recursive=True)[0])
hull = rs[rs["service_pc"].astype(str).str.startswith("HU6")]
print("HU6 vehicles:", sorted(hull["vehicle_id"].unique()), "| routes:", sorted(hull["route_id"].unique()))
assert hull["route_id"].nunique() == 1, "the 3 Hull orders should be ONE tour now"
PY
```
Expected: exactly one HU6 route_id (one vehicle). Compare the run's coverage KPIs
(`md/` summary or `route_seed_summary.md`) to the current baseline — ON_TIME / NOT_PLANNED / UNSERVED
must not worsen.

- [ ] **Step 3: Run the SAME window with `--no-tour-depot-direct-as-delivery` and assert it splits**

Run:
```
python -m freight_planner.run_alns --start 2026-01-12 --end 2026-01-18 \
  --qargo freight_planner/data/enriched_orders_2026-01.parquet \
  --no-tour-depot-direct-as-delivery --out-dir freight_planner/_val_tour_off
```
Then check the HU6 route count is back to the pre-fix value (3):
```
python - <<'PY'
import pandas as pd, glob
rs = pd.read_csv(glob.glob("freight_planner/_val_tour_off/**/route_stops.csv", recursive=True)[0])
hull = rs[rs["service_pc"].astype(str).str.startswith("HU6")]
print("flag-off HU6 routes:", hull["route_id"].nunique())
assert hull["route_id"].nunique() >= 2, "flag OFF must reproduce the split (ablation works)"
PY
```
Expected: ≥2 routes with the flag off — proving the flag gates the behaviour (ablation faithful).

- [ ] **Step 4: Clean up the scratch dirs** — `rm -rf freight_planner/_val_tour_on freight_planner/_val_tour_off`.

- [ ] **Step 5: Checkpoint** — record the ON vehicle-day/km vs the OFF run in the task notes.

---

### Task 5: Documentation + memory

**Files:**
- Modify: `freight_planner/PIPELINE.md` (tours section), `freight_planner/README_DYNAMIC.md`
  (config-defaults / tours), the `tour-consolidation-gap` memory + `MEMORY.md` line.

- [ ] **Step 1: Document the behaviour + flag**

In `PIPELINE.md`'s tours section, add a sentence: a DIRECT collected at its anchor depot is planned as
a depot-loaded delivery (`TOUR_DEPOT_DIRECT_AS_DELIVERY`, default on) so same/near-destination far
orders consolidate onto one tour; `--no-tour-depot-direct-as-delivery` reproduces the pre-fix split.
Note it changes the flow label of those orders (direct → delivery) but not km/coverage.

In `README_DYNAMIC.md`, add the flag to the config-defaults / flag table with the same one-line
description and the provenance (root cause: atomic collect→deliver directs can't consolidate; the
depot-loaded ones functionally are deliveries).

- [ ] **Step 2: Update memory**

Edit `tour-consolidation-gap.md`: mark the fix SHIPPED (default on), with the validated result (3 Hull
orders → 1 tour; flag-off reproduces the split). Update the `MEMORY.md` index line.

- [ ] **Step 3: Checkpoint** — final full run: `python -m pytest tests/freight_planner/ -q` +
  `node --test tests/freight_planner/maplogic.test.cjs` → all green.

---

### Follow-up (after the January baseline finishes — not part of this plan)

Run a fresh full-January dynamic run (fix ON) into a new folder and compare vehicle-days / km /
coverage against the baseline in `freight_planner/run_jan_dynamic`. That is the headline
before/after for the consolidation fix.

---

## Self-Review

- **Spec coverage:** transform (Task 1-2), flag + ablation (Task 2-3), output-as-delivery (automatic
  via `rjob.leg_kind` emission — noted), coverage-safety (disabled-unchanged test + Task 4 coverage
  assertion), validation (Task 4), docs (Task 5). All spec sections mapped.
- **Placeholder scan:** none — every step has concrete code/commands. The one soft spot
  (`_build_parser` vs inline argparse in Task 3) is flagged with the exact fallback (match the
  existing vehicle-day-cost CLI test's access pattern).
- **Type/name consistency:** `_as_depot_delivery(rjob, anchor_xy, enabled)` used identically in Task 1
  (definition/tests) and Task 2 (call site); `TOUR_DEPOT_DIRECT_AS_DELIVERY` consistent across config,
  tour_plan, both runners, and tests; `depot_direct_as_delivery` param name consistent.
- **Risk to watch during execution:** the freight ledger. A reclassified delivery commits via
  `_commit_leg(CUSTOMER_DELIVERY)`; if any Hull order is rejected for want of depot-available freight,
  the Task 4 coverage assertion fails — that is the signal to seed the freight state at the depot for
  depot-loaded directs. (Expected fine: these are FULL_FLEET orders whose freight originates at the
  depot.)
