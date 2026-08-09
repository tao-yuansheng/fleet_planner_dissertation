# README_STATIC — the static ALNS planner, in detail

How the full-knowledge planner (`run_alns.py`) works: the data pipeline that turns raw TMS
orders into schedulable jobs, the staged constructive seed, and the coverage-aware ALNS
search that improves it. This is the *narrative* companion to `PIPELINE.md` — read this to
understand the machine, read PIPELINE.md (§ references below) for every constant and the
code-verified fine print. Setup and data-quality rules live in `README.md`; the dynamic
dispatcher that drives this machinery online lives in `README_DYNAMIC.md`.

**What "static" means.** One solve sees a whole Mon–Sat window with full knowledge of every
order in it — no booking times, no commitment, no replanning. That makes it the optimistic
*backtest bound* (what a clairvoyant dispatcher could have planned) and the shared solve
engine: the dynamic loop calls these same builders and solvers once per decision epoch.
Weeks chain through a handover artifact so a month replays as a realistic sequence.

**The problem it solves** (formally §10a): a multi-depot, multi-trip, site-dependent
PDPTW with *optional service* over a heterogeneous 79-vehicle fleet, ~2,400–2,600 daily
jobs + 50–100 tour jobs per week. The objective is lexicographic: **maximise served jobs
first** (no distance saving can ever displace a served order), then minimise generalized
cost = `Σ g_type × (road_km + 2.6 × out-of-catchment km)` where the per-km road rate
`g_type = fuel + R&M` (tractor 0.487 / rigid 0.356 / van 0.257 GBP/km = measured fuel
0.327 / 0.236 / 0.177 **plus** maintenance/tyres 0.16 / 0.12 / 0.08, R&M layer added
2026-07-25) **+ the driver-day activation/overtime cost
(§10a) + the soft delivery-window lateness penalty** (`λ·late² + ε·early`, delivery-only,
default ON 2026-07-18 — a late delivery is priced, not hard-rejected, giving the
hierarchy on-time < early < late < slip; `--hard-time-windows` restores the hard cutoff).
Misses are information, not failures: every unserved order carries an explicit reason.

## Module map

| stage | modules |
|---|---|
| entry points | `run_alns.py` (one window), `run_month.py` (handover-chained windows), `build_phase0.py` (data spine only), `run_rolling.py` (dynamic — see README_DYNAMIC) |
| orders → demand | `demand.py` (DemandRecords + responsibility), `enrich.py`/`build_enriched.py` (verified-leg embedding), `shared/scope.py` (classification + window policies), `shared/verified_legs.py` |
| demand → legs | `legs.py` (movement-leg emission, splits, hubs), `date_basis.py` (window scoping), `handover.py` (week chaining) |
| geography | `geocode.py` (+ `shared/postcode_resolver.py`), `shared/routing.py` (OSRM client + pair cache), `route_costs.py` (road_km / road_minutes / service / breaks), `osrm_setup.py` (warm-up), `speed_calibration.py` (per-road-class factors) |
| fleet | `vehicles.py` (vehicle states incl. the 13 h shift clamp), `catchment.py` (learned P95 radii), `vehicle_cost.py` (fuel + R&M rates + phantom-km), `vehicle_actuals.py` (telematics actuals, validation side) |
| legs → jobs | `jobs.py` (candidate jobs, dependency typing, hard blockers), `options.py`/`options_resolver.py` (DIRECT/XDOCK, TRUNK/HUBDROP pre-resolve), `compatibility.py` (vehicle×job mask), `dayflex.py` (K2, dormant) |
| freight state | `state.py` (initial states), `freight_ledger.py` (stateful execution ledger), `ledger.py` (stateless post-hoc checker) |
| seed | `route_seed.py` (daily greedy insertion), `tour_plan.py` (multiday orchestration), `tours.py` (classification/clustering/evaluate_tour), `trunk.py` (T1/T2 night + day trunks), `shuttle.py` (K1 carve-out), `cross_depot.py` (accounting) |
| search | `alns.py` (the ALNS), `routing_adapter.py` (evaluate_route/evaluate_day — the feasibility oracle), `merge_sweep.py` (post-loop same-address merges) |
| emission | `reports.py`, `manifest.py`, `plan_records.py`, `plan_schema.py`, `plan_full.py`, `kpi.py`, `utilization.py`, `runsheets.py`, `month_summary.py`, `output_layout.py`, `reconcile.py`/`validate.py` (report writers), `plan_validation.py` (temporal audit), `progress.py` (run log) |
| viz (read-only) | `viz_app.py` (validate scorecard), `viz_map.py`, `viz_timeline_build.py` (dynamic board) |
| legacy (superseded, kept) | `seed_planner.py`/`planner_state.py` (M2/M3 first seed), `repair.py` (early unassigned-repair pass), `run_seed.py`/`run_route_seed.py` |

