# Multi-Day Dispatcher Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extend the single-day Cambridge VRPTW dispatcher into a multi-day planner that tracks order state across days, models overnight trunk runs to the Palletline (B37 7HB) and Hazchem (LE10 3BS) hubs, and replays or plans multiple consecutive days with accurate cross-docking, 2-day lookahead, and per-day HTML map output.

**Architecture:** Layered — a rule-based trunk planner handles tractor hub assignments; the existing VRPTW handles rigid deliveries; a day coordinator orchestrates both; a state manager persists end-of-day state as JSON files between days. Shift budgets are soft constraints (driver swaps possible), not hard blocks.

**Scope:** Duxford-circuit vehicles only (`CircuitName == 'Duxford - Rigid'` and `'Duxford - Artic'`, excluding Service Vans). Bedford, St Ives, and Stoke circuits are excluded for now.

**Modes:** Both backtest (replay historical days, compare vs telematics actuals) and forward planning (compute tomorrow's plan from today's state).

---

## Verified Operational Facts

From telematics analysis (Jan 6–7 2026):

- **Palletline trunk**: tractors depart CB22 after all daytime collections are complete → drive 161.7 km / 2.46h HGV to B37 7HB Birmingham → cross-dock overnight (median 380 min / 6.3h) → return to CB22 ~03:00–05:00 with PL_IMPORT freight.
- **Hazchem trunk**: tractors serve LE10 3BS Hinckley (138.3 km / 2.03h HGV from CB22) on the same overnight cycle (median 330 min / 5.5h dwell). X8GNW, X88GNW and Y88RNW are the dominant Hazchem tractors in January — assignment is dynamic, not hardcoded.
- **PL_IMPORT freight** does NOT get delivered directly to CB22 by Palletline. Our tractors pick it up from the hub every morning.
- **FULL_FLEET cross-docking**: goods collected on Day D with `destination_date > D` are staged at CB22 depot overnight and delivered on the correct future day.
- **Trunk departure is dynamic**: tractors do not leave at a fixed time — they depart after all planned collections have returned to depot plus a 30-minute loading buffer.
- **Driver swaps are allowed**: shift budget is a soft preference, not a hard block. A vehicle can run across multiple drivers in one day.

---

## Daily Operation Timeline

```
Day D-1 evening:
  Rigids complete final deliveries and return to CB22.
  Collections for Day D-1 arrive back at depot.
  Trunk planner calculates departure time:
    departure = max(all collection return times) + 30 min loading buffer
  Tractors load PL_EXPORT freight and depart for B37 / LE10 3BS.
  FULL_FLEET freight collected today but delivery_date > D-1 is
  cross-docked at CB22 → written to state JSON.

Overnight:
  Tractors at hub. Cross-dock / sort.

Day D ~03:00–05:00:
  Trunk tractors return to CB22 with PL_IMPORT freight
  (all PL_IMPORT orders where destination_date == D).

Day D 06:00 — Day-start plan computed:
  Order pool assembled (see Order Pool Assembly below).
  2-day lookahead: check D+1 PL_IMPORT volume to validate tonight's trunk size.
  Trunk plan for tonight generated.
  Rigid VRPTW runs for deliveries.
  HTML map written.

Day D 06:00–18:00+:
  Rigids execute delivery routes (multiple trips).
  Tractors execute long-haul PL_EXPORT collections.
  Tractors with no collections assist with rigid-range deliveries (fallback).

Day D evening (dynamic departure):
  All collection tractors return to CB22.
  Trunk departs for B37 / LE10 3BS.

End of Day D:
  state_YYYY-MM-DD.json written.
```

---

## Order Date Model

The **`date`** column in Qargo = order creation date = the day the dispatcher first knows about this order. It can plan ahead from creation.

| Flow | Collection day | Delivery day |
|---|---|---|
| PL_IMPORT | n/a (tractor picks up at hub on delivery morning) | `destination_date` |
| FULL_FLEET | `origin_date` | `destination_date` |
| PL_EXPORT | `origin_date` | n/a (we deliver to hub; Palletline/Hazchem does last mile) |
| PALLETLINE-no-sub + CB22 vehicle | `origin_date` | `destination_date` |

**PALLETLINE-no-sub orders** (8 in January): currently classified `None` (dropped). Fix: if a CB22 vehicle is assigned → classify as `FULL_FLEET`. In forward mode (no assignment yet): if `origin_postal_code` is within 100 km catchment → classify as `FULL_FLEET`. Otherwise drop.

---

## Order Pool Assembly (each Day D)

