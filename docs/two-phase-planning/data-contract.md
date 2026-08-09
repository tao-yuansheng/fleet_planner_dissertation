# Data Contract — Phase 1 ↔ Phase 2

**Status:** Design — pre-implementation  
**Date:** 2026-06  
**See also:** `architecture-two-phase.md`, `phase1-design-options.md`

---

## Overview

Phase 1 produces a `WeeklyPlan`. Each morning Phase 2 consumes the relevant `DayAllocation` slice of that plan and produces a `DayExecutionResult`. The execution result feeds back into Phase 1 so it can update the remaining week's plan with real overnight positions and manifest states.

```
Phase 1  ──[WeeklyPlan]──►  Phase 2  ──[DayExecutionResult]──►  Phase 1 update
```

---

## Shared types

### OrderClass (enum)

```python
class OrderClass(str, Enum):
    LOCAL = 'LOCAL'    # same-day out-and-back from home depot
    TOUR  = 'TOUR'     # multi-day vehicle deployment to remote region
    TRUNK = 'TRUNK'    # flows through B37 or LE10 hub (handled by trunk_planner)
```

### ScopedOrder (extended from existing)

New fields added to the existing `ScopedOrder` dataclass:

```python
@dataclass
class ScopedOrder:
    # --- existing fields (unchanged) ---
    order_id: str
    flow: str                     # 'DIRECT', 'PL_IMPORT', 'PL_EXPORT'
    delivery_date: date
    origin_pc: str
    destination_pc: str
    pallets: float
    weight_kg: float
    stop_type: str                # 'delivery' | 'pickup'
    depot_id: str                 # 'CB22' | 'BEDFORD' | 'ST_IVES' | 'OVERFLOW'

    # --- new fields ---
    order_class: OrderClass       # LOCAL | TOUR | TRUNK
    release_time: datetime        # earliest the vehicle can load this order
                                  # PL_IMPORT: datetime.combine(delivery_date, time(6,0))
                                  # others:    timestamp_created (already available)
    timestamp_created: datetime   # when the order became visible in Qargo
```

**Release time rules:**

| Flow | Release time |
|------|-------------|
| `PL_IMPORT` | `datetime.combine(delivery_date, time(6, 0))` — goods arrive at B37 hub overnight, trunk returns ~03:00–05:00, conservative release at 06:00 depot |
| `DIRECT` | `timestamp_created` — order is bookable immediately |
| `PL_EXPORT` | `timestamp_created` — customer collection bookable immediately |

**Order class assignment rules (applied in `order_classifier.py`):**

```
if flow == 'PL_IMPORT':
    order_class = LOCAL          # always — release_time prevents TOUR use

elif flow == 'PL_EXPORT':
    order_class = TRUNK          # export collections → B37 hub each evening

elif flow == 'DIRECT':
    drive_time = osrm_duration(depot, destination_pc)
    if drive_time <= TOUR_THRESHOLD_SECONDS:   # ~4h one-way
        order_class = LOCAL
    else:
        order_class = TOUR
```

---

## Phase 1 output types

### Tour

A multi-day vehicle assignment. Phase 1 creates one Tour per remote deployment.

```python
@dataclass
class Tour:
    tour_id: str                          # e.g. 'T-X888GNW-20260106'
    vehicle_id: str                       # e.g. 'X888GNW'
    home_depot: str                       # 'CB22' | 'BEDFORD'
    region: str                           # human label, e.g. 'SW_ENGLAND'
    order_ids: list[str]                  # all orders on this vehicle's manifest
    depart_date: date                     # day vehicle leaves depot
    return_date: date                     # day vehicle returns to depot
    planned_overnight_pcs: dict[date, str]
    # e.g. {date(2026,1,6): 'EX2 7', date(2026,1,7): 'TR18 3'}
    # Phase 2 updates these with actual overnight positions each evening
```

### DepotDayBudget

For each depot on each day, what Phase 2 can work with.

```python
@dataclass
class DepotDayBudget:
    depot_id: str
    date: date

    # Vehicles available at this depot this morning
    available_vehicles: list[str]

    # Where each vehicle starts the day
    # Values: 'CB22_DEPOT', 'BEDFORD_DEPOT', 'REMOTE:{lat}:{lon}', postcode string
    vehicle_start_positions: dict[str, str]

    # Orders already on vehicles mid-tour (do not re-assign these)
    # These vehicles appear in available_vehicles only if they need more work today
    pre_assigned_manifests: dict[str, list[str]]   # vehicle_id -> [order_id, ...]

    # Orders Phase 2 should route today for this depot
    # LOCAL orders only; TOUR orders are managed by Phase 1; TRUNK by trunk_planner
    local_order_pool: list[str]                    # order_ids

    # Capacity reserved for PL_IMPORT (arrives from B37 this morning)
    # Phase 2 must keep enough vehicles available to handle this volume
    pl_import_pallet_budget: float
    pl_import_order_count: int
```

### WeeklyPlan

The top-level Phase 1 output.

