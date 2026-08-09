# Freight-planner codebase separation — Phase 1: dependency closure (design of record)

Goal: every code file the CURRENT pipeline uses lives inside `freight_planner/`.
Phase 2 (separate, later): reorganize `freight_planner/` internally (code vs runs vs reports).

Method: AST import scan of all 62 `freight_planner/*.py` files, then transitive closure of
every external module reached, plus every `Path(__file__)`-anchored data file. Verified
2026-07-13 on the restored tree (post rename-back).

## A. External modules the pipeline imports (the complete list)

| module | lines | imported by (freight_planner) | drags in |
|---|---|---|---|
| `cambridge/config.py` | 639 | 15 files (catchment, compatibility, cross_depot, epoch_state, manifest, options_resolver, osrm_setup, route_costs, routing_adapter, run_rolling, tours, tour_plan, utilization, vehicles, viz_app) | 3 data-file anchors (§C) |
| `cambridge/scope.py` | 1,080 | catchment, demand, legs | config, plan_types, verified_legs, postcode_resolver |
| `cambridge/plan_types.py` | 208 | (via scope) | — leaf |
| `cambridge/verified_legs.py` | 95 | demand (+ scope) | — leaf |
| `simulation/postcode_resolver.py` | 174 | (via scope; verify_legs) | — leaf |
| `simulation/routing.py` | 327 | route_costs, run_alns, speed_calibration, viz_app, viz_map | `profitability_report.profitability_report_merged._haversine_km` ONLY (7-line function → inline it); 2 data anchors (§C) |
| `operational_analysis/fleet_replay_data.py` | 412 | viz_map, vehicle_actuals | shapely (3rd-party, fine); 4 data anchors (§C) |

Also: 5 test files in `tests/freight_planner/` import cambridge/simulation directly
(test_breaks, test_compatibility_screen_flag, test_geocode, test_plan_records, test_tours) —
re-pointed in the same pass.

## B. Offline data-repair scripts (pipeline members living in OTHER folders)

| script | lines | role | external imports |
|---|---|---|---|
| `planning_agent/verify_legs.py` | 573 | verified-leg truth regen | cambridge.config/.scope internals, simulation.postcode_resolver, operational_analysis.export_replay |
| `planning_agent/build_vehicle_master.py` | 290 | vehicle_master.csv regen (untracked artifact) | cambridge.config |
| `operational_analysis/export_replay.py` | 1,712 | telematics replay export (verify_legs dep) | fleet_replay_data (already in §A), postcode_resolver |

Decision: include in Phase 1 (recommended — single source of truth, no drift window) or
re-point in Phase 2. Their closure adds NO new modules beyond §A.

## C. Data files (do NOT move; re-anchor code instead)

All anchors currently resolve `<pkg>/__file__ → parent(s) → logistics root`. Copied modules
one level deeper would silently break them — this exact failure crippled run partb6
(vehicle list unread → seed halved). Fix: ONE explicit root in the vendored package —
`LOGISTICS_ROOT = Path(__file__).resolve().parents[2]` — and every path derived from it.

- `data/Input/supatrak/supatrak_vehicle_list_enriched.csv` (config: fleet master)
- `data/Output/cambridge/vehicle_profiles_derived.json` (config: v1.5 profiles)
- `data/Output/cambridge/state/` (config: STATE_DIR)
- `data/Output/osrm_cache.json`, `data/Output/cambridge/tod_multiplier.json` (routing)
- `data/Input/supatrak/` (telematics CSVs), `data/Input/orders/`, `depot_data/depot_addresses.json`, `.cache/` (fleet_replay_data)

## D. Target layout (Phase 1)

New subpackage `freight_planner/shared/` — COPIES (verbatim + import/anchor edits only);
legacy `cambridge/`, `simulation/`, `operational_analysis/` stay untouched in place until
Phase 2 decides their fate:

```
freight_planner/shared/
  __init__.py
  paths.py               # LOGISTICS_ROOT — the single data anchor
  config.py              # <- cambridge/config.py   (3 anchors -> paths.LOGISTICS_ROOT)
  scope.py               # <- cambridge/scope.py    (imports -> relative .config/.plan_types/.verified_legs/.postcode_resolver)
  plan_types.py          # <- cambridge/plan_types.py
  verified_legs.py       # <- cambridge/verified_legs.py
  postcode_resolver.py   # <- simulation/postcode_resolver.py
  routing.py             # <- simulation/routing.py (haversine inlined; 2 anchors -> paths)
  fleet_replay_data.py   # <- operational_analysis/fleet_replay_data.py (4 anchors -> paths)
```

