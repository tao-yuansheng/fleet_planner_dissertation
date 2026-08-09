# Freight Planner — Guide (setup · layout · running · data quality)

This package is a **freight dispatch planner** for a UK regional palletised-freight
(groupage) carrier: it ingests a week of TMS ("Qargo") orders and emits vehicle-by-vehicle,
stop-by-stop route plans for a ~79-vehicle fleet across three depots and two national
pallet-network hubs. It runs in two modes over the same machinery:

- **Dynamic rolling-horizon dispatcher** (`run_rolling.py`) — the operative mode: replays
  each day as decision epochs, seeing only orders knowable at each moment and *committing*
  work to drivers as it dispatches. This is the realistic online problem a live product runs.
- **Static full-knowledge planner** (`run_alns.py`) — plans one whole window at once; the
  optimistic backtest bound and the shared solve machinery the dynamic loop drives.

Both are validated against January–February 2026 by re-planning the weeks the human
dispatchers actually planned and comparing against GPS/odometer telematics.

**This file is the guide only** — how to set up, run, and trust the data. For how the
system works, read the deep-dives below.

## Documentation map

| doc | what it is |
|---|---|
| `README.md` (this file) | setup, folder layout, run commands, **data quality control** (verified_leg, flow semantics) |
| `README_STATIC.md` | the static ALNS planner in detail — data pipeline, staged seed, the ALNS search, outputs |
| `README_DYNAMIC.md` | the dynamic rolling-horizon dispatcher — epochs, visibility, commitment, the rules, mechanisms |
| `PIPELINE.md` | the code-verified reference: every stage, every config value, the decision boundary, limitations. When prose here and PIPELINE.md disagree, PIPELINE.md wins. |
| `RULES.md` | the dynamic system's invariants (what it must obey / must never do), one rule per enforcement point, + known gaps |
| `QUEST_LOG.md` | chronological build/debug log and backlog (newest first) |
| `DESIGN_LOG.md` | the original architecture rationale + the Q1–Q11 decision record (was README.md until 2026-07-14; frozen history) |
| `REMAINING_IMPLEMENTATION_SPEC.md` | historical build spec (M-milestones); superseded by the shipped system |
| `experiments/README.md` | the Ch.5 experiment campaign design-of-record (+ `METRICS.md`, `PROVENANCE.md`) |

## Folder layout

```
freight_planner/
  *.py                    the pipeline (flat, import-stable: provenance-pinned commands
                          like `python -m freight_planner.run_alns ...` keep working)
  shared/                 vendored network-model library — config (depots/fleet/constants),
                          scope (order classification + window policies), plan_types,
                          verified_legs, postcode_resolver, routing (OSRM client + cache),
                          fleet_replay_data (telematics loading);
                          shared/paths.py holds LOGISTICS_ROOT, the ONE data-file anchor
  tools/                  offline data-repair, run as `python -m freight_planner.tools.<x>`:
                          verify_legs (regenerate the ownership truth), build_vehicle_master,
                          export_replay, diff_verified_legs
  data/                   planner-owned runtime artifacts: enriched_orders_<month>.parquet
                          (the order input), vehicle_master.csv (the fleet input, UNTRACKED —
                          regenerate), verified_legs.csv (regen artifact + fallback),
                          mot_results.csv, calibration/ (OSRM speed factors)
  runs/                   CANONICAL monthly outputs — runs/<YYYY-MM>/<window>/{inputs,plan,reports}
  viz_timeline_template.html   the evolving-plan dispatch-board template
  README*.md · PIPELINE.md · RULES.md · QUEST_LOG.md · DESIGN_LOG.md   (see map above)
```

**Two config.py files — both are read, they are different layers:**

- `freight_planner/config.py` — the PLANNER'S knobs: how the system behaves. Tour rules,
  trunk sizing/depots, shuttle, micro cadence (`MICRO_EVERY_MIN`), the convergence gate
  (`ALNS_CONVERGE_*`), the 13 h duty clamp, feature flags (`USE_OSRM_DURATIONS`,
  `SAME_ADDRESS_DWELL_MERGE`, `TOUR_ATTACH_ENABLED`…). Tuning happens here.