```python
@dataclass
class WeeklyPlan:
    plan_id: str                         # e.g. 'WEEK-2026-01-06'
    created_at: datetime
    planning_start: date                 # first day covered
    planning_end: date                   # last day covered (inclusive)

    # All multi-day tours scheduled this week
    tours: list[Tour]

    # Per-day per-depot allocations consumed by Phase 2
    # Key: (date, depot_id)
    daily_allocations: dict[tuple[date, str], DepotDayBudget]

    # Orders Phase 1 could not assign (need manual review or next-week carry)
    unassigned_order_ids: list[str]

    # Aggregate metrics for dashboard
    metrics: dict[str, Any]
    # e.g. {'total_orders': 1847, 'tour_orders': 143, 'local_orders': 1512,
    #        'trunk_orders': 192, 'unassigned': 0, 'vehicle_days_used': 287}
```

**Serialisation:** `WeeklyPlan` serialises to `week_plan_{planning_start}.json` in `fleet_replay_exports/`. Phase 2 and the re-optimiser load it from disk each morning.

---

## Phase 2 input contract

Phase 2 (`day_coordinator.py`) currently accepts:
- `day: date`
- `all_dispatch_orders: list[ScopedOrder]`
- `all_veh_locs: dict[str, str]`
- `postcode_cache: dict`

**Extended interface** (additions only, existing signature preserved):

```python
def run_day(
    day: date,
    all_dispatch_orders: list[ScopedOrder],
    all_veh_locs: dict[str, str],
    postcode_cache: dict,

    # NEW — supplied by Phase 1
    day_allocation: DepotDayBudget | None = None,
    # If None: Phase 2 falls back to current behaviour (all vehicles at home depot,
    # all LOCAL orders in pool). This keeps the existing backtest working unchanged.
) -> DayDispatchOutput:
```

When `day_allocation` is supplied:
- `available_vehicles` overrides the fleet roster for this depot+day
- `vehicle_start_positions` overrides `all_veh_locs` for the vehicles in this depot
- `pre_assigned_manifests` orders are excluded from the local pool (already on board)
- `local_order_pool` replaces the full order list as Phase 2's input

When `day_allocation` is None (legacy mode): existing behaviour unchanged.

---

## Phase 2 output types

### DayExecutionResult (new, wraps existing DayDispatchOutput)

```python
@dataclass
class DayExecutionResult:
    date: date
    depot_id: str

    # Existing DayDispatchOutput fields (unchanged)
    dispatch_output: DayDispatchOutput

    # New fields for Phase 1 feedback loop
    end_of_day_positions: dict[str, str]
    # vehicle_id -> postcode of last stop, or 'CB22_DEPOT' / 'BEDFORD_DEPOT'
    # Phase 1 uses these as vehicle_start_positions for the next day

    delivered_order_ids: list[str]
    # Orders confirmed delivered today (remove from any tour manifests)

    still_on_board: dict[str, list[str]]
    # vehicle_id -> [order_ids remaining on vehicle at end of day]
    # Phase 1 uses this to update Tour.order_ids for tomorrow

    unassigned_order_ids: list[str]
    # Orders Phase 2 could not route today (feed to re-optimiser or next day)
```

---

## State carry-forward

Each evening after Phase 2 runs, the feedback loop updates `WeeklyPlan`:

```
for each DayExecutionResult:
    1. Update vehicle positions:
       WeeklyPlan.daily_allocations[(tomorrow, depot)].vehicle_start_positions
           ← result.end_of_day_positions

    2. Update tour manifests:
       for each Tour where vehicle in result.still_on_board:
           Tour.order_ids = result.still_on_board[vehicle]  (remaining only)
           Tour.planned_overnight_pcs[today] = result.end_of_day_positions[vehicle]

    3. Release completed tours:
       Tours where Tour.return_date == today and still_on_board is empty
           → vehicle returned to home depot, available tomorrow

    4. Carry unassigned orders:
       WeeklyPlan.unassigned_order_ids += result.unassigned_order_ids
       (re-optimiser or next morning's Phase 1 update handles these)
```

This loop runs in `multiday_backtest.py` (or the live scheduler in production).

---

## DayState (existing — minimal extension needed)

The existing `DayState` (`multiday_state.py`) stores vehicle locations between days. One new field is needed:

```python
@dataclass
class DayState:
    # existing fields (unchanged)
    vehicle_locations: dict[str, str]
    trunk_manifest: dict     # B37/LE10 trunk state

    # new field
    vehicle_manifests: dict[str, list[str]]
    # vehicle_id -> [order_ids still on board at end of day]
    # Empty dict for vehicles that returned to depot.
    # Populated only for TOUR vehicles mid-deployment.
```

No other changes to `DayState`.

---

## File locations

| Artefact | Path |
|----------|------|
| `WeeklyPlan` JSON | `fleet_replay_exports/week_plan_{start_date}.json` |
| `DayExecutionResult` JSON | `fleet_replay_exports/day_result_{date}_{depot}.json` |
| Phase 1 source | `cambridge/week_planner/` (new package) |
| Phase 2 source | `cambridge/day_coordinator.py` (extended) |
| Shared types | `cambridge/plan_types.py` (new — `Tour`, `DepotDayBudget`, `WeeklyPlan`, `DayExecutionResult`) |
| Extended `ScopedOrder` | `cambridge/scope.py` (add `order_class`, `release_time`) |

---

## Backward compatibility

The existing single-day backtest (`multiday_backtest.py --days 3`) continues to work unchanged:
- Phase 2 is called with `day_allocation=None`
- Falls back to current behaviour: all vehicles at home depot, full order list
- All existing tests pass without modification

Phase 1 is additive — it produces allocations that Phase 2 optionally consumes.
