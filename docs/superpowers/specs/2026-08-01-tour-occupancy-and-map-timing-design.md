# Tour occupancy, return-state and map-timing design

## Problem

The rolling planner can assign an ordinary route to a vehicle-day already occupied by a fixed multi-day tour. Midnight construction and micro-insertion exclude reserved tour vehicle-days, but noon warm re-optimisation does not carry the same exclusion into ALNS. Tour-tail rebuilding can also resume from the wrong physical state because customer jobs and synthetic overnight/return stops are indexed as though they were one-to-one. Separately, the timeline map collapses multiple overnight events on one date into one coordinate and reconstructs anchor times from geometry, while the board uses authoritative emitted timestamps.

## Considered approaches

1. **Patch noon exclusion and clamp map times.** Smallest change, but it leaves tour-tail state alignment, mixed-route auditing and incorrect map anchors unresolved.
2. **Preserve the separate tour architecture and make occupancy and physical events authoritative.** Reuse the existing reservation model at every planning action, identify tour cursor state by physical event identity/time, validate emitted vehicle-days, and make map timing consume the same events as the board. This is the selected approach.
3. **Move tours into the ordinary ALNS solution.** This would unify occupancy implicitly, but would reopen delicate tour formation, attachment and multi-day feasibility logic and is outside the required scope.

## Runtime design

### Vehicle-day occupancy

A tour reserves its assigned vehicle for every date from departure through its final return or parking endpoint. The same reserved-key set must be supplied to midnight construction, noon warm re-optimisation and every micro-insertion pass. Warm ALNS must not create or retain an ordinary route on a reserved key.

After each planning action, a hard invariant rejects any `(vehicle_id, service_date)` containing both tour and ordinary work. This is a correctness check, not a soft objective term.

### Tour commitment and tail rebuilding

The commitment frontier applies to physical tour events, including depot departure, customer service, overnight movement and the return leg. Customer-job count is retained only for splitting the mutable job sequence; it is not used as a positional index into the physical stop sequence.

When rebuilding an uncommitted tour tail, the resume cursor is obtained from the latest physical event at or before the frontier. It carries:

- current location;
- daily driving already consumed;
- elapsed duty;
- driving since the last statutory break;
- freight aboard.

The committed physical prefix remains byte-for-byte stable. A newly visible order may be inserted only into the remaining tour suffix and must never create a separate ordinary route on the tour vehicle-day.

After evaluation or tail rebuilding, emitted per-day records are independently checked against the 600-minute driving and 780-minute duty limits. An inconsistent tour is rejected before commitment.

### Timeline map

Tour overnight events remain ordered events rather than a single date-keyed coordinate. The earliest applicable overnight event provides the morning resume anchor; the final event provides the evening park anchor. Both carry their emitted timestamps.

Map geometry determines only the spatial polyline. Movement times come from the same normalized tour-day events used by the board. Missing or negative inferred anchor times are treated as invalid data rather than clamped to midnight.

## Audit design

The vehicle-day audit aggregates all work for a vehicle and date across tour and ordinary route identifiers. It reports a hard mixed-occupancy violation if both kinds are present and computes driving/duty from the combined physical event stream. This prevents separate rows from hiding overlap.

## Tests

Regression coverage must demonstrate:

1. Noon warm re-optimisation cannot use a tour-reserved vehicle-day.
2. Midnight and micro behavior remain unchanged.
3. A tour with a synthetic overnight before a committed customer resumes from the correct physical event and preserves consumed driving.
4. A rebuilt return is split before total daily driving exceeds 600 minutes.
5. Mixed tour/ordinary work is rejected and surfaced by the audit.
6. Two overnight records on one date produce distinct resume and park anchors.
7. Map node times equal the authoritative board/event times and never begin at 00:00 unless the plan actually does.

## Validation

Run targeted unit tests first, then the affected C0 dates: 26 January, 30 January, 3 February, 11 February and 25 February 2026. Acceptance requires zero mixed vehicle-days, zero tour drive/duty violations, no commitment regression, and agreement between board and map movement times.