- `freight_planner/shared/config.py` — the vendored WORLD MODEL (verbatim copy of the
  legacy `cambridge/config.py`, byte-gated at the 2026-07-13 separation): depot anchors,
  the fleet map/profiles (loaded from `data/vehicle_master.csv` at import), calibrated
  service-time and speed constants, EU-break constants. Rarely edited by hand — the fleet
  side regenerates via `tools/build_vehicle_master`.

Rule of thumb: changing how the planner *decides* → `config.py`; changing what the
*world is* (depots, vehicles, calibrated physics) → `shared/config.py` / the vehicle
master. Values of record for both: PIPELINE.md §18.

Data **inputs** stay OUTSIDE the package at the logistics root and are reached only via
`shared.paths.LOGISTICS_ROOT` (a relocated module must fail loudly, never silently read
nothing):

```
BackEnd/logistics/
  data/Input/orders/qargo_<span>.parquet    raw monthly TMS order universes (enrichment inputs)
  data/Input/supatrak/...csv                telematics traces (validation/calibration only)
  data/Output/postcode_cache.json           geocode store (postcodes.io-backed)
  data/Output/osrm_cache.json               persisted OSRM pair matrix
  depot_data/ · fleet_replay_exports/       depot config sources, telematics replay exports
```

## Setup

1. **Python**: the project venv is `ZECURE-Phase2-main/.venv-1` (Python 3.12).
   `pip install -r requirements.txt` from `BackEnd/logistics` (pandas, numpy, geopandas,
   folium, requests, …). NOTE: parquet IO additionally needs **pyarrow** — it is installed
   in the venv but not yet listed in requirements.txt.
2. **Working directory**: run everything from `BackEnd/logistics` as a module —
   `python -m freight_planner.<entry>`. Never copy the package elsewhere and run the copy
   (see traps below).
