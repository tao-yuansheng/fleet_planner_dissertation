# Freight Planner — Design Log (original architecture rationale + decision record)

> **This file was `README.md` until 2026-07-14**, when the docs were restructured:
> **`README.md`** is now the setup / layout / data-quality guide, **`README_STATIC.md`**
> explains the static ALNS planner, **`README_DYNAMIC.md`** explains the dynamic
> rolling-horizon dispatcher, and **`PIPELINE.md`** remains the code-verified reference of
> every stage, config value and limitation. THIS file is the original design rationale and
> the Q1–Q11 decision log — kept frozen for the "why"; its stage descriptions predate
> shipped subsystems (tours, shuttle, catchments, trunk, dynamic dispatch, per-epoch plan
> snapshots) and its run commands may age, so trust the live docs above for the "how".
> Since **2026-07-13** the package is fully SELF-CONTAINED (no imports outside
> `freight_planner/`; the load-bearing legacy modules live in `shared/`, offline tools in
> `tools/`, finished ad-hoc runs in `runs_archive/`) — layout map in README.md.

## Purpose

This folder is the **freight planner** — a clean-sheet replacement for the legacy Cambridge
dispatch pipeline, now built and operating as a **dynamic rolling-horizon dispatcher** (see
`PIPELINE.md` for the current system). The goal was never to copy Qargo's historical plan
exactly, nor to keep adding special-case rules around `LOCAL`, `TOUR`, and `TRUNK`.

It is a physical freight planner:

- every piece of work is represented as a movement of freight;
- every vehicle is represented as a time-and-location resource;
- every depot and hub is represented as freight state;
- every route is generated from physically available freight;
- historical telematics is used to verify which leg our fleet actually performed
  when the Qargo order record is ambiguous.

The current codebase already contains useful pieces: postcode lookup, OSRM road
times, verified leg inference, vehicle capacities, trunk timing rules, and daily
VRPTW/ALNS routing. The new architecture keeps those pieces but changes the
shape of the pipeline.

## Why Change The Current Structure

The current pipeline makes too many hard decisions before optimization starts:

- an order is classified as `LOCAL`, `TOUR`, or `TRUNK`;
- a depot is assigned early;
- tour commitments are created before the daily dispatcher sees the whole
  resource picture;
- some pickup and crossdock legs are recovered after routing instead of being in
  the dispatch pool from the beginning;
- the manifest has to repair or explain missing work after the fact.

This makes the dispatcher look less capable than the fleet actually is. An idle
vehicle at one depot cannot naturally help another depot unless a special pass
allows it. A full-fleet crossdock pickup can vanish from the routing pool and
only appear later as an unassigned accounting row. A Palletline export can be
treated as `TRUNK` even though our fleet still has to collect the freight from
the customer first.

The new planner should not ask "which bucket does this order belong to?" as the
main question. It should ask:

- what freight movements are physically required?
- what movement options can satisfy them?
- what vehicles, depots, hubs, and time windows make those options feasible?
- which combination gives the best valid plan over the planning horizon?

## High-Level Pipeline

```text
Raw data
  -> Demand model
  -> Verified responsibility / leg model
  -> Movement options
  -> Freight and vehicle state
  -> Horizon optimizer
  -> Executable day plans
  -> Manifest, map, and KPI accounting
```

The optimizer should operate on movement legs and resource state, not raw Qargo
rows and historical class labels.

## Stage 1: Raw Inputs

Inputs:

- Qargo orders;
- vehicle master data from Supatrak;
- telematics traces;
- postcode/geocode cache;
- OSRM road-time cache;
- depot, hub, trunk, and shift rules;
- verified leg output where available.

The raw Qargo order is treated as commercial demand, not as the final movement
plan. A Qargo order can imply one physical leg, two physical legs, trunk
movement, depot staging, or manual exclusion depending on its verified
responsibility.

## Stage 2: Demand Model

The demand model normalizes Qargo rows into stable commercial records.

Each `DemandRecord` should contain:

```python
DemandRecord(
    order_id: str,
    order_name: str,
    source_system: str,
    origin_pc: str | None,
    destination_pc: str | None,
    collect_window: tuple[datetime, datetime] | None,
    deliver_window: tuple[datetime, datetime] | None,
    pallets: float,
    weight_kg: float,
    network: Literal["PALLETLINE", "HAZCHEM", "FULL_FLEET", "LOCAL", "UNKNOWN"],
    cancelled: bool,
    specialist_required: bool,
    historical_resources: list[str],
)
```

This stage should not decide the route. It only describes what the customer or
network requires.

Examples:

- A Palletline import has a hub-to-depot inbound dependency and a final delivery
  requirement.
- A Palletline export has a customer pickup requirement and an outbound hub
  dependency.
- A full-fleet order has origin and destination demand; it may be direct or
  crossdocked.
- A partial fleet order may only have one leg that our fleet historically
  performed.

## Stage 3: Verified Responsibility

Telematics verification remains part of the architecture.

Reason:

- Qargo alone often tells us the commercial order, but not which physical leg
  our fleet was responsible for.
- For historical validation, we must know whether our fleet collected, delivered,
  did both, or only performed a partial leg.
- Operators in a live environment would normally know the intended responsibility
  before dispatching. In this backtest environment, verified telematics is the
  best proxy for that operational knowledge.

So `verified_legs.py` should be treated as a data preparation component that
produces a planning input, not as a late validation-only script.

The verified responsibility output should classify each demand record into one
of these responsibility shapes:

```text
FULL_END_TO_END
  Our fleet is responsible for both pickup and delivery.

PICKUP_ONLY
  Our fleet collects from the customer, then freight leaves our control
  through a hub, subcontractor, or later manual handling.

DELIVERY_ONLY
  Freight is assumed to be available at a depot/hub handoff, and our fleet
  performs the final delivery.

NETWORK_IMPORT
  Freight arrives from Palletline/Hazchem hub, then our fleet delivers.

NETWORK_EXPORT
  Our fleet collects, then freight is trunked to Palletline/Hazchem hub.

AMBIGUOUS_PARTIAL
  In-universe historically, but the available data does not prove which leg we
  did. Keep in manifest/accounting, exclude from automated dispatch.

OUT_OF_SCOPE
  Cancelled, no fleet resource, crane/specialist, or otherwise not our normal
  dispatch work.
```

The live planner should not depend on historical hindsight fields such as the
actual historical resource assignment. In forward mode, this verified
responsibility would come from operator input, customer/network contract, or a
pre-dispatch classification process. In backtest mode, telematics verification
fills that role.

## Stage 4: Movement Legs

The planner should expand demand into physical movement legs.

These are examples of canonical leg generation:

```text
Palletline import
  B37 hub -> depot trunk
  depot -> customer delivery

Hazchem import
  LE10 hub -> depot trunk
  depot -> customer delivery

Palletline export
  customer -> depot pickup
  depot -> B37 hub trunk

Hazchem export
  customer -> depot pickup
  depot -> LE10 hub trunk

Full fleet direct
  customer origin -> customer destination

Full fleet crossdock
  customer origin -> depot pickup
  depot -> customer destination delivery

Partial delivery
  inferred depot/handoff -> customer destination

Partial collection
  customer origin -> selected depot/handoff

Ambiguous partial
  no automated movement leg; manifest row remains for manual handling.
```

Important: `TRUNK` is not a replacement for customer pickup. For an export,
the customer pickup is a dispatchable leg. The trunk is the next movement after
that pickup succeeds.

Each movement leg should have:

```python
MovementLeg(
    leg_id: str,
    order_id: str,
    leg_kind: Literal[
        "CUSTOMER_PICKUP",
        "CUSTOMER_DELIVERY",
        "DIRECT_CUSTOMER_MOVE",
        "OUTBOUND_TRUNK",
        "INBOUND_TRUNK",
        "DEPOT_TRANSFER",
    ],
    origin_node: str,
    destination_node: str,
    service_node: str,
    service_type: Literal["pickup", "delivery", "trunk"],
    earliest_start: datetime,
    latest_finish: datetime,
    freight_ready_time: datetime | None,
    pallets: float,
    weight_kg: float,
    allowed_vehicle_types: set[str],
    hard_constraints: dict,
    soft_preferences: dict,
)
```

The route optimizer should route customer-facing service nodes. The freight
ledger should update the freight location after each leg is completed.

## Stage 5: Freight State Ledger

The planner needs a freight state ledger so impossible movements cannot occur.

Freight states:

```text
NOT_READY
AT_CUSTOMER_ORIGIN
ON_VEHICLE
AT_DEPOT
AT_HUB
DELIVERED
MANUAL_HANDLING
OUT_OF_SCOPE
```

