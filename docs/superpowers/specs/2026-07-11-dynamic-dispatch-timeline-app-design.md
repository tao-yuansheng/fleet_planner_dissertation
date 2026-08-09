# Dynamic-Dispatch Timeline App — Design

**Date:** 2026-07-11
**Status:** approved (inline), building v1

## Purpose

Let the user and stakeholders *see* the rolling-horizon dispatch plan evolve through a
single day — making concrete the mechanism that is otherwise only in the code and the logs:
decision epochs, the commit frontier, and above all the distinction between an **internal
insertion** to the plan and a **real commitment to a driver**.

Audience: the user (understanding) and stakeholders / thesis demonstration. Not a live
operational dispatcher view.

## Scope

- **One real day: 2026-01-12** from `runs_arr_full` (541 stops, 38 vehicles, 44 trips, 2
  anchors + 9 micro passes). Multi-day navigation and a geographic map are explicitly OUT of
  scope for v1.
- Self-contained interactive HTML (a claude.ai Artifact): no server, no external calls; the
  day's data is embedded as inline JSON.

## Data sources (all from the run)

- `plan/route_stops.csv` — per-stop vehicle, trip_index, sequence, stop_type,
  planned_arrive/depart, order_id, service_pc.
- `reports/stop_provenance.csv` — per collection stop: `first_epoch`, `first_kind`
  (seed / warm / micro) = the epoch that first placed it.
- `qargo` parquet — `timestamp_created` = when each order was booked.
- `plan/rolling_manifest.json` + `reports/micro_passes.csv` — the decision epochs.

A small Python preprocessor (`freight_planner/viz_timeline_build.py`) joins these into one
compact JSON: `{ meta, epochs[], vehicles[ {id, lanes/trips[ {stops[ {…} ] } ] } ], orders{…} }`,
embedded into the HTML at build time.

## The state model (what scrubbing shows)

The clock scrubs 03:00→18:00. Each stop is styled by its state **at the current clock T**,
derived from three timestamps it carries: `booked` (order creation), `placed` (its placement
epoch — seed/warm/micro), and `arrive`/`depart` (scheduled). The commit frontier sits at
`T + delta` (delta = 60 min, the dispatch lead-time).

| State | Condition at clock T | Visual |
|---|---|---|
| Not yet booked | T < booked | invisible |
| Booked, unplaced | booked ≤ T < placed | faint ghost at its slot |
| **Internal insertion (tentative)** | placed ≤ T and `T + delta < arrive` (right of the frontier band) | **hollow / dashed** block |
| **Driver commitment (firm)** | `T + delta ≥ arrive` and T < depart (stop has entered the frontier band) | **solid** block |
| Done | T ≥ depart | dimmed solid |

Commit rule (uniform, per-stop): a stop commits — the driver is told — when the clock is
within the dispatch lead-time of the vehicle arriving, `commit = arrive − delta`, clamped so
it can never precede `placed`. This is exactly the frontier band sweeping in from the right:
a block crossing into the `now → now+delta` band is the commitment moment. It captures both
the lead-time and the watermark idea (stops firm up as the vehicle rolls toward them, while
the still-distant tail stays editable).

**Insertion ≠ commitment is first-class:**
- The tentative→firm crossing fires a **"→ dispatched to driver"** pulse on the block and
  increments a running **driver-notifications** counter — the moment of real commitment,
  visibly separate from the earlier internal insertion.
- The hover tooltip shows the full lifecycle: `booked HH:MM · internally inserted HH:MM
  (kind) · committed to driver HH:MM`.
- **Micro-inserted stops are visually distinct** (accent colour) — the afternoon arrivals
  that make the dynamic point.

Deliveries and seed-placed collections have `placed = 03:00` (the seed); only collection
stops carry a real per-epoch `placed` from provenance. Stops with no provenance fall back to
`placed = booked` (best effort). This is a **reconstruction** from the final plan +
provenance (faithful to *when things were known and decided*), NOT a frame-by-frame replay of
solver state — labelled as such in the UI.

## Layout (top to bottom)

1. **Time axis** 03:00→18:00 with labelled epoch markers (seed / micros / re-opt / close);
   the current epoch highlighted.
2. **`now` line** (draggable) + shaded **commit-frontier band** (now → now+60).
3. **38 vehicle lanes** (compact rows), stops as blocks at their scheduled x-position,
   coloured by type (pickup / delivery / direct / depot) and styled by state. A one-click
   filter: "only trucks that got intraday inserts."
4. **Live summary panel**: orders known / booked-so-far / internally placed / committed to
   drivers; inserted this hour; driver notifications so far.
5. **Legend + play/pause** (animate the day).

## Interactions

- Drag the scrubber or play/pause to advance the clock.
- Hover a stop → lifecycle tooltip.
- Toggle the intraday-inserts filter.
- Theme-aware (light/dark), responsive, horizontal content scrolls inside its own container.

## Build approach

1. `viz_timeline_build.py` — pure preprocessor: CSVs + qargo → compact JSON (unit-checkable:
   row counts, state-timestamp completeness).
2. The HTML artifact — inline CSS/JS, embeds the JSON, renders the Gantt + scrubber.
3. Build v1, publish as an Artifact, iterate on visuals with the user.

Because this is a single self-contained artifact iterated visually (not a multi-file code
subsystem), the build skips the heavy multi-task plan / subagent ceremony — the design above
is the contract, and v1 is the review surface.

## Out of scope (v1)

Geographic map view; multi-day navigation; live/operational data feed; per-epoch solver
snapshots (would need new emit instrumentation); driver-acceptance / comms-failure modelling.