```
1. PL_IMPORT, destination_date == D
     → in rigid delivery pool (freight arrived from trunk this morning)

2. depot_inventory from D-1 state, delivery_date == D
     → in rigid delivery pool (cross-docked from previous day(s))

3. FULL_FLEET, origin_date == D AND destination_date == D
     → same-day: rigid collects from shipper, returns to depot, delivers

4. FULL_FLEET, origin_date == D AND destination_date > D
     → rigid collects only; cross-docked at depot; written to D state

5. PL_EXPORT, origin_date == D
     → rigid or tractor collects from shipper; brought back to CB22 before trunk
     → Hazchem sub → LE10 3BS; Palletline sub → B37 7HB

6. unassigned_carry_forward from D-1 state
     → retry before new orders; only if delivery_date <= D

7. 2-day lookahead (D+1 PL_IMPORT)
     → NOT in rigid pool; used only by trunk planner to size tonight's run
```

**Cross-dock multi-day**: if `destination_date == D+2` or later, freight stays in `depot_inventory` until its delivery date. The state file holds it until the correct day.

---

## State File Format

One file per day: `output/state/state_YYYY-MM-DD.json`

```json
{
  "date": "2026-01-07",

  "vehicle_locations": {
    "X88RNW":  "B37_HUB",
    "X8GNW":   "LE10_HUB",
    "X88GNW":  "LE10_HUB",
    "W88GNW":  "CB22_DEPOT",
    "S88GNW":  "CB22_DEPOT"
  },

  "depot_inventory": [
    {
      "order_id": "abc123",
      "flow": "FULL_FLEET",
      "origin_pc": "CB21 6BS",
      "destination_pc": "CM21 9LE",
      "pallets": 1.0,
      "weight_kg": 300.0,
      "delivery_date": "2026-01-08",
      "delivery_window": ["2026-01-08T06:00:00", "2026-01-08T18:00:00"]
    }
  ],

  "unassigned_carry_forward": ["order_id_x", "order_id_y"],

  "trunk_manifest": {
    "B37_HUB": {
      "tractors": ["X88RNW", "W88GNW"],
      "pallets_outbound": 34.0,
      "departed": "2026-01-07T19:22:00",
      "expected_return": "2026-01-08T04:15:00"
    },
    "LE10_HUB": {
      "tractors": ["X8GNW", "X88GNW"],
      "pallets_outbound": 12.0,
      "departed": "2026-01-07T18:45:00",
      "expected_return": "2026-01-08T03:30:00"
    }
  }
}
```

**Backtest mode**: `vehicle_locations` is derived from telematics (last known postcode of each Duxford tractor after 18:00 on Day D-1 — if B37 7\* postcode → `B37_HUB`, if LE10 3\* → `LE10_HUB`, else `CB22_DEPOT`).

**Forward mode**: `vehicle_locations` is written from the previous day's trunk plan output.

---

## Trunk Planner Logic

`trunk_planner.py` — rule-based, no VRPTW.

```
Inputs:
  pl_export_orders_today   list of ScopedOrder (PL_EXPORT, collected today)
  pl_import_tomorrow       list of ScopedOrder (PL_IMPORT, destination_date == D+1)
  available_tractors       list of tractor IDs at CB22 after collections complete
  collection_return_times  dict[vehicle_id → estimated_return_datetime]

Steps:
  1. Partition PL_EXPORT by hub:
       palletline_pallets = sum(o.pallets for o in pl_export if not hazchem)
       hazchem_pallets    = sum(o.pallets for o in pl_export if hazchem)

  2. Per-tractor capacity from VEHICLE_PROFILES (no global constant):
       tractor_capacity = {vid: VEHICLE_PROFILES[vid]['capacity_pallets_per_trip']
                           for vid in available_tractors}

  3. 2-day lookahead sizing:
       tomorrow_pl_import_pallets = sum(o.pallets for o in pl_import_tomorrow)
       # Minimum trailers needed to carry outbound PL_EXPORT to B37
       trailers_b37 = tractors_to_cover(palletline_pallets, tractor_capacity,
                                         available_tractors)
       # Bump up if tomorrow's inbound needs more capacity
       trailers_b37 = max(trailers_b37,
                          tractors_to_cover(tomorrow_pl_import_pallets,
                                            tractor_capacity, available_tractors))
       trailers_le10 = tractors_to_cover(hazchem_pallets, tractor_capacity,
                                          available_tractors)

  4. Tractor selection — no hardcoded preferences:
       Tractors already at a hub (from state) stay assigned to that hub for continuity.
       Remaining tractors sorted by collection_return_time ascending (earliest-free first).
       Assign le10-bound tractors first (smaller pool), then b37-bound.
       If fewer tractors available than needed: assign max available, log warning.

  5. Trunk departure:
       if collection_return_times:
           depart = max(collection_return_times.values()) + TRUNK_LOADING_BUFFER_MIN
       else:
           depart = datetime.combine(day, time(16, 0))   # fallback if no collections

  6. Drive times — queried from OSRM at runtime (no hardcoded constants):
       drive_b37_h  = osrm_duration(CB22_DEPOT_ANCHOR, PALLETLINE_HUB_COORDS) * TRUCK_DURATION_FACTOR / 3600
       drive_le10_h = osrm_duration(CB22_DEPOT_ANCHOR, HAZCHEM_HUB_COORDS)    * TRUCK_DURATION_FACTOR / 3600
       # Validated: 2.46h to B37, 2.03h to LE10 (OSRM Jan 2026)

  7. Expected return (next morning):
       B37:  return = depart + drive_b37_h  + TRUNK_B37_DWELL_MIN/60  + drive_b37_h
       LE10: return = depart + drive_le10_h + TRUNK_LE10_DWELL_MIN/60 + drive_le10_h

Output: TrunkPlan dataclass with assignments, departure, return estimates
```