Rules:

- a delivery leg cannot be loaded unless the freight is at the vehicle's start
  depot, hub, or vehicle;
- a crossdock delivery cannot happen before the pickup leg succeeds;
- outbound trunk freight only exists after customer pickup reaches the depot;
- inbound import freight only exists after the hub trunk arrives;
- uncollected freight must not appear as delivered in the manifest.

This ledger is the mechanism that eliminates phantom crossdock deliveries.

## Stage 6: Vehicle And Depot State

Vehicles should be resources, not fixed depot buckets.

Each `VehicleState` should contain:

```python
VehicleState(
    vehicle_id: str,
    home_depot: str,
    current_node: str,
    available_from: datetime,
    remaining_duty_minutes: int,
    remaining_drive_minutes: int,
    capacity_kg: float,
    capacity_pallets: float,
    vehicle_type: Literal["rigid", "tractor", "van"],
    can_sleep_out: bool,
    can_trunk: bool,
)
```

Each `DepotState` should contain:

```python
DepotState(
    depot_id: str,
    node: str,
    freight_inventory: list[str],
    vehicle_ids: list[str],
    inbound_trunk_arrivals: list[str],
    outbound_trunk_cutoffs: list[str],
)
```

Depot ownership should usually be a cost preference, not an absolute wall.

For example:

- using the nearest home depot is cheap;
- using a neighbouring depot is allowed but costs more;
- sending a vehicle from depot A to cover depot B is allowed if duty time and
  positioning cost make sense;
- some freight remains hard-pinned to a depot if that is where it physically
  exists.

## Stage 7: Candidate Generation

The planner should generate feasible candidate actions before optimization.

Candidate examples:

```text
Serve customer pickup with vehicle V on day D.
Serve customer delivery with vehicle V after freight is ready.
Run direct full-fleet order origin -> destination.
Crossdock full-fleet order through depot X.
Attach far delivery to an existing multiday route.
Move idle vehicle from depot A to serve depot B work.
Hold flexible freight for tomorrow.
Run outbound trunk from depot to hub.
```

Candidate generation should respect hard impossibilities early:

- no geocode;
- impossible time window;
- load exceeds every single vehicle and split handling is not enabled;
- specialist/crane excluded from normal dispatch;
- freight not physically available.

But it should avoid premature business-mode decisions such as "this must be
TOUR" or "this must stay with Bedford" unless there is a real physical reason.

## Stage 8: Horizon Optimizer

The optimizer should plan over a horizon, not one isolated day.

Recommended initial horizon:

```text
planning horizon: 5 to 7 days
commit horizon: today / next operating day
replan cadence: daily or whenever major new freight arrives
```

The optimizer should decide:

- vehicle-to-leg assignment;
- route sequencing;
- depot staging choice;
- direct vs crossdock for eligible full-fleet freight;
- same-day vs later-day movement for flexible freight;
- whether far work should become multiday;
- whether idle vehicles should support another depot;
- which misses are real physical misses.

The first implementation can still use the current ALNS/VRPTW solver as the
route engine, but it should be called from a planner that owns the horizon-level
state.

Target objective, in priority order:

```text
1. no illegal freight movement;
2. maximize served in-universe required work;
3. avoid missing hard deadlines;
4. minimize late days for flexible work;
5. minimize vehicle-days and excessive overtime;
6. minimize road km and deadhead;
7. prefer home depot / familiar operating patterns;
8. prefer plans that are explainable to operators.
```

The first two are hard priorities. The rest can be weighted and tuned.

## Stage 9: Executable Day Plan

The horizon optimizer should output executable daily plans:

```python
VehiclePlan(
    vehicle_id: str,
    planned_day: date,
    start_node: str,
    end_node: str,
    route_stops: list[RouteStop],
    carried_freight: list[str],
    duty_start: datetime,
    duty_end: datetime,
    planned_km: float,
)
```

The day plan is not a separate optimizer that reinterprets the work. It is a
projection of the selected horizon plan into operational instructions.

## Stage 10: Manifest And KPI Accounting

The manifest should report the planned result from the same movement-leg ledger.

It should not infer missing physical work after the fact.

Manifest rows should be leg-level:

```text
order_id
order_name
leg_id
leg_kind
responsibility_shape
flow/network
service_date
planned_day
origin_node
destination_node
service_postcode
assigned_vehicle
plan_status
unassigned_reason
freight_state_before
freight_state_after
```

Assignment-rate accounting should separate:

- out of scope;
- ambiguous manual handling;
- accepted massive/multi-vehicle work not yet supported;
- true model miss;
- served;
- served early;
- served late;
- phantom prevented by freight-state gate.

## Relationship To Existing Code

**Status 2026-07-13 — the separation is DONE.** `freight_planner/` imports nothing outside
itself. The reusable components below were resolved as follows (byte-identical gates —
static solve, rolling 6-day, verify_legs regen — in
`docs/superpowers/specs/2026-07-13-freight-planner-separation-design.md`):

- vendored VERBATIM into `freight_planner/shared/`: `config` (calibrated constants, depot
  anchors, vehicle map/profiles), `scope` (order normalization/classification/windows),
  `plan_types`, `verified_legs`, `postcode_resolver` (postcode cache + postcodes.io),
  `routing` (OSRM road matrix/cache), `fleet_replay_data` (telematics day-loading);
- moved into `freight_planner/tools/`: `verify_legs`, `export_replay`,
  `build_vehicle_master` (run as `python -m freight_planner.tools.<name>`);
