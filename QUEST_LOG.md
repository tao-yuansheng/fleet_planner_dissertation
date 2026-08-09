# Freight Planner — Quest Log

Living backlog for the next phase of the dispatcher: big-ticket builds, known
gaps/bugs, and a stage-by-stage logic-validation log we fill as we walk the
pipeline together.

Status legend: `TODO` · `IN PROGRESS` · `BLOCKED` · `DONE` · `WONTFIX`

## Map view — feedback round 2 (2026-07-14) — DONE

- **Board gutter**: ⛺ tour and ⇅ trunk symbols moved OUT of the vehicle-id text into their
  own aligned columns (`TOURX`/`TRUNKX`/`IDX` consts; GUT 112→118) so every id starts at the
  same x. Same ⛺/⇅ indicator column in the map sidebar.
- **Board & Map are now PARALLEL MODES** (was: map = a click-triggered overlay). A segmented
  **Board | Map toggle** lives in BOTH chromes (board head top-right + map bar right, synced
  by `setMode` which show/hides `.wrap` vs `#mapWrap`). Map mode gained a **left vehicle
  sidebar** (`#mapSidebar`, depot-grouped, ⇅/⛺ icons, click to focus without leaving the map,
  highlights the focused, own day-nav ◀▶). `openMap(vid)` (board vehicle click) now just
  `setMode("map")` + focus. Removed the map "Back" button (the toggle replaces it) and the
  hover-strip's popup z-index bug fix stays.
- **Cleaned** the board's verbose subtitle + footer + hint (kept the legend, per request).
- Verified: node 12/12, suite 809, screenshots (board gutter columns, map sidebar with 47
  vehicles + toggle + focus switch, cleaned board). No JS errors.

## Map view — feedback round 1 (2026-07-14) — DONE

User feedback on the shipped map, all addressed (node 12/12, suite 809, screenshot-verified):
- **UI**: Focus button → **iOS toggle switch** (grey=committed / green=internal) top-right,
  left of Back; the **clock centered** at top with **play + stop** icon buttons below it;
  **Back** relabelled + **red**; **Show-other-trucks removed** (state kept, button gone);
  truck marker → **bright cyan HOLLOW ring** (was a filled hue dot, hard to see).
- **Strip hover** now shows the SAME per-stop popup as the board lanes (extracted
  `jobTipHTML`/`placeTip`, shared by both; strip records `stripHits`).
- **BUG #8 direct carries** only stored their DEST coord, so the map drew depot→dest and
  skipped the collect ORIGIN (wrong when origin is far from depot). Fixed: jobs carry
  `clat/clon` (collect), `route_pairs`/`MAPLOGIC.stopCoords` expand a direct to
  origin→dest (both legs baked, geom 694→703), a small hollow **collect marker** is drawn,
  and `committedTimedNodes` threads the simulated truck THROUGH the origin (time split by
  leg length). N888WSM verified: depot→collect(CB22 4PS)→Croydon.
- **BUG #9 commit frontier** the map coloured commitment from the raw snapshot flag (frozen
  at the epoch) while the board uses the live 90-min frontier — so the map lagged. New
  `MAPLOGIC.commitFlags(snap, snapEp, delta, t)` replicates the board's `firm||done` rule;
  the map's committed/internal split, marker fade, count, truck route AND the strip all use
  it now → map and board agree.

## Map view on the dashboard — SHIPPED (2026-07-14) — DONE

Brainstormed → spec (`docs/superpowers/specs/2026-07-14-map-dashboard-design.md`) → plan
(`docs/superpowers/plans/2026-07-14-map-dashboard.md`) → built in 5 TDD phases. Click a
truck's name in the board → full-screen Leaflet map of its route; gantt collapses to a
bottom time-strip.

- **Build side** (`viz_geometry.py`, pytest TDD): `route_pairs` collects every unique
  consecutive coord-leg across all snapshots (depot connectors → home anchor); `bake`
  fetches OSRM road geometry per leg once, disk-caches (`data/Output/osrm_geometry_cache.json`),
  straight-lines on a miss. Wired into `build()` → payload gains `geom` + `depots`;
  `--no-geometry` opts out (board byte-unchanged; the regression guard). Real build: 694
  legs baked. Caught+fixed a lon-0 truthiness bug in the spec (Greenwich-meridian coords).
- **Browser side**: route math in `viz_timeline_maplogic.cjs` (ONE source — inlined into
  the template at build via a MAPLOGIC marker in `render_page`, AND Node-unit-tested
  `tests/freight_planner/maplogic.test.cjs`, 8/8: `legGeom`/`stopCoord`/`routePolyline`/
  `truckPos`/`hasCoord`). Template gained a Leaflet overlay (CartoDB dark tiles), numbered
  leg-type-shaped stop markers, yellow depot pins, a bottom canvas time-strip, and one
  MASTER CLOCK: scrubbing/Play re-forms the route across epochs (verified LL68LZE 6→12
  stops as micros land) and slides a simulated truck puck along the committed route
  (`truckPos` interpolation). Focus: committed⟷internal overlay (solid vs dashed, same hue,
  faded uncommitted stops) + Show-other-trucks faint-fleet lens.
