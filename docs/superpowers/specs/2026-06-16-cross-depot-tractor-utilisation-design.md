# Cross-Depot Tractor Utilisation Design

**Date:** 2026-06-16
**Status:** Approved (design); pending implementation plan
**Scope:** `cambridge/week_planner/capacity_allocator.py`, `cambridge/day_coordinator.py` (+ supporting helpers/tests)

> Local/uncommitted — `e:\BEAT` is not a git repo and changes stay local per standing instruction.

---

## Problem

On peak days the planner leaves capable tractors idle while in-scope orders go
undelivered. Measured on the Jan 12–16 2026 three-depot backtest:

- Fleet roster is **65 vehicles**; the plan deploys only **48–57/day** (8–17 idle).
- On **Jan 15** (peak): 12 idle vehicles, of which **6 are CB22 tractors** — and
  5 of those (`R88GNW, W88GNW, X88GNW, Y88RNW, Y90RNW`) start the day parked **at
  CB22 depot**, given **zero** stops, while **34 orders** are dropped as
  `CAPACITY_OVERFLOW`.
- Utilisation split: rigids run at ~180% of single-load capacity (multi-trip,
  saturated); **tractors run at ~60% pallet fill, 43% of tractor-days under 50%**.
  The spare capacity is in tractors.
- The dropped Jan-15 work is **31 OVERFLOW-tagged + 3 CB22**. The London cluster
  (NW/W/WC1B/HA/HP) is OVERFLOW and routes by proximity to **Bedford** (closer),
  which was nearly fully deployed — while the idle tractors sat at **CB22**.

**Root cause:** dispatch runs **per-depot**. OVERFLOW work piles onto the depot
nearest the destination regardless of whether that depot still has tractor
capacity, and there is no cross-depot mechanism to bring another depot's idle
tractors to reachable, otherwise-dropped freight. The result is idle tractors at
one depot coexisting with `CAPACITY_OVERFLOW` misses fed to a saturated depot.

### Why it must be layered (the freight-location constraint)

The fix is shaped by **where the freight physically is**:

- **Depot-tied freight** — PL_IMPORT trunked into a home depot overnight, and
  FF_XDOCK freight cross-docked at a depot. A CB22 tractor cannot grab Bedford's
  trunked freight on the day. The only clean fix is to **decide at plan/trunk time**
  which depot the freight goes to, so freight and an available tractor co-locate.
- **Field-collectible freight** — FULL_FLEET / FF_DIRECT collected from a shipper,
  and PL_EXPORT pickups. Any tractor can collect these in the field regardless of
  home depot, so a same-day cross-depot sweep is physically valid.

These are two physically different freight types, so the design has two layers
that are **not redundant**: Layer 1 fixes depot-tied freight at allocation time;
Layer 2 mops up field-collectible leftovers at dispatch time.

### Explicit non-goals / recorded decisions

- **No subcontract valve.** Earlier framing (price misses as subcontract cost /
  hand to Palletline) is withdrawn at the user's direction. Remove that framing
  from memory and the README.
- **Long-haul singletons are a Phase 1 problem, recorded for later.** Orders too
  far for any idle tractor to day-trip (e.g. Belfast BT31, Yorkshire YO12,
  Newcastle NE6, Wales SA19, Scotland) are **out of scope for this fix**. They are
  a Phase 1 weekly tour-coverage gap and must remain visible as unassigned, not
  absorbed by an uneconomic lone-artic marathon and not subcontracted.

---

## Layer 1 — Vehicle-type-aware OVERFLOW balancing

**File:** `cambridge/week_planner/capacity_allocator.py` (`_assign_overflow_local`)

**Today:** capacity is sized as `depot_vehicles[dep] × _OVERFLOW_TARGET_STOPS (7.0)`
— a single, **vehicle-type-blind** stop pool. Each OVERFLOW order is assigned to
the nearest depot that still has stop-headroom, else the farther depot. It cannot
see "Bedford's *tractors* are full but CB22's are idle."

**Change:** split headroom into two pools per depot:

- **rigid-stop headroom** — `rigid_count[dep] × _OVERFLOW_TARGET_STOPS` (today's
  behaviour, but counting rigids only).
- **tractor headroom** — `tractor_count[dep] × _OVERFLOW_TARGET_TRACTOR_STOPS`
  (a tractor-specific target; tractors do fewer, longer stops than rigids).

Classify each OVERFLOW order as **tractor-needing** vs **rigid-serviceable** using
the existing distance bands already used by the dispatcher (rigid catchment
`CATCHMENT_RADIUS_KM`; beyond it the order needs a tractor). Assignment rule:

- **Rigid-serviceable** order → nearest depot with rigid-stop headroom (else
  farther), exactly as today.
- **Tractor-needing** order → depot with **tractor headroom**, preferring the
  nearest such depot; only fall back to a depot without tractor headroom when no
  candidate has any. This tilts tractor work toward the depot that will have an
  available tractor, so the freight is trunked there and co-locates with the
  vehicle that delivers it.

**Interfaces touched:** `_assign_overflow_local` gains the per-type vehicle counts
(it already receives `depot_vehicles`; extend the caller in `allocate_local_capacity`
to pass rigid/tractor counts separately from the `fleet` roster it already holds).
No change to `DepotDayBudget.local_order_pool` shape — assignment output stays
`dict[depot_id -> list[order_id]]`.

**New constant:** `_OVERFLOW_TARGET_TRACTOR_STOPS` (tractor stop target; start ~3,
matching observed tractor median stops/day, tune in backtest).

---

## Layer 2 — Widen the existing cross-depot field-collectible sweep