- NOT taken (superseded by this planner's own machinery): `cambridge/dispatcher.py`,
  `cambridge/trunk_planner.py`, `simulation/vrptw_alns.py`, `cambridge/movement_legs.py` —
  the legacy packages were ARCHIVED to `_archive/2026-07-13_separated_legacy/` (with their
  test suites; live artifacts verified_legs.csv + mot_results.csv extracted to
  `freight_planner/data/` first).

The original reuse plan (kept for the design record):

- `cambridge/verified_legs.py`
- `cambridge/movement_legs.py`, after it becomes authoritative
- `cambridge/scope.py`, for raw order normalization and known Qargo fields
- postcode cache and postcodes.io lookup
- OSRM road matrix/cache
- vehicle enrichment and capacity analysis
- `simulation/vrptw_alns.py`
- `cambridge/dispatcher.py`, as a route evaluator/route builder where possible
- trunk timing logic from `cambridge/trunk_planner.py`
- map and manifest rendering, after moving to movement-leg IDs

Components to demote or replace:

- `OrderClass.LOCAL/TRUNK/TOUR` as pipeline gates;
- post-dispatch missing-pickup recovery;
- manifest-side phantom detection as the primary safeguard;
- one-way Phase 1 tour commitment before the daily dispatcher sees the full
  resource picture;
- depot assignment as an absolute pre-routing boundary.

## Current Pipeline Features That Must Be Preserved

This section is a guardrail. The new planner is allowed to simplify the pipeline
shape, but it must not silently drop operational rules that the Cambridge
pipeline already learned.

### Order Universe And Exclusions

The planner must preserve explicit inclusion/accounting categories:

- cancelled Qargo orders are reported but excluded from dispatch;
- `NO_RESOURCES` orders are reported but excluded from fleet assignment;
- crane hire and specialist movements are reported separately;
- ambiguous partial-fleet orders remain in the manifest but are not auto-routed;
- bad geocode rows are data gaps, not capacity failures;
- massive orders above the supported single-vehicle limit are accepted misses
  until split-load planning is implemented.

The accounting model should distinguish:

```text
OUT_OF_SCOPE
DATA_GAP
AMBIGUOUS_MANUAL
MASSIVE_UNSUPPORTED
MODEL_MISS
SERVED
```

This prevents assignment rate from being diluted by work the planner was not
supposed to carry.

### Verified Leg Responsibility

The current pipeline uses `verified_legs.py` and telematics to correct the
responsibility shape for full-fleet and partial-fleet work. The new planner must
carry this over.

Specific behaviours to preserve:

- Palletline/Hazchem classification happens before partial-fleet inference;
- verified full-fleet direct work stays full-fleet;
- verified single-leg work becomes `LOCAL_COLLECT` or `LOCAL_DELIVER`;
- ambiguous partial-fleet work is kept visible in reporting but excluded from
  automated dispatch;
- backtest validation must not rely on historical resource assignment as a
  forward-planning input unless it is explicitly marked as historical truth.

### Network And Hub Rules

The planner must preserve the distinction between customer work and network
work:

- Palletline import: B37 hub to depot trunk, then depot to customer;
- Palletline export: customer pickup, then depot to B37 hub trunk;
- Hazchem import/export follows the same pattern via LE10;
- customer pickup is always dispatchable work even when the onward movement is
  trunk;
- B37 and LE10 must remain separate hub channels;
- trunk departure and next-morning hub departure times must be represented;
- trunk capacity must be driven by collected/export freight and next-day inbound
  demand.

Important: the old `TRUNK` label mixed two concepts. In the new planner, trunk
is a movement leg, not a reason to hide the customer pickup from dispatch.

### Geography And Geocoding

The planner must preserve postcode precision work:

- use postcodes.io-derived postcode-unit coordinates as the preferred source;
- keep FC/hub aliases and resolve them before routing;
- keep OSRM road routing / road matrix support where available;
- keep a clear fallback policy when OSRM is unavailable;
- keep depot anchors for CB22, Bedford, St Ives, Stoke, B37, and LE10;
- keep territory maps as preferences and reporting dimensions, even if they are
  no longer hard routing walls.

Territory assignment should remain useful for:

- default depot staging;
- operator reporting;
- home-depot preference cost;
- franchise/in-scope checks for network imports;
- identifying `OVERFLOW` work.

### Time Windows And Freight Readiness

The current pipeline has several window semantics that must not be flattened:

- hard delivery slots;
- soft deadline/date-only deliveries;
- pickup windows;
- PL_EXPORT pickup cutoffs before trunk departure;
- full-fleet collection timestamp vs delivery timestamp;
- effective dispatch windows that may differ from raw Qargo windows;
- freight-ready times for depot-held and crossdocked freight;
- stale historical timestamps that should not become impossible forward windows.

The new `MovementLeg` model should carry both:

```text
raw_window
effective_planning_window
window_hardness
freight_ready_time
```

This is important because a planner can relax or reinterpret a soft planning
artifact, but a route executor still needs to respect genuinely hard service
requirements.

### Vehicle Master, Capacity, And Service Time

The planner must preserve current vehicle profiling:

- vehicle roster from Supatrak;
- depot/circuit-derived home depot assignment;
- excluded vehicle list, even if currently empty;
- recently released / Stoke satellite handling;
- observed payload capacity where available;
- fallback asset-type capacity where observation is missing;
- pallet capacity and weight capacity as separate constraints;
- capacity provenance in reporting;
- fixed customer-visit dwell by vehicle type, with contiguous same-address
  orders treated as one visit.

Do not treat `master_max_tonnes` as payload capacity unless the master data is
explicitly converted from gross vehicle weight to payload. Current dispatch
capacity is derived payload/profile capacity.

### Driver Hours, Shifts, And Multi-Trip Day Work

The planner must preserve operational time constraints:

- per-vehicle shift start/end;
- rigid and tractor shift profiles;
- maximum driving time per day;
- maximum on-duty time;
- depot dwell between trips;
- ability for a vehicle to run multiple same-day trips when duty remains;
- hub-returning vehicle availability after trunk return;
- legal overnight/multiday constraints for tractors.

The new state model should track:

```text
available_from
remaining_drive_minutes
remaining_duty_minutes
current_node
requires_depot_return
can_sleep_out
```

### Vehicle Location And Repositioning

The planner must preserve vehicle location state:

- home depot;
- hub location after trunk departure;
- remote overnight position;
- deadhead return when remote vehicles receive no work;
- idle-hour repositioning toward future work;
- cross-depot support when feasible.

The new architecture should make these normal vehicle-state transitions rather
than special post-routing corrections.

### Depot And Freight Inventory

The planner must preserve inventory state:

- previously collected full-fleet crossdock freight;
- import freight arriving from hub;
- export freight staged for outbound trunk;
- depot-specific inventory, not just global CB22 inventory;
- freight handoff date/time;
- no delivery before freight exists at the departure depot.

Every freight state transition should be auditable:

```text
customer -> vehicle -> depot -> vehicle -> customer
hub -> vehicle -> depot -> vehicle -> customer
customer -> vehicle -> depot -> hub
```

### Existing Dispatch Capabilities

The current dispatcher has useful behaviours that should be preserved as
planner capabilities, not necessarily as separate passes:

- daily VRPTW/ALNS route optimization;
- cross-pass repair;
- long-haul tractor repair for far same-day work;
- idle/remote tractor sweep;
- idle home-depot tractor rescue for depot-loaded freight;
- depot-load withholding from remote vehicles;
- trunk planning after local/export collection routes;
- load-on-board and capacity-utilization tracking.

These should be expressed through candidate actions and resource constraints in
the new planner.

### Manifest And Validation

The manifest must remain a first-class output:

- every raw Qargo order in the reporting window appears in accounting;
- full-fleet crossdock can produce separate collect and deliver rows;
- assigned rows include vehicle, sequence, trip/job ID, load, capacity, window,
  arrival, and leg distance;
- unassigned rows have a specific reason;
- assignment rate separates out-of-scope, data gaps, model misses, and massive
  unsupported work;
- phantom deliveries are impossible by construction, but reporting should still
  flag any ledger inconsistency as a hard validation error.

Historical validation should compare against verified responsibility, not just
raw Qargo order dates.

## Design Questions — Resolved

The design questions that gated the build are all closed. Their authoritative answers are the
**Decision Log** below (implemented, and where the implementation later evolved, annotated
inline). This heading is kept only as a signpost to that log.

## Resolved Design Decisions (2026-06-23 Decision Log)

These close the open questions above and the spec's `Open Design Questions`. They
are the authoritative answers; later milestones implement against them. Items
marked **[rework]** change components that already exist.

### Q1 — Service-time model
**Superseded 2026-07-29.** The earlier decision reused the old pipeline's
load-based `service_minutes_for_load(pallets)`. The live model now charges a
fixed dwell per distinct customer visit: 15 minutes for vans and rigids and
30 minutes for tractors. Pallet count is not used because it explained only
about 4–6% of observed dwell variation. Contiguous order rows at the same
coordinates share one dwell, while a direct customer-to-customer movement
pays at both endpoints. Implemented through
`CUSTOMER_SERVICE_MIN_BY_TYPE` in `freight_planner/shared/config.py`.

### Q2 — Commitment horizon *(SHIPPED)*
Resolved and built as the **dynamic rolling-horizon dispatcher** (`run_rolling.py`, PIPELINE
§13a): each day replays as decision epochs (00:00 midnight seed, noon re-opt, hourly micros within 06:00–18:00) seeing only
orders knowable at each moment; the 90-min commit freeze locks the near-term plan while the
uncommitted horizon re-optimises (the once-planned second 60-min "decision lag" level proved
vestigial and was retired 2026-07-13). The plan at every epoch is
persisted (`plan_snapshots.csv`) — exactly what a live dispatcher would send to drivers.

### Q3 — Trunk movement (+ opportunistic hub-drop)
A scheduled per-depot trunk remains the default rule (fixed/reserved; B37 Palletline, LE10
Hazchem). **Addition:** an export-collecting vehicle near a hub may drop directly at the hub
(a deliver-to-hub leg competing on cost with return-to-depot + trunk), freeing trunk capacity.

> **Implemented + updated 2026-07-12.** Night trunk sized single-deck **26 pal** (the earlier
> 52-pal double-deck rested on a night-only assumption telematics overturned — the hub flow is
> day+night). Trunk-drawn tractors are NOT held the next morning by default (vehicle ≠ driver,
> driver swaps; `TRUNK_NEXT_DAY_HOLD` off). STOKE, which has no night trunk, day-trunks its
> exports to B37 the same day (`TRUNK_DAY_DEPOTS`). Per-order hub-drop machinery is live but
> inert on this geography (customers sit nearer their depot). See PIPELINE §11.5.

### Q4 — Sleep-out / multiday eligibility **[rework: vehicles.py]**
Prefer tractors (artics) for multiday tours — more capacity per long trip. But
rigids are eligible as an exception (they did sleep out historically), and are
*preferred* when a tour carries few pallets, to avoid wasting a tractor's
capacity. So multiday eligibility is a cost/preference, not a hard tractor-only
wall. Relax `can_sleep_out` accordingly when M8 lands.

### Q5 — Depot-to-depot transfers
Vehicle repositioning only (cost signal), no freight depot-transfer legs until a
concrete need appears.

### Q6 — Crossdock / dock capacity
Unlimited for now, but **instrument it**: track per-depot intake / peak
concurrent inventory so we can see if a depot saturates. Surfaces in the M9
`depot_inventory_timeline.csv` and KPI.

### Q7 — Split-load / massive orders
Split oversized non-hazardous orders by pallet count. Per-child weight =
average pallet weight (`total_weight / total_pallets`) × child pallets, rather
than exact item weights. Never split hazardous/specialist orders. Expose
`split_group_id`, `split_index`, `split_count`; the manifest still shows one
commercial order with multiple vehicle legs.

### Q8 — Forward responsibility source / broken leg data **[rework: demand.py]**
The raw dataset does not reliably say which leg was ours. Decision: build a
persisted **enriched orders dataset** for the Jan–Feb orders that adds the
verified leg as an explicit column, so the physical movement of each order is
known up front. This enriched column is the backtest responsibility source —
replacing runtime `verified_leg()` inference with a precomputed field. It is
still leg *verification* only (allowed); it must not be used to choose future
vehicle/route assignments (the non-negotiable invariant stands).

> **Implemented 2026-06-23.** `verify_legs.py` gained an additive `--qargo` arg
> and was run over Jan+Feb, regenerating `verified_legs.csv` to 19,458 rows
> (Jan 91.2%, Feb 90.2%; Jan-only backup at `verified_legs_jan_only.bak.csv`).
> `freight_planner/enrich.py` joins it onto the orders; `build_enriched.py` writes
> `freight_planner/data/enriched_orders_2026-01_2026-02.parquet` (90.7% leg
> coverage). `demand.py` backtest mode reads the column (falls back to the CSV).
> Feb backtest now classifies 92% via `telematics_verified` (was ~0%).

### Q9 — Manual / ambiguous rows in KPIs
With Q8 applied, genuine ambiguity should be largely eliminated. For any residual,
keep the recommended model: a separate `AMBIGUOUS_MANUAL` accounting bucket,
excluded from the assignment-rate denominator (as the spine already does).

### Q10 — Weekly-plan runtime budget
~30 minutes per 7 days is acceptable (matches the old pipeline) **provided the
time is spent on real optimization**. No redundant passes or duplicated work in
the pipeline; spend the budget on ALNS/search quality.

### Q11 — Direct-vs-crossdock authority **[rework: legs.py + verified leg]**
The planner chooses direct vs crossdock for eligible full-fleet freight. The
verified leg / responsibility model must therefore stop sub-typing full-fleet
into "direct" vs "crossdock" (currently inferred from collect/deliver dates in
`legs.py`); it should classify only `FULL_FLEET`, and M7 option generation emits
both the direct and the via-depot option as a mutually exclusive group.

## Data Spine (`build_phase0`)

`build_phase0.py` builds the canonical tables that make the freight universe explicit before
any optimizer sees it (demand records, verified responsibility, movement legs, freight states,
compatibility, ledger issues) — the shared input stage the full planner also runs. Two
responsibility modes: `forward_structural` (default, operating mode — order structure + network
rules; empty resource columns = unassigned work, not out-of-scope) and `backtest_verified`
(historical validation — verified_legs + resource evidence). Date-basis modes (`planning_window`
default, `service_date`, `demand_touch`, `manifest_compat`) scope which legs enter the window.
Run from `BackEnd/logistics`:

```powershell
python -B -m freight_planner.build_phase0 --start 2026-01-05 --end 2026-01-10
```

Outputs share the `runs/<YYYY-MM>/<window>/{inputs,plan,reports}` layout (mode/basis appended to
the folder name only when non-default). See PIPELINE.md §1–§10 for the current, code-verified
description of every table this produces.

## Running The Full Planner

`build_phase0` only builds the spine. The planners that route vehicles are **`run_rolling`**
(the dynamic rolling-horizon dispatcher — the operative mode), `run_alns` (one window,
full-knowledge — the backtest bound), and `run_month` (a handover-chained sequence of static
windows). All share the flattened `runs/<YYYY-MM>/<window>/` layout above.

Dynamic dispatch (the realistic online plan; also writes `reports/plan_snapshots.csv`):

```powershell
python -m freight_planner.run_rolling --start 2026-01-12 --end 2026-01-17 `
  --iterations 2000 --epochs 00:00,12:00 --micro-every-min 60 --delta-min 60 --delta-r1-min 90 `
  --out-dir freight_planner/runs_dyn
```

Static single window (full-knowledge backtest, cold start):

```powershell
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m freight_planner.run_alns `
  --start 2026-01-12 --end 2026-01-17 --time-budget 120
```

Chain a whole month, each week opening from the prior week's end-state
(`plan/handover.json` is emitted by each run and consumed via `--handover-in`;
`run_month` wires this automatically, cold-starting the first window):

```powershell
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m freight_planner.run_month `
  --windows 2026-01-01:2026-01-03 2026-01-05:2026-01-10 2026-01-12:2026-01-17 `
            2026-01-19:2026-01-24 2026-01-26:2026-01-31 --time-budget 120
```

`run_month` emits per-week viz and then a rollup at
`runs/<YYYY-MM>/month_summary.md` (km table + odometer + honest matched gap, and a
handover-continuity check). Rebuild the rollup without re-running via
`run_month --windows <any window in the month> --summary-only`.

Key knobs (`run_alns`): `--time-budget` seconds per ALNS restart (120 is the
production default; the ALNS is time-limited, not converged — a higher budget
keeps improving); `--handover-in` a prior week's `handover.json`; `--log-every`
iterations between km-vs-cost checkpoints in `alns_progress.log` (default 200).
Distance uses OSRM by default (`--router osrm`); it must be running, or the run
falls back to haversine.

## Build History

Milestones M1–M9 (canonical tables → leg-pool parity → freight-state gate → cross-depot use →
direct-vs-crossdock → horizon multiday → trunk/hub-drop → sleep-out tours → inventory/KPI) are
all shipped — plus the K1 shuttle carve-out, learned catchments, week-to-week handover, the OSRM
travel-time model, and the E6 dynamic rolling dispatcher with per-epoch plan snapshots and the
evolving-plan board. The chronological record is `QUEST_LOG.md`; the current state is `PIPELINE.md`.

## Design Principle

Rules are still needed. They encode the business and physical world.

The change is where the rules live:

- bad: rules hard-classify work into separate pipelines before optimization;
- good: rules define constraints, costs, and candidate options inside one
  planner.

The new planner should be operationally realistic, but it should not copy
historical operations blindly. Historical data, especially verified telematics,
is used to identify responsibility and validate the planner. The planner itself
should then choose the best physically valid plan from the available resources.


## 2026-07-16 — Over-ceiling orders split in EVERY flow; hazchem stays whole

**Decision (user):** an order above the single-vehicle ceiling (26 pal / 28 t from the
vehicle master) is served as a MULTI-VEHICLE SPLIT in every flow branch — imports,
exports and local flows gained the same `_split_parts` loop FULL_FLEET always had. The
alternative (exclude all over-ceiling orders from the universe as "double-deck work we
don't model") was considered and explicitly rejected in favour of serving them; the
known cost is that a split books ~2 vehicle-days where the real operation runs one
double-deck artic, biasing plan-vs-actual AGAINST the planner on these orders (~29/month,
2026-01). Revisit when trailer data enables real double-deck capacity profiles.

**Hazchem exception (kept, original rule "never split hazardous/specialist orders"):**
splitting a dangerous-goods consignment is not free the way splitting pallets is — each
carrying vehicle needs an ADR-certified driver, the DG declaration must be re-issued per
vehicle, and segregation rules constrain the second vehicle's co-load; the model tracks
none of these, so it cannot promise a hazchem split is executable. If one ever appears it
surfaces as a visible MASSIVE/blocked row for a human decision. Empirically dead today:
0 of 1,393 hazardous orders in 2026-01 exceed the ceiling.

**Split accounting invariant (Scenario C, fixed same day):** records carry the PART
freight id in `order_id` (`uuid#S1`) so the FreightLedger gates per part. Any
parent-level accounting (ledger reconcile, tour crediting, handover) MUST normalize via
`order_id.split("#", 1)[0]`. The finalize reconcile did not, and demoted seven fully
planned-and-launched 30-34-pal split orders to NOT_IN_PLAN — the trips were on the CB9
shuttles all along; the "miss" was a string-identity illusion. Known residual: cross-week
`handover.delivered_order_ids` still carries part ids (partial-split delivery semantics
unhandled; flagged in RULES.md).

## 2026-07-16 — Telematics behavior numbers de-wired from ALL operating constraints

**Decision (user):** observed fleet behavior is calibration/validation material, never a
constraint. Removed in one pass: (1) per-vehicle `shift_start`/`shift_end` MEDIAN walls
(the fleet works one operating day, available from 06:00; NO end wall — 19:00 is a soft
target, service coverage first; the 13h duty / 10h driving caps bound the day); (2) the
trip-count cap κ_v = max(2, median_trips) at every gate (seed, both ALNS insertion
enumerators, repair) — duty/driving/window feasibility is the only per-day limit; (3) the
telematics-p95 per-trip capacity profile columns (physical `payload_kg`/`pallet_capacity`
are THE capacity truth — `_resolve_capacity` always made them win; the profile figures
sat beside them contradicting physics, e.g. a 1.2 t van labeled 10 t).

**Why:** the medians were *descriptive* misused as *prescriptive*. Fleet audit: the
enforced end wall sat on average 76 min before the same vehicle's P75 observed end
(19/35 profiled vehicles ≥1 h; 44/79 vehicles had NO profile and wore a generic
07:00–17:00 fallback). Jan-13 trace: two fresh tractors were activated for 1-pallet
evening errands while returned vehicles sat "idle" — refused only because their MEDIAN
day had ended. Paid-for capacity (the 9 h guaranteed-shift floor) was made phantom-
unavailable, then a second activation was bought.

**Mechanism that made reuse work (`RouteJob.depart_floor`):** the dispatch floor now
rides ON the micro-arrival job; a trip it LEADS may not start driving before the floor —
`evaluate_route` holds the vehicle AT THE DEPOT (route_start = floor). Job-carried means
snapshots and emission re-derive the same held departure (a per-call `trip_earliest`
wiring was rejected: it would have accepted trips whose emitted times violated the
floor). Complement: a BLANK shift_end no longer invents an 18:00 default wall.

**Kept deliberately (telematics as evidence, not constraint):** catchment_km (SOFT
ranking prior from order history — user call pending), GUARANTEED_SHIFT_HOURS 9.0 (P25
duty spans; a cost floor, revisited by the overtime design), FREIGHT_DURATION_FACTOR
(speed physics), trunk network structure + depot anchors (facts), verified_legs /
odometer (validation).

**OPEN (next design):** overtime + fairness — price work beyond the paid floor / past
19:00 and spread it across drivers (no one driver runs to midnight); until then late
running is bounded only by the duty/driving caps.

## 2026-07-16 — Overtime + fairness cost (the priced 19:00) + split-shift duty chains

**Decision (user, same day):** with the walls gone, late work is PRICED, not forbidden.
Two stacked surcharges on the driver-day cost (`driver_day_cost_ev`, default ON,
`--no-overtime-cost` ablation): payroll overtime (×1.5 beyond the 9h paid floor, working
hours only) and an unsocial-hours premium that RAMPS with clock time past 19:00 (×1.5
rising +0.25/h → ×2.0 at 21:00, continuous). **The ramp, not bands** — the user's probe
("one driver 2h OT vs two drivers 1h each?") exposed that flat bands are a dead TIE
within a band; a linear ramp makes late cost quadratic per vehicle-day, so the split is
strictly cheaper whenever geometry is equal and fairness emerges from the objective with
zero bookkeeping. Deliberately a preference: a genuine km saving still consolidates
(`LATE_RAMP_PER_HOUR` = the exchange rate).

**Split-shift duty (user):** the 13h duty cap now binds PER CHAIN in `evaluate_day`
(`DUTY_CAP`; a ≥3h depot gap ends a chain — driver rests or swaps; the gap is unpaid).
This is what legally lets a 06:00 morning vehicle take a floored evening trip. The 10h
DRIVING cap stays whole-day (EU daily driving does not reset on a short rest). NOTE:
before this, the daily evaluator had NO duty check at all — the "13h duty" rode on the
deleted telematics shift wall, so the per-chain check is the first honest duty bound.

**Honesty:** we plan vehicles; trunk tractors swap drivers (1.41 drivers/veh-day) — a
second chain's "overtime" may really be a second driver's straight time; the 9h floor is
charged once per vehicle-day. Refine when driver-level data exists. **Deferred (phase
2):** cross-day evenness (late-hours ledger in handover.json, escalating rates) — build
only if week runs still show one vehicle hogging evenings despite the ramp.

Spec: docs/superpowers/specs/2026-07-16-overtime-fairness-cost-design.md. Suite 910.

## 2026-07-16 — WT254009: tours may not carry pickups to the wrong depot

**The find (user, off the map):** the 2-day probe run commissioned an idle STOKE tractor
for a 23-pallet MK42 6EA pickup destined for the BEDFORD depot — five minutes from the
pickup — and hauled the freight ~195 km back to Stoke. The map drew it faithfully; the
wrongness was upstream. Two defects: (1) no site checked that a pickup's freight lands
at its `target_depot` (a tour returns to its VEHICLE's home); (2) `commission_intraday`
called `select_tour_vehicle` with no `prefer_depot` and no busyness, so an all-idle pool
sorted on capacity alone — geography never entered the pick. The seed's own anchor
comment ("the tour returns there, so anchor at the COLLECTION depot") already stated the
invariant its cross-depot branch violated.

**Decision (user):** (a) target-depot consistency gate at ALL THREE pick sites — seed
`_assign_one` (pickups never cross-depot; `DEPOT_LOAD` cross-tours remain for
deliveries), intraday attach (skip foreign-homed tours), intraday commissioning
(`Candidate.target_depot` + `_depot_bound_mismatch` pool filter); (b) commissioning
passes `prefer_depot` so idle picks are depot-first; (c) regression tests pin the exact
shape (foreign-only pool → honest fall-through/NO_FEASIBLE_TOUR; mixed pool → the
target-depot vehicle wins). No qualifying vehicle = the order stays with the daily/slip
machinery — an honest miss beats freight at the wrong depot. Suite 914.

## 2026-07-17 — ST4 8JB: collocated-origin DIRECTs become depot-loaded deliveries

**The find (user, off the board):** Y888AUK's Jan-12 route ping-ponged — ST4 8JB→OL7,
back, →M23, back, →WA8, ~469 km for 6 pallets — because a customer sits ON the Stoke
depot's estate (ST4 8JB vs anchor ST4 8HP) and its same-day FULL_FLEET orders ride as
`DIRECT_CUSTOMER_MOVE`, an ATOMIC collect→deliver arc in `evaluate_route`. Two DIRECTs
can never be on board together, so same-origin consolidation is unrepresentable; the
ping-pong was optimal FOR THE MODEL (no consolidation gradient — a representation
failure, not a search failure). Seven such orders on Jan-12 (1–5 pal each, six into one
NW corridor, ~682 plan-km ≈ one ~250–300 km sweep); C29BAL drove to Wigan twice; CB22-
and BEDFORD-homed rigids each fetched one. Why no rescue: (1) the tour-side cure
(`TOUR_DEPOT_DIRECT_AS_DELIVERY`) lives in the tour batcher — these are daily-range;
(2) the resolver WOULD choose XDOCK on cost, but the same-day staged window (collection
DEADLINE + 90) empties every delivery window for these wide/date-only collection
windows → `xdock_window_infeasible` → forced DIRECT. The source data shows the real
operator collected all seven in ONE dock visit 06:11–07:41.

**Decision (user: "go through 1-4, write the spec, plan and implement"):** emit the
special case at the SOURCE — legs.py same-day FULL_FLEET branch. Origin within
`DAILY_ORIGIN_AT_DEPOT_RADIUS_KM` (2.0 — deliberately tighter than the tour side's 8:
on a daily trip an unpriced approach is real km) of the source-depot anchor → ONE
depot-loaded `CUSTOMER_DELIVERY` (`:DIR` id kept, no XC/XD pair), `depart_floor` =
collection-open + `COLLOCATED_STAGING_MIN` (30, window-open anchored — non-anticipative,
never the deadline pessimism), `depot_bound` = source depot. Emission-site placement is
load-bearing: freight states derive from legs_df (delivery-only shape →
`AT_DEPOT_OR_HUB_PENDING` at the depot, no state code change), and candidates/
compatibility/resolver/manifest/viz all inherit from one site, idempotently per epoch.
Two evaluator mechanisms: (a) `depart_floor` binds TRIP-WIDE (max over members —
depot-loaded freight boards at departure wherever its job rides; lead-only was a silent
hold-loss for mid-trip jobs) with a B2 suffix guard (`_retimes_committed_departure`)
so a floored job cannot re-time a LAUNCHED trip's committed departure (caught by the
shipped Jan-13 reuse test — the old lead-only read was accidentally load-bearing);
(b) `DEPOT_BOUND` hard infeasibility in `evaluate_route` — the daily path has NO depot
affinity for deliveries (135/603 deliveries in run_depotgate ride foreign-homed
vehicles with no depot visit: freight teleports), and the new leg class must not
inherit that hole. Flags: `--no-daily-depot-direct-as-delivery`,
`--daily-depot-direct-radius-km`, `--collocated-staging-min`. Known scope-outs (own
designs later): the fleet-wide cross-depot delivery teleport; the daily pickup
landing-depot hole (a pickup lands at the vehicle's return depot while the ledger
stamps target_depot); the inbound mirror case (DIRECT terminating AT the collocated
customer). Micro nuance: reclassified legs are deliveries, so intraday-booked
collocated orders wait for the next anchor/warm re-opt (existing "deliveries are
anchor-planned" rule) instead of a 30-min micro. Suite 922.

**Audit follow-ups (same day):** (a) `TOUR:N8GNW:2026-01-12` — a 47 km one-stop
"tour" — was the commissioning safety net serving an 11-pal CB9 collection that failed
noon insertion (NO_FEASIBLE_ROUTE; 11 pal needs artic headroom on a day 17 artics were
trunk-drawn and CB9 shuttle trips are pinned): mechanism correct, service in-window,
cost priced — but the route-backdating audit called it UNTRACED because commissioned
tours were minted outside the anchor stamping loop. Fixed: `_tour_commission` now
stamps `tour_created_at` at commit (threaded through `_tour_attach_step`; suite 929).
Candidate improvement, not built: commission near-range orders as a fresh DAILY trip
instead of a tour record. (b) Reg-name splits ("M888 WSM" supatrak vs "M888WSM"
master) are already neutralized at every production join by `vehicles._norm_reg`-style
collapse — capacity for M888 WSM was correct all along; only ad-hoc telematics
comparisons must canon before joining (corrected: Jan-12 real-active in-master = 60,
plan 52; F8GNW is a genuinely non-fleet asset pinging on the account).

**Probe 1 (run_collocated) + the accounting hole it exposed:** consolidation worked
exactly as designed — six of the seven ST4 8JB orders rode ONE Stoke tractor (C29BAL) as
consecutive corridor drops (~187 leg-km vs ~682; Y888AUK's Jan-12 vehicle-day vanished);
21 reclassified legs fleet-wide (13 CB22-collocated, 7 STOKE, 1 BEDFORD — the estate
customers exist at every depot), all 21 on vehicles homed at their own source depot
(DEPOT_BOUND: 0 mismatches). BUT the service ledger shrank 453→432: `serviceable_
collect_ids` keys the universe on pickup-KIND legs, so the 21 orders (delivery-only
now) silently LEFT the service population — routed but untracked, meaning a dropped one
would fail invisibly. Fixed (TDD, suite 928): bound deliveries are collection-satisfying
for service accounting — `serviceable_collect_ids` includes depot_bound delivery
orders; `_collection_satisfying_job` marks collected_day at the loop + daily sites;
`collection_orders_in_plan` counts a CUSTOMER_DELIVERY on a `:DIR` leg id (the
reclassification signature, daily and tour-side alike). The pre-window carve-out
precedent does NOT apply here: those collections happened outside the plannable window;
these orders are 100% in-window fleet work.

## 2026-07-17 — Disturbance anchor: report the stability the objective can price

**Ask (user):** "we have the disturbance term set to 0; in the future we can tune it,
but we need something like that as an anchor" — do the reports show plan disturbance?

**Find:** No. The objective side existed (warm re-opt = cost + beta·disturbance,
imminence-weighted, reassign ×1 / resequence ×γ=0.5 / additions free; `--beta` default
0 = regression gate), and `csv/churn.csv` logged raw per-anchor rows — but nothing
rolled into any report, and churn_pct is a coarser quantity (unweighted vehicle-day
reassignments only). Measured at beta=0: 94.3% / 94.1% / 83.8% of comparable
uncommitted assignments moved per anchor — the invisible free-reshuffle baseline.

**Decision (user: "build it that way"):** anchor on the OBJECTIVE's own quantity,
measured where beta bites — after each warm solve, `disturbance_breakdown(result,
warm-start reference, γ, the same imminence weights the solver received)` (new pure fn
beside `disturbance`); locked/pinned jobs can't move, so the whole-plan score IS the
uncommitted disturbance. churn.csv gains kind/beta/resequenced/disturbance_score/
weighted_comparable/disturbance_pct; `02_kpi_summary.md` gains a "## Plan stability
(disturbance)" section (`plan_stability_md`, appended by the rolling finalize — the
static path has no epochs); the runlog prints one disturbance line per warm epoch.
"Uncommitted" for churn = jobs on vehicle-days not yet launched (expire_commit moves
launched days into `inflight`, whole-key granularity; open suffixes on launched days sit
out of the churn population, and watermarked prefixes are pinned inside the re-opt).


## 2026-07-17 — Depot pinning: freight is served from where it physically sits (A1)

Sizing the final results campaign exposed the last big physical-consistency hole
(A1 gate, experiments/FINAL_CAMPAIGN.md): **130/618 routed delivery legs in the
2-day probe rode vehicles that never visit the freight's depot** — the freight
teleports into the truck. Evidence method: freight's TRUE location from the plan's
own manifest (PL_IMPORT = the inbound trunk's landing depot; xdock = the picking
TRIP's actual final depot from route_stops, not the ledger claim — 31/415 pickup
claims disagree with physics; prestaged = staging label). Two classes: PL_IMPORT
(70 legs) is the trunk lander and daily assigner disagreeing — re-landing those
pallets on the existing nightly hub lanes costs +0 trips/+0 km (bookkeeping);
xdock-landed + prestaged (60 legs, 274 pal) is GENUINE unpriced inter-depot
repositioning — worst-case dedicated-shuttle repair 4,277 km = **12.4% of the
window's combined km**, one-directionally flattering the plan vs the incumbent
(exactly the dissertation's overclaim risk). The trunk network is hub-and-spoke
only: no depot<->depot lane exists to price a repair.

**Fix: `DEPOT_PINNING` (default ON).** Every daily leg is emitted with
`depot_bound` = the depot label it already carries — pickups bind to
`target_depot` (freight must LAND there: the delivery stages there / the outbound
trunk departs there — this also closes the export-side mirror), deliveries to
`source_depot` (freight RESTS there); DIRECT/HUB_DROP stay unbound. Enforced by
the existing kind-agnostic `DEPOT_BOUND` evaluator gate and the tour-side
`_depot_bound_mismatch` — zero evaluator changes; the whole fix is emission-site
stamping (`legs._pinned`) exactly like the collocated rule, because candidates /
resolver / seed / ALNS / micro / tour paths all inherit from `legs_df`.

Rejected alternative: propagating the pickup's ACTUAL landing depot onto the
delivery at pickup-commit time. Circular — pickup (day D) and delivery (day D+1)
are co-planned at the same anchors, so the bound would derive from an assignment
the same search is free to move. Static pinning is the tour-side precedent
(WT254009 gate) extended to the daily path; labels become physical.

**Accounting re-key (regression caught in design):** two ledger sites inferred
"collocated reclassified delivery" from `depot_bound` presence
(`_collection_satisfying_job`, `serviceable_collect_ids`). Under universal pinning
that key floods the collection ledger with every import; both now key on the
`:DIR` leg/job-id tail (the reclassification identity; `collection_orders_in_plan`
already did). Honest coverage stance: no bound-depot vehicle available -> visible
UNASSIGNED (`DEPOT_BOUND`), never fake feasibility; plan km is EXPECTED to rise —
that is the point. `--no-depot-pinning` = the teleport ablation for the campaign's
mechanism table. Suite 929 -> 940.


**ACCEPTANCE (run_pinned vs run_collocated2, 2026-07-17): 0 spatial violations**
(was 130); ledger 453/0/0 both; combined km +12.05% (34,376 -> 38,518, ~the
worst-case bound — the freedom was worth real km, window-wide not tail-only);
veh-days 98->100; unassigned set REPLACED (ref's 6 tails served; 7 different
beyond-window tails honest-fall-through, collection-side ON_TIME). Same-budget
caveat: part of +12% may be search shortfall (B5 disentangles). Pre-pinning
plan-vs-incumbent numbers were inflated up to ~12% at window scale.


## 2026-07-17 — KPI Assignment section now CLOSES its identity (user-caught)

run_pinned's 02_kpi read "996 in-universe / 943 assigned / 7 unassigned" — a
46-order hole. Two latent bugs in kpi.build_kpi's all-jobs-selected rule:
(a) OPTION-GROUP LOSERS (the XC/XD pair when DIRECT won, the DIR when XDOCK won)
are runnable candidates the search rightly did not pick — 47 such jobs demoted
their fully-served orders out of `assigned orders` while appearing in no
unassigned table; (b) split parts (uuid#S1) were grouped per PART against a
per-ORDER denominator. Fix: per-PARENT, option-aware completeness (no-option jobs
all selected AND each option_set's CHOSEN group fully selected; a set with
nothing chosen = genuine miss), plus new fields partial_orders /
zero_assigned_orders / option_alternative_jobs and an explicit identity line:
`fully + partial + wholly-unassigned = in-universe [OK|MISMATCH]`. run_pinned
corrected: **989 fully (99.3%) + 7 partial + 0 wholly = 996 [OK]**; job side
closes too (1054 assigned + 7 rejected + 47 alternatives = 1108 runnable).
Suite 940 -> 943.

Follow-up (same day, user-caught): the 7 partials' deliveries open Jan-14/15 —
AFTER the window's last day — so NO_FEASIBLE_ROUTE misdescribed an
accounting-scope fact as a physical failure. New `kpi.relabel_beyond_window`
(called ONCE at the top of write_reports so manifest / unassigned_jobs / KPI /
universe agree): earliest allowed service > window end -> DUE_BEYOND_WINDOW
(REPAIRED_DIRECT never touched). KPI adds a within-window line ("100% — all
unplaced jobs are DUE_BEYOND_WINDOW, staged; hands over") and the unassigned
block is retitled "Unassigned By Reason". run_pinned: within-window completeness
100%. Suite 943 -> 945.


## 2026-07-18 — A2 readiness-lag flag SHIPPED + 2-day gate PASSED

`--readiness-lag-min M` (config READINESS_LAG_MIN, default 0): floors PL_IMPORT
delivery legs' depart_floor to day-start(06:00)+M via legs._readiness_floor. Stamps
ONLY import deliveries — not vehicle start, pickups, exports, local, or crossdock —
so the fleet does morning work and imports ride later trips (freight-availability
gate, NOT a shorter day). CLI in both runners; suite 945 -> 949.

**Gate (run_readiness120 = Jan 12-13, M=120 -> 08:00, vs run_pinned M=0):** the
user's day-compression worry — DEFEATED. (1) non-import routes still depart 06:00
(median; 21/38 before 08:00 vs ref 24/52) — fleet busy pre-arrival. (2) import
routes' median departure 06:00 -> 08:00, 0 of 110 before the floor — availability
gate works. (3) ZERO obligated-service loss: 477/477 imports served BOTH runs,
ledger 453 ON_TIME/0/0 both, no slips. The km -7% / unassigned 7->17 is a
WINDOW-EDGE artifact — all 17 are FULL_FLEET BEYOND-window (Jan-14/15) tails; the
ref pre-served ~10 next-window deliveries on spare Jan-12/13 capacity, the lag uses
that capacity differently so fewer future tails pull forward (per-served km even
DROPS 50.6->46.9 as the un-pulled tails were the far ones). Edge artifact dominates
a 2-day cold start, washes out on interior days.

**Measurement consequence:** the 2-day window validates the DESIGN but is the wrong
place to MEASURE the readiness magnitude (edge swamps the in-window cost). The week
ladder (interior days) is where true cost shows. Early signal: at M=120 (08:00) the
fleet absorbs the lag with zero service loss — the readiness assumption may not be
load-bearing at moderate lags; confirm on the week at M=240/360.


## 2026-07-18 — Soft delivery time windows (earliness/tardiness penalty)

The hard delivery TIME_WINDOW cutoff was incoherent with soft coverage: a delivery
that missed its intra-day window was forced to slip to TOMORROW rather than be
delivered slightly late TODAY (day-late preferred over minute-late — backwards for
the customer). And the DAY-granular service ledger (ON_TIME = right date) hid
intra-day timing entirely ("always all on time"). Telematics
([[delivery-window-model-validated]]) confirmed the real op treats windows softly
(points delivered ~50 min early).

Replaced with a SOFT earliness/tardiness penalty on the lexicographic objective.
Service hierarchy (stakeholder-confirmed): **on-time(0) < early(small) <
late(large convex) < slip/unserved(worst)**. Slip/unserved is the EXISTING top
lexicographic coverage tier (served-first), so it is worst by construction — a
very-late same-day delivery still beats an on-time next-day one, and an order only
slips when serving it today is duty-INFEASIBLE (genuine last resort). on-time/early/
late are penalties in the cost tier: `EARLINESS_COEF*early + TARDINESS_COEF*late^2`
(convex p=2, mirrors the overtime ramp). Delivery legs only; pickups keep hard
windows. Hard floors retained under the earliness penalty: freight-availability
(depart_floor), non-anticipation, duty/shift.

Mechanism: window policy widens the EFFECTIVE window to all-day for every class
(latest_finish = the hard duty bound) while the tight customer window survives in
raw_window -> plumbed to RouteJob.window_open/deadline (via CandidateJobRecord
raw_window_start/end); evaluate_route stops rejecting late deliveries and instead
accumulates lateness_cost (RouteEvaluation/DayEvaluation carry it; StopTiming carries
minutes_late/minutes_early); the ALNS objective adds it via `_day_nonkm_cost` at
route_cost + all 5 incremental insert-delta sites. Reporting: route_stops gains
deadline/minutes_late; 02_kpi gains a "Delivery timeliness (intra-day)" section
(on-time %, late count, avg/median/p90/max late minutes) — the day-granular ledger is
unchanged. Config: SOFT_DELIVERY_WINDOWS (default ON), TARDINESS_COEF 0.05 (SEED),
TARDINESS_POWER 2.0, EARLINESS_COEF 0.1 (SEED). Flags: --hard-time-windows (ablation
= hard-VRPTW arm), --tardiness-coef, --earliness-coef.

Suite 949 -> 965. VALIDATION + COEF CALIBRATION HELD (in-universe set may change):
calibrate after the universe settles so ~0 lateness at the chosen lambda (matching
reality) with no km distortion, then adopt for all campaign runs. Interaction: the
readiness-lag experiment now gains a lateness COST signal (a floored import delivered
past its deadline shows tardiness, not just a slip) -> that ladder becomes sensitive.
Headline-affecting -> all campaign runs are post-change (pre-freeze, correct).


## 2026-07-18 — KPI: within-window completeness is the HEADLINE; beyond-window tails are handovers not partials

User-caught on a soft-window run: 946 fully / 50 partial / 996, "within-window 100%
— all 4 unplaced jobs DUE_BEYOND_WINDOW". The 50-vs-4 was self-contradictory: only
4 legs were actually unplaced (manifest: 4 UNASSIGNED, all beyond-window), yet 50
orders read partial. Root: build_kpi counted an order's BEYOND-window delivery tail
(pickup in-window Jan-13, delivery Jan-14) as a required-unplaced leg → partial, even
though the tail is NEXT window's obligation (a staged handover), not a this-window
miss. Only 4 of these reached the rejected list (relabel_beyond_window); the other
~46 were runnable candidates deferred silently.

Fix: build_kpi's completeness loop now EXCLUDES legs dated after window_end from the
required/option-group check (service_date > win_end) and tags such orders as
handovers. So an order whose only shortfall is a beyond-window tail counts as FULLY
served this window + handover, NOT partial. New KpiReport.handover_orders +
within_window_pct property. kpi_summary_md REORDERED: within-window completeness is
now the HEADLINE at the top of Assignment ("within-window completeness: X% — N of M
in-window obligations met"), fully-served shows the handover count inline, partial =
IN-WINDOW shortfalls only. run_tard0 corrected: 996 fully (incl. 50 handover) /
0 partial / 0 wholly / within-window 100.0%. Suite 965 (test renamed
test_beyond_window_tail_is_a_handover_not_a_partial). NOTE: the in-flight λ sweep
used pre-fix code (reports show 50-partial) — the λ CALIBRATION is unaffected
(lateness/km only); the assignment fix lands on all future runs.


## 2026-07-18 — Tardiness-coef (lambda) calibration: lambda=0.05, result lambda-INSENSITIVE

4-run 2-day sweep (Jan 12-13, --tardiness-coef {0, 0.05, 0.5, 2.0}). RESULT:
- lambda=0 (no penalty): 91.1% on-time, 6239 late-min, 55 late deliveries.
- lambda=0.05 (seed): 98.9% on-time, 318 late-min, km -0.6% vs lambda=0.
- lambda=0.5 / 2.0: ~99.2-99.5% on-time; km/late-min wobble (+1.8%/+0.3% km) = local-
  optima NOISE on the 2-day cold-start window, not a trend.
VERDICT: keep default lambda=0.05. The whole gain is at 0->0.05 (9% late -> ~1% for
~0 km); above 0.05 the result is lambda-INSENSITIVE (loose windows + available freight
-> the solver hits deadlines whatever the exact weight). 98.9% matches the telematics
anchor (97% not-late on ranges). Residual ~1% is structural (forced by tight windows).
Framing = a robustness finding, not a knife-edge tune. Full-week runs give clean
datapoints (the 2-day 0.5/2.0 wobbles are edge-artifact noise).

CAVEAT for the chapter: the timeliness on-time base (620 deliveries) includes
missing-window deliveries scored against the 18:00 operating bound, not a real
customer window — so "98.9%" mixes "hit the customer window" (~30% real windows) with
"delivered by end of day" (~70% missing). Refine the timeliness report to count only
REAL-window deliveries before citing the on-time rate. Sweep runs archived.

## 2026-07-23 — Endogenous DIRECT-vs-XDOCK (static ρ=1.6 resolver deleted)

The same-day FULL_FLEET DIRECT-vs-XDOCK mode is now chosen ENDOGENOUSLY by the
seed + ALNS on real routed cost, replacing the static pre-router `resolve_options`
(`DEFAULT_XDOCK_RATIO = 1.6`), which was deleted. A decision-split sweep had shown ρ
governed the mode for 77% of same-day option sets and that 1.6 sat exactly at the
all-XDOCK saturation point — an uncalibrated knob standing in for the groupage
piggyback (XC rides a collection route, XD a delivery route) that a standalone score
can't see. Letting the optimizer price each mode against the routes it actually
consolidates onto is the principled version of what 1.6 faked.

Mechanism (all TDD): `option_mutex.OptionMutex` (at-most-one-group-per-freight,
key = option_set == freight_id); option-group-aware supersede in `insertion_pass`
(drops the RIVAL group, keeps the XDOCK XC↔XD partner); mutex guard in the ALNS
repair loop + option-set-aware coverage count (`alns._served_units`, so a swap is
coverage-neutral and decided on cost, not leg count); the `OptionSwap` ALNS destroy
operator (re-prices against the full solution; filtered out when no option sets so
option-free runs stay bit-identical); seed mutex + a strengthened DIRECT readiness
guard (reject a direct move over freight not AT_CUSTOMER_ORIGIN/ON_VEHICLE — carry-in
freight already staged AT_DEPOT); and `ledger.drop_superseded_option_legs`, a
commit-boundary backstop that enforces the invariant on the final plan across the
rolling loop's separate passes/epochs/ledgers. Report read back from the plan by
`option_report.endogenous_option_choices`.

Validation (Feb 2–4 rolling, combined parquet): 100% within-window completeness,
0 unserved, 0 double-serve, 0 strand, ledger/temporal violations 0; endogenous split
18 DIRECT / 301 XDOCK (realistic groupage-heavy mix, not ρ=1.6's forced all-XDOCK).
The full rolling integration — not the unit tests — surfaced two carry-in crashes
(daily seed + tour commit) and the cross-path double-commit the emission backstop now
catches. TRUNK-vs-HUBDROP stays a pre-router decision (the scheduled depot→hub trunk
is not in routed km). Spec/plan: docs/superpowers/{specs,plans}/2026-07-23-endogenous-*.

## 2026-07-28 — Route-backdating root-cause fix, then two DIRECT/XDOCK integrity bugs

User-reported "ROUTE BACKDATING" violation (WT262812/WT262802/WT262818, Y90RNW,
2026-02-02) traced through several REFUTED hypotheses (micro_ctx propagation loss —
disproven by direct instrumentation; the vehicle-level avail_overrides fix — insufficient,
confirmed by a real re-run still showing the violation) to the true cause:
`rebuild_daily_routes_after_drop` (route_seed.py, the post-drop re-time after
`drop_orphan_deliveries`/`drop_superseded_option_legs` remove a leg) used a bare vehicle
profile with no per-job floor awareness, so a surviving stop could re-time to arrive
before the epoch that placed it. Fixed by threading each surviving job's own dispatch
floor (`job_floors`, from the rolling loop's `placement` trace) into the rebuild via a
`_FloorOverride` proxy reusing the existing `no_early_arrival` mechanism. Confirmed 0
violations in a full re-run (`route-backdating audit: 0 violations`).

The board's timeline visualization had a related but separate bug: it rendered every
snapshot epoch's stop time from the FINAL committed `route_stops.csv`, not just the last
one — rewriting history for stops whose time changed during the post-drop rebuild, making
them look like they'd already jumped to their future position mid-day (indistinguishable
from a 90-min freeze violation on the board). Fixed by reconciling only the LAST snapshot
epoch against committed geometry; every earlier epoch keeps its own historically-accurate
recorded time (`viz_timeline_build.py`).

Investigating the user's follow-up ("still appearing... 90-min freeze not met") surfaced
two REAL, more serious bugs, both in DIRECT/XDOCK option-set handling:

1. **Double-commit**: the rolling loop's `insertion_pass` (E6 micro-pass) had only
   `_supersede_pending`'s WITHIN-BATCH dedup — no mutex against jobs already resident in
   the solution from an EARLIER epoch. A freight's XDOCK alternative (a different job_id
   than its DIRECT leg) could ride in on a later micro pass with the option_set already
   resolved, undetected. `drop_superseded_option_legs` silently cleaned this up at
   emission — including, 92% of the time (61/66 on one Feb 2-3 run), by dropping a leg
   that had ALREADY been watermark-committed (locked to a driver), silently reassigning
   promised work to a different vehicle (R888GNW/2026-02-02: a committed DIRECT collect
   leg vanished, freight resurfacing on an unrelated vehicle via XDOCK). Fixed in two
   layers: (a) `drop_superseded_option_legs` gained a `committed_leg_ids` param — a group
   that would be dropped but holds a committed leg is now NEVER dropped, logged as
   `!! OPTION CONFLICT` instead of silently "resolved"; (b) root cause: `insertion_pass`
   now takes an `option_index` (job_id -> (option_set, option_group)), seeding an
   `OptionMutex` from the current solution before considering any new candidate — the
   same invariant the seed/ALNS repair already had, extended to the path that lacked it.
   Effect on a real Feb 2-3 run: OPTION CONFLICT count 61 -> 0.

2. **DIRECT never got a fair cost comparison at all**: `route_seed.py`'s `_DEP_RANK`
   always ranks XDOCK's pickup (`PRODUCES_DEPOT_FREIGHT`, 0) ahead of the same freight's
   DIRECT leg (`NONE_DIRECT`, 1) — so the seed's `OptionMutex` claim was pure insertion
   ORDER, never a price check; DIRECT was rejected `OPTION_SUPERSEDED` before its cost was
   ever computed. The only remaining chance, ALNS's `option_swap` operator, draws its
   candidates from `unassigned`, which `_repairable_unassigned_meta` populates only for
   reasons in `alns._REPAIRABLE_REASONS` — `OPTION_SUPERSEDED` was missing, so the seed's
   loser never even reached `option_swap`. DIRECT was structurally dead the instant XDOCK
   claimed the option_set, real cost or not: a real Feb 2-3 run (192 option sets) showed
   0/192 chose DIRECT with only the double-commit fixed. Fixed by adding
   `OPTION_SUPERSEDED` to `_REPAIRABLE_REASONS`. Same run, same 192 option sets: 12/192
   chose DIRECT once given a genuine cost-based shot (180/192 XDOCK) — 549->548 on-time
   (1 order slipped 1 day), plausible search-neighborhood collateral, not investigated
   further. `06_plan_choices.md` (`option_report.endogenous_option_choices`) is the
   authoritative source for this split going forward, not ad-hoc route_stops.csv counts.

Full suite 1118 -> 1125 across the session, all green. Validation trail:
`runs_backdate_fixed2` (route-backdating + viz fixes only) -> `runs_backdate_fixed3`
(committed-leg protection added, OPTION CONFLICT surfaced at 61) -> `runs_backdate_fixed4`
(insertion_pass mutex root-cause fix, OPTION CONFLICT -> 0, DIRECT -> 0/192) ->
`runs_backdate_fixed5` (OPTION_SUPERSEDED repairable, DIRECT -> 12/192, all health checks
clean: ledger/temporal violations 0, route-backdating audit 0 violations, 0 unserved).
