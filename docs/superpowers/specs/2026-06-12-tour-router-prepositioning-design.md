# Idle-Hour Repositioning (Pre-Position & Early Return) — Design

**Date:** 2026-06-12
**Status:** Approved (design); not yet implemented
**Scope:** Phase 1b tour router (`cambridge/week_planner/tour_router.py`) **and**
Phase 2 remote-overnight handling (`cambridge/day_coordinator.py`), via a shared
helper.

## Problem

When a vehicle ends an operating day **away from its home depot** (a multi-day
tour, or a Phase 2 long-haul delivery tractor that stays out overnight), the
planner parks it at its **last stop** for the rest of the day and overnight. It
never uses the day's **remaining drivable hours** to move toward where it needs
to be next. This wastes capacity in two symmetric ways:

1. **No outbound pre-positioning.** A tractor sitting at the depot can't reach a
   far delivery in one shift — not because of raw distance (Cambridge→Kilmarnock
   is ≈640 km road ≈ 8–9h drive, under the 13h shift) but because arriving that
   late misses the delivery's time window (e.g. 18:00). It never drives north on
   the prior day, so the far delivery is never reachable.
2. **No early return / repositioning.** A tractor that finishes its last delivery
   at 13:00 with a 20:00 shift end parks at the delivery point, wasting ~7h of
   driving it could spend heading back toward the depot (freeing next-day
   capacity) or toward the next pending cluster.

Both are the same defect: **the day-end position is set to the last stop (or the
depot), never advanced using the remaining hours.**

### Where it occurs

- **Phase 1b — `route_tour`:** the day-by-day loop only advances the tractor when
  it *assigns a delivery* (overnight = last stop); an empty day leaves it parked.
  Multi-day far-region tours therefore return **0 km, 100% unrouted**.
- **Phase 2 — `day_coordinator`:** routes that don't require depot return set the
  vehicle's overnight to `REMOTE:{last_stop_lat}:{last_stop_lon}`
  ([day_coordinator.py:753-769](../../../cambridge/day_coordinator.py)). Same
  park-at-last-stop waste for long-haul delivery tractors that stay out.

### Verification evidence (Jan 12–14 backtest)

- `route_tour` for HX17CUA SCOTLAND (4 orders) → `0 km, 4 UNROUTED`.
- Isolated solver test: single KA1 delivery from CB22 depot → 0 assigned; same
  order with the tractor starting 10 km away → assigned, 14.5 km. The delivery
  window end (Jan 15 18:00) is the binding constraint, not distance.

## Decisions (settled during brainstorming)

- **Vehicle model:** single tractor out-and-back. The assigned vehicle does the
  whole run itself. We do NOT model the real two-tractor relay (much larger
  change, breaks the one-vehicle abstraction).
- **Positioning rule:** drive toward the chosen target, capped by a legal daily
  **driving** limit (~9h ≈ ~650 km) — not the full 13h on-duty span.
- **Scope:** applies wherever a vehicle ends a day away from base — both Phase 1b
  tours and Phase 2 remote-overnight tractors — via one shared helper.

## Design

### Shared helper (new): `cambridge/repositioning.py`

A single module both phases call, so the behaviour is defined once.

- **`remaining_drive_hours(route_dict, shift_end) -> float`** — remaining hours
  after the last action: from `route_dict['return_time_iso']` (Phase 1b solver
  output) or `shift_start_iso + on_duty_minutes` (Phase 2 output), vs `shift_end`.
  Returns 0 when neither is available or the vehicle is already at shift end.
- **`advance_toward(cur, target, max_km) -> (lat, lon, leg_km)`** — pure
  geometry. Straight-line move from `cur` toward `target`, clamped so it never
  overshoots; returns the new point and the leg distance.
- **`daily_drive_cap_km(remaining_hours) -> float`** —
  `min(remaining_hours, LEGAL_DAILY_DRIVE_HOURS) × trunk_speed`, using the SAME
  speed model the solver uses (`ROAD_DISTANCE_FACTOR` + routing speed) so a
  repositioned point stays solver-consistent.

The **target** is chosen by the caller (this is the only phase-specific part):

- **Phase 1b (`route_tour`):** nearest **pending (unrouted) delivery** if any
  remain; otherwise the **home depot** (early return).
