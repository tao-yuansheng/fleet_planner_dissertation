# Night Trunk Service (T1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> Steps use checkbox (`- [ ]`) syntax.
>
> **STANDING RULES:** NO git commands ever. Tests run from
> `e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics` with `python -m pytest`.

**Goal:** Model the nightly depot↔B37 trunk as a FIXED scheduled service — sized per
depot-night at double-deck (52-pal) capacity, staffed by a nightly draw from the
artic pool with next-day availability cost, reported as a separate KPI line.

**Spec (READ FIRST):** `docs/superpowers/specs/2026-07-04-night-trunk-service-design.md`
— it carries the verified double-deck derivation and the no-Stoke evidence that the
code comments must reference.

**Key code facts (verified 2026-07-04):**
- Legs frame rows carry `flow` (legs.py:113 dataclass field). Candidate rows may NOT
  carry `flow` — check `jobs.py::candidate_jobs_frame`; if absent, add a one-field
  `flow` passthrough there (Task 1 step 1.3a) so `trunk_schedule` can run on the
  candidate frame the orchestrator already has.
- `tour_plan.py`: `MultidaySeedResult` dataclass at :84 (extend with trunk fields);
  tour reservation completes before the daily seed call at :361
  (`run_route_seed_plan(daily_candidates, vehicles, compatibility, freight_states,
  ledger=ledger, excluded_vehicle_days=reserved)`); result constructed at :616.
- `route_seed.py`: `_route_vehicle(vrow, day)` at :121 composes `start_time` from
  the row's `available_from` (default 06:00); the `_rv(vid, day)` cache inside
  `run_route_seed_plan` at :210-215 is the single seed-side construction site.
- `alns.py`: `_route_vehicle(vm, day)` at :313 (same composition); call sites at
  :429, :515, :567, :676, :1036, :1059, :1154 — most behind per-(vid,day) caches
  inside closures; `improve_route_seed` (:1176) → `improve_existing_solution`
  (:1074) → `improve_solution` threading pattern already exists for
  `pinned_job_ids` — mirror it for `avail_overrides`.
- `cambridge/config.py`: `TRUNK_DEPART_HOUR = 21`, `TRUNK_PREP_MARGIN_H = 1.0`
  (shared with scope windows — do NOT move them).
- B37 coordinates: geocode `"B37 7HB"` (legs.py `HUB_POSTCODE`); depot anchors in
  `cambridge.config.DEPOT_ANCHORS`; road km via `route_costs.road_km` (OSRM-backed
  with haversine fallback).
- `run_alns.py`: KPI assembly around :315-330 (`cross_depot_km=` pattern),
  `build_validation_metrics` at :62; run-log via `runlog.log`. `kpi.py` renders the
  summary (see the `cross_depot repositioning km` line at kpi.py:167 for the style).
- Vehicle frame: `vehicle_type == "tractor"`, `home_depot`, `vehicle_id`.
- Suite baseline: tests/freight_planner 383 green + tests/cambridge/test_scope.py 76.

---

### Task 1: Config + pure trunk module (TDD)

**Files:** modify `freight_planner/config.py` (append knobs); create
`freight_planner/trunk.py`; create `tests/freight_planner/test_trunk.py`;
possibly modify `freight_planner/jobs.py` (flow passthrough, step 1.3a).

- [ ] **1.1 Config knobs** (leaf module, zero imports — keep it that way):

```python
# --- Nightly B37 hub trunk as a fixed scheduled service (T1, spec 2026-07-04) ---
# TRUNK_DECK_PALLETS = 52 is a VERIFIED operational fact, not a tuning knob:
# in-universe hub flow needs 12.3 mean / 15 peak trips/night at single-deck 26 pal
# but telematics shows only 7.0 mean / 11 peak fleet tractors at B37 per weeknight
# — double-deck (6.5 / 8 required) is the only fleet assumption that reconciles
# demand with observation. See the T1 spec for the full derivation.
# TRUNK_DEPOTS: Bedford (9 regs / 49 night reg-nights) and CB22 (12 / 44) verified
# from January telematics; STOKE deliberately absent — zero night visits, its two
# B37 visitors run 10:00-17:00 daytime hub drops inside normal routes.
TRUNK_ENABLED: bool = True
TRUNK_DECK_PALLETS: float = 52.0
TRUNK_DEPOTS: tuple = ("BEDFORD", "CB22")
TRUNK_NEXT_DAY_START: str = "10:00"
```

- [ ] **1.2 Failing tests** for the pure module (plain frames, no routing):

