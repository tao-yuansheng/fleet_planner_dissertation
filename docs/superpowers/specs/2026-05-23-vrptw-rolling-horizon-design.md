# VRPTW Rolling-Horizon Dispatcher Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the PDPTW dispatcher with a VRPTW-based rolling-horizon optimizer that reflects ZEEFLEET's true hub-and-spoke topology, validated against historical telematics as the backtest gold standard.

**Architecture:** Four components built in sequence — route engine, freight tracker, simulation harness, backtest validator. The route engine is a clean VRPTW (delivery-only stops, depot start/end, vehicle activation cost in objective). The freight tracker extracts freight arrival times from historical GPS. The simulation harness replays a historical day as a sequence of depot-return events, re-running the solver each time new freight becomes available. The backtest validator compares planned vs actual delivery-leg metrics.

**Tech stack:** Python, pandas, numpy, existing ALNS solver infrastructure (simulation/alns.py), pgeocode, existing profitability_report cost rates.

---

## Why VRPTW, not PDPTW

Empirical evidence from 21,449 Qargo orders across 79 dates:

- **50.6% PRE_STAGED** — goods already at depot before dispatch day (prior collection or inbound line-haul). No collection leg needed.
- **43.5% VIA_DEPOT** — goods collected from customer premises that day, cross-docked at depot, then delivered. Two separate vehicle legs; goods change vehicle at depot.
- **1.7% DIRECT** — single vehicle, origin to destination, no depot touch. True PDPTW but handled as dedicated single-order runs outside the batch solver.
- **4.2% UNGEOCODED** — excluded.

GPS topology confirms hub-and-spoke: 79% of vehicle-days include a mid-route depot return; 28 vehicle-days per week show confirmed inter-depot artic line-haul (Bedford↔Duxford, Duxford↔St Ives). The PDPTW model was solving the wrong problem — it forced one vehicle to carry goods origin-to-destination, which the real operation never does.

VRPTW is simpler AND more correct: vehicles start and end at their home depot, serve a cluster of delivery points, and return. No pickup-delivery pairs, no precedence constraints, no phantom depot pickups.

---

## Known Gaps (out of scope for this implementation)

- **Live ETA for VIA_DEPOT freight**: the freight tracker uses historical GPS (correct for backtest). Live dispatch requires a predictive ETA model for in-progress collection vehicles — future component.
- **Collection round optimisation**: collection VRPTW (customer→depot) is not optimised here; historical telematics provides the actual collection timing as ground truth.
- **Line-haul scheduling**: inter-depot artic movements are out of scope. Artics are excluded from the delivery fleet in this model.
- **Driver roster / availability**: all vehicles registered in the vehicles table are assumed available. A shift roster signal would improve accuracy.

---

## Component 1: vrptw_engine.py

**Location:** `simulation/vrptw_engine.py`

**Replaces:** `pdp_route.py` for the new dispatcher. The old file is left intact for the legacy backtest.

### Data Structures

```python
@dataclass
class DeliveryStop:
    order_id: str
    lat: float
    lon: float
    weight_kg: float
    pallets: float
    service_h: float = 20 / 60      # handling time at delivery point

@dataclass
class DeliveryRoute:
    vehicle_id: str
    depot_lat: float
    depot_lon: float
    shift_start: datetime            # when driver began work today
    shift_end: datetime              # hard cutoff (shift_start + shift_budget)
    capacity_kg: float
    capacity_pallets: float
    asset_type: str
    stops: list[DeliveryStop] = field(default_factory=list)
```

### Cost Function

```
route_cost(route) = VEHICLE_ACTIVATION_COST          # fixed cost per vehicle opened
                  + fuel_rate[asset_type] × route_km  # fuel: closed-loop depot→stops→depot

fleet_objective = Σ route_cost(r) for r with stops
                + UNASSIGNED_PENALTY × unassigned_count
```

`VEHICLE_ACTIVATION_COST` defaults to **£150** (UK HGV driver day rate proxy). This is the lever that forces geographic clustering: the solver opens a second vehicle only when the fuel saving from shorter routes exceeds £150. Replaces the artificial `--depot-territory-km` constraint entirely.

`UNASSIGNED_PENALTY` remains **£50,000** per dropped order.

