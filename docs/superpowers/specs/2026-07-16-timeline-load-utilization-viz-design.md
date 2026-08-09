# Timeline Load-Utilization Viz — Design

**Date:** 2026-07-16
**Status:** Approved (design); implementation pending
**Files:** `freight_planner/viz_timeline_build.py`, `freight_planner/viz_timeline_template.html`,
`freight_planner/viz_timeline_maplogic.cjs` (+ `tests/.../maplogic.test.cjs`)

## Problem / goal

The timeline dashboard shows *when* an order is served but nothing about *how much* it is, and the
map strip gives no sense of how loaded the selected vehicle is through the day. Two additions:

1. **Order tooltip (both board + map modes):** show the order's pallets and weight.
2. **Map strip only:** two step-line charts — pallet-utilization and weight-utilization over the
   day — drawn *behind* the semi-transparent order blocks, so the load visibly rises at pickups and
   falls at deliveries as you read left-to-right against the clock.

## Data (the build must bake what the snapshot stream lacks)

`plan_snapshots.csv` (which drives the strip) carries no load/pallet/kg columns, so
`viz_timeline_build.py` must add:

- **Per order** (`j` dict, [viz_timeline_build.py:439](../../../freight_planner/viz_timeline_build.py)):
  `pallets` and `kg`, joined from `plan_full.csv` (`pallets`, `weight_kg`) by leg/order. Used by the
  tooltip AND the JS load computation.
- **Per vehicle** (vehicle dict, [viz_timeline_build.py:490](../../../freight_planner/viz_timeline_build.py)):
  `capP` and `capKg` (capacity), from the fleet/vehicle data (the same source
  `trip_capacity_utilization.csv` reports per vehicle). Used to turn load into utilization %.

Both are additive JSON fields — absent-data defaults (0) keep older/geometry-less rows harmless.

## Part 1 — Tooltip pallets + weight

`jobTipHTML(j,hit)` ([viz_timeline_template.html:665](../../../freight_planner/viz_timeline_template.html))
is the single tooltip builder called by both the board (line 661) and map (line 952) hover paths.
Add two rows after "scheduled":

```
pallets   <j.pallets>
weight    <j.kg> kg
```

Format kg with a thousands separator; show "—" when the field is 0/absent. One edit → both modes.

## Part 2 — Load-utilization lines (map strip)

### loadProfile (testable, in maplogic.cjs)

Load math lives in `viz_timeline_maplogic.cjs` (the ONE source, inlined at build and Node-tested —
it must stay `.cjs`, the repo root is `type: module`). New pure function:

```
loadProfile(stops, jobs) -> [{ t, p, kg }, ...]   // step points, load AFTER time t, per trip
```

Model (mirrors the solver's running-load in `evaluate_route`/`evaluate_tour`), computed **per trip**
(reset at each depot start/return, keyed by the stop tuple's trip index):

- **Trip start (depot depart):** load = sum of `pallets`/`kg` over the trip's **delivery** stops
  (deliveries ride out of the depot pre-loaded). Emit a point at the depot-depart time.
- **delivery** stop: `load -= job.pallets/kg` — emit at the stop's depart time (steps down).
- **pickup** stop: `load += job.pallets/kg` — emit at depart (steps up; carried to the depot).
- **direct** carry (collect→deliver in one leg): a transient bump — `load += …` at the stop's arrive,
  `load -= …` at its depart — so the line rises for the block's width then returns.

Returns the step points for pallets and kg together. Utilization is `load / capacity`, clamped to
`[0, 1]`, computed at draw time (keeps the function capacity-agnostic and easy to test).

### drawStrip rendering

In `drawStrip` ([viz_timeline_template.html:865](../../../freight_planner/viz_timeline_template.html)),
for the focused vehicle, **before** the block-drawing loop (so the `alpha 0.3–0.5` blocks overlay the
lines):

- Map time→x with the existing `sx(t)`; map utilization→y across the block band
  (`by … by+bh`), 0% at the bottom, 100% at the top: `y(u) = by + bh*(1-clamp(u,0,1))`.
- Draw two **step polylines** on the shared 0–100% scale: **pallet-utilization (teal)** and
  **weight-utilization (amber)**, ~1.5 px, ~0.9 opacity.
- A faint dashed line at 100% (`y(1)`) = the "full" ceiling, and tiny `0 / 50 / 100%` ticks at the
  left gutter.
- The existing master-clock frontier band already marks "now," so the lines are a static day-profile
  read against the clock (no animation).

### Colors (theme-aware)

Add two palette entries — `--util-pal` (teal) and `--util-wt` (amber) — with light and dark values
(following the template's existing `:root` / dark-media / `data-theme` token pattern), surfaced into
the JS `P` palette next to the other `g("--…")` reads. Exact hues finalised against a Playwright
screenshot in both themes.

## Testing

- **Node** (`maplogic.test.cjs`): `loadProfile` on a hand-built route — deliveries + a pickup + a
  direct across one trip — asserts: initial load = sum of deliveries; steps down per delivery; steps
  up per pickup; the direct is a transient bump; multi-trip resets at the depot. Runs with the
  existing `.cjs` node test harness.
- **Playwright:** screenshot map mode with a loaded vehicle selected, in light and dark, confirming
  the two lines read clearly *through* the transparent blocks and the tooltip shows pallets/weight.
- **Regression:** the board mode and the existing map route/puck are unchanged; `--no-geometry`
  board still builds.

## Out of scope

- Board mode gets the tooltip fields but **not** the lines (map-mode, single-selected-vehicle only).
- No animation of the lines; no dual axis (both are % on one scale).
- Directs are modelled as a transient bump, not a separately-drawn origin sub-point.

## Success criteria

- Tooltip shows pallets + weight in both modes.
- Map strip shows two utilization step-lines behind the transparent blocks, rising at pickups and
  falling at deliveries, on a 0–100% scale with a 100% ceiling, legible in both themes.
- `loadProfile` Node tests green; Playwright screenshots confirm; board mode unchanged.