- **Verification**: Node 8/8; full pytest suite 809 (map is additive — 0 py regressions);
  Playwright screenshots of every phase (map route, morph, truck, all 3 overlay states,
  board-intact) — all "no JS errors". Verified via `node`/Playwright (installed this
  session; no repo tooling existed — plan's "screenshot helper" was corrected).
- **Decisions**: local-file + online tiles (not a CSP Artifact); planned-position sim only
  (no telematics); extend the board (not a sibling app); single master clock. The map needs
  internet for tiles; the gantt stays offline-capable. `.cjs` extension because the repo
  root package.json is `type:module`.

## B2 tightened: DEPARTURE-based flooring (WT255677, 2026-07-14 late evening) — DONE

User caught it on the new auto-built smoke board: WT255677 (b66a7567, booked 11:07:53)
first appeared at the 11:30 micro already `committed to driver`, appended as stop 11 of
FJ72XFF's long-launched trip — arrival 13:07 cleared the arrival floor (13:00) by 7 min,
but the truck departs its last committed stop (seq 10) at 12:17, i.e. the DRIVE toward
the new stop begins 47 min after the decision, inside the "frozen" window. User verdict:
flooring arrivals is structurally leaky ("an order 100 min of driving away always
arrives outside the 90-min freeze — but you have to start driving toward it") — floor
the departure.

Fix (TDD, suite 803): `floor_ok`'s partial-commit branch now requires the **deviation
point** — the last committed stop's departure, the first moment the driver's remaining
plan changes — to be ≥ floor. One condition suffices: suffix arrivals ≥ floor already
chain every LATER suffix drive compliant (each starts from a prior stop departing ≥ its
own ≥-floor arrival); the only unguarded segment was the first one. The now-guard
(bu20vhy return-leg rule) is subsumed whenever now ≤ floor; kept as defense in depth.
Single choke point ⇒ every insertion door (ALNS ranked/best/eject, micros, merge sweep)
inherits. Existing test `test_floor_ok_rejects_past_work` re-pinned to the new
semantics + new named test `test_floor_ok_departure_based_flooring_rejects_drive_inside
_the_window`. Net behavior: a launched trip is top-uppable only while ≥ Δ of ground
remains before the insertion point — same-day suffix top-ups get rarer, more late
bookings go to fresh trips (depot departure ≥ floor, unchanged rule) or slip; that is
the intended cost. Docs: RULES B2/A3/known-gaps, README_DYNAMIC §3/§8, PIPELINE §13a.
**Verified (runs_deptfloor, same 2-day window):** forensic snapshot sweep
(scratchpad check_deviation_floor.py: every stop first appearing on a standing trip must
have predecessor-departure AND arrival >= epoch+90) — pre-fix control run: **5**
violations of exactly this class (WT255677/FJ72XFF + BU71TYW x2, M888 WSM, R888GNW — all
deviation-only, every arrival cleared: the leak was structural); fixed run: **0** of 476
suffix insertions. WT255677 relocated exactly as prescribed: same 11:30 micro, now a
FRESH trip (AY18JWA trip 2) departing the depot 13:15 >= floor 13:00, arrive 13:49 —
still served same-day. Ledger outcome IDENTICAL (ON_TIME 449 / UNSERVED 4): zero
coverage cost on this window — all five leaky insertions found legal homes. All four
audits 0.

## Run-folder restructure + auto plan_full/timeline + rolling converge flags (2026-07-14) — DONE

User-commissioned output overhaul (suite 786→802, all green):

- **New run layout** (`output_layout.py`): root = `run_manifest.json`, AUTO `plan_full.csv`,
  `runsheets.html`, dynamic `timeline.html`, `alns_progress.log`, `handover.json`,
  `validation_metrics.json`, `rolling_manifest.json`; ALL tables → `csv/`; ALL markdown →
  `reports/`. `inputs/` no longer created by planner runs (only `build_phase0` writes it — it
  keeps its own). Mechanism: `RunPaths` extension-router returned by `run_dirs` — writers
  kept their `dir / "name.ext"` joins untouched (one choke point, ~0 write-site edits);
  `RunPaths.parent` = the window dir so legacy `plan_dir.parent` idioms hold.
- **Dual-layout readers**: `find_artifact`/`artifact_dir` resolve root/csv/md THEN legacy
  plan/reports — run_month handover chain, month_summary, viz_app, viz_map,
  viz_timeline_build all read BOTH (verified on runs_rules: timeline 6 days/263 lanes,
  viz_app 282 trips). Old runs stay viewable; new-layout default html outputs land at the
  run root.
- **plan_full.csv is now AUTO** at the end of EVERY run (static + dynamic;
  `plan_full.emit_plan_full` in `emit_outputs`, loud-warn non-fatal) at the run ROOT —
  works on dynamic runs unchanged (reads the same manifest/route_stops contract; the
  rolling run_manifest carries qargo/start/end). Dictionary → `reports/`.
- **timeline.html AUTO** on dynamic finish (`_emit_timeline` →
  `viz_timeline_build.write_dashboard`, extracted callable + `render_page`); built BEFORE
  the strict audit (the board is a forensic surface — it caught WT255038) and non-fatal.
- **runsheets.html confirmed usable on dynamic runs** (built from route_stops; runs_rules'
  is 582KB real content) — now a root deliverable.
- **`--converge-pct/-window/-min-iters` added to run_rolling** (`_parse_rolling_args` +
  `_solver_base` extracted, TDD): the convergence gate was config-only in dynamic runs;
  now per-run adjustable and recorded in run_manifest. Docs: README "Anatomy of a run
  folder" (the per-file orientation map), README_DYNAMIC (gate paragraph + new outputs),
  README_STATIC, PIPELINE §0/§13/§13a/§15.

## Docs restructured: README split into guide + two deep-dives (2026-07-14) — DONE

The old README.md (design rationale + Q1–Q11 decision log) is now **`DESIGN_LOG.md`**
(frozen history; header notes the rename). New doc set: **`README.md`** = guide only
(setup, folder layout, run commands, data quality — verified_leg regen chain + the
flow-responsibility naming trap front and centre); **`README_STATIC.md`** = the static
ALNS planner narrative (module map, data pipeline, staged seed, the search, outputs);
**`README_DYNAMIC.md`** = the rolling dispatcher narrative (decision grid, visibility,
commitment/watermarks, guard stack, RULES.md family summary, board, full CLI incl. the
`--strict` degenerate-floor vs `FP_STRICT_CAUSALITY` distinction). PIPELINE.md stays the
code-verified reference (cross-refs updated); RULES.md stays the invariant contract (now
points to README_DYNAMIC as its narrative companion). Historical "README" mentions in
this log refer to today's DESIGN_LOG.md. Found while writing the guide: `pyarrow` is
required for parquet IO but missing from requirements.txt (noted in README setup).

## Route-backdating FIXED same day + micro cadence 30 min (2026-07-14 evening) — DONE

User caught it on the board (WT255038's stop retimed into the noon anchor's past) and
commissioned the fix. Root cause was NOT the suspected stale context:

- **The merge sweep was the one unguarded insertion path.** The noon warm anchor's
  post-ALNS same-address sweep (`apply_zero_cost_merges`) top-upped WT255131/81a2457f
  onto WT255038/bd5beb0c's MK41 0LF stop inside a FULLY-DEPARTED trip — no watermark
  min-position, no floor, no now-guard (every ALNS insertion path had correctly refused;
  the sweep's "top-up into pinned trips is allowed" doctrine was the door). Fix: the
  sweep takes `watermarks`/`commit_floor`/`now`, inserts only after the watermark, and
  the merged day must clear `floor_ok` (census bucket FLOOR). Static calls (kwargs None)
  byte-identical.
- **Past-day placement (3 seed cases): two layers.** `_clamp_past_candidates` drops
  candidates dated before the epoch's day from every rolling solve (committed legs
  exempt; `ns.min_service_day`); `_floor_guard_active` broadens the ALNS floor guard to
  any today-or-past key even WITHOUT a watermark entry (a never-launched past key used
  to disarm the guard by absence).
- **Verification (strict mode, rc=0):** `route-backdating audit: 0 violations` (was 4);
  day-clamp visibly intercepting 1→12 stale legs per anchor; plan and ledger now agree
  on all four orders — 81a2457f served honestly SAME DAY at 16:06 (S888GNW), f814feb2
  genuinely ON_TIME, fd3187a5/f9e4fe38 honestly UNSERVED and absent from the plan.
  Buggy run archived: runs_archive/runs_rules_backdating_bug (forensics:
  reports/stop_provenance.csv). Suite 786 green.
- **Micro cadence is a config knob:** `MICRO_EVERY_MIN = 30` default (was hard 60 via
  CLI). A micro pass costs ~2 s wall (52 passes ≈ 100 s over a 6-day run; anchors are
  95%+ of wall time), so cadence is a service-level choice: 30 min halves a new
  booking's wait for its first insertion attempt. CLI `--micro-every-min` overrides;
  pre-change replays MUST pass 60. Board: epoch labels thin out automatically when the
  cadence packs them. Fixed smoke ran at 30 (105 micro passes, ~51 min wall).

## RULES.md gap-closure pack (2026-07-14) — DONE, zero open gaps

User reviewed the five known gaps and commissioned fixes; all five closed TDD:

- **Gap 1 (A4 route-level audit):** `audit_route_backdating` at every dynamic finalize —
  tour rows' `service_date` >= their creating seed's day (`tour_created_at` stamped per
  seed), daily stops' `planned_arrive` >= first-placement epoch (the `placement` trace).
  Proof-of-fire: flags i5000's pre-Fix-8 backdated tour (2 rows). `FP_STRICT_CAUSALITY`
  raises, same as the order-level audit.
- **Gap 2 (hash-seed byte-stability) DISPROVED:** controlled A/B, PYTHONHASHSEED 0 vs 7
  (OSRM cache snapshot/restored, iteration-bound): inputs/seed/400-iter probes AND
  2,000-iteration full-CLI static runs → every plan artifact byte-identical (only
  wall-clock timestamps differ in run_manifest/progress log). The gate-era instability
  was the wall-clock budget + cache mutation, misattributed. Pin kept as free
  defense-in-depth (rolling path not separately A/B'd).
- **Gap 3 RECLASSIFIED (intentional → rule B7):** tours are born only at 03:00 seeds;
  micros may only attach to an existing tour's un-departed tail without adding tour days
  (`TOUR_ATTACH_ENABLED`, still flag-off). The Jan-15 case for the record: 9118b3ed was
  booked Jan-13 15:05 / due Jan-14 (missed via NO_FEASIBLE_ROUTE, then backdated by the
  pre-fix tour — i5000's ledger says UNSERVED while its route_stops shows a Jan-15
  delivery, an inconsistency only possible pre-Fix-8); 78897baf was booked Jan-15 09:22
  and waited for the Jan-16 seed BY DESIGN (SLIPPED 1).
- **Gap 4 (driver-day bounds):** the open suffix is bounded — new
  `MAX_DUTY_H_PER_DAY = 13.0` clamps shift windows at vehicle-state build
  (`vehicles._clamp_shift_end`): X88GNW / Y888GNW / Y88RNW carried 14.1-15.1h
  telematics windows (vehicle spans under trunk driver swaps, not one driver's day).
  10h driving was already enforced across trips (`evaluate_day` DRIVING_CAP +
  `duty_after_freeze`). BEHAVIOR CHANGE for those 3 CB22 tractors (evening availability
  shrinks to start+13h). Disruption factors stay 0 for now (user: trial later).
- **Gap 5 (trunk = real vehicle assignment):** `draw_tractors` stamps each night with
  `vehicles` (picked, draw-preference order) + `feasible` (eligible pool);
  `trunk_schedule.csv` exports both (`trunk_schedule_frame`); `day_close_trunk` now
  returns the DRAWN nights (was the raw pre-draw schedule — rolling emitted nameless
  rows). Board: named trips render as ⇅ teal blocks on the assigned tractor's own lane
  (lane created if the tractor was otherwise idle); the separate trunk section is now
  only a fallback for unassigned/legacy trips (i5000 verified rendering via fallback).

**Verification smoke `runs_rules` (12-17, 5000-cap, seed 0; ran twice, deterministic):**
89.3% assigned, 47,893 km, 12 tours ALL frozen, 0 tour-backdating rows (Fixes 8+9 at
scale), 0 temporal / 0 ledger violations, every trunk night names its tractors (6/night
from 9-25 feasible), board fallback section empty. Board republished (artifact 77172d5f).

**NEW FINDING — the gate caught a real leak on its first full run (`TODO`, now the one
open RULES.md gap):** 4 daily stops planned in the PAST of their deciding epoch (all
late-booked same-day collections, ~0.2% of stops; order-level audit blind to all four).
Forensics (reports/stop_provenance.csv): 3× a next-day 03:00 seed placed a still-unserved
due-yesterday candidate onto YESTERDAY's vehicle-day (e.g. fd3187a5 booked 01-13 17:14,
after the day's last micro, placed by the 01-14 seed on an 01-13 route; past-day keys
never launch so emission re-times them from raw morning) — daily work needs the Fix-8
day clamp tours got. 1× the noon warm anchor added work to a partially-launched key
under a 13:30 floor and emission used the key's LAUNCH ctx (11:34 arrive off a 07:00
start) — commit ctx must become per-TRIP (B4 refinement). Fix deliberately NOT rushed
tonight: solver-core changes, own TDD session. `audit_route_backdating` now runs LAST
in finalize so a strict raise can never abort the forensic outputs again.

## CAMPAIGN LOG — E5 complete, headline corrected, depth check in flight (2026-07-07)

**E5 (anytime/budget sweep) COMPLETE** — 5 seeds × 20,000 iters, reference config,
Jan 12-17: cost **11,434 ± 186** (best 11,208; seed 0 — used by ALL prior sweeps — is
the WORST of 5, so prior quotes were conservative). Coverage 2,315/2,317 invariant across
every seed+depth (pre-registered "compute buys efficiency not coverage" CONFIRMED).
Steep region (>0.5%/1k rule) ends iter ~9,800-15,000 ⇒ **N=2,500 ablation depth is
INSIDE it** ⇒ depth-robustness check upgraded to 2,500-vs-20,000. it/s wobbled 4.8-7.0
overnight (±20%) ⇒ iteration-primary protocol is NECESSARY, not cosmetic. Traces:
experiments/E5_sweep_traces/; figure e5_convergence.png; 5 rows in results.csv.

**HEADLINE CORRECTED (stakeholder review caught it):** "−9% vs odometer" was NOT
like-for-like — raw odometer includes non-order km (the reverse hole: workshop/MOT/fuel/
empty repositioning). Like-for-like = (vehicle,day)-MATCHED gap (week_reality machinery):
**matched mean −5.2% ± 0.7 (best −5.8%)** vs naive −9.1% ± 0.8 (context-only, labelled
flattering). ~4 points of the old headline were reverse-hole inflation. Script:
experiments/analysis/e1_like_for_like.py. Residual caveat: intra-day non-order km still
in matched pairs ⇒ reverse-hole characterization now directly strengthens E1.

**Metric scopes unified:** experiments/METRICS.md — one headline per concept, all
cross-file discrepancies resolved to named scopes (trace served=daily LEGS 2,482 vs
coverage_served=whole-plan ORDERS 2,315; alns_km=daily-only vs planned_km=+tours vs
combined=+trunk; two vehicle-day countings). Provenance rule: only git 70d5253a rows
citable. Pre-registered E3 expectation: SA near-inert (traces show cost==best_cost at
every checkpoint), sa_off delta ≈ 0 statistically (NOT bit-identical — rng stream shifts).

**Depth check (minimal) IN FLIGHT:** stakeholder trimmed 5→2 configs — only the true
phase-risk ablations (drop_shaw, destroy_random_only) × seeds 0-2 @20k WITH traces
(⇒ full delta-vs-depth curve vs E5 reference; ~6h). SA/uniform/regret stay at cheap
depth (mechanism-not-phase effects, honestly stated). First checkpoint diverged from
reference (173/16,942 vs 167/16,908) = toggle provably live. Verdict machinery
PRE-BUILT with pre-stated FLIP criteria (analysis/depth_verdict.py: F1 sign-flip
beyond 0.3% noise floor, F2 inert-early/material-late ≥1%). E3 main batch orchestrator
PRE-WIRED (E3_ablations/run_e3_main.py: 12 configs × 10 seeds × 3 frozen-handover
windows @2,500 ≈ 57h full; --configs/--seeds/--windows trim levers; per-run live
collection into results.csv). Chain: deep arm lands → depth_verdict → if HOLDS,
launch E3 main.

## SHIPPED — Ch.5 experiment instrumentation + E0 pilot (2026-07-06)

Paper-experiments campaign home: `freight_planner/experiments/` (GITIGNORED — .gitignore:45;
README pins windows/reference-config/protocol; results.csv = single metrics ledger).
Restore mechanism per stakeholder requirement: `experiments/code_snapshots/`
(pre-instrumentation byte copies + RESTORE.md + instrumentation.diff, 95 added lines).

**Instrumentation (plan docs/superpowers/plans/2026-07-06-experiment-instrumentation.md,
TDD, 522 tests):** all ENV-gated, DEFAULT-OFF, in alns.py + run_alns.py:
`FP_ALNS_REMOVAL_MIN/MAX` (destroy band, was hardcoded 2..5), `FP_ALNS_DESTROY_OPS`
(operator restriction), `FP_ALNS_UNIFORM_WEIGHTS` (adaptive-weights off),
`FP_ALNS_ACCEPT=rrt` + `FP_ALNS_RRT_DEVIATION` (record-to-record acceptance;
sa default preserves EXACT rng consumption), `FP_ALNS_TRACE=<csv>` (anytime curve:
elapsed_s,iteration,accepted,cost,best_cost,served; zero hot-loop I/O — rows buffered,
written post-loop), manifest `env_toggles` provenance. **All-defaults proven
BIT-IDENTICAL** (fingerprint 167/16,908@200, 707/14,583@1000).

**E0 pilot results:** (1) incumbent extraction GO — `experiments/analysis/incumbent_metrics.py`
pulls reality's odo-km/veh-days for all 4 campaign windows, cross-checks scorecard +
month_summary (Jan12-17: 89,556 km / 317 vd); (2) toggle smoke ALL PASS (6 stub runs,
manifests capture env exactly, trace writes); (3) runtime: ~5.5 it/s, ablation run @N=5,000
≈ 17.5 min → full E3 ≈ 96h, trimmed N=2,500 ≈ 44h; E5 = 5 deep traces ≈ 5.2h.
**OPEN:** E0.2 OR-Tools spike awaits pip-install decision; N trim decision (5,000 vs 2,500).

## SHIPPED (dormant) — K2 v1 earlier-only day-flex + population finding (2026-07-06)

Spec/plan: `docs/superpowers/*/2026-07-06-k2-dayflex*`. Stakeholder-scoped: FULL_FLEET
XDOCK-shaped delivery legs only; EARLIER-only within `[freight-ready → historical due]`
(never later — bulletproof fairness, no service-promise model needed); 2-day cap;
`window_start` respected when present. Mechanism = approach A: `JobMeta.flex_variants`
(per-day window-transplanted RouteJob variants, `dayflex.shifted_route_job`), both ALNS
insertion fns loop `allowed days × vehicles`; seed/tours/shuttle/trunk untouched.

**Shipped:** `freight_planner/dayflex.py` (resolver + transplant + stats), `day_flex_min`
col on candidate jobs, `--day-flex` flag on run_alns/run_month (default OFF, in manifest),
`due_date`/`days_early` audit cols on route_stops (ALWAYS on — they also expose tour
early-serves), K2 block in kpi_summary when flag on. **506 tests** (20 new, TDD).
**Flag-off proven BIT-IDENTICAL** (checkpoint fingerprint incl. accepted-move counts:
167@200 / 707@1000, cost 16,908 / 14,583 — exact match to the sweep runs).

**A/B Jan 12-17 @120s (same seed/handover; conserve-mode clean, 0 violations):** km-NEUTRAL
(85,925 vs 85,939), coverage identical — because **eligible = 2/2,317**. 1800s leg skipped
(nothing to exploit).

**POPULATION FINDING (the real result):** 316 FF deliveries/wk dwell ≥1 day in depot
(201×1d, 78×3d, 26×4d…) — the load-building population EXISTS — but **316/316 carry
`raw_window_start` exactly == the due date**, so the window guard pins them all. A window
field that always repeats the scheduled date looks like a TMS stamp, not a customer booking
(same species as the collection-time hindsight fix). Relaxing stamp-dates ⇒ **270 eligible/wk
(~12% of orders)**. PL_IMPORT is NOT a population: 1,121 deliveries all dwell=0 by data
construction (import ready derived same-day) — the import extension dies cleanly.

**DECISION (stakeholder 2026-07-06): keep v1 DORMANT** — flag stays, default off, zero risk.
**Reopen path (v1.1):** treat `window_start == due date` as a non-binding stamp (differing
dates stay respected), rerun the A/B on the 270-job population — pending a window-provenance
check with ops (customer-booked vs system-stamped?).

## FINDING — ALNS budget sweep to 3600s: plateau confirmed, 1800s is the operating point (2026-07-06)

Controlled 4-point budget sweep on **Jan 12-17** (chained handover from Jan 5-10; `seed_km`
75,369.7 identical across ALL FOUR runs, so inputs are provably the same — only ALNS budget
varies). Runs live side-by-side: `runs/` (120s month baseline), `runs_archive/runs_exp_viz/` (5,200-iter cap,
818s), `runs_archive/runs_exp_1800s/`, `runs_archive/runs_exp_3600s/` (both time-budgeted, no iteration cap,
`--no-improve 4000` gate armed).

| budget | ALNS wall | iters | planned km | combined | daily vd | cost | vs odo 89,571 |
|---|---|---|---|---|---|---|---|
| 120s | 123s | — | 85,939 | 95,655 | 297 | 15,330 | +6.8% |
| 818s (5.2k cap) | 818s | 5,200 | 75,457 | 85,173 | 275 | 12,464 | -4.9% |
| 1800s | 1,802s | 13,200 | 73,175 | 82,892 | 260 | 11,752 | -7.5% |
| 3600s | 3,603s | 19,400 | 72,748 | 82,465 | 261 | 11,664 | -7.9% |

Coverage 2,315/2,317 (99.9%) and 0 violations at every budget — all gains are pure efficiency.

**Key results:**
- **Marginal efficiency collapses:** 15.1 → 2.3 → 0.24 planned-km saved per extra ALNS-second.
  Planned-km steps -12.2% / -3.0% / **-0.58%**. Doubling 1800→3600 buys almost nothing (and adds
  a vehicle-day, 260→261).
- **Convergence (answers "will a heuristic ever converge?"):** practically YES, formally NO.
  3600s run sat flat at cost 11,667 for ~600 iterations then found a 3-unit improvement — the
  micro-improvements keep resetting the 4,000-stale gate, so it never trips even on the plateau.
  ALNS gives a high-quality feasible solution, never a certificate; report the *convergence
  curve* (marginal km/s), not "converged".
- **Deep-budget scorecard vs telematics (per veh-day, 1800s ≈ 3600s — conclusion is
  budget-stable):** fleet 291-292 vs actual 317 (**-8%**), speed 51.5 vs 52.0 (**-1%**,
  calibrated), stops 8.7 vs ~8-9 net delivery dwells (parity), combined km **-7.4% / -7.9%** vs
  odometer. Matched (veh,day)-paired daily km -27% (directional only — excludes trunk on the plan
  side).
- **For the paper:** plan km is an *achievable upper bound* (true optimum ≤ it), so
  "plan beats reality by ~7-8% total km with 8% fewer vehicle-days at 99.9% coverage" is
  conservative — more compute can only widen it.

**Decision: 1800s is the re-baseline budget** (past the knee; 3600s = 2× compute for -0.58%).
**Next:** re-run the 5-window January chain at `--time-budget 1800` for the month-wide headline
(current month numbers are under-optimized 120s plans).

## FINDING — multi-axis plan-vs-actual validation (2026-07-06)

Beyond distance, the telematics GPS trace lets us validate the plan on independent axes. Scorecard
for **Jan 12-17, 6-day window, per vehicle-day** (scratchpad `plan_vs_actual_scorecard.py`):

| axis (per veh-day) | ACTUAL (telematics) | PLAN 120s | PLAN optimized |
| vehicle-days (total) | 317 | 327 (+3%) | 306 (-3%) |
| stops / veh-day | 11.0 | 7.7 | 8.3 |
| drive/move hours / veh-day | 5.19 | 4.64 | 4.28 |
| avg speed km/h | **52.0** | 51.4 | 51.5 |
| km / veh-day | 282.5 | 238.0¹ | 221.3¹ |

**Key results:**
- **Speed CALIBRATED** — planner assumes `AVG_SPEED_KMH=50`; reality averages **52 km/h** moving (~4%).
  Validates the whole time model (drive times -> windows -> feasibility). Strongest new figure.
- **Fleet size MATCHED** — plan within **±3%** of the vehicle-days reality used (327/306 vs 317);
  optimization consolidates (327->306).
- **Drive time tighter** — plan drives 4.3-4.6h vs reality 5.2h moving (efficiency, consistent w/ less km).
- **Stops roughly comparable** — actual 11.0 counts ALL dwells >=5min off-depot (incl. breaks/traffic);
  net of ~2-3 non-delivery dwells, real delivery stops ~8-9 ~= plan 7.7-8.3.

**Caveats / gaps:**
- ¹ km/veh-day plan is 6-day-window-only (excludes multi-day tour tails on Jan18-21), so understates plan
  km slightly; the clean km number is the combined-vs-odometer matched gap (thread a / [[alns-budget-limited]]).
- **Duty-hours axis BROKEN** — `route_stops.csv` emits NO timing on `depot_start`/`depot_return` rows
  (only customer stops), so depot-to-depot duty can't be computed (customer-span gave duty<drive, impossible).
  Reality's active span is 10.7h/vd with only 5.2h moving (~5.5h idle/stops). **Follow-up:** emit depot
  timings to open this axis.

**Takeaway for the paper:** validation is now multi-dimensional — the planner **matches reality on fleet
size + speed** (calibrated) and **beats it on distance (-5 to -7% matched) + drive-time** (efficient), each
backed by an independent telematics-derived figure. Follow-ups: (a) emit depot timings for duty-hours;
(b) run the scorecard across the whole month for 4-week support.

## DONE — viz surfaces postcode recoveries (2026-07-05)

`viz_app.py` trip map now shows a "Postcode recoveries" panel (bottom-left) so silently-recovered
postcodes are verifiable, not invisible. Viz-side only — classifies each planned stop from the cache
entry's `source`/`precision` (repaired / outcode-fallback / terminated), whole-plan, tiered by risk
(outcode+repaired "needs check", terminated "retired units, low risk"). No pipeline change / re-run.
Jan12-17: 4 outcode + 2 repaired (e.g. `AL10 9B5→AL10 9BS`, `MK43 OYL→MK43 0YL`) + 22 terminated.
Spec/plan `docs/superpowers/*/2026-07-05-postcode-recovery-panel*`. FC aliases out of scope (curated).
Also this session: **road_km memo** (route_costs.py, +23.8% ALNS throughput, determinism-proven) +
**ALNS budget-limited finding** (120s→85,939 / 1200s→75,299 km on Jan12-17, still improving at 16k iters)
— see memory `alns-budget-limited`. 486 tests.

## DONE — monthly run structure + January backtest (2026-07-04)

Flattened output to `runs/<YYYY-MM>/<window>/{inputs,plan,reports}` (was 5 scattered `out*` dirs
4-levels-deep under a constant `forward_structural/planning_window/`). `DEFAULT_OUT_DIR` `out`→`runs`;
mode/basis now in `run_manifest.json`, suffixed onto the window folder only when non-default; historical
`out*` untouched. New `run_month.py` (handover-chained orchestrator over a window list; per-week
`viz_app --validate`; `--summary-only` rebuilds the rollup) + `month_summary.py` (KM table + odometer +
honest matched gap + handover-continuity). Ran ALL January (5 windows, Jan1-3 stub cold→4 weeks):
**handover chain verified — all 4 hops `match ✓`**; month-total honest matched gap **−0.8% (parity)**;
per-week matched −0.1/+1.2/+5.1% for the full weeks. Spec/plan `docs/superpowers/*/2026-07-04-monthly-run-structure*`.
478 tests. See memory `monthly-run-structure-shipped`.

**Open (deferred):** thread (b) — characterize the REVERSE HOLE (our fleet drove 70k/55k km in
telematics that the plan doesn't cover: genuinely out-of-scope subcontract/local, or under-utilization?).

## BLOCKED — massive-load service needs a double-deck TRAILER assumption (2026-07-03)

The real motivation behind the leg re-validation is to serve MASSIVE orders (`is_massive`: >26 pal or
>28t → `MASSIVE_UNSUPPORTED`, the last coverage blocker). Investigation (Jan+Feb, 57 massive orders):

- **93% (53/57) fit ONE double-deck artic** (≤52 pal AND ≤28t) — 47 are 27-34 pal at ~27.8t, OVER
  single-deck pallet space (26) but UNDER 28t weight. Telematics: **28 of 39 tracked delivered by a
  SINGLE vehicle** (WT254741 = N8GNW alone with 34 pal). They are single **double-deck** loads, NOT
  two-vehicle splits. `_split_parts` (legs.py:160, `{order}#S{i}of{n}`) is wired to FULL_FLEET only.
- Two **96-pal** orders (WT259833/WT260483) = a LOCAL SHUTTLE (PE19↔PE19 ~3.8km, one tractor N88GNW,
  garbage weight 6,000t / 0) — a shuttle, not a split. **>28t "weight" cases are data errors** (320t).

**BLOCKER:** capacity is split across assets we half-have. A **tractor couples to any trailer**;
**weight (44t GVW → ~28t payload) is tractor-bound and always holds**, but **pallet capacity (26
single-deck vs 52 double-deck) is a TRAILER property**. `freight_planner/data/vehicle_master.csv` has
ONLY tractors (45 Tractor Units, ALL nominal `pallet_capacity=26`) + rigids/vans — **no trailers, no
double-deck row anywhere**. Can't read which loads were double-deck; can't give every artic 52 pal
because double-deck trailers are a LIMITED (unknown) pool. **Cannot model massive-load service without
the fleet's double-deck trailer count / assumption — STOPPED here pending that.**

Next when unblocked: get the trailer list from stakeholder OR estimate the double-deck count from
telematics (max concurrent >26-pal loads/day = lower bound); then keep the 28t per-vehicle ceiling, add
a rationed 52-pal double-deck option, route 96-pal locals via the shuttle path, sanitise bad weights.

## SHIPPED (partial) — DIRECT consolidation in tour building (2026-07-04): STRUCTURAL win, km-NEUTRAL

Spec `docs/superpowers/specs/2026-07-04-direct-consolidation-in-tours-design.md`, plan
`docs/superpowers/plans/2026-07-04-direct-consolidation-in-tours.md`. 464 tests.

**OUTCOME: structural goal met, but it is NOT the km lever.** WT255892 now rides a shared 4-order
X90GNW sweep (was a dedicated X888RNW run); wk1 has **0 dedicated far-direct tours** (all 9 consolidated),
wk2 has 5 dedicated (km-guard correctly left them — folding wouldn't help). BUT plan km did NOT drop:
wk1 93,622->95,463 (+2%), wk2 84,823->84,859 (flat), combined +1.1% — all within ALNS ±2-4% variance.
Coverage held 99.9/100%, 0 violations. So: matches reality's X8RNW pattern, km-neutral (km-guarded, not a
regression), but the **+15% overshoot is NOT closed** — the km driver lives elsewhere. User (2026-07-04)
chose KEEP + hunt the km lever next.

**Task 2 (salvage exclusion) was DROPPED during implementation** — dropping it regressed delivery
re-merges: a same-day far-origin DIRECT poisons the salvage re-pool (its backtrack pickup makes the
whole consolidation LATE-infeasible, and `_per_depot` then fragments the deliveries that WOULD merge
without it). Task 1 (main-pass km-guarded consolidation) is the whole fix; the salvage two-point
exclusion STAYS. Only `tours.py::resolve_cluster` changed (gate -> `_keep_or_split` guard).

**NEXT — HUNT THE KM OVERSHOOT LEVER (open):** restructuring the seed (consolidating directs) did NOT
move fleet km, so the +15% overshoot vs odometer is driven by something else. Candidates to investigate:
(a) the resolver's XDOCK-vs-DIRECT split / `1.6` ratio (far multi-day -> XDOCK depot backtracks);
(b) ALNS already consolidates efficiently regardless of seed structure (so seed changes wash out);
(c) per-order km inflation from FULL_FLEET modelling (each full journey planned even when reality
piggybacks). Measure with a controlled (fixed-seed) before/after to beat the ±2-4% ALNS noise.

---

## (superseded) DESIGNED — DIRECT consolidation (2026-07-04) — see SHIPPED above

Spec: `docs/superpowers/specs/2026-07-04-direct-consolidation-in-tours-design.md`. Addresses the
**FULL_FLEET consolidation gap** (the wk1 +15.4%-vs-odometer overshoot from
[[verified-leg-postcode-match-fix]]).

**Problem:** far full-fleet orders reality serves as ONE consolidated multi-drop sweep get split by
our planner into a dedicated DIRECT run + a separate delivery tour. Concrete: real Scotland run was
ONE vehicle X8RNW (Jan 14-15, KY11+KA1+KA6+ML6); our plan used TWO — X888RNW (WT255892 Stoke->KY11 as
a standalone DIRECT) + W88RNW (the 3 as an XDOCK tour).

**Root cause (a deliberate exclusion, NOT a physics gap):** the tour engine already carries freight
on board (`evaluate_tour` running peak), a two-point DIRECT job already self-drives `prev->origin->dest`
(`_leg_km` tours.py:167), and multi-depot load-stops already work. The ONLY blocker is a policy gate in
`resolve_cluster` (tours.py:514): any DIRECT whose origin isn't AT a depot forces the whole cluster to
`_per_depot()` (strands it dedicated), + the salvage pass excludes two-point moves ("they poison
consolidation", tour_plan.py:334).

**Design (tour-builder ONLY, resolver untouched):** (1) replace the DIRECT-origin exclusion in
`resolve_cluster` with a guarded consolidation — build the tour INCLUDING the DIRECT(s), keep it iff
feasible AND `total_km <= sum(_per_depot() km)`, else fall back (so `_per_depot()` IS the km_split
baseline; off-corridor origins fall back automatically; coverage can't drop). Triggers ONLY for
clusters with a non-depot DIRECT — pure-delivery clusters unchanged. (2) drop the salvage two-point
exclusion so stranded on-corridor directs re-pool.

**Resolver stays as-is (incl. `DEFAULT_XDOCK_RATIO=1.6` + `_window_infeasible`):** those were only
harmful because DIRECT meant *dedicated*; once DIRECT consolidates, same-day->DIRECT rides a sweep
(correct) and far->DIRECT feeds line-haul (correct). DEFERRED: whether far *multi-day* orders the 1.6
ratio sends to XDOCK (depot backtrack) would be better as consolidated directs — measure the tour fix
first. Validation target: WT255892 rides a shared Scotland sweep, wk1 km DOWN toward odometer 89,571,
coverage held 99.9/100%. Brainstormed w/ user 2026-07-04 (chose "same-day consolidated line-haul";
key user insight: the depot-load-in-sweep already works, only DIRECT was excluded).

## Session log — 2026-07-04b (verified-leg postcode-match fix: WT253752 collection recovered)

**Trigger:** user flagged **WT253752** (SEALITE MK43 0UT Husborne Crawley -> ACEC DIST Dublin IE) stamped
DELIVERY but it's our COLLECTION (origin in our patch, collected by our own rigid S888RNW 15:44 Jan 8;
Dublin is Palletline's export). api PL_IMPORT is a data-import label, not freight direction.

**Root cause (two compounding failures):** (1) S888RNW was **stopped at Location_Postcode=MK43 0UT** (== the
order origin) at 15:44, but that ping is **~800m from the cached postcode CENTROID** -> outside the 500m
GPS radius; `_stopped_at`'s postcode-string fallback only ran when coords were MISSING, so the exact
postcode agreement was never used. (2) Destination "DUBLIN" (Ireland) is un-geocodable -> `_distance_pred`
= None -> the structural fallback used `direction = api_pred` = PL_IMPORT -> DELIVERY.

**Fix:** `_stopped_at` now matches if `GPS<=500m` **OR** `_pc_matches(order_pc, ping_pc)` (sector) even when
coords exist. ASSIGNED-VEHICLE-ONLY (`_any_fleet_at`/substitutes unchanged, stays distance-gated ->
WT254741 anti-fabrication preserved). TDD: `test_stopped_at_matches_far_gps_when_postcode_agrees`; 23 tests.

**Blast radius (measured, then confirmed on Jan+Feb regen): 455 legs flip (2.34%), ALL -> telematics HIGH.**
FULL_FLEET 2500 -> 2882 (+382); telematics-verified 91%. ~23 one-end DIRECTION flips (WT253752 class,
km-neutral); ~407 one-end -> two-end FULL_FLEET promotions; ~25 flow_full_fleet(LOW) -> one-end(HIGH).
Regenerated verified_legs.csv (snapshot `verified_legs.before_pcfix.csv`) + rebuilt enriched.

**STAKEHOLDER DECISION (user): KEEP THE FULL FIX** despite km cost. FULL_FLEET orders are modelled as full
journeys and the planner consolidates them less tightly than the real groupage -> plan km overshoots.
Handover-chain re-run (runs_archive/{out_wk1_ho,out_wk2_ho}, corrected legs): wk1 combined 95,669 (+6.8%) -> **103,338
(+15.4%)** vs odo 89,571; wk2 90,917 (-2.0%) -> 94,780 (+2.1%) vs odo 92,789; combined +2.3% -> **+8.6%**.
Coverage HELD 99.9/100%, 0 violations. **NEXT LEVER: planner consolidation of FULL_FLEET orders** (the
overshoot source; deferred open question from the two-end-only work, now the top km lever). Not taken:
"split" fix (postcode -> DIRECTION only, GPS required for FULL_FLEET) — needs a classify_leg per-end refactor.

## Session log — 2026-07-04 (T2 week-to-week state handover SHIPPED)

**Goal:** close the run-to-run state gap — each weekly run bootstrapped fresh (all vehicles home
Monday, freight state re-derived), so a multi-day tour still out at the week boundary and its
already-delivered spill orders were invisible to the next week. Brainstorm → spec
(`docs/superpowers/specs/2026-07-04-week-to-week-handover-design.md`) → plan
(`docs/superpowers/plans/2026-07-04-week-to-week-handover.md`) → 6 TDD tasks via subagents.

**Model (user-decided):** ROLLING SIM (wk N+1 opens from wk N's PLAN end-state, not independent
history) + WHOLE-TOUR OWNERSHIP (a tour that starts in wk N is owned end-to-end by wk N even if it
delivers Mon of wk N+1; tours stay atomic).

**Build:** new `freight_planner/handover.py` — `build_handover(selected_df, demand_df, start, end)`
derives from the plan's FINAL freight state: `delivered_order_ids`, `vehicle_availability` (last job
ends after Saturday), `staged_freight` (AT_DEPOT & not DELIVERED & not collection-only). Plus
`load_handover`/`save_handover` and consumers `apply_exclusion`/`apply_availability`/`staged_depot_map`.
Wired into `run_alns.py` via `--handover-in` (absent = cold start) at 4 points + always-emit
`handover.json`. `state.py::build_initial_freight_states` gained `staged_overrides` kwarg.

**Validation (controller inline, wk1 Jan12-17 → wk2 Jan19-24, runs_archive/{out_wk1_ho,out_wk2_ho}):**
- **delivered-exclusion = the win:** wk2 in-universe **2381→2335 = the 46 double-planned spill orders**
  removed (wk1's tour tail delivers them Jan19-21; wk2 no longer re-plans them). Coverage 100%, 0
  ledger/0 temporal violations.
- **vehicle-availability:** 18 in-flight vehicles held; held-vehicles active Monday **17→12** (6 fully
  held to Tue/Wed, rest shifted to Mon PM per real return time). Fixes the Monday capacity leak.
- **staged-freight INERT (0 rows) — and that's correct.** KEY FINDING: trunk/hub handoff legs are NOT
  in `selected_plan_alns.csv` (they're in `trunk_schedule.csv`), so PL_EXPORT collections end AT_DEPOT
  in selected_df and LOOK staged. The 829 AT_DEPOT-final = 735 NETWORK_EXPORT + 93 PICKUP_ONLY + 1
  synthetic = ALL collection-only, none ours. Filtered by `COLLECTION_ONLY_SHAPES`. Genuine full-fleet
  staging is 0 because whole-tour ownership DELIVERS everything collected (spill tail) rather than
  staging it. The earlier "94 staged" estimate (leg-pattern heuristic) was WRONG (those are PICKUP_ONLY).
- **km is a stakeholder line, not a regression:** wk1 85,953 (= baseline within ALNS variance;
  cold-start identical 99.9%/36 tours). wk2 88,979 → **80,960 (−9.0%)**, per-order 37.4→34.7 — the drop
  is removing the 46 double-counted spill deliveries (their km belongs to wk1's tour tail). Combined
  two-week km is now NON-OVERLAPPING. Cold-start guarantee held (empty handover → all no-ops). 462 tests.

Status: **DONE.** Closes the run-to-run gap. Baseline runs_archive/{out_wk1,out_wk2} preserved; chain in
runs_archive/{out_wk1_ho,out_wk2_ho} + run_wk*_ho.log.

## Session log — 2026-07-03b (verified-leg correctness rewrite: GPS-distance + two-end-only FULL_FLEET SHIPPED)

**Trigger:** WT254741 (order `d220744d`, Ardex CB9 8QP → Rayleigh SS6 7NG) was stamped COLLECTION,
but our N8GNW physically delivered it (147 m from SS6 7NG at 06:49). Investigation found the verified
leg is consumed as **truth in forward mode** (`cambridge/verified_legs.py::corrected_flow` overrides
the raw api tag), so a wrong leg mis-plans the order — and found two verifier defects.

**Two defects in `planning_agent/verify_legs.py`:**
1. `_pc_matches` used asymmetric `order.startswith(telem)` — rejected N8GNW's real delivery because the
   reverse-geocoded ping (SS6 7**UA**) differs from the order unit (SS6 7**NG**) in the same sector.
2. Substitute-at-shared-origin fabrication: a fleet vehicle merely present at Ardex (57 orders / 49
   dests that day) was stamped as collecting THIS order. Presence at a shared shipper ≠ order-specific.

**Shipped (6 tasks, subagent-driven; 452→476 tests):**
- **GPS-distance matching** (`_within_m`, `_endpoint_coords`, vidx/garr carry lat/lon): match ≤
  `MATCH_RADIUS_M` metres to the endpoint's postcode-centroid coords; symmetric-sector postcode
  fallback when coords missing. **Radius calibrated to 500m during the measurement run** — 250m
  rejected ~45% of legit offset stops (centroid sits 250-600m from where trucks park); >1km = truly
  elsewhere. (Recovered telematics_assigned to 16,660, above the old 15,515.)
- **Shared-endpoint gating** (`build_endpoint_order_counts`, `_endpoint_is_shared`, `SHARED_MIN_ORDERS=5`):
  a substitute match at a shared endpoint is ignored (killed ~1,300 fabricated substitute COLLECTIONs).
- **TWO-END-ONLY FULL_FLEET** (superseded the plan's `_should_demote_full_fleet` trust-the-booking
  trigger): assert FULL_FLEET only when telematics confirms BOTH ends; one-end → the single confirmed
  leg. Chosen after measurement: promoting all one-end cases modelled them as directs → combined km
  **+24%** vs odometer; two-end-only keeps the ~391 hard-evidence directs.
- Baseline preserved (`verified_legs.before_gpsmatch.csv`) + order-by-order diff (`diff_verified_legs.py`).

**Measured (wk1 2026-01-12→17, wk2 2026-01-19→24, 90s budget):** coverage **99.9% / 100.0% HELD**;
plan 87,139 / 88,979; trunk 9,717 / 9,958; **combined 96,856 / 98,937 vs odometer 89,571 / 92,789 =
+8.1% / +6.6%** (≈ the honest baseline range; the ~1.5pp over the prior 85,472/87,790 baseline is the
legitimate cost of the 391 genuine directs previously half-counted). FULL_FLEET 2,567 → 2,500.
WT254741 → **DELIVERY** (its provable leg). 0 temporal / 0 ledger, trunk shortfall 0.

**KEY finding — FULL_FLEET = OWNERSHIP, not single-vehicle direct.** 1,456 of 2,312 assigned-FULL_FLEET
are two DIFFERENT booked vehicles (one collects, one delivers) = **hub relays** — correctly FULL_FLEET
(whole journey ours, no Palletline leg).

**CORRECTED (user pushback, same session): the pipeline ALREADY routes full-fleet through the depot.**
`legs.py:353` emits multi-day FULL_FLEET as mutually-exclusive DIRECT-vs-XDOCK options and the resolver
already chose **XDOCK for 293 of 349 served FF orders (84%), DIRECT only 56** (wk1 selected_plan_alns.csv).
So the km driver is NOT direct-vs-hub geometry — it's **how many full end-to-end journeys we PLAN**.
Promote-all planned ~895 extra collect+deliver pairs (mostly XDOCK) and still overshot to +24%, i.e. the
extra second legs push our fleet's km above its OWN odometer — consistent with the unconfirmed second leg
often being Palletline's, not ours. Two-end-only plans only the ~391 both-legs-provably-ours journeys →
matches odometer. **The earlier "route relays through the hub = next lever" was WRONG (already done).**
OPEN (deferred): could keep more one-end orders as FULL_FLEET→XDOCK if the unconfirmed leg was genuinely
ours — needs a fleet/depot-level odometer reconciliation of the promote-all extra legs to decide.

## Session log — 2026-07-05a (geocode structural repair + LE10 Hazchem trunk SHIPPED — wk2 hits 100%)

**Stakeholder asks:** (1) "besides B37 there should be a Hazchem trunk — check telematics"; (2) "some
BAD_GEOCODEs look correct (PE19 0UL) — use a UK-postcode regex to know WHICH character is wrong; is
the old retired-postcode fallback ported?"

**Verified:** LE10 3BS = real nightly trunk — 1,333 fleet pings at LE10* (980 at 3BS), 98% between
18:00-06:00, ~1 tractor/weeknight (20 reg-nights), ALL CB22-homed, Bedford ZERO (hazchem origins:
679 CB22-territory vs 34 Bedford-ish of 1,623/mo). The terminated-postcode fallback WAS already
ported (old simulation resolver = same live→terminated chain); the failures were different classes:
2 character typos (MK43 OYL letter-O, AL10 9B5 5-for-S), 2 structurally-valid-but-unissued
(PE19 0UL, MK41 9JJ — 404 on live AND terminated; sector exists), 2 district-only (SG8, CO10).

**Built (plan docs/superpowers/plans/2026-07-05-geocode-repair-hazchem-trunk.md, tests 416→452):**
- geocode.py: strict/lenient UK-postcode regexes; slot-class-driven repair (letter-in-digit-slot
  O→0/I→1/S→5/B→8 and inverse, ONLY at violating positions — a strict-valid postcode is NEVER
  mutated, short-circuit proven adversarially incl. EC1A/W1A/SW1A); then /outcodes/ centroid
  fallback (precision="outcode_district", honest provenance). PRODUCTION TRAP FOUND: the persisted
  cache held LEGACY null entries for all six keys (negative-cached pre-fallback) — cache-first
  short-circuited the new chain. Fix: legacy null + network-on = retryable miss; fresh failures
  cache as versioned marker {"failed": true, "chain": 2} (old pipeline reads it safely as negative).
- LE10 trunk: TrunkNight.hub; schedule groups (hub, depot, night); LE10 rows FORCED to CB22 (per
  telematics); min-1 trip on hazchem nights; roundtrip_km re-keyed (depot, hub); LE10_LATLON pinned
  to the cache entry; per-hub log/CSV/viz (amber lines to both hubs). SECOND PRODUCTION TRAP: the
  dispatchable customer legs (PL_IMPORT "D", PL_EXPORT "C") never carried hub= (only T_IN/T_OUT/H
  did) → candidates all hub="" → LE10 never sized. Fixed in legs.py (+2 tests). Review also caught
  a same-night double-draw hole (1-tractor pool, B37+LE10 same night → draws-dict overwrite, NO
  shortfall) — the one-trip-per-tractor-night rule now exists in code, not just in intent.

**Measured (one run/week):**
| | before | now |
|---|---|---|
| wk1 coverage | 99.7% | **99.9%** (2357/2359; only 2 MASSIVE_UNSUPPORTED left) |
| wk2 coverage | 99.9% | **100.0% (2380/2380) — first perfect week** |
| BAD_GEOCODE | 6 / 2 | **0 / 0** (six legacy keys self-upgraded: 2 repaired to unit, 4 outcode) |
| trunk | B37 only | B37 29-30 trips + **LE10 CB22 6 trips / 1,684 km** per wk; shortfall 0 |
| plan km | 83,900 / 90,071 | 85,472 / 87,790 (within the ±2-4% run band) |
| combined vs odometer | +4.0% / +6.2% | **+6.3% / +5.3%** — km/order 40.4 vs 40.5 and 41.1 vs 41.6 |

wk2's only unassigned universe: 44 BEFORE_PLANNING_START (pre-window = the T2 handoff class).
Viz regenerated: 3 trunk lines render (BEDFORD→B37, CB22→B37, CB22→LE10).

## Session log — 2026-07-04c (T1 SHIPPED — nightly B37 trunk as a fixed double-deck service)

**Built (spec docs/superpowers/specs/2026-07-04-night-trunk-service-design.md, plan
2026-07-04-night-trunk-service.md, subagent-driven, tests 383→411):**
- `freight_planner/trunk.py` (pure): per depot-night sizing `trips = ceil(max(import_pal,
  export_pal)/52)` — PL_IMPORT deliveries charge night = service_date−1 at source_depot,
  PL_EXPORT pickups charge their day at target_depot; TRUNK_DEPOTS=(BEDFORD, CB22) only
  (STOKE verified NO night trunk: zero night B37 pings, its 2 hub visitors run 10:00-17:00);
  weeknight departures; first-day imports prestaged; NaN-guarded; sizing runs on the FULL
  candidate frame (review-caught: tour-classified hub-flow pallets still ride the trunk).
- Nightly draw from the artic pool (stakeholder call): least-recently-drawn rotation, one
  round trip per tractor-night, skips tour-reserved (night AND next day); drawn tractor's
  next-day availability delayed to 10:00 via `avail_overrides` threaded into BOTH vehicle
  constructions (route_seed `_rv` + alns `_rv_ov` at every placement/timing site incl. the
  merge-sweep injection; `_time_of` gotcha: it silently swallows bare "HH:MM" — use strptime).
  Post-draw stranded-repair guard: drawn next-days join `daily_busy`. Shortfall = loud log +
  km still counted (never silently dropped).
- **Separate fixed-service reporting** (stakeholder call): trunk km NOT in plan km; run-log
  block + KPI "Fixed trunk service (double-deck 52 pal)" section + validation-metrics fields.
- `TRUNK_DECK_PALLETS = 52` documented in config as a VERIFIED FACT: single-deck would need
  12.3/15 trips/night vs 7.0/11 observed tractors; double-deck needs 6.5/8 — the only
  assumption reconciling demand with telematics.

**Measured (one run/week, no tuning):**
| | window-fix baseline | now |
|---|---|---|
| wk1 plan km | 87,134 | 83,900 (−3.7%; window-fix shuttle harvest grew: 49 trips/1,270 pal) |
| wk2 plan km | 91,770 | 90,071 (−1.9%) |
| trunk | — | **wk1 9,220 km / 33 trips; wk2 8,515 km / 31 trips; shortfall 0** |
| trips/night | predicted 6.5-8 | **6.6 / 6.2 — matches observed 7.0** |
| coverage | 99.7 / 99.9 | **HELD 99.7 / 99.9** (0 violations) |

**THE headline — plan-vs-reality at combined level (both sides now include trunk):**
wk1 combined 93,120 vs odometer 89,571 = **+4.0%**; wk2 98,586 vs 92,789 = **+6.2%**.
Per order served: wk1 39.6 vs reality 40.5 km/order (**plan BETTER**), wk2 41.5 vs 41.6
(**parity**) — while serving ~6% more orders than reality's telematics-covered set.
The like-for-like gap that started at +16%/+27% is effectively CLOSED at the km/order level.
NEW BASELINES: plan 83,900 / 90,071 (+ trunk 9,220 / 8,515 reported separately).

**Run-to-run variance note (2026-07-04, viz rerun):** an identical-config rerun landed wk1 86,424
/ wk2 93,722 (+3.0%/+4.1% vs the same-day T1 run) with coverage and the trunk schedule IDENTICAL —
ALNS is a time-budgeted stochastic search, so plan-km baselines carry a ±2-4% run band. Read km
deltas smaller than that band as noise; coverage, trunk (deterministic), and structure metrics are
the stable comparators.

## Session log — 2026-07-04b (collection window anchoring — hindsight times removed; SHIPPED, big km win)

**Trigger (stakeholder question):** ST4 8JB pickup 8548cf16 NO_FEASIBLE_ROUTE "at the door of the
Stoke depot". Diagnosis: infeasible in TIME not space — `_pickup_anchor_timestamp`'s day-reschedule
override adopted the ACTUAL collection timestamp (18:37 evening) as `earliest_start`; replay showed
all 79 OK-set vehicles fail SHIFT (even empty days at the Stoke anchor) or EXCESS_WAIT. January
scale: **1,221/10,495 collections had hindsight-hardened windows, 205 outside 06:00-16:00**
(unservable by any modeled shift — the real op's evening/overnight arm, 14.5% of movement).

**Stakeholder rule (2026-07-04):** collections never comply with historical ACTUAL times; specific
REQUESTED times stay binding; deliveries unchanged (already correct: hard "HH:MM - HH:MM" slots
comply, 00:00 placeholders/date-only → whole operating day, single-time = deadline-only).

**Fix (one function, TDD):** `cambridge/scope.py::_pickup_anchor_timestamp` — actual-timestamp
fallbacks (reschedule override + requested-missing) now return `combine(actual.date(), 00:00)`;
the midnight marker flows through `_is_pickup_placeholder_time` → operating-day expansion in both
`_collection_window` and `_pl_export_window`. Rewrote `test_pl_export_window_uses_actual_origin_
timestamp` → `..._DATE_only`; +3 anchor tests. Scope suite 73→76, freight_planner 383 green.
Plan: docs/superpowers/plans/2026-07-04-collection-window-anchoring.md (no spec — stakeholder).

**Measured (one run/week, no tuning) — MAJOR:**
| | K1 baseline | now |
|---|---|---|
| wk1 km | 92,743 | **87,134 (−6.0%)** |
| wk2 km | 100,082 | **91,770 (−8.3%)** |
| wk1 coverage | 99.7% | 99.7% held (remaining tail 14→4) |
| wk2 coverage | 99.8% | **99.9%** (2378/2380; tail 18→**0**) |
| wk2 unassigned | mixed | ONLY 44 BEFORE_PLANNING_START + 2 BAD_GEOCODE (typos "MK43 OYL"/"AL10 9B5") |

8548cf16 now collected 08:04 by BX67ZFV (Stoke). The evening ST4 cohort rides the normal network —
REPAIRED_DIRECT dropped to 0 (stranded repair has nothing left); ST4 8JB even joins shuttle bins
(2 trips 01-20). Shuttle/sweep healthy (35 trips / 904 pal; sweep 73 applied −469 km). The ~1,000
mid-day hardened windows were silently forcing afternoon-only collections and warping route shapes
— releasing them is where the km came from. **NEW BASELINES: wk1 87,134 / wk2 91,770.**
Plan-vs-reality like-for-like gap narrows: wk1 +15.9%→+10.5%, wk2 +27%→+11.5% (vs odometer−trunk).

## Session log — 2026-07-04a (K1 SHIPPED — mega-shipper shuttle carve-out + zero-cost merge sweep)

**Built (subagent-driven, spec docs/superpowers/specs/2026-07-03-shuttle-carveout-design.md, plan
docs/superpowers/plans/2026-07-03-shuttle-carveout.md):**
- `freight_planner/shuttle.py` — pure detection/packing: qualify address-day (day, unit pc,
  leg_kind, anchor depot) at ≥`SHUTTLE_MIN_PALLETS` (26), FFD into bins sized by the largest
  eligible anchor-depot vehicle (tractors first), ship at ≥`SHUTTLE_MIN_FILL` (0.9) of capacity.
  ALL float gates epsilon-tolerant (`_EPS=1e-6`) — review-caught: exact-90%-fill bins silently
  failed to ship; NaN pallets/hard_blocker guards.
- `route_seed.run_route_seed_plan` pre-loop carve: shuttle bins become real trips via
  `evaluate_day` (duty/breaks honest; `_trip_cap` deliberately bypassed — dedicated shuttles ARE
  the multi-trip reality), ledger parity incl. review-caught pickup `FREIGHT_DELIVERED` gate
  (would have CRASHED the seed via FreightUnavailableError), dissolve-to-pool fallback (coverage
  can't drop by construction), `RouteSeedResult.shuttle_job_ids`/`shuttle_stats`.
- ALNS pinning (`pinned_job_ids` threaded improve_route_seed→improve_existing_solution→
  improve_solution): destroy pool init+refresh filtered, worst/Shaw filter pinned INTERNALLY
  (review-caught adaptive-weight bias + Shaw anchoring on pinned clusters wasting iterations),
  targeted-ruination filter, eviction guard in `_best_insert_for_job`, `_pinned_check` under
  FP_ALNS_CONSERVE. Pinning is by JOB ID — greedy may top-up shuttle trips with small jobs.
- `freight_planner/merge_sweep.py` + wiring inside `improve_existing_solution` (pre-emission, so
  the B16 conserve/emission guards see the swept solution): one greedy pass, apply iff net ≥
  −1e-6, census vocabulary matches the replay (TRIP_*/BASE_INFEASIBLE/MERGED_DAY_INFEASIBLE/
  PINNED/EXCLUDED...), job-conservation multiset assert. km_delta rides into km_after (verified
  raw-physical km both sides, no penalized-basis mismatch).
- Knobs in freight_planner/config.py: SHUTTLE_ENABLED, SHUTTLE_MIN_PALLETS, SHUTTLE_MIN_FILL,
  MERGE_SWEEP_ENABLED. Tests 364→383 (+19 net incl. counterfactual-proofed pinning tests).

**Measured (one run per week, no tuning):**
| | baseline | now | note |
|---|---|---|---|
| wk1 coverage | 99.7% | **99.7% HELD** | 0 temporal / 0 ledger violations |
| wk2 coverage | 99.8% | **99.8% HELD** | |
| wk1 km | 91,390 | **92,743 (+1.5%)** | tours shifted too (30 tours / 26.5k tour km) |
| wk2 km | 104,743 | **100,082 (−4.5%)** | two-week net −3,308 (−1.7%) |
| CB9 8QP veh/day | 17.6 / 17.2 | **11.8 / 11.0** | real op: 5 |
| routes per district-day | 2.44 | **2.25 / 2.27** | real op: ~1.6 |
| redundant same-address visits | 440 / 492 | **290 (−34%) / 353 (−28%)** | |
| pallet fill mean/median | 71/81 · 68/73 | 72/83 · 67/75 | regression watch: held |

Shuttle in production: wk1 18 address-days → 39 trips / 331 jobs / 1,010 pallets (CB9 4-8
trips/day); wk2 20 → 32 / 267 / 828. Merge sweep: 83/673 and 82/662 applied, −600/−587 km
(more than the replay's −99 predicted — it runs post-shuttle on different shapes; 134/76
candidates pinned-blocked). Census top blockers: TRIP_CAPACITY 251/312, TRIP_SHIFT 110/83.

**Honest reading:** structure moved decisively (CB9 −5.9/−6.2 vehicles/day, redundant visits
−34/−28%) but wk1 km went UP 1.5% while wk2 fell 4.5% — the spec's anticipated tradeoff shape
(routes that lost their CB9 backhaul filler run emptier on wk1). CB9 is still 11-12 veh/day vs
the real 5 because (a) sub-load residuals + CB9 *deliveries* (~10/day, below the 26-pal qualify
line) still ride passing routes, and (b) on heavy days bins dissolve when no CB22 anchor vehicle
is free (01-14: only 4 of ~8 needed trips committed → 16 vehicles). Candidate levers if we
return: cross-depot/rigid fallback vehicles for dissolved bins; K2 day-flexibility.

**Incident:** `freight_planner/data/vehicle_master.csv` vanished mid-session (untracked, cause
unknown — no agent touched it, a git clean would have wiped much more). Regenerated cleanly via
`python -B planning_agent/build_vehicle_master.py` (cached MOT scrape) → suite green again.
Consider committing the master or its builder inputs. NEW BASELINES: wk1 92,743 / wk2 100,082.

## Session log — 2026-07-03a (like-for-like km gap DECOMPOSED — same-address splits are the headline; K-series filed)

**Question (stakeholder):** even without trunk, why does the plan run more km than reality when we
serve MORE orders and we're the ones optimizing?

**Like-for-like method:** actual = odometer fleet km − trunk estimate (fleet tractor-nights at B37
× depot↔B37 round trip: 10,698/10,516 km, 38/35 tractor-nights); orders = covered in-universe
(regs with telematics movement, pseudo-reg normalized). Result: **wk1 plan 38.9 km/order vs actual
35.7 (+9%); wk2 44.1 vs 36.9 (+20%)**. Raw: wk1 91,390 vs 78,873; wk2 104,743 vs 82,273.

**Where the excess lives (ranked by evidence):**
1. **Same-address same-day splits (biggest):** ~270-280 unit-postcode-days/wk with ≥2 customer
   stops; **72%/79% split across ≥2 vehicles → 440/492 redundant vehicle-visits/wk**; 50/76 of the
   split groups have a pickup AND a delivery at the same address on different trucks (reality:
   one visit, drop + collect). Suspicious as an OPTIMIZER gap too — a second stop at an
   already-visited address is ~0 km to insert, yet seed+ALNS leave it.
2. **Half-empty long singles:** 36-43 sole trips/wk with ≤2 customer stops = 8.1-8.6k km (8-10% of
   plan km), longest 380-480 km out-and-backs, mean pallet fill **43-48%** (only 5-11 are ≥80%
   legit FTL). Reality holds them a day, tours them, or hub-injects to a partner member.
3. **No day-smoothing:** plan front-loads (wk2 Mon 25.1k vs actual 19.4k) and dies Fri/Sat (13.9k
   vs 18.9k). Reality spreads 48-72h-promise freight across the week.
4. **Fleet spreading:** plan 336/355 vehicle-days vs actual 311/316, incl. regs with ZERO real
   movement (Y88WSM, BU20VHY, C29BAL, TA70WTL, Y888AUK at 1.5-3.4k plan km each). wk2 rigids:
   plan 43.3k vs actual 33.3k (+10k) — thin trips + splits land on rigids.

**Retired evidence:** "drops per vehicle-day" from telematics is unreliable both directions (loose
ping clustering overcounts, ≥3-ping/≥10-min dwell undercounts) — don't use stop-density claims.
**Caveats:** not all split visits recoverable (some capacity-forced by 26-pal ceiling); the ≥80%-
fill tiny trips are legitimate point-to-point loads.

**Unmodeled real-operator levers, in km order:** (a) co-load per address + single-visit
drop-and-collect; (b) hold non-urgent freight to build full rounds; (c) hub-inject out-of-round
singles (fee, not km); (d) trunk w/ backhaul (=T1); (e) Saturday running.

### K1. Mega-shipper scatter / territorial overlap — `DONE (shipped 2026-07-04 — shuttle carve-out + merge sweep; see session 2026-07-04a)`
> **The original "0-km merges left on the table" theory is DISPROVED by replay.** Rebuilt wk1
> inputs + final routes and re-ran every split-pair merge through the real evaluators (811 guest
> jobs, 193 groups): TRIP_CAPACITY 306, FEASIBLE 302, TRIP_SHIFT 106, TRIP_TIME_WINDOW 76,
> OK_SET_EXCLUDED 12, DEPOT_MISMATCH 9. The 302 feasible merges are **km-neutral**: host delta
> mean 2.3 km, guest removal saving mean 1.7, NET −99 km total (positive-only 544). Both routes
> already pass through the address — the split visit is a SYMPTOM, not the cost. Probe:
> scratchpad k1_merge_replay.py (rebuilds run inputs; OSRM localhost:5000 must be up).
>
> **Actual root cause — territorial overlap, dominated by mega-shipper scatter.** Presence
> metric (robust, not the retired stop-count proxy): plan runs **2.44 routes per postcode-
> district-day vs reality's 1.6** (multi-route district-days 40-43% vs ≤28%, and the actual
> figure is inflated by transit pings); 51.6k/62.8k of plan km rides on routes touching
> contested districts. The extreme case: **CB9 8QP (Haverhill shipper) = 313/259 pickups/wk,
> 204/143 pallets/DAY ≈ 8/5.5 artic loads — plan drags 17-18 vehicles/day through CB9;
> telematics shows exactly 5 dedicated fleet vehicles/day** (the Stoke ST4 8JB pattern again).
> Next tier: ST4 8JB 62/43 pal/day (4 veh), SG8 5RL 56/38 (7-8 veh!), AL7 3UB 27/22 (8-9 veh!).
> Greedy seed hands each passing route a few pallets until its peak fills; single-job ALNS moves
> can't undo route-shape overlap (that's exactly the km-neutral pairwise result above).
>
> **Fix candidates:** (A — recommended) pre-seed SHUTTLE CARVE-OUT: addresses ≥ ~1 truckload/day
> get floor(pallets/veh-capacity) dedicated full-load round trips on nearest-depot vehicles;
> only the residual rides the general pool. Mirrors reality, small blast radius, measurable.
> (B) geographic cluster-destroy ALNS operator (frees a district-day's jobs together) — attacks
> general overlap but repair may re-scatter; uncertain payoff. (C) cluster-first seed restructure
> — biggest hammer, biggest risk. Stakeholder to pick before code.

### K2. Day-flexibility / load-building across days — `SHIPPED DORMANT (v1 2026-07-06 — see session entry at top)`
> Plan pins every order to its date; reality holds non-urgent freight to fill rounds (and runs
> Saturdays). Needs a service-promise model (which orders may shift ±1 day?) — design question,
> touches the T2 operator brainstorm. Known related: missing window_start front-loading.
> **2026-07-06: v1 shipped behind `--day-flex` (earlier-only, FF-XDOCK, 2d cap), km-neutral
> because 316/316 multi-day dwellers are window-stamp-pinned; kept DORMANT. Reopen = v1.1
> stamp-vs-constraint window rule (270 eligible/wk) after window-provenance check.**

### K3. Hub injection for out-of-round singles — `TODO (filed 2026-07-03; design-level)`
> Reality never drives a half-empty truck 400 km for one drop — it pays the network to deliver
> from B37. Introduces a fee-vs-km tradeoff the objective doesn't have; depends on T1 trunk and
> belongs in the T2/operator product conversation.

## Session log — 2026-07-02h (plan-vs-reality km + trunk sizing analysis; NEXT: T1 trunk, then week-to-week)

**Plan vs reality (telematics odometer + estimate for uncovered orders):** wk1 plan 91,390 vs
reality ≈95,653 (odometer 89,571 over 68 fleet vehicles + ~6.1k for the 150 in-universe orders
served by no-telematics regs at the fleet's ~41 km/order) → plan = 95.5%. wk2 plan 104,743 vs
≈98,942 → 105.9%. Upper-bound estimates (dedicated round trips per uncovered order) put plan at
69.6%/94.0%. Data trap: qargo resources like "P888RNW 2" space-strip into pseudo-regs that look
untracked — normalize before matching (uncovered is ~6%, not 13%).

**Trunk sizing (stakeholder question: if ALL in-universe PL_IMPORT comes down from B37 on our
trunk):** import 307 pal/night mean (max 388), export 205 (280); round trips carry both ways →
**12.3 mean / 15 peak trips @26-pal single-deck, but 6.5 / 8 @52-pal double-deck — and telematics
shows 7.0 mean / 11 peak fleet tractors at B37 per weeknight (25 distinct regs, ~zero rigids).**
Conclusion: the real trunk runs at DOUBLE-DECK scale and matches the requirement almost exactly;
it is not under-run. Confirmed the model contains NO trunk km ("the scheduled depot->hub trunk is
not in routed km", options_resolver) — ~9-11k km/week of real driving (≈7 round trips/night,
Bedford↔B37 ~250 km, some CB22 ~350 km) sits in the odometer actuals but not in the plan, so
like-for-like our plan is ~10% heavier than reality's equivalent work, not at parity.

### T1. Nightly hub trunk as a scheduled service — `DONE (shipped 2026-07-04 — see session 2026-07-04c)`
> Model the B37 trunk as a FIXED nightly service, not per-order routing: ~7 double-deck (52-pal)
> round trips departing ~20:00-22:00, sized by max(import, export) pallets/night; tractors on
> trunk duty flagged as reduced daytime capacity; PL_EXPORT TRUNK freight gets a real departure
> cutoff (miss the trunk = next night); PL_IMPORT freight availability at the depot becomes the
> trunk's morning arrival instead of materializing free. Adds the missing ~10k km/week and makes
> artic capacity honest. Telematics evidence: B37 stop pings peak 21:00-23:00 + 00:00-03:00;
> 14.5% of fleet movement is outside 07:00-18:00.

### T2. Week-to-week state handoff — `TODO (filed 2026-07-02; AFTER T1)`
> Runs bootstrap cold; reality never does (in-flight tours, trailers loaded Friday, freight
> collected last week delivering Monday — the window-edge asymmetry visible in the km
> comparison). Stakeholder: needs serious operator-perspective + product-use-case design before
> implementation — start with a brainstorm on how a dispatcher would actually run week rollover,
> not with code. Supersedes/absorbs the old "run-to-run state gap" note.

## Session log — 2026-07-02g (vehicle catchment B15 — soft per-vehicle territories; SHIPPED. + planner config home)

Spec [docs/superpowers/specs/2026-07-02-vehicle-catchment-design.md] (2 amendments), plan
[docs/superpowers/plans/2026-07-02-vehicle-catchment.md]. Subagent-driven, TDD, 342 planner tests.

**Shipped:**
1. **`freight_planner/config.py` (NEW)** — stakeholder decision mid-execution: planner-owned knobs
   (tour formation, breaks/waits, catchment) moved OUT of the outdated cambridge/config.py into a
   leaf module the new pipeline owns. Consumer-audited: shared infra (DEPOT_ANCHORS, fleet master,
   VEHICLE_DEPOT_MAP, MAX_DRIVING_H_PER_DAY, MULTIDAY_AVG_SPEED_KMH — week_planner still consumes)
   stays in cambridge. No re-export shims.
2. **Calibration** — `freight_planner/catchment.py`: per-vehicle radius = P95 of its own Jan
   history (home-anchor → responsible-leg postcode, haversine; ≥20 samples), type-P95 fallback,
   30 km floor; `fleet_types` backfill guarantees EVERY fleet reg a radius (amendment: zero-history
   vehicles like Y88WSM were unpenalized and attracted exactly the long work — the wk1 validation
   caught it). Real radii: 64 earned + 15 fallback; L88GNW 262 km (long-lane rigid keeps its lanes),
   P888RNW 97 km; type P95s rigid 186 / tractor 206 / van 90.
3. **Soft penalty** — `vehicle_cost.out_of_area_penalty_km`: phantom km = 2.6 × overshoot
   (**amendment: factor 1.0 could NEVER flip a same-depot rigid→artic** — round-trip road km ≈
   2.6 × dist caps pen/route at 38% vs the 47.7% fuel-rate gap; 2.6 = overshoot at round-trip
   road scale, flip at ~1.9× radius). Wired into EVERY ranking/acceptance site: seed
   `best_insertion` (km units), ALNS insert rankings incl. eviction netting, `changed_costs`,
   init ledger, `route_cost`/`solution_cost` (GBP). Reported km stays physical. Tours exempt.
4. **run_alns wiring** — calibration into `vehicle_df["catchment_km"]` + 2 diagnostic log lines
   (regs mapped; out-of-area share of final daily jobs).

**MEASURED (both weeks, FP_ALNS_CONSERVE=1, 0 temporal/ledger, all accounted):**
wk1 99.7% HELD, km 94,034→**91,390 (−2.8%)**, out-of-area 7/2,498 (0.3%). wk2 99.8% HELD,
km 108,296→**104,743 (−3.3%)**, out-of-area 13/2,490 (0.5%). Proper territories IMPROVED km —
the penalty stopped the optimizer scattering long work onto cheap-per-km rigids and the global
geometry got better. The old ">120 km rigid legs" proxy is retired as a metric: per-vehicle radii
legitimize the long-lane rigids (wk1 44/52 such legs are in-territory); the honest metric is the
out-of-area share (0.3–0.5% ≈ the real network's exception rate).

**Filed (final review, pre-existing):** `freight_planner/repair.py` is DEAD CODE (no production
imports) and ranks catchment-blind on raw km — delete or align before anyone rewires it.

**Roadmap next (reordered 2026-07-02h):** **T1 nightly trunk service** → **T2 week-to-week state
handoff** → tour-merge pass (uncommissioned) → scope.py `datetime.now()` anchor fix →
B17 stranded-repair stale-eval edge → rolling replan (other contributor).

## Session log — 2026-07-02f (realism + operator pack — breaks, wait cap, runsheets, type-view; SHIPPED)

Spec [docs/superpowers/specs/2026-07-02-realism-operator-pack-design.md], plan
[docs/superpowers/plans/2026-07-02-realism-operator-pack.md]. Executed subagent-driven (fresh
implementer per task + spec/quality review each); TDD throughout; 815 tests (3 known environmental
failures unrelated).

**Shipped:**
1. **Statutory breaks (EU 561/2006 core)** — `route_costs.statutory_breaks` (45 min per 270 min
   cumulative driving, multi-break long legs, exact-limit owed before NEXT drive); applied in BOTH
   evaluators (`evaluate_route` + `evaluate_tour`), vans exempt; breaks consume SHIFT/duty clock but
   never the driving cap; accumulator threads across daily trips (reload < 45 min never resets) and
   resets at tour day boundaries; `break_minutes_before` on StopTiming/TourStop. Knobs
   `DRIVE_BREAK_AFTER_MIN=270`, `DRIVE_BREAK_MIN=45`.
2. **Stop-wait slack cap** — first stop of a trip departs the depot just-in-time (wait → later
   `route_start`, no curbside idle); later stops fail `EXCESS_WAIT` beyond `MAX_STOP_WAIT_MIN=90`;
   reason is ALNS-repairable.
3. **Consolidated runsheets** — new `freight_planner/runsheets.py` (+CLI, auto-emitted by run_alns as
   `reports/runsheets.html`): printable per-vehicle pack for the whole window with break rows,
   reload boundaries, two-point COLLECTION rows (review caught drivers losing the pickup address —
   107 direct moves now render collect+deliver), tour-day markers, NaN-safe CSV round-trip.
   `route_stops.csv` gained `vehicle_type` + `break_minutes_before`; tour `depot_return` rows now
   carry the FINAL tour day (via `tour_return_dates` from write_reports), fixing the old cosmetic bug.
4. **Viz type view** — trip app "Colour by: Vehicle | Type" toggle (tractor red / rigid blue / van
   green, grey fallback for pre-schema plans) recoloring lines, markers, list and card, with legend.

**MEASURED (both weeks rerun, FP_ALNS_CONSERVE=1, 0 temporal / 0 ledger / all accounted):**
wk1 99.7% (2,352/2,359) — coverage HELD; km 89,566→94,034 (+5.0%). wk2 99.8% (2,376/2,380) —
coverage HELD; km 99,124→108,296 (+9.3%); daily vehicle-days 298→327 (breaks split tight days).
The km increase is the honest price of drivers' hours + no phantom curbside idling — wk1 runsheets
show 102 statutory-break rows, wk2 146. Baselines for the next pack: these numbers.

**Filed (pre-existing, found by final review, NOT fixed):** B17 — stranded-repair host shrink
(`tour_plan.py` ~429-438) updates `host.jobs` unconditionally but keeps the STALE evaluation when the
shrunk re-evaluation is infeasible → jobs/evaluation out of sync in that narrow case. Benign so far
(validators clean); fix shape = only shrink when re-eval feasible, else leave host untouched. Also:
dead `route_costs` import in run_alns.py.

**Roadmap next:** vehicle catchment (B15) → tour-merge pass (uncommissioned) → scope.py
`datetime.now()` anchor fix (reproducibility) → rolling replan (other contributor).

## Session log — 2026-07-02e (B16 FIXED — ALNS silent job loss was a phantom-saving/lossy-emission pair)

Diagnosed with per-accepted-move conservation asserts (`FP_ALNS_CONSERVE=1`, kept in alns.py). The
search bookkeeping was CLEAN — the per-move checks never fired. The assert that fired was at the
**emission boundary**: 12–13 jobs present in the final solution got no plan records. All of them sat
on ONE vehicle-day, `(M888 WSM, 2026-01-21)`, which evaluated **infeasible (TIME_WINDOW)** in the
accepted solution.

**Root cause (two defects compounding):**
1. `improve_solution` never re-validated days that a destroy op only REMOVED jobs from. Removal is
   NOT feasibility-preserving on real roads: OSRM distances violate the triangle inequality, so
   bypassing a stop can lengthen a leg past a window/shift. And `evaluate_day` returns
   `total_km=0.0` for an infeasible day, so `cost()` priced the broken day at ZERO — destroying a
   route scored as a huge phantom saving and was greedily accepted (part of the shipped run's
   "11,962 km saved" was this mirage).
2. `build_plan_records` silently `continue`d past jobs with no stop timing — an infeasible day has
   truncated/empty trip evaluations, so its jobs vanished with no record and no rejection.

**Fix (TDD, red→green):** (a) `changed_costs()` in the spec loop — every changed vehicle-day is
costed AND feasibility-checked in one `evaluate_day` pass (same evaluation count as before); a spec
with any infeasible changed day is refused. (b) `build_plan_records` now raises `ValueError` instead
of silently dropping (infeasible day, missing candidate, or missing stop timing) — this class of bug
can never be silent again. Tests: `test_plan_records.py` (new, 3) +
`test_alns.py::test_improve_solution_rejects_moves_that_leave_a_changed_day_infeasible` (a
triangle-inequality-violating `_ChainRouter` via `set_router`). 797 tests, 3 pre-existing
environmental failures (live OSRM answers cache-miss tests in old-dispatcher suites).

**MEASURED (wk2 rerun, conservation checks armed):** see run log — coverage recovered to ~99.8%,
no assert fired, 0 temporal / 0 ledger. NOTE for reproducibility hunts: wk2's official window is
01-19..**01-24**; also `cambridge/scope.py` anchors missing-timestamp orders to `datetime.now()`,
so runs on different DAYS can shift one boundary order in/out of the universe (why the first
diagnostic replay came back clean).

## Session log — 2026-07-02d (honest universe — NO_RESOURCES in all modes; SHIPPED. +B16 filed)

Stakeholder caught it from one order: the EH48 2PE "delivery" (our 1,151 km singleton tour) has NO
fleet resource in Qargo — `resource_subcontractor="Palletline (import from API)"`, no verified leg,
DONT_INVOICE, and telematics shows no ZEEFleet vehicle ever stopped there (X8RNW passed Bathgate on
the M8 at speed). It was a NETWORK movement. Census: **112 wk1 / 88 wk2 in-universe orders (≈4.5%)
were subcontractor-only** (mostly Palletline API), incl. tour stops NE29 (zero real pings all week),
WA11, ML1 — a big slice of why our plan needed more northern vehicles than reality's 2.

**Stakeholder decision (verbatim intent): activate NO_RESOURCES in forward mode too, and a
subcontractor does NOT count as a resource.** `demand.exclusion_reason` now returns NO_RESOURCES in
ANY mode when no powered fleet vehicle touched the order. CANCELLED unchanged. TDD
(test_demand_modes rewritten to the new contract); 649 tests.

**MEASURED:** wk1 universe 2476→**2359** (NO_RESOURCES 83 excluded), coverage **99.7%** (2352),
km 95,201→**89,566**, tour vehicle-days 56→47, 0 violations — perfect. wk2 universe 2472→**2380**
(NO_RESOURCES 73), km 101,333→**99,879**, 0 violations, BUT coverage read 99.3% (2363/2380) —
which exposed:

**B16 (NEW BUG, latent ALNS job loss — DONE, fixed in 2026-07-02e above):** 13 daily jobs (ALL dated 2026-01-21, normal
PL_IMPORT/PL_EXPORT/LOCAL with fleet vehicles) are served by the seed (log: seed selected=2,597)
but absent from the final plan (2,584) with ALNS reporting `remaining=18` unchanged and
`inserted=0` — a SILENT, unaccounted loss inside the 90s search. Evidence: no duplicate job_ids;
zero-budget ALNS loses nothing; reserved∩daily-route days = ∅ (not the repair's reservations);
wk1 unaffected; run h (same code minus universe fix) had no loss → trajectory-dependent latent bug,
surfaced by the demand change. Suspect area: destroy/repair pool bookkeeping dropping jobs whose
meta lookup fails mid-search (alns.py `_build_job_meta`/pool accounting). True wk2 coverage without
the loss ≈ 2,376/2,380 (99.8%). Per stakeholder ("stop running again"), outputs left as-is;
diagnose with per-move job-count instrumentation next session.

## Session log — 2026-07-02c (stranded-backhaul repair — the residual tail CLEARED; SHIPPED)

The remaining in-universe tail (7/9 orders per week: NE42 full loads + the ST4 shipper's
distribution) was one pattern: XDOCK orders whose contended/far pickup stranded and whose delivery
cascaded. Ground truth (X8RNW telematics + Qargo): reality serves exactly these as **DIRECT carries
on tours' empty legs** — X8RNW collected the 26-pallet NE42→SG1 order (27623f3d, the long-standing
NO_FEASIBLE_ROUTE example) on its Scotland sweep's homebound leg. Spec
[docs/superpowers/specs/2026-07-02-stranded-backhaul-repair-design.md], plan
[docs/superpowers/plans/2026-07-02-stranded-backhaul-repair.md]. TDD; 649 tests green.

**Shipped:** post-seed pre-commit repair step in `run_multiday_seed_plan`
(`STRANDED_REPAIR_ENABLED` knob). Eligibility strictly bounded: BOTH XDOCK legs stranded (the
anti-bloat guarantee vs the reverted 74-flip resolver). Synthetic `RD:` DIRECT jobs; **Mode 1**
inserts into an existing tour via new `try_insert_tour_job` (every position, full physics, new
`floor_offsets`/EARLY readiness mirror of LATE), accepted only when added km beats a standalone
run; **Mode 2** batches leftovers into new DIRECT tours via the normal build/resolve/select
machinery (daily-busy vehicle-days excluded). Superseded legs → reason `REPAIRED_DIRECT`
(ALNS ignores: not in `_REPAIRABLE_REASONS`); synthetic candidates threaded to reports; KPI:
superseded legs no longer gate order completion.

**Execution findings (why the first attempts did nothing / broke):**
1. Tour-planned deliveries whose pickup stranded only bounce AT COMMIT — the repair pulls them
   off their host tours pre-commit (re-evaluating the shrunk tour) instead of waiting.
2. Seed-time strand reasons are the LAST insertion failure (mostly `SHIFT`), NOT the post-ALNS
   `NO_FEASIBLE_ROUTE` relabel — eligibility now matches all insertion-failure reasons. Safe:
   a both-stranded XDOCK pair is beyond ALNS anyway (its delivery stays dependency-gated).
3. The old plans carried **phantom km**: tours drove to doomed delivery stops (bounced at commit,
   km kept) and ALNS inserted doomed pickups (collections that could never deliver) — ~8k km/wk
   in wk1. The repair removes both.

**MEASURED (final, both weeks):** wk1 **99.7%** (2469/2476, +7 orders), km 103,188→**95,201**
(−7.7%: phantom-km removal >> the +1.6k honest RD tour km); wk2 **99.8%** (2468/2472, +8),
km 107,122→101,333. NFR+DBP tail: wk1 **0**, wk2 2 (the known SN5-type case, not the XDOCK
pattern). 0 temporal / 0 ledger / 0 phantom. `REPAIRED_DIRECT` 14/16 legs in the unassigned
table. **Flagship:** 27623f3d rides TOUR:X8GNW:2026-01-14 day 3 homebound, 26 pallets on the
emptied trailer, delivered 12:26 on its due date — the X8RNW template reproduced. Not committed.

## Session log — 2026-07-02b (tour consolidation restored — skip-not-break + salvage pass; SHIPPED)

Stakeholder caught a regression from the realism pack: Scotland tours fragmented into near-empty
singleton sweeps (e.g. one 1,151 km round trip for a single EH48 order; on 01-15 THREE separate
Scotland-bound tours, one of them 15 km from another's stop). Diagnosis (instrumented replay of the
real wk1 seed): the new hard constraints (LATE + freight-readiness) were CORRECT but exposed two
batcher weaknesses:

1. **`build_tours` accretion broke on the first infeasible candidate** — under dwell, mid-accretion
   evaluation almost never failed, so the `break` was latent; with LATE it fired constantly and one
   bad candidate ended a tour's growth, stranding compatible neighbours. Fix: blocked-set skip
   (infeasibility is monotone as a tour grows, so a failed candidate stays blocked for that tour).
2. **`resolve_cluster` fallout was never re-pooled** — a far-origin DIRECT in a cluster (DH3/SG8) or
   one infeasible consolidation (ML6 reached past the day cap under NN ordering) triggered the
   all-or-nothing per-depot fallback, and the resulting orphans (KA1, ML6 — 35 km apart, same due
   day, readiness-compatible) each burned their own ~1,000 km sweep. Fix: **salvage pass** in
   tour_plan — re-pool feasible single-delivery tours once through build_tours (+resolve_cluster);
   two-point moves excluded (they are what poisons consolidation). Consolidation mode only.
   Also threaded `ready_by_job` through `resolve_cluster`/`_per_depot` (was silently dropped).

**Telematics ground truth (stakeholder suggestion):** the real X8RNW ran the week's Scotland work as
ONE 3-day sleep-out sweep (Cambridge → 2h collection at the Stoke ST4 yard → Glasgow overnight →
Ayr KA6 + Lanark ML11 drops → Retford overnight → Grantham/Bedford/Stevenage deliveries homebound;
1,720 km). Only TWO vehicles went north of 54.5 all week. Deep-first-then-work-homeward is the real
ordering pattern (our NN due-first orders shallow-first — possible future ordering improvement).

**MEASURED (both weeks):** coverage/violations unchanged (99.4%/99.5%, 0 temporal / 0 ledger / 0
phantom). wk1 singleton tours 15→11, Scotland-bound 7→6, KA1+ML6 now one CB22 tour w/ Stoke
load-stop, km 103,914→103,188. wk2 singletons 13→9, X8RNW-style 10-stop 2,448 km consolidated sweep
formed, km 107,344→107,122. Remaining splits are constraint-true (EH48 due 01-13 vs KA-cluster
ready 01-15 = readiness physics; G52/DG2 pickups due 01-14 can't ride an 01-15 departure = deadline
physics). 642 tests. Not committed.

**Lesson:** hard constraints amplify greedy-heuristic weaknesses — after adding a constraint, audit
every `break`/fallback the constraint can newly trigger. And telematics is the consolidation oracle.

## Session log — 2026-07-02 (tour realism pack — dwell deleted, honest tour physics, runsheet emit; SHIPPED)

From an operator-perspective review of the whole planner (gaps: drivers not modeled, tour model softer than
daily, free depot work, no catchment, brittleness, operator UX). Stakeholder picked the roadmap: rolling
replan excluded (owned elsewhere); this pack is item 1. Spec
[docs/superpowers/specs/2026-07-01-tour-realism-pack-design.md], plan
[docs/superpowers/plans/2026-07-01-tour-realism-pack.md]. TDD throughout; 640 tests green.

**Shipped (tours.py / tour_plan.py / manifest.py / cambridge/config.py):**
- **Dwell → LATE (stakeholder: "dwell is wasted time and resource"; due = deadline, early delivery OK).**
  `evaluate_tour` no longer idles the vehicle in-region until a stop's due day; a stop reached *after* its
  due day is infeasible (`LATE`). Early service is bounded by the existing due-spread gate (≤4 days).
- **Honest tour physics.** Tour legs + return use `road_km` (OSRM; was straight-line haversine) at
  `MULTIDAY_AVG_SPEED_KMH`; every stop costs load-based `service_minutes` (2x for two-point kinds,
  `DEPOT_LOAD` pays the base); tour days split on TWO caps: 10h driving AND 13h elapsed duty
  (`TOUR_DAY_ELAPSED_CAP_MIN`, new knob beside the other tour params; `TOUR_DAY_START_HOUR=7`).
- **Runsheet-grade emit.** `TourStop` carries `arrive_minute`/`depart_minute`; the tour emit writes each
  stop's true serve day (`service_date = start + day_index`) and real clock times (was flat "start 12:00:00").
  Plus a latent `manifest.build_route_stops` bug fixed: it flattened per-stop `service_date` to the route's
  first record (exposed the moment per-stop dates became real).
- **Freight-readiness gate (found by full-run validation, the key catch).** First validated run showed **27
  temporal violations (wk1)**: early-served tour deliveries departed BEFORE their feeding pickup ran — the old
  dwell had been *accidentally* enforcing availability (serve-on-due always landed after the pickup), and the
  day-granular ledger can't see clock order. Fix: `build_tours(ready_by_job=...)` — a tour departing on its
  batch's earliest due date may only carry jobs whose freight is at the depot by then (fed jobs are ready the
  day AFTER their predecessor pickup's date); late-ready jobs seed their own later tour. Start-day safety net
  in tour_plan (`day = max(min_due, max_ready)`).

**MEASURED (both weeks, final run):** 0 temporal / 0 ledger violations, 0 phantoms.
wk1 99.4% (2462/2476, = baseline), tour vehicle-days **76→55 (−21)**, km 101,736→102,390 (+0.6%: tour km
22.5k→29.0k = the honest 1.29 road factor, offset by daily km −6k from freed vehicles).
wk2 **99.3→99.5%** (2455→**2460**, +5 orders), tour vehicle-days **71→53 (−18)**, km 98,872→109,135
(+10%: honest tour km + coverage reinvestment for the 5 new orders).
Residual tail unchanged (NFR 7/10, DBP — the known Stoke-shipper/far-collection set). trip_app regenerated
for both weeks. Not committed.

**Lesson:** deleting a "wasteful" mechanism can delete a hidden invariant it was accidentally enforcing —
the dwell was doubling as freight-availability sequencing. Full-run validation (not the unit suite) is what
caught it; clock-honest emit is what made it visible at all.

## Session log — 2026-07-01 (tour-aware DIRECT-vs-XDOCK resolver — TRIED, net-NEGATIVE, REVERTED)

Full brainstorm→spec→plan→TDD ([docs/superpowers/specs/2026-07-01-tour-aware-option-resolution-design.md] — REVERTED). 632 tests green after revert.

**Hypothesis (from a systematic-debugging investigation):** the remaining in-universe tail (~17 orders/2wk, all multi-day FF)
was a phase-ordering gap — `_cost_choice` picks XDOCK on **static km before tours exist**; the far XDOCK pickup strands
(NE42 too far to serve daily / ST4 competes for Stoke's ~1.5 tractors) and the delivery cascades to DELIVERY_BEFORE_PICKUP;
DIRECT would ride a tour. Rule tried: for multi-day FF, if the XDOCK **delivery** is `is_tour_only`, resolve DIRECT.

**MEASURED (both weeks) — net-NEGATIVE.** Coverage up (wk1 99.4→99.7%, wk2 99.3→99.4%) BUT total km **+17-21%**, tour km
**doubled** (wk1 22.5k→47.2k). Root cause: "delivery tour-only" is too coarse — it flipped **74 orders/wk** to DIRECT, only
~6 of which actually stranded; the other ~68 were already served cheaply via **consolidated XDOCK** and became expensive
dedicated DIRECT carries. The spec's assumption ("consolidation isn't lost — the tour builder batches DIRECT") was WRONG:
depot-staged XDOCK deliveries batch far tighter than origin-specific DIRECT moves. It also MISSED the ST4→NW orders
(delivery ~60 km from Stoke, not tour-only) which strand on **Stoke fleet undersizing** (the single big ST shipper's volume
vs ~1.5 tractors) — a resourcing problem, not a resolver one. **User: revert + leave for now.**

**Lesson (recorded so it isn't re-tried):** a resolve-time signal can't distinguish "will strand under XDOCK" from "served
fine" — servability is phase-ordering-dependent. The right shape is a **targeted post-seed repair**: flip only the orders
that actually stranded to DIRECT and retry (~17 flips, no km bloat) — deferred. Remaining tail (~14-17/wk) is genuinely
hard: far collections + the Stoke-shipper/fleet mismatch. Cf. [[coverage-is-tour-capacity-bound]] (revert net-negative).

## Session log — 2026-07-01 (multi-day tour CAPACITY bug — the NO_FEASIBLE_TOUR tail was FALSE rejects, FIXED)

Systematic-debugging investigation → TDD fix. **Not committed.** 632 freight_planner+cambridge tests green. Confirms the
[[coverage-is-tour-capacity-bound]] thesis with the concrete capacity bug.

**Root cause (instrumented the two NO_FEASIBLE_TOUR emit points + replayed the wk2 seed via a driver):** the ~18/week NFT
were **servable orders wrongly rejected by two tour-capacity bugs**, NOT the "Stoke→London structural gap" I'd assumed:
1. **Undersized proto:** [tour_plan.py](tour_plan.py) `_PROTO_CAPACITY_KG` hardcoded **24000** while the fleet max is
   **28000** (the fleet-ceiling cap fix updated legs.py but MISSED this proto). Six 24-28 t single loads (BS4 27.3 t,
   DL10/NP11 26 pal/27 t, CM23 27.5 t) failed the proto but fit a real 28 t artic → EVAL CAPACITY reject.
2. **Sum-vs-peak:** `evaluate_tour` tracks the true PEAK load (deliveries ride from the depot; DIRECT/pickups load
   transiently), but `select_tour_vehicle` was fed **sum(all jobs)**. A Stoke→London tour = 6 deliveries (19,413 kg at
   depot, fits one artic) + 3 DIRECT moves collected en route → summed to **43,105 kg** → "no vehicle." Actual peak ≤24 t.

**Fixed (TDD, red→green, 8 new tests):**
- [tour_plan.py](tour_plan.py): `_PROTO_CAPACITY_P, _PROTO_CAPACITY_KG = fleet_capacity_ceiling()` (single source of truth,
  auto-tracks the master); the `select_tour_vehicle` call now passes `evaluation.peak_pallets/peak_kg`.
- [tours.py](tours.py): `TourEvaluation` gains `peak_pallets/peak_kg`; `evaluate_tour` computes the peak (max over stops,
  incl. transient pickup/DIRECT loads); `build_tours` already gates on `evaluate_tour`, so it now batches to the real 28 t peak.
- **Constants consolidated** into [../cambridge/config.py](../cambridge/config.py) tour section (`TOUR_COHESION_KM`,
  `LIGHT_TOUR_PALLETS`, `TOUR_ORIGIN_AT_DEPOT_RADIUS_KM`) — one place for tour knobs; the proto capacity is deliberately
  NOT copied there (derived from the fleet master so it can't drift again).

**MEASURED (both weeks, full runs):** NO_FEASIBLE_TOUR **18→0 (wk2)** and **4→0 (wk1)** — the bucket is ELIMINATED.
Coverage **wk2 98.8→99.3% (+~13 orders), wk1 99.2→99.4%**; 0 temporal/0 ledger both weeks. +km is coverage reinvestment
(serving far/heavy orders that were false-rejected) — lexicographic win. Remaining unassigned tail is genuine
(BEFORE_PLANNING_START, NO_FEASIBLE_ROUTE daily shift-bound, BAD_GEOCODE, 2 MASSIVE_UNSUPPORTED) — no tour-capacity rejects
left. Week outputs in `freight_planner/out/`.

## Session log — 2026-07-01 (flow-aware staging depot — imports land at the CB22 gateway, WORKS)

Full brainstorm→spec→plan→TDD ([docs/superpowers/specs|plans/2026-07-01-flow-aware-staging-depot*]).
**Not committed.** freight_planner + cambridge suites green (625). Window 2026-01-12→17 (OSRM, seed 0).

**What & why.** `source_depot` was set from `assign_depot(pc)`, which returns `OVERFLOW` for anything outside a depot
territory; two `nearest_depot` fallbacks ([tour_plan.py](tour_plan.py), [tours.py](tours.py) `_depot_of`) then resolved
OVERFLOW to the **geographically-nearest anchor** — which for anything northern is the **dockless Stoke ST4 satellite**.
So a Palletline **import** to Scotland (EH48 `fbcb92a2`) mis-staged at Stoke even though the freight lands at a member
depot and no Stoke vehicle serves that lane. Root cause: the fallback was **flow-blind** (discarded the import-vs-
collection distinction the pipeline had tracked). Full trace in the spec + [[api-label-is-leg-authority]].

**Telematics evidence (Jan, 5 Stoke vehicles + verified_legs).** "Stoke" is ~1.5 vehicles (BX67ZFV 30/31 nights at ST4;
BU69XGK half; the other 3 overnight CB22). ST4 is a simplified single-vehicle yard (no dock). The fleet is
**collection-dominated** — PL_EXPORT 163 (55%) / FULL_FLEET 123 (41%) / PL_IMPORT **8** — with **ST origins 96%/90%**:
one ST shipper, destinations spraying the Midlands/North. **None serve Scotland.** ⇒ **do NOT widen Stoke territory**
(it keys on origin, already ST=Stoke; widening would only capture imports *into* the corridor and re-create the bug).
One lever only: far/out-of-territory **deliveries** must stage at a resourced gateway.

**Built (Approach B — resolve upstream, single source of truth):**
- [../cambridge/scope.py](../cambridge/scope.py): `GATEWAY_DEPOTS=(CB22,BEDFORD)` + `resolve_staging_depot(pc,*,is_delivery_anchor,lat,lon)`
  — in-territory unchanged (incl. STOKE); OVERFLOW **delivery**→CB22 (capability-primary, user call), OVERFLOW
  **collection**→nearest of {CB22,BEDFORD}. `assign_depot` untouched.
- [legs.py](legs.py): wired resolver into `origin_depot`/`dest_depot`; legs never emit `OVERFLOW` as a dispatchable
  `source_depot` again. Removed now-dead `depot_for_pc`.
- [tour_plan.py](tour_plan.py): `_anchor_or_nearest` — bucketer now trusts ANY real `source_depot` (dropped the
  FULL_FLEET-only condition); `_depot_of` already trusted it, so no change there.

**MEASURED (window 12-17, consolidate ON):** coverage **99.3% (2458/2476)**, 0 temporal / 0 ledger, NO_FEASIBLE_TOUR
**4** (≤ baseline 5). The Scotland tour `Y88WSM:01-13` now **anchors CB22** (was Stoke-tangled): EH48 loads at the CB22
depot_start; the single **STOKE load-stop is for ML6 only** (`0f36bf4b`, genuinely Stoke-staged — picked up by Stoke
daily vehicle B29BAL at ST4). Plan-wide 4 load-stops all on real gateways (CB22/BEDFORD/STOKE), no phantoms. 6 new tests
(scope 6 / legs 4 / tour_plan 2 / tours 1 regression guard). Deferred: collapse collection-side to CB22 if capability
pressure appears. Out of scope: territory widening, `staging_depot`/`run_from_depot` field split, eval-anchor-vs-vehicle-home (H14).

**COLLECTION-SIDE REFINEMENT (same session, 2nd week 01-19→24):** running a 2nd week surfaced that excluding Stoke from
ALL staging over-corrected — 2 northern PL_EXPORT collections (DL10 Richmond, NP11 Newport) moved from Stoke (~160 km,
nearest) to Bedford (~270 km). Fix: collections need no dock, so `resolve_staging_depot` now SPLITS — deliveries→CB22
(dock gateway, Stoke excluded), collections→nearest of **`COLLECTION_BASES` {CB22,BEDFORD,STOKE}** (Stoke re-included as a
northern collection base; new `_nearest_base` helper). 629 tests green. **HONEST OUTCOME:** it correctly re-stages DL10/NP11
at Stoke but does NOT recover them — they stay NO_FEASIBLE_TOUR. **Staging was not their blocker:** isolated far PL_EXPORT
collections don't form a feasible tour (only 1 far collection toured all week). Coverage net-neutral (12-17 99.2-99.3%,
19-24 98.8-98.9%, within the 90 s wall-clock ALNS run-to-run band). Kept as a correctness/symmetry fix; the real lever for
far collections is tour-formation batching (separate — [[multiday-tour-target-xdock-gap]]). Week outputs now land in
`freight_planner/out/` (not scratchpad); trip_app viz regenerated for both weeks.

## Session log — 2026-07-01 (cross-depot tour consolidation — the tour-formation lever, WORKS)

Full brainstorm→spec→plan→TDD ([docs/superpowers/specs|plans/2026-06-30-cross-depot-tour-consolidation*]).
**Not committed.** 285 freight_planner tests green. Window 2026-01-12→17 (OSRM, seed 0).

**What & why.** Multi-day far tours were batched **per source depot**, so two depots' freight bound for the
same region never merged. Spotted live: `X888RNW:01-13` (Bedford) + `W88RNW:01-15` (CB22) both run to Scotland
on 01-15 ~50 km apart — a redundant ~1,000 km round trip. Root cause: per-depot bucketing in [tour_plan.py](tour_plan.py).

**Built** (behind `--consolidate-tours`, default OFF; flag-off path byte-unchanged):
- [tours.py](tours.py): `DEPOT_LOAD` waypoint + `load_stop_job` (zero-load front-of-tour depot visit; `evaluate_tour`
  walks it via its existing `else` branch — km counted, peak load unchanged); `resolve_cluster` — single-depot →
  as today; depot-loadable multi-depot → ONE tour with front load-stops at the other depots (primary = most pallets);
  multi-depot incl. a far DIRECT move → fall back per source depot.
- [tour_plan.py](tour_plan.py): pool ALL far jobs, cluster once vs a **centroid** proto (emergent regions, no postcode
  table), resolve per cluster into a uniform `(depot, jobs, evaluation)` list the assignment loop consumes.

**MEASURED A/B (equal ALNS budget):** OFF 99.0% (2451) / 92,477 km / 33 tours → ON **99.3% (2458) / 93,356 km / 35 tours**.
The Scotland pair **merged 2→1 tours** (W88RNW's redundant run gone); **+7 orders served, 0 newly blocked, 0 temporal/0 ledger**.
km +879 is *reinvestment*: merging freed ~800 km + a vehicle-day, spent serving 7 previously-unservable far orders
(real deliveries). Lexicographic (coverage→km) **strict win**. **CONFIRMS** the [[coverage-is-tour-capacity-bound]] thesis:
tour-formation is the coverage lever (seed-ordering / resolver overrides were not).

**BROADEN TO DIRECT (same session, shipped):** a DIRECT move whose collection **origin is at a depot** (the Stoke
yard — ST4 8JB ≈ ST4 8HP) collects there via its own origin→dest leg, so it is depot-loadable, NOT cross-territory
customer collection. `resolve_cluster` now: (1) only a DIRECT with a *non-depot* customer origin (`_origin_at_depot`,
8 km) forces the per-depot fallback; (2) load-stops + primary come from **delivery** depots only (DIRECT/pickups ride
along via their own legs — no double-visit). **MEASURED (consolidate ON, vs OFF):** 99.0%→**99.3% (+7) for just +55 km**
(was +879 km before broadening — **−824 km**), **NO_FEASIBLE_TOUR 12→5**, 0 newly blocked, 0 violations. The 3
**Stoke-origin DIRECT** far-FF (ST4→IP1/CM1/CB9) are recovered (2 tours now carry Stoke-origin freight). 277 tests green.
Remaining far tail: 5 NO_FEASIBLE_TOUR + the 5 Stoke→**London** crossdock orders (no-trunk modeling gap, separate) +
`f98b3f06` straggler. Still default OFF per user.

**FLIPPED DEFAULT ON** (user: "i thought the default is on"): `consolidate_tours=True`, opt out via `--no-consolidate-tours`.
Main plan + trip_app refreshed to consolidation-on. 287 tests green.

**Trip-app viz (same session):** (1) **load-stop now drawn** — the consolidated tour's cross-depot collection hop
(`DEPOT_LOAD`) is emitted as a real route-stop (unique job_id per tour; depot coords from `DEPOT_ANCHORS` in
[manifest.py](manifest.py)) and rendered as a **yellow load-stop marker** on the line ([viz_app.py](viz_app.py)); the
return leg km is no longer inflated (e.g. Y88WSM Scotland tour: CB22→**STOKE load 186 km**→Scotland, return 602→**416 km**).
Answered the user's Q: routing was always correct (it DID collect at the other depot), the map just wasn't drawing it.
(2) **postcode-pin** search box: type a postcode → 📍 on the map; index built from stop/depot postcodes + **geocoded
unassigned orders** (70/76) so you can locate WHY/WHERE unserved orders are. `_txt` NaN-guard for the load-stop label.

**Scope/decisions:** Customer-origin DIRECT far moves still stay per-depot (no cross-territory collection).
Task 4 (`depot_load` marker) **DONE** (above). ~~Deferred:~~ historical note: (headline
km already honest: the load-stop hop is in the tour eval / route total, just folded into the return leg, not a separate
marker; needs per-tour-unique load-stop `job_id`s to avoid duplicate-assignment). Honest viz polish, low priority.

## Session log — 2026-06-30b (unassigned triage + fleet-max cap fix; seed/Stoke/tour backlog)

All TDD, **not committed**. 271 freight_planner tests green. Window 2026-01-12→01-17 (OSRM, seed 0).

### Unassigned triage (the honest denominator)
Pulled `unassigned_jobs.csv` for the master-caps run: **87 unassigned JOBS → only 27 genuine order misses** (98.9%
coverage). **54 jobs are benign** `BEFORE_PLANNING_START` — pickups whose collection physically happened the prior
week (Jan 8-9) and whose DELIVERY leg is `ROUTED` in-window (manifest uses `ROUTED`/`ACCOUNTING`/`BLOCKED`/`UNASSIGNED`,
NOT `ASSIGNED` — watch that). The 27 real misses, by mechanism:
- **NO_FEASIBLE_TOUR — 11 orders** (biggest lever). 9-12 of the legs FIT one vehicle on size → it's tour
  *formation/availability*, not capacity. → option (3) below.
- **BAD_GEOCODE — 6 orders.** Postcode typos: `MK43 OYL` (letter O for 0), `AL10 9B5` (5 for S), outward-only `SG8`,`CO10`.
- **Stoke corridor — 5 orders.** ST4 8JB→SW-London; → option (2) below.
- **MASSIVE_UNSUPPORTED — 5 orders.** 3 are weight-only ~27 t (the cap bug, fixed below); 2 are 34/41-pallet (genuinely need splitting).

### Shipped (DONE)
1. **Fleet-max capacity ceiling** ([legs.py](legs.py) + [vehicles.py](vehicles.py) `fleet_capacity_ceiling`) — `is_massive`,
   `_status_for_leg`, and `_split_parts` hardcoded **26 000 kg / 26 pal**, but the validated master has **45 artics @ 28 000 kg**.
   So 27 t loads were rejected `MASSIVE_UNSUPPORTED` (network PL_EXPORT/PL_IMPORT legs, which don't split) AND a 27 t
   full-fleet load was needlessly split into two trucks. Fixed by sourcing the ceiling from the master (`(26, 28000)`,
   conservative `(26, 26000)` fallback). 4 new TDD tests ([test_legs_capacity.py](../tests/freight_planner/test_legs_capacity.py)).
   **MEASURED 12-17:** assignment 98.9→**99.1%** (+5 orders: the 3 weight-only ~27 t incl. the hazardous one, + the HP2
   26.5 t tour pair), MASSIVE 5→2 (the 34/41-pal correctly stay), total km 95,156→**94,404** (down — no more needless split).
   0 newly blocked. This is the clean current baseline.

### Backlog (TODO) — three options surfaced by the triage
1. **Greedy seed insertion — alternatives/tunes** (`MEASURED → seed is NOT the lever; change REVERTED`).
   Built a `--seed-strategy` toggle (default `greedy` = baseline, untouched) with two alternatives — `constrained`
   (most-constrained-first ordering: fewest compatible vehicles, then size FFD, then window) and `regret` (regret-2 with
   incremental top-2 cache, tightness tie-break) — all TDD, behind the flag. **A/B at equal ALNS budget (12-17):**
   greedy **99.1% / 91,954 km**; constrained **99.1% / 93,514 km** (same coverage, +1.6k km, zero rescue); regret hung in
   the seed on a wedged OSRM call (0 CPU 17 min — OSRM has no client timeout; not the loop). **Verdict:** the greedy seed
   is already coverage-strong (the `+10000` idle-vehicle bias), so seed *ordering* recovers none of the residual ~22 misses —
   they are **structural** (≈9 tour-formation, 6 geocode, 5 Stoke, 2 oversized). Reverted the whole toggle for a clean tree
   (281 tests). **The coverage lever is tour-capacity (option 3 / Stoke), not the seed.** If ever revisited: add an OSRM
   client timeout first; and judge on post-ALNS coverage, never seed-km.
2. **Stoke far-FF = a TOUR-CAPACITY problem, not a resolver one** (investigated deeply 2026-06-30b, change REVERTED).
   - **Root cause (confirmed):** [options_resolver.py](options_resolver.py) `_cost_choice` keeps XDOCK whenever
     `xdock_km ≤ 1.6×direct_km` (a groupage bias). For the 5 Stoke→London FF, `direct_km ≈ xdock_km` (569≈570) so XDOCK
     wins with ZERO km saving, dropping the DIRECT option. XDOCK is then structurally undeliverable at Stoke (no onward
     trunk / delivery round) → all 5 unrouted (pickup `NO_FEASIBLE_ROUTE`, delivery `DELIVERY_BEFORE_PICKUP`). The DIRECT
     carry IS `is_tour_only=True`, so forcing DIRECT routes it to a sleep-out tour. NOT a resource gap (STOKE anchor + 5 tractors exist).
   - **What I tried + measured:** a per-depot trunk-awareness flag forcing Stoke FF→DIRECT (+ Stoke PL→HUBDROP). Two
     dead ends: (a) forcing **Stoke PL→HUBDROP** sent all **103 Stoke-staged PL exports** on individual hub runs →
     cross-depot 5k→26k km, coverage 99.1→**96%** (REVERTED). (b) **FF→DIRECT alone** is **capacity-bound net-neutral**:
     it serves the 5 London FF but displaces 5 East Anglia FF (ST4→CB9/IP1/CM1) into `NO_FEASIBLE_TOUR` (assignment flat
     at 2454, +9k km). There are ~10 far multi-day Stoke FF but only ~5 Stoke tour-vehicle slots — the resolver just
     **reshuffles which ones win the vehicles**. Whole change REVERTED to the clean 94k/99.1% baseline.
   - **The real fix:** Stoke far-FF coverage needs **more tour capacity / cross-depot backhaul** (Stoke→East-Anglia
     freight piggybacking returning home-depot vehicles) — i.e. it's the SAME root as option (3) tour-formation. Do them
     together. The resolver DIRECT-forcing is a necessary *prerequisite* but worthless without the capacity. Keep Stoke PL
     on the free TRUNK default until real Stoke trunk modeling lands (don't naively HUBDROP).
3. **Tour formation** (the 11-order NO_FEASIBLE_TOUR bucket). 9-12 of these legs fit one vehicle on size, so the blocker is
   tour *availability/formation*, not capacity. Dig into `select_tour_vehicle` / tour bundling / the geometry gate — why feasible
   far singletons (incl. near-depot DIRECT moves like SG1 2FW @ 28 km, IP3 9SJ @ 72 km) can't get a tour vehicle.

## Session log — 2026-06-30 (tour gate + kg cap + sector normalization + viz collect-stop; trunk backlog)

All TDD, **not committed**. 264 freight_planner tests green. Window 2026-01-12→01-17 (OSRM, seed 0).

### Shipped (DONE)
1. **Tour-eligibility gate fix** ([tour_plan.py](tour_plan.py)) — `is_tour_only` was gated on the fleet's
   LONGEST elapsed shift (`shift_min`=905 min/15.1 h), not the 10 h DRIVING cap (600 min). Far singletons
   whose round-trip busts the driving cap but fits the shift fell through BOTH phases → `NO_FEASIBLE_ROUTE`.
   Fix: gate on `min(shift_min, _DAY_DRIVE_CAP_MIN)`. Result: NO_FEASIBLE_ROUTE 15→1, assignment 97.5→99.3%,
   tours 7→21, total km 82,059→80,950 (DOWN — batching far work beats failed daily round-trips). Lone
   remaining NO_FEASIBLE = the ST4→KT9 pickup/delivery dependency tangle.
2. **Tour kg-cap enforcement (lever A)** ([tours.py](tours.py) `select_tour_vehicle`/`_fits` + `Tour.total_kg`)
   — tours were feasibility-tested on a 24 t PROTO vehicle then assigned on PALLETS ONLY, so heavy tours
   (e.g. 16 t York) landed on rigids → 8 trips >100% kg-util (max 1004%). Selection now requires
   `capacity_kg ≥ tour_kg`. Result: kg-overloaded trips 8→0, coverage held 99.3%. Daily evaluator already
   enforced kg; this closed the TOUR hole. (Lever B — capacity_kg = observed-p95 not physical — still open.)
3. **Sector-aware postcode normalization** ([verify_legs.py](../planning_agent/verify_legs.py)) — `MIN_PREFIX=5`
   dropped 4-char sectors; a 3-char outward (KA6 → sector KA65, 4 chars) was discarded, so far Scotland/North
   deliveries matched only the local origin → tagged COLLECTION not FULL_FLEET. Fix: sector = outward+1
   (`_sector_len`), keep ≥3-char sector pings. Regenerated verified_legs (**FULL_FLEET 1469→2567, +1098**;
   WT255768 Scotland flipped COLLECTION→FULL_FLEET) + rebuilt enriched. End-to-end: total km 80,950→**97,750
   (+21%**, the real full-fleet legs we were hiding), tours 21→37, full-fleet option sets 199→397, coverage
   99.3%. Backups: `verified_legs.before_sectorfix.csv`, `enriched…before_sectorfix.parquet`.
4. **Viz collect-stop** ([viz_app.py](viz_app.py); `collect_pc` threaded leg→candidate→route_stops) — a
   two-point DIRECT/HUB-DROP move rendered only its DELIVERY end, so the collection (e.g. Manchester M12 5DD
   on a Stoke run) had no marker/list row and a real 158 km move looked like "doing nothing at ST4 8JB".
   Now splits into collect+deliver stops (diamond marker at the true collection point, drive km on the
   collect leg). New `collect_pc` route_stops column (105/105 direct moves populated). NB the 19-stop
   ST4 8JB trips are CORRECT — Building Adhesives Ltd (1,658 orders Jan–Feb) / ARDEX consolidation.

### Vehicle master — single source of truth for capacity (+ MOT), WIRED IN
**Problem:** the planner's `capacity_kg`/`capacity_pallets` came from telematics **observed-p95**
(`vehicle_profiles_derived.json`) or asset-type defaults — wildly wrong (vans defaulted 10t/15pal;
a 26t rigid showed 4.6t/11pal from light running; artics 15t/18pal). This caused the weight-util
blow-ups and starved the tour kg-cap of real numbers.
**Built** [planning_agent/build_vehicle_master.py](../planning_agent/build_vehicle_master.py) →
**[freight_planner/data/vehicle_master.csv](data/vehicle_master.csv)** (98 veh/79 active, per-field
provenance). Pipeline of sources:
1. **Supatrak** enriched list — roster, AssetType, make/model, gross GVW/GCW, depot. (NB its
   `max_tonnes` is mostly a PLACEHOLDER — 26t for ~every lorry, 70t tractors — so NOT used for capacity.)
2. **Jigsaw** `vehicleGroup2_Name` = per-vehicle operating **tonnage class** (44/26/18/12/7.5 TONNE/VAN),
   **AssetType-guarded** (tractor⇒artic, lorry⇒rigid; catches jigsaw mis-groups like a D-Range tagged 44t).
3. **Online UK class standards** (sourced) → payload_kg + pallet_capacity per class (44t→28t/26pal,
   26t→15t/18, 18t→9.5t/14, 12t→6.5t/12, 7.5t→3.5t/8, van→1.2t/3).
4. **Telematics axle-weight cross-check** (`derived_capacity_kg_max`, has_AxleWeight) — validated all
   classes (no axle vehicle exceeds cap); dropped sensor glitches (one read 5,991,730 kg); pallet maxes
   are daily/multi-trip totals so reference-only.
5. **4 validated overrides** (`VALIDATED_CLASS`): N88RNW→18t (axle ~9.2t); BF65WBY/FJ72XFF (DAF LF 150 FA
   = 7.5t, reg-plate + Qargo per-trip max 2.6t); RF22HRO (LF180 FA, 7.5t MEDIUM, ambiguous 7.5-16t).
6. **MOT** — Selenium scrape of check-mot-history.co.uk (real Chrome clears AWS WAF; randomised 1.2-3.5s
   sleeper), cached [planning_agent/mot_results.csv](../planning_agent/mot_results.csv) → `mot_model`,
   `mot_expiry_date`, `mot_expired`, joined on rebuild. **0 expired**; 15 no-record (HGV annual-test
   tractors / cherished 888-plates — not MOT scheme).
Final: confidence HIGH 65 / MEDIUM 14 / REVIEW 0.
**WIRED** [freight_planner/vehicles.py](vehicles.py) `_resolve_capacity`: capacity_kg←payload_kg,
capacity_pallets←pallet_capacity from the master (source `vehicle_master`), observed-p95 profile only a
fallback for any vehicle absent. 267 tests green (TDD). Re-run 12-17 to measure the assignment/util shift.
**Reproduce:** refresh MOT via the Selenium scraper → `mot_results.csv`; `python -B planning_agent/build_vehicle_master.py` → `vehicle_master.csv`; capacities flow into the planner automatically.

### Backlog (TODO) — model Stoke-vs-others trunk to the Palletline/Hazchem hub
Trunk to the hub (B37 Palletline / LE10 Hazchem) **is our work** (deliver collected exports to the hub;
collect imports from it). Today the depot↔hub trunk legs (`OUTBOUND_TRUNK`/`INBOUND_TRUNK`) are emitted
`customer_dispatchable=False` (accounting-only — km NOT counted), and `resolve_hub_drop` always picks TRUNK
over HUBDROP (0/804) because the trunk km is free in that comparison.
- **Operational rule (USER):** CB22 & Bedford run a DEDICATED EVENING TRUNK (collect→depot all day →
  consolidated depot→hub run at night). **Stoke has NO trunk**: full-fleet → we deliver end-to-end;
  Palletline → the collecting vehicle drops at the hub DURING normal ops (= HUBDROP inline).
- **Already built:** hub nodes (geocoded B37 7HB / LE10 3BS), all 3 leg kinds, TRUNK-vs-HUBDROP
  mutual-exclusion option ([options_resolver.py](options_resolver.py) `resolve_hub_drop`), ledger states
  (AT_DEPOT/AT_HUB/WITH_NETWORK). HUB_DROP is ALREADY routable.
- **Plan:** (a) per-depot `has_evening_trunk` flag; Stoke→force HUBDROP in `resolve_hub_drop` (EASY, reuses
  the existing routable leg) + a hub→customer inbound-direct variant for Stoke imports (EASY-MED).
  (b) CB22/Bedford evening trunk as ROUTED consolidated work = a deterministic rule-based pass (aggregate a
  depot's day PL freight → `ceil(pallets/cap)` trunk runs depot→hub→depot, reserve a tractor, count km) —
  MIRRORS [tour_plan.py](tour_plan.py). MEDIUM.
- **Implication:** routing the trunk ADDS km (the nightly B37 runs — "8/9 Bedford artics trunk nightly")
  → planned moves closer to actual, like the full-fleet recovery did.

## Session log — 2026-06-29 (per-type cost + single-day validation + verified_legs repair)

Three threads, all TDD (~270 tests green). **Not committed** (docs-only update per user).

### 1. Per-type generalized cost (objective layer 1)
- ALNS objective was pure km. Real fuel cost differs by type (Jigsaw cards, Jan tank-to-tank):
  **44t artic 0.319 GBP/km, rigid 0.216, van 0.150** (artic ~1.5x rigid). Old cambridge pipeline
  HAD per-type cost (`profitability_report/vehicle_cost_rates.json` + `MULTIDAY_COST_PER_TRACTOR=200`);
  the new pipeline dropped it.
- New [vehicle_cost.py](vehicle_cost.py) (`fuel_cost_per_km`, derived rates, `FREIGHT_FUEL_UNIFORM`
  env toggle = old km behaviour for A/B). `solution_cost` + insertion deltas + `improve_solution`
  now rank on per-type GBP; reported km stays physical (`km_after` recomputed; `cost_before/after`
  added). A/B (same seed): coverage identical, work shifts to cheaper types (tractor km share down,
  van up), modest cost cut. Default ON.
- KEY: fuel/km ALONE does not fix rigid-long-hauls (rigids are cheaper/km) — needs driver-hour +
  standing-day layers (deferred) and catchment (B15).

### 2. Single-day route validation (`viz_app --validate`)
- New [vehicle_actuals.py](vehicle_actuals.py): per-vehicle actual km from telematics. **Odometer
  (`CANbusData_Odometer`, in MILES)** is the truth (haversine-of-pings undercounts ~3%);
  `actual_km_by_vehicle(prefer_odometer)`, `normalize_pc` (outward code), `visited_postcodes`.
- `viz_app --validate` scorecard: planned-vs-actual km (same-set, odometer), vehicle-days,
  seed->ALNS delta, **postcode stop-coverage**; `run_alns` writes `validation_metrics.json`.
- Gap (01-15): headline 27% planned>actual = ~12pt no-telematics accounting asymmetry + ~3pt
  haversine method + **~11% genuine over-plan**. Plan credible (98.3% district coverage; 2 misses
  = far singletons reality offloaded).
- Vehicle count: we plan 70, reality used **69** (fleet ∩ telematics). The earlier "58" was
  planned∩movers (WRONG). So we do NOT over-use vehicles. Real diff = trips/vehicle (reality 1.43,
  profile median ~1.9; us 1.03) — reality runs more/smaller/tighter trips (multi-trip reload), we
  run fewer/bigger -> the ~11% extra km is trip-COMPACTNESS, not count. So the standing-cost /
  consolidation lever is weaker than first pitched.

### 3. verified_legs audit + repair (the big one) — see [[verified-legs-data-repair]]
- USER INTENT: verified_legs/enriched is a **data-repair layer treated as truth in forward mode**,
  patching Qargo's missing "which leg is ours" field — NOT replay-only (overrides the old
  movement-leg-architecture.md note).
- Audit flaws: consumer ignored confidence; ±8h window; outward (MIN_PREFIX=3) match; first-index
  substitute; `ships==powered=>full` (only 30% true); no service date.
- Retune ([verify_legs.py](../planning_agent/verify_legs.py), offset-study-driven): anchor
  `*_timestamp_local` -> `*_end_timestamp_local` (NEVER `requested_start` = midnight placeholder,
  +10-13h off); windows ±2h deliver / ±3h collect (+3h if placeholder); MIN_PREFIX 3->5 (sector);
  **emit `service_date`** (100%).
- **STRUCTURAL RULE** (user's idea, validated): `ships > powered` (comma-split `shipment_names` vs
  powered regs) => SINGLE leg (**99.6%**); direction via distance (**98.4%**) -> MEDIUM
  `structural_single`. `ships==powered` NOT asserted full. Resolved ~89% of the untracked tail.
  (`is_partial_fleet_unknown`/`_is_structural_full_fleet` already existed in cambridge/scope.py.)
- Consumer fix: `cambridge.verified_legs.corrected_flow` treats `UNVERIFIED` as no-override (was KeyError).
- Regenerated (Jan+Feb, 19458) + rebuilt `enriched_orders` parquet. orig->retune->structural:
  usable(HIGH+MED) 95%(loose)->67%->**96%**; pure-guess 5%->30%->**0%**; dated 0->**100%**;
  FULL_FLEET 2948->**1469** (over-classification halved). End-to-end (re-run 01-15): DIRECT/XDOCK
  options & direct-moves ~halved (they only come from full-fleet), coverage 98.2->98.5%.
- Backups: `verified_legs.{before,retune}.csv`, `enriched_orders...before.parquet`; plan `out/validation_v2`.
- Confidence is now PROVENANCE metadata (observed/structural/inferred), NOT a gate-to-raw (raw is
  the unreliable input we replace). RF22HRO: a 369km real day, 0 planned — most were PL_EXPORT whose
  Qargo `destination_date` is Palletline's ONWARD delivery, not our (earlier-dated) collection leg.

Resume: forward-mode for FUTURE orders needs the structural rule live (not just baked in the CSV).
Deferred: driver-hour+standing cost layer, catchment (B15), promote validated structural_single.

---

## Session log — 2026-06-27d (viz_app rich map + opening-state ledger fix)

Built the improved trip map and, via eyeballing it, found + fixed a real correctness gap.

- **A2 v2** — [viz_app.py](viz_app.py): custom-Leaflet app (not folium) off a run's `plan/`.
  Trip-focus dimming (click a trip → others fade to 5% + stops hidden; single-select so the
  sidebar always shows one trip), numbered stop sequencing on the selected trip, shape-by-kind
  (circle delivery / diamond collection / square direct) with an on-map legend, rich sidebar
  (drive/pallet/weight utilisation bars, freight states, run summary, **assignment rate**,
  unassigned panel), filters (day / vehicle search / trip type / util band). 4 viz tests.
  `python -m freight_planner.viz_app --plan-dir <plan> [--date ...]`. (folium `viz_map.py`
  retained for the simple/compare maps for now.)
- **Opening-state ledger fix (run-to-run state gap, new pipeline)** — eyeballing 12-17 showed
  74% of unassigned were temporal, not capacity. Root cause: freight collected BEFORE the
  window (53/58 orphaned deliveries) seeds `AT_CUSTOMER_ORIGIN` (the pre-window pickup leg is
  in the window-filtered set) → pickup hard-blocked BEFORE_PLANNING_START, delivery orphaned
  DELIVERY_BEFORE_PICKUP. Fix (3 parts, all gated on `planning_start`):
  1. [state.py](state.py) `build_initial_freight_states(..., planning_start)`: pickup/direct
     collected pre-window → seed `AT_DEPOT_OR_HUB_PENDING` at the delivery depot, ready_time =
     collection time (ready all window, not the delivery date).
  2. [jobs.py](jobs.py) `_dependency_maps(..., planning_start)`: pre-window pickup → delivery
     becomes `PRESTAGED_DELIVERY` (no `REQUIRES_PRIOR_PICKUP` on a never-planned pickup).
  3. [options_resolver.py](options_resolver.py) `resolve_options(..., planning_start)`: force
     **XDOCK** for pre-collected freight (DIRECT origin-collect is impossible once at depot —
     this was crashing `deliver_direct` with `FreightUnavailableError`).
  Wired `planning_start=start` through run_alns/build_phase0/run_seed/run_route_seed.
  **Result (12-17, OSRM):** assignment 96.3% → **98.4%** (2444/2484), DELIVERY_BEFORE_PICKUP
  62 → 5, 0 ledger / 0 temporal violations, no crash. 236 tests green (+4 state-gap/viz tests).
  Residual unassigned: 60 BEFORE_PLANNING_START are the pre-window pickup *legs* (pre-done;
  orders served) — cosmetic, could be suppressed from the unassigned view later.

---

## Session log — 2026-06-27c (B8 — SA retest + regret-2, both measured)

With throughput fixed the search finally iterates enough to retest the acceptance/repair
levers. Both measured on the pickled seed (fair back-to-back A/Bs); **230 tests green.**

- **SA retest** — now *marginally positive* (was inert). Sweep at 90s (rep=20):
  sa_temp 0.0 → 3,811 km / 78 moves; 0.001/0.005/0.02 all → 3,822 km / 89 moves
  (identical; best-tracking converges). Gain ≈ +11 km (+0.3% of the savings). Enabled a
  small default: **`--sa-temp` 0.0 → 0.005**. Never worse here (best-tracking protects).
- **Regret-2 repair** — implemented (`_ranked_inserts_for_job` + regret loop on the base
  km spec only; eviction-bearing coverage specs keep greedy order) behind `regret_repair`
  / `--regret-repair`. **Measured net-NEGATIVE** at our destroy sizes: A/B 90s →
  off 3,296 km (5.3%) / 70 moves vs **on 2,385 km (3.8%) / 53 moves**. Recomputing top-2
  for every pending job costs ~k× the repair → fewer iterations/budget, and reordering
  2–5 jobs doesn't offset it (regret shines on *large* insertion batches, not ours).
  Kept correct + tested but **default OFF**; revisit if destroy sizes grow.

Standing lesson reinforced (3rd time): **km-per-wall-second** is the objective, not
km-per-iteration — a time budget rewards cheap iterations over clever-but-slow ones.
Note: absolute % wobbles ~5–6% with machine load (time-budget → iteration-count), so
trust back-to-back A/Bs, not cross-run absolutes.

---

## Session log — 2026-06-27b (B8 throughput — the optimizer can finally iterate)

Profiled the ALNS loop (cProfile on the real window via a pickled seed) and fixed
the binding constraint. **228 tests green.** End-to-end (2026-01-05→01-10, haversine,
90s budget): **km saved 428 (0.7%) → 2,832 (4.6%), accepted moves 7 → 60** — a 6.6×
jump, now in the range a working ALNS should reach. Coverage unchanged (2263).

**Diagnosis:** `evaluate_route` was called ~4,700×/iteration → only ~20 iter/s, so a
90s budget barely iterated. Two causes, two fixes:
1. **Per-call cost** — `evaluate_route` re-parsed the same ISO datetime strings
   millions of times and formatted per-stop arrive/depart strings the search discards.
   Fixes: `@lru_cache` on `_parse` ([routing_adapter.py](routing_adapter.py)); a
   `detail=False` fast path (skips arrive/depart formatting, keeps km + feasibility +
   stop ordering) used by all ALNS-internal `evaluate_route/evaluate_day/try_insert_job`
   calls; final plan emit stays `detail=True`. Guard test asserts the two agree on km.
2. **Call count (dominant)** — the coverage-repair block re-attempts the ~18
   un-insertable rejects every iteration (~40 destroy/repair specs/iter). Fix:
   `repair_every` cadence — every iteration still does the cheap km move; coverage-repair
   runs every Nth. Measured (40 fixed iters): rep=1 = 17.0 s/iter, rep=20 = 1.3 s/iter
   (13× faster). At equal iters rep=1 saves more km, but **per wall-second rep=20 is
   ~7.5× better** — and a time budget is the production reality. `--repair-every`
   default **20**; rep=1 reproduces the old every-iteration behaviour (unit-test default).

**Next levers unchanged:** regret-2 repair, 2-opt/or-opt intra-route, revisit SA (now
that iterations are plentiful SA may finally have room to help — worth a retest).

**Run (current best):**
```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -B -m freight_planner.run_alns --start 2026-01-05 --end 2026-01-10 --router haversine --time-budget 90
```

---

## Session log — 2026-06-27 (B8 phase 2 — real ALNS operators)

Built the missing two-thirds of canonical ALNS and validated km gain on the
2026-01-05→01-10 window (haversine, same seed), **226 tests green**:

- **B8 ph2** ✅ destroy operators + adaptive weights in `alns.improve_solution`:
  - `_worst_removal` — rips out highest in-trip-detour jobs (cheap detour score,
    `y**p` randomisation) so repair can rehome them
  - `_shaw_removal` — removes a spatially-related **same-day** cluster so repair can
    re-consolidate onto one passing vehicle (stays in-day; cross-day can't consolidate)
  - `_AdaptiveOps` — Ropke–Pisinger reward tiers (33 new-best / 9 better / 13 accepted),
    weights reblended every 50 iters → search leans on whatever's paying off
  - removal size bumped to k=2..5 (was 1..3) to give clusters room
- **Result (same seed, haversine):** old random-only ALNS = 155 km / 0.2% / **1** move;
  new operators = **428 km / 0.7% / 7 moves**, +1 coverage (2059→2060). Optimizer went
  from inert to actually working (~2.7× km, 7× moves).
- **SA still neutral** at the 90s budget: off / warm (0.002) / hot (0.02, cool 0.9995)
  all return the **identical** 428 km / 7 moves. Best-tracking protects the result;
  exploration just can't run enough iterations to beat hill-climb. `--sa-temp` stays
  **default 0.0** (deterministic, cheaper) — SA infra retained, revisit after throughput.

**Diagnosis — why only 0.7% (next levers, in priority order):**
1. **Throughput** is the binding constraint. With ~2,261 jobs the per-iteration repair
   (day re-evaluations across eligible vehicles) limits us to few destroy-repair cycles
   in 90s, so most of the solution is never touched. Profile repair, then cache/cheapen.
2. **Regret-2 repair** — deferred this session: removal sizes are small (k≤5) so regret's
   payoff is modest here, and it interacts delicately with the dependency/ejection
   ordering. Worth it once removal sizes grow / throughput improves.
3. **Revisit SA** once 1–2 land and iterations are plentiful.

**Run (the validation comparison):**
```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -B -m freight_planner.run_alns --start 2026-01-05 --end 2026-01-10 --router haversine --time-budget 90
```

---

## Session log — 2026-06-26 (full pipeline review + first execution pass)

Reviewed all 13 pipeline stages (section C) then executed the instruments +
first M8 capabilities. Shipped, each verified end-to-end (0 ledger / 0 temporal
violations, every order accounted, 98.9% assigned) and **222 tests green**:

- **B12** ✅ period-scoped output folders `…/<start>_to_<end>/` + `run_manifest.json`
- **B13** ✅ first-class `trip_id` (`ROUTE:VEH:DAY#T1`) in plan + manifest
- **A1 ph1** ✅ OSRM road costs across the optimizer (one `road_km` chokepoint);
  batch pre-warm at startup (~104s once/window, cached; OSRM server localhost:5000),
  `--router {osrm,haversine}` (default osrm), haversine fallback + count
- **B6** ✅ multi-day on-vehicle DIRECT option — 249/299 FF orders are multi-day
  and now choose direct vs crossdock (was forced crossdock); closed B5 residual
- **B14** ✅ `is_tour_only` two-point fix (DIRECT carry classified on origin→dest)
- **B8 ph1** ✅ SA acceptance + best-tracking — but **inert** (ALNS accepts ~2
  moves/run); real lever is neighborhood operators → **B8 phase 2 is the resume point**
- **B2** re-verified → folded into B6/B4

**Next session resume point:** B8 phase 2 (relocate/or-opt + 2-opt + related/worst
removal), then turn SA back on. See B8 entry.

**Run (forward, OSRM):**
```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -B -m freight_planner.run_alns --start 2026-01-05 --end 2026-01-10 --time-budget 20
```

---

## Solver optimisation roadmap (future paths)  — *paused 2026-06-27, current state is good*

Where the ALNS stands: greedy seed + worst/Shaw destroy + adaptive weights + cadenced
coverage-repair + small SA. End-to-end ~4–6% km saved over the seed (varies with machine
load on the time budget), 0 ledger/0 temporal violations, ~99% coverage. The levers below
are ordered by expected value-for-effort. **Overarching rule learned 3× this session:
optimise km-per-wall-second, not km-per-iteration — a time budget rewards cheap iterations.
Always A/B back-to-back (absolute % drifts with CPU load).** Validate every change with the
pickled-seed harness (`scratchpad/build_seed.py` → fast experiments, no rebuild).

1. **2-opt / or-opt intra-route moves** *(next; untried family)* — uncross a single route
   and relocate short chains within/between trips. Cheap per move, complements the
   inter-route consolidation that already works. Likely the next real km.
2. **Throughput round 2** *(high; `evaluate_route` still ~280k calls/run dominates)* —
   (a) **incremental day evaluation**: only re-evaluate the changed trip, reuse cached
   per-trip km for the rest of the vehicle-day (today `evaluate_day` re-evaluates every
   trip); (b) **delta-evaluation** of an insertion (marginal km without a full re-eval);
   (c) cap `eligible_vehicles` to nearest-N in repair. Each could 2–5× iterations → more km.
3. **Larger destroy + regret, together** *(medium; we proved regret alone is net-negative
   at k≤5)* — bigger destroy (k≈8–15) gives regret enough to amortise its ~k× cost. Test
   the **pair**, not regret alone. If revisiting, also make regret incremental (recompute
   only affected jobs, not all-pending each step). Flag already exists: `--regret-repair`.
4. **Set-partitioning matheuristic polish** *(high ceiling; do after 1–3)* — pool every
   route the ALNS generates, solve a set-cover MIP to pick the best non-overlapping
   combination. The literature's "last few %"; needs a rich route pool first.
5. **OSRM duration model (A1 phase 2)** — feasibility (time windows / shift / driving cap)
   still uses `drive_minutes(km)`; switch to OSRM travel *time*. Changes the feasible set
   to be more honest, not just the distances. Also unify the tour model onto the same costs.
6. **Better acceptance schedule** — try late-acceptance hill-climbing (LAHC) or
   record-to-record travel as parameter-light alternatives to SA (SA is only marginal here).
7. **Parallel multi-start** — `--restarts` exists but is serial; run restarts across cores
   and keep the best for equal wall-time quality.
8. **Multiday-tour improvement loop** — tours are build-only today (no ALNS over them);
   an improvement pass could trim tour km (~2.4–3.5k km/window currently untouched).
9. **Adaptive destroy size** — grow k when the search stalls, shrink when it's productive.

Deferred/■ infra already in place to build on: SA acceptance + best-tracking, adaptive
operator weights, `repair_every` cadence, `detail=False` fast path, `_ranked_inserts_for_job`.

---

## A. Big-ticket builds

### A1. OSRM wiring — `PHASE 1 DONE` ✓ *(2026-06-26)*
> Verified end-to-end on Jan 5-10: `--router osrm` → 0 fallbacks (cache fully
> warm), 0 ledger/0 temporal violations, every order accounted, 98.9% assigned,
> total 64,665 km (OSRM) vs 64,477 km (haversine) — ~0.3% apart, confirming the
> old ×1.3 factor was well-calibrated on average; the win is per-leg honesty (one
> borderline assignment flipped). 221 tests pass. Phase 2 items below remain.
Phase 1 DONE: `route_costs.road_km` now delegates to an installable road router
(default = haversine, so tests stay deterministic). Runners get `--router
{osrm,haversine}` (default osrm). Reuses the shared `simulation.routing`
`OSRMRouter` + its 29 MB pair cache + live-query/haversine fallback — one source
of truth. So the evaluator, seed, ALNS, options resolver, and cross-depot all
optimise honest road km via the single `road_km` chokepoint.
- _Perf finding:_ a week touches ~873 unique coords; the cambridge-built cache
  missed **77%** of their pairs → lazy per-pair live queries **stalled** the run.
  Fix: **batch pre-warm** at startup (`osrm_setup.warm_osrm_for_run` →
  `route_costs.warm_and_install_osrm` → `build_osrm_matrix` over all run coords),
  ~104s one-time, then persisted — subsequent runs hit the cache and are instant.
- Fallback policy (explicit, per README Geography): OSRM live for misses; if the
  server is unreachable, OSRMRouter degrades to haversine and counts fallbacks
  (printed in the summary). `--router haversine` forces the offline model.
- **Phase 2 (deferred):** (a) thread OSRM *duration* so drive time is real, not
  km/const-speed; (b) unify the tour model (`tours.py` still uses straight-line +
  `MULTIDAY_AVG_SPEED`) onto OSRM (F12.5); (c) OSRM-back the compatibility screen
  (`compatibility.py` still haversine, F7.3). Tests: 2 new in `test_route_costs.py`.

### A2. Visualization / trip dashboard — `v1 DONE` ✓ *(2026-06-27)*
> **Built** [viz_map.py](viz_map.py) — two self-contained folium HTML views off a run's
> period-scoped `plan/` folder (no server). `python -m freight_planner.viz_map --plan-dir
> <plan> --mode trips|compare [--date ...] [--vehicle ...]`. 4 tests; 234 suite green.
> - **`build_trip_map`** (mode `trips`) — per-trip *planned* view: every trip
>   (`route_id#T{trip_index}`) is a toggleable LayerControl layer (the "display specific
>   trips" ask), colored by vehicle, OSRM road-snapped, direct moves drawn depot→collect→
>   drop, stop popups (trip/stop/order/pc/times/pallets). Reads `route_stops.csv` only —
>   coords already in the plan, **no geocoding**. Validated 01-12: 45 trips, 491 markers.
> - **`build_plan_vs_actual_map`** (mode `compare`, one date) — old comparison ported to
>   the new pipeline: planned (solid) + telematics actual (dashed). Vehicle identity is
>   plan `vehicle_id` == telematics `AssetName` (direct match, 36/43 on 01-12). Fleet
>   scope = vehicles the planner uses across the window. Highlights the missing-trip
>   signal: **fleet vehicles that moved but weren't planned that day** → red "⚠ actual —
>   NOT planned" layer (01-12: 30 such). Reuses `fleet_replay_data` telematics loaders.
> **Next (deferred, user "comes later"):** richer filters (multi-select trips, by region/
> depot), per-trip KPI side panel, auto-emit from run_alns, week-level compare summary.
Decision history: form = **static HTML map per run** (existing cambridge pattern). Data
layer used: `route_stops.csv` (ordered stops + coords + `trip_id`, collect coords for
direct) + OSRM `get_route_geometry` (road-snapped polylines).

### A3. Trunk as a separate, first-class leg — `TODO`
Trunk is currently not modeled as its own dispatchable/resourced movement.
- README intent: trunk is a *movement leg* after pickup succeeds, not a label
  that hides the customer pickup. `OUTBOUND_TRUNK`/`INBOUND_TRUNK` exist in the
  leg schema but are excluded from the planning_window basis and not optimized
  as a resource.
- Decide (per design Q5/Q3): scheduled fixed trunk vs trunk vehicles optimized
  jointly with customer work.
- Tie to A1 (trunk timing/cutoffs) and the multiday/state work below.
- Relates to: `multiday-tour-target-xdock-gap`, M7b deferred Option-B hub-drop.

---

## B. Known gaps / bugs (surfaced before this session)

### B1. Run-to-run state handoff — `TODO`
Multi-day planner has NO week-to-week state handoff; each run bootstraps fresh.
Deferred next step = carry in-flight tours + already-collected freight set into
the next window. This is the correct boundary where the `planning_window` filter
stops (the one prior-window pickup `7cce8393` stays gated until this lands).

### B2. FF_XDOCK_DELIVER far deliveries — `RE-VERIFIED → subsumed by B6/B4` *(2026-06-26)*
Re-verified empirically (Jan 5-10, OSRM run): tours DO build and carry deliveries
(not a hard "LOCAL dead-end bypass" anymore), but ~16 customer-deliveries/week
still fall out as `NO_FEASIBLE_ROUTE` — far work that left daily routing and was
not rescued onto a tour. So no longer a standalone bug; it's the far-work→tour
gap that B6 (multi-day options) + B4 (horizon) + tour classification will close.
Revisit the 16 after B6/B4.

### B3. Cross-day / idle-hour repositioning not modeled as state — `TODO`
Old pipeline had idle-hour repositioning + deadhead return for remote vehicles
(`cambridge/repositioning.py`). New planner should express these as normal
vehicle-state transitions, not post-routing corrections. Currently absent.

### B4. M8 horizon multiday planning — `TODO`
Replace any remaining hard tour pre-commitment with horizon-level vehicle
scheduling; far/multiday work competes with same-day tractor demand in one
planning state; vehicles unavailable on days they're physically away. Includes
the deferred dynamic Option-B hub-drop from M7b.

### B5. Window date precedence — `REASSESSED → mostly WONTFIX, one open decision`
> Operating model (confirmed by user): forward mode only; the broken dataset
> doesn't say which leg was ours, so the **enriched verified leg is treated as
> operational truth** (we know which job is ours, like a real operator). That
> truth covers *responsibility* (which leg), NOT *timing*.
>
> Re-eval of the original "hindsight leak":
> - For **window membership**, actual-first (`origin_timestamp_local` first) is
>   DEFENSIBLE under this model: it lands each order on the day it really
>   operated, and the requested timestamps are often placeholders (00:00 / 06:00
>   day-markers) that would scatter orders onto wrong days. **Not a bug.**
> - The original "must prefer requested" call was too strong. Downgraded.
>
> DECISION (user, this session): the DIRECT vs CROSSDOCK axis is **"did the
> freight touch a depot,"** NOT same-day vs multi-day.
>   - DIRECT  = freight stays on the vehicle origin→destination (one trip/trip-ID).
>     Same-day normally, but ALSO a multi-day sleep-out where the vehicle never
>     returns to a depot — freight was never staged.
>   - CROSSDOCK = freight passes through a depot (collect-in, stage, deliver-out).
> So multi-day alone must NOT force crossdock. See B6 for the resulting gap.
`demand.py first_date()` priority is `origin_timestamp_local →
origin_requested_start_timestamp_local → origin_date`. In `cambridge/scope.py`
the first column is explicitly the **actual achieved** time (`actual`) and the
second is the **requested** time. So in the default `forward_structural` mode,
window membership (`in_window`) **and** the `collect_date`/`deliver_date` fields
are keyed on when the job *actually ran*, not when it was requested — exactly the
hindsight the README forbids as a forward-planning input.
- Only bites when forward mode runs over historical data (our validation harness)
  because in a true forward run the actual column would be empty and naturally
  fall through to requested. But that *is* how we validate, so it matters.
- **Fix:** port the old `_pickup_anchor_timestamp` semantics
  ([scope.py:611-633](../cambridge/scope.py#L611)): prefer `requested`; fall back
  to `actual` only when `actual.date() != requested.date()` (genuine reschedule);
  use `actual`/`origin_date` only when `requested` is missing. Apply symmetrically
  to the delivery side. Consider whether `backtest_verified` mode should keep
  actual-first (it validates against what happened) — i.e. make precedence
  mode-aware.

---

### B6. Multi-day on-vehicle direct (sleep-out) option — `DONE` ✓ *(2026-06-26)*
> Implemented: `legs.py` multi-day FULL_FLEET now emits a `DIRECT_CUSTOMER_MOVE`
> option (carry-on-vehicle, served on the delivery day, carried from collection)
> as a mutually-exclusive group vs the depot crossdock pair. The resolver +
> tour/daily path choose; substrate already executed it (C12/F12.1). Verified
> Jan 5-10: **249 of 299 FF orders are multi-day** (so this is the majority path,
> previously forced to crossdock); resolver chose DIRECT for 55, XDOCK for 205;
> **0 ledger / 0 temporal violations**, every order accounted, 98.9% assigned. 4
> cascaded tests updated; suite 221 green. **Also closes B5's residual F4.1** —
> multi-day no longer deletes the direct option. *Remaining:* ~16 far deliveries
> still strand because the resolver pre-commits XDOCK before learning it strands —
> that's the F5.1/B7 swap, next.
> Original analysis follows:
Per the B5 decision, a multi-day FULL_FLEET order where the vehicle sleeps out
and never returns to a depot is a **direct** move (freight stays on board). But
`legs.py` models every multi-day FULL_FLEET order as crossdock only —
`CUSTOMER_PICKUP` (→depot) + `CUSTOMER_DELIVERY` (depot→), freight forced through
a depot ([legs.py:350-376](legs.py#L350)). There is no `DIRECT_CUSTOMER_MOVE`
option spanning two days, so the optimizer can never choose carry-on-vehicle for
far work.
- **Fix sketch:** for multi-day FULL_FLEET, also emit a multi-day
  `DIRECT_CUSTOMER_MOVE` (collect day 1, deliver day 2, `freight_ready_time` =
  collect, service on deliver day) as a mutually-exclusive option vs the depot
  crossdock pair, and extend `resolve_options` to choose for multi-day.
- **DE-RISKED (C12/F12.1):** the execution substrate already exists — the tour
  evaluator routes a two-point DIRECT across days, `_commit_leg` gates it on the
  shared ledger, and `select_tour_vehicle` already allows rigids (can_sleep_out is
  dead, F7.2-corrected). So B6 is mostly a `legs.py` emit + `resolve_options`
  change; routing/ledger/sleep-out are done. Toured DIRECT also bypasses ALNS, so
  it sidesteps B7.
- **Overlaps:** B2 (re-verify — may be fixed), B4 (M8 horizon multiday).

### B10. Tour deliveries ignore intra-day time windows — `TODO` *(found this session, C12/F12.4)*
Tours commit every stop at a placeholder `12:00:00` arrival; `evaluate_tour`
tracks day_index + km but no clock. So tours honor date-level dwell (no early
delivery) but NOT intra-day hard delivery slots — a gap vs the README's hard-slot
requirement. Give the tour evaluator a real intra-day clock + window check.

### B11. Dead field: `can_sleep_out` — `TODO` *(found this session, C12/F12.2)*
Computed in `vehicles.py` (tractor-only), written to CSV, but **never read** by
any planner. Either wire it as the real sleep-out gate or delete it; today it
misleads (implies rigids can't tour, while `select_tour_vehicle` tours them).

### B7. ALNS is not ledger-aware (M8 prerequisite) — `TODO` *(found this session, C10/F10.1)*
ALNS guards freight dependencies with a leg-presence check (predecessor pickup
assigned?), not the stateful `FreightLedger`. Safe **only** because ALNS never
moves a job's day or depot, plus a post-hoc validation backstop. M8 (horizon
multiday, day-moving, B6 multi-day-direct) breaks that assumption — ALNS must
then consult the stateful ledger during destroy/repair. Hard prerequisite for B4/B6.

### B8. Optimizer is greedy LNS, not true ALNS — `PHASE 2 + THROUGHPUT + SA/REGRET DONE` ✓ *(2026-06-27)*
> **SA/regret (DONE):** with throughput fixed, retested both. SA now marginally positive
> (+0.3% of savings) → `--sa-temp` default 0.005. Regret-2 implemented behind
> `--regret-repair` but measured **net-negative** at small destroy sizes (A/B: 5.3% off
> vs 3.8% on) so **default OFF**. See session-log 06-27c. Remaining open: 2-opt/or-opt
> intra-route; larger-destroy + regret; OSRM duration model (A1 ph2).
> ---
> **Throughput (DONE):** the real ceiling on phase-2 gains was iteration speed.
> `evaluate_route` ran ~4,700×/iter (~20 iter/s). Fixed via (1) `@lru_cache` parse +
> `detail=False` search fast path in [routing_adapter.py](routing_adapter.py), and
> (2) `repair_every` cadence on the coverage-repair explosion ([alns.py](alns.py),
> `--repair-every` default 20). End-to-end same window/90s: **428 km (0.7%) → 2,832 km
> (4.6%), 7 → 60 moves**, coverage unchanged. 228 tests green. See session-log 06-27b.
> ---
> **Phase 2 (DONE):** destroy operators + adaptive weights in
> `alns.improve_solution` (`_worst_removal`, `_shaw_removal`, `_AdaptiveOps`; removal
> size k=2..5). Validated on 2026-01-05→01-10 (haversine, same seed): old random-only
> ALNS = 155 km / 0.2% / 1 move → **428 km / 0.7% / 7 moves**, +1 coverage. The
> optimizer is no longer inert. 4 new tests; 226 green. **SA still neutral** at 90s
> (off/warm/hot all identical 428 km) → `--sa-temp` stays default 0.0; infra retained.
> **Next levers (priority):** (1) **throughput** — repair day-re-eval limits iterations
> on ~2.3k jobs, so most of the solution is untouched; profile + cheapen. (2) **regret-2
> repair** — deferred (small removal sizes ⇒ modest payoff here, delicate vs dependency
> ordering). (3) revisit SA once 1–2 land. 2-opt/or-opt intra-route also still open.
> ---
> **Phase 1 (DONE):** SA acceptance + best-solution tracking implemented in
> `alns.improve_solution` (`sa_temp_fraction`/`sa_cooling` threaded through to a
> `--sa-temp` flag; coverage stays monotone, SA applies to km at equal served;
> returns the best-seen, not the wandering final). Correct, 222 tests green.
> **KEY FINDING (reframes B8):** SA is **inert on real data** — a 20s run accepts
> only **2 moves** (16 km / 0.02%). The ALNS is near-inert; the *seed* makes the
> plan. Cause: the neighborhood is too weak — random 1-3 job removals either
> improve (rare) or drastically worsen (SA rightly rejects). No small-worsening
> gradient for SA to climb. So `--sa-temp` defaults to 0 (off) until operators land.
> **Phase 2 (the real lever, TODO):** strong neighborhood operators — relocate/
> or-opt (move a stop/short chain to a better position), 2-opt (intra-route),
> related/Shaw removal + worst-removal (cost-biased destroy). These create the
> gradient; THEN SA + adaptive operator weights pay off. This is where the km/
> quality improvement actually comes from.

### B9. Dead code: `repair.py` — `TODO` *(found this session, C10/F10.4)*
Not imported anywhere; live repair is inline in `alns.improve_solution`.
Duplicates the dependency check and can silently drift. Delete or wire intentionally.

### B12. Output folders are not period-scoped — `DONE` ✓ *(2026-06-26)*
> Implemented: `run_dirs(out_dir, window)` nests every run under
> `out/<mode>/<basis>/<start>_to_<end>/{inputs,plan,reports}`, plus a
> `run_manifest.json` (window/args/timestamp). All four runners wired; 5 new
> tests in `test_output_layout.py`; full suite 218 pass; verified end-to-end.
Default run dir is `out/<mode>/<basis>/{inputs,plan,reports}` with **no planning
window in the path** ([output_layout.py](output_layout.py),
[run_alns.py:113](run_alns.py#L113)). Running a different time period writes to the
same folder and overwrites prior outputs; the user currently works around it with a
manual `--out-dir week_...`. Fix: fold the window into the default path, e.g.
`out/<mode>/<basis>/<start>_to_<end>/...`, so runs are self-describing and never
collide. Consider a small `run_manifest.json` (window, args, timestamp, git rev).

### B13. First-class trip_id — `DONE` ✓ *(2026-06-26)*
> Implemented: `SelectedPlanRecord.trip_id = {route_id}#T{trip_index}`, auto-set
> in `SelectedPlanBuilder.assign`; surfaced in the plan CSV (auto via schema), the
> export depot-return rows, and the operator manifest (`trip_id` column). A
> **trip** (one depot loop) now has a single stable id; `job_id` already ids a leg.
> Verified end-to-end. *Deferred sub-item:* a per-row `run_id` for cross-run
> aggregation — B12's period-scoped folders already separate runs on disk, so this
> is only needed if multiple runs' CSVs get concatenated without folder context.
> Original analysis follows:
Identifiers today (see answer in chat): `job_id = JOB:{order_id}:{suffix}` (one
planned leg) ✓; `route_id = ROUTE:{vehicle}:{day}` or `TOUR:{vehicle}:{start}` (a
vehicle-day route); a **trip** (one depot loop) is the *composite* `(route_id,
trip_index)`, surfaced only as the synthetic key `{route_id}#T{trip_index}` in
route_totals/exports — there is **no standalone `trip_id` column**. Also `route_id`
is **not run/period-scoped** (`ROUTE:VEH:2026-01-13` is identical across two runs
that touch that day), which compounds B12. Fix: add a stable first-class `trip_id`
(and a per-run id) to the plan schema so trips can be referenced in the dashboard
(A2) and operationally. Pairs with B12.

### B14. `is_tour_only` ignores the DIRECT/HUB_DROP origin carry — `DONE` ✓ *(2026-06-26)*
> Fixed: `is_tour_only` takes optional `origin_lat/lon`; for two-point legs
> tour_plan passes them so classification uses `depot→origin→dest→depot`. Verified:
> tours 2→3, assigned 2058→2059, 0 violations; 1 new test; suite 222 green. Modest
> on Jan 5-10 (only ~1 order matched the far-origin/near-dest pattern) but a real
> defect removed. Found by diagnosing strands first, which also showed F5.1/B7
> would NOT have rescued these. Original note follows:
`tours.is_tour_only(lat,lon,...)` tests only `2×(depot→service_point)` against the
day cap. For a two-point leg (DIRECT_CUSTOMER_MOVE / HUB_DROP) the real day length
is `depot→origin→dest→depot`. So a DIRECT move with a near *dest* but far *origin*
is mis-classified "daily", falls to the daily seed, and strands on SHIFT instead of
being toured (the tour evaluator handles the two-point carry fine). Strand
diagnostic (Jan 5-10): 8 stranded DIRECT moves trace to this. Fix: pass the origin
coords for two-point legs and measure the full carry. **Note:** the F5.1/B7
"option-swap" framing does NOT rescue these — the stranded work is a tour-
classification bug, not a pre-resolution choice problem. Found by diagnosing first.

## Design note — the B5/B6/F5.1 convergence (2026-06-26)

B5, B6, F5.1 are one fault line: **option generation and selection sit on the
wrong side of the optimizer.** `legs.py` generates a narrow option set and lets
achieved dates *delete* options (multi-day → no direct); `options_resolver.py`
then prunes the rest with a geometric heuristic *before* ALNS. So every
interesting choice (direct/depot, trunk/hub-drop, carry-overnight/stage) is made
before the optimizer runs — the "decide before you optimize" shape the rebuild
exists to remove.

Unifying principle: **multi-day is a property of an option's time windows, not a
switch that deletes options.** Always emit both FULL_FLEET groups — DIRECT (one
on-vehicle move; spanning days = sleep-out, no depot return) and XDOCK (via
depot) — let the date span set windows/duty, and let ALNS *swap groups* during
improvement (resolver becomes a seed, not the verdict). That one change dissolves
B5's residual, closes B6, and resolves F5.1.

Recommended sequence (instruments before optimizer surgery):
1. finish the validation walk (esp. ledger + tours = the execution substrate for
   a multi-day-direct leg; verify it routes before adding the option);
2. OSRM (A1) so option/ALNS costs are honest, not haversine proxies;
3. dashboard (A2) as the measurement + acceptance instrument;
4. M8: option-aware ALNS + B6 + multi-day/window decoupling — the real lift, last.

### B15. No vehicle-type catchment → rigids do long hauls — `DONE (2026-07-02, session 2026-07-02g)`
> **Shipped fix differs from the sketch below** (which proposed re-introducing the old
> hard per-type radii): stakeholder chose a SOFT per-vehicle design — radii learned from
> history (P95 per vehicle, type fallback, 30 km floor, `freight_planner/catchment.py`)
> feeding a proportional out-of-area phantom-km penalty (`OUT_OF_AREA_KM_FACTOR=2.6`,
> round-trip road-km scale) into every seed/ALNS ranking-and-acceptance cost site.
> No hard gate; coverage structurally safe. Spec:
> docs/superpowers/specs/2026-07-02-vehicle-catchment-design.md. Measured: out-of-area
> daily jobs 0.3%/0.5% (wk1/wk2), coverage held 99.7/99.8, km −2.8%/−3.3%.
> **Found via viz_app eyeball** of BF65WBY:2026-01-15#T1: a *rigid* runs a ~318 km
> Cambridge↔Birmingham round trip at ~33% load. Legs 1.7 / 156.8 / 150.8 / 8.6 km — one
> order 157 km out among local stops.
> **Root cause (confirmed):** the new pipeline's compatibility screen
> ([compatibility.py:122-137](compatibility.py#L122)) gates ONLY on capacity + geocode +
> time-reachability. The old pipeline's per-type catchment is GONE: cambridge had
> `CATCHMENT_RADIUS_KM=100` (rigid) / `TRACTOR_CATCHMENT_RADIUS_KM=300`, enforced in
> dispatcher.py:1074 + scope.py:469 (far orders → tractors). New pipeline grep finds only
> `EARTH_RADIUS_KM`. So a rigid is eligible for any distance it can reach in-shift.
> **Systemic:** 75 rigid legs >120 km this week (12-17), ~58% mean load.
> **Second, deeper cause (this example):** the far order's destination is `CHUB` (Palletline
> hub, geocodes to B37 7HB) — hub-bound *trunk* freight mismodeled as a DIRECT_CUSTOMER_MOVE,
> so a rigid does a dedicated run instead of riding the nightly artic trunk. = the **A3**
> (trunk as first-class leg) gap. See [[palletline-hub-at-b37-7hb]], [[network-scope-mismatch-in-backtest]].
> **It is "optimal" only for min-km s.t. capacity/time/ledger** — missing the catchment
> constraint AND the trunk leg type that reality has. Operators would reject it.
> **Fix (deferred, user logged 2026-06-27):** (1) re-introduce per-type catchment in
> compatibility (rigid ~100 km / tractor ~300 km from depot) — small, high-leverage;
> (2) A3 trunk modeling — bigger. Recommended: catchment first.

## C. Pipeline logic-validation log

Walking the new pipeline stage by stage to confirm the logic is correct.
Each entry: what it should do, what it actually does, verdict, follow-ups.

> Order of walk (entry → output):
> 1. Entry / orchestration (`run_alns.py`, `build_phase0.py`)
> 2. Demand model (`demand.py`, `date_basis.py`)
> 3. Verified responsibility (`enrich.py`, verified legs)
> 4. Movement legs (`legs.py`)
> 5. Options + resolver (`options.py`, `options_resolver.py`)
> 6. Freight state ledger (`state.py`, `freight_ledger.py`, `ledger.py`)
> 7. Vehicles / compatibility (`vehicles.py`, `compatibility.py`)
> 8. Candidate jobs (`jobs.py`)
> 9. Route seed (`route_seed.py`, `routing_adapter.py`, `route_costs.py`)
> 10. ALNS (`alns.py`, `repair.py`)
> 11. Cross-depot (`cross_depot.py`)
> 12. Tours / multiday (`tours.py`, `tour_plan.py`)
> 13. Manifest / KPI / reconcile (`manifest.py`, `kpi.py`, `reconcile.py`, `validate.py`)

### C1. Entry / orchestration (`run_alns.py`, `build_phase0.py`) — `REVIEWED`
**Should:** wire stages in the README order (demand → responsibility → legs →
state → optimizer) with one consistent input build.

**Actual build sequence (run_alns `build inputs`):**
load qargo/cache → `build_demand_records` (applies flow-aware `in_window`) →
`build_movement_leg_records(qargo_df, demand_records, cache)` →
`filter_legs_by_basis` → `vehicle_states_frame` → `candidate_jobs_frame` →
`resolve_options` (direct vs xdock) → `resolve_hub_drop` (trunk vs hubdrop) →
`vehicle_job_compatibility_frame` → demand basis-filter + `align_demand_to_legs`
→ `build_initial_freight_states` → multiday seed → ALNS → outputs.
Order matches the README. ✓

**Findings:**
- _F1.1 (divergence, low risk):_ `build_phase0.py` (the diagnostic spine) does
  **not** call `resolve_options` / `resolve_hub_drop`; it emits a separate
  `job_options.csv` via `job_options_frame`. So `candidate_jobs.csv` from the
  spine is **pre-resolution**, while the planner's `candidate_df` is
  **post-resolution**. Two files named "candidate jobs" mean different things.
  Not a bug, but the spine can't be used to audit the planner's actual option
  choices. Consider having the spine optionally run the resolvers, or rename.
- _F1.2 (confirmed OK):_ the window is filtered twice — order-level flow-aware
  `in_window` inside `build_demand_records`, then leg-level
  `filter_legs_by_basis`. We already reconciled these (partner-leg retention).
  Legs are built only for in-window demand records, then basis-filtered. ✓

### C2. Demand model (`demand.py`, `date_basis.py`) — `REVIEWED`
**Should:** normalize Qargo rows into commercial `DemandRecord`s, set
corrected flow + responsibility shape, and admit orders whose relevant endpoint
touches the window — without using hindsight as a planning input.

**Findings:**
- _F2.1 (correct):_ `in_window` keys membership on the **corrected** flow
  (PL_EXPORT/LOCAL_COLLECT→collect; PL_IMPORT/LOCAL_DELIVER→deliver;
  FULL_FLEET→collect OR deliver). This is the fix we validated; 0 same-day
  splits. ✓
- _F2.2 (REASSESSED → not a bug for window membership; see B5):_ `first_date()`
  prefers `origin_timestamp_local` (the achieved time). Under the confirmed
  operating model (forward-only, verified leg = operational truth, requested
  timestamps often placeholders), actual-first correctly lands orders on the day
  they really operated. The only residual is the multi-day→direct/xdock gating,
  tracked as the open decision in B5.
- _F2.3 (note):_ `responsibility_shape` is computed here, folding the README's
  "Stage 3 Verified Responsibility" into the demand model rather than a separate
  stage. Acceptable, but it means responsibility correctness lives in
  `demand.py` + `cambridge/verified_legs.py`, reviewed next.

### C3. Verified responsibility (`enrich.py`, verified-leg join) — `REVIEWED` ✓
**Should:** join the precomputed verified leg onto orders as a responsibility
input, never leaking the historical vehicle assignment.
**Findings:**
- _F3.1 (correct):_ `build_enriched_orders` carries only
  `verified_leg/confidence/method`; `matched_vehicle` is deliberately omitted
  (docstring + code agree). Invariant respected. ✓
- _F3.2 (correct):_ `verified_leg_lookup` returns `None` when the column is
  absent so `demand.py` falls back to the runtime CSV — graceful degradation. ✓
- _F3.3 (note):_ row count/order preserved, blanks not NaN. Clean. No issues.

### C4. Movement legs (`legs.py`) — `REVIEWED` (faithful, but B5 propagates)
**Should:** expand each demand record into the README's canonical legs (PL
import = inbound trunk + delivery; PL export = pickup + outbound trunk OR
hubdrop; FF = direct OR xdock; LOCAL = single leg), with mutual-exclusion option
groups and freight-state ready/result tags.
**Findings:**
- _F4.0 (correct):_ leg generation matches the spec's canonical patterns,
  including option groups (`option_set`/`option_group`) and ready/result states
  for the ledger. Excluded records still emit an `ACCOUNTING_ONLY` row so they
  stay in the manifest; an unknown-flow fallthrough emits `AMBIGUOUS_MANUAL`. ✓
- _F4.1 (ESCALATES B5 — structural, not just accounting):_ `multi_day` for
  FULL_FLEET is `collect_date != deliver_date`, and these dates come from
  `demand.py`'s actual-first `first_date`. So if an order was *requested*
  same-day but *executed* across two days, B5 makes it look multi-day → the
  planner emits **crossdock-only** and never offers the DIRECT option (line
  348-377). B5 therefore distorts the option *structure* the optimizer sees, not
  just window membership. Raises B5's priority.
- _F4.2 (internal inconsistency confirming B5):_ the PL_IMPORT inbound-trunk date
  at `legs.py:281` uses `destination_requested_start_timestamp_local` **first**
  (requested-first — the correct order), while the delivery leg's `service_date`
  = `record.deliver_date` is actual-first. Same file, two different precedences;
  the requested-first one is the one B5 should standardize on.
- _F4.3 (minor gap):_ `_split_parts` (Q7 massive-order split) is only invoked in
  the FULL_FLEET branch. A massive **non-hazardous** PL_EXPORT/LOCAL order is not
  split and falls to `MASSIVE_UNSUPPORTED`. Confirm whether that's intended or a
  coverage gap.
- _F4.4 (note for Stage 9):_ `DIRECT_CUSTOMER_MOVE` encodes a two-point move as a
  single service node at `dest_pc` carrying `origin_lat/lon`. Must verify the
  routing adapter consumes `origin_lat/lon` so direct km isn't measured as a
  zero-length depot stop. Flagged for the route-cost review.
- _F4.5 (minor robustness):_ `hub_for_row` routes to LE10 only when the literal
  `"hazchem"` appears in subcontractor/import-type text; everything else →
  B37. A Hazchem order missing that token would be mis-trunked to Palletline.

### C5. Options + resolver (`options_resolver.py`) — `REVIEWED` (key architectural finding)
**Should:** for each mutually-exclusive option set (DIRECT/XDOCK, TRUNK/HUBDROP)
keep one option so the dispatch pool + ledger stay unambiguous.
**Findings:**
- _F5.1 (ARCHITECTURAL — the core "true optimization" gap):_ both resolvers are
  **pre-resolution (Option A)** — they collapse each option group to a single
  choice **before** ALNS, via a geometric heuristic. ALNS never swaps options.
  So the planner does **not** actually own the direct-vs-crossdock or
  trunk-vs-hubdrop decision; a pre-routing heuristic does. This is precisely the
  "hard decision before optimization" the README set out to remove (Q11). It is a
  *known, deliberate* staging choice (M7a Option A; ALNS option-swap = Option B
  deferred to M8), but it is THE lever for true optimization of these choices,
  and it's where B6's multi-day-direct option must eventually plug in. Tracks B4.
- _F5.2 (correct):_ the two resolvers share one `groupby("option_set")` space but
  each `continue`s on the other's groups (DIRECT/XDOCK vs TRUNK/HUBDROP), and
  run_alns runs them sequentially on disjoint sets — clean separation. ✓
- _F5.3 (note):_ run_alns calls `resolve_options(..., allow_same_day_xdock=True)`,
  so the geometric cost choice IS active (not the same-day-DIRECT shortcut).
  Means same-day xdock timing (intra-day depot staging) must be sound; relies on
  `staged_delivery_start` + `_window_infeasible`. Spot-check in Stage 6.
- _F5.4 (heuristic caveat):_ `_cost_choice` scores XDOCK as two independent
  depot→stop→depot round trips and sums them — over-counts vs the real groupage
  piggyback, compensated by `ratio=1.6`. Acknowledged proxy; honest fix is F5.1.
- _F5.5 (consistent w/ memory):_ `resolve_hub_drop` (ratio=1.0, strictly cheaper,
  TRUNK default) is near-inert because depot→hub trunk km is treated as saved
  though the trunk runs anyway for other freight; real opportunity is dynamic
  (ties to B4/B6). Matches the prior 0/722-beneficial finding.

### C6. Freight-state ledger (`state.py`, `freight_ledger.py`, `ledger.py`) — `REVIEWED` ✓ (clears B6)
Three distinct mechanisms, correctly separated:
- `state.py` = initial freight placement (where each unit starts the horizon);
- `freight_ledger.py` = stateful execution ledger (phantom-prevention by construction);
- `ledger.py` = stateless set-check (a selected delivery requires its pickup selected).
**Findings:**
- _F6.1 (KEY — ledger does NOT block B6):_ `deliver_direct` accepts freight in
  `AT_CUSTOMER_ORIGIN` **or** `ON_VEHICLE` and consumes it → `DELIVERED` in one
  atomic leg, with **no time-span constraint**. So a multi-day on-vehicle direct
  modeled as a single `DIRECT_CUSTOMER_MOVE` (collect+deliver, spanning 2 days)
  passes the ledger cleanly. **B6's blocker is therefore NOT here — it's the
  vehicle/tour duty model (can a vehicle hold a leg across an overnight without a
  depot return?), Stage 12.** Narrows B6 nicely.
- _F6.2 (correct):_ phantom prevention is real — `deliver_from_depot` raises
  `FreightUnavailableError` unless the unit is physically `AT_DEPOT`; pickups
  produce depot freight; hub-drop/direct are terminal. Matches the README's
  "impossible by construction" claim. ✓
- _F6.3 (verify wiring — Stage 9/12):_ three integrity checks exist (stateless
  set-check `ledger.py`, stateful `FreightLedger`, temporal `plan_validation`).
  run_alns wires the stateless (`plan_ledger_violations`) + temporal checks
  explicitly. Must confirm the **stateful** `FreightLedger` is actually consulted
  during seed/tour selection (route_seed/tour_plan), not just available. If it
  only runs as a post-hoc check, the "by construction" guarantee is weaker than
  advertised. Flagged for those stages.
- _F6.4 (note):_ states `NOT_READY` / `AT_HUB` / `ON_VEHICLE` are defined but
  have **no producing transition** — effectively dead today. Fine now (origin→
  depot→delivered, or origin→delivered covers all current legs), but an explicit
  load/carry/unload-across-days model for B6 would need real `ON_VEHICLE`
  transitions if we ever move off the single-atomic-leg representation.
- _F6.5 (minor):_ `pickup_to_depot`/`handoff_to_hub`/`deliver_direct` auto-register
  unregistered freight at origin — pragmatic, but it can mask a missing
  initial-state record rather than surfacing it. Low risk.

### C7. Vehicles + compatibility (`vehicles.py`, `compatibility.py`) — `REVIEWED` (two M8 blockers + an OSRM hit)
**Findings:**
- _F7.1 (CORRECTED in C12 — NOT a blocker):_ `can_sleep_out = (vtype ==
  "tractor")` ([vehicles.py:79](vehicles.py#L79)) looks like a hard tractor-only
  wall, BUT grep confirms `can_sleep_out` is **only written, never read** by any
  planner. The multiday path (`select_tour_vehicle`) ignores it and assigns tours
  by capacity + Q4 type preference — already *preferring rigids* for light tours.
  So Q4's intent is effectively honored and the field is **dead/inconsistent
  metadata**, not a B6 blocker. (Cleanup: either wire it or drop it.) See C12.
- _F7.2 (core M8/B4 gap — vehicle state is a single-day static snapshot):_
  `VehicleStateRecord` has `available_from`/`shift_end`/`home_depot` but NOT the
  README's evolving fields (`remaining_drive_minutes`, `remaining_duty_minutes`,
  `current_node` carry-forward, `requires_depot_return`). Vehicles are rebuilt
  fresh at `start` each run with no cross-day evolution. Horizon multiday (B4) and
  the run-to-run handoff (B1) both need this to become a mutating resource. The
  duty/drive *limits* may still be enforced per-route in the evaluator (verify
  Stage 9), but the multi-day **carry-forward** is absent.
- _F7.3 (OSRM/A1 hit — feasibility runs on haversine, not road):_
  `current_to_service_km = haversine × ROAD_DISTANCE_FACTOR`, drive minutes =
  km/AVG_SPEED. So `capacity_ok`/`time_reachable` — the gate that admits a
  vehicle↔job pair — is decided on approximate geometry, not OSRM. A pair can be
  admitted/excluded wrongly vs real roads. Concrete reason A1 precedes cost-driven
  option choice.
- _F7.4 (correct):_ `can_cross_depot = True` for all vehicles — depot is a cost
  bias, not a wall (M6 design). ✓ `can_trunk = tractor-only` is the eligibility
  gate the future trunk-as-resource (A3) will hang off.
- _F7.5 (note, coarse-but-fine):_ compatibility is a loose necessary-condition
  filter — single-job capacity and single-hop `current→service ≤ latest_finish`
  reachability, permissive when windows are missing. The route evaluator does the
  real multi-stop/duty check downstream. Reasonable as a pre-filter.

### C8. Candidate jobs (`jobs.py`) — `REVIEWED` (confirms A3 root; B6-ready deps)
**Findings:**
- _F8.1 (A3 STRUCTURAL ROOT — trunk never enters the optimizer):_ only
  `CUSTOMER_LEG_KINDS = {CUSTOMER_PICKUP, CUSTOMER_DELIVERY, DIRECT_CUSTOMER_MOVE,
  HUB_DROP}` become candidate jobs. INBOUND/OUTBOUND_TRUNK (and ACCOUNTING_ONLY)
  are filtered out, so trunk is structurally absent from the dispatch/optimization
  pool — exactly the A3 "trunk as first-class leg" gap, at its source. Making
  trunk a resource means adding it here (+ vehicle `can_trunk` gating from F7.4).
- _F8.2 (correct, and B6-ready):_ `_dependency_maps` wires the ledger set-check
  substrate: DIRECT→`NONE_DIRECT`, hub-drop/pickup-only→`PICKUP_TERMINAL`,
  delivery-only→`PRESTAGED_DELIVERY`, paired→`PRODUCES_DEPOT_FREIGHT` /
  `REQUIRES_PRIOR_PICKUP` with predecessor/successor links. Crucially it runs
  *before* option resolution, so a same-day FF freight group holds DIRECT **and**
  XDOCK pickup/delivery at once — and it tags all three correctly; whichever group
  survives resolution keeps valid deps. A future B6 multi-day DIRECT lands in the
  same group shape, so **jobs.py needs no dependency change for B6.** ✓
- _F8.3 (correct):_ `BEFORE_PLANNING_START` hard-blocks `service_date <
  planning_start` — the gate for the B1 prior-window pickup. ✓
- _F8.4 (minor — verify, not clearly wrong):_ `MISSING_WINDOW` is a *hard*
  blocker, but `legs.py` window policies fall back to an operating-day window, so
  it should only fire when no date anchor exists at all (genuinely unschedulable).
  Confirm it isn't catching orders with a date but an unparsed window — the
  README wants stale/missing windows relaxed to a default, not blocked.
- _F8.5 (note):_ `feasible_vehicle_count`/`allowed_vehicle_types` are
  capacity-only (ignore geo/time); `NO_CAPABLE_VEHICLE` means "no vehicle big
  enough," not "none reachable." Reachability is the compatibility frame + route
  evaluator. Consistent with the loose-pre-filter design.

### C9. Route evaluator + seed (`route_costs.py`, `routing_adapter.py`, `route_seed.py`) — `REVIEWED` (resolves 3 threads)
**Resolutions of earlier flags:**
- _F4.4 → RESOLVED ✓:_ two-point kinds (`DIRECT_CUSTOMER_MOVE`, `HUB_DROP`) with
  `origin_lat/lon` are priced as `road_km(prev→origin)+road_km(origin→dest)` and
  charged 2× service ([routing_adapter.py:179-204](routing_adapter.py#L179)).
  `make_route_job` passes the coords through. Direct geometry is honest.
- _F6.3 → RESOLVED ✓:_ the **stateful** `FreightLedger` actively gates the seed,
  not post-hoc: before committing each leg it checks (`exists_at_depot` for
  delivery, not-DELIVERED for direct, not-already-DELIVERED for pickup/hub-drop →
  reject `DELIVERY_BEFORE_PICKUP`/`FREIGHT_ALREADY_DELIVERED`) and updates after
  (`pickup_to_depot`/`deliver_from_depot`/`handoff_to_hub`/`deliver_direct`,
  [route_seed.py:285-327](route_seed.py#L285)). Priority sort puts pickups
  (dep_rank 0) before deliveries (4) and earlier service_date first, so freight is
  at depot before its delivery is considered. "Impossible by construction" holds
  **in the seed** — must re-confirm ALNS preserves it (Stage 10).
- _F7.3 / A1 → CONFIRMED at the core:_ `road_km = haversine × ROAD_DISTANCE_FACTOR`
  ([route_costs.py:34](route_costs.py#L34)); the entire evaluator/seed/ALNS
  optimizes approximate distance. Signature is OSRM-ready (drop-in), but **OSRM is
  not wired**. This is the single biggest honest-cost lever (A1).
**New findings:**
- _F9.1 (B6 routing blocker CONFIRMED here):_ evaluator + seed are strictly
  **single-day** — vehicle starts each day at home depot, `return_to_depot=True`,
  `shift_end` same day; docstrings say "multiday spans are Milestone 8." A
  multi-day DIRECT (service on deliver day) would be evaluated as a fresh same-day
  depot loop, losing the prior-day collection + sleep-out. So B6's execution
  blocker is exactly the per-day route model + the tour machinery (Stage 12),
  matching the F6.1 prediction. The single-atomic-leg ledger trick is fine; the
  *routing* of it across an overnight is what's missing.
- _F9.2 (seed limitation):_ `same_order_handoff_conflict` forces a crossdock
  pickup and its paired delivery onto **separate trips** (no explicit
  depot-intermediate stop yet). Correct given the model, but it means same-day
  XDOCK always splits across two trips/vehicles — a constraint the M8 depot-stop
  work would lift.
- _F9.3 (seed cross-depot is fallback, not cost-competed):_ `best_insertion` tries
  **all same-depot** vehicles first, cross-depot only if none feasible
  ([route_seed.py:312-314](route_seed.py#L312)). A much-closer cross-depot vehicle
  won't be picked if any same-depot one fits. Greedy approximation; ALNS may
  rebalance. Depot-as-cost-bias is thus only partially realized in the seed.
- _F9.4 (correct):_ multi-trip days (capacity resets at depot, reload dwell,
  DRIVING_CAP + SHIFT enforced in `evaluate_day`) preserve the old pipeline's
  same-day multi-trip capability; new-trip-on-used-vehicle penalized to keep
  coverage ahead of km compression. Per-day duty IS enforced (answers part of
  F7.2 — limits enforced per-route even though the record lacks `remaining_*`;
  only the cross-day carry-forward is missing).

### C10. ALNS + repair (`alns.py`, `repair.py`) — `REVIEWED` (2 findings central to "true optimization")
**Findings:**
- _F10.1 (CRITICAL for M8 — ALNS is NOT ledger-aware; safe only by immutability):_
  ALNS does **not** use the stateful `FreightLedger`. It guards dependencies with a
  weaker **leg-presence** check: a `REQUIRES_PRIOR_PICKUP` job may be placed only
  if its predecessor leg is currently assigned ([alns.py:481-484](alns.py#L481)).
  It does NOT verify depot match, temporal order, or physical freight production.
  This is sufficient **only because** ALNS never changes a job's `day` (`day =
  meta.day`, fixed) or its depot, and run_alns backstops with a post-hoc
  stateless + temporal validation (0 in current runs). **The moment M8 lets ALNS
  move jobs across days or insert a multi-day-direct (B6), this check becomes
  insufficient and ALNS must consult the stateful ledger.** Flag loudly for M8.
- _F10.2 (optimization quality — it's greedy LNS, not true ALNS):_ acceptance is
  pure hill-climb — a move is accepted only if it raises served jobs or lowers km
  ([alns.py:529](alns.py#L529)); no worsening moves (no SA temperature), and the
  destroy/repair operators have **no adaptive weights** (the "A" in ALNS). It's a
  randomized greedy LNS with multi-restart diversification. For the stated goal of
  genuine optimization this is the quality ceiling: limited escape from local
  optima. Candidate upgrade alongside M8 (SA acceptance + adaptive operators).
- _F10.3 (reconfirms F5.1):_ ALNS has **no option-swap operator**. It receives the
  already-resolved candidate pool (DIRECT/XDOCK, TRUNK/HUBDROP pre-pruned in Stage
  5), so it can never reconsider those choices. "Option B" remains unbuilt.
- _F10.4 (dead code):_ `repair.py` is **not imported anywhere** (live repair is
  inline in `improve_solution`). It duplicates the same leg-presence check. Cleanup
  candidate — delete or wire intentionally; today it can silently drift from alns.py.
- _F10.5 (correct):_ coverage is strictly prioritized over km (lexicographic
  `(served_after, -km)`), matching README objective order. Eject move allows
  kicking one job to insert a higher-priority one. Sound LNS neighborhoods. A1
  applies — every evaluation is haversine `evaluate_day`.

### C11. Cross-depot (`cross_depot.py`) — `REVIEWED` ✓ (reporting-only, as intended)
**Findings:**
- _F11.1 (confirms F9.3):_ this module is **accounting-only** — it classifies each
  assignment SAME/CROSS and measures repositioning km for the KPI/report; it does
  NOT influence planning. The actual cross-depot choice lives in the seed (same-
  depot-first fallback) + ALNS eligibility. So cross-depot is reported here but
  *decided* as a fallback ordering, never cost-competed (the F9.3 limitation).
- _F11.2 (reporting caveat — no double-count, but two nuances):_ `repositioning_km`
  is a **notional** home-anchor→territory-anchor straight road_km, reported
  separately and NOT added into headline `planned_km` (good). And
  `cross_depot_km` (with route_totals) attributes a route's **entire** km to
  cross-depot if it carries even one cross leg — overstates cross-depot km. Both
  are presentation-only; no effect on the plan or objective.
- _F11.3 (minor):_ OVERFLOW work (empty `source_depot`) classifies as SAME, so
  cross-depot counts can under-report overflow. Cosmetic.

### C12. Tours / multiday (`tours.py`, `tour_plan.py`) — `REVIEWED` (B6 substrate EXISTS; de-risks B6)
**The big result — B6 is smaller than feared:**
- _F12.1 (B6 execution substrate ALREADY EXISTS):_ the tour evaluator handles
  two-point moves across days — `_TWO_POINT_KINDS` includes `DIRECT_CUSTOMER_MOVE`,
  `_leg_km` prices `prev→origin→dest`, `evaluate_tour` carries the load over the
  segment then drops it, `TourStop.day_index` spans days, and `_commit_leg`
  applies `deliver_direct` against the shared ledger. So a multi-day on-vehicle
  DIRECT **would route and gate correctly today** if it reached the tour phase.
  B6's real remaining work is just: (a) emit the multi-day DIRECT leg in
  `legs.py`, (b) make multi-day FF a DIRECT/XDOCK **option group** (today multi-day
  emits no group), (c) extend `resolve_options` to choose for multi-day (Stage 5
  only resolves same-day). The hard substrate (routing+ledger+sleep-out) is done.
  Bonus: toured DIRECT bypasses ALNS, so it sidesteps B7.
- _F12.2 (corrects F7.1):_ `select_tour_vehicle` assigns tours by capacity +
  busyness + Q4 type preference (prefers rigid for light tours) and **never reads
  `can_sleep_out`**. Rigids already get multi-day tours. So `can_sleep_out` is dead
  metadata, not a constraint — F7.1 downgraded; no Q4 rework needed for B6.
**Other findings:**
- _F12.3 (B2 likely ADDRESSED — re-verify):_ far crossdock **deliveries**
  (REQUIRES_PRIOR_PICKUP, not the excluded PRODUCES_DEPOT_FREIGHT pickups) ARE
  eligible for touring and commit against the shared ledger after the daily
  pickup. So the current M8a path batches far crossdock deliveries onto tours —
  contradicting the older "FF_XDOCK_DELIVER dispatch as LOCAL dead-ends, bypass
  build_tours" note. **B2 may be stale/fixed; needs an empirical re-check** (run +
  inspect a far multi-day FF order) rather than assuming it still bites.
- _F12.4 (real limitation — tour intra-day timing is a placeholder):_ tours commit
  every stop at a fixed `12:00:00` arrival ([tour_plan.py:255](tour_plan.py#L255));
  `evaluate_tour` tracks day_index + km but no clock. So tours respect *date*-level
  dwell (don't deliver before due day) but NOT intra-day hard delivery slots — a
  gap vs the README's hard-slot requirement. New backlog candidate.
- _F12.5 (A1 nuance — TWO distance models):_ daily uses `road_km`
  (haversine×factor, 50km/h); tours use straight-line `MULTIDAY_AVG_SPEED_KMH`
  motorway speed. Deliberate (road model over-estimates trunking), but tour km and
  daily km aren't directly comparable, and A1/OSRM should unify both onto real
  routing.
- _F12.6 (optimization quality):_ tours are greedy-batched and **not ALNS-improved**
  (fixed `tour_records` bypass ALNS); reserved vehicle-days are excluded from ALNS.
  The multiday portion has no improvement loop — ties to B8.
- _F12.7 (correct):_ ordering is sound — classify far jobs, reserve vehicles, run
  daily seed on a shared ledger (records collections), THEN commit tours against
  it, so far deliveries gate on near collections. Dwelling prevents early delivery.
  A tour that can't get a vehicle releases its jobs (`NO_FEASIBLE_TOUR`). ✓

### C13. Manifest / KPI / validation (`manifest.py`, `kpi.py`, `plan_validation.py`) — `REVIEWED` ✓
**Findings:**
- _F13.1 (correct):_ manifest buckets every movement into ROUTED / ACCOUNTING /
  UNASSIGNED / BLOCKED + synthetic `depot_return` rows; accounting-only (trunk) is
  never counted as a routed job; pre-routing blockers are accounted too. Every
  order with a leg lands in exactly one bucket (the "every order accounted: True"
  check). Matches README Stage 10. ✓
- _F13.2 (correct, strict):_ KPI denominator is explicit — `in_universe` =
  non-excluded orders with a dispatchable `corrected_flow`; an order counts
  `assigned` only if **all** its runnable jobs are selected (`required <=
  selected_ids`), so a crossdock order needs both pickup+delivery served. Sound. ✓
- _F13.3 (the F10.1 backstop, confirmed):_ `temporal_violations` checks the FINAL
  plan's timestamps (delivery not before pickup+handoff), and run_alns also runs
  the stateless `plan_ledger_violations`. These two post-hoc checks are what make
  the not-ledger-aware ALNS (F10.1) safe today. **Caveat:** the temporal guard only
  pairs CUSTOMER_PICKUP↔CUSTOMER_DELIVERY by order_id — it does NOT validate a
  single-leg DIRECT move's schedule, so a future B6 multi-day-direct would have no
  temporal backstop. Add a direct-move check when B6 lands.
- _F13.4 (A2 DE-RISK — the dashboard data layer largely exists):_ `build_route_stops`
  emits a map-ready per-stop table: ordered `depot_start → stops → depot_return`
  with lat/lon, `collect_lat/lon` for two-point legs, times, leg km, load, and an
  `is_tour` flag. That's "everything a visualization needs to draw the route." So
  A2 is mostly a *rendering* job on top of an existing table, not a data-plumbing
  job. Good news for A2.
- _F13.5 (cosmetic):_ `manifest.py` docstring/comments contain mojibake (mangled
  em-dashes, `鈥?`) — a source-encoding slip. Harmless; clean up opportunistically.
- _F13.6 (note):_ `validate.py` (spine validation report) and `reconcile.py`
  (compare vs old manifest) are diagnostic-only, off the critical plan path —
  reviewed lightly; revisit if we lean on the old-manifest reconciliation again.

### Walk complete — all 13 stages reviewed.

## Validation summary (2026-06-26)

**Verdict: the pipeline is logically sound and faithful to the README's
movement-leg architecture.** Phantom prevention, leg generation, the demand/window
model, KPI accounting, and the per-day route+ledger all hold up. No correctness
bug that produces a wrong/illegal plan was found. The gaps are about *optimization
reach* and *operability*, not validity.

**Two findings I corrected mid-walk** (worth remembering): B5's "hindsight leak"
was over-called — actual-first dating is fine for the forward-only, data-as-truth
model (only the multi-day→option-gating residual remained → B6). And F7.1's
"sleep-out wall" was wrong — `can_sleep_out` is dead metadata; rigids already tour.

**The M8 cluster (one body of work, the real "true optimization" lift):**
- F5.1 — options are pre-resolved before ALNS (no option-swap operator);
- B6 — no multi-day on-vehicle direct option *(DE-RISKED: tour substrate exists;
  it's a legs.py emit + resolver change)*;
- B7 — ALNS is not ledger-aware (safety gate on any day-moving / multi-day move);
- B8 — optimizer is greedy LNS, not true ALNS (no SA, no adaptive operators);
- B4 — horizon multiday / evolving vehicle state.

**Instruments (do before the M8 surgery):**
- A1 — OSRM: `road_km` is haversine everywhere; the whole optimizer optimizes
  approximate distance; two different distance models (daily vs tour) to unify;
- A2 — dashboard: *data layer largely exists* (`build_route_stops`) — mostly a
  rendering job;
- B12 — period-scoped output folders; B13 — first-class run-scoped `trip_id`.

**Smaller/independent:** A3 (trunk never enters the optimizer — `jobs.py` filters
it out), B2 (likely fixed by M8a — re-verify empirically), B9/B11 (dead code:
`repair.py`, `can_sleep_out`), B10 (tour intra-day windows), F4.3 (massive split
FF-only), F4.5 (hub "hazchem" string match), F13.5 (mojibake).

**Recommended order (unchanged):** instruments first (A1 honest costs → A2
visibility → B12/B13 operability), then the M8 cluster with B7 as its safety gate.

## February backtest + three fixes (2026-07-08)

**February 2026 ran end-to-end** (4 Mon–Sat windows, 120s/seed 0, chained from
January's final handover via new `run_month --initial-handover` + `--qargo`
passthroughs): coverage 99.7–100%, all handover hops ✓, month matched gap +2.2%
(wk1 −10.1% → wk4 +17.1% — the 120s starvation gradient; heaviest week suffers
most, re-baseline argument). Outputs `runs/2026-02/` + `month_summary.md`.

**Meridian bug (FIXED, simulation/routing.py):** SG8 5QP geocodes to
lon −0.000089; f-string rendered `-8.9e-05` into the OSRM URL → HTTP 400 →
window 4 failed deterministically. Both URL builders now `:.6f` fixed-point;
regression test `test_osrm_urls_use_fixed_point_never_scientific_notation`.
Note `test_osrm_router_falls_back_when_pair_missing` is env-dependent (fails
whenever a live OSRM answers — pre-existing, unrelated).

**viz_app --validate window-scope fix (SHIPPED):** the validate scorecard was a
single-day panel silently compared against whole-window planned km (Δ −81%
artifacts). Now: actuals aggregated over nominal-window days (run_manifest
start..end), trunk km on the planned side of Δ, tail-day spill excluded and
shown, per-trip popups day-correct, vehicle-days = true (veh,day) counts,
`time budget · seed` row, fleet-level-context disclaimer. All 9 Jan+Feb pages
rebuilt; 19 viz tests (2 new).

**plan_full.py (NEW):** per-window `plan_full.csv` — old-pipeline-style single
denormalized file (manifest spine + stop detail + leg endpoints/order_name/
windows/sizes; row count == plan_manifest_new). `--month` emits all windows +
`plan_full_dictionary.md`. Jan + Feb built, 100% endpoint reconciliation.

**Ops lesson (00:32–00:51):** production chain + E3 workers share the OSRM
server and matrix-cache JSON — concurrent warm-save corrupted one E3 read and
hung two runs on dead sockets (kill parent tree first; orchestrators self-resume;
mop-up list in experiments/PROVENANCE.md). TODO: atomic-write/lock the shared
OSRM cache; client timeout on live road_km path.

## PIPELINE.md shipped — the current-state methodology document (2026-07-08)

Wrote `freight_planner/PIPELINE.md`: the code-verified end-to-end description of the
planner as it runs TODAY (19 sections: inputs → demand → legs → handover → catchments →
candidates → road model → mode resolve → compatibility → ledger → staged seed
(tours/trunk/shuttle/daily/stranded-repair) → ALNS (full action space + operators +
acceptance + invariants) → emission inventory → verification layers → viz/rollups →
decision-boundary table → determinism → config values → limitations). Written entirely
from source, not memory — README.md remains the design-rationale record; PIPELINE.md is
the methodology core for the paper.

**Correction found while verifying:** the ALNS removal band default is **2..5**
(`_removal_band`), NOT 4..12 as previously quoted in discussion; E3's `band_2_8` config
only raises REMOVAL_MAX to 8 ⇒ the −3.3% finding means WIDER destroys win (the default
under-destroys) — the opposite of the earlier "narrower band" reading. Docs/memory fixed.
