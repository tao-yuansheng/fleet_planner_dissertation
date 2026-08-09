# Fixed Vehicle-Type Service Dwell

Date: 2026-07-29

## Decision

Replace the load-dependent customer service-time rule with one fixed duration
per distinct customer visit:

- van: 15 minutes;
- rigid: 15 minutes;
- tractor: 30 minutes.

The duration is working time and therefore contributes to elapsed duty, but it
does not contribute to driving time or satisfy a statutory driving break.

## Evidence

The existing tractor rule, `10 + 6 * pallets` minutes, was inherited rather
than estimated for this fleet. It assigns 166 minutes to a 26-pallet visit.
After consolidating the January-February order records into distinct
vehicle/site/timestamp visits, the observed means are approximately 15 minutes
for rigids and 30 minutes for tractors. Pallet count explains only about 4-6%
of observed visit-duration variation. There is not enough verified van data for
an independent estimate, so vans use the rigid value as the closest operational
fallback.

The fixed values represent expected baseline operation. Robustness to slower
operation remains tested through the campaign's travel-speed slack scenarios;
the baseline does not additionally impose a 90th-percentile dwell at every
stop.

## Behaviour

`service_minutes(pallets, vehicle_type)` remains the common public primitive so
all existing route, tour and compatibility consumers stay aligned. Its
`pallets` argument remains accepted for API compatibility but no longer changes
the result.

Same-address consolidation remains enabled. A contiguous group of jobs at the
same coordinates pays one fixed visit duration, not one duration per order.
This requires the existing merge logic to subtract the complete fixed duration
from every additional co-located job.

A two-point direct movement still represents two physical customer visits and
therefore pays two fixed durations: one at collection and one at delivery.
When the movement is split overnight, the collection duration is attributed to
the collection day and the delivery duration to the delivery day.

Depot reload, trunk-hub dwell and other explicitly scheduled non-customer dwell
parameters are unchanged.

## Configuration and documentation

Define the three fixed values in the shared configuration and remove the
obsolete per-pallet service constants and functions from live use. Update the
pipeline documentation and dissertation methodology formula source so they
describe fixed vehicle-type visit dwell and its empirical basis.

## Verification

Tests must establish:

1. pallet count does not change service duration within a vehicle type;
2. van and rigid visits are 15 minutes and tractor visits are 30 minutes;
3. co-located orders pay one visit duration;
4. two-point direct movements pay two visit durations;
5. the maintained test suite stays green;
6. a 19-20 February validation has zero hard, temporal, non-anticipation,
   backdating and option-conflict violations.