## 1 · From raw orders to schedulable jobs

**Inputs** (§1): the monthly *enriched* orders parquet (raw universe + embedded
`verified_leg` — see README.md), `vehicle_master.csv`, the postcode cache, depot anchors in
`shared/config.py`, optionally a prior week's `handover.json`. Telematics is NOT a planning
input (calibration/validation only).

**Demand** (`demand.py`, §2): one `DemandRecord` per in-window order. The verified leg
decides responsibility (`responsibility_shape()`): FULL_END_TO_END / NETWORK_IMPORT (we
deliver) / NETWORK_EXPORT (we collect) / PICKUP_ONLY / DELIVERY_ONLY / OUT_OF_SCOPE /
AMBIGUOUS_PARTIAL — mind the naming trap (README.md). Exclusions (CANCELLED, CRANE_HIRE,
NO_RESOURCES — a subcontractor does NOT count as our resource, AMBIGUOUS_MANUAL) stay in
the universe as accounting rows so coverage always reconciles.

**Movement legs** (`legs.py`, §3): each order expands to its physical movements with
origin/destination *nodes* (CUSTOMER / DEPOT / B37_HUB / LE10_HUB), raw AND effective
windows + hardness, freight-ready times. PL_IMPORT → inbound-trunk accounting leg + a
dispatchable delivery; PL_EXPORT → pickup + outbound-trunk, OR a direct HUB_DROP (mutually
exclusive options); FULL_FLEET → DIRECT (one vehicle end-to-end) vs XDOCK (pickup + later
depot delivery) option groups; LOCAL_* → single legs. Window policies come from
`shared/scope.py`: date-only midnight stamps expand to operating-day windows, and
**collections never comply with historical actual times** (that would leak the human
plan's answer — hindsight hardening). Orders above the fleet ceiling split into per-part
freight units; hazchem routes to LE10, everything else to B37.

**Vehicles + catchments** (§5): one row per vehicle from the master (type, capacities,
home anchor, shift span clamped to 13 h, multi-trip history). `catchment.py` learns each
vehicle's service radius = P95 of historical order distances (fallback per-type, floor
30 km); it feeds a **soft** ranking penalty only (2.6× per out-of-area km) — never a hard
gate, so territory can't cost coverage.

**Candidate jobs** (`jobs.py`, §6): dispatchable legs become jobs with dependency typing —
`PRODUCES_DEPOT_FREIGHT` ↔ `REQUIRES_PRIOR_PICKUP` (XDOCK pairs), `PRESTAGED_DELIVERY`,
`PICKUP_TERMINAL`, `NONE_DIRECT` — and hard blockers set once (BAD_GEOCODE,
MASSIVE_UNSUPPORTED, NO_CAPABLE_VEHICLE, MISSING_WINDOW…). Blocked jobs skip planning but
stay in the manifest with their reason.

**Mode resolution** (§8): **DIRECT-vs-XDOCK is endogenous** (2026-07-23) — both option groups
flow into the optimizer and the seed + ALNS choose the mode on real routed cost (the
in-loop "option-swap" that was once future work is now the `OptionSwap` ALNS operator; the
static `ρ = 1.6` resolver was deleted). Mutual exclusion is held by `option_mutex.OptionMutex`
across seed + ALNS, by an `option_index`-seeded mutex inside the rolling loop's `insertion_pass`
(2026-07-28 — closed a gap where a later micro-pass could insert a freight's rival option-group
leg with no memory that an earlier epoch had already committed the other side), and by a
`drop_superseded_option_legs` commit-boundary backstop that additionally never drops an
already watermark-committed leg (2026-07-28). The seed's own DIRECT-vs-XDOCK pick is insertion
order (`_DEP_RANK`), not a live cost comparison — `_REPAIRABLE_REASONS` including
`OPTION_SUPERSEDED` (2026-07-28) is what gives the seed's loser a genuine cost-based second
look via ALNS's `OptionSwap` operator. Only **TRUNK-vs-HUBDROP** (`options_resolver.py`,
PL_EXPORT) is still resolved before routing — the scheduled depot→hub trunk is not in routed
km, so the router cannot price it.

