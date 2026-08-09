# PIPELINE.md — the freight planner, end to end

**Status: the design-of-record for how the planner actually runs (code-verified 2026-07-13).**
The system now runs primarily as a **dynamic rolling-horizon dispatcher** (`run_rolling.py`,
§13a); the full-knowledge static planner (`run_alns.py`, §1–§13) is the shared machinery it
drives and the optimistic backtest bound.
Every claim below was checked against source on the date above; module and function names are
the anchors (line numbers drift, names don't). This document describes the *current* system —
setup/layout/data-quality guide in `README.md`, narrative deep-dives in `README_STATIC.md`
(the static ALNS planner) and `README_DYNAMIC.md` (the rolling dispatcher), the original
design rationale and decision log in `DESIGN_LOG.md` (was README.md until 2026-07-14), and
the chronological history in `QUEST_LOG.md`.

Reading guide: **§−1 orients a reader with no prior knowledge** (business context, object
model, codename legend, a worked example). §1–§10 build the problem (data → jobs), §10a
states it formally, §11–§12 solve it (staged seed → ALNS), §13–§15 account for it (outputs,
invariants, reports). §16 is the single authoritative table of what the solver can and
cannot change. §18 lists every tuned constant with its value and provenance.

Confidentiality note: this is an internal working document — real site names, postcodes and
measured cost rates appear here for engineering precision. Dissertation and any external
prose must use the anonymised forms (see the dissertation folder's AGENT.md rules); never
copy this file's identifying details into publishable text.

---

## −1. Orientation — what this system is (read this first if you're new)

**The business.** A UK regional palletised-freight carrier runs a hub-and-spoke *groupage*
network: many small consignments (1–26 pallets) share vehicles. It operates three depots
(one anchor, one full second depot, one single-vehicle satellite) and is a member of two
national pallet networks — overnight trunk vehicles exchange freight with the networks'
national hubs, so freight the network brings IN is delivered locally by this fleet
(`PL_IMPORT`), freight collected locally and handed OUT rides the network beyond the
region (`PL_EXPORT`), and `FULL_FLEET` orders are carried end-to-end by this fleet alone.
Dispatchers currently plan each day manually in a TMS ("Qargo" — orders carry human-facing
"WT" numbers). **This system replaces that manual planning**: it ingests a week of TMS
orders and emits vehicle-by-vehicle, stop-by-stop route plans.

**The shape of the computation.** One solve per Mon–Sat week: every day of the week
coexists in a single solution (state keyed `(vehicle, day)`), so the search trades work
between days as well as between vehicles. Weeks chain through a handover artifact (§4).
Service is *optional but lexicographically protected*: an order the plan cannot feasibly
serve is surfaced with an explicit reason rather than silently forced in or dropped —
misses are information (subcontract/roll-over decisions), and no distance saving can ever
displace a served order.

**Operative mode: dynamic rolling-horizon dispatch.** The system now runs primarily as a
**dynamic dispatcher** (`run_rolling.py`, §13a): it replays each day as decision epochs,
seeing only the orders knowable at each moment and *committing* work to drivers as it
dispatches — the realistic online problem, and the one a live product would run. The
full-knowledge **static** planner (`run_alns.py`, §1–§13) is the shared machinery it drives
and the optimistic full-knowledge bound. Both are **validated** against January–February
2026: re-plan those weeks from the same order data the humans had and compare against what
the fleet *actually drove* (GPS/odometer telematics — "the incumbent"). The honest
comparison is the **(vehicle, day)-matched gap** (same vehicles, same days, plan km vs
odometer km); fleet-total comparisons are context only because the odometer includes
non-order movement. Live deployment would change data feeds (trailing-month catchment
calibration, ops-confirmed windows), not the algorithm.

**The object model** (each stage's noun, in order):

| object | one per | produced by |
|---|---|---|
| order | commercial consignment (TMS row, WT number) | input |
| demand record | in-window order + responsibility verdict | §2 |
| movement leg | physical movement the order implies (an order yields 1–3+, incl. mutually exclusive options) | §3 |
| candidate job | dispatchable leg made schedulable (windows, dependencies, blockers) | §6 |
| freight unit | conserved thing the ledger tracks (order, or split part of one) | §3/§10 |
| manifest movement | accounting row every order resolves to (routed / accounted / unassigned-with-reason) | §13 |

**Codename legend** (internal names used throughout this repo's docs and comments):
`cambridge/` = the LEGACY pipeline package. Since the 2026-07-13 codebase separation the
planner imports NOTHING outside `freight_planner/`: the load-bearing pieces (calibrated
constants, scope policies, depot config, verified-legs machinery, postcode resolver, the
OSRM router, telematics replay loading) are vendored VERBATIM into `freight_planner/shared/`
— every reference below of the form `shared.<module>` is one of those copies, and older
mentions of `cambridge.<module>` describe the same code at its pre-separation home (the
legacy packages now live in `_archive/2026-07-13_separated_legacy/`). The
migration is proven byte-identical (static + rolling 6-day gates; spec
`docs/superpowers/specs/2026-07-13-freight-planner-separation-design.md`). B-numbers =
logged bugs (B14 two-point tour
gating, B15 vehicle catchments, B16 the phantom-saving/silent-job-loss ALNS bug). K1 =
mega-shipper shuttle carve-out; K2 = day-flexibility (dormant). T1/T2 = the fixed night
trunk services (B37 Palletline hub / LE10 Hazchem hub). M-numbers = build milestones.
E0–E6 = the Ch.5 experiment campaign. Q-numbers = the eleven recorded design decisions
(DESIGN_LOG.md). **Convergence gate (2026-07-13, default ON):** every solve stops once the best
objective improves by less than `ALNS_CONVERGE_PCT` (0.15%) over the last
`ALNS_CONVERGE_WINDOW` (500) iterations, after at least `ALNS_CONVERGE_MIN_ITERS` (1,500) —
`--iterations` is now a CAP, not a target; a served-count gain always counts as improvement.
Anchored on the budget sweeps (1800→3600 s bought only −0.7%). `--converge-pct 0` restores
the fixed-budget behavior (pre-gate provenance replays need it). **Trunk deck = 52-pallet
double-deck** (`TRUNK_DECK_PALLETS`, RESTORED 2026-07-21 — the 2026-07-12 single-deck 26 was
a proxy for un-modelled day flow; the hub dock-visit census now measures it: reality's ~9
round-trips/day can only carry the ~500-pallet nightly demand double-decked, and val4
reproduced reality's trip count exactly at 52 — §11.5). Pallet capacity only; payload caps
untouched. CUSTOMER-leg artic capacity stays 26 (raising it is an open scope decision).
"OVERFLOW" = work whose natural depot cannot stage it (e.g. the dockless satellite), which
therefore stages at the anchor-depot gateway.

**Repository layout** (post 2026-07-13 separation + reorganisation):

```
freight_planner/
  *.py                    the pipeline (flat, import-stable: provenance-pinned commands
                          like `python -m freight_planner.run_alns ...` keep working)
  shared/                 vendored network-model library — config, scope, plan_types,
                          verified_legs, postcode_resolver, routing (OSRM), fleet_replay_data;
                          paths.py holds LOGISTICS_ROOT, the ONE data-file anchor
  tools/                  offline data-repair: verify_legs, export_replay,
                          build_vehicle_master (run as `python -m freight_planner.tools.<x>`)
  data/                   small planner-owned artifacts (vehicle_master.csv,
                          verified_legs.csv — the runtime responsibility truth,
                          mot_results.csv, enriched_orders parquet)
  runs/                   CANONICAL monthly outputs — runs/<YYYY-MM>/<window>/
  runs_archive/           finished ad-hoc & smoke runs + their console logs
  experiments/            Ch.5 campaign (gitignored; README/METRICS/PROVENANCE = record)
  viz_timeline_template.html  the dispatch-board template (viz_timeline_build --html)
  README.md (guide) · README_STATIC.md · README_DYNAMIC.md · PIPELINE.md (this file) ·
  RULES.md (invariants + known gaps) · QUEST_LOG.md (chronology) · DESIGN_LOG.md (rationale)
```

Data inputs stay OUTSIDE the package at the logistics root (`data/`, `depot_data/`,
`fleet_replay_exports/`, `.cache/`) and are reached only via `shared.paths.LOGISTICS_ROOT` —
a relocated copy of any module must fail loudly, not read nothing (the partb6 lesson).

**One order end-to-end** (multi-day FULL_FLEET, the richest path): a 6-pallet order,
collect Tuesday at customer A, deliver Thursday at customer B. §2 verifies it is ours
end-to-end → §3 emits THREE legs in two mutually exclusive groups: `DIRECT` (one
customer→customer move, Thursday) vs `XDOCK` (`CUSTOMER_PICKUP` Tuesday + depot
`CUSTOMER_DELIVERY` Thursday) → §6 makes jobs of them: the XDOCK delivery depends on its
pickup (`REQUIRES_PRIOR_PICKUP`) → §8 resolves the mode (say XDOCK: the depot is on the
path) → §9 masks which vehicles may serve each job → §11 seed inserts the pickup into a
Tuesday rigid route; the ledger records the freight at the depot Tuesday night → §12 ALNS
later moves the Thursday delivery onto a cheaper tractor's second trip (allowed: predecessor
placed, capacity/windows/duty all re-evaluated) → §13 the manifest shows two ROUTED
movements with vehicle/route/km; `route_stops.csv` carries the stop timings; if instead
NO feasible slot had existed, the order would appear UNASSIGNED with the failure reason.
Had it been same-day and far from the depot, the resolver would have kept DIRECT and §11's
tour path could have carried it on a multi-day sweep.

---

## 0. Entry points and orchestration

| entry point | role |
|---|---|
| `run_alns.py` | plans ONE window (the unit of planning is a Mon–Sat week) with FULL knowledge — the static planner and the backtest baseline. Parses args, builds inputs, runs the staged seed + ALNS, writes all outputs. |
| `run_rolling.py` | plans ONE window as a **dynamic rolling-horizon dispatcher** — replays the week as decision epochs with only the orders knowable at each moment, committing work as it dispatches (§13a). Wraps `run_alns`'s builders/solvers; same output contract. This is the realistic dispatcher; `run_alns` is the full-knowledge bound. |
| `run_month.py` | shells `run_alns` once per window in order, wiring each week's `--handover-in` to the prior week's emitted `plan/handover.json`; `--initial-handover` seeds the first window from a previous month; `--qargo` passes the order file to every window (default since 2026-07-22 = the COMBINED Jan+Feb enriched file, so month-boundary windows see late-prior-month bookings). After the chain it emits per-week viz and the month rollup. NO planner logic lives here. |
| `build_phase0.py` | standalone data-spine build (demand/legs/states CSVs without planning) — shares the same input builders. |

Defaults that define the production configuration (all overridable):
`--time-budget 120` s, `--iterations 100000` (cap never binds at 120 s), `--no-improve 4000`,
`--seed 0`, `--restarts 1`, `--sa-temp 0.005`, `--sa-cooling 0.999`, `--repair-every 20`,
`--router osrm`, `--date-basis planning_window`, `--responsibility-mode forward_structural`,
`--vehicle-day-cost` **on** (driver-day activation cost, default since 2026-07-15, §10a;
`--guaranteed-shift-hours 9`; `--no-vehicle-day-cost` = fuel-only ablation).
Every run writes `run_manifest.json` (window, args, `env_toggles`, timestamp) before doing
anything else, so every output directory is self-describing and reconstructable.

Output layout (`output_layout.py`, restructured 2026-07-14): `runs/<YYYY-MM>/<start>_to_<end>/`
with the deliverables at the run ROOT (`run_manifest.json`, auto `plan_full.csv`,
`runsheets.html`, dynamic `timeline.html`, `alns_progress.log`, `handover.json`,
`validation_metrics.json`), all tables in `csv/`, all markdown in `reports/`. Writers route by
extension through `RunPaths`; readers resolve BOTH this and the legacy
`{inputs,plan,reports}` layout via `find_artifact`/`artifact_dir` (old runs stay
viewable). `inputs/` is created only by `build_phase0`. The mode/basis appear in the
folder name only when non-default.

---

## 1. Raw inputs

| input | path / source | role |
|---|---|---|
| Orders (enriched) | `freight_planner/data/enriched_orders_2026-01_2026-02.parquet` — the COMBINED Jan+Feb file, the DEFAULT `--qargo` since 2026-07-22. The monthly files (`enriched_orders_<month>.parquet`) are BOOKING-month universes: a window in a month's first week silently misses deliveries booked late the prior month (the Feb-2 hole — 521 dues live in the Jan file), so cross-boundary windows need the combined file. It is a plain concat of the monthly files and MUST be rebuilt whenever a monthly file is regenerated (guard test: `test_default_qargo_is_the_combined_cross_month_universe`). Caveat: vehicle-catchment radii calibrate on the whole input frame, so combined-file runs are not calibration-identical to monthly-file runs; the `--qargo` CLI override was REMOVED 2026-07-22 (fixed input, user rule) — reproducing earlier monthly-file results means editing `paths.DEFAULT_ENRICHED`. Each monthly enriched file mirrors the raw `data/Input/orders/qargo_<month>.parquet` universe 1:1 plus the `verified_*` columns. Raw files remain the enrichment inputs. | the commercial order universe: ids, WT names, status, flow tag, origin/destination postcodes + requested timestamps, pallets/weight, written resources (`resource_rigid/tractor/van`, `resource_subcontractor`), import-integration type (hazchem detection) + embedded `verified_leg/confidence/method`. |
| Verified legs | `shared.verified_legs` machinery over telematics | per-order GPS-verified responsible-leg + corrected flow — the forward-mode ownership truth that OVERRIDES the raw API flow tag. Embedded in the enriched orders file (preferred path); `freight_planner/data/verified_legs.csv` is the regen artifact and runtime fallback. |
| Postcode cache | `data/Output/postcode_cache.json` | geocode store (compact-postcode key). Live lookups on miss; failures cached as versioned failure markers; outcode fallback for malformed inward codes. |
| Vehicle master | `freight_planner/data/vehicle_master.csv`, built by `tools/build_vehicle_master.py` (untracked; regenerate) | since 2026-07-13 the ONE runtime fleet file: per-vehicle depot + fleet_kind (from CircuitName — the circuit map lives in the tool), the full dispatcher profile (shift spans, per-trip capacities + provenance, multi-trip stats), physical payload/pallets, MOT. `shared/config` reads the fleet from it; the supatrak vehicle list + `vehicle_profiles_derived.json` are REGEN inputs of this file, not runtime inputs. Fleet ceiling read by `vehicles.fleet_capacity_ceiling()`. |
| Depot config | `shared/config.py` | `DEPOT_ANCHORS`: CB22 (Duxford, CB22 4PS), BEDFORD (MK42 0LF), STOKE (ST4 8HP satellite), ST_IVES (PE27 3WR, structural key only); `VEHICLE_DEPOT_MAP`, `VEHICLE_PROFILES` now loaded from the vehicle master (legacy supatrak/JSON builders remain only as the no-master fallback). |
| Telematics | `data/Input/supatrak/…csv` (monthly) | NOT a planning input — used by catchment calibration (via qargo resources), and downstream by validation/viz only. |
| Prior handover | `<prior window>/plan/handover.json` | opening state (§4). |

**Audited order-load corrections.** `build_enriched_orders()` preserves the raw
weight as `goods_weight_reported` and exposes the planner's effective value as
`goods_weight`. Three UUID-keyed corrections are currently approved: WT259833
5,991,360 g → 5,991.360 kg; WT271534 and WT271550 320,000 kg → 22,432 kg each
from the order documentation. `goods_weight_correction_reason` records the
provenance. Zero/missing weight or pallet values are reported but do not exclude
an otherwise in-scope movement, preserving its geography and kilometres.

---

## 2. Demand model (`demand.py`)

`build_demand_records(qargo_df, start, end, responsibility_mode)` → one `DemandRecord` per
in-window order.

**Flow and responsibility.** The raw Qargo `api_flow` is only a fallback:
`_flow_and_leg()` calls `corrected_flow()` / `verified_leg()` so the telematics-verified leg
decides what we were actually responsible for. `responsibility_shape()` maps
(flow, verified leg, exclusion) to one of: `FULL_END_TO_END`, `NETWORK_IMPORT`,
`NETWORK_EXPORT`, `PICKUP_ONLY`, `DELIVERY_ONLY`, `OUT_OF_SCOPE`, `AMBIGUOUS_PARTIAL`, each
with a provenance source (`telematics_verified` / `structural_rule` / …). Two modes exist:
`forward_structural` (production — structural rules where no verified leg) and
`backtest_verified`.

> **Naming trap — read before reasoning about who does which leg.** The
> `NETWORK_*` shapes name the leg the PARTNER NETWORK performs; **OUR** leg is
> the OTHER one. `NETWORK_EXPORT` (raw `PL_EXPORT`) = the network does the
> export *delivery*, so **WE COLLECT** (a `CUSTOMER_PICKUP`). `NETWORK_IMPORT`
> (raw `PL_IMPORT`) = the network already did the import *collection*, so **WE
> DELIVER** (a `CUSTOMER_DELIVERY` out of our depot). `NETWORK_IMPORT` does NOT
> mean "the network does it all." `FULL_END_TO_END` (`FULL_FLEET`) = we do both
> legs; `PICKUP_ONLY`/`DELIVERY_ONLY` = local single-leg. `responsibility_source
> = telematics_verified` means the GPS confirms we actually ran our leg → in
> scope. Mislabelling an import as "network handles it" silently drops every
> import delivery (the 2026-07-11 regression: 1 146 import deliveries → 0).

**Window membership** (`in_window`): collection-anchored flows (PL_EXPORT, LOCAL_COLLECT)
by collect date; delivery-anchored (PL_IMPORT, LOCAL_DELIVER) by deliver date; FULL_FLEET by
either.

**Exclusions** (`exclusion_reason`, evaluated in order): `CANCELLED`; `CRANE_HIRE` /
`SPECIALIST_MOVEMENT` (vehicle-category strings); `AMBIGUOUS_MANUAL` (no flow derivable);
`NO_RESOURCES` — **no fleet vehicle was ever written on the order; a subcontractor does NOT
count** (stakeholder ruling 2026-07-02: those are third-party/network movements — the class
that once sent phantom tours to Scotland). Excluded orders stay in the universe as
accounting rows so coverage always reconciles (§13).

---

## 3. Movement legs (`legs.py`)

`build_movement_leg_records(qargo_df, demand_records, postcode_cache)` → the physical
movement universe. Each `MovementLegRecord` carries: leg/freight/order ids, flow, kind,
origin/destination NODES (`CUSTOMER`/`DEPOT`/`B37_HUB`/`LE10_HUB`), service postcode, staging
depots, hub, ready/result freight states, raw AND effective time windows + hardness, freight
ready time, pallets/kg, geocode status, and the mutual-exclusion option fields.

**Per-flow emission:**

- **Excluded orders** → one `ACCOUNTING_ONLY` leg (`result_state` = the exclusion reason).
- **PL_IMPORT** (network brings freight to our depot; we deliver): an `INBOUND_TRUNK`
  accounting leg (hub → depot, dated the day BEFORE the requested delivery) + a dispatchable
  `CUSTOMER_DELIVERY` (depot → customer).
- **PL_EXPORT** (we collect; network delivers) — **two mutually exclusive option groups**:
  `TRUNK` = `CUSTOMER_PICKUP` (customer → depot) + `OUTBOUND_TRUNK` accounting leg
  (depot → hub); `HUBDROP` = one two-point `HUB_DROP` leg (customer → hub directly, the
  collecting vehicle drops at the hub, freeing trunk deck). Resolved in §8.
- **FULL_FLEET, different-date** (collect date ≠ deliver date) — XDOCK only:
  `CUSTOMER_PICKUP` on the collect day followed by `CUSTOMER_DELIVERY` from the depot on
  the delivery day. A vehicle is not allowed to hold ordinary freight between those dates.
  The delivery leg may still use a multi-day tour after the freight has been staged.
- **FULL_FLEET, same-day** — same DIRECT vs XDOCK pair; the XDOCK delivery's effective
  window start is pushed to collection-deadline + `SAME_DAY_XDOCK_HANDOFF_MIN` (90 min dock
  handoff), so a same-day crossdock is only offered when physically staged in time.
  **Collocated exception (2026-07-17):** when the origin geocodes within
  `DAILY_ORIGIN_AT_DEPOT_RADIUS_KM` (2 km) of the source depot's anchor
  (`DAILY_DEPOT_DIRECT_AS_DELIVERY`, default ON), NO option pair is emitted — the order
  becomes ONE depot-loaded `CUSTOMER_DELIVERY` (`:DIR` leg id kept, `ready_state=AT_DEPOT`,
  `origin_pc` retained for provenance) carrying `depart_floor` = collection-open +
  `COLLOCATED_STAGING_MIN` (30) and `depot_bound` = the source depot. Same-origin orders
  then co-load like any deliveries (the ST4 8JB customer on the Stoke estate: previously
  each same-day DIRECT was an atomic collect→deliver arc, so consecutive orders ping-ponged
  through the yard — deadline+90 made the XDOCK alternative window-infeasible for exactly
  these wide-collection-window orders, forcing the DIRECT). Freight-state derivation needs
  no special case: the delivery-only shape branch places the freight `AT_DEPOT_OR_HUB_PENDING`
  at the source depot with the staged ready time.
- **LOCAL_COLLECT / LOCAL_DELIVER** → single pickup / delivery leg.

**Windows.** `delivery_windows`/`pickup_windows` come from `shared.scope` policies: raw
TMS stamps are expanded to operating-day windows where the raw stamp is a date-only midnight
artifact, and collections NEVER comply with historical actual times — using the time a
collection *actually happened* as its constraint would leak the human plan's answer into
the backtest (hindsight hardening: the planner may only know what a dispatcher knew before
planning). Both raw and effective windows are kept on the record with a `window_hardness`
tag naming which policy produced them; the planner enforces the effective window.

**Splits.** `_split_parts` chunks orders above the fleet ceiling
(`MAX_VEHICLE_PALLETS/MAX_VEHICLE_KG` from the vehicle master) into per-part freight ids
(`<order>#S<i>`, legs `:C_S1of2` etc.) — the multi-vehicle dispatch a real operator would
send. Since 2026-07-16 (user decision) the split applies to EVERY flow branch — FULL_FLEET,
PL_IMPORT, PL_EXPORT, LOCAL_COLLECT, LOCAL_DELIVER — not just FULL_FLEET (the old asymmetry
is why seven 30-34-pal FULL_FLEET orders planned as splits while identical-load import/
export twins were labelled `MASSIVE_UNSUPPORTED`). Export parts keep the TRUNK-vs-HUBDROP
choice mutually exclusive per part (`option_set` = part freight id). The ONLY loads that
never split are HAZCHEM consignments (DESIGN_LOG: "never split hazardous/specialist
orders" — each carrying vehicle would need an ADR-certified driver, a re-issued
dangerous-goods declaration and segregation checks the model does not track), so an
over-ceiling hazchem order is the only remaining `MASSIVE_UNSUPPORTED` and leaves the
universe via the all-legs-massive rule (RULES.md). Empirically moot today: of 1,393
hazardous orders in 2026-01, ZERO are over-ceiling (all 29 over-ceiling orders are
ordinary freight). CAVEAT for plan-vs-actual reads: a split costs the model ~2
vehicle-days where the real op ran ONE double-deck artic — a bias AGAINST the model on
these ~29 orders/month. The TRUNK deck is 52 since 2026-07-21, but CUSTOMER-leg artic
capacity remains vehicle_master's 26 — direct evidence the real trailers double-deck on
deliveries too: WT267025 (31 pal, 25.3 t) rode ONE artic (X88RNW) to B32 3BZ on 2026-02-16
while the plan split it 15.5+15.5. Raising customer-leg capacity is an open scope decision.
Downstream, records carry the PART id in `order_id` (`uuid#S1`) so the freight ledger
gates per part; every parent-level accounting boundary must normalize with
`order_id.split("#", 1)[0]` — `collection_orders_in_plan` and the tour crediting do
(Scenario C fix, 2026-07-16: the finalize reconcile compared part ids to parent ids and
demoted every split order to NOT_IN_PLAN despite both parts being planned and launched).
Hazchem rows (subcontractor / import-type string contains "hazchem") route to `LE10_HUB`
(LE10 3BS), everything else to `B37_HUB` (B37 7HB, the Palletline national hub).

---

## 4. Window filtering and the handover chain

- `date_basis.filter_legs_by_basis(legs, start, end, "planning_window")` scopes the leg
  universe to the week (other bases: `service_date`, `demand_touch`, `manifest_compat`).
- In the rolling path, `_clamp_future_candidates` prevents ordinary daily routes after
  the requested window end. The paired future leg remains in the order universe: when
  its collection is served, the freight closes `AT_DEPOT` and is passed to the next
  window through `handover.json`. Multi-day tour stops are the only planned vehicle
  activity allowed to cross the boundary, because the tour is already physically in
  flight.
- **Handover-in** (`handover.py`): week N+1 consumes week N's `handover.json` —
  (a) `delivered_order_ids` are dropped from the leg frame (spill orders the prior plan
  already delivered — prevents double-planning); (b) `apply_availability` holds in-flight
  vehicles (on multi-day tours at window open) until their `available_from`;
  (c) `staged_freight` seeds initial freight states at the depot the prior plan left them,
  using the selected pickup leg's physical depot and ready time. Empty/absent handover =
  cold start.

---

## 5. Vehicles and learned catchments

`vehicles.vehicle_states_frame(start)` builds the fleet frame from the shared config:
one row per vehicle with type, capacities (the master's physical payload/pallets), home
depot + anchor coordinates, and a fleet-wide availability of **06:00 with no end wall**
(user rule 2026-07-16: telematics shift medians and trip-count history are NOT operating
constraints — the 10h driving / 13h duty caps bound the day; 19:00 is a soft target,
coverage first). The operating window is TWO-LAYER (2026-07-20, calibrated on the hourly
movement curve): vehicles roll from `FLEET_DAY_START_HOUR` 06:00, but CUSTOMER service
windows open at `CUSTOMER_DAY_START` 08:00 (shared/config; applied at every window-open
site in scope.py) — early hours are for depot work and driving, not doorsteps.

`catchment.build_vehicle_catchment` (B15): each vehicle's service radius = **P95** of
haversine distances from its home anchor to postcodes of orders historically written on it;
< `CATCHMENT_MIN_SAMPLES` (20) falls back to the fleet-wide per-type P95; floored at
`CATCHMENT_RADIUS_FLOOR_KM` (30). The radius feeds a SOFT ranking penalty only
(`vehicle_cost.out_of_area_penalty_km`: each km beyond the radius counts
`OUT_OF_AREA_KM_FACTOR` = 2.6× extra in the objective) — no hard gate, so coverage can never
drop because of territory. Calibrating from the same month is a fleet-behaviour prior, not
per-order hindsight; live deployment would feed trailing months.

---

## 6. Candidate jobs and precedence (`jobs.py`)

`candidate_jobs_frame(legs, vehicles, start)` converts dispatchable legs to schedulable
`CandidateJobRecord`s (`job_id = JOB:<leg_id>`): windows (`earliest_start`/`latest_finish` =
the effective window), freight ready time, sizes, allowed vehicle types, feasible-vehicle
counts, and:

**Dependency typing** (`_dependency_maps`, grouped per freight unit):
`PRODUCES_DEPOT_FREIGHT` (a pickup feeding a later delivery) ↔ `REQUIRES_PRIOR_PICKUP`
(the delivery; carries `predecessor_leg_id`); `PRESTAGED_DELIVERY` (freight already at the
depot — no in-window pickup, or the pickup predates the window: the run-to-run state-gap
fix); `PICKUP_TERMINAL` (collection-only, incl. hub-drops); `NONE_DIRECT` (two-point moves).

**Hard blockers** (`_hard_blocker` — set once, never revisited): `BEFORE_PLANNING_START`,
any non-DISPATCHABLE planner status passed through from legs (`BAD_GEOCODE`,
`MASSIVE_UNSUPPORTED`, exclusion reasons), `NO_CAPABLE_VEHICLE`, `MISSING_WINDOW`. Blocked
jobs skip planning but stay in the manifest with their reason.

**K2 (dormant):** `day_flex_min` = earliest allowed EARLIER service day for depot-controlled
FULL_FLEET deliveries (≤ 2 days early, never later); "" = pinned. Only populated under
`--day-flex`, which is off by default and proven bit-identical when off. Population finding:
essentially all multi-day-dwell FF deliveries have `raw_window_start == due date` (TMS stamp
suspicion), so v1 is km-neutral; reopening requires a window-provenance check with ops.

---

## 7. The road model (`route_costs.py`, `simulation/routing.py`)

- `road_km(a, b)`: OSRM road distance when the router is installed (production), else
  haversine × `ROAD_DISTANCE_FACTOR` (1.3). Results are memoised in-process (the +23.8 %
  iterations/s throughput fix) and persisted in a shared pair-matrix JSON cache.
- **Warm-up** (`osrm_setup.warm_osrm_for_run` → `route_costs.warm_and_install_osrm`): collects
  every coordinate the run can touch (vehicle positions, depot anchors, candidate service
  points, two-point origins), batch-builds the OSRM `/table` matrix over them
  (block size `DEFAULT_MAX_TABLE_SIZE` = 100 → chunks of 50), persists new pairs, installs an
  `OSRMRouter` whose misses live-query then fall back to haversine. URL coordinates are
  fixed-point `:.6f` (never scientific notation — the Greenwich-meridian postcode SG8 5QP,
  lon −0.000089, produced `-8.9e-05` and an OSRM HTTP 400 until 2026-07-08).
- Zero-distance guard: OSRM returning 0 m for unroutable pairs is replaced by the haversine
  estimate (storing (0,0) once made breaking a route look like a huge saving — B16 family).
- **Constant-speed model (now the fallback):** `drive_minutes(km)` = km / `AVG_SPEED_KMH` (50) × 60
  for daily routes; multiday tours use `MULTIDAY_AVG_SPEED_KMH` (80, motorway trunking). Used when no
  OSRM router is installed or `USE_OSRM_DURATIONS=False`; this is the model the done E3/E5 ablations
  were measured on.
- **v1.1 OSRM travel-time model** (`road_minutes`, `config.USE_OSRM_DURATIONS`, **default ON as of
  2026-07-09**): the DAILY evaluator times each leg via
  `router.duration_h(a,b) / TRUCK_DURATION_FACTOR (1.24) × FREIGHT_DURATION_FACTOR[type] × 60`,
  per road segment (two-point legs sum two calls) so urban/motorway speed differences survive —
  a constant 50 km/h is simultaneously too fast in town and too slow on motorways, right only on
  average. Applied per-type factors — **tractor 1.0, rigid 1.0, van 0.75, EV 1.0** — come from a
  per-(vehicle type × road class) calibration on telematics moving hops (`speed_calibration.py`,
  Tables A/B below): OSRM car free-flow already matches realized HGV truck time across every road
  class (per-class factors 0.99–1.03), so HGVs need no correction; vans are ~25 % faster. (Supersedes
  a preliminary open-road-biased 0.92; the legacy global 1.24 was conservative.) Setting the flag
  **False** restores the constant model above byte-identically (bit-identical solve fingerprint) — the
  reference the done E3/E5 ablations were measured on — and offline / no-OSRM runs fall back to it
  automatically. `StopTiming.drive_minutes` carries the evaluator's actual leg time so `planned_drive_minutes` and
  utilization always match the model used. Multiday tours stay on `MULTIDAY_AVG_SPEED_KMH`. Jan
  12–17, OSRM (calibrated 1.0 factors) vs constant-speed, matched settings: coverage identical
  (99.9 %), 0 violations, utilization consistent (max daily 85 %, none over cap), depot timing
  emitted (291/291 daily); **−8.5 % daily vehicle-days (270 vs 295)** — the fleet-consolidation
  effect of accurate road-type times (the preliminary 0.92 factor overstated this at −10.5 %).

**Table A — observed HGV/van in-motion speed by road class (km/h), Jan–Feb telematics** (moving
pings, `GPSSpeed` mph→km/h, road class from `Location_Road`):

| vehicle type | minor/urban | B-road | A-road | motorway |
|---|---|---|---|---|
| rigid | 41.0 | 57.3 | 68.9 | 82.1 |
| tractor | 46.8 | 55.6 | 72.6 | 81.3 |
| van | 51.6 | 70.9 | 78.8 | 103.6 |

**Table B — calibration factor** (observed time ÷ OSRM car free-flow) per road class, plus the
applied per-type factor (HGV hops 600–670/class; van sparse 65–120):

| vehicle type | urban | B-road | A-road | motorway | **applied** |
|---|---|---|---|---|---|
| rigid | 0.99 | 0.88 | 1.01 | 1.02 | **1.00** |
| tractor | 1.00 | 0.93 | 1.01 | 1.03 | **1.00** |
| van | 0.63 | – | 0.84 | 0.90 | **0.75** |

Factors ≈ 1.0 for HGVs across all classes ⇒ OSRM already matches realized HGV time (no correction);
vans ~25 % faster.
- `service_minutes(pallets, vehicle_type)` charges a fixed customer-visit dwell:
  15 minutes for vans and rigids, 30 minutes for tractors. These are rounded
  Jan-Feb observed mean visit durations; pallet count explained only about 4-6%
  of the variation. With `SAME_ADDRESS_DWELL_MERGE` enabled, contiguous orders
  at the same coordinates share one dwell rather than paying once per order.
  A direct customer-to-customer movement pays at both endpoints. This applies
  in `evaluate_route` and `evaluate_tour`.
- `statutory_breaks(drive_since_break, drive)`: EU 561/2006 core rule — 45 min owed per
  4.5 h cumulative driving; a long leg can owe several; applied INSIDE route evaluation and
  carried across trips and tour days.

---

## 8. Mode resolution — DIRECT vs XDOCK (endogenous), TRUNK vs HUBDROP (`options_resolver.py`)

- **DIRECT vs XDOCK** (same-day FULL_FLEET): **endogenous** (2026-07-23). Both option groups
  (DIRECT, and the XDOCK pickup+delivery pair) flow into the optimizer; the seed and ALNS
  choose the mode on **real routed cost**, where XDOCK's legs are priced against the routes
  they consolidate onto (the co-load the static ratio used to fake). The static `ρ = 1.6`
  pre-resolver (`resolve_options`) was **deleted**. Mutual exclusion — at most one group per
  freight — is enforced by `option_mutex.OptionMutex` in the seed and ALNS, by the
  OptionSwap ALNS operator, and as a commit-boundary backstop by
  `ledger.drop_superseded_option_legs` at emission. Coverage counts an option set once
  regardless of mode (`alns._served_units`) so the choice is decided on cost, not leg count.
  Fixed tour assignments and the daily seed share the same option claim: a tour may claim
  DIRECT or XDOCK, the daily seed may add legs from that same group, and it cannot select
  the rival. Final cleanup is therefore an integrity backstop, not the cross-component
  mode resolver.
  A window-infeasible same-day XDOCK delivery simply fails to place, so the mode falls to DIRECT. The
  final split is read back from the plan by `option_report.endogenous_option_choices`.
  Different-date FULL_FLEET orders are XDOCK-only; their depot delivery may still enter the
  tour path (§11). Collocated-origin
  same-day orders never enter an option set: §3's collocated exception emits them as a single
  depot-delivery with no option group.
- **Two 2026-07-28 fixes closed a gap between this design and what actually ran.** (1) The
  rolling loop's real-time `insertion_pass` (E6 micro-pass) had NO mutex check against the
  EXISTING solution — only against jobs in the same micro-batch — so a freight's XDOCK
  alternative could be inserted in a LATER epoch even though its DIRECT leg (a different
  job_id) was already committed in an EARLIER one. `drop_superseded_option_legs` silently
  cleaned this up at emission, favoring whichever side happened to deliver — which meant a
  chunk of "DIRECT" in the final plan was really just an artifact of this race, not a cost
  decision (R888GNW/2026-02-02). Fixed by threading an `option_index` (job_id ->
  (option_set, option_group)) into `insertion_pass`, seeding an `OptionMutex` from the
  current solution before considering any new candidate. (2) Separately, `route_seed.py`'s
  `_DEP_RANK` always processes XDOCK's pickup (`PRODUCES_DEPOT_FREIGHT`, rank 0) before the
  same freight's DIRECT leg (`NONE_DIRECT`, rank 1) — so the seed's mutex claim was pure
  insertion ORDER, never a cost comparison; DIRECT was rejected `OPTION_SUPERSEDED` before its
  cost was ever computed. The only remaining chance was ALNS's `option_swap` operator, but it
  draws candidates from `unassigned`, which `_repairable_unassigned_meta` populated only for
  reasons in `_REPAIRABLE_REASONS` — `OPTION_SUPERSEDED` was missing, so the loser never even
  reached `option_swap`. Real effect on a Feb 2-3 backtest (192 option sets): 0/192 chose
  DIRECT with the race fixed but this gap still open; 12/192 once `OPTION_SUPERSEDED` was
  added to `_REPAIRABLE_REASONS`, giving DIRECT a genuine cost-based shot.
- **TRUNK vs HUBDROP** (PL_EXPORT), `resolve_hub_drop` — a genuine pre-routing decision (the
  scheduled depot→hub trunk is not in routed km, so the router cannot price it): keeps the
  scheduled TRUNK unless the customer is strictly closer to the hub than to its depot
  (per-order HUBDROP is inert on this geography — customers sit nearer their depot;
  reported in `hub_drop_choices.md`). STOKE has NO night trunk, so its PL_EXPORT instead
  reaches the hub via a same-day **day-trunk** (§11.5) — which makes its TRUNK choice a real
  onward leg rather than a dead end where freight strands at the depot.

The search can reconsider same-day DIRECT/XDOCK mode against the full current solution.
Depot-loaded tour consolidation remains a bounded post-seed correction (§11).

---

## 9. The compatibility mask (`compatibility.py`) — SDVRP filtering

`vehicle_job_compatibility_frame(candidates, vehicles, cache)` computes the full
vehicle × job matrix. Per pair: same/cross depot, `capacity_ok` (pallets AND kg),
`time_reachable`, distance/drive estimates, and a status: `OK` / `CAPACITY` / `BAD_GEOCODE`
/ `TIME_REACH`. Both the seed (`route_seed._ok_options`) and ALNS (`JobMeta.eligible_vehicles`)
consume ONLY `OK` pairs — i.e. the engine already implements the Pisinger–Ropke
site-dependent-VRP mechanism (a vehicle subset per request, enforced at every insertion).
What is *not* populated is site-access attributes (dock/tail-lift/artic access is not
derivable from a postcode) — a data-provenance gap, not an encoding gap; adding a
`SITE_ACCESS` status is one `np.where` term once a site table exists.

---

## 10. Freight states and the execution ledger

- `state.build_initial_freight_states` seeds each freight unit's opening state
  (`AT_CUSTOMER_ORIGIN`, prestaged `AT_DEPOT`/`AT_HUB`, …) from demand + legs + any
  handover staged overrides.
- `freight_ledger.FreightLedger` is the mutable execution ledger: freight cannot be
  delivered from a depot unless it is physically there (prestaged or produced by an earlier
  committed pickup); violating this raises `FreightUnavailableError` rather than going
  negative. **This is what makes phantom crossdock deliveries impossible by construction**
  (as opposed to `ledger.py`, the stateless post-hoc checker used for validation §14).

---

## 10a. Formal problem statement (what the heuristics are solving)

**Instance scale** (Jan–Feb 2026 backtests): 1,960–2,470 in-universe orders/week →
≈ 2,400–2,600 dispatchable daily jobs + 50–100 tour-classified jobs; 79-vehicle
heterogeneous fleet (≈ 68 telematics-active) across CB22 / BEDFORD / STOKE; plans use
285–380 vehicle-days and 65–95k plan km + ≈ 10k trunk km per week.

**Sets & parameters.** Jobs `j ∈ J` (dispatchable legs, §6) with size `(q_j pal, w_j kg)`,
effective window `[e_j, l_j]`, freight-ready time, pinned service day `day(j)` (K2 widens
this to an earlier-only set for eligible FF deliveries), optional predecessor `pred(j)`,
and an allowed-vehicle set `A_j ⊆ V` (the §9 mask). Vehicles `v ∈ V` with type
`τ(v) ∈ {tractor, rigid, van}`, capacities `(Q_v, W_v)`, home-depot anchor, and
catchment radius `ρ_v`. Days `d` in the window. Cost rate `c_τ` (0.319/0.216/0.150
GBP/km), penalty factor `φ = 2.6`. (The learned trip cap `κ_v` was REMOVED 2026-07-16,
user rule — duty/driving feasibility bounds the day's trips.)

**Decision.** For each (vehicle, day): an ordered partition of assigned jobs into
depot-loop trips (sequence within trip = visit order; count bounded only by the day's
duty/driving/window feasibility); plus the unserved set `U`. Equivalently: a
multi-depot, multi-trip, site-dependent PDPTW with optional service.

**Objective (lexicographic).**
```
maximise   |J \ U|                                  (coverage first — never traded for cost)
then min   Σ_(v,d)  c_τ(v) · [ KM(v,d) + φ · Σ_{j∈(v,d)} max(0, dist(anchor_v, j) − ρ_v) ]
                 +  𝟙[vehicle-day cost] · Σ_(v,d): occupied  h_τ(v) · max(G, duty(v,d))
                 +  Σ_(v,d) Σ_{j∈(v,d): delivery} [ λ · late(j)^p  +  ε · early(j) ]   (soft delivery windows)
```
where `KM(v,d)` is the road distance of the day's trip sequence including depot returns.
The bracketed phantom term is ranking-only (§5); reported distance is physical km. Tours
and trunk are staged before the search (§11), so their km is a constant offset — the
search objective correctly covers only the daily portion. The three cost lines are
**fuel-per-km + driver-day (activation/overtime/late-ramp) + soft-delivery-window
lateness** — the last two are detailed below. All three sum in ONE scalar under the
coverage tier; coverage is never traded for any of them.

**Vehicle-day activation cost (DEFAULT ON since 2026-07-15).** The second line is the
optional per-vehicle-day *driver* activation cost (`config.VEHICLE_DAY_COST_ENABLED`,
`--vehicle-day-cost`, env `FREIGHT_VEHICLE_DAY_COST`). Without it the objective is fuel-per-km
only, so the search is indifferent between reusing an already-working vehicle and opening a
fresh one for a small job — and, with per-vehicle catchments, a nearby fresh vehicle often
*wins* on km. Since **2026-07-16** (`driver_day_cost_ev`, `config.OVERTIME_COST_ENABLED`,
`--no-overtime-cost` = the straight-time ablation) the term charges each **occupied** `(v,d)`:

```
paid_base = h_τ(v) · max(G, W)          W = WORKING hours = Σ duty-chain spans
duty_ot   = h_τ(v) · 0.5 · max(0, W−G)  payroll overtime ×1.5 beyond the paid floor
late      = h_τ(v) · ∫ (0.5 + 0.25·t) dt over working time t hours past 19:00
```

Chains follow C4's split-shift rule (a ≥3h depot gap is unpaid rest/swap). The late RAMP
(×1.5 at 19:00 → ×2.0 at 21:00, `LATE_PREMIUM_*`/`LATE_RAMP_PER_HOUR`) makes late cost
QUADRATIC per vehicle-day — one vehicle's second late hour always costs more than another
active vehicle's first, so evening work SPREADS across drivers with no fairness
bookkeeping (spec 2026-07-16; a genuine km saving still consolidates — the ramp slope is
the fairness-vs-distance exchange rate). A short day pays the flat 9 h floor; an empty
vehicle-day pays 0. So reusing a driver already on shift (staying ≤ 9 h) is free, while a
fresh vehicle pays a whole shift — the search fills each activated vehicle toward its
duty limit before opening the next. Coverage is untouched: the lexicographic serve-first
rule means the term can only re-rank *equal-coverage* solutions, and a fresh vehicle
still opens whenever it is the only feasible placement (this is *why* the old
scalar-penalty version raised unassignment and the current design does not). Because it
is a labour cost, it is not weighted by `c_τ` (fuel); the **£70/day standing cost** in
`vehicle_cost_rates.json` is deliberately **excluded** — it is depreciation, sunk whether
the vehicle runs or parks, so charging it would penalize *using* an owned vehicle.

*Where the numbers come from.* `h_τ`: UK DVSA licence-class wage survey, "avg adjusted for
hours paid" upper bound, adopted 2026-07-27 — **£16.05/h tractor (Class C+E), £14.395/h
rigid (Class C1+C averaged — this model has no light/heavy rigid split), £13.48/h van
(Class B)** (unknown types fall back to rigid). Supersedes the prior £47.59/£40.97 rates
from `profitability_report/vehicle_cost_rates.json`'s `driving_hourly_gbp` (v2.1), a
fully-loaded/overhead-inclusive figure that overweighted driver cost relative to distance
in the routing objective; that file is unchanged (it serves a different,
profitability-reporting purpose). `G = 9 h` = **P25 of per-driver telematics
duty spans** (supatrak Jan+Feb 2026, ~2,100 weekday driver-days / 71–73 drivers; median driver day
~10 h, ~13 % under 8 h — the low end of a normal day; brackets the guarantee to 8–10 h, exact
contracted minimum is a payroll fact), overridable per run via `--guaranteed-shift-hours` /
`FREIGHT_GUARANTEED_SHIFT_HOURS`. The 13 h ceiling is C4's duty cap, not a price. Rates and the
`driver_day_cost()` function live in `vehicle_cost.py`; the term is wired into `alns.route_cost`
and the ALNS delta paths (`changed_costs`, `route_cost_by_key`, `_ranked_inserts_for_job`,
`_best_insert_for_job`) — five sites, one helper.

*Provenance cut-line:* **default ON since 2026-07-15** (`config.VEHICLE_DAY_COST_ENABLED = True`);
pre-cut runs were fuel-only and are not comparable — use `--no-vehicle-day-cost` for a fuel-only
baseline. *Validation — converged week* (2026-01-12→18, convergence gate, 9 h floor): vehicle-days
**229 → 198 (−13.5 %)**, distinct vehicles 62 → 57, **coverage identical** — same totals
(`ON_TIME 1150 / NOT_PLANNED 58 / UNSERVED 15`) AND the same 15 in-window UNSERVED orders/reasons
(zero extra strands; the 58 NOT_PLANNED are the cold-start boundary set), combined km +3.5 %. The
lexicographic serve-first guarantee holds empirically over a full week. (A 2-day/400-iter/8 h probe
gave −8.8 %, the floor.)

**Soft delivery-window lateness cost (DEFAULT ON since 2026-07-18).** The third
objective line prices delivery timing. A `CUSTOMER_DELIVERY` served past its tight
customer deadline is NOT infeasible — the hard `TIME_WINDOW` cutoff was incoherent
with soft coverage (it forced a whole-day SLIP rather than a minutes-late same-day
delivery). Instead each delivery pays

```
late(j)  = max(0, service_start_j − deadline_j)            (minutes past the tight deadline)
early(j) = max(0, window_open_j − service_start_j)         (minutes before the window opens; range windows only)
cost(j)  = λ · late(j)^p  +  ε · early(j)                  λ=TARDINESS_COEF(0.05), p=TARDINESS_POWER(2), ε=EARLINESS_COEF(0.1)
```

`deadline`/`window_open` are the **raw** (tight) customer window, plumbed to the
`RouteJob` (`evaluate_route._delivery_lateness` → `RouteEvaluation.lateness_cost` →
`DayEvaluation.lateness_cost`); the widened `latest_finish` stays the hard
operating/duty bound (past end-of-day = duty-infeasible = slip). The penalty is
CONVEX (`p=2`): a modest slip is cheap, big lateness ramps hard, so the solver treats
lateness as a genuine last resort. The resulting service **hierarchy — on-time <
early < late < slip/unserved** — falls out for free: slip/unserved is the top
lexicographic coverage tier (worst by construction), on-time/early/late are the three
levels within the cost tier. Consequence (intended): a very-late same-day delivery is
still preferred over an on-time next-day one, and an order slips ONLY when serving it
today is duty-infeasible. Scope: DELIVERY legs only (pickups keep hard windows); the
70% of orders with no stated window incur no penalty. Calibration (2026-07-18, 2-day
sweep): λ=0.05 gives 98.9% on-time at ~0 km, and the result is **λ-INSENSITIVE above
~0.05** (delivery timing is loosely constrained in this operation — validated). CLI:
`--hard-time-windows` (ablation = hard-VRPTW cutoff), `--tardiness-coef`,
`--earliness-coef`. Wired into the objective via `alns._day_nonkm_cost` — one helper
that bundles `driver_day_cost_ev + day_ev.lateness_cost`, swapped in at `route_cost`
and all five incremental insert-delta sites.

**Unit asymmetry (state it honestly):** the constructive seed ranks insertions on
`Δkm + φ·penalty` (physical-km flavoured); ALNS ranks on the GBP objective above. The
cost-weighting is what produces the fleet-mix wedge observed in deep runs (cost falls
≈ −24 % while km falls ≈ −15 %: the search shifts work toward cheaper vehicle types).
The vehicle-day activation cost lives **only** in the ALNS objective, never in the seed. The
seed deliberately does the opposite — it adds a `+10000` km penalty to opening a *second loop*
on an already-used vehicle (`route_seed.py`, `best_insertion`) to *spread* first-trips across
idle vehicles and keep duty headroom for later eligibility-constrained jobs, because greedy
construction is myopic and cannot recover from stranding one. The division of labour is
intentional: the seed builds a coverage-safe spread; the fixed-cost-aware ALNS, which has
global search and the serve-first safety net, re-consolidates it onto fewer vehicle-days.

**Constraints and WHERE each is enforced** — the enforcement map matters because there is
no monolithic model; feasibility is distributed across four mechanisms (evaluator, candidate
generator, ledger, pre-resolve):

| # | constraint | enforced by |
|---|---|---|
| C1 | capacity at every point of a trip (deliveries pre-loaded at depot; pickups accumulate; direct moves carried over their inbound segment) | `evaluate_route` load walk |
| C2 | time windows: service start ≤ `l_j`; curbside wait ≤ 90 min at non-first stops; first stop absorbs slack by just-in-time depot departure | `evaluate_route` (`TIME_WINDOW`/`EXCESS_WAIT`) |
| C3 | daily driving ≤ 10 h summed ACROSS trips | `evaluate_day` (`DRIVING_CAP`) |
| C4 | duty ≤ 13 h **per CHAIN** (`MAX_DUTY_H_PER_DAY`, `DUTY_CAP` — a depot gap ≥ `SPLIT_SHIFT_GAP_H` 3h ends a chain: split shift, driver rests/swaps). Per-vehicle shift walls REMOVED 2026-07-16 (user rule): the fleet works one operating day, available from 06:00, NO `shift_end` wall — telematics shift medians were descriptive (half of real days ended later) and refused paid-for afternoon capacity; 19:00 is a soft target (coverage first) PRICED by the overtime + late-ramp cost (see objective). A blank `shift_end` builds no wall; the `SHIFT` gate fires only on explicitly-set synthetic frames | `evaluate_day` chain walk (`DUTY_CAP`); `vehicles.build_vehicle_states` (`FLEET_AVAILABLE_FROM`) |
| C5 | EU-561 core breaks: 45 min per 4.5 h cumulative driving, carried across trips and legs (vans exempt) | `statutory_breaks` inside the walk |
| C6 | 30-min reload dwell between trips | `evaluate_day` |
| C7 | ~~trips per vehicle-day ≤ `κ_v` (telematics-learned)~~ REMOVED 2026-07-16 (user rule): trip count is bounded by C3/C4 feasibility, not a habit count | — |
| C8 | precedence: a `REQUIRES_PRIOR_PICKUP` delivery inserts only when its predecessor leg is placed | ALNS available-leg set; seed ledger gate |
| C9 | depot freight conservation (no delivery of freight not physically at the depot) | `FreightLedger` by construction + post-hoc `plan_ledger_violations` |
| C10 | vehicle–job compatibility (capacity/geocode/time-reach; SDVRP mask) | candidate generation (`OK` pairs only) |
| C11 | service-day pinning (`day(j)`); K2 earlier-only relaxation behind `--day-flex` | candidate generation |
| C12 | same freight's pickup and delivery never share one trip (may share a day across trips) | `same_order_handoff_conflict` |
| C13 | option-group mutual exclusion (DIRECT/XDOCK, TRUNK/HUBDROP) | pre-resolve (§8) — one group ever enters the pool |
| C14 | shuttle carve-out jobs immutable | pinned-set filter inside destroy ops |
| C15 | vehicle availability (handover in-flight holds; trunk-drawn tractors held to 10:00 next day only under `TRUNK_NEXT_DAY_HOLD`, default off) | vehicle start-time overrides |
| C16 | depot-bound legs — UNIVERSAL under `DEPOT_PINNING` (default ON, 2026-07-17): every pickup binds to its `target_depot` (freight must LAND there), every delivery to its `source_depot` (freight RESTS there); DIRECT/HUB_DROP stay unbound; inter-depot movement rides priced trunks only. Without it 130/618 delivery legs teleported cross-depot (unpriced repositioning worst-case 12.4% of window km) and 31/415 pickups landed away from their ledger depot. `--no-depot-pinning` = the teleport ablation; the collocated single-delivery bind stays under its own flag | emission (`legs._pinned`); `evaluate_route` (`DEPOT_BOUND`); tour candidate paths via `_depot_bound_mismatch` |
| C17 | trip departure ≥ max `depart_floor` over ALL member jobs (depot-loaded freight boards at departure; B2 depot-hold, trip-wide since 2026-07-17); a floored job may not re-time a LAUNCHED trip's committed departure | `evaluate_route` route-start hold; `_retimes_committed_departure` at the insertion doors |
| C18 | delivery time windows are SOFT (default, 2026-07-18): a CUSTOMER_DELIVERY past its tight customer deadline is FEASIBLE with a convex tardiness penalty (+ small earliness before the window opens), NOT a hard cutoff — so the solver delivers slightly late rather than slipping a whole day. Hierarchy on-time<early<late<slip (slip/unserved = the lexicographic coverage tier). The hard `latest_finish` stays the widened operating/duty bound; PICKUP windows stay hard. `--hard-time-windows` = the hard-VRPTW ablation | `evaluate_route` `_delivery_lateness` → `lateness_cost`; objective via `_day_nonkm_cost` |

**The feasibility oracle** (`routing_adapter.evaluate_route`, used by everything): walk the
stop sequence computing leg km → drive minutes → owed breaks → arrival; apply the wait rule;
check the window; add the fixed vehicle-type customer-visit dwell (15 minutes for vans and
rigids, 30 minutes for tractors; two-point moves pay it at both endpoints); update the load
timeline per leg kind; add the depot return; check shift.
`evaluate_day` chains trips with reload dwell, carrying the break clock and summing the
driving cap. `detail=False` is the search fast path (skips timestamp formatting);
`try_insert_job(…, "best")` evaluates every insertion position and returns the cheapest
feasible one.

---

## 11. Staged planning — the multiday seed (`tour_plan.run_multiday_seed_plan`)

Ordering is load-bearing (a far tour delivery can depend on a near daily collection):

1. **Tour classification** (`tours.is_tour_only`): a job is tour-only when a same-day
   round-trip from its depot (depot → [origin →] dest → depot) cannot fit
   `min(longest fleet shift, MAX_DRIVING_H_PER_DAY = 10 h)`. Two-point legs classify on the
   full carry (a near destination with a far origin is correctly tour-only — B14). Pickups
   that feed deliveries (`PRODUCES_DEPOT_FREIGHT`) never tour (daily commits first, so
   pickup-before-delivery is guaranteed). Leg times use OSRM per-road-type durations
   (`TOUR_OSRM_DURATIONS`, default ON, spec `2026-07-15-tour-osrm-durations-design.md`) — the
   same model the daily router uses, in both the gate and the executor (`evaluate_tour`) — so
   "can't round-trip in a day" reflects real motorway speed (boundary ~250→~425 road-km
   one-way); `--no-tour-osrm-durations` reverts to the flat 50 km/h gate / 80 km/h executor
   (byte-identical).
2. **Daily pre-pass**: the daily seed runs once WITHOUT tours to measure per-vehicle-day
   busyness, so tour vehicle selection can prefer idle vehicles (minimal displacement).
3. **Tour formation** (`tours.build_tours` + `resolve_cluster`): far jobs pool fleet-wide
   (consolidation mode, default), cluster under a cohesion radius (`TOUR_COHESION_KM` = 200 —
   stops Scotland and Cornwall merging just because pallets and dates fit), then each cluster
   resolves to concrete tours: multi-depot clusters can become ONE tour with front
   `DEPOT_LOAD` stops; far non-depot DIRECT moves consolidate onto shared sweeps under a
   km-guard (`_keep_or_split`). A salvage pass re-pools degenerate single-job sweeps
   (two-point moves excluded — a far-origin DIRECT poisons the re-pool). A DIRECT collected
   AT its anchor depot (depot-loadable, `_origin_at_depot`) is reclassified to a depot-loaded
   delivery BEFORE clustering (`_as_depot_delivery`, `TOUR_DEPOT_DIRECT_AS_DELIVERY` default
   on): the atomic collect→deliver pairing served no purpose (the load is picked up where the
   tour starts) yet made two same-destination directs evaluate as two round trips → infeasible
   → each stranded on its own tour. As deliveries they consolidate normally, so a shipment
   split N ways (e.g. 3 same-day Hull orders on 3 vehicles) consolidates onto fewer;
   `--no-tour-depot-direct-as-delivery` reproduces the split. Two things travel with the
   reclassification so it is coverage-safe end-to-end: the freight is placed `AT_DEPOT` in the
   ledger (it loads at the depot, so the delivery commit's `exists_at_depot` gate does not reject
   it — without this the order silently drops), and the stop EMITS as a `customer_delivery` (the
   tour emission uses the reclassified `leg_kind`), so the plan/board/map read it as a depot
   delivery, not a direct carry. Tours are evaluated
   by `evaluate_tour`: real road km, fixed vehicle-type visit dwell, per-day driving cap
   (10 h) AND elapsed duty cap (13 h), due-date-as-deadline (early is fine, late is
   infeasible; never dwell), hard ceiling `MAX_TOUR_DAYS_HARD` = 4 days. With
   `MULTIDAY_MIDLEG_OVERNIGHT` (default **ON** since 2026-07-22 — designed-on, had shipped
   OFF) a day ends part-way ALONG the leg — the vehicle banks the drive-cap residual and
   sleeps en route rather than parking at the last stop (≤ one overnight per leg, gated on
   `day_elapsed > 0` so it only relocates a boundary the stop-boundary split would already
   create). Verified on 6,000 random far tours: km identical, `days` ≤ OFF (~20% −1 day via
   residual banking), zero coverage loss. Each day's start (depot, then the interpolated
   overnight coord + freight aboard) is emitted as `TourEvaluation.day_starts`; every MID-LEG
   sleep additionally emits a synthetic `TOUR_OVERNIGHT` selected row (job rows gap-numbered
   2*i, overnight at 2*n_prev+1; coords self-carried in the node `OVERNIGHT@lat,lon`;
   timeless) → route_stops `stop_type=tour_overnight` → the map draws the day's polyline to
   the sleep point and resumes the next day from it (excluded from the assigned-jobs KPI).
   Known approximations: the split point interpolates time∝km (approximate under OSRM
   per-road durations), and the coordinate is geometric — real drivers snap to facilities
   (Fort William case: model slept at Bridge of Orchy, reality in Glasgow; overnight-node
   snapping is the identified fix). Tours also run on the 05:00 duty clock and do NOT clamp
   deliveries to the 08:00 customer-window layer (open decision).
4. **Tour vehicle assignment** (`select_tour_vehicle`): prefer an artic; a light tour
   (< `LIGHT_TOUR_PALLETS` = 10) prefers a rigid ONLY when the tour is short
   (`TOUR_TRACTOR_KM` 250, 2026-07-21 role calibration — telematics: tractor median 351
   km-day, rigid 160; the fuel-only per-km preference had inverted the roles); prefer the
   anchor depot and idle vehicles;
   start day = earliest member due date, floored by freight readiness (an XDOCK-fed tour
   departs the morning AFTER its feeding pickup's day). The chosen vehicle's day-span is
   **reserved** (excluded from the daily seed); a tour that can't get a vehicle releases its
   jobs as `NO_FEASIBLE_TOUR` without consuming one.
5. **T1/T2 fixed night trunk** (`trunk.py`): nightly depot↔hub shuttle sized from the
   candidate frame — **EXPORT-ONLY**: trips/night = ceil(PL_EXPORT pallets / `TRUNK_DECK_PALLETS`
   = **52, double-deck** — restored 2026-07-21, run-validated in val4 at exactly reality's
   trip count) per depot ∈ `TRUNK_DEPOTS` (BEDFORD, CB22). Network IMPORT freight arrives at
   the depot via the "invisible hub" resource we neither own nor model (treated as spawning
   at the depot), so it NEVER charges a trunk trip; the tractor still round-trips and returns
   empty (km unchanged — only trip count is export-driven). LE10 hazchem trunk is
   CB22-only (`LE10_FORCED_DEPOT` — telematics: all LE10 night visitors are CB22 tractors).
   km = trips × real road round-trip. `draw_tractors` assigns artics (skipping
   tour-reserved days). By default a drawn tractor has NO next-day hold — telematics shows
   trunk artics run full customer days too (vehicle ≠ driver, driver swaps); the legacy
   10:00 next-morning start (`TRUNK_NEXT_DAY_START`) applies only under `TRUNK_NEXT_DAY_HOLD`.
   STOKE has no night trunk, so its PL_EXPORT reaches the hub via a same-day **day-trunk**
   (`TRUNK_DAY_DEPOTS` = (STOKE,), export-only, sized ceil(export / deck), flagged `day_trunk`
   and exempt from the next-day hold) — day-run to B37 rather than stranded at the depot.
   Trunk km is a separate fixed-service accounting line, never inside the optimizer's km.
6. **Daily seed** (`route_seed.run_route_seed_plan`): greedy constructive insertion over the
   remaining jobs. Order = (`service_date`, dependency rank [pickups first], `latest_finish`,
   job_id). For each job, over its `OK` vehicles: try the best position of every EXISTING
   trip, then OPENING A NEW TRIP under the vehicle's cap — a new loop on an already-used
   vehicle carries a +10,000 discouragement score, so the seed prefers idle vehicle-days
   over compressing marginal km (coverage/resource headroom first). Ranking metric =
   `Δday_km + out-of-area phantom km` (NOTE: physical-km flavoured — only ALNS ranks on the
   GBP objective, §10a). Freight readiness gates deliveries via the shared FreightLedger
   before any insertion is attempted; same-freight handoff conflicts are rejected per trip.
   **K1 shuttle carve-out** (`shuttle.py`, applied inside the seed): an address-day whose
   same-direction volume reaches an artic load (`SHUTTLE_MIN_PALLETS` = 26,
   `SHUTTLE_MIN_FILL` = 0.9) is packed into dedicated shuttle trips on anchor-depot vehicles;
   those job ids are PINNED for the ALNS (never destroyed).
7. **Commit tours** against the same ledger — feeding collections are now in place, so tour
   deliveries gate correctly.

Result: `seed.daily` (routes as (vehicle, day) → trips + shuttle pins), `seed.tours` (+
records), `seed.rejected`, `seed.reserved`, and `seed.trunk`.

---

## 12. The improvement layer — coverage-aware ALNS (`alns.py`)

**State.** `solution[(vehicle_id, day)] = [trip, trip, …]`, each trip an ordered
depot-loop of `RouteJob`s — **multi-trip aware**: capacity resets per trip, a 30-min
reload dwell separates trips, shift/driving/breaks evaluate across the whole day
(`routing_adapter.evaluate_day`). The state also carries the repairable unassigned pool
(rejected jobs with reasons in `{SHIFT, DRIVING_CAP, TIME_WINDOW, NO_FEASIBLE_ROUTE,
EXCESS_WAIT}`), so coverage can INCREASE inside the search.

**Objective** (lexicographic): maximise served jobs, then minimise a single GBP scalar
per `(vehicle, day)` = **fuel-per-km + driver-day + soft-delivery-window lateness**:
`c_τ · (road_km + Σ out-of-area phantom km) + driver_day_cost_ev + Σ_{delivery} [λ·late² + ε·early]`.
Fuel rates are measured tank-to-tank from Jan-2026 Jigsaw fuel cards: tractor 0.319,
rigid 0.216, van 0.150 GBP/km (`FREIGHT_FUEL_UNIFORM` collapses all to the rigid rate).
The driver-day and lateness terms are detailed in §10a (Objective) — the lateness term
(2026-07-18) makes a late delivery FEASIBLE-with-penalty rather than a hard reject, so
the solver prefers minutes-late-today over a next-day slip. The whole non-km part is
bundled in `alns._day_nonkm_cost`, applied at `route_cost` and every incremental
insert-delta site. Phantom km (§5) is ranking-only; reported plan distance stays
physical km. Tours and trunk are FIXED during the search, so their cost is invariant
and correctly excluded from the search objective.

**Iteration.** Up to `--iterations`, stopped by time budget or `--no-improve` (4000)
iterations without a new best:

1. **Destroy**: adaptive roulette picks an operator from `{random, worst, shaw}`;
   removal size k ~ uniform **[2, 5]** (`FP_ALNS_REMOVAL_MIN/MAX` defaults — E3 2026-07-08:
   widening the max to 8 improves cost ≈ 3.3 % at 2,500 iterations, i.e. the default
   under-destroys). `worst` ranks by in-trip detour (d(prev,j)+d(j,next)−d(prev,next),
   depot-anchored) with Ropke–Pisinger roulette bias p=3; `shaw` grows a same-day
   spatially-related cluster from a random anchor (bias p=5; same-day only because the day
   axis is otherwise fixed). Shuttle-pinned jobs are excluded inside the operators (not
   post-filtered, which would silently shrink k and distort rewards). Destroys that empty a
   trip drop it.
2. **Coverage specs** (every `--repair-every` = 20th iteration): alongside the base
   destroy/repair spec, build extra specs that try to INSERT up to 8 unassigned jobs —
   removing same-day assigned jobs or ruining whole candidate trips (up to 4 targeted specs)
   to make room; an ejection path can displace one incumbent (the evictee re-queues first).
   All specs are evaluated; the best (served, −cost) wins.
3. **Repair**: greedy best-position insertion of removed jobs (priority-ordered), or
   regret-2 ordering under `--regret-repair` (off by default; measured cost-neutral at
   2–3× the wall — E3). For every job the insertion enumerates each eligible vehicle-day
   (K2 variants included when on) × each existing trip × best position, PLUS opening a
   new trip (no trip-count cap since 2026-07-16 — duty/driving/window feasibility is the
   only limit). Same-order pickup/delivery handoff conflicts within one trip are rejected;
   `REQUIRES_PRIOR_PICKUP` deliveries only insert once their predecessor leg is present.
4. **Feasibility pricing** (`changed_costs`): every touched day is re-evaluated whole; if ANY
   changed day is infeasible the entire spec is refused (B16 root cause: OSRM road distances
   violate the triangle inequality, so REMOVING a stop can bust a day; an infeasible day
   pricing as 0 km once made breaking routes look like savings).
5. **Acceptance**: improving moves (more served, or cost down) always accept. Worse moves:
   simulated annealing `exp(−Δ/T)` with T₀ = `--sa-temp` (0.005) × seed cost, geometric
   cooling 0.999 per iteration (E3: statistically inert — the search is effectively greedy
   with best-tracking); `FP_ALNS_ACCEPT=rrt` swaps in record-to-record travel (E3:
   significantly worse). Best solution is tracked separately and returned, so acceptance
   noise can never degrade the result.
6. **Adaptive weights** (`_AdaptiveOps`): rewards 33 (new best) / 9 (improving) / 13
   (accepted-worse), blended into weights every 50 iterations with reaction 0.1
   (`FP_ALNS_UNIFORM_WEIGHTS=1` freezes selection uniform for ablation).
7. **Invariants** (`FP_ALNS_CONSERVE=1`): job-conservation and pinned-set assertions after
   every accepted move (the B16 silent-job-loss diagnostic); emission re-raises on any
   dropped job.

**Empirical characterization (Ch.5 ablation campaign, paired CRN seeds ×3 windows @N=2,500;
status 2026-07-08, batch ~68% complete — final table in `experiments/`):**

| component | verdict vs reference |
|---|---|
| worst-removal (`drop_worst`) | **load-bearing**: removing it costs +3.9 % (p<0.0001, r=+0.98) |
| removal band | default 2..5 **under-destroys**: widening max to 8 gives −3.3 % (p<0.0001, r=−0.96) |
| Shaw removal (`drop_shaw`) | inert at 2,500 (+0.6 %, p=0.37) but +8.8 % at 20k — **phase-dependent**: diversification pays only at depth |
| SA acceptance (`sa_off`) | inert (−0.06 %, p=0.58, n=30 — pre-registered and confirmed) |
| RRT acceptance (`rrt`) | worse (+0.71 %, p=0.022) |
| regret-2 repair (`regret_on`) | cost-inert at 2–3× wall — dominated at equal time |
| repair cadence (`repair_1`) | bit-identical to reference in 10/11 pairs (cadence already 1 at the base path) — doubles as a determinism check |
| type-aware fuel rates (`fuel_uniform`) | objective-units incomparable on raw cost; judged on km/fleet-mix (pending) |

Net reading: at operational budgets the **destroy neighbourhood (size + worst-removal) is
the only lever that matters**; acceptance and insertion sophistication are inert or harmful,
and operator value is budget-phase-dependent. `uniform_weights`, `repair_50`,
`destroy_random` and the deep spot-check were still in flight at this snapshot.

**The loop, compactly:**

```
solution ← staged seed (§11);  best ← solution;  U ← repairable rejected pool
T ← sa_temp (0.005) × cost(seed)
for it = 1..N  while wall < budget  and  it − last_improve < 4000:
    op ← adaptive_roulette({random, worst, shaw})
    k  ~ Uniform[2, 5]                                   # FP_ALNS_REMOVAL_MIN..MAX
    R  ← op.remove(k, solution ∖ pinned)                 # emptied trips collapse
    specs ← {(R, insert=∅)}
    if U ≠ ∅ and it ≡ 0 (mod repair_every=20):
        specs += coverage specs: try up to 8 jobs from U, each with room made by
                 same-day removals / ruined trips / single-incumbent ejection
    for each spec:
        greedy best-position repair over eligible (vehicle, day[, K2 variants], trip,
        position) INCLUDING opening a new trip under κ_v; re-evaluate every changed
        day whole; refuse the spec if any day goes infeasible
    champion ← best spec by (served, −cost)
    accept if improving; else with prob exp(−Δ/T)         # or RRT band if FP_ALNS_ACCEPT=rrt
    on accept: commit; reward op (33 new-best / 9 better / 13 accepted-worse);
               best ← max(best, solution) by (served, −cost)
    every 50 it: blend operator weights (reaction 0.1);   T ← 0.999 · T
return best
```

**Post-loop**: `apply_zero_cost_merges` (`merge_sweep.py`, K1 component 2) collapses
same-day same-postcode split visits when feasible and net-km ≥ 0 — operational realism
(one truck per dock), replay-proven km-neutral, never applied when net-negative.
`--restarts` runs independent ALNS passes (seed+i) and keeps the best by
(served, −km, inserted, −accepted).

**Instrumentation** (all default-off, bit-identical when unset, recorded in
`run_manifest.env_toggles`): `FP_ALNS_TRACE=<csv>` writes an anytime curve
(elapsed_s, iteration, accepted, cost, best_cost, served; buffered, cadence 200);
`FP_ALNS_REMOVAL_MIN/MAX`, `FP_ALNS_DESTROY_OPS`, `FP_ALNS_UNIFORM_WEIGHTS`,
`FP_ALNS_ACCEPT`/`FP_ALNS_RRT_DEVIATION`, `FREIGHT_FUEL_UNIFORM`, `FP_ALNS_CONSERVE`.

---

## 13. Emission and accounting (`reports.write_reports` + `run_alns` write stage)

Everything the run knows lands in the run folder — since 2026-07-14 the table/report
paths below read as `csv/<name>` / `reports/<name>` with json/html/log at the run root
(the historical `plan/`/`reports/` prefixes are kept in this table because every
pre-restructure run still uses them; `find_artifact` resolves either):

| file | grain / content |
|---|---|
| `plan/selected_plan_alns.csv` | the selected job set (daily + tour records), route/trip keyed. |
| `plan/plan_manifest_new.csv` | **the reconciliation spine**: one row per movement for EVERY in-universe order — ROUTED (vehicle/route/trip/date/km) or ACCOUNTING (`AT_DEPOT`, `WITH_NETWORK`, `NO_RESOURCES`, `CANCELLED`, …) or UNASSIGNED/BLOCKED with reason. Kinds: customer_delivery/pickup, direct_customer_move, depot_load, depot_return, inbound/outbound_trunk, accounting_only. |
| `plan/route_stops.csv` | stop-by-stop execution detail per route/trip (postcodes, lat/lon, planned arrive/depart, breaks, leg km, load after stop; always-on `due_date`/`days_early` audit columns). |
| `plan/vehicle_routes.csv`, `vehicle_day_utilization.csv`, `trip_capacity_utilization.csv` | per-route and per-trip rollups (km, drive minutes, pallet utilisation, peaks). |
| `plan/unassigned_jobs.csv` | genuine in-window misses with their failed constraint or dependency reason; excludes selected jobs, losing DIRECT/XDOCK alternatives and next-window deferrals. |
| `plan/depot_inventory_timeline.csv` | ledger-derived depot stock over time. |
| `plan/trunk_schedule.csv` | per-night trunk sizing (export pallets, trips, km, hub — export-only; the `import_pallets` column was removed) + the per-vehicle ASSIGNMENT (2026-07-14, gap 5): `vehicles` = tractors picked by the draw (semicolon-joined, draw order), `feasible` = the eligible pool it chose from. The board renders each named trip on that tractor's own lane. |
| `plan/handover.json` | week N end-state for week N+1 (§4). |
| `plan/validation_metrics.json` | seed→ALNS km/cost, moves, planned vehicle-days, trunk line. |
| `plan/kpi_summary.md` | in-universe / assigned / rate + per-type cost lines (+ K2 ledger when on). |
| `plan_full.csv` at the RUN ROOT (+ `reports/plan_full_dictionary.md`) | `plan_full.py` — since 2026-07-14 AUTO-EMITTED at the end of every run (static AND dynamic; `emit_plan_full`, loud-warn non-fatal): one denormalised row per manifest movement (ids, WT name, endpoints, vehicle, times, sizes, km) — the whole-plan single-file view; row count always equals the manifest. `--month`/`--window` CLI still rebuilds offline. |
| `reports/` | `alns_progress.log` (staged run log + convergence), `alns_summary.md`, `cross_depot_report.md`, `option_choices.md`, `hub_drop_choices.md`, `runsheets.html` (per-driver), `trip_app_validate.html` (viz §15), `temporal_violations.csv` (only if non-empty). |

**Metric-scope discipline:** every distance/coverage concept exists at several scopes
(orders vs legs; daily-portion vs +tours vs +trunk; raw vs matched incumbent comparisons).
Quote one headline per concept and name the scope — the authoritative scope table lives in
`experiments/METRICS.md` (coverage = whole-plan ORDERS; external km comparisons =
`combined_km`; the search-sensitive metric = daily-portion `alns_cost`; incumbent
comparisons = (vehicle,day)-MATCHED).

Caches persist at the end of the run: postcode cache and (if grown) the OSRM pair matrix.
NOTE: these are shared mutable JSON files — concurrent runs can collide (a mid-write read
crashed one E3 run on 2026-07-08); an atomic-write/lock is a known TODO.

---

## 13a. Dynamic rolling dispatch — the E6 rolling-horizon wrapper (`run_rolling.py`)

Everything above (§1–§13) is the **static** planner: it sees the whole window at once and
optimises with full knowledge. That is the right tool for a backtest baseline, but a real
dispatcher does not know tomorrow's orders. `run_rolling.py` (spec 4.7a) replays each day as
a sequence of **decision epochs**, re-running the static seed + ALNS on only what was knowable
at that moment and **committing** work as it is dispatched. `--beta 0` makes the warm-start
objective pure cost — the regression gate; `β>0` adds `cost + β·disturbance` to damp churn.
CLI: `python -m freight_planner.run_rolling --start … --end … --iterations … --epochs 00:00,12:00
--delta-min 60 --delta-r1-min 90 [--micro-every-min N] [--converge-pct P] [--handover-in …]`.

**Epoch cadence** (`micro_times`, `run_dynamic_loop`): per day, two **anchor** epochs — 00:00
(**midnight seed** — first optimization of the day) and 12:00 (warm re-opt) — with
**micro-inserts every `config.MICRO_EVERY_MIN` minutes** (default 30 since 2026-07-14; a micro
pass costs ~2 s wall so the cadence is a service-level choice, not a compute constraint —
pre-change replays pass `--micro-every-min 60`) running only within the **06:00–18:00** window
(06:00 floor, 18:00 **day close**). Anchors re-solve the uncommitted problem from scratch; micros
only *insert* newly visible orders into the standing plan (collection-side, one branch per
order), never re-open it.

**Visibility gate (no future knowledge).** At epoch *t*, `build_window_inputs` is handed
`visible_order_ids` = orders whose `timestamp_created ≤ t` (`DELIVER_FLOWS` — imports/local
deliveries — are revealed at 18:00 the day before their delivery day, since the freight is
already in the network). The anchor loop also drops **expired** orders — those whose target
service day is already past — keyed by `target_service_day(qargo, flow_of)`: the **delivery**
date for `DELIVER_FLOWS`, the origin (collection) date for collections. Using the collection
date for *everything* was the 2026-07-11 import-drop bug — a network-collected import has an
origin date days before its delivery day, so every import was wrongly expired before the seed.

**Commit frontier (two levels).** The horizon is `now + delta` (`delta_min` 60; the first
re-opt uses `delta_r1_min` 90). (1) `expire_commit` moves any daily trip whose depot departure
precedes the horizon into `inflight` — **immutable**; the anchor re-solve owns only the
remainder. (2) `advance_watermarks` pins the served/rolling prefix of each in-flight route so
the solver never touches a stop already underway; the remaining suffix opens only under
**departure-based flooring** (2026-07-14 evening, WT255677/FJ72XFF): `floor_ok` requires the
trip's deviation point — the last committed stop's departure, the first moment the driver's
remaining plan changes — to sit at/after `now + Δ`, as well as every suffix arrival (flooring
arrivals alone was leaky: a far order's arrival always clears the window while its drive
starts inside it). Whole **tours** whose first departure precedes
the horizon are frozen into `merged_tour_records`. A weekly `handover.json` chains end-state
(in-flight vehicles, delivered orders, staged freight) into the next window.

**Non-anticipation invariant (the core correctness property, user rule 2026-07-11).** A vehicle
may not **arrive** at a collection before its order was booked — if the order does not exist yet,
routing to it is future knowledge (data leakage). Enforced in depth, all **dynamic-only** (the
static planner never sets the `collect_creation_floor` flag, so its baselines are bit-identical):

- **Creation floor** (`run_alns._floor_collection_earliest_to_creation`): every collection leg's
  `earliest_start` is floored to its order's creation time — a FIXED per-order value that survives
  every anchor re-plan (unlike a decision-time dispatch floor, which is lost when a day re-plans
  from the candidate frame). A separate `creation_floor` column carries the raw booking time.
- **Daily** (`routing_adapter.evaluate_day`): such a collection is marked `no_early_arrival`
  (`route_seed.make_route_job`), so an early drive-up is **infeasible** (`EARLY_ARRIVAL`) rather
  than an honest curb wait — the optimiser must sequence it later (arrive ≥ booking) or elsewhere.
- **Tour** (`tour_plan.run_multiday_seed_plan`): `ready_by_job` is floored to the creation day, so
  `_assign_one` starts the sweep no earlier than the booking day. `evaluate_tour` is day-granular
  and ignores `earliest_start`, so without this a multi-day tour could serve a DIRECT carry the day
  BEFORE its order existed (the 2026-07-11 tour back-dating).
- **Audit** (`audit_non_anticipation`, run every dynamic finish): flags any collection whose
  `planned_arrive` precedes booking (1 s tolerance for whole-second timing rounding);
  `FP_STRICT_CAUSALITY` raises. `stop_provenance` / `emit_stop_provenance` trace every collection
  stop back to the epoch, kind and floor that first placed it (`reports/stop_provenance.csv`,
  `non_anticipation_detail.csv`).
- **Route-level audit** (`audit_route_backdating`, 2026-07-14, closes the RULES.md gap-1 blind
  spot): no emitted stop may be planned in the PAST of the decision that created it — tour rows'
  `service_date` >= their creating seed's day (`tour_created_at`, stamped at each seed) and daily
  stops' `planned_arrive` >= the epoch that first placed their job (the `placement` trace). This
  is the gate that catches the Fix-8 class (i5000's Jan-16 seed emitting "Jan-15 13:06" tour work
  passed the order-level audit because every ORDER existed). Same `FP_STRICT_CAUSALITY` raise.

**Outputs.** The loop calls the §13 emitter **once** at the end with a merged `SolveResult`, so a
dynamic run yields the same run-folder artifact contract as a static one (`route_stops.csv`,
`runsheets.html`, KPI, handover, auto `plan_full.csv`) plus the provenance/churn/micro-pass
reports. It also writes
`csv/plan_snapshots.csv` — the **live plan captured at EVERY epoch** (per vehicle-trip-stop:
job, order, arrive/depart, committed flag, and each trip's exact depot departure + return via
`trip_timings`/`plan_snapshot_rows`) — i.e. exactly what a driver would have been sent at 00:00,
at noon, at each micro. That is the record a real dispatch product needs (the intermediate plans,
not just the final one) and the substrate the evolving-plan board (§15) is built on — which is
**auto-emitted at `<run>/timeline.html`** on every dynamic finish (`_emit_timeline` →
`viz_timeline_build.write_dashboard`; built BEFORE the strict audit so a violating run keeps its
board, loud-warn non-fatal). The convergence gate is per-run adjustable here too
(`--converge-pct/-window/-min-iters` flow into every anchor solve). Runs live
under `--out-dir`, separate from the static `runs/`. Finished ad-hoc/smoke runs are parked
in `runs_archive/<name>/` (2026-07-13 reorganisation) — only the canonical monthly `runs/`
and the provenance-pinned `experiments/` stay top-level.

---

## 14. Verification layers (every run, printed and must-be-zero)

1. `plan_schema.plan_ledger_violations` — stateless re-check of the selected set against
   freight availability (phantom deliveries; B16 emission also raises on silent drops).
2. `plan_validation.temporal_violations` — window/precedence timestamp audit of the plan.
3. KPI reconciliation — `every order accounted in manifest: True` (universe closure).
4. `FP_ALNS_CONSERVE` in-loop assertions (opt-in, §12).
5. Coverage is lexicographically protected: no km move can drop a served job.
6. `audit_non_anticipation` (dynamic runs, §13a) — no collection is arrived at before its order
   was booked; `FP_STRICT_CAUSALITY` promotes the count to a raise for CI/gate use.
7. `audit_route_backdating` (dynamic runs, §13a, 2026-07-14) — no stop is planned in the past of
   its deciding epoch (tour day-level vs the creating seed; daily arrive vs first placement).
8. `option_conflicts` (dynamic runs, added 2026-07-28) — option sets left with BOTH a DIRECT and
   an XDOCK leg selected because `ledger.drop_superseded_option_legs` refused to drop an
   already watermark-committed leg (`RULES.md` C3 exception). Previously runlog-only; now folded
   into `feasibility_audit.csv`/`09_feasibility_audit.md` by `feasibility_audit.
   augment_with_dynamic_audits` alongside `non_anticipativity`/`route_backdating`, so all four
   correctness-audit families read 0 in one structured artifact, not just the log.

---

## 15. Downstream: validation viz and month rollups

- `viz_app.py --plan-dir <plan> --validate` renders `trip_app_validate.html`: trip map +
  per-trip day-correct telematics km + the fleet scorecard — WINDOW-scoped (nominal
  run_manifest days on both sides; trunk km on the planned side of Δ; tour tail-day spill
  excluded and disclosed; time budget + seed shown). The Δ is fleet-level context; the
  citable number is the (vehicle,day)-MATCHED gap from `month_summary` (odometer includes
  non-order movement — the "reverse hole").
- `month_summary.py` (via `run_month`): per-window km table (plan/trunk/combined vs 6-day
  odometer; naive gap labelled as artifact; honest matched gap) + handover-continuity table
  (every hop consumer==producer). Jan 2026: matched −0.8 % (parity); Feb 2026: +2.2 % with a
  −10.1 % → +17.1 % week gradient (the 120 s budget starving the heaviest week — the deep-
  budget re-baseline evidence: 1800 s ≈ −15 % plan km on the same seed).
- **Map view** (`viz_geometry.py` + `viz_timeline_maplogic.cjs` + the template, 2026-07-14):
  clicking a truck in the board opens a full-screen Leaflet map of its route on road-snapped
  OSRM geometry (baked per unique leg at build, disk-cached, straight-line fallback;
  `--no-geometry` opts out and the board is byte-unchanged). One master clock re-forms the
  route across epochs and slides a simulated truck (`truckPos` interpolation) along the
  committed route; a committed-vs-internal overlay toggle and a faint-fleet toggle make
  abnormal assignments obvious. Route math is Node-unit-tested (`maplogic.test.cjs`); needs
  internet for tiles (the gantt does not).
- **Evolving-plan board** (`viz_timeline_build.py`; auto-emitted at `<run>/timeline.html` on
  every dynamic finish since 2026-07-14, `write_dashboard`; reads current + legacy layouts):
  consumes `plan_snapshots.csv` (§13a) into a compact per-day / per-vehicle / per-epoch structure
  and renders the plan **as it stood at any clock time** — the 00:00 midnight seed is continuous, the noon
  re-opt visibly reshuffles the uncommitted tail, committed stops only ever delay — with day paging
  and 90-min-frontier commit colouring. Published as a self-contained claude.ai Artifact; the
  substrate for a future moving-vehicle map (real depot times + stop coords are recorded).
- `viz_map.py`: static trip/compare maps. Viz regeneration is ALWAYS read-only — it reads outputs
  only and can never change a plan.

---

## 16. The decision boundary — what the solver can and cannot change

**ALNS searches over (every iteration):** job→vehicle assignment across both depots
(cross-depot priced by geography, §5 penalty + real km); position within a trip;
**intra-day trip structure** (open new depot loops up to the learned `_trip_cap`, collapse
emptied trips — trips are search variables, NOT seed-frozen); the served set itself
(insert/evict under lexicographic coverage); service day for eligible FF deliveries under
`--day-flex` (default off).

**Staged-deterministic (decided before the search, never revisited inside it):**
DIRECT-vs-XDOCK and TRUNK-vs-HUBDROP mode choice (§8; option-swap = designed future work;
two targeted post-seed correctors exist); multiday tour formation, membership and km (§11);
the nightly trunk schedule and its tractor draw (§11.5); the K1 shuttle carve-out (pinned);
depot staging (`resolve_staging_depot`: OVERFLOW deliveries stage at the CB22 gateway,
collections at the nearest of CB22/BEDFORD/STOKE).

**Hard mask, not a decision:** the vehicle×job compatibility matrix (§9) — capacity,
geocode, time-reachability today; site-access attributes are the missing data, not a
missing mechanism. Plus hard blockers (§6) and the freight ledger (§10).

---

## 17. Determinism and experiment provenance

One `random.Random(seed)` drives the search; fixed iterations + seed reproduce cost
trajectories bit-identically (fingerprinted during the Ch.5 campaign; wall-clock varies
±20 %, so experiments are iteration-primary). All experiment toggles are env-gated,
default-off, and recorded in `run_manifest.json.env_toggles`; with none set, every toggle
path is proven bit-identical to pre-instrumentation behaviour (including rng-draw order —
`_accept_worse` reproduces the original consumption pattern exactly). The Ch.5 campaign
design-of-record lives in `experiments/` (gitignored): README (design), METRICS (metric
scopes), PROVENANCE (frozen commit 70d5253a + snapshots + mid-campaign edit log).

---

## 18. Configuration reference (values as of 2026-07-22)

**`freight_planner/config.py`** (planner-owned): tour — `TOUR_COHESION_KM` 200,
`LIGHT_TOUR_PALLETS` 10, `TOUR_ORIGIN_AT_DEPOT_RADIUS_KM` 8, `TOUR_DAY_ELAPSED_CAP_MIN`
780 (13 h), `TOUR_DAY_START_HOUR` **5** (operating window 05:00–19:00, user rule
2026-07-16: start earlier → end earlier; 10 h driving / 13 h duty caps unchanged),
`MULTIDAY_MIDLEG_OVERNIGHT` **True** (default ON 2026-07-22: tour-days end mid-leg with a
banked-residual overnight; km-identical, days ≤ OFF; emits TOUR_OVERNIGHT rows — §11.3);
`TOUR_TRACTOR_KM` 250 (light-tour rigid preference is distance-bounded, 2026-07-21);
`DAILY_RANGE_SOFT_KM` {rigid 387.0, van 166.3} — each type's OWN real P90 daily
range, Jan+Feb 2026 telematics (soft daily-range prior in the objective; excess
km beyond it are priced at the type's own `road_cost_per_km` again, not a
separate flat rate — 2026-07-27; tractors unbounded); breaks/waits —
`DRIVE_BREAK_AFTER_MIN` 270, `DRIVE_BREAK_MIN` 45, `MAX_STOP_WAIT_MIN` 90; catchment —
P95 / min 20 samples / floor 30 km / penalty factor 2.6; shuttle — enabled, 26 pal, 0.9
fill; merge sweep — enabled; **same-address dwell merge `SAME_ADDRESS_DWELL_MERGE` True**
(default ON 2026-07-22);
tour attach `TOUR_ATTACH_ENABLED` **True** (default ON 2026-07-16); trunk — enabled, **52 pal double-deck** (2026-07-21), night depots
(BEDFORD, CB22), **day-trunk depots `TRUNK_DAY_DEPOTS` (STOKE)**, next-day hold
`TRUNK_NEXT_DAY_HOLD` **False** (legacy 10:00 `TRUNK_NEXT_DAY_START` only when True);
**v1.1 OSRM time model — `USE_OSRM_DURATIONS` True (default as of 2026-07-09; False = constant-speed),
`FREIGHT_DURATION_FACTOR` {tractor 1.0, rigid 1.0, van 0.75, EV 1.0} (per-(type × road-class)
calibrated), `OSRM_SCREEN_SPEED_KMH` 100** (artifact: `freight_planner/data/calibration/speed_factors.json`).

**`shared/config.py`** (the vendored world model — was `cambridge/config.py` until the
2026-07-13 separation): `ROAD_DISTANCE_FACTOR` 1.3, `AVG_SPEED_KMH` 50,
`MAX_DRIVING_H_PER_DAY` 10, `MAX_TOUR_DAYS_HARD` 4, `MULTIDAY_AVG_SPEED_KMH` 80, service
minutes 10 + 6/pal (tractor) or + 3/pal (rigid), depot anchors/fleet maps, EU-core break
constants mirrored above.

**`vehicle_cost.py`** (measured, Jan-2026 fuel cards): tractor 0.319 / rigid 0.216 / van
0.150 GBP/km; unknown types → rigid.

**Objective & constraint knobs added 2026-07-15→18** (all default ON; each has a CLI
ablation): `VEHICLE_DAY_COST_ENABLED` True (`--no-vehicle-day-cost`),
`GUARANTEED_SHIFT_HOURS` 9 (`--guaranteed-shift-hours`), `OVERTIME_COST_ENABLED` True
(`--no-overtime-cost`); `DEPOT_PINNING` True (`--no-depot-pinning`, §C16); soft delivery
windows — `SOFT_DELIVERY_WINDOWS` True (`--hard-time-windows` = hard-VRPTW cutoff),
`TARDINESS_COEF` 0.05 (`--tardiness-coef`, GBP per late-minute²), `TARDINESS_POWER` 2.0
(convex), `EARLINESS_COEF` 0.1 (`--earliness-coef`); `FLEET_DAY_START_HOUR` 6 +
`CUSTOMER_DAY_START` 08:00 (the two-layer operating window, 2026-07-20);
`READINESS_LAG_MIN` 0.0 (`--readiness-lag-min`, EXPERIMENT knob, off by default —
retired as a results experiment 2026-07-18 but the flag stays). λ=0.05 is the
calibrated tardiness weight (delivery timing is λ-insensitive above ~0.05).

**ALNS defaults** (CLI §0 + env §12): removal band 2..5, ops {random, worst, shaw},
weights adaptive (33/9/13, blend 50, reaction 0.1), SA T₀ 0.005 × seed cost, cooling 0.999,
repair-every 20, max coverage candidates 8, trip cap learned (2..12).

---

## 19. Known limitations and dormant switches (state them, don't hide them)

- Same-day FULL_FLEET mode can be reconsidered during search, while different-date
  FULL_FLEET orders are deliberately restricted to XDOCK because ordinary vehicles cannot
  retain freight between service dates.
- Delivery timing is loosely constrained in this operation (validated against telematics
  2026-07-18): only ~20% of orders carry a hard range window; the rest are point/deadline
  targets the real op delivers ~50 min early, or have no stated window. So the soft
  delivery-window penalty achieves ~99% on-time at negligible km and is λ-INSENSITIVE
  above ~0.05 — delivery-timing robustness is not a strong experimental axis here, and we
  do not oversell tight-window optimisation. Pickup/collection windows stay HARD (soft
  windows are delivery-only; collection lateness has different downstream semantics — a
  possible extension).
- Multiday tours are seeded, not searched — formation is a deterministic feasibility rule;
  claim automated *classification*, not optimisation, of tour-vs-day work.
- Driver-hours model is the EU-561 core (4.5 h/45 min, daily driving cap, duty span), not
  the full regime (weekly/fortnightly limits, split breaks, rest classes). Vans are exempt
  in evaluation.
- The removal band default (2..5) measurably under-destroys at operational budgets (E3:
  2..8 ≈ −3.3 % at N=2,500) — re-tuning pending the deep-budget spot-check.
- K2 day-flex is shipped-dormant (window-stamp provenance blocks the population); per-order
  hub-drop is inert on this geography (customers sit nearer their depot than the hub) — the
  Stoke day-trunk (§11.5) is how trunkless-depot exports reach the hub; regret-2 repair is
  off (dominated at equal time). A known simplification: the hub trunk is modelled night-only,
  while the census shows ~5 night + ~4 day round-trips/day — the model runs all ~9 overnight
  at the real 52-pallet double deck (state the roster cost: night deployment 1.6–1.8× the
  incumbent's).
- Cost model = fuel/km + vehicle-day activation (hourly × max(9h, duty)) + overtime ×1.5 +
  convex tardiness/earliness on soft delivery windows + soft daily-range overage
  (rigid/van). Still absent: tolls, refuelling stops, maintenance windows, fatigue/slack
  pricing (plans compress far tours to the legal caps — reality spends ~+1 driver-day of
  human slack on them; vehicle-days are a lower bound).
- Site-access compatibility data absent (§9); OSRM cache/postcode cache lack write locks.
- v1.1 OSRM travel-time model is **default ON** (2026-07-09). The constant-speed model remains available
  (`USE_OSRM_DURATIONS=False`) and is what the done E3/E5 ablations were measured on: **E3 is kept as-is**
  (a paired ablation of config *changes* in a fixed environment — model-independent; note in the write-up
  "ablations on the reference configuration"), while **E5 is re-baselined and E1/reverse-hole/E2 run on
  OSRM**. Open caveats: the van factor rests on a sparse hop sample (65–120/class); the model applies
  OSRM's *native* per-class durations × a per-type factor (validated by the per-class table — HGV ~1.0
  across all classes), so a full per-(type × road-class) *application* via OSRM-profile tuning is future
  work; tours keep the 80 km/h motorway model; no time-of-day multiplier (the `duration_h(depart_time)`
  hook exists, unused); the return-to-depot leg's drive in secondary reports is estimated at the route's
  effective pace (exact per-stop legs and the utilization % are OSRM-consistent).
- Duty-hours validation axis is now LIVE (v1.1): `route_stops` emits depot start/return timings and
  `viz_app --validate` shows planned-vs-actual duty hours for daily routes (tour depot spans blank).
