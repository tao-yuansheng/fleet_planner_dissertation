# Cambridge Regional Dispatcher — Design Spec
**Audience:** Project teammates and future-self
**Date:** 2026-05-28
**Status:** Draft v1 — awaiting review

---

## Summary

A depot-anchored VRPTW dispatcher scoped to the Cambridge (CB22 Duxford) operation. Reuses the existing VRPTW engine and rolling-horizon dispatcher; adds a Cambridge-scope filter, a collection planner for the FULL_FLEET subset, and a backtest comparison framework.

**Scope decisions:**

- One depot, CB22. ~73 in-scope orders/day median, ~3,221 over Jan–Feb 2026.
- Two flows: PALLETLINE-import (delivery only) and FULL_FLEET (collection + delivery).
- Match-first, beat-later validation: v1's goal is to reproduce the real operation, not to optimise it.
- Day-level + distributional comparison for v1 (levels 0 + 1). Per-vehicle route comparison deferred to v2.

**Why this works around the data gaps documented in [`freight/data_gaps_for_providers.md`](../../../../freight/data_gaps_for_providers.md):**

The headline gap — "the order record doesn't say which depot the freight passes through" — is **defined away by single-depot scope**. Every in-scope order is anchored to CB22 by construction; we don't need Qargo to record it.

---

## How this differs from the failed PDPTW dispatcher

The archived PDPTW dispatcher (now in `legacy_pdptw/`) routed each vehicle `Depot → origin (collect) → destination (deliver)` for every order. That invented phantom national collection legs for ~98 % of orders, because the operation is actually a groupage hub-and-spoke where Palletline does the long trunks and our fleet does the local delivery. This produced 3-6× over-plan km on the backtest.

The Cambridge dispatcher avoids the failure mode in three ways:

1. **Order classification at plan time.** Each order is tagged `PL_IMPORT` or `FULL_FLEET` from `order_import_integration_type` + `resource_subcontractor` suffix. Only `FULL_FLEET` orders get collection planning. `PL_IMPORT` orders are delivery-only — no phantom collection legs.

2. **Single-depot scope.** Everything anchored at CB22; no multi-depot ambiguity.