**Compatibility mask** (`compatibility.py`, §9): the full vehicle × job matrix (capacity,
geocode, time-reachability) — the SDVRP mechanism. Seed and ALNS both consume only `OK`
pairs.

**Freight ledger** (`freight_ledger.py`, §10): the stateful execution ledger — freight
cannot be delivered from a depot it isn't physically at (prestaged or produced by an
earlier committed pickup). Phantom crossdock deliveries are impossible *by construction*;
`ledger.py` re-checks the final plan statelessly.

**Road model** (`route_costs.py`, §7): OSRM road km (memoised + persisted pair cache;
haversine ×1.3 fallback). Travel time default is the **v1.1 OSRM duration model** —
per-road-class times × calibrated per-type factors (HGV 1.0 — OSRM car free-flow matches
realized HGV time; van 0.75), the constant-speed model (50 km/h daily / 80 km/h tours)
remains the flag-off fallback. Customer service time is fixed per distinct visit:
15 minutes for vans/rigids and 30 minutes for tractors.
EU-561 core breaks (45 min per 4.5 h driving) accrue inside route evaluation.

## 2 · The staged seed (§11)

Order is load-bearing (a far tour delivery can depend on a near daily collection):

1. **Tour classification** (`tours.is_tour_only`): a job whose same-day depot round-trip
   cannot fit the driving cap is tour-only; two-point legs classify on the full carry.
2. **Daily pre-pass** — measures per-vehicle-day busyness so tours prefer idle vehicles.
3. **Tour formation** (`tours.build_tours`): far jobs pool fleet-wide, cluster under a
   200 km cohesion radius, resolve to concrete multi-day tours (depot-load fronts,
   km-guarded consolidation of far DIRECT moves, salvage re-pooling). `evaluate_tour`:
   real road km at 80 km/h, 10 h driving + 13 h duty per tour-day, due-date-as-deadline,
   4-day ceiling.
4. **Tour vehicle assignment**: prefer artics (rigids for light tours), anchor depot, idle
   vehicles; the chosen vehicle's day-span is **reserved** away from daily planning.
5. **T1/T2 fixed night trunk** (`trunk.py`): nightly depot↔hub shuttles sized **export-only**,
   ceil(PL_EXPORT pallets/deck) per depot (BEDFORD, CB22; LE10 hazchem is
   CB22-only); `draw_tractors` names the tractors (recorded per-night as `vehicles` +
   `feasible` pool). Network IMPORT freight arrives at the depot via the unmodelled "invisible
   hub" (treated as spawning at the depot) and never charges a trunk trip — the tractor still
   round-trips and returns empty (km unchanged). STOKE has no night trunk — its exports ride a
   same-day **day-trunk** to B37. Trunk km is a separate fixed accounting line, never inside the
   search objective.
6. **Daily seed** (`route_seed.py`): greedy constructive insertion, jobs ordered by
   (service_date, pickups-first, latest_finish); for each job try every OK vehicle's
   existing trips then a new trip under the learned cap (+10,000 discouragement on extra
   loops — prefer idle vehicle-days over marginal km). Freight readiness gates deliveries
   through the shared ledger. **K1 shuttle carve-out** (`shuttle.py`): an address-day
   reaching an artic load (≥26 pal, ≥0.9 fill) packs into dedicated shuttle trips, PINNED
   for the search.
7. **Stranded-backhaul repair** (in `tour_plan.py`): only when BOTH XDOCK legs stranded,
   flip the order to a synthetic DIRECT carry into an existing tour's empty leg or a new
   batched tour.
8. **Commit tours** against the same ledger (feeding collections are in place by now).

## 3 · The ALNS search (`alns.py`, §12)

**State**: `solution[(vehicle, day)] = [trip, trip, …]` — multi-trip aware (capacity
resets per trip, 30-min reload dwell, shift/driving/breaks evaluated across the whole day
by `routing_adapter.evaluate_day`), plus the repairable unassigned pool — so **coverage can
increase during the search**. Tours, trunk and shuttle pins are fixed (their cost is a
constant offset, correctly outside the search objective).

**Loop** (up to `--iterations`, stopped by time budget, no-improve 4000, or convergence):