```python
# sizing
def test_trips_ceil_of_max_direction():
    # depot-night with 60 import pal (next-day delivery) and 30 export pal
    # -> ceil(60/52) = 2 trips (max, not sum)
def test_exact_multiple_boundary():
    # 52.0 pal -> 1 trip; 52.1 -> 2; 104.0 -> 2
def test_first_day_imports_prestaged_no_trip():
    # imports delivered ON window start day charge no night (pre-window trunk)
def test_last_day_exports_charge_their_night():
def test_stoke_never_scheduled():
    # PL_EXPORT into STOKE never creates a STOKE trunk night (flows to nothing);
    # assert schedule depots ⊆ TRUNK_DEPOTS
def test_import_export_share_the_same_trips():
    # 40 import + 45 export on one depot-night -> 1 trip (both fit one 52-pal
    # round trip, directions don't add)
# draw
def test_draw_rotates_least_recently_drawn():
    # 3 tractors, 2 trips/night over 3 nights -> deterministic rotation, no
    # tractor drawn twice in a night
def test_draw_skips_tour_reserved():
    # a tractor reserved (vid, night_day) or (vid, next_day) is never drawn
def test_draw_shortfall_reported():
    # trips > available tractors -> shortfall recorded, remaining trips still
    # in the schedule (km still counted)
def test_next_day_override_time():
    # drawn (vid, night N) -> overrides[(vid, N+1 iso)] == TRUNK_NEXT_DAY_START
```

- [ ] **1.3 Implement `freight_planner/trunk.py`** (pure; no routing imports —
  km injected as a `roundtrip_km: dict[depot, float]` argument computed by the
  caller):

```python
@dataclass(frozen=True)
class TrunkNight:
    depot: str
    night: str            # departure date ISO (freight collected day N goes up night N)
    import_pallets: float # pallets delivering FROM depot on day N+1
    export_pallets: float # pallets collected INTO depot on day N
    trips: int            # ceil(max(import, export) / TRUNK_DECK_PALLETS)
    km: float             # trips * roundtrip_km[depot]

@dataclass
class TrunkPlan:
    nights: list[TrunkNight]
    draws: dict            # (vehicle_id, night_iso) -> True (drawn that night)
    avail_overrides: dict  # (vehicle_id, next_day_iso) -> TRUNK_NEXT_DAY_START
    shortfalls: list       # (depot, night, missing_trips)
    total_km: float
    total_trips: int

def trunk_schedule(candidates, window_start, window_end, roundtrip_km) -> list[TrunkNight]
def draw_tractors(nights, vehicle_df, reserved) -> TrunkPlan
```

Sizing source rows: `flow == "PL_IMPORT"` CUSTOMER_DELIVERY legs (depot =
`source_depot`, night = service_date − 1 day) and `flow == "PL_EXPORT"`
CUSTOMER_PICKUP legs (depot = `target_depot`, night = service_date). Only
depots in `TRUNK_DEPOTS`; nights Mon-Fri (skip Sat/Sun departures — verified
weeknight operation); nights strictly before `window_start` are dropped
(prestaged), nights on/after `window_start` count even when the DELIVERY day
is outside the window edge. Epsilon on the ceil boundary (`- 1e-6` inside the
division) — same float discipline as shuttle.py.

- [ ] **1.3a Flow passthrough:** if candidate rows lack `flow`, add it in
  `jobs.py::candidate_jobs_frame` (copy from the leg row; one dataclass field +
  one assignment; check `tests/freight_planner/test_jobs*.py` for the record
  fixture to extend). If candidates already carry flow, skip.

- [ ] **1.4** All test_trunk tests green; full `tests/freight_planner` green.

---

### Task 2: Orchestrator + seed integration (TDD)

**Files:** modify `freight_planner/tour_plan.py`, `freight_planner/route_seed.py`;
test `tests/freight_planner/test_tour_plan.py` + `test_route_seed.py` (append).

- [ ] **2.1 Failing tests:**

```python
def test_trunk_plan_produced_and_reserved_skipped(...):
    # multiday fixture with PL_IMPORT/PL_EXPORT volume at BEDFORD; result has
    # .trunk (TrunkPlan) with expected trips; a tour-reserved tractor never drawn
def test_avail_override_delays_next_morning(...):
    # run_route_seed_plan with avail_overrides {(vid, day): "10:00"} -> a job
    # only servable 06:00-09:00 on that vid/day is NOT seeded there (lands on
    # another vehicle or rejects); without the override it seeds there
def test_trunk_disabled_no_plan(monkeypatch, ...):
    # TRUNK_ENABLED False -> result.trunk is None, no overrides applied
```

- [ ] **2.2 route_seed:** `run_route_seed_plan(..., avail_overrides=None)`;
  inside `_rv(vid, day)` apply the override before constructing:
  the override REPLACES the row's `available_from` time for that (vid, day)
  (build the RouteVehicle then `replace(veh, start_time=...)`, or pass an
  overridden vrow copy — implementer's choice; keep `_route_vehicle` itself
  untouched for other callers). Cache key stays (vid, day).

