# Strict Planning-Window Handover Design

## Purpose

Treat the requested planning window as the decision boundary for ordinary
vehicle-days. Freight collected within the window for a later delivery must
remain staged in `handover.json`, so the next chained window—not the current
one—decides its delivery route.

## Required behaviour

- Keep the full order and its paired movement legs in the planning universe.
  This preserves the freight ledger and the link between the in-window
  collection and later delivery.
- Ordinary daily candidates with a `service_date` after the requested window
  end are not offered to the seed or ALNS.
- An in-window collection whose delivery falls after the boundary may still be
  selected. At close, its final freight state is `AT_DEPOT`, so
  `build_handover` emits it in `staged_freight` and not in
  `delivered_order_ids`.
- The next chained window consumes that staged-freight record and may plan the
  depot delivery once its service date is inside that window.
- A genuine multi-day tour that physically starts within the window may emit
  later tour stops and vehicle availability beyond the boundary. This is an
  in-flight movement, not an ordinary future vehicle-day.

## Implementation boundary

Add a maximum service-day clamp alongside the existing minimum service-day
clamp in the window-input builder. The rolling dispatcher supplies the
requested window end as that maximum. The clamp applies to candidate legs
before compatibility, seeding, or ALNS; it does not remove demand metadata or
rewrite the date-basis universe.

The static runner is unchanged unless it explicitly supplies a maximum day.
Existing tour construction remains unchanged: a tour candidate beginning
within the window is evaluated and emitted by the tour machinery, whose
multi-day stop dates may cross the boundary.

## Handover semantics

`build_handover` continues to derive state from the selected plan:

- `DELIVERED` orders enter `delivered_order_ids`;
- orders ending `AT_DEPOT` enter `staged_freight`;
- only actual post-window in-flight tour work should create future vehicle
  availability.

Preventing future ordinary legs upstream removes the false `DELIVERED` state
and false future vehicle availability without duplicating routing logic inside
handover generation.

## Verification

Regression coverage must demonstrate:

1. the maximum-day clamp removes a future ordinary candidate while retaining
   in-window candidates;
2. a collected order with a later delivery ends staged, not delivered;
3. a chained next window can initialise that order from staged freight;
4. a tour beginning within the window can still carry an emitted tail beyond
   the boundary; and
5. ordinary selected output contains no vehicle-day after the requested end.

The full freight-planner test suite must remain green.