**Drive times**: computed at runtime via OSRM using `PALLETLINE_HUB_COORDS` and
`HAZCHEM_HUB_COORDS` already in `config.py`, multiplied by `TRUCK_DURATION_FACTOR = 1.24`.
Validated values: CB22 → B37 = 2.46h HGV, CB22 → LE10 = 2.03h HGV.
No hardcoded drive-time constants — OSRM is always queried live.

**Trailer capacity**: use `VEHICLE_PROFILES[vid]['capacity_pallets_per_trip']` per tractor.
No global `TRAILER_CAPACITY_PALLETS` constant — capacity varies by vehicle.

**Hub dwell times**: derived from January telematics (overnight visits: arrive 18:00–23:00,
depart 00:00–08:00). Stored in `config.py` as observed medians:
```python
TRUNK_B37_DWELL_MIN:  int = 380   # median 6.3h — B37 overnight sort (Jan 2026 telematics)
TRUNK_LE10_DWELL_MIN: int = 330   # median 5.5h — LE10 overnight sort (Jan 2026 telematics)
```
These are starting values; they can be re-derived by replaying the telematics script.

**Hazchem tractor assignment**: not hardcoded. The trunk planner assigns available
Duxford tractors to LE10 based on whoever has completed their daytime work first.
Historical pattern (X8GNW, X88GNW, Y88RNW dominate LE10 in January) emerges naturally
from the state file — if a tractor slept at LE10 last night it is already `LE10_HUB` in
the state, so continuity is preserved without preference lists.

---

## Rigid VRPTW Changes

Minimal changes to `dispatcher.py`:

1. **Pre-staged orders**: `depot_inventory` items (cross-docked from prior days) are injected into the order pool with `freight_ready = day_start` (already at depot at 06:00). No trunk wait required.

2. **PL_IMPORT freight ready time**: set to `trunk_return_time` from state file (e.g. 04:15). Orders cannot be loaded before the trunk arrives. If no state available (first day), default to 05:00.

3. **Shift budget soft**: already implemented (break rule is a cost penalty, not a feasibility rejection). No new changes needed for multi-day — the soft constraint naturally allows driver swaps within a day.

4. **Scope fix**: `classify_order` updated to include PALLETLINE-no-sub + CB22 vehicle → `FULL_FLEET`.

---

## Day Coordinator

`day_coordinator.py` — the thin orchestration layer.