3. **Geographic catchment filter.** A `FULL_FLEET` order needs its origin within 100 km of CB22 to qualify. Drops cross-region planning artifacts (e.g. ST4 Stoke orders flagged with a Cambridge tractor that didn't actually do the trip).

---

## 1. Architecture & file layout

A new `cambridge/` package wraps the existing VRPTW chain. Nothing in `simulation/` is modified.

```
logistics/
├── cambridge/                       ← NEW
│   ├── __init__.py
│   ├── config.py                    ← CB22 constants (depot, fleet, defaults, thresholds)
│   ├── scope.py                     ← order classification + scope filter (pure functions)
│   ├── collection_planner.py        ← FULL_FLEET trunk scheduling (single-origin out-and-back)
│   ├── dispatcher.py                ← Cambridge orchestrator (calls into rolling_dispatcher)
│   └── backtest.py                  ← level 0 + 1 planned-vs-actual comparison
│
├── simulation/                      ← UNCHANGED — reused as-is
│   ├── vrptw_engine.py              ← route math (cost, capacity, time windows)
│   ├── vrptw_alns.py                ← ALNS solver
│   ├── rolling_dispatcher.py        ← rolling-horizon orchestration
│   ├── freight_tracker.py           ← freight availability events
│   ├── data_loader.py
│   ├── actuals_loader.py
│   └── leg_labeller.py
│
└── tests/cambridge/                 ← NEW tests for the cambridge package
```

**Reasoning:**

- `cambridge/` is a single import path that downstream `bedford/` and `st_ives/` packages can mirror later. Each depot becomes one package.
- `config.py` is where every Cambridge-specific override lives. Live `simulation/` constants are never edited; `dispatcher.py` calls the existing setter functions with Cambridge values before each solver run.
- `scope.py` has no I/O — pure classification functions. Easy to test.
- `collection_planner.py` only runs when there are FULL_FLEET orders. Empty otherwise.
- `dispatcher.py` is the orchestrator: scope → collection_planner → existing rolling_dispatcher.
- `backtest.py` is an adaptation of `backtest_vrptw.py` filtered to Cambridge orders and Cambridge rigids.

---

## 2. Order classification & scope filter

### Inputs available at plan time

| Field | Use |
|---|---|
| `order_import_integration_type` | First classifier signal. PALLETLINE / MANUAL / null / HAZCHEM / CLARUS. |
| `resource_subcontractor` | Direction suffix (`import from API` / `export to API`). Verified 91 % reliable for exclusion. |
| `origin_postal_code`, `destination_postal_code` | Geographic scope check. |
| `transport_service` | Identify `Specialist Movement` directs (out of scope for v1). |
| Time-window fields, weight, pallets | Constraints. |

`resource_tractor` and `resource_rigid` are **not** trusted at plan time — the dispatcher decides assignments. They are only consulted for backtest-mode confirmation.

### Classification rules

| Flow tag | Rule | Routing |
|---|---|---|
| `PL_IMPORT` | `integration_type == 'PALLETLINE'` AND `resource_subcontractor` contains `import from API` | Delivery-only from CB22 |
| `FULL_FLEET` | `integration_type in ('MANUAL', null)` AND no `resource_subcontractor` flag AND origin within 100 km of CB22 | Collection + delivery |
| Out of scope (v1) | Specialist Movement; HAZCHEM; CLARUS; sub-only; ambiguous MANUAL+export; FULL_FLEET origin outside catchment | — |

### Cambridge geographic scope

Destination postcode prefix ∈ allow-list defined in `config.py`. Initial allow-list from the cambridge_audit:

```python
CB22_SERVICE_PREFIXES = {'CB', 'SG', 'CM', 'AL', 'IP', 'SS', 'PE', 'RH', 'NW', 'LU'}
```

### Two modes

| Mode | Use case | Scope filter |
|---|---|---|
| **Backtest** | v1 validation against historical days | Rule-based + telematics ground truth (only orders Cambridge fleet demonstrably touched) |
| **Forward** | Live dispatch | Rule-based only; what a live dispatcher would have |

### Verified reliability (from `investigations/verify_direction_and_pickups.py`)

| Classifier | Precision | Notes |
|---|---|---|
| `PL_IMPORT` → fleet did NOT collect | **91 %** | Strong negative signal; the `(import from API)` suffix is reliable. |
| `PL_EXPORT` → fleet DID collect | 79 % | Mostly reliable for "we collected". |
| `FULL_FLEET` → fleet DID collect | 61 % | Weaker — `resource_tractor` field sometimes records planning intent that didn't execute. |

**Implication:** in backtest mode, use telematics ground truth to filter FULL_FLEET to orders where the collection trip actually happened. In forward mode, accept the 60 % precision floor as a known limitation.

### Output

```python
@dataclass
class ScopedOrder:
    order_id: str
    name: str                      # WT...
    flow: Literal['PL_IMPORT', 'FULL_FLEET']
    origin_pc: str | None          # None for PL_IMPORT
    destination_pc: str
    weight_kg: float
    pallets: float
    delivery_window: tuple[datetime, datetime]
    collection_window: tuple[datetime, datetime] | None  # FULL_FLEET only
```

---

## 3. Delivery routing core

What `cambridge/dispatcher.py` calls into the existing `vrptw_alns.run_vrptw()` for. Same engine, Cambridge-scoped inputs.

### Decision variables (per planning event)

- Which scoped orders get assigned to which rigid
- Stop sequence per rigid's route
- Implied: arrival times per stop, departure time from CB22

### Objective function (inherited from `vrptw_engine.py`)

```
total_cost = sum(VEHICLE_ACTIVATION_COST  if route has stops else 0
                 + fuel_rate[asset_type] * route_km)
             + UNASSIGNED_PENALTY * unassigned_count
```

Defaults: activation = £150/vehicle, road-distance factor = 1.3×, average speed = 50 km/h, service = 20 min/stop. All overridable from `cambridge/config.py`.

### Hard constraints

| Constraint | Source |
|---|---|
| Capacity (weight kg, pallets) | `supatrak_vehicle_list_enriched.csv` (max_tonnes) |
| Time windows per stop | `[destination_requested_start_timestamp_local, that + tw_hours_for_service_level]` |
| Shift budget | `config.py` (default Rigid = 11 h, configurable per-rigid) |
| Depot anchor | All routes closed-loop at CB22 GPS coords |
| End-of-day cutoff | All rigids back at CB22 by `operating_day_end` (default 18:00) |

### Time-window width from service level

Mapped in `config.py`:

```python
SERVICE_LEVEL_WINDOW_HOURS = {
    'Next day':  24,
    'Economy':   48,
    'Specialist Movement': 4,
    # Date-only windows default to operating day [06:00, 18:00]
}
```

If `destination_requested_start_timestamp_local` has time = 00:00, treat as date-only and apply the operating-day default.

### Solver

`simulation/vrptw_alns.run_vrptw()` — no solver code changes.

### Per-event I/O

```python
@dataclass
class DispatchInput:
    available_orders: list[ScopedOrder]
    available_rigids: list[VehicleFleetState]
    planning_time: datetime
    locked_routes: dict[vehicle_id, Route]

@dataclass
class DispatchOutput:
    routes: dict[vehicle_id, list[Stop]]
    unassigned: list[order_id]
    metrics: dict
```

### Vehicle re-availability rule

A rigid is eligible for re-dispatch only if it can complete a viable trip and return to CB22 by the end-of-day cutoff:

```python
def is_eligible_for_redispatch(rigid, planning_time, operating_day_end):
    remaining = min(rigid.shift_end, operating_day_end) - planning_time
    return (rigid.status == 'at_depot'
            and remaining >= MIN_VIABLE_TRIP_HOURS)  # default 1.5h
```

---

## 4. Collection planning (FULL_FLEET only)

What `cambridge/collection_planner.py` does. Not VRPTW — a simple scheduler.

### Algorithm

1. Group FULL_FLEET orders by `(delivery_date, origin_postcode)`.
2. For each group, schedule **one** single-origin out-and-back trip on **prior day morning**.
3. Pick a Cambridge tractor by greedy cheapest-fit (lowest fuel + activation, sufficient shift remaining).
4. Emit freight-arrival time: `tractor_return_to_CB22 + 30 min cross-dock buffer`. This feeds `freight_tracker.py`.

The Cambridge tractor pool is identified at startup using the same static vehicle→depot map as the rigids — vehicles of `asset_type == 'Tractor Unit'` whose telematics shows them parking overnight at CB22 ≥ 90 % of the time. List materialised in `config.py`.

### Per-origin timing profile (learned from telematics)

Default-departure-hour is **per-origin**, not global. Derived from `investigations/verify_collection_patterns.py`:

| Origin | Median depart from CB22 | Median trip duration (h) | Median origin dwell (min) | Sample n |
|---|---|---|---|---|
| **CB9 ARDEX** | 10:00 | 3.2 | 62 | 17 |
| **SG8 Royston** | 08:00 | 5.7 | 282 | 45 |
| **AL7 Welwyn** | 07:00 | 9.5 | 289 | 13 |
| **SG6 Letchworth** | 09:00 | 6.8 | 66 | 5 |

Stored in `config.py` as a `collection_profile` dict. New origins get a global fallback (08:00 depart, 60-min dwell).

### Collection trip object

```python
@dataclass
class CollectionTrip:
    trip_id: str
    tractor_id: str
    origin_pc: str
    orders: list[order_id]
    depart_cb22: datetime
    arrive_origin: datetime
    depart_origin: datetime
    arrive_cb22: datetime
    freight_ready_at_depot: datetime    # = arrive_cb22 + cross_dock_buffer
```

### Hard constraints

- Round-trip + service time ≤ tractor's shift budget
- Freight back at CB22 before next-day 06:00 delivery dispatch
- Tractor + trailer capacity ≥ grouped order volume

### Failure handling

A collection trip that fails (tractor never reached origin per telematics, or returned after deadline) → affected FULL_FLEET orders rolled to the next day's collection_planner event. Logged in the day's report.

### What v1 does NOT do (deferred to v2)

- Multi-origin trunk runs in one trip
- Trailer drop-and-swap modelling
- Same-day collection
- Co-loaded multi-day collections (one trip covering Wed + Thu)
- Decomposing the SG8 / AL7 long-dwell pattern (we treat empirical median as-is)

### Verified facts (from telematics)

- **80 collection trips traced** across the four target origins (Jan + Feb).
- **74 of 80 depart 05:00–11:00** — confirms morning, not afternoon.
- **93 % are prior-day** collection.
- **Multi-stop trips are rare** (10 of 80) — single-origin out-and-back is dominant.
- **SG8 / AL7 anomaly:** 4-5 hour median origin dwell suggests trailer-drop or scheduled-appointment behaviour we don't yet model. v1 uses empirical median as a safe over-estimate.

---

## 5. Rolling-horizon orchestration

### Day pattern (operating day D)

```
                              ─── DAY D-1 ───
06:00 D-1   collection_planner runs for orders delivering on day D
08:00 D-1   tractors depart CB22 per origin profile (morning)
end of D-1  tractors return; freight at CB22

                              ─── DAY D ───
06:00 D     delivery_dispatch event #1
            input: PRE_STAGED PL_IMPORT orders + FULL_FLEET freight ready
            output: route plans for all eligible Cambridge rigids

during D    delivery_dispatch event #N (rolling)
            triggers: Palletline trunk arrival
                   OR rigid returns to CB22 with shift time remaining
            input: newly available orders + eligible rigids + locked routes
            output: incremental routes for unfrozen rigids

end of D    all rigids back at CB22 by operating_day_end (default 18:00)
            unassigned orders → rolled to next day
```

### Freight availability sources

| Order type | Forward mode | Backtest mode |
|---|---|---|
| PL_IMPORT, PRE_STAGED | Default 06:00 | Telematics (Palletline trunk arrival inferred from depot GPS) |
| PL_IMPORT, VIA_DEPOT mid-day | Default 12:00 (configurable) | Telematics |
| FULL_FLEET | From `collection_planner` output | Telematics (tractor return time) |

### Locking

- Routes returned from any solver event are committed; added to `locked_routes`.
- An order on a locked route is never re-planned by later events.
- A vehicle on a locked route returns to the eligible pool only after completing it (`at_depot` status restored).

### Late-arrival handling

Time-window enforcement handles this naturally — no special logic. `service_level_name` maps to a window width; the solver simply won't try to deliver an order if its window doesn't open today. Late freight rolls naturally to its first feasible day.

### Component flow

```
   scope.classify
        │
        ▼
  ┌──────────────────┐
  │  scoped orders   │
  └──────────────────┘
       │     │ FULL_FLEET
       │     ▼
       │  ┌──────────────────────────┐
       │  │  collection_planner       │
       │  │  → trips + freight times  │
       │  └──────────────────────────┘
       │                │
       │ PL_IMPORT      │
       │                ▼
       │       ┌──────────────────────┐
       │       │  freight_tracker     │
       │       │  (when ready?)       │
       │       └──────────────────────┘
       │                │
       └────────────────┴──────► rolling delivery dispatch loop
                                 (existing rolling_dispatcher)
                                          │
                                          ▼
                                 ┌──────────────────────┐
                                 │ routes per rigid     │
                                 │ per planning event   │
                                 └──────────────────────┘
```

---

## 6. Validation framework

What `cambridge/backtest.py` does. Levels 0 + 1, day-grain, jan/feb split.

### Run pattern

```
for each operating day D in 2026-01-02 ... 2026-02-28:
    1. Run cambridge.dispatcher in backtest mode for D
       (collection_planner on D-1, rolling delivery on D)

    2. Build actuals from telematics:
       - Per-rigid GPS-derived km, active trip count
       - Stops inferred via leg_labeller for confirmable orders

    3. Compute level 0 + level 1 metrics

    4. Emit per-day comparison report + day_compare_<date>.json
```

### Level 0 (day totals)

| Metric | Planned source | Actual source | Pass threshold (v1) |
|---|---|---|---|
| Total delivery km | sum(route_km) over scoped orders | sum(GPS km) over Cambridge rigids | within ±20 % |
| Vehicles used | count(routes with ≥1 stop) | count(rigids with ≥100 moving pings) | within ±2 |
| Fuel cost £ | sum(route_cost) | Jigsaw fuel records (`actuals_loader._jigsaw_fuel_gbp`) | within ±25 % |
| Assignment rate (in-scope) | scoped orders routed / scoped orders | scoped orders confirmed delivered / scoped orders | within ±10 pp |
| On-time rate | scoped orders within window | Qargo `destination_timestamp` vs window | within ±10 pp |

A day passes level 0 if all five metrics within threshold.

### Level 1 (distributional)

| Metric | Test | Pass threshold |
|---|---|---|
| Per-vehicle stop-count histogram | KS distance vs actual | ≤ 0.30 |
| Per-vehicle km histogram | KS distance vs actual | ≤ 0.30 |
| Destination postcode-district set | Jaccard overlap | ≥ 0.80 |
| Median rigid departure time from CB22 | Difference (min) | ≤ 60 min |
| Median rigid return time to CB22 | Difference (min) | ≤ 60 min |

### Per-day report format

```
==============================================================
  CAMBRIDGE BACKTEST  2026-01-07
==============================================================
                                      PLANNED       ACTUAL    DELTA
  Total km                              1,820        1,510   +20.5%  ⚠
  Vehicles used                            11           10     +1
  Fuel cost £                             762          640   +19.1%  ⚠
  Assignment rate (in-scope)           96.4 %       94.1 %  +2.3pp
  On-time rate                         98.2 %       95.6 %  +2.6pp
  ────────────────────────────────────────────────────────────
  L1 stop-count KS dist                  0.18                  pass
  L1 km histogram KS dist                0.22                  pass
  L1 postcode-district Jaccard           0.84                  pass
  L1 median depart-time                  06:32       06:55  -23min  pass
  L1 median return-time                  16:45       17:18  -33min  pass
  ────────────────────────────────────────────────────────────
  Day verdict: PASS (1 L0 warning: km +20.5% > threshold)
==============================================================
```

### Aggregate report

After all 46 operating days: median, p25, p75 of each metric; pass/partial/fail histogram; identification of systematically-failing days for inspection.

### Test data split (v1)

- **January** = development set. We tune thresholds and fix bugs here.
- **February** = held-out validation. We don't peek. Final reported metrics come from this set.

### Output artefacts

- `data/Output/cambridge/day_compare_<date>.json` — per-day raw metrics
- `data/Output/cambridge/aggregate_<run_id>.json` — summary across the run

### What this does NOT do (level 2/3, deferred to v2)

- No per-order vehicle attribution
- No edit-distance comparison of stop sequences
- No detection of "wrong-truck-for-the-right-orders" beyond what postcode-district Jaccard catches

---

## 7. Cambridge data feasibility — confirmed

From `investigations/cambridge_audit.py`:

| Metric | Value |
|---|---|
| In-scope orders, 2 months | **3,221** |
| Operating days with orders | 46 of 59 calendar days (Mon-Fri pattern, Sat ~1/day, no Sun) |
| Per-day order distribution | median 73 (p25=69, p75=85, p90=96, max=100) |
| Cambridge rigids identified | 11 (active 30-41 days each = 51-69 % of calendar days) |
| Cambridge-rigid telematics pings | 343,158 |
| Orders with directly confirmable delivery event (today's matcher) | 1,722 (54 %); ~75–80 % achievable with better stop-clustering |

The data is sufficient for both planning and validation.

---

## 8. Open questions & TBDs

These are deferred to implementation-time decisions, not blockers for the spec:

1. **`service_level_name` → window-width mapping.** Initial values in `config.py` need a sanity check against historical on-time rates. If "Economy = 48h" produces 100 % on-time and "Next day = 24h" produces 80 % on-time, the latter window may need widening.
2. **Depot-area destinations.** Orders ending in CB22 postcode area: treat as regular stop with service time, or as zero-mile depot drop. Default v1: regular stop. Validate one day's telematics during implementation to confirm rigids actually make a discrete stop at depot-area customers.
3. **SG8 / AL7 long-dwell decomposition.** v1 uses empirical median as a safe over-estimate. v2 may need to decompose into "load time + waiting time" or model appointment scheduling. Not a v1 blocker.
4. **VIA_DEPOT mid-day default arrival time.** v1 default = 12:00 for orders not arriving at 06:00 pre-staging. May need per-trunk profiling.
5. **Cross-dock buffer.** v1 default = 30 min between tractor return and freight ready for delivery. Validate against `freight_tracker.py` derivation.

---

## 9. v2 / v3 roadmap (deliberately deferred)

| v2 | Why deferred |
|---|---|
| Level 2 validation (per-vehicle order-set Jaccard) | Requires order-to-stop matcher; only worthwhile if v1 levels 0+1 pass |
| Multi-origin trunk runs | Rare per the data (10 of 80 trips); diminishing returns |
| Trailer drop-and-swap | Adds significant solver complexity; not the dominant pattern |
| Decomposed SG8/AL7 dwell | Operationally distinct, needs more investigation |
| Forward-mode classifier confidence calibration | v1 already operates in backtest mode where ground truth is available |
| Beat-actual metrics (cost / km reduction claims) | Per the success-metric decision: match first, beat later, and only with careful operational-rules review |

| v3 | Why deferred |
|---|---|
| Bedford (MK42) regional dispatcher | Mirror the Cambridge pattern once v1 is validated |
| St Ives (PE27) regional dispatcher | Same |
| Multi-depot coordination | Requires inter-depot transfer modelling; not in current data |
| Direct moves (Specialist Movement) | Different routing problem; ~0.4 % of volume |
| HAZCHEM scope | Different operating rules; separate certification |

---

## 10. Reference to related docs

- [`freight/data_gaps_for_providers.md`](../../../../freight/data_gaps_for_providers.md) — why the live data record is insufficient for forward planning at network scale, and what we need from providers to expand beyond v1.
- [`freight/network_scope_problem.md`](../../../../freight/network_scope_problem.md) — methodological framing of the network-vs-fleet scope mismatch and how single-depot scope sidesteps it.
- [`freight/operation_rules.md`](../../../../freight/operation_rules.md) — operating constants (shift hours, service times, etc.) derived from telematics.
- [`freight/dissertation_rq.md`](../../../../freight/dissertation_rq.md) — research questions; v1 of this dispatcher proves RQ1 + RQ2 are feasible.
- [`dispatcher-explained.md`](dispatcher-explained.md), [`dispatcher-summary.md`](dispatcher-summary.md), [`vrptw-dispatcher-explained.md`](vrptw-dispatcher-explained.md) — context on the earlier PDPTW + VRPTW dispatchers that this design builds on / supersedes.

---

## Appendix A — Verified data facts

Recorded for traceability. All from `investigations/`.

- **Order classification reliability (n=30/category):** `PL_IMPORT` 91 % accurate for exclusion; `PL_EXPORT` 79 % for inclusion; `FULL_FLEET` 61 % for inclusion. Source: `verify_direction_and_pickups.py`.
- **Cambridge FULL_FLEET origins (top 10):** CB9 (ARDEX, 178), ST4 (Stoke, 100 — flagged but geographically out of catchment), SG8 (Royston, 66), CB2 (55), AL7 (32), SG6 (17), MK4 (5), CM2 (4), CB1 (3), EX1 (2).
- **Collection trip patterns:** 80 trips traced; 74 of 80 depart 05:00–11:00; 93 % prior-day; 10 of 80 multi-stop. Source: `verify_collection_patterns.py`.
- **Cambridge audit coverage:** 3,221 in-scope orders / 46 days / 11 rigids / 343 k pings / 1,722 confirmable deliveries. Source: `cambridge_audit.py`.

---

## Appendix B — Glossary

| Term | Meaning |
|---|---|
| CB22 | Cambridge depot at Duxford CB22 4PS |
| MK42 | Bedford depot |
| PE27 | St Ives depot |
| PL_IMPORT | Order where Palletline brought freight INTO our network; we deliver only |
| FULL_FLEET | Order where we own both collection and delivery legs |
| VRPTW | Vehicle Routing Problem with Time Windows (delivery-only formulation) |
| PDPTW | Pickup-and-Delivery Problem with Time Windows (the failed earlier formulation) |
| ALNS | Adaptive Large Neighbourhood Search — the solver |
| Level 0 / 1 / 2 / 3 | Validation grain: day totals / distributional / per-vehicle set / per-vehicle sequence |