- **Phase 2 (`day_coordinator`):** the **home depot** (early return). Next-day
  orders aren't known at overnight-computation time, so Phase 2 uses the
  depot-return variant only.

### Phase 1b integration

In `route_tour`'s per-day loop, replace the overnight-position logic
([tour_router.py:135-142](../../../cambridge/week_planner/tour_router.py)). At the
end of each **non-final** day, compute `remaining_drive_hours`, pick the target
(nearest pending delivery, else depot), `advance_toward` it within the cap, set
that as the overnight `REMOTE` position, and add the leg km to the tour total.
This fills the pre-sized tour days: an empty early day becomes a trunk-north leg;
a light delivery day ends with a return/repositioning leg. The final day still
runs with `requires_depot_return=True`.

### Phase 2 integration

In `day_coordinator`'s end-of-day `REMOTE` computation
([day_coordinator.py:753-769](../../../cambridge/day_coordinator.py)), for a
vehicle that ends away from base with hours to spare, call the shared helper with
target = home depot. Replace the raw `REMOTE:{last_stop}` with
`REMOTE:{advanced_point}` (closer to the depot), reducing next-day deadhead. When
remaining hours are ~0 (the long-haul delivery consumed the shift), the helper
returns the last stop unchanged — no regression.

## Testing

1. **Unit — `advance_toward`:** far target → moves the full cap; near target →
   lands exactly at target (no overshoot); zero cap → stays put.
2. **Unit — `remaining_drive_hours`:** Phase 1b-style (`return_time_iso`) and
   Phase 2-style (`shift_start_iso` + `on_duty_minutes`) inputs both resolve;
   missing fields → 0.
3. **Integration — `route_tour` on a synthetic far tour:** 3-day Scotland tour,
   one delivery unreachable same-day from depot. Assert non-zero
   `total_planned_km`, the order routed (not in `unrouted_order_ids`), and day-1
   yields a positioning leg with no delivery stop.
4. **Integration — Phase 2 early return:** a remote-overnight vehicle that
   finishes deliveries with spare hours ends overnight **closer to the depot**
   than its last stop; a vehicle with ~0 spare hours is unchanged.
5. **Backtest — window including delivery days:** re-run **Jan 12–16** (not
   12–14) so the Jan 15–16 tour deliveries land in-window. Confirm HX17CUA's tour
   goes from `0 km / 4 UNROUTED` to a real route with orders ASSIGNED, and
   in-universe coverage rises.

## Edge cases & risks

- **Trunk-speed consistency:** positioning km↔hours must use the solver's own
  speed model, or a repositioned start that looks feasible to this code will be
  rejected by the solver. Reuse existing routing constants.
- **Tour-days budget too short for the farthest regions** (e.g. IV32 Elgin ~970
  km): leftover orders stay `unrouted`. Honest; `REGION_TOUR_DAYS` tuning is out
  of scope here.
- **Two far-apart clusters on one tour** (Scotland + Yorkshire): nearest-first
  handles one then moves toward the other; if days run out, remainder unrouted.
- **Map rendering:** positioning/return legs have no delivery stop — confirm the
  combined HTML map renders a start→overnight move (may need a synthetic
  waypoint) so we avoid a straight-line artifact.
- **Phase 2 next-day interaction:** moving a remote vehicle closer to the depot
  changes its next-day start position; verify the next day's dispatch picks it up
  correctly from the advanced `REMOTE` point (existing REMOTE-handling path).
- **Planned-km rises:** these legs add real tractor trunk km (correct — the real
  tractors drive them). Flag alongside the earlier +6,087 km so the km trend
  stays explainable.
- **No regression when no spare hours:** a vehicle at shift end stays at its last
  stop (Phase 2) / makes no positioning move (Phase 1b).

## Out of scope

- Two-tractor relay modelling.
- `REGION_TOUR_DAYS` re-tuning for the farthest regions.
- Phase 2 *toward-next-day-work* repositioning (next-day orders unknown at
  overnight time) — depot-return only for Phase 2.
- Pattern-1 source-data relabelling (collections mistagged as
  `Palletline (import from API)` deliveries — a separate data-quality issue).