```python
@dataclass
class DayPlan:
    date: date
    trunk_plan: TrunkPlan
    rigid_routes: DayDispatchOutput
    end_state: DayState

def plan_day(
    day: date,
    prev_state: DayState,        # from state_YYYY-MM-DD.json (D-1)
    qargo_df: pd.DataFrame,      # full Qargo dataset (all months)
    telem_df: pd.DataFrame,      # telematics (backtest only, else None)
    postcode_cache: dict,
    mode: Literal['backtest', 'forward'],
) -> DayPlan:

    # 1. Build order pool
    orders_today    = build_scoped_orders(qargo_df, postcode_cache, day=day)
    orders_tomorrow = build_scoped_orders(qargo_df, postcode_cache, day=day + 1)

    # 2. Classify into pools
    rigid_pool      = [o for o in orders_today if o.flow == 'PL_IMPORT'
                       and o.delivery_date == day]
    rigid_pool     += prev_state.depot_inventory_for_date(day)
    rigid_pool     += [o for o in orders_today if o.flow == 'FULL_FLEET'
                       and o.delivery_date == day]
    rigid_pool     += prev_state.unassigned_carry_forward

    collection_pool = [o for o in orders_today
                       if o.flow in ('FULL_FLEET', 'PL_EXPORT')]

    pl_import_tomorrow = [o for o in orders_tomorrow if o.flow == 'PL_IMPORT']

    # 3. Run rigid VRPTW (collections happen during the day; deliveries too)
    rigid_output = run_day_multi_trip(
        day, rigid_pool + collection_pool,
        mode=mode, telem_df=telem_df,
        freight_ready={o.order_id: prev_state.trunk_return_time('B37')
                       for o in rigid_pool if o.flow == 'PL_IMPORT'},
    )

    # 4. Run trunk planner
    collected_pl_export = [o for o in collection_pool
                           if o.flow == 'PL_EXPORT'
                           and o.order_id in rigid_output.assigned]
    available_tractors  = [v for v in CB22_TRACTORS
                           if prev_state.vehicle_locations.get(v) == 'CB22_DEPOT']
    collection_returns  = _estimate_collection_returns(rigid_output)

    trunk_plan = plan_trunk(
        pl_export_orders=collected_pl_export,
        pl_import_tomorrow=pl_import_tomorrow,
        available_tractors=available_tractors,
        collection_return_times=collection_returns,
    )

    # 5. Compute new end-of-day state
    cross_docked = [o for o in collection_pool
                    if o.flow == 'FULL_FLEET'
                    and o.delivery_date > day
                    and o.order_id in rigid_output.assigned]

    end_state = DayState(
        date=day,
        vehicle_locations={
            **{v: loc for v, loc in prev_state.vehicle_locations.items()
               if v not in trunk_plan.all_tractors},
            **{v: 'B37_HUB'  for v in trunk_plan.b37_tractors},
            **{v: 'LE10_HUB' for v in trunk_plan.le10_tractors},
        },
        depot_inventory=prev_state.depot_inventory_for_future(day) + cross_docked,
        unassigned_carry_forward=rigid_output.unassigned,
        trunk_manifest=trunk_plan.manifest,
    )

    return DayPlan(date=day, trunk_plan=trunk_plan,
                   rigid_routes=rigid_output, end_state=end_state)
```

---

## Multi-Day Backtest Loop

`multiday_backtest.py`

```python
def run_multiday_backtest(
    start_date: date,
    end_date: date,
    qargo_df: pd.DataFrame,
    telem_df: pd.DataFrame,
    postcode_cache: dict,
    state_dir: Path,
    output_dir: Path,
):
    prev_state = DayState.bootstrap_from_telematics(start_date - timedelta(1), telem_df)

    for day in daterange(start_date, end_date):
        plan = plan_day(day, prev_state, qargo_df, telem_df, postcode_cache, mode='backtest')

        # Write state
        state_path = state_dir / f'state_{day}.json'
        plan.end_state.to_json(state_path)

        # Write HTML map (rigid routes + trunk arcs)
        html_path = output_dir / f'plan_replay_{day}.html'
        build_map(day, plan, telem_df, html_path)

        # Print daily report
        actuals = actuals_for_day(day, telem_df, qargo_df)
        print_day_report(day, plan, actuals)

        prev_state = plan.end_state
```

**Bootstrap from telematics** (Day 0, no prior state file): scan telematics for the evening of `start_date - 1`. For each Duxford tractor, find last known location after 16:00:
- postcode starts with `B37` → `B37_HUB`
- postcode starts with `LE10` → `LE10_HUB`
- else → `CB22_DEPOT`

---

## HTML Map Changes

`export_plan_replay.py` — add trunk route arcs:

- **Outbound trunk arc**: CB22 depot → B37 hub (dark purple dashed line, one per tractor)
- **Inbound trunk arc**: B37 hub → CB22 depot (dark purple dotted line, return journey)
- **Hazchem arcs**: CB22 → LE10 3BS (dark red dashed/dotted)
- **Cross-docked orders**: marked on map with a depot icon at CB22 (collected yesterday, delivering today)
- **Legend**: updated to include trunk arc colours and cross-dock marker

Hub coordinates (already in `config.py`):
```python
PALLETLINE_HUB_COORDS = (52.467, -1.787)   # B37 7HB
HAZCHEM_HUB_COORDS    = (52.537, -1.376)   # LE10 3BS
```

---

## New Dataclasses