Import rewrites (mechanical, freight_planner + its tests only):
`cambridge.config → freight_planner.shared.config` (15+ files) ·
`cambridge.scope → freight_planner.shared.scope` ·
`cambridge.verified_legs → freight_planner.shared.verified_legs` ·
`simulation.routing → freight_planner.shared.routing` ·
`operational_analysis(.fleet_replay_data) → freight_planner.shared.fleet_replay_data`.

If §B included: scripts move to `freight_planner/tools/` with the same rewrites.

## E. Gates (all must pass before the migration is "done") — EXECUTED 2026-07-13

The naive byte-gate is INVALID without a pinned protocol: the solver is wall-clock
sensitive (`--time-budget` cuts by load), hash-seed sensitive, and the OSRM cache file
mutates across runs. Valid protocol, proven by a two-run control pair (identical bytes):
`PYTHONHASHSEED=0` + iteration-bound only (`--time-budget 600` so 300 iters always bind)
+ `data/Output/osrm_cache.json` snapshot/restored before every gate run. Pre-migration
references are produced by mechanically inverting the six import rewrites (SHA-bookkeeped,
verified restored), since the tree has no git.

1. Suite: **PASS** (703).
2. Static byte-gate (12→13 Jan, 300 iters, seed 0): **PASS** — but only after catching a
   REAL migration bug the first anchor sweep missed: `verified_legs.py` anchored
   `planning_agent/verified_legs.csv` at `__file__.parent.parent`; the copy silently read
   NO verified legs → classification drift (11-order universe shift, 2 BAD_GEOCODE).
   Re-anchored via paths.LOGISTICS_ROOT; full `__file__` sweep of shared/ + tools/ now
   clean (paths.py is the only anchor).
3. Rolling byte-gate (partb7 args, pinned pre/post pair, 6 days, 14 CSVs incl.
   plan_snapshots): **PASS — byte-identical.**
4. build_vehicle_master A/B (legacy vs tools, aside-copy protocol): **PASS with a
   documented improvement** — outputs identical except the `depot` column and row order.
   The legacy script's own `sys.path` never included the logistics root, so its
   `from cambridge.config import VEHICLE_DEPOT_MAP` ALWAYS failed silently
   (`except Exception: {}`) and the column has been all-NaN forever; the tools copy
   fills 79/98. Consumer-safe: `vehicles.py` reads only reg/payload/pallets from the
   CSV and takes home depots from VEHICLE_DEPOT_MAP at runtime.
   verify_legs A/B (aside-copy protocol on verified_legs.csv, before_gpsmatch and the
   postcode cache; run strictly AFTER the rolling gate since it overwrites an input of
   that gate): **PASS — byte-identical** (legacy 6e81ec59… == tools 6e81ec59…).

**Phase 1 verdict: COMPLETE.** freight_planner imports nothing outside itself. Gate scratch
dirs removed; the invalid partb6 run carries an INVALID_RUN.md marker.

**Archival (same day, user-approved).** `cambridge/`, `simulation/`, `operational_analysis/`,
`planning_agent/` moved to `_archive/2026-07-13_separated_legacy/` together with their test
suites (tests/cambridge, tests/planning_agent, and the root path-hack tests of simulation:
test_routing, test_vrptw_*, test_window_start, test_route_collapse, test_actuals_loader,
test_data_loader, test_on_time). Before the move, LIVE artifacts were extracted:
`verified_legs.csv` (the runtime responsibility truth — the raw qargo parquet has no
verified_leg column, so every run takes the CSV fallback; verified in-sync with the enriched
parquet, 19,458/19,458 exact) and `mot_results.csv` → `freight_planner/data/`;
`diff_verified_legs.py` vendored to tools/. Tests covering vendored code were re-pointed and
moved into tests/freight_planner (postcode_resolver, fleet_replay_data, verify_legs,
diff_verified_legs; two stale assertions fixed: the postcode test predated the
terminated-endpoint retry, and two OSRM tests monkeypatched "simulation.routing" by STRING —
string module paths evade import-rewrite sweeps; grep quoted paths too). Full tests/ tree
now collects and passes (761) — it did not even collect cleanly before the archive.
Known now-broken dependents left in place by user scope: investigations/*.py,
run_analysis_pipeline.py, supatrak_run_analysis_pipeline.py, optifleet_run_analysis_pipeline.py
(see _archive/2026-07-13_separated_legacy/README.md).

## F. Explicitly OUT of the closure (checked, not needed)

`cambridge/{dispatcher,day_coordinator,collection_planner,multiday_*,repositioning,...}`
(the old Cambridge pipeline), `simulation/data_loader.py` (only legacy phase0_spike),
`profitability_report/` (one 7-line function, inlined), root scripts
(`convert_qargo_parquet.py`, analysis pipelines), `old_simulation` remnants. `tests/cambridge/`
keeps testing the legacy package; the scope/config/verified_legs test files get shared/
counterparts in Phase 2.
