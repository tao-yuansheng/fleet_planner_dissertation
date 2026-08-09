# Strict Planning-Window Handover Implementation Plan

## Goal

Stop ordinary daily routing at the requested end date and carry collected,
not-yet-delivered freight into the next planning window through
`handover.json`, while preserving genuine multi-day tour tails.

## Tasks

1. Add failing unit tests for an upper service-day candidate clamp. Ordinary
   future legs have no exemption; genuine in-flight multi-day work is emitted
   by the tour path rather than offered as a daily candidate.
2. Add a rolling regression in which a collection is inside the window and its
   depot delivery is outside it. Assert that the future ordinary delivery is
   not selected and the order is staged in handover.
3. Add or retain a chained-handover assertion showing that the next window
   receives the staged depot and ready time.
4. Implement `_clamp_future_candidates` in `run_alns.py`, parallel to the
   existing past-day clamp, and invoke it from `build_window_inputs`.
5. Pass `max_service_day=end.isoformat()` from the rolling dispatcher.
6. Confirm that tour emission is unaffected and that only actual post-window
   selected tour rows can create vehicle-availability handover.
7. Run focused tests, then the full freight-planner suite excluding the known
   optional `folium` map dependency if it remains unavailable.
