# freight-planner v1.1 — OSRM travel-time model + depot-timing emission + duty-hours validation

**Status:** design approved (2026-07-09). Not committed (standing rule: no git commits).
**Owner:** freight_planner.
**Scope:** one spec, four parts, delivered together (A+B are the speed work; C+D the emission/validation).

---

## 1. Goal

Replace the planner's constant-speed drive-time model (`drive_minutes(km) = km / 50 × 60`
for daily routes) with **OSRM road-type travel times, calibrated per vehicle type**, behind a
default-off flag; and expose + validate the resulting vehicle-day clock so the time model can be
checked against telematics.

One sentence: *make travel time road-type-accurate and per-type-calibrated for the product, without
disturbing the dissertation experiment campaign.*

## 2. Motivation (evidence)

Observed in-motion speed from January telematics (moving pings, `GPSSpeed` mph→km/h, road class from
`Location_Road` prefix; 698k moving pings):

| vehicle type | minor/urban | B-road | A-road | motorway | overall |
|---|---|---|---|---|---|
| rigid | 40.8 | 57.1 | 68.6 | 81.5 | 54.1 |
| tractor | 46.6 | 56.2 | 72.2 | 81.7 | 66.1 |
| van | 51.3 | 73.1 | 78.1 | 103.1 | 67.4 |

Driving mix: urban 46%, A-road 31%, motorway 22%, B-road 0.8%.

Findings that drive the design:
1. **The constant 50 km/h is wrong in both directions by road type and only right on average** — too
   fast in urban (real 41–51), far too slow on A-road/motorway (69–103). Urban (~46%) offsets
   trunk (~53%), so the fleet-wide mean lands near 50 and *looks* calibrated while being structurally
   wrong per segment.
2. **Road type dominates vehicle type.** Cross-road-class spread (41→82, ~2×) dwarfs the per-type
   spread on a class (~5–10 km/h). OSRM already differentiates road type in its base durations, so the
   dominant fix comes from switching to OSRM durations.
3. **Van is the type outlier** (much faster, esp. motorway 103 vs ~82 for HGVs); tractor slightly
   faster than rigid. A per-type factor is worth it mainly to separate van from HGV.
4. **Units validated:** HGV motorway 81.5–81.7 km/h ≈ 50.7 mph = the UK HGV motorway limiter, confirming
   `GPSSpeed` is mph.

## 3. Decisions locked

- **Rollout:** Part B (the only solve-mutating piece) is **flag-gated, default OFF**. Reference config
  and pending experiments (E1, reverse-hole, E2) stay on the constant-speed model; done E3/E5 are
  snapshot-pinned. Product/live deploys flip the flag on. (Chosen over "new default + re-baseline".)
- **Calibration granularity:** **per-vehicle-type factor applied to OSRM per-segment durations**;
  per-road-class correction is *gated on validation* (added later only if OSRM's per-class shape is
  shown systematically off). (Chosen over a full per-(type×road-class) table now, and over a single
  global factor.)
- **Calibration basis:** structural per-type / per-road-class speed from GPS journeys and moving pings,
  **never a historical-daily-total fit** (that overfits forward/backtest mode and fails on unseen days).

## 4. Scope

**In:** daily-evaluator time model (flag-gated, per-type); a reproducible speed-calibration + per-type
validation script; depot-timing emission (always-on); duty-hours validation axis (always-on).

**Out:** multi-day tour speed model (stays `MULTIDAY_AVG_SPEED_KMH = 80`, motorway, seeded not
searched, straight-line distance); time-of-day traffic multiplier (the `duration_h(depart_time=…)`
hook exists but is a separate feature); making OSRM the default; touching the shared
`simulation.routing` OSRM cache format or `TRUCK_DURATION_FACTOR` (other consumers: `trunk_planner`,
`simulation`).

---

## 5. Part A — Speed calibration & per-type validation *(new, upstream)*

