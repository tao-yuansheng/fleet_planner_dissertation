# Dynamic map dashboard — design of record (2026-07-14)

**Goal.** Add a real, road-accurate map view to the evolving-plan dashboard so a person can
*see* the rolling-horizon plan on a map: click a vehicle, watch its route form and re-form
across decision epochs, watch a simulated truck move along it as the day-clock advances, and
compare the internally-planned route against what was committed to the driver. Primary use:
spotting abnormal assignments (a route sprawling across the map, two trucks overlapping a
dock, a tentative tail that never should have been planned).

**Non-goals.** No live OSRM/tile calls from the browser (geometry is baked at build). No
actual-telematics overlay in v1 (planned-position simulation only). No new plan/solver
behavior — this is read-only visualization built from artifacts the run already emits.

## Locked decisions (from brainstorming)

1. **Target = local HTML file + online tiles.** Opens from the run root (`timeline.html`) in
   a browser with internet. Full Leaflet + OpenStreetMap tiles allowed; no claude.ai-Artifact
   CSP constraints. (A degraded self-contained Artifact build is explicitly out of scope.)
2. **Vehicle simulator = planned position only.** A marker interpolates along the planned
   road route by `planned_arrive`/`planned_depart`. No telematics dependency.
3. **Architecture = extend the existing board (Approach A).** The map lives in
   `viz_timeline_template.html` and is built by `viz_timeline_build`; one file, one build,
   auto-emitted per run (the `timeline.html` root deliverable). No separate map app.
4. **One master clock** = wall-clock-of-day. Scrubbing it both selects the plan *valid at t*
   (latest epoch ≤ t) and moves the truck marker to its planned position at t. Not two sliders.
5. **Encoding.** Hue = vehicle identity; opacity + solid/dashed = commitment state (committed
   solid/bright, internal-tail dashed/dim — reusing the board's 90-min-frontier palette);
   marker shape + number = leg type + sequence (pickup circle, delivery square, direct
   diamond, trunk ⇅). Dark theme, fonts, teal trunk styling carried over from the board.
6. **Focus model.** Click a vehicle → map focuses that vehicle; an optional "show others
   faintly" toggle greys the rest of the fleet underneath for overlap-spotting.

## Architecture

The board today is a Canvas gantt in `viz_timeline_template.html`, fed by a JSON payload
built by `viz_timeline_build.build(run_dir)` and embedded via `write_dashboard`. This design
adds a build-time geometry layer and a Leaflet map layer; the gantt is unchanged except that
it (a) becomes clickable per lane and (b) collapses to a bottom strip when the map is open.

### Component boundaries (files)

| file | change | responsibility |
|---|---|---|
| `freight_planner/viz_geometry.py` (NEW) | create | Build-time OSRM leg-geometry baking + disk cache. Pure Python, no viz coupling. |
| `freight_planner/viz_timeline_build.py` | extend | Call `viz_geometry` for the run's stop-pairs; add `geom` + `depots` to the JSON payload. `--no-geometry` flag. |
| `freight_planner/viz_timeline_template.html` | extend | Add the Leaflet map pane, click-to-open overlay, master clock, simulator, internal/committed toggle. Reuse `viz_app.py`'s Leaflet marker/popup/selection patterns (ported inline). |
| `freight_planner/shared/routing.py` | reuse | `get_route_geometry` (already exists) is the OSRM road-polyline source. |
| `freight_planner/shared/config.py` | reuse | `DEPOT_ANCHORS` → the `depots` payload. |
| `tests/freight_planner/test_viz_geometry.py` (NEW) | create | TDD for the baking module (dedup, cache keying, fallback, JSON shape). |

`viz_geometry.py` is deliberately standalone so it is unit-testable without any HTML/JS and
so a geometry failure is isolated from the board.

### Build-time data flow

```
run_dir → viz_timeline_build.build()
  existing: route_stops + plan_snapshots → days[].jobs[{lat,lon,ty,…}] + vehicles[].snaps
  NEW: collect every consecutive stop-pair across all snapshots (+ depot connectors)
       → viz_geometry.bake(pairs) → {coordkey: [[lat,lon],…]}   (OSRM /route per pair,
         subsampled ~40 pts, disk-cached; straight-line/omit on OSRM miss)
  payload += {"geom": {…}, "depots": [{name,lat,lon},…]}
→ write_dashboard embeds payload into the template
```

- **Pair key**: `f"{lat1:.5f},{lon1:.5f}|{lat2:.5f},{lon2:.5f}"`. Geometry of a leg is
  epoch-independent (just the road between two points), so each unique pair is baked once and
  reused across every epoch and both the committed/internal routes.
- **Cache**: a JSON keyed by pair (e.g. `data/Output/osrm_geometry_cache.json`), mirroring the
  existing OSRM pair-cache discipline (load → fill misses → save). Cross-run reuse is free.
- **Fallback**: OSRM unreachable or returns non-Ok → the pair is omitted from `geom`; the
  browser straight-lines any missing pair. Build logs the miss count; never fatal.
- **Size**: one polyline per unique pair (~40 pts). A 6-day run's unique-pair set is bounded
  by the number of distinct consecutive stop pairs, not epochs — manageable.

### Map view (browser)

- Leaflet map with CartoDB **dark_matter** tiles to match the board's dark theme (light
  `positron` as a fallback if a light mode is ever wanted). Tile + Leaflet JS/CSS load from
  their CDNs — allowed, since the target is a local file with internet (not a CSP Artifact).