3. **OSRM via Docker Desktop** (production distance/time model): the planner queries a
   self-hosted OSRM server at `http://localhost:5000`, run with Docker.
   - **Install Docker Desktop** (on Windows: WSL2 backend) and make sure the engine is
     RUNNING before you start a planner run — a stopped daemon is the usual cause of
     "connection refused" and a silent haversine fallback.
   - **One-time preprocessing** (~already done on this machine: the Great Britain dataset
     lives at `e:\BEAT\osrm\`, extracted → partitioned → customized, car profile). On a
     fresh machine, download the Geofabrik GB extract and run the three
     `osrm/osrm-backend` containers (`osrm-extract` → `osrm-partition` →
     `osrm-customize`) — full commands in `../docs/osrm-setup.md` (note: that doc's usage
     example shows the LEGACY dispatcher; the freight_planner flag is `--router osrm`).
   - **Start the server** (from the folder holding the `.osrm` files):
     ```powershell
     docker run -t -i -p 5000:5000 -v "${PWD}:/data" osrm/osrm-backend `
       osrm-routed --algorithm mld --max-table-size 1000 /data/great-britain-latest.osrm
     ```
     Verify: `curl "http://localhost:5000/table/v1/driving/-0.12,51.5;0.16,52.1?annotations=distance,duration"`.
   - Warm-up is automatic (the run pre-builds its `/table` matrix and persists new pairs
     to `data/Output/osrm_cache.json`). Without OSRM the run falls back to haversine ×1.3
     and the constant-speed model — it works, but plans are NOT comparable to OSRM-based
     runs; keep the server up for anything citable.
4. **Reproducibility protocol** (for citable runs): `PYTHONHASHSEED=0 python -B -m …`,
   iteration-bound (fixed `--iterations`, and `--converge-pct 0` when replaying pre-gate
   provenance runs), and snapshot/restore the two shared caches if byte-identity matters.
   Hash-seed sensitivity was disproved by A/B (RULES.md F1) — the pin stays as
   defense-in-depth. The two caches are shared mutable JSON files with no write lock:
   don't run two planners (or a planner + verify_legs) concurrently.

## Running the planner

Dynamic rolling dispatch (the operative mode; see `README_DYNAMIC.md` §7 for every flag).
The frequently-used flags together, explicit convergence gate included, so the command is
reproducible on its own without cross-referencing `config.py`:

```powershell
python -B -m freight_planner.run_rolling --start 2026-02-02 --end 2026-02-03 `
  --out-dir freight_planner/runs_<label>_2day `
  --iterations 10000 --seed 0 --delta-r1-min 90 --micro-every-min 30 --converge-pct 5
```

Bash (Git Bash / WSL) equivalent, one line, no backtick continuations:

```bash
python -m freight_planner.run_rolling --start 2026-02-02 --end 2026-02-03 --out-dir "freight_planner/runs_<label>_2day" --iterations 10000 --seed 0 --delta-r1-min 90 --micro-every-min 30 --converge-pct 5
```

**`--converge-pct 5` (5%) is a deliberately LOOSE gate**, not the default
(`config.ALNS_CONVERGE_PCT` = 0.15%) — fast turnaround for development/debugging, not
citable results. Omit the flag (or pass `--converge-pct 0.15`) for the default gate before
reporting numbers.

Static single window (full-knowledge backtest; see `README_STATIC.md`):

```powershell
python -B -m freight_planner.run_alns --start 2026-01-12 --end 2026-01-17 --time-budget 120
```

Chain a whole month (each week warm-starts from the prior week's `plan/handover.json`;
the default order file since 2026-07-22 is the COMBINED Jan+Feb enriched parquet — the
monthly files are booking-month universes and lose a month's first-week carry-in dues):

```powershell
python -B -m freight_planner.run_month `
  --windows 2026-01-05:2026-01-10 2026-01-12:2026-01-17 --time-budget 120
```

Data spine only, no routing (`build_phase0.py`): builds the demand/legs/states CSVs.

Viewing results: `viz_app.py --plan-dir <plan> --validate` (trip map + fleet scorecard vs
telematics), `viz_map.py` (static trip/compare maps), `viz_timeline_build.py --run-dir
<window> --out <data.json> --html <board.html>` (the evolving-plan board for dynamic
runs). Viz is ALWAYS read-only.

## Anatomy of a run folder

Every run writes `<out-dir>/<YYYY-MM>/<start>_to_<end>/` (layout since 2026-07-14; the
old `{inputs,plan,reports}` split is retired — every reader still opens pre-restructure
runs via the legacy fallback):

```
<window>/
  run_manifest.json        what produced this run: window, args, env toggles
  plan_full.csv            AUTO: one denormalised row per plan movement — the whole-plan
                           overview (row count == manifest; columns documented in
                           reports/plan_full_dictionary.md)
  runsheets.html           per-driver printable runsheets (from route_stops)
  timeline.html            AUTO on dynamic runs: the evolving-plan gantt dashboard +
                           click-to-open map view (road-snapped OSRM routes, a simulated
                           truck, committed-vs-internal overlay). Needs internet for tiles.
  alns_progress.log        the tailable run log (stages, convergence, audits)
  handover.json            end-state consumed by the next window's --handover-in
  validation_metrics.json  seed→ALNS km/cost, moves, vehicle-days, trunk line
  rolling_manifest.json    dynamic runs: anchor/micro registry + ledger summary
  csv/                     every table: plan_manifest_new.csv (the reconciliation
                           spine), route_stops.csv (stop-by-stop detail),
                           selected_plan_alns.csv, vehicle_routes / utilization,
                           unassigned_jobs, depot_inventory_timeline, trunk_schedule,
                           service_ledger, plan_snapshots (the live plan at EVERY
                           epoch), stop_provenance, churn, micro_passes, traces …
  reports/                      every markdown report: kpi_summary, utilization_summary,
                           alns_summary, cross_depot / option / hub-drop choices,
                           plan_full_dictionary …
```

Where each file is *explained*: this section is the orientation map; `PIPELINE.md` §13
(static) and §13a (dynamic) are the authoritative per-file tables; `plan_full.csv` has
its own column-by-column dictionary generated next to it. `build_phase0` (the spine
build) still writes its `inputs/` folder — planner runs no longer create one.

## Data quality control

### Single sources of truth (RULES.md section E)

| input | THE source | trap |
|---|---|---|
| Orders + responsibility | `freight_planner/data/enriched_orders_2026-01_2026-02.parquet` (COMBINED, the `--qargo` DEFAULT since 2026-07-22) — plain concat of the monthly enriched files, each mirroring its raw universe 1:1 **plus** embedded `verified_leg/confidence/method` columns | the monthly files are BOOKING-month universes: a window in a month's first week silently misses deliveries booked late the prior month (Feb 2: 521 dues live in the Jan file). The combined file MUST be rebuilt after any monthly regen (guard test enforces freshness). Catchment calibrates on the whole input frame, so combined-file plans differ from monthly-file plans; the `--qargo` runner flag was REMOVED (fixed input, user rule) — reproducing pre-2026-07-22 runs means editing `paths.DEFAULT_ENRICHED`. |
| Fleet | `freight_planner/data/vehicle_master.csv`, alone — depot + fleet_kind (from CircuitName), asset identity/class, physical payload/pallets (THE capacity truth), MOT. Telematics behavior columns (shift spans, per-trip capacities, multi-trip stats) REMOVED 2026-07-16 (user rule: not operating constraints — fleet available 06:00, duty/driving caps bound the day) | UNTRACKED — regenerate via `python -m freight_planner.tools.build_vehicle_master`. Vehicles are keyed by RAW AssetName (one reg legitimately contains a space — "P888RNW 2" is a pseudo-reg, don't "fix" it). |
| Every data path | `shared/paths.py::LOGISTICS_ROOT` | a `__file__` grep of `shared/` and `tools/` must return only paths.py. |

### Order load corrections and missing payload fields

The raw order feed remains immutable. During enrichment, three reviewed weight
errors are corrected for planning: WT259833 is converted from 5,991,360 reported
grams to 5,991.360 kg, while WT271534 and WT271550 use the documented 22,432 kg
instead of 320,000 kg. The original value is retained in
`goods_weight_reported`; `goods_weight_correction_reason` records why the
effective `goods_weight` changed. Corrections are keyed by stable order UUID in
`enrich.py`, not by the display WT number.

Orders with zero or missing weight and/or pallet fields remain in the planning
universe. They retain zero for the unavailable capacity dimension and are
reported as missing-payload observations; they are not removed, because the
recorded movement still contributes genuine origin/destination work and road
kilometres.

### verified_leg — the ownership truth

The raw Qargo order says what the customer bought, **not which physical leg our fleet
performed**. `verified_leg` is the per-order, GPS-verified answer (collect / deliver /
end_to_end), produced by matching telematics visits to the order's endpoints — a visit
counts when it is within **500 m GPS radius OR in the same postcode sector** as the
endpoint, and full journeys are verified at **both ends**. Its companion `corrected_flow`
**overrides the raw `api_flow` tag** wherever they disagree: the API label is just the
commercial tag; the verified leg is what physically happened, and it is the planner's
responsibility truth (RULES.md E1).

Rules for reasoning about it:

- **Forward mode output IS the truth.** Don't second-guess it per-vehicle: far/long-haul
  work CAN legitimately be ours. Validate at fleet/depot/order level, never by "does this
  vehicle usually go there".
- **Regen chain** (offline, never concurrently with a planner run):
  1. `python -m freight_planner.tools.verify_legs --qargo <raw monthly parquet>` →
     rewrites `freight_planner/data/verified_legs.csv`;
  2. `python -m freight_planner.build_enriched --qargo <raw monthly parquet> --out
     freight_planner/data/enriched_orders_<YYYY-MM>.parquet` → rebuilds that month's
     enriched file (raw universe + verified columns, must stay 1:1 with the raw file).
     THEN rebuild the combined default file as a plain concat of the two monthly files
     (2026-07-22 — it goes stale otherwise; the
     `test_default_qargo_is_the_combined_cross_month_universe` guard fails loudly if
     you forget);
  3. the planner reads the parquet's embedded columns; `verified_legs.csv` is the regen
     artifact and runtime fallback (the raw qargo parquet does NOT carry the column).
  `tools/diff_verified_legs.py` compares two generations before you overwrite.
- Coverage is ~90% of orders (Jan 91.2% / Feb 90.2%); the residual stays visible as
  `AMBIGUOUS_PARTIAL` accounting rows, excluded from automated dispatch.

### Flow semantics — the naming trap (read before reasoning about legs)

The `NETWORK_*` responsibility shapes name **the leg the PARTNER NETWORK performs; OUR leg
is the OTHER one**:

| raw flow | shape | the network does | **WE do** |
|---|---|---|---|
| `PL_IMPORT` | `NETWORK_IMPORT` | brought the freight into our region (hub) | **DELIVER** (depot → customer, after the inbound hub trunk) |
| `PL_EXPORT` | `NETWORK_EXPORT` | carries it onward beyond the region | **COLLECT** (customer → depot, then depot → hub trunk) |
| `FULL_FLEET` | `FULL_END_TO_END` | — | both legs (direct, or crossdocked via a depot) |
| `LOCAL_COLLECT` / `LOCAL_DELIVER` | `PICKUP_ONLY` / `DELIVERY_ONLY` | the other leg | the named single leg |

`NETWORK_IMPORT` does **not** mean "the network handles it" — mislabelling imports that way
silently dropped 1,146 import deliveries to 0 in the 2026-07-11 regression. Window
membership follows the same logic: collection-anchored flows enter a window by collect
date, delivery-anchored by deliver date, FULL_FLEET by either.

Related distinction: **DIRECT vs XDOCK is about the depot touch**, not about days. DIRECT =
freight stays on one vehicle customer-to-customer (including multi-day sleep-out tours);
XDOCK = freight is handed off through a depot (pickup leg + later delivery leg). A same-day
order can be either; so can a multi-day one.

### Every-run verification gates (must be zero / true — RULES.md F2)

Printed at the end of every run; non-zero is a stop-the-line defect, never a warning:

1. temporal violations = 0 (windows/precedence);
2. ledger violations = 0 (no phantom deliveries — freight must physically be at the depot);
3. every order accounted in the manifest (universe closure: routed / accounted /
   unassigned-with-reason);
4. dynamic runs add the two causality audits — `audit_non_anticipation` (no collection
   arrived-at before its order was booked) and `audit_route_backdating` (no stop planned in
   the past of the epoch that decided it). `FP_STRICT_CAUSALITY=1` promotes both to raises.

### Trap checklist (each cost a real debugging session)

- **Never run off a copied package** — a copied `cambridge/`-era module anchored data at
  `__file__` and silently crippled the vehicle universe. If vehicle-days drop, check the
  import path first.
- **Postcode cache**: failures are cached as *versioned failure markers*; legacy null
  entries mean "retry", not "known-bad". Repairs go through the slot-class postcode repair
  (see `geocode.py`), not hand edits.
- **Odometer is in MILES** in the validation scorecard inputs; ~60/79 regs have usable
  odometer data. Plan-vs-actual is multi-axis — never compare a single day, and quote the
  (vehicle, day)-MATCHED gap, not fleet totals (odometer includes non-order movement).
- **Metric scope discipline**: every km/coverage number has a scope (orders vs legs;
  daily-portion vs +tours vs +trunk; raw vs matched). Name the scope; the authoritative
  table is `experiments/METRICS.md`.
- **Replays of pre-2026-07-14 dynamic runs** pass `--micro-every-min 60` (the cadence
  config changed to 30); pre-convergence-gate static replays pass `--converge-pct 0`.
- `verify_legs` overwrites a planner input — never run it while a planner run is live
  (RULES.md E4).
