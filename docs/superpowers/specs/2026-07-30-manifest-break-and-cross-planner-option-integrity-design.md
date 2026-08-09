# Manifest break replay and cross-planner option integrity

## Purpose

Correct two defects exposed by the W0 baseline without changing the intended
forward-mode or multi-day-tour rules:

1. the final manifest must not invent a second statutory break when the route
   evaluator has already placed the required break on an earlier leg; and
2. a DIRECT/XDOCK option set must have one mode decision even when its legs are
   divided between the fixed tour planner and the ordinary daily planner.

## Manifest break replay

`build_route_stops` currently reconstructs return-leg breaks separately for
each trip. This loses the evaluator's vehicle-day driving state and can count a
recorded break without applying its reset to the replayed accumulator.

The manifest will instead replay driving in chronological trip order with one
drive-since-break accumulator per vehicle and service day. The accumulator
will include generated depot-return legs and will carry into the next trip.
For every selected stop, the same statutory-break function used by routing
will advance the accumulator over `planned_drive_minutes`. A break already
recorded on that stop is emitted once; replay is used only to recover the
post-leg accumulator. The generated return receives a break only when replay
shows that a further break becomes due.

This preserves the current geometry-derived return time and distance. It
changes only duplicated break time.

Regression case: an HGV has two trips; cumulative driving causes a 45-minute
break on the outbound leg of trip two, followed by a return that remains below
the next 4.5-hour threshold. The return must carry zero additional break and
its arrival must equal the evaluator's route end.

## Shared DIRECT/XDOCK claim

Tour construction and daily construction currently create independent
`OptionMutex` state after candidates have been divided into `tour_candidates`
and `daily_candidates`. A far DIRECT can therefore enter a tour while the
daily seed independently selects the rival XDOCK pickup.

The multi-day seed coordinator will own one option claim map spanning both
components. Once a feasible tour assignment is accepted, every freight option
represented by its real jobs claims that job's `option_group`. Before the
ordinary daily seed runs, candidates from rival groups are withheld while
candidates from the claimed group remain available. This is important for an
XDOCK delivery placed on a tour: its same-group daily pickup must remain
eligible.

Tour assignments themselves must not contain rival groups from the same
option set. Candidate metadata will provide the mapping from job to
`(option_set, option_group)`, and accepted assignments will be checked against
the coordinator's claims before reservation and emission. A conflicting tour
assignment is rejected rather than deferred to final-output cleanup.

The ordering remains consistent with the current architecture: fixed tours
are formed before the daily seed, so an accepted tour claims the mode. This is
a correctness repair, not a redesign into a joint tour/daily optimiser.

Regression cases:

- a feasible far DIRECT tour blocks its rival daily XDOCK pickup and delivery;
- an XDOCK delivery assigned to a tour retains eligibility for its same-group
  daily pickup;
- no final selected records contain both option groups, and correctness does
  not depend on `drop_superseded_option_legs`.

## Failure handling

Only an accepted feasible tour assignment may claim a mode. A rejected tour
does not claim anything, leaving the daily alternatives available. Existing
ledger checks remain the authority for pickup-before-delivery and freight
location. Final option cleanup remains as a defensive audit backstop, but a
normal seed must reach it with no conflict.

## Verification

Implementation will follow red-green TDD:

1. add and run the manifest multi-trip regression, confirming the duplicate
   return break failure;
2. implement vehicle-day break-state replay and confirm the focused test;
3. add and run the cross-planner DIRECT/XDOCK regressions, confirming both
   modes are currently selected;
4. implement the shared claim and confirm the focused tests;
5. run the affected manifest, tour-plan, seed-option, ledger, utilization and
   dynamic end-to-end test modules;
6. run the complete `tests/freight_planner` suite.

No validation campaign run is part of this change unless requested after the
test suite is green.