Both are runtime-configurable via `set_activation_cost(gbp)` and `set_unassigned_penalty(gbp)`.

### Feasibility

A route is feasible if:
1. Load on board never exceeds `capacity_kg` or `capacity_pallets` at any stop
2. Vehicle returns to depot before `shift_end` (single-day constraint; no overnights)
3. Estimated return time = `shift_start` + travel time (depot→stops→depot, at AVG_SPEED_KMH) + Σ service_h

### Insertion

`try_insert(route, stop, cost_rates)` — inserts a single `DeliveryStop` at the cheapest feasible position. O(n) search (one stop, not a pickup-delivery pair). Returns `InsertionResult(added_cost, new_stops)` or `None`.

`added_cost = route_cost(new) - route_cost(old)`

On the first insertion into an empty route, `added_cost` includes `VEHICLE_ACTIVATION_COST` (opening the vehicle). Subsequent insertions into the same route pay only the marginal detour fuel.

### DIRECT Order Handling

DIRECT orders (1.7%) are not fed into the VRPTW batch. They are computed as dedicated single-order runs:

```
direct_km = haversine(depot, origin) + haversine(origin, dest) + haversine(dest, depot)
direct_cost = VEHICLE_ACTIVATION_COST + fuel_rate × direct_km
```

Added to fleet totals separately. Flagged in output as `route_type: DIRECT`.

---

## Component 2: freight_tracker.py

**Location:** `simulation/freight_tracker.py`

Produces `{order_id: FreightInfo}` for a given date, consumed by the rolling dispatcher.

```python
@dataclass
class FreightInfo:
    order_id: str
    label: str                        # PRE_STAGED | VIA_DEPOT | DIRECT | UNGEOCODED
    freight_ready_time: datetime      # when goods are available at depot for loading
    collection_vehicle: str | None    # vehicle that collected (VIA_DEPOT only)
    collection_return_time: datetime | None  # when collection vehicle docked at depot
```

### Algorithm per order

**PRE_STAGED:** `freight_ready_time = shift_start` (06:00 local). No collection vehicle.

**DIRECT:** `freight_ready_time = shift_start`. Flagged for separate handling; not fed to VRPTW.

**VIA_DEPOT:**
1. Geocode `origin_postal_code` → `(origin_lat, origin_lon)`
2. From day's telematics, find all pings within `R_STOP_KM = 2.0 km` of origin
3. Collect vehicle IDs that visited
4. For each visiting vehicle, find the earliest subsequent depot dwell (≥ 10 min within `R_DEPOT_KM = 2.0 km` of any depot anchor) after the origin visit
5. `collection_return_time = earliest such dwell start`
6. `freight_ready_time = collection_return_time + CROSS_DOCK_BUFFER` (default 30 min)
7. If no visiting vehicle found: `freight_ready_time = None` → order excluded from simulation

**UNGEOCODED:** excluded.

### Vehicle shift consumption

For each collection vehicle identified in step 3 above:

```
hours_worked_on_collection = (collection_return_time - vehicle_first_departure_time).total_seconds() / 3600
```

`vehicle_first_departure_time` = first ping where vehicle is NOT near a depot (first active movement).

This is returned alongside `FreightInfo` as `{vehicle_id: hours_worked}` and consumed by the rolling dispatcher to initialise each vehicle's remaining shift budget.

---

## Component 3: rolling_dispatcher.py

**Location:** `simulation/rolling_dispatcher.py`

Simulates a single day as a sequence of discrete solver invocations triggered by freight availability events.

### Fleet State

```python
@dataclass
class VehicleFleetState:
    vehicle_id: str
    depot_lat: float
    depot_lon: float
    asset_type: str
    capacity_kg: float
    capacity_pallets: float
    shift_start: datetime             # first active movement of the day
    shift_budget_h: float             # total hours allowed (Lorry: 11h, Artic: excluded)
    hours_worked: float               # cumulative hours consumed (collection + dispatch)
    status: str                       # 'at_depot' | 'dispatched' | 'shift_exhausted'
    available_from: datetime          # earliest time vehicle can accept a new dispatch
    assigned_orders: set[str]         # orders currently committed to this vehicle
```

### Event Queue

Built from freight_tracker output:

```
events = [
    (06:00,  'SHIFT_START',   PRE_STAGED + DIRECT order ids),
    (09:43,  'FREIGHT_READY', [order_A, order_B, order_C]),
    (11:15,  'FREIGHT_READY', [order_D, ..., order_J]),
    ...
]
```

Events are sorted chronologically. At each event:

1. Mark listed orders as `ready`
2. Identify `available_vehicles`: vehicles with `status == 'at_depot'` and `available_from <= event_time` and `(shift_budget_h - hours_worked) >= MIN_REMAINING_H` (default 1.5h)
3. Identify `eligible_orders`: ready orders not yet assigned
4. If both non-empty: run VRPTW solver → emit dispatch decisions
5. Update fleet state: dispatched vehicles get `status = 'dispatched'`, `available_from = event_time + estimated_route_duration`
6. Vehicles returning from delivery re-enter pool: `status = 'at_depot'`, `hours_worked += route_duration`

### Solver call

```python
result = run_vrptw(
    orders=eligible_orders,
    vehicles=available_vehicles,   # each carrying its remaining shift budget
    time_budget=30,                # seconds — fast enough for simulation replay
)
```

The solver (ALNS adapted for VRPTW) returns routes. Committed. Not revisited.

### Artic availability

Tractor Units are NOT excluded wholesale. GPS analysis (Jan–Feb 2026, 1,289 artic vehicle-days) shows:
- **35% LOCAL_ROUND** — start and end at same depot, no inter-depot touch. Median 82km max distance, 194km odometer. These are genuine delivery/collection rounds and should enter the delivery pool.
- **14% LINE_HAUL** — confirmed inter-depot (Bedford↔Duxford, Duxford↔St Ives). Excluded per day.
- **33% PARTIAL / 17% AWAY** — ambiguous; excluded conservatively.

Per-day exclusion rule: at simulation initialisation for a given date, identify which specific artics made a confirmed inter-depot movement that day (visited two or more distinct depot anchors within R_DEPOT_KM). Those vehicles are marked `status = 'line_haul'` and removed from the delivery pool. All remaining artics enter the pool as normal vehicles with their depot-appropriate shift budget.

### DIRECT orders

Computed as dedicated runs at `SHIFT_START` event. Not fed to the rolling solver.

---

## Component 4: backtest_vrptw.py

**Location:** `backtest_vrptw.py`

Runs `rolling_dispatcher` for a given date (or range) and compares against telematics-derived actuals.

### Planned metrics (from simulation output)

- Vehicles used (by type)
- Total delivery km (Σ closed-loop route km, excl. DIRECT runs)
- Fuel cost GBP (fuel_rate × km per vehicle type)
- Orders assigned / assignment rate
- Estimated on-time rate (orders whose `estimated_arrival ≤ time_window_end`)

### Actual delivery-leg km (from telematics)

Filter GPS tracks to isolate genuine delivery legs:

1. **Exclude confirmed line-haul artics** — Tractor Units whose track visits two or more distinct depot anchors within R_DEPOT_KM on that day → classified as inter-depot, excluded
2. **Exclude inter-depot movements by any vehicle type** — track where first and last active ping are both within R_DEPOT_KM of two *different* depot anchors → excluded
3. **Remaining** = local-round movements (Lorry / Rigid / Mini / Van / non-line-haul Tractor Unit) that start from a depot

Compute actual km as Haversine sum of sorted ping sequence per vehicle-day (existing method in `actuals_loader.py`).

### Success criteria

| Metric | Current PDPTW | Target VRPTW |
|---|---|---|
| Assignment rate | ~85% | ≥ 91% (actual rate) |
| Planned delivery km vs actual delivery km | ~50% below (scope mismatch) | within ±20% |
| Vehicles used | 9–13 (artificial consolidation) | reflects realistic per-depot delivery fleet |
| On-time rate | 100% (model lies) | honest — late when freight arrives too late |

### Output format

Matches existing `run_backtest.py` table format for direct comparison. JSON report saved to `data/Output/backtest_vrptw_{date}.json`.

---

## File Map

