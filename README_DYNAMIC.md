# README_DYNAMIC — the rolling-horizon dispatcher, rules and mechanisms

How `run_rolling.py` turns the static planner into a **live dispatcher**: it replays each
day as a sequence of decision epochs, seeing only the orders knowable at each moment,
committing work to drivers as it dispatches, and never revising what a driver was already
sent. This file explains the mechanisms; **`RULES.md` is the authoritative invariant list**
(one rule per enforcement point — cited as A1…F3 below). The static machinery it drives is
`README_STATIC.md`; PIPELINE.md §13a is the code-verified reference.

**Why it exists.** The static planner is clairvoyant — it sees Friday's bookings on
Monday. A real dispatcher doesn't. The rolling loop is the honest online problem: the
plan *evolves* as orders arrive, and the gap between its result and the static bound is
the price of not knowing the future. It is also what a live product would actually run —
which is why the loop persists the plan **as it stood at every epoch**, not just the final
outcome.

## 1 · The decision grid

Per operating day (`run_dynamic_loop`, times configurable):

| epoch | what it may do |
|---|---|
| **00:00 midnight seed anchor** | first optimization of the day — full re-solve of everything uncommitted: staged seed + ALNS over all visible, unexpired orders. **The only epoch that can CREATE multi-day tours** (rule B7 — user-confirmed design: a far order booked mid-morning waits for the next seed and is accounted SLIPPED; the intraday lever is tour-tail attachment, `TOUR_ATTACH_ENABLED`, default ON since 2026-07-16). |
| **micro passes**, every `config.MICRO_EVERY_MIN` min (default 30), within **06:00–18:00** | *insertion-only*: newly booked orders are inserted into the standing plan (collection-side, one branch per order). Micros never reshuffle existing work, never create tours, may top-up an in-flight trip's un-departed tail. A pass costs ~2 s wall, so cadence is a service-level choice — 30 min halves a new booking's mean wait vs 60 (pre-2026-07-14 replays pass `--micro-every-min 60`). |
| **12:00 warm re-opt anchor** | re-solves the uncommitted remainder warm-started from the live plan (`--beta` adds `β × disturbance` to damp churn; 0 = pure cost, the regression gate). |
| **18:00 close** | end-of-day bookkeeping: day close, trunk close (`day_close_trunk` — the drawn, per-vehicle-named nights), service-ledger settlement. |

Between and at all of these runs the **commitment heartbeat** (§3). Epoch/band state,
slip pool and the service ledger live in `epoch_state.py`; order visibility in
`visibility.py`.

## 2 · Knowledge — the plan may only use what exists (RULES A)

- **Visibility** (`visibility.visible_order_ids`, A1): an epoch sees an order only if
  `timestamp_created ≤ epoch`. Collections reveal at booking; `DELIVER_FLOWS`
  (imports / local deliveries) reveal at **18:00 the day before** their delivery day — the
  freight is already in the network, which is exactly what a real depot knows.
- **Expiry**: anchors drop orders whose *target service day* is past —
  `target_service_day` = delivery date for delivery-anchored flows, collection date for
  collections (using the collection date for everything was the 2026-07-11 bug that
  expired every import before the seed). Late-but-serveable work goes to the **slip pool**
  and is accounted SLIPPED, never silently dropped.
- **Non-anticipation** (A2, the core correctness property): a vehicle may not *arrive* at
  a collection before its order was booked. Enforced in depth, all dynamic-only (static
  baselines stay bit-identical):
  - `_floor_collection_earliest_to_creation` (run_alns): every collection's
    `earliest_start` is floored to its booking time — a per-order fact that survives every
    re-plan;
  - `no_early_arrival` (route_seed → evaluate_day): an early drive-up is *infeasible*
    (EARLY_ARRIVAL), not an honest curb wait;
  - tour readiness floors (`tour_plan`): a sweep starts no earlier than its members'
    booking days.
- **No planning in the past** (A4): epochs can't re-draw past trunk nights (`trunk_from`);
  tour day-1 is clamped to the anchor's day (a member due yesterday is served late =
  SLIPPED, never backdated); the candidate frame is **day-clamped** every rolling epoch
  (`_clamp_past_candidates` — stale-dated unserved legs aren't plannable; committed legs
  keep their rows for injected-trip metadata).