- [ ] **2.3 tour_plan:** after tour reservation completes (post :358 loop) and
  BEFORE the daily seed call (:361), when `TRUNK_ENABLED`:
  compute `roundtrip_km` (2 × road_km(depot anchor, B37 latlon) per TRUNK_DEPOT
  — geocode "B37 7HB" via the postcode cache available in the pipeline; if
  tour_plan lacks the cache, accept `roundtrip_km` as a new optional parameter
  computed in run_alns and passed down — decide by reading what tour_plan
  imports; report the choice), then
  `nights = trunk_schedule(daily_candidates, start, end, roundtrip_km)`,
  `trunk = draw_tractors(nights, vehicles, reserved)`, pass
  `avail_overrides=trunk.avail_overrides` to `run_route_seed_plan`, and extend
  `MultidaySeedResult` with `trunk: object = None`. Drawn night-days are NOT
  added to `reserved` (the tractor still works day N; only its N+1 morning is
  delayed via the override).

- [ ] **2.4** Suites green (tour_plan, route_seed, full freight_planner).

---

### Task 3: ALNS threading + run_alns reporting (TDD)

**Files:** modify `freight_planner/alns.py`, `freight_planner/run_alns.py`,
`freight_planner/kpi.py`; tests `tests/freight_planner/test_alns.py` (append).

- [ ] **3.1 Failing test:** `test_alns_respects_avail_override` — a two-vehicle
  solution where moving a morning job onto vehicle A would save km, but A has an
  override "10:00" that makes the move infeasible; run improve_existing_solution
  with `avail_overrides={...}` -> the job stays put; without the override it moves
  (counterfactual, mirror the pinned-test style).

- [ ] **3.2 alns.py:** `avail_overrides=None` threaded improve_route_seed →
  improve_existing_solution → improve_solution (the `pinned_job_ids` pattern).
  Apply at the vehicle construction sites: add a small helper
  `_rv_override(vehicle_meta_entry, day, overrides)` next to `_route_vehicle`
  and use it at the cached sites (:515, :567, :676, :1036) and the direct sites
  (:429, :1059, :1154) — CHECK each site's closure has access to the overrides
  (thread as parameter where needed). Merge-sweep's injected `route_vehicle`
  must also respect overrides: in improve_existing_solution pass a lambda
  closing over the overrides instead of raw `_route_vehicle`.

- [ ] **3.3 run_alns.py:** pass `avail_overrides=getattr(seed, "trunk", None) and
  seed.trunk.avail_overrides or {}` into improve_route_seed; emit run-log block:

```
trunk: BEDFORD 28 trips / 5 nights 7,022 km | CB22 11 trips / 5 nights 3,857 km
       total 10,879 km, 39 trips | shortfall nights: 0
       combined: plan 87,134 + trunk 10,879 = 98,013 km
```

  KPI (`kpi.py`): add `trunk_km`, `trunk_trips`, `trunk_shortfall_nights` to the
  report dataclass + a "Fixed trunk service" section in the summary md
  (double-deck 52-pal note in the header line). `build_validation_metrics`
  gains the same three fields. When `seed.trunk is None` everything renders as
  absent/zero (backward compatible).

- [ ] **3.4** Suites green; FP_ALNS_CONSERVE=1 smoke on test_alns.

---

### Task 4: Validation runs (controller executes inline — NOT a subagent)

- [ ] wk1 + wk2, one run each, no tuning. Report: coverage (hold 99.7 / 99.9),
  plan km vs 87,134 / 91,770 (expected flat-to-slightly-up from morning capacity
  loss), trunk line (expect ≈6.5-8 trips/night, ≈9-11k km/wk, shortfall 0),
  combined km vs reality odometer (89,571 / 92,789 incl. real trunk),
  trunk-drawn tractor count vs the observed 7.0/night.

## Self-review (done at plan-writing time)

Spec coverage: sizing rules ✓(T1) double-deck+no-Stoke documentation ✓(1.1
comments reference the spec) draw+rotation+shortfall ✓(T1) next-day override
✓(T1/T2) orchestrator ordering ✓(T2.3) both vehicle-construction sites ✓(T2.2,
T3.2 incl. merge-sweep lambda) separate KPI line ✓(T3.3) export cutoff = no code
(documented dependency) ✓ import ready-time no-op = no code ✓ acceptance ✓(T4).
Known judgment points left to implementers (explicitly): roundtrip_km plumbing
location (T2.3), candidate flow passthrough need (T1.3a), override application
mechanics in each ALNS closure (T3.2).