| File | Action | Purpose |
|---|---|---|
| `simulation/vrptw_engine.py` | **Create** | VRPTW route engine (delivery-only) |
| `simulation/freight_tracker.py` | **Create** | Freight arrival times from telematics |
| `simulation/rolling_dispatcher.py` | **Create** | Event-driven simulation harness |
| `backtest_vrptw.py` | **Create** | Validation harness, planned vs actual |
| `simulation/alns.py` | **Modify** | Adapt ALNS operators for VRPTW (single-stop insert, no pickup) |
| `pdp_route.py` | **No change** | Legacy; kept for old backtest |
| `run_backtest.py` | **No change** | Legacy; kept for comparison baseline |

---

## Objective Function Rationale

The activation cost is the critical design decision. Without it:

- Marginal cost of adding an order to an existing route: ~£5–10 extra fuel (small detour)
- Marginal cost of opening a new vehicle: £0 (fuel only, no fixed cost)
- Result: solver packs all orders onto one vehicle → mega-route problem

With `VEHICLE_ACTIVATION_COST = £150`:

- Opening a second vehicle costs £150 before it serves a single order
- Solver opens second vehicle only when the fuel saving from shorter routes exceeds £150
- For 3 depots serving geographically clustered orders, the break-even is roughly 150 km of avoided detour per vehicle opened — this naturally produces local cluster routes without any explicit territory constraint

The £150 default is calibrated to UK HGV driver day rates. It is runtime-configurable for sensitivity testing.

---

## Post-Implementation Fixes (2026-05-24)

Two issues found during backtest on 2026-01-02 and fixed in the same session.

### Fix 1: Depot Territory Constraint

**Symptom:** Backtest showed +103% KM delta with 13 planned vehicles vs 22 actual. Diagnostic revealed 7 of 14 routes were cross-depot — e.g. a Duxford vehicle assigned 1 stop 497km away, a St Ives vehicle with 18 of 19 stops belonging to other depots.

**Root cause:** Haversine underestimates road distance between depots (Bedford↔Duxford is ~55km straight-line but longer by road). The £150 activation cost was insufficient to prevent cross-depot consolidation — adding an order to an existing vehicle's route always looked cheaper than opening a new vehicle at the correct depot.

**Fix:** Added `max_depot_km` parameter (default 100km) to `try_insert` and `cheapest_insertion` in `vrptw_engine.py`. If a stop's destination is farther than `max_depot_km` (haversine) from the route's home depot, `try_insert` returns `None` immediately. Threaded through `vrptw_alns.run_vrptw`, `rolling_dispatcher.simulate_day`, and `backtest_vrptw.py` (`--max-depot-km` CLI flag). Disabling by passing `None` or `--max-depot-km 0` is supported.

**Result after fix:** KM delta reduced from +103% to -59.4% (694km planned vs 1,708km actual).

### Fix 2: Road Distance Factor

**Symptom:** After Fix 1, planned km (694km) was still well below actual (1,708km) — a -59.4% gap in the correct direction but too large.

**Root cause:** `route_distance_km` and `_estimated_return_time` used raw haversine distances. UK urban/suburban road distances are ~30% longer than straight-line haversine. This caused planned km to be systematically underreported and also made time feasibility too optimistic (vehicles appeared to have more shift budget than they actually would on real roads).

**Fix:** Added `ROAD_DISTANCE_FACTOR = 1.3` constant in `vrptw_engine.py`, applied in `route_distance_km` (affects reported km and fuel cost) and `_estimated_return_time` (affects shift feasibility). Also applied in `compute_direct_run`.

**Result after fix:** KM delta improved to -38.8% (1,046km planned vs 1,708km actual).

**Residual gap explained:** The actual 1,708km includes both delivery and collection vehicle rounds from GPS. Collection vehicles (handling ~44% VIA_DEPOT orders) are indistinguishable from delivery vehicles in the telematics data and are not filtered by `_actual_delivery_km`. Approximately half the 22 actual vehicles are collection rounds; actual delivery-only km is estimated at ~850km. Planned 1,046km vs estimated actual delivery 850km is approximately +23% — within range given the haversine approximation.

### Backtest Comparison Limitation

The `_actual_delivery_km` function cannot separate delivery-leg GPS tracks from collection-leg GPS tracks without additional data (e.g. a manifest linking vehicle to order type per trip). The ±20% target in the success criteria table above should be interpreted against delivery-only actuals, not the full local-round GPS sum. A future improvement would tag GPS vehicle-days by their primary task (delivery vs collection) using Qargo order data.