```
op ← adaptive roulette over {random, worst, shaw};  k ~ U[2,5]
R  ← op.remove(k, solution ∖ pinned)
every 20th iteration: add coverage specs — try inserting up to 8 unassigned jobs,
  making room by same-day removals / trip ruins / single-incumbent ejection
repair: greedy best-position insertion over (vehicle, day, trip, position),
  including opening a new trip under the learned per-vehicle cap (2..12)
price: every changed day re-evaluated WHOLE; any infeasible day refuses the spec
  (B16: OSRM breaks the triangle inequality — removing a stop can bust a day)
accept: improving always; worse via SA exp(−Δ/T), T₀ = 0.005×seed cost, ×0.999/iter
best is tracked separately — acceptance noise can never degrade the result
```

**Feasibility oracle** (`routing_adapter.evaluate_route`/`evaluate_day`): walks stops
computing km → drive minutes → owed breaks → arrivals; window + 90-min curbside wait rule;
load walk per leg kind; depot return; shift + 10 h driving cap across trips. Everything —
seed, ALNS, merge sweep — prices through this one oracle.

**Convergence gate** (default ON since 2026-07-13): stop once best improves < 0.05% over
the last 500 iterations (min 1,500); a served-count gain always counts as improvement.
`--iterations` is a cap; `--converge-pct 0` restores fixed budgets (provenance replays).

**Post-loop**: `merge_sweep.apply_zero_cost_merges` collapses same-day same-address split
visits when feasible and net-km ≥ 0 (one truck per dock; in dynamic runs it carries the
full watermark/floor guards — see README_DYNAMIC). `--restarts` keeps the best of
independent passes.

**What empirically matters** (Ch.5 ablations, §12): at operational budgets the destroy
neighbourhood is the only lever that matters — worst-removal is load-bearing (+3.9% cost
without it) and the default 2..5 band under-destroys (2..8 ≈ −3.3%); SA acceptance is
inert, RRT worse, regret-2 repair cost-neutral at 2–3× the wall; Shaw pays only at depth
(20k+). ALNS is budget-limited: 1800 s is the re-baselined deep operating point.

## 4 · Outputs and verification (§13–§14)

Folder anatomy in README.md (root deliverables + `csv/` + `reports/`; legacy runs keep
`plan/`+`reports/` and stay readable). Run root: `plan_full.csv` (AUTO since 2026-07-14 —
one denormalised row per movement, the whole-plan overview), `runsheets.html`,
`handover.json`, `validation_metrics.json`, `alns_progress.log`, `run_manifest.json`.
`csv/`: `plan_manifest_new.csv` (the reconciliation spine — every order ROUTED /
accounted / unassigned-with-reason), `route_stops.csv` (stop-by-stop with timings, breaks,
load, due-date audit columns), `selected_plan_alns.csv`, vehicle/trip utilization rollups,
`unassigned_jobs.csv`, `depot_inventory_timeline.csv`, `trunk_schedule.csv` (per-night
sizing + named tractor assignments). `reports/`: `kpi_summary`, `alns_summary`, decision-log
reports, `plan_full_dictionary`.

Every run must end with: temporal violations = 0, ledger violations = 0, every order
accounted in the manifest; coverage is lexicographically protected throughout. Optional
in-loop assertions: `FP_ALNS_CONSERVE=1` (job conservation after every accepted move).

## 5 · Running it

```powershell
python -B -m freight_planner.run_alns --start 2026-01-12 --end 2026-01-17 --time-budget 120
```

Key knobs (defaults = production config, §0): `--time-budget 120` (seconds per restart),
`--iterations 100000` (cap), `--no-improve 4000`, `--seed 0`, `--restarts 1`,
`--repair-every 20`, `--router osrm`, `--handover-in <prior handover.json>`,
`--converge-pct/-window/-min-iters` (the gate), `--day-flex` (K2, off), `--regret-repair`
(off). Month chaining via `run_month.py --windows a:b c:d …` (auto-wires handovers;
cross-month MUST pass `--qargo`; emits `month_summary.md` — the (vehicle,day)-MATCHED gap
is the citable comparison).

**Determinism** (§17): one `random.Random(seed)`; fixed iterations + seed reproduce cost
trajectories bit-identically (wall-clock varies ±20%, so experiments are
iteration-primary). All experiment toggles are env-gated, default-off, recorded in
`run_manifest.env_toggles`, and proven bit-identical when unset. Reproducibility protocol
in README.md.

**Known limitations** (§19): mode choice pre-resolved, not searched; tours seeded, not
searched (claim automated *classification*); driver-hours = EU-561 core only (vans
exempt); cost is fuel-only; site-access data absent; shared caches lack write locks;
removal band under-destroys at operational budgets.