**Responsibility:** produce (1) the reproducible by-type speed validation, and (2) the per-type
duration factors Part B consumes — replacing the undocumented global `1.24` with a committed artifact.

**Files:**
- Create `freight_planner/speed_calibration.py`
- Create `tests/freight_planner/test_speed_calibration.py`
- Outputs (artifacts, written under a calibration output dir, e.g. `freight_planner/data/calibration/`):
  `speed_by_type_road.csv` (validation table) and `speed_factors.json` (per-type factors).

**Inputs (already on disk):**
- `data/Input/supatrak/supatrak_telematics_cleaned_20260101_to_20260131.csv` (+ Feb file). Columns:
  `LocalTime, AssetName, Ignition, Latitude, Longitude, GPSSpeed (mph), Location_Road,
  Location_Postcode, CANbusData_Odometer (miles), …`.
- `data/Input/supatrak/supatrak_vehicle_list_enriched.csv` (`AssetName, AssetType, metric{GCW|GVW},
  max_tonnes, fuel_type`).

**Method:**
- **Type map:** `AssetType` → {tractor (Tractor Unit / GCW), rigid (Lorry/Rigid Truck/Mini Truck /
  GVW), van (Service Van), EV (fuel_type Electric — reported, folded into HGV factors, tiny n)}.
- **Road class** (pure string prefix, no map-matching): `^M\d`→motorway, `^A\d`→A_road, `^B\d`→B_road,
  else minor_urban, missing→unknown.
- **Validation table** — moving pings (`Ignition` true, `GPSSpeed` > 2 mph), km/h = mph × 1.609344;
  mean/median/count by (type × road_class) and per-type overall. This is the "validate speed by type"
  deliverable; reproduces §2.
- **Per-type factor** — journey-based (same construction as the legacy global `1.24`, now per type):
  extract ignition-on trips per vehicle; per trip observed_duration = last−first `LocalTime`,
  OSRM_freeflow_duration = OSRM car free-flow O→D time (from the shared router/cache; live-query O→D
  when absent). `factor[type] = Σ observed_duration / Σ OSRM_freeflow_duration` over that type's trips.
  Guard against trips with no OSRM route or implausible speed. Emit `speed_factors.json` =
  `{"tractor": f_t, "rigid": f_r, "van": f_v}` (+ n, journey count, date range as provenance).
- **Per-class validation gate:** per (type, road_class) compare observed moving speed against OSRM's
  implied class speed; emit a residual table. A residual beyond ±15% flags that class for a future
  per-class correction. (Report only — does not change v1.1 behaviour.)

**Constraint encoded (documented in the module docstring):** factors derive from per-journey /
per-road-class structural speeds, not from matching Jan/Feb daily km or time totals.

**Note on the `1.24` reconciliation:** `simulation.routing.TRUCK_DURATION_FACTOR = 1.24` stays as is
(the shared cache stores car×1.24). Part B divides it back out and applies `factor[type]`, so the
shared cache and other consumers are untouched (see §6).

## 6. Part B — OSRM travel-time model *(solve-mutating; flag-gated, default off)*

**Responsibility:** time each daily leg by OSRM road-type duration, per vehicle type, instead of km/50.