**File:** `cambridge/day_coordinator.py` (the existing Pass 2, ~lines 581–653)

**Today:** after all per-depot `run_day_multi_trip` calls, `plan_day` runs a
cross-depot "Pass 2" that offers still-unassigned **field-collectible** orders
(`not _needs_depot_load(o)`) to **REMOTE** tractors (those overnight away from
base), gated by `_MAX_REMOTE_REACH_KM = 200`.

**Change:** widen *who* feeds that pass. In addition to REMOTE tractors, include
**idle home-depot tractors** fleet-wide:

- A tractor is eligible if it is **idle** — at a depot location (per
  `vehicle_locations`, not `REMOTE:`/hub) and assigned **zero stops** by any
  depot's `run_day_multi_trip`. An idle tractor trivially has its full shift
  budget, so no per-vehicle consumed-hours bookkeeping is needed at `plan_day`
  level (that figure is internal to each per-depot dispatch and not exposed here).
- It must not be committed to a tour or the nightly trunk that day (both are
  observable in `plan_day`: tour vehicles via the tour plans, trunk via the
  trunk manifest).
- Its start location is its current depot anchor (from `vehicle_locations`).
- *Partially-used* tractors (some stops, residual budget) are deliberately out of
  scope for this fix — their remaining budget isn't exposed at `plan_day` level,
  and the idle tractors are the measured problem. Revisit only if backtest shows
  residual need.
- Keep the existing `_needs_depot_load` filter so only field-collectible orders
  are offered — this preserves freight-location correctness (depot-tied freight is
  never cross-served here; Layer 1 already handled it at allocation time).
- Apply a **feasible day-trip reachability cap** analogous to
  `_MAX_REMOTE_REACH_KM`: only offer an idle tractor orders within a round-trip it
  can complete in its remaining shift. Driver-hours feasibility is ultimately
  enforced by the VRPTW engine (`feasible()` rejects routes that overrun
  `shift_end`); the cap is a pre-filter to avoid offering obviously-infeasible
  far singletons.

**Effect:** an idle CB22 tractor can now legally take a reachable, field-collectible,
otherwise-dropped order in Bedford/London/Midlands territory. Long-haul singletons
beyond the cap stay unassigned and are recorded as a Phase 1 tour-coverage gap.

**Interfaces touched:** the Pass-2 tractor-collection block in `plan_day`. Reuse
the existing `run_day_multi_trip` invocation; only the set of tractor routes fed in
and the reachability pre-filter change. No change to `DayDispatchOutput`.

---

## Data flow

```
PHASE 1 (weekly)
  allocate_local_capacity()
    └─ _assign_overflow_local()      # Layer 1: rigid vs tractor headroom split
         → DepotDayBudget.local_order_pool (per depot)   # freight trunked to chosen depot

PHASE 2 (per day)  plan_day()
  for depot in (CB22, BEDFORD, ST_IVES):
      run_day_multi_trip(depot)      # per-depot dispatch (unchanged)
  merge unassigned across depots
  Pass 2 — cross-depot field-collectible sweep          # Layer 2
      feed: REMOTE tractors  +  idle home-depot tractors (NEW)
      filter: not _needs_depot_load(o)  AND  within day-trip reach
      run_day_multi_trip(swept tractors, remaining field-collectible orders)
  residual unassigned → recorded; long-haul singletons flagged Phase-1 gap
```

## Error handling / edge cases

- **No idle tractors / no field-collectible leftovers** → Pass 2 is a no-op (as
  today when there are no REMOTE tractors).
- **Tractor already on tour/trunk** → excluded by the shift-budget / commitment
  check; never double-booked.
- **Layer 1 tractor headroom estimate wrong** → Layer 2 is the safety net that
  still catches field-collectible residual; depot-tied residual remains a genuine
  miss (correctly, since freight location can't be undone same-day).
- **Long-haul singleton** → intentionally left unassigned, recorded as Phase 1 gap.

## Testing

**Unit — Layer 1 (`tests/cambridge/test_overflow_balancing.py`)**
- Two depots: nearest depot (Bedford) has 0 tractor headroom, farther (CB22) has
  tractor headroom; a tractor-needing OVERFLOW order is assigned to **CB22**.
- A rigid-serviceable local OVERFLOW order still goes to the nearest depot with
  rigid headroom (no regression).

**Unit — Layer 2 (`tests/cambridge/test_cross_depot_sweep.py`)**
- An idle home-depot tractor (full shift budget, at depot) is fed into the
  cross-depot pass and picks up a reachable, field-collectible, unassigned order
  beyond rigid reach.
- A depot-tied (`_needs_depot_load`) order is **not** offered to a cross-depot
  tractor.
- An order beyond the day-trip reach cap is **not** offered (stays unassigned).

**Integration — Jan 12–16 backtest**
- Idle-tractor count on Jan 14/15 drops; `CAPACITY_OVERFLOW` count falls.
- No new planned lateness (`feasible()` still gates shifts).
- In-universe coverage rises; remaining misses concentrate in long-haul singletons
  (Phase 1 gap) and genuine depot-tied saturation.

## Success criteria

1. No idle tractor coexists with a reachable, field-collectible, unassigned order
   on the same day (Layer 2 guarantee).
2. Tractor-needing OVERFLOW is allocated to a depot with tractor headroom when one
   exists (Layer 1).
3. Jan 12–16 peak-day idle-tractor count and `CAPACITY_OVERFLOW` both fall, with no
   new lateness and no regression in served local orders.
4. Long-haul singletons remain visible as unassigned and are labelled a Phase 1
   tour-coverage gap (not subcontracted, not absorbed by lone-artic marathons).
