# Vehicle Catchment (B15) — Design

Date: 2026-07-02. Stakeholder-approved: **option B** (soft cost preference, no
hard gate), **per-vehicle radii with per-type fallback**, **P95 percentile**.

## Problem

The freight_planner pipeline dropped the old dispatcher's vehicle-catchment
rule; compatibility checks only capacity + geocode + time, so the optimizer
freely sends rigids on long hauls (75 rigid legs >120 km on the 12-17 week —
bug B15). Worse, the per-type fuel rates (rigid 0.216 £/km < tractor 0.319)
make the search actively PREFER rigids for long distance — backwards from
reality, where rigids do close multi-trip work and artics do the long/multiday
runs. But reality is ~95%, not 100%: the exceptions are structural — roughly
seven rigids habitually run 130–190 km lanes while half the rigid fleet never
leaves ~30–50 km — so a hard gate or a single per-type radius would outlaw or
constantly penalize real work.

Calibration evidence (Jan 2026 qargo, distance home-depot anchor →
responsible-leg postcode, haversine; probe 2026-07-02):

| type    | n     | P50 | P75 | P90 | P95 | P99 |
|---------|-------|-----|-----|-----|-----|-----|
| rigid   | 5,792 | 24  | 42  | 121 | 186 | 312 |
| tractor | 6,614 | 40  | 136 | 186 | 206 | 331 |
| van     | 296   | 15  | 34  | 50  | 90  | 194 |

Per-vehicle rigid P90s range from ~30 km (P888RNW, R888RNW) to ~187 km
(L88GNW, P88GNW, W88RNW) — the exceptions are ROLE, not noise.

> **Amendment 2026-07-02 (stakeholder):** planner-owned tuning knobs moved from
> `cambridge/config.py` to the new **`freight_planner/config.py`** (leaf module;
> shared infra — DEPOT_ANCHORS, fleet master, VEHICLE_DEPOT_MAP — stays in
> cambridge). Every "cambridge/config.py" knob reference below now means
> `freight_planner/config.py`.

## Design

### 1. Calibration — `freight_planner/catchment.py` (new module)

`build_vehicle_catchment(qargo_df: pd.DataFrame, postcode_cache: dict) ->
dict[str, float]` — vehicle reg → radius km:

- For every non-CANCELLED order and every reg in `resource_rigid` /
  `resource_tractor` / `resource_van` (comma-split, upper, spaces stripped):
  distance = haversine from the vehicle's home-depot anchor
  (`VEHICLE_DEPOT_MAP` + `DEPOT_ANCHORS`, cambridge.config) to the
  responsible-leg postcode(s): origin for PL_EXPORT/LOCAL_COLLECT, destination
  for PL_IMPORT/LOCAL_DELIVER, both endpoints otherwise (flow via
  `cambridge.scope.classify_order`). Postcodes resolve through the shared
  geocode cache; unresolvable postcodes and regs without a depot mapping are
  skipped.
- A vehicle with ≥ `CATCHMENT_MIN_SAMPLES` (20) samples gets
  `radius = percentile(samples, CATCHMENT_PERCENTILE)` (95).
- Fewer samples → fallback to the fleet-wide per-type P95 (computed from the
  same frame; the vehicle's type comes from the fleet master via
  `ALL_RIGIDS`/`ALL_TRACTORS` membership, else "van").
- Every radius is floored at `CATCHMENT_RADIUS_FLOOR_KM` (30).
- Runs once in `run_alns`'s build-inputs stage (the qargo frame and postcode
  cache are already in hand); the result lands on `vehicle_df` as a
  `catchment_km` float column and flows into `VehicleMeta` (new field
  `catchment_km: float = 0.0`; 0.0 disables the penalty for that vehicle, so
  older callers/tests without the column are unaffected).

Deployment caveat (document in the module docstring): calibrating from the
planning window's own month is a fleet-behavior prior, not per-order
hindsight; a live deployment would feed trailing months instead.

### 2. Penalty — proportional phantom km in the generalized cost

New helper (in `freight_planner/vehicle_cost.py`, beside `fuel_cost_per_km`):

```python
def out_of_area_penalty_km(dist_km: float, catchment_km: float) -> float:
    """Phantom km added to the RANKING cost for a job beyond its vehicle's
    catchment: each km beyond the radius counts OUT_OF_AREA_KM_FACTOR times
    extra. 0 within the radius or when the catchment is unknown (0)."""
    if catchment_km <= 0.0:
        return 0.0
    return OUT_OF_AREA_KM_FACTOR * max(0.0, dist_km - catchment_km)
```

Job distance = haversine(vehicle home lat/lon, job service lat/lon); for
two-point jobs (DIRECT/HUB_DROP with origin coords) use the MAX of the origin
and destination distances (a near delivery with a far collection is still
out-of-area work). Straight-line is deliberate — the radius was calibrated on
the same metric.

Why proportional, not flat: a flat surcharge cannot scale with how far out of
area a job is, and small overshoots must stay almost free.

> **Amendment 2026-07-02 (found during implementation — the original example
> compared one-way km):** the ranking's route cost is a ROUND-TRIP road-km
> figure (≈ 2 × ROAD_DISTANCE_FACTOR = 2.6 × straight-line distance), so at
> factor 1.0 the penalty is bounded by 38% of route cost while flipping a
> same-depot rigid→artic needs ≥ 47.7% (the fuel-rate gap 0.319/0.216).
> **Factor 1.0 can never flip the primary B15 scenario.** Corrected default:
> `OUT_OF_AREA_KM_FACTOR = 2.6` — the overshoot counts as the round-trip ROAD
> km actually driven beyond the territory (2 × road factor), which is the
> physically meaningful scale. Consequences: a 50 km-catchment rigid loses a
> job to a same-depot artic once the job is ≥ ~1.9 × its radius away
> (~96 km), keeps jobs at moderate overshoot (60–90 km), and the long-lane
> rigids (radii 130–190 km) only flip beyond ~250–360 km. Verified:
> dist 137 km, catchment 50 → rigid 0.216 × (356.9 + 226.9) = £126.1 vs artic
> 0.319 × 356.9 = £113.8 → artic wins; dist 60 km → rigid £49.4 vs £64.7 →
> rigid keeps it.