```python
# multiday_state.py
@dataclass
class DayState:
    date: date
    vehicle_locations: dict[str, Literal['CB22_DEPOT', 'B37_HUB', 'LE10_HUB']]
    depot_inventory: list[ScopedOrder]           # cross-docked, delivery_date > today
    unassigned_carry_forward: list[str]          # order_ids that failed yesterday
    trunk_manifest: dict[str, TrunkHubManifest]  # keyed by hub name

    def trunk_return_time(self, hub: str) -> datetime: ...
    def depot_inventory_for_date(self, d: date) -> list[ScopedOrder]: ...
    def depot_inventory_for_future(self, d: date) -> list[ScopedOrder]: ...
    def to_json(self, path: Path): ...

    @classmethod
    def from_json(cls, path: Path) -> 'DayState': ...

    @classmethod
    def bootstrap_from_telematics(cls, day: date, telem_df: pd.DataFrame) -> 'DayState': ...


@dataclass
class TrunkHubManifest:
    tractors: list[str]
    pallets_outbound: float
    departed: datetime
    expected_return: datetime


# trunk_planner.py
@dataclass
class TrunkPlan:
    b37_tractors: list[str]
    le10_tractors: list[str]
    b37_depart: datetime
    le10_depart: datetime
    b37_expected_return: datetime
    le10_expected_return: datetime
    manifest: dict[str, TrunkHubManifest]

    @property
    def all_tractors(self) -> list[str]:
        return self.b37_tractors + self.le10_tractors
```

---

## Scope Fixes (scope.py)

```python
# classify_order — new branch before the final `return None`:
if import_type_str == 'PALLETLINE' and not sub_str:
    # No subcontractor: treat as FULL_FLEET if a CB22 vehicle was assigned.
    # In forward mode (cb22_fleet_only=False), the geographic filter in
    # in_cambridge_scope() will gate it. In backtest, the vehicle filter gates it.
    return 'FULL_FLEET'
```

This means 8 previously-dropped Palletline-no-sub orders with CB22 vehicles are now `FULL_FLEET` and included in scope.

---

## Config Additions (config.py)

```python
# Hub overnight dwell times — derived from January 2026 telematics.
# Overnight visits only (arrive 18:00–23:00, depart 00:00–08:00), median of observed durations.
# Re-derive by running investigations/derive_trunk_parameters.py on fresh telematics data.
TRUNK_B37_DWELL_MIN:  int = 380   # B37 7HB Palletline — median 6.3h (n=20 visits)
TRUNK_LE10_DWELL_MIN: int = 330   # LE10 3BS Hazchem   — median 5.5h (n=15 visits)

# Loading buffer at CB22 depot before trunk departure (time to load trailers after last collection returns).
TRUNK_LOADING_BUFFER_MIN: int = 30

# Output directory for daily state files.
STATE_DIR: Path = Path(__file__).parent.parent / 'data' / 'Output' / 'cambridge' / 'state'

# Drive times are NOT hardcoded — trunk_planner.py calls OSRM at runtime using
# PALLETLINE_HUB_COORDS and HAZCHEM_HUB_COORDS, multiplied by TRUCK_DURATION_FACTOR.
# Validated: CB22→B37 = 2.46h HGV, CB22→LE10 = 2.03h HGV (OSRM + 1.24 factor).
```

---

## File Summary

| File | Status | Change |
|---|---|---|
| `cambridge/multiday_state.py` | NEW | `DayState`, `TrunkHubManifest`; read/write JSON; bootstrap from telematics |
| `cambridge/trunk_planner.py` | NEW | `TrunkPlan`; size trailers, assign tractors, compute departure/return |
| `cambridge/day_coordinator.py` | NEW | `plan_day()`; orchestrates pool assembly → VRPTW → trunk → state |
| `cambridge/multiday_backtest.py` | NEW | Day-loop; per-day report + HTML map; accumulates multi-day metrics |
| `cambridge/scope.py` | MOD | PALLETLINE-no-sub → FULL_FLEET |
| `cambridge/config.py` | MOD | Add trunk drive times, trailer capacity, loading buffer, hazchem tractors, STATE_DIR |
| `cambridge/dispatcher.py` | MOD | Accept pre-staged (cross-docked) orders with `freight_ready = day_start` |
| `operational_analysis/export_plan_replay.py` | MOD | Add trunk arcs (CB22↔B37, CB22↔LE10) and cross-dock markers to HTML map |

---

## What Is Explicitly Out of Scope

- Bedford, St Ives, Stoke circuits — excluded for now
- Rolling horizon / mid-day re-planning — future phase
- Driver identity tracking (which named driver in which vehicle) — not modelled
- Trailer unit tracking (which physical trailer is attached to which tractor) — not modelled; tractors are treated as tractor+trailer units
- Live operational integration (API push to Qargo or driver app) — future phase (Option B/C output)