**Files:**
- Modify `freight_planner/config.py` (leaf, stdlib-only): add
  `USE_OSRM_DURATIONS: bool = False` and `FREIGHT_DURATION_FACTOR: dict[str, float]` (per-type;
  default every type = `1.24` so flag-on-pre-calibration reproduces today's OSRM×1.24). A helper
  `duration_factor_for(vehicle_type: str) -> float` with a safe default for unknown types.
- Modify `freight_planner/route_costs.py`: add `road_minutes(a_lat, a_lon, b_lat, b_lon,
  vehicle_type)` mirroring `road_km`, with its own memo `_min_cache` cleared in `set_router`/
  `reset_router` (alongside `_km_cache`).
- Modify `freight_planner/routing_adapter.py`: replace the three `drive_minutes(leg_km)` call sites in
  `evaluate_route` (normal leg, two-point leg, return-to-depot leg) with per-segment `road_minutes`.
- Modify `freight_planner/compatibility.py`: make the reach screen flag-aware (see below).

**Interface (`road_minutes`):**
```
router = _active_router                      # route_costs' own installed router
if USE_OSRM_DURATIONS and router is not None and hasattr(router, "duration_h"):
    from simulation.routing import TRUCK_DURATION_FACTOR   # lazy
    hours = router.duration_h(a_lat, a_lon, b_lat, b_lon)  # cached car×1.24
    minutes = hours / TRUCK_DURATION_FACTOR * duration_factor_for(type) * 60
else:
    minutes = road_km(a_lat, a_lon, b_lat, b_lon) / AVG_SPEED_KMH * 60     # == today's drive_minutes
```
- Use `route_costs`'s **own** `_active_router` — `install_osrm_router` / `warm_and_install_osrm` set it
  to the `simulation.routing.OSRMRouter` instance, which *does* expose `duration_h`. So `road_km` and
  `road_minutes` share one router instance. The `hasattr(router, "duration_h")` guard means a
  distance-only or offline/`None` router (tests, no-OSRM runs) falls straight through to the
  constant-speed branch and never imports OSRM deps. `TRUCK_DURATION_FACTOR` is lazy-imported from
  `simulation.routing` only on the OSRM branch. Per-pair `duration_h` already degrades to haversine/50
  internally if a live query fails.

**Per-segment, not total-km:** two-point legs sum two `road_minutes` calls (mirror how `leg_km` sums
two `road_km`), so urban/motorway differentiation survives. Never convert summed km at one blended
speed.

**Break/wait/window logic unchanged:** `statutory_breaks`, `earliest_start` waits, `latest_finish`,
`shift_end`, `DRIVING_CAP` all consume the new `dm` exactly as before.

**Screen safety (prevents a coverage regression):** with OSRM on, motorway legs are *faster* than 50,
so a constant-50 reach screen ([compatibility.py:121](../../freight_planner/compatibility.py)) would
wrongly *reject* some reachable jobs. The screen is a cartesian jobs×vehicles frame, so per-pair OSRM
there is costly and the per-segment evaluator is the real time authority anyway. So under the flag the
screen simply uses a **generous screen speed** (`config.OSRM_SCREEN_SPEED_KMH`, default 100) for
`estimated_drive_minutes` — a permissive upper bound on real speed that guarantees the screen never
rejects a job the OSRM evaluator would accept (at the cost of a few extra evaluations). O(1) vectorised,
no per-pair OSRM.

**Experiment safety:** flag OFF ⇒ `road_minutes` is byte-identical to `drive_minutes(road_km)` ⇒
**bit-identical solve fingerprint** (same proof technique as the E3 instrumentation toggles). Reference
config and all pending experiments are unaffected unless the flag is set.

## 7. Part C — Depot-timing emission *(always-on, non-mutating)*

**Responsibility:** fill the currently-blank `depot_start.planned_depart` /
`depot_return.planned_arrive` cells with the clock the evaluator already computes
([QUEST_LOG](../../freight_planner/QUEST_LOG.md): "Duty-hours axis BROKEN").

**Files:**
- Modify `freight_planner/alns.py`: add `_route_times_from_solution` (sibling of
  `_route_totals_from_solution`, [alns.py:1191](../../freight_planner/alns.py)) returning
  `route_times[route_id] = (day_ev.day_start, day_ev.day_end)` and
  `route_times[f"{route_id}#T{trip_index}"] = (trip_ev.route_start, trip_ev.route_end)`. Add
  `route_times: dict = field(default_factory=dict)` to `RouteSeedImprovement`
  ([alns.py:360](../../freight_planner/alns.py)); populate at the return in `improve_existing_solution`.
- Modify `freight_planner/reports.py`: add `route_times: dict | None = None` to `write_reports`, pass
  into `build_route_stops`. Update the CLI caller(s) of `write_reports` to pass
  `route_times=improvement.route_times`.
- Modify `freight_planner/manifest.py::build_route_stops`: add `route_times: dict | None = None`; on the
  `depot_start` row set `planned_depart = route_times[trip_key][0]`; on `depot_return` set
  `planned_arrive = route_times[trip_key][1]`, reusing the existing `trip_key = f"{route_id}#T{trip_index}"`
  keying (same keys `route_totals` already uses at
  [manifest.py:385](../../freight_planner/manifest.py)). Blank when absent (tours/other paths).

**Data flow:** `evaluate_day`/`evaluate_route` (compute `day_start/day_end`, `route_start/route_end`) →
`_route_times_from_solution` → `RouteSeedImprovement.route_times` → `write_reports` →
`build_route_stops` → `route_stops.csv` depot rows.

**Safety:** additive; fills blank columns; changes no plan/km/coverage. Reflects whichever time model
is active (OSRM if Part B flag on, else constant-50). Multi-day `TOUR:` depot rows stay blank (out of
scope; daily routes are the duty-hours target).

## 8. Part D — Duty-hours validation axis *(always-on, downstream)*

**Responsibility:** add a fourth plan-vs-actual axis to `viz_app --validate`: planned duty span vs
telematics actual duty span, per vehicle-day.

**Files:**
- Modify `freight_planner/vehicle_actuals.py`: add `actual_duty_by_vehicle(day, *, loader=_load_day) ->
  dict[str, float]` — per vehicle, moving pings (`GPSSpeed` > ~2 mph), duty hours = (last − first)
  moving `LocalTime`. Mirrors `actual_km_by_vehicle` (same grouping/sort).
- Modify `freight_planner/viz_app.py`: in the `per_day_actuals` builder ([viz_app.py:357](../../freight_planner/viz_app.py))
  add per-(vehicle,day) actual duty; in `_build_validation` ([viz_app.py:200](../../freight_planner/viz_app.py))
  compute planned duty per (vehicle,day) = last `depot_return.planned_arrive` − first
  `depot_start.planned_depart` for that vehicle-day (from `route_stops`), and add a scorecard row
  "duty hours (plan vs actual)" plus per-vehicle popup values.

**Data flow:** Part C depot timings → planned duty; telematics moving span → actual duty; compared in
the window-scoped validation scorecard already built.

**Safety:** does not affect the solve. Validates the end-to-end time model (Part B) without the
driving-vs-crawl speed ambiguity; complements Part A (which calibrates the per-segment speed in).

---

## 9. File structure (all changes)

**Create:**
- `freight_planner/speed_calibration.py` — calibration + validation (Part A)
- `tests/freight_planner/test_speed_calibration.py`
- Artifacts: `freight_planner/data/calibration/speed_by_type_road.csv`, `…/speed_factors.json`

**Modify:**
- `freight_planner/config.py` — `USE_OSRM_DURATIONS`, `FREIGHT_DURATION_FACTOR`, `duration_factor_for` (Part B)
- `freight_planner/route_costs.py` — `road_minutes` + `_min_cache` (Part B)
- `freight_planner/routing_adapter.py` — three call-site swaps in `evaluate_route` (Part B)
- `freight_planner/compatibility.py` — flag-aware reach screen (Part B)
- `freight_planner/alns.py` — `_route_times_from_solution`, `RouteSeedImprovement.route_times` (Part C)
- `freight_planner/reports.py` — thread `route_times` (Part C)
- `freight_planner/manifest.py` — `build_route_stops` depot-row timing (Part C)
- `freight_planner/vehicle_actuals.py` — `actual_duty_by_vehicle` (Part D)
- `freight_planner/viz_app.py` — duty axis in `_build_validation` + `per_day_actuals` (Part D)
- Tests alongside each (`tests/freight_planner/test_route_costs*.py`, `…/test_routing_adapter*.py`,
  `…/test_compatibility*.py`, `…/test_manifest*.py` / `test_reports*.py`, `…/test_vehicle_actuals.py`,
  `…/test_viz_app_validation.py`).

## 10. Testing strategy (TDD)

- **Part A:** road-class classifier; type map; factor computation and validation table on tiny synthetic
  frames with a stub OSRM duration. (Full-data run is a script/report, not a unit test.)
- **Part B:**
  - flag-OFF leg-level identity: `road_minutes(...) == drive_minutes(road_km(...))` for sampled legs.
  - flag-OFF **solve fingerprint** identical to pre-v1.1 (the experiment-safety gate).
  - flag-ON: `road_minutes` uses `duration_h` with the per-type factor (mock simulation router);
    per-segment composition for two-point legs; fallback to constant-speed when the router raises / lacks
    `duration_h`.
  - screen flag-awareness (mask matches the evaluator under the flag).
- **Part C:** `build_route_stops` fills `depot_start.planned_depart` / `depot_return.planned_arrive`
  per trip from `route_times`; blank when absent; multi-trip keys resolve; `_route_times_from_solution`
  keys match `_route_totals_from_solution`.
- **Part D:** `actual_duty_by_vehicle` on a synthetic ping frame; `_build_validation` duty row from a
  synthetic `route_stops` + telematics; window-scoped correctly.

## 11. Success criteria / acceptance

1. Flag OFF: full test suite green **and** a solve fingerprint bit-identical to the pre-v1.1 baseline on
   an illustrative window (experiments provably safe).
2. Flag ON: a Jan and a Feb window run clean (no OSRM crashes; fallback_count reported), coverage not
   *lower* than flag-off on the same window (screen-safety check).
3. `speed_calibration.py` reproducibly emits `speed_by_type_road.csv` (matching §2 within tolerance) and
   `speed_factors.json`; per-class residual report emitted.
4. `route_stops.csv` depot rows carry `planned_depart`/`planned_arrive`; `viz_app --validate` shows the
   duty-hours axis (planned vs actual).

## 12. Experiment-safety & provenance

- Only Part B mutates the solve, and only when `USE_OSRM_DURATIONS` is set. The reference config leaves
  it unset. Pending experiments (E1, reverse-hole, E2) and the done, snapshot-pinned E3/E5 are unaffected.
- All changes tracked/restorable (standing rule). **No git commits** — this spec and all code changes
  stay uncommitted; the spec's file existence is the record, not a commit.
- `speed_factors.json` + the calibration script are the reproducible replacement for the undocumented
  `1.24` code comment.

## 13. Risks & open questions

- **OSRM server availability** for Part A journey calibration and Part B runs: the calibration script
  must handle live-query failures (fall back / skip journey, count). If the OSRM server is down when the
  calibration runs, factors default to `1.24` (neutral) and the script reports incomplete coverage.
- **`Location_Road` quality:** reverse-geocoded, some pings missing/mislabelled; robust only in
  aggregate (used for the validation table + gate, not for per-pair plan-time decisions), which is why
  per-class correction is deferred.
- **Trip O/D matching to OSRM cache:** ignition-on trip endpoints are arbitrary GPS, often not cached →
  live `/route` or `/table` queries in the calibration script (bounded, ~1–2k journeys).
- **Tour/daily boundary for duty hours:** only daily `ROUTE:` rows get timings; `TOUR:` depot spans stay
  blank in v1.1.

## 14. Out of scope / future

- Per-(type × road-class) OSRM correction (gated on §5 residuals).
- Time-of-day congestion multiplier (existing `duration_h(depart_time)` + `tod_multiplier` hook).
- OSRM durations for multi-day tours.
- Making OSRM durations the default and re-baselining the campaign.