Applied at EVERY site that ranks or accepts on generalized cost, so insertion
ranking and move acceptance never disagree (all in `freight_planner/alns.py`
unless noted):

- `_best_insert_for_job` and `_ranked_inserts_for_job`: delta becomes
  `rate * (day_km_delta + out_of_area_penalty_km(job_dist, vm.catchment_km))`.
- `changed_costs`: day cost becomes
  `rate * (ev.total_km + Σ penalty_km over the day's jobs)`.
- `route_cost_by_key` init and `solution_cost`/`route_cost` (used for
  cost_before/cost_after): same formula — otherwise the initial ledger
  disagrees with `changed_costs` and every first touch of a route creates a
  phantom delta.
- Seed insertion ranking (`freight_planner/route_seed.py::best_insertion`,
  ~line 227): both `delta` computations (existing-trip insertion ~line 252 and
  new-trip fallback ~line 264) gain the same penalty-km term. Note the seed
  ranks on km not GBP, so the term there is the raw penalty km (no rate
  multiplier) — the flip logic still works because the seed compares vehicles
  of both types on the same day and the penalty inflates only the out-of-area
  candidate.
- NOT applied: tour vehicle selection (`tour_plan`/`tours` — artic preference
  already exists via `LIGHT_TOUR_PALLETS`, and tours are long-distance by
  definition), and the KPI/report layer.

**Reported km stays physical everywhere** — `km_before`/`km_after`, route
totals, manifests and viz never include penalty km. The penalty exists only
inside the ranking objective, exactly like the per-type fuel rates.

Diagnostic: `run_alns` logs one line — how many assigned jobs sit beyond
their vehicle's catchment (count + share) — so drift is visible per run
without a new report artifact.

### 3. Knobs — `freight_planner/config.py` (as amended)

```python
CATCHMENT_PERCENTILE: float = 95.0     # per-vehicle radius = P95 of its history
CATCHMENT_MIN_SAMPLES: int = 20        # fewer -> fall back to the type radius
CATCHMENT_RADIUS_FLOOR_KM: float = 30.0
OUT_OF_AREA_KM_FACTOR: float = 2.6     # = 2 x road factor (see amendment above)
```

> **Amendment 2026-07-02 (validation finding):** fleet vehicles with ZERO
> qargo history got no radius at all (penalty disabled) — exactly where the
> optimizer then dumps long work. `build_vehicle_catchment` gained a
> `fleet_types` parameter; every fleet reg is guaranteed a radius
> (own P95 → type fallback → floor).

## Error handling

- Missing/empty `catchment_km` on a vehicle (old fixtures, direct API users):
  0.0 → penalty disabled for that vehicle; nothing crashes.
- Vehicle master/geocode gaps during calibration: skipped samples (counted and
  logged), never raise.
- No hard infeasibility is introduced anywhere: coverage cannot structurally
  drop from this change.

## Testing (TDD)

Unit (`tests/freight_planner/test_catchment.py`, new):
- calibration: a vehicle with ≥20 synthetic samples gets its own P95; one with
  <20 gets the type fallback; everything floored at 30 km; CANCELLED orders
  and unmapped regs ignored.
- penalty arithmetic: within radius → 0; at radius → 0; beyond → factor ×
  overshoot; catchment 0 → 0.
- two-point job uses max(origin, destination) distance.

Integration (`test_alns.py` / `test_route_seed.py`):
- ranking flip: a far job with both a rigid (small catchment) and a tractor
  (large catchment) feasible must choose the tractor once the overshoot
  penalty exceeds the fuel-rate gap; the same job within both catchments must
  still choose the cheaper rigid.
- acceptance consistency: `changed_costs` on a day containing an out-of-area
  job includes the same penalty as the insertion ranking (no phantom
  improvement from moving a job without changing anything).
- coverage guard: when ONLY the out-of-area rigid is feasible, the job is
  still served (soft, not a gate).

## Validation

One `run_alns` per week (12-17, 19-24; 90 s budget; `FP_ALNS_CONSERVE=1`;
`freight_planner/out`), trip-app regeneration only. Success criteria, reported
without tuning iterations (standing rule):
- B15 symptom shrinks: rigid legs >120 km drop from ~75/week toward only the
  long-lane vehicles' work;
- coverage holds (wk1 99.7%, wk2 99.8%);
- km/cost deltas reported against the realism-pack baselines
  (wk1 94,034 km / wk2 108,296 km).

## Scope / out of scope

In scope: `freight_planner/catchment.py` (new), `vehicle_cost.py`,
`alns.py` (cost sites + VehicleMeta), `route_seed.py` (ranking site),
`run_alns.py` (calibration call + diagnostic log), `cambridge/config.py`
(4 knobs), tests, QUEST_LOG entry.

Out of scope: tour vehicle selection, hard compatibility gates, per-lane or
per-customer modeling, trailing-month calibration windows, viz/report changes.

## Constraints

- **No `git commit`** (standing stakeholder instruction).
- Viz regeneration = trip_app (`viz_app.py`) only.
- Pipeline outputs → `freight_planner/out`.
- No validation-run tuning loops; report results as they land.