- Default: gantt board as today. Click a vehicle lane → map pane opens as the main frame, that
  vehicle's gantt row becomes the bottom strip, close button restores the full board.
- For the focused vehicle at the current clock t:
  - **Depots**: ◆ markers from `depots`.
  - **Route polylines**: concatenate `geom[pair]` for each consecutive stop pair in the plan;
    committed vs internal styled per §Encoding.
  - **Stop markers**: numbered by sequence, shaped by leg type; popup = WT#, postcode,
    arrive/depart, leg type, committed state. (Ported from `viz_app.py` `stopIconNumbered` /
    `_stop_popup`.)
  - **Other vehicles**: hidden by default; "show others faintly" toggle draws them greyed,
    non-interactive, underneath — the overlap/abnormality lens.

### Time model + simulator

- Master clock `t` = wall-clock-of-day, driven by the gantt x-axis playhead + a play button.
- **Plan valid at t** = the snapshot at the latest epoch ≤ t. As t crosses a micro/anchor
  boundary the focused route re-forms (add/rearrange of the uncommitted tail). This is the
  "plan dynamically changing" axis.
- **Truck position at t**: find the leg the truck is on (between `planned_depart[i]` and
  `planned_arrive[i+1]` of the committed route), interpolate along that leg's baked geometry by
  time fraction; during service (`arrive`→`depart`) park at the stop. All client-side.
- Play animates t forward at a chosen speed; route morphs at epoch boundaries, marker slides.

### Internal-vs-committed overlay

- At t, for the focused vehicle, two routes in the vehicle's hue:
  - **Committed** = stops with `committed=1` in the plan-valid-at-t (what the driver holds).
  - **Internal** = the full standing plan at the latest epoch ≤ t (committed prefix +
    uncommitted tail the optimizer is holding). Committed ⊆ Internal always (you cannot commit
    a stop not in the plan), so the internal route is the committed route plus a tail.
- Same hue; a toggle chooses which is foreground (full opacity, solid) and drops the other to
  faint. The divergence — the internal tail beyond the committed prefix — is the tentative work
  and the thing to inspect.

### Encoding (reconciled)

| channel | encodes | values |
|---|---|---|
| hue | vehicle identity | per-vehicle color (viz_app palette) |
| opacity + line style | commitment state | committed = bright + solid; internal-tail = dim + dashed (board frontier palette) |
| marker shape | leg type | pickup circle · delivery square · direct diamond · trunk ⇅ |
| marker number | sequence | stop index within the trip/day |

## Error handling

- OSRM down at build → straight-line legs (pair omitted), logged, board still builds.
- A stop missing lat/lon → skip its marker, straight-line through it.
- The map layer is strictly additive: any geometry/render failure degrades the map but never
  breaks the gantt board (which already ships today).
- `--no-geometry` build flag → skip baking entirely; board renders exactly as today (the
  regression guard).

## Testing

- **Python TDD** (`test_viz_geometry.py`): pair dedup + key format; cache load/fill/save;
  OSRM-miss fallback (pair omitted, no raise); the `build()` payload gains well-formed
  `geom`/`depots`. Mock OSRM (no live dependency in tests).
- **Regression**: full suite (currently 803) stays green; `--no-geometry` payload equals the
  current payload shape (the board is unchanged without geometry).
- **UI verification**: build against the `runs_deptfloor` smoke run, headless screenshot of the
  map overlay + simulator + toggle; `node --check`-equivalent JS sanity before publish.

## Phasing (each phase leaves a working dashboard)

1. **Geometry baking + JSON** — `viz_geometry.py` + `viz_timeline_build` wiring, Python/TDD.
   Verifiable with no UI (inspect the payload).
2. **Map overlay + static render** — Leaflet pane, click-to-open, depots + OSRM route +
   numbered/shaped stops at a single epoch.
3. **Master clock** — plan-valid-at-t morphing + gantt playhead sync.
4. **Vehicle simulator** — moving marker interpolation along baked geometry.
5. **Internal-vs-committed overlay** — two-route render, toggle, "show others faintly."

## Standing constraints (this project)

- NO git commits (also: `e:\BEAT` is not a git repo).
- TDD for the Python build code; flag-off / `--no-geometry` keeps the board byte-similar.
- The map is a read-only view — it must never be able to change a plan (viz is always read-only).
- Auto-emission of `timeline.html` per dynamic run (already shipped) must keep working.