- **No ordinary planning beyond the window**: `_clamp_future_candidates` withholds daily
  candidates dated after `--end`. A collection completed inside the window remains
  `AT_DEPOT` in `handover.json`, including its physical staging depot and ready time, and
  the next chained window decides its delivery. Only an already-started multi-day tour
  may emit stops beyond the boundary.

## 3 · Commitment — a promise made is a promise kept (RULES B)

The commit lag is `Δ = --delta-r1-min` (90): solve wall + driver notification +
contingency. (`--delta-min` is vestigial, kept for CLI compatibility.)

- **Launch** (B1, `expire_commit` + the `live_departures` overlay): any trip whose depot
  departure enters `next decision + Δ` becomes **in-flight** — priced off the LIVE plan,
  so micro-inserted trips launch exactly like seed trips.
- **Watermarks** (B2/B3, `advance_watermarks`): within an in-flight trip, the stops that
  have begun (or roll inside now+Δ) form the committed **prefix** — pinned against
  destroy, never re-timed, only ever *delayed*. The suffix opens only beyond the window:
  **departure-based flooring** (tightened 2026-07-14 evening, the WT255677 catch) — a
  suffix change is legal only if the trip's *deviation point*, the last committed stop's
  departure (the first moment the driver's remaining plan changes), is itself ≥ the Δ
  floor, and every suffix arrival clears the floor too. Flooring arrivals alone was
  structurally leaky: an order 100 min of driving away always *arrives* outside a 90-min
  freeze, yet the truck must start driving toward it inside it. The now-guard remains as
  defense in depth (nothing appends behind a fully-DEPARTED prefix). Net: within now+Δ,
  neither the driver's stops nor his planned drives ever change; a launched trip is
  top-uppable only while ≥ Δ of ground remains before the insertion point. (A brief
  full-immutability experiment was tried and REVERTED 2026-07-14; this is the middle
  ground that survived.)
- **Build context** (B4, `_built_ctx`/`apply_commit_ctx`): a launched vehicle-day is
  forever evaluated under the dispatch context it was built with — later epochs may not
  re-floor or re-time it.
- **Tour freeze** (B5, `_freeze_due_tours`): a tour freezes at ANY decision the moment its
  first departure enters the horizon; its orders leave the candidate universe and its
  vehicle-days are reserved everywhere. Frozen tours are never re-planned.
- **Snapshots tell the truth at their own epoch** (B6): `_snapshot` runs *after* the
  heartbeat, so a trip inserted-and-launched at 10:00 is already `committed=1` in the
  10:00 snapshot.

## 4 · Every insertion path is guarded (RULES D1)

The search may only improve what is uncommitted. The guard stack (watermark min-position +
`floor_ok` on the changed day + the now-guard) applies to **every** door into a plan:
seed placement, ALNS insertion/ejection, micro inserts, **and the post-ALNS merge sweep**
— the sweep was the one unguarded path until 2026-07-14, when it top-upped a departed trip
(the WT255131 finding; `apply_zero_cost_merges` now takes `watermarks`/`commit_floor`/
`now`). `_floor_guard_active` arms the floor check for any today-or-past key even without
a watermark entry (a never-launched past key must not disarm the guard by absence).

Duty stays hard under commitment (C5): suffix insertions into a launched trip re-evaluate
the whole day — **13 h duty** (shift windows clamped at vehicle-state build,
`MAX_DUTY_H_PER_DAY`) and **10 h driving** (`DRIVING_CAP` across all trips,
frozen-trip drive deducted via `duty_after_freeze`).

## 5 · The rules, compactly

`RULES.md` is the contract — six families, each rule naming its enforcement site:

| family | one-line essence |
|---|---|
| **A. Knowledge** | invisible until booked; never served before booked; nothing dispatched before t+Δ; nothing planned in the past of its epoch |
| **B. Commitment** | launch at the horizon; committed prefix inviolable, suffix open; committed stops only delay; frozen context; tours freeze at any decision, are born only at seeds |
| **C. Physical honesty** | freight conserved; one vehicle one place; deliveries need their pickups; readiness gates service; capacity + 13h/10h duty hard everywhere |
| **D. Solver** | only the uncommitted improves (every path guarded); coverage outranks distance; converge-stop; km physical, cost the objective |
| **E. Data** | enriched parquet = orders; vehicle_master = fleet; one path anchor; verify_legs never concurrent |
| **F. Reproducibility** | iteration-bound + cache snapshots; every run ends 0/0/accounted; migrations byte-gated |

Known gaps are listed in RULES.md **the day they are found** (none open as of 2026-07-14
evening).

## 5a · The vehicle-day activation cost (DEFAULT ON since 2026-07-15)

By default the objective is fuel-per-km only, so it has no reason to prefer reusing an
already-returned vehicle over opening a *fresh* one for a small intraday job — and with
per-vehicle catchments, a nearby fresh vehicle often wins outright on km. `--vehicle-day-cost`
(config `VEHICLE_DAY_COST_ENABLED`, env `FREIGHT_VEHICLE_DAY_COST`) adds a per-vehicle-day
**driver** activation cost so the search only opens another vehicle when the routing saving beats
a whole shift.

The cost of one occupied vehicle-day is a **guaranteed-shift floor + overtime**:

```
driver_day_cost(v,d) = hourly[τ(v)] · max(G, duty(v,d))          # 0 if the day is empty
```

- `G` = guaranteed paid shift = `GUARANTEED_SHIFT_HOURS` (default **9 h**; `--guaranteed-shift-hours`).
- `duty(v,d)` = `day_end − day_start` in hours; the **13 h** duty cap (RULES C4) means `duty ≤ 13`.
- A short day pays the flat 9 h **floor** (the activation cost); hours 9→13 add real **overtime**;
  an empty vehicle-day pays **0**.

So a returning driver still under 9 h absorbs the job for **free** (already-paid time), while a
fresh vehicle pays a whole £369/£428 shift — the search fills each activated vehicle toward its
duty limit before opening the next. It is wired into **both** the anchor ALNS *and* the
micro-insertion passes (five cost sites, one `driver_day_cost()` helper), because the micro-pass
ranks on its own km-delta — the intraday complaint lives there.

**Coverage is never traded for it.** The objective is lexicographic (RULES D: coverage outranks
cost), so this term can only re-rank *equal-coverage* plans; a fresh vehicle still opens whenever
it is the only feasible home for a job. This is exactly why an earlier scalar-penalty attempt
raised unassignment and this one does not. The seed is left untouched — it deliberately *spreads*
work for coverage safety (§ its `+10000` headroom rule); the ALNS consolidates afterward.

**Where the numbers come from — not invented:**

- **Driver hourly** £16.05 tractor (Class C+E) / £14.395 rigid (Class C1+C averaged) /
  £13.48 van (Class B) = UK DVSA licence-class wage survey, "avg adjusted for hours paid"
  upper bound, adopted 2026-07-27 (unknown types fall back to rigid). Supersedes the prior
  £47.59/£40.97 rates from `profitability_report/vehicle_cost_rates.json`'s
  `driving_hourly_gbp` (v2.1) — a fully-loaded/overhead-inclusive figure that overweighted
  driver cost relative to distance in the routing objective; that file is unchanged, since
  it serves a different (profitability-reporting) purpose.
- **Guaranteed 9 h** = **P25 of per-driver telematics duty spans** (supatrak Jan+Feb 2026, ~2,100
  weekday driver-days across 71–73 drivers; median driver day ~10 h, only ~13 % under 8 h) — the
  low end of a normal driver day. This *worked-span* figure brackets the guarantee to 8–10 h and
  rules out anything lower; the exact contracted minimum is a payroll fact — confirm if a firmer
  number is wanted. Tune per run via `--guaranteed-shift-hours`.
- **13 h ceiling** = the duty (`SHIFT`) feasibility cap in the evaluator — a wall, not a price.
- **Excluded: the £70/day standing cost** in the same file is *depreciation* — sunk, incurred
  whether the vehicle is driven or parked — so putting it in the objective would wrongly penalize
  *using* an owned vehicle. The activation cost we charge is the **driver**, not the vehicle.
- **Fuel** £0.319/0.216/0.150 per km = measured Jan-2026 Jigsaw tank-to-tank (unchanged).

**Provenance cut-line: DEFAULT ON since 2026-07-15** (`config.VEHICLE_DAY_COST_ENABLED = True`).
Runs before this date used the fuel-only objective; the two are NOT comparable, so pin a baseline
with `--no-vehicle-day-cost` (the fuel-only ablation, which reproduces the pre-cut behaviour) when
you need an apples-to-apples fuel-only reference. Spec + plan:
`docs/superpowers/{specs,plans}/2026-07-14-vehicle-day-cost*`.

**Validation — converged week** (2026-01-12→18, convergence gate, 9 h floor, `--no-vehicle-day-cost`
vs default): vehicle-days **229 → 198 (−13.5 %)**, distinct vehicles 62 → 57, at **identical
coverage** — not just the totals (`ON_TIME 1150 / NOT_PLANNED 58 / UNSERVED 15` both ways) but the
*same* 15 in-window UNSERVED orders and reasons, so the cost strands zero extra orders; the 58
NOT_PLANNED are the cold-start boundary set (collection predates the window). Combined km
45,136 → 46,719 (+3.5 %), the intended detour-for-fewer-shifts trade (milder than a short window's
+6.4 %). ≈ 31 avoided driver-days × ~£370–430 ≈ £11–13 k of shifts for the week vs ~£0.35 k fuel.
An earlier 2-day/400-iter run at the 8 h floor gave −8.8 % — that was the floor; the converged
week is the operating result.

## 5b · Soft delivery time windows (DEFAULT ON since 2026-07-18)

The objective's third cost line prices delivery **timing**. Previously a delivery that
could not make its intra-day window was a HARD `TIME_WINDOW` reject — which, with soft
coverage, forced the order to slip a whole DAY rather than be delivered a few minutes
late the same day (day-late preferred over minute-late — backwards for the customer).

Now a `CUSTOMER_DELIVERY` past its tight customer deadline is **feasible with a
penalty**, not rejected:

```
late(j)  = minutes past the tight deadline
early(j) = minutes before the window opens (range windows only)
cost(j)  = TARDINESS_COEF · late(j)²  +  EARLINESS_COEF · early(j)     # λ=0.05, convex (p=2), ε=0.1
```

The penalty is **convex** — a small slip is cheap, big lateness ramps hard — so the
solver treats lateness as a genuine last resort. This yields the service hierarchy
**on-time < early < late < slip/unserved**: slip/unserved is the top lexicographic
coverage tier (worst by construction), while on-time/early/late are the three levels
inside the cost tier. So a very-late same-day delivery still beats an on-time next-day
one, and an order slips ONLY when serving it today is duty-infeasible.

- **Delivery legs only** — pickups keep hard windows; the ~70% of orders with no stated
  window incur no penalty.
- The hard operating/duty bound (`latest_finish`, end-of-day) is unchanged — past it a
  delivery is duty-infeasible and slips.
- The DAY-granular service ledger (ON_TIME = right date) is unchanged; **intra-day**
  lateness is now a reported metric (02_kpi "Delivery timeliness", `route_stops.minutes_late`).
- **Validated + calibrated (2026-07-18):** the real op delivers point windows ~50 min
  early and hits range windows 97% not-late, so softening reproduces its behaviour; the
  model reaches ~99% on-time at λ=0.05 and is **λ-INSENSITIVE above ~0.05**. Wired via
  `alns._day_nonkm_cost` (bundles driver-day + lateness) at `route_cost` + the five
  insert-delta sites. Ablation: `--hard-time-windows` (hard-VRPTW cutoff). Spec + plan:
  `docs/superpowers/{specs,plans}/2026-07-18-soft-delivery-time-windows*`.

**Tour consolidation of depot-loaded directs** (`config.TOUR_DEPOT_DIRECT_AS_DELIVERY`, DEFAULT ON
since 2026-07-15; `--no-tour-depot-direct-as-delivery` reproduces the pre-fix split). A DIRECT move
collected AT its anchor depot (origin within `TOUR_ORIGIN_AT_DEPOT_RADIUS_KM`) is planned as a
depot-loaded delivery, so same/near-destination far orders consolidate onto one tour instead of a
dedicated vehicle each (fixes "one shipment split N ways" — 3 same-day Hull orders that had ridden
3 tractors). It also registers the freight `AT_DEPOT` (so the delivery commit doesn't reject it) and
emits the stop as `customer_delivery`. Coverage-safe (infeasible batches stay split; non-depot-origin
directs untouched). Details: PIPELINE.md §11.3; spec/plan
`docs/superpowers/{specs,plans}/2026-07-15-tour-depot-direct-consolidation*`.

**OSRM-timed tour boundary** (`config.TOUR_OSRM_DURATIONS`, DEFAULT ON since 2026-07-15;
`--no-tour-osrm-durations` reverts to the flat 50 km/h gate / 80 km/h executor, byte-identical).
Both the tour gate (`is_tour_only`) and the executor (`evaluate_tour`) time legs with OSRM
per-road-type durations — the same model the daily router uses — instead of a flat local speed,
so the "can't round-trip in a day" boundary reflects real motorway speed (~250 → ~425 road-km
one-way) and fewer mid-range orders are needlessly pulled into dedicated tours. Spec/plan
`docs/superpowers/{specs,plans}/2026-07-15-tour-osrm-durations*`.

## 6 · Verification and outputs

Two causality audits run at every dynamic finish (beyond the static gates):

- `audit_non_anticipation` — no collection arrived-at before booking (order-level);
- `audit_route_backdating` (2026-07-14) — no emitted stop planned in the past of the
  decision that created it: tour rows ≥ their creating seed's day (`tour_created_at`),
  daily stops ≥ their first-placement epoch (the `placement` trace). It runs LAST in
  finalize so a strict raise can never abort the forensic outputs.

`FP_STRICT_CAUSALITY=1` promotes both audits to raises (CI/gate use).

A dynamic run yields the **same run-folder contract as a static one** (manifest,
route_stops, KPI, handover, auto `plan_full.csv` — §13 emitter called once with the
merged result; folder anatomy in README.md) plus:

| artifact | content |
|---|---|
| `timeline.html` (run root) | AUTO: the evolving-plan gantt dashboard + the click-to-open **map view** (§below), built at the end of every dynamic run |
| `csv/plan_snapshots.csv` | the live plan at EVERY epoch (per vehicle-trip-stop: job, order, arrive/depart, committed flag, exact trip depot departure/return) — what a driver would have been sent at 00:00, at noon, at each micro |
| `csv/stop_provenance.csv` | every collection stop traced to the epoch/kind/floor that first placed it |
| `csv/non_anticipation_detail.csv` | the order-level audit detail |
| `csv/churn.csv`, `csv/micro_passes.csv` | per-pass insertion outcomes and plan-churn accounting |
| `csv/service_ledger.csv`, `rolling_manifest.json` | per-order outcomes; anchor/micro registry |
| `csv/trunk_schedule.csv` | per-night trunk sizing + named tractor draw (`vehicles`) and eligible pool (`feasible`) |

**The evolving-plan board** is auto-emitted at `<run>/timeline.html` (built before the
strict audit, so even a violating run keeps its board — it is a forensic surface: it
caught WT255038). Rebuild or export manually with `viz_timeline_build.py --run-dir
<window> --out <data.json> --html <board.html>` (template
`viz_timeline_template.html`; reads current and legacy run layouts): renders the plan as
it stood at any clock time —
day paging, per-vehicle lanes, the 90-min commit frontier colouring, micro-epoch strip,
trunk legs on their named tractor's own lane (⇅ teal), tooltips with per-stop provenance.
The board is how three of the shipped bugs were caught; treat it as a first-class
verification surface. Read-only, like all viz.

**The map view** (2026-07-14): the board and the map are **two parallel modes** — a
Board | Map toggle (top-right of each) switches between them. Map mode has a **left vehicle
sidebar** (depot-grouped, ⇅ trunk / ⛺ tour icons, its own day-nav) so you can pick any truck
without leaving the map; clicking a truck's name on the board also jumps straight to its map.
The route sits on a full Leaflet map, with a bottom time-strip (hover a block for detail) and
a separate clock slider below it.
The route is drawn on **road-snapped OSRM geometry** baked into the payload at build time
(`viz_geometry.py`; each unique leg fetched once, disk-cached; straight-line fallback on
an OSRM miss; `--no-geometry` skips it and the board is unchanged). Stops are numbered by
sequence and shaped/coloured by leg type (pickup circle · delivery square · direct
diamond), depots are yellow pins. One **master clock** drives everything: scrubbing the
bottom strip (or Play) re-forms the route as the plan re-optimises across epochs (verified:
a vehicle grows 6→12 stops as micros land) AND slides a simulated truck puck along the
committed route by planned times (`MAPLOGIC.truckPos`, interpolated on the baked geometry).
An iOS-style **committed ⟷ internal** switch overlays the driver-committed route (solid)
against the optimizer's full internal plan (dashed, = committed prefix + the uncommitted
tail) in the vehicle's own hue — the divergence is the tentative work; uncommitted stops
render faded. Commitment on the map uses the **same 90-min frontier as the board**
(`MAPLOGIC.commitFlags` = the board's `firm||done` rule), so the two always agree — a stop
commits once the clock reaches 90 min before its drive start, not when the snapshot's raw
flag flips. **Direct carries** draw their collect ORIGIN as well as the deliver dest (the
route runs depot→collect→deliver and the simulated truck threads through the collect
point) — a single-point rendering would skip the pickup when it sits away from the depot.
**Multi-day tours** (⛺) are seed-committed and live OUTSIDE the snapshot stream, so they
are drawn from a separate per-day route (`veh.tourDay`, `MAPLOGIC.tourDayNodes`) and are
**split by day**: the map shows only the leg that belongs to the day in view — day 1 leaves
the depot and stays out overnight (no depot return), a later day RESUMES from where the truck
parked (the previous day's last stop, marked faintly), and only the final day returns to the
depot. The ⛺ symbol, the coloured route, the animated truck AND the strip blocks appear only
on the days a vehicle is actually touring (driven off the per-day `tsegs`, not a global
flag) — an ordinary local-work day stays clean. A tour's depot-out / overnight-resume /
depot-return legs carry no recorded times, so the puck's drive time along them is synthesized
from road distance at the tours' own planning speed (`TOUR_ANCHOR_KMH` = `MULTIDAY_AVG_SPEED_KMH`
= 80, display-only — no plan/KPI number depends on it); the customer stops keep their real
planned times. The map's bottom strip renders a tour from the **same** nodes, so each block
spans the whole leg (drive-start → depart) like a normal vehicle's — the depot-out drive folds
into the first block and the final day shows its return-to-depot leg (a depot-coloured block).
Hovering the bottom strip shows the same per-stop popup as the board lanes. The
route-reconstruction math lives in `viz_timeline_maplogic.cjs` (one source, inlined into
the template at build AND Node-unit-tested: `tests/freight_planner/maplogic.test.cjs`).
The map needs internet (Leaflet + OSM tiles from CDN); the gantt itself stays offline-capable.

## 7 · Running it

The frequently-used flags together — a standalone window (no prior-week chaining), explicit
convergence gate, and micro cadence, so the command is fully reproducible on its own without
cross-referencing `config.py`:

```powershell
python -B -m freight_planner.run_rolling --start 2026-02-02 --end 2026-02-03 `
  --out-dir freight_planner/runs_<label>_2day `
  --iterations 10000 --seed 0 --delta-r1-min 90 --micro-every-min 30 --converge-pct 5
```

Bash (Git Bash / WSL) equivalent, one line, no backtick continuations:

```bash
python -m freight_planner.run_rolling --start 2026-02-02 --end 2026-02-03 --out-dir "freight_planner/runs_<label>_2day" --iterations 10000 --seed 0 --delta-r1-min 90 --micro-every-min 30 --converge-pct 5
```

Chaining onto a prior window adds `--handover-in <prior window>/plan/handover.json` (carries
in-flight vehicles, delivered orders, staged freight — see the flag table below).

**`--converge-pct 5` (5%) is a deliberately LOOSE gate**, not the default
(`config.ALNS_CONVERGE_PCT` = 0.15%) — it stops each anchor far sooner, trading solution
quality for fast turnaround during development/debugging/comparison sweeps. **Do not cite
numbers from a `--converge-pct 5` run as final results** — rerun with the default (omit the
flag, or pass `--converge-pct 0.15`) before reporting. `--micro-every-min 30` above matches
the config default; it's spelled out explicitly for the same reproducibility reason.

| flag | meaning (default) |
|---|---|
| `--iterations` | ALNS iterations per ANCHOR epoch (10000; a cap — the convergence gate usually stops earlier) |
| `--converge-pct` / `--converge-window` / `--converge-min-iters` | per-anchor convergence gate overrides (default: `config.ALNS_CONVERGE_PCT/WINDOW/MIN_ITERS` = 0.15% / 500 / 1500); `--converge-pct 0` = fixed budget for provenance replays |
| `--epochs` | anchor times per day (`00:00,12:00` — 00:00 midnight seed + noon re-opt) |
| `--micro-every-min` | insertion-pass cadence; 0 disables (config.MICRO_EVERY_MIN = 30; pre-2026-07-14 replays used 60) |
| `--delta-r1-min` | the commit lag Δ (90) |
| `--delta-min` | VESTIGIAL, CLI compatibility only |
| `--handover-in` | prior window's `handover.json` (chains weeks; in-flight vehicles, delivered orders, staged freight) |
| (orders input) | FIXED to the combined Jan+Feb enriched parquet since 2026-07-22 — no `--qargo` flag (monthly files are booking-month universes and miss cross-month dues; the override invited that mistake). Reproducing pre-2026-07-22 monthly-file runs requires editing `paths.DEFAULT_ENRICHED`. |
| `--beta` | warm-start stability weight: objective = cost + β·disturbance (0 = pure cost, the regression gate) |
| `--vehicle-day-cost` / `--no-vehicle-day-cost` | driver-day activation cost in the objective (default ON since 2026-07-15; `--no-vehicle-day-cost` = the fuel-only ablation; see §5a) |
| `--guaranteed-shift-hours` | paid minimum shift = floor of the driver-day cost (`config.GUARANTEED_SHIFT_HOURS` = 8) |
| `--no-overtime-cost` | drop the overtime + 19:00 late-ramp from the driver-day cost (straight-time ablation) |
| `--no-depot-pinning` | let any vehicle serve any leg (the cross-depot teleport ablation; default pins pickup→target / delivery→source depot, §C16) |
| `--hard-time-windows` | delivery windows become a HARD cutoff instead of the default soft tardiness penalty (§5b; the hard-VRPTW ablation) |
| `--tardiness-coef` / `--earliness-coef` | soft-window penalty weights (GBP per late-minute² / per early-minute; defaults λ=0.05, ε=0.1) |
| `--readiness-lag-min` | EXPERIMENT: floor PL_IMPORT delivery departures to 06:00 + M (default 0 = off; retired as a results experiment 2026-07-18) |
| `--strict` | **degenerate floor config**: whole-trip freezing, no suffix insertion, no micro passes — the lower-bound measurement, NOT the causality gate (that is `FP_STRICT_CAUSALITY=1`) |
| `--trace` | per-anchor ALNS anytime curves (`reports/trace_ep*.csv`) |
| `--seed` | RNG seed (0) |

**The convergence gate in dynamic runs.** Every anchor solve carries the same gate as the
static planner (RULES D3): stop once the best objective improves by less than
`ALNS_CONVERGE_PCT` (0.15%) over the last `ALNS_CONVERGE_WINDOW` (500) iterations, after
at least `ALNS_CONVERGE_MIN_ITERS` (1,500) — a served-count gain always counts as
improvement, and `--iterations` is the cap, not a target. The knobs live in
`freight_planner/config.py` and, since 2026-07-14, are per-run adjustable on this CLI too
(`--converge-pct/-window/-min-iters` — they flow into every anchor's solve). Micros are
insertion passes, not searches, so the gate does not apply to them.

Reproducibility: same protocol as static (README.md) — `PYTHONHASHSEED=0 python -B`,
fixed iterations, cache snapshots. A rolling run is deterministic given those (two
identical smoke runs produced identical outputs, wall-clock stamps aside).

## 8 · Behaviour notes (what to expect on the board)

- The 00:00 midnight seed lays the day; the noon re-opt visibly reshuffles only the uncommitted
  tail; committed stops only ever slide later. If a committed stop *moves*, that's a bug —
  report it (board observations caught the tour-backdating, merge-sweep AND
  departure-flooring defects). Likewise: a block whose DRIVE toward it begins inside the
  90-min frontier of the epoch that created it is a violation (B2 departure-based
  flooring) — the drive segment, not just the stop, must sit beyond the frontier.
- A far order booked at 09:30 does NOT get a new tour that day (B7) — it waits for the
  next 00:00 midnight seed and shows as SLIPPED. That's the designed cost of tour stability, not a
  defect.
- Micro-inserted trips commit like any other: once their departure is inside now+Δ they
  launch, and later epochs treat them as in-flight.
- Coverage under rolling is lower than static on the same window — that gap is the price
  of online knowledge, and comparing the two is the point of the exercise (E6). Quote both
  with their mode named.
