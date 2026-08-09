# Tour-Aware DIRECT-vs-XDOCK Resolution — Design

**Date:** 2026-07-01
**Status:** IMPLEMENTED then **REVERTED** (2026-07-01) — net-negative on validation: the
"delivery tour-only" signal flipped ~74 orders/week to DIRECT (only ~6 actually stranded),
doubling tour km because DIRECT batches worse than consolidated XDOCK. See QUEST_LOG and
the memory note `tour-aware-resolver-reverted`. The correct shape is a targeted post-seed
repair of only-stranded orders (deferred).
**Author:** brainstormed with stakeholder (after a systematic-debugging investigation)

## Problem

The entire remaining in-universe unassigned tail (17 orders across the 12-17 and
19-24 weeks) is **one phase-ordering gap**. Every stranded order is a **multi-day
FULL_FLEET** move that the option resolver settled as **XDOCK**, which then could not
be served:

- The resolver's `_cost_choice`
  ([`freight_planner/options_resolver.py:114`](../../../freight_planner/options_resolver.py))
  picks DIRECT vs XDOCK on a **pure geometric km comparison**
  (`chosen = "XDOCK" if xdock_km <= ratio * direct_km`). This choice is locked in
  **before** tours are formed and vehicles assigned — so it cannot know whether the
  XDOCK legs are actually servable.
- XDOCK splits the order into a `CUSTOMER_PICKUP` (daily phase) + `CUSTOMER_DELIVERY`
  (needs the pickup first). The pickup **strands** as `NO_FEASIBLE_ROUTE`:
  - **far collections (NE42 Newcastle → SE):** the pickup is ~350 km from any depot,
    unreachable in a daily round-trip, and pickups are excluded from the tour path;
  - **Stoke-yard collections (ST4 8JB → London/NW — the big ST shipper):** the pickup
    is *at* the depot (0 km) but needs a Stoke-local vehicle, and Stoke's ~1.5 tractors
    are already reserved for tours; a cross-depot vehicle can't reposition ~200 km for a
    pickup within a day.
- The delivery then cascades to `DELIVERY_BEFORE_PICKUP`.
- **DIRECT would have served every one of them** — one vehicle collects at the origin
  and carries to the destination over the multi-day window, which is exactly what a
  multi-day tour does (confirmed live: `TOUR:TA70WTL:2026-01-16` already runs a
  Newcastle sweep 15-20 km from the stranded NE42 collection).

Root cause: **the DIRECT-vs-XDOCK choice is made blind to tourability/servability.**
Every stranded order is feasible within time, capacity, and flow — only the phase
ordering (resolve → tour → daily) makes it unservable.

## Key scoping insight

The stranding signal is the **delivery** being tour-only, **not** the pickup:

- NE42's pickup is tour-only (far), but ST4's pickup is *at* the depot (0 km, not
  tour-only). Keying the rule on the pickup would miss the ST4 majority (14 of 17).
- Both patterns share one thing: the **XDOCK delivery leg is tour-only** (its
  `stage_depot → dest` round-trip exceeds the daily driving cap, so it needs a
  multi-day tour regardless). That catches ST4 **and** NE42.

## Design

### The rule (in `_cost_choice`)

For a multi-day FULL_FLEET order, after the existing coordinate/stage-depot guards and
**before** the km comparison, add:

```python
# If the delivery already needs a multi-day tour (its stage_depot -> dest round-trip
# can't fit the daily driving cap), XDOCK only orphans a pickup that competes for
# scarce local vehicles / is unreachable daily. DIRECT carries the whole order as one
# tour-able unit instead. (Consolidation is NOT lost: the tour builder batches DIRECT
# moves the same as deliveries.)
if is_tour_only(dest[0], dest[1], depot=stage_depot):
    return "DIRECT", "direct_tour_delivery", 0.0, 0.0
chosen = "XDOCK" if xdock_km <= ratio * direct_km else "DIRECT"
return chosen, "cost", float(direct_km), float(xdock_km)
```

`is_tour_only(lat, lon, *, depot=...)` already exists in
[`freight_planner/tours.py`](../../../freight_planner/tours.py) and uses the local
road model to decide whether a there-and-back-plus-service day trip from `depot`
exceeds the daily driving cap. `options_resolver` importing it is a clean one-way
dependency (tours.py does not import the resolver — no cycle).

### Why this is correct, not a patch

When the delivery already requires a tour, XDOCK's separate depot-collection is pure
fragmentation: it orphans a pickup that either can't be reached daily (NE42) or
competes for a scarce local fleet the tours already reserved (ST4), and the delivery
cascades behind it. DIRECT keeps the order as a single tour-able unit. Consolidation is
preserved because the multi-day tour builder batches DIRECT moves exactly like
deliveries, so several far orders to one region still merge onto one sweep.

### Preserved behaviour (unchanged branches)

- `pre_collected → XDOCK` (freight collected before the window is already at a depot;
  DIRECT is physically impossible).
- `_window_infeasible(delivery) → DIRECT` (existing early return).
- `stage_anchor is None → DIRECT` / `no geocode → XDOCK` (existing guards run first, so
  the tour-only check only runs when `dest` and `stage_depot` are valid).
- **Near-delivery** multi-day FF (delivery not tour-only) still goes through the
  existing km `cost` comparison — the rule only diverts far-delivery orders.

## Data flow after the change

A multi-day FF `ST4 → London` (or `NE42 → SG1`): resolver sees the XDOCK delivery is
tour-only from its stage depot → chooses **DIRECT** → the DIRECT two-point move
(`depot → origin → dest`) is tour-only → it enters the tour path → the tour builder
forms/joins a multi-day sweep that collects at the origin and delivers en route. No
orphaned pickup, no `DELIVERY_BEFORE_PICKUP` cascade.

## Testing

**Unit (`tests/freight_planner/test_options_resolver*`):**
- multi-day FF with a **far** delivery (tour-only from stage depot) → resolver keeps
  the DIRECT leg, drops the XDOCK legs, choice reason `"direct_tour_delivery"`.
- multi-day FF with a **near** delivery (not tour-only) → still the km `cost` choice
  (unchanged).
- **pre-collected** order → still `XDOCK` (rule does not fire).

**Integration:**
- Build legs for a synthetic `ST4 8JB → London` and `NE42 → SG1` multi-day FF; after
  `resolve_options`, only the DIRECT leg survives for each.

**Validation (full runs, 12-17 and 19-24):**
- the `NO_FEASIBLE_ROUTE` + `DELIVERY_BEFORE_PICKUP` in-universe tail clears (→ ~0);
- coverage rises toward ~99.9%; 0 temporal / 0 ledger violations;
- km stays sane (DIRECT carries replace XDOCK round-trips — expect neutral-to-lower
  system km for the far orders, plus the coverage reinvestment for newly-served ones).

## Scope / files

- `freight_planner/options_resolver.py` — add the tour-only delivery check in
  `_cost_choice`; import `is_tour_only` from `freight_planner.tours`.
- Tests: `tests/freight_planner/test_options_resolver*`.
- `freight_planner/QUEST_LOG.md` + memory note.

Out of scope: the broader "prefer DIRECT for all multi-day FF" variant; making the
resolver fully vehicle-state-aware; the daily-vs-tour pickup-eligibility split itself.

## Constraints

- **No `git commit` this session** (standing stakeholder instruction) — write files only.
- Viz regeneration, if any, is `trip_app` (`viz_app.py`) only.
