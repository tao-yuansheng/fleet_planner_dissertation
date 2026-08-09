# Per-Epoch Plan Snapshots — Design

**Date:** 2026-07-12
**Status:** approved (implement Part A now; Part B next pass)

## Problem

The rolling dispatcher keeps ONE live plan (`current_sol`) in memory, re-optimises
it at each epoch (03:00 seed, 12:00 warm re-opt, hourly micros), and writes **only
the final plan** to disk (`emit_outputs`, once). The intermediate plans — the exact
instructions that, in a live product, would have been **sent to the drivers at each
epoch** — are discarded.

Two consequences:
- **Product:** an optimiser that dispatches to drivers must persist each epoch's
  plan (you can't re-derive "what the 09:00 driver was told" from the final plan).
- **Visualisation / map:** the board reconstructs from the final geometry, so the
  3am plan shows holes where later inserts will land, and a map has no real route
  to move a vehicle along at time T. (See the timeline diagnosis, 2026-07-12.)

The churn report already proves the plan changes materially each epoch (noon re-opt
moves 46–67% of uncommitted jobs). Every one of those versions is real dispatch.

## Fix

Persist a **snapshot of the live plan at every decision epoch**. The dispatcher
already fires `_track(current_sol, epoch, kind, floor)` after each solve (seed,
warm, micro) — the same place, add snapshot capture.

### Part A — run persists snapshots (this pass)

New module-level pure function in `run_rolling.py`:

```
plan_snapshot_rows(sol, epoch_iso, kind, order_of_job, timings_fn, view=None)
    -> list[dict]
```

For each `(vehicle, day)` in `sol` it calls `timings_fn(key, trips)` (a thin
wrapper over the existing `stop_timings`, which returns `(dep0, per_trip
[(arrive, depart)…])`) and emits one row per stop:

`epoch, epoch_kind, vehicle_id, service_date, trip_index, sequence, job_id,
order_id, leg_kind, planned_arrive, planned_depart, committed`

- `committed` = `sequence < committed_count_for_trip` from the watermark `view`
  (0 when no view) — the stops already locked to the driver at that epoch.
- `timings_fn` is injected so the function is unit-testable without the solver;
  the closure passes `lambda key, tt: stop_timings(vrows0[key[0]], key, tt,
  override=_commit_ctx(key))`.

Wired at BOTH `_track` sites (anchor solve + micro insert) and appended to a run
list; written once at the end to `reports/plan_snapshots.csv`. This is faithful to
the accepted plan because it reuses the SAME `stop_timings` (same override) the
dispatcher advances watermarks with.

Cost: one `evaluate_day` per (vehicle, day) per epoch (~13 epochs × ~50 vehicles);
negligible against the ALNS.

### Part B — viz consumes snapshots (next pass, separate)

**Data (compact, to stay artifact-sized).** `viz_timeline_build.py` emits, per DAY:
a `jobs` lookup (`job_id -> {pc, ty, lat, lon, nm, order}` — the STATIC attrs, joined
once from the final `route_stops`), and per vehicle a per-epoch array of
`[jobIdx, arrive, depart, committed]` for its stops at that epoch. Only those four
change across epochs; static attrs are stored once. (Full per-epoch objects would be
~20× today's 125 KB/day — too big; the index+array form keeps it ~150 KB/day.)

**Rendering.** `timeline_v2.html` renders the snapshot valid at clock T (the last
epoch ≤ T) instead of the final plan with visibility gating — so the seed plan shows
CONTINUOUS, the noon re-opt visibly reshuffles the uncommitted tail, and committed
stops persist / only-delay across snapshots (guaranteed by the commit lock). This is
the substrate the map is built on.

**Multi-day (user rule 2026-07-12).** The window spans several days; each day is its
OWN Gantt view. The JSON carries `days: [dayData, ...]`; the page shows left/right
buttons at the top with the current date between them — paging back and forth like
turning pages, one day rendered at a time.

## Non-goals

- Changing the dispatcher's decisions — snapshots are read-only observation.
- The map itself (Part B unblocks it; the map is its own project).

## Tests (Part A, TDD)

1. `plan_snapshot_rows` with a stub `timings_fn`: a 2-vehicle solution → correct
   rows (epoch/kind/vid/seq/job_id/times); `committed` reflects the `view` counts.
2. Empty solution → no rows.
3. Multi-trip vehicle → `trip_index` increments; sequence resets per trip.
4. Integration: a short rolling run writes `reports/plan_snapshots.csv` with >1
   distinct `epoch` and the seed epoch present.
