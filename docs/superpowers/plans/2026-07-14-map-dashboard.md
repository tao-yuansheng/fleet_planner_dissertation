# Dynamic Map Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a road-accurate Leaflet map to the evolving-plan dashboard — click a vehicle to open a map overlay that shows its OSRM route re-forming across epochs, a simulated truck moving by planned times, and the internal plan vs the driver-committed plan as same-hue/different-opacity overlays.

**Architecture:** Extend the existing `viz_timeline_build` + `viz_timeline_template.html` (Approach A). A new standalone Python module bakes OSRM road geometry per unique stop-pair into the JSON payload at build time; the template gains a Leaflet map layer that reconstructs routes from the per-epoch `snaps` arrays already in the payload. The map is strictly additive — the gantt board is untouched when geometry is absent.

**Tech Stack:** Python 3.12 + pandas (build), pytest (Phase 1 TDD), Leaflet + CartoDB dark tiles + OSM (browser, local file), OSRM `/route` for road geometry.

**Standing constraints (this project):** NO git commits — every "Checkpoint" step stages/verifies only. TDD for the Python build code. `--no-geometry` must keep the payload byte-identical to today (regression guard). The map is read-only — it can never change a plan. The existing 803-test suite stays green. `node --check` (or equivalent JS parse check) before any publish. Verify UI by building against `freight_planner/runs_deptfloor/2026-01/2026-01-12_to_2026-01-13`.

**Key data facts (verified against source 2026-07-14):**
- `build()` returns `{"meta": {...}, "days": [day, ...]}`. Each `day` has `jobs` (list; `jobs[i] = {o,nm,pc,pco,ty,lat,lon,bk,new}`) and `vehicles` (list; each `{id,type,home,snaps,...}`).
- `snaps[e]` = the vehicle's plan at epoch index `e`: a flat list of stops, each `[jobIdx, arriveMin, departMin, committed, tripIdx, reopt]`. `jobIdx >= 0` → `jobs[jobIdx]` (has lat/lon); `jobIdx == -2` → depot→first-stop connector; `jobIdx == -1` → last-stop→depot connector. `arriveMin`/`departMin` are minutes-of-day floats.
- `day.snapAt[e]` = wall-clock minutes of epoch `e`; `day.snapKind[e]` ∈ {seed, warm, micro, close}.
- Depot anchors: `freight_planner.shared.config.DEPOT_ANCHORS` = `{"CB22": (52.0859, 0.1717), "BEDFORD": (52.1225, -0.43149), ...}` (name → (lat, lon)).
- OSRM geometry: `freight_planner.shared.routing.get_route_geometry([(lat,lon),...], osrm_url)` → `[[lat,lon],...]` or `None`. `coord_key(lat,lon)` → `f"{lat:.5f},{lon:.5f}"`.

---

## Phase 1 — Build-time geometry (Python, TDD)

### Task 1: `viz_geometry.py` — collect unique directional coord-pairs from a built payload

**Files:**
- Create: `freight_planner/viz_geometry.py`
- Test: `tests/freight_planner/test_viz_geometry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/freight_planner/test_viz_geometry.py
from freight_planner.viz_geometry import route_pairs


def _day():
    # jobs 0,1 have coords; a vehicle snap visits depot(-2) -> job0 -> job1 -> depot(-1)
    return {
        "jobs": [
            {"lat": 52.10, "lon": 0.10}, {"lat": 52.20, "lon": 0.20},
        ],
        "vehicles": [
            {"id": "V1", "home": "CB22",
             "snaps": [[[-2, 400, 400, 0, 0, 0], [0, 420, 430, 1, 0, 0],
                        [1, 450, 460, 0, 0, 0], [-1, 480, 490, 0, 0, 0]]]},
        ],
    }


def test_route_pairs_collects_consecutive_coords_including_depot():
    depot = {"CB22": (52.00, 0.00)}
    pairs = route_pairs([_day()], depot)
    # depot->job0, job0->job1, job1->depot
    assert ((52.00, 0.00), (52.10, 0.10)) in pairs
    assert ((52.10, 0.10), (52.20, 0.20)) in pairs
    assert ((52.20, 0.20), (52.00, 0.00)) in pairs


def test_route_pairs_dedupes_across_epochs_and_vehicles():
    day = _day()
    day["vehicles"].append(dict(day["vehicles"][0], id="V2"))   # identical route
    day["vehicles"][0]["snaps"].append(day["vehicles"][0]["snaps"][0])  # repeat epoch
    pairs = route_pairs([day], {"CB22": (52.00, 0.00)})
    assert len(pairs) == 3   # still just the 3 unique directional legs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 python -B -m pytest tests/freight_planner/test_viz_geometry.py -q`
Expected: FAIL — `ModuleNotFoundError: freight_planner.viz_geometry`.

- [ ] **Step 3: Write minimal implementation**

```python
# freight_planner/viz_geometry.py
"""Build-time OSRM road-geometry baking for the map dashboard.

Routes on the map are reconstructed in the browser from the per-epoch ``snaps``
already in the timeline payload; this module bakes the road polyline for each
UNIQUE consecutive coordinate-pair those snaps traverse (depot connectors
included) so the browser needs no live OSRM. Geometry is directional and
epoch-independent, so each pair is fetched once and cached to disk.
"""
from __future__ import annotations

from pathlib import Path

Coord = tuple[float, float]
Pair = tuple[Coord, Coord]


def _depot_coord(vehicle: dict, depots: dict) -> Coord | None:
    anchor = depots.get(str(vehicle.get("home", "")))
    if anchor is None:
        return None
    return (float(anchor[0]), float(anchor[1]))


def route_pairs(days: list[dict], depots: dict) -> set[Pair]:
    """Every distinct directional (from, to) coordinate leg any vehicle traverses
    across every epoch snapshot, depot connectors resolved to the vehicle's home
    anchor. Deduped."""
    pairs: set[Pair] = set()
    for day in days:
        jobs = day.get("jobs", [])
        for veh in day.get("vehicles", []):
            depot = _depot_coord(veh, depots)
            for snap in veh.get("snaps", []):
                seq: list[Coord] = []
                for stop in snap:
                    ji = stop[0]
                    if ji == -2 or ji == -1:
                        c = depot
                    elif 0 <= ji < len(jobs):
                        j = jobs[ji]
                        c = (float(j["lat"]), float(j["lon"]))
                    else:
                        c = None
                    if c is not None:
                        seq.append(c)
                for a, b in zip(seq, seq[1:]):
                    if a != b:
                        pairs.add((a, b))
    return pairs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 python -B -m pytest tests/freight_planner/test_viz_geometry.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Checkpoint** — no commit (standing rule). Confirm the two tests pass and move on.

---

### Task 2: `viz_geometry.bake()` — fetch + cache per-pair OSRM polylines

**Files:**
- Modify: `freight_planner/viz_geometry.py`
- Test: `tests/freight_planner/test_viz_geometry.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/freight_planner/test_viz_geometry.py
from freight_planner import viz_geometry


def test_bake_fetches_each_pair_once_and_caches(tmp_path, monkeypatch):
    calls = []

    def fake_geom(waypoints, osrm_url="x", max_points=600):
        calls.append(tuple(waypoints))
        (a, b) = waypoints
        return [[a[0], a[1]], [b[0], b[1]]]   # trivial 2-point line

    monkeypatch.setattr(viz_geometry, "get_route_geometry", fake_geom)
    pairs = {((52.0, 0.0), (52.1, 0.1)), ((52.1, 0.1), (52.0, 0.0))}
    cache = tmp_path / "geom.json"

    geom = viz_geometry.bake(pairs, cache_path=cache)
    assert set(geom) == {"52.00000,0.00000|52.10000,0.10000",
                         "52.10000,0.10000|52.00000,0.00000"}
    assert geom["52.00000,0.00000|52.10000,0.10000"] == [[52.0, 0.0], [52.1, 0.1]]
    assert len(calls) == 2 and cache.exists()

    # second bake: all cached -> zero new OSRM calls
    calls.clear()
    geom2 = viz_geometry.bake(pairs, cache_path=cache)
    assert calls == [] and geom2 == geom


def test_bake_omits_pair_on_osrm_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(viz_geometry, "get_route_geometry",
                        lambda w, osrm_url="x", max_points=600: None)
    geom = viz_geometry.bake({((52.0, 0.0), (52.1, 0.1))}, cache_path=tmp_path / "g.json")
    assert geom == {}   # OSRM down -> pair omitted, browser straight-lines it
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 python -B -m pytest tests/freight_planner/test_viz_geometry.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'bake'`.

- [ ] **Step 3: Write minimal implementation** (append to `viz_geometry.py`)

```python
import json

from freight_planner.shared.routing import coord_key, get_route_geometry
from freight_planner.shared.paths import LOGISTICS_ROOT

GEOM_CACHE_PATH = LOGISTICS_ROOT / "data" / "Output" / "osrm_geometry_cache.json"
GEOM_MAX_POINTS = 40   # per leg; keeps the payload small, still road-shaped


def _pair_key(a: Coord, b: Coord) -> str:
    return f"{coord_key(a[0], a[1])}|{coord_key(b[0], b[1])}"


def _load(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path: Path, cache: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f)


def bake(pairs: set[Pair], osrm_url: str | None = None,
         cache_path: Path = GEOM_CACHE_PATH) -> dict:
    """Return {pair_key: [[lat,lon],...]} for each pair, fetching misses from OSRM
    and persisting them. A pair OSRM cannot route is OMITTED (the browser
    straight-lines it). Never raises on OSRM failure."""
    cache = _load(cache_path)
    dirty = False
    out: dict = {}
    kwargs = {"osrm_url": osrm_url} if osrm_url else {}
    for a, b in pairs:
        k = _pair_key(a, b)
        if k not in cache:
            try:
                line = get_route_geometry([a, b], max_points=GEOM_MAX_POINTS, **kwargs)
            except Exception:
                line = None
            if line:
                cache[k] = line
                dirty = True
            else:
                continue
        if cache.get(k):
            out[k] = cache[k]
    if dirty:
        _save(cache_path, cache)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 python -B -m pytest tests/freight_planner/test_viz_geometry.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Checkpoint** — confirm all four `test_viz_geometry` tests pass.

---

### Task 3: Wire geometry + depots into `viz_timeline_build.build()`

**Files:**
- Modify: `freight_planner/viz_timeline_build.py` (imports; the `build()` return at the `{"meta":..., "days":...}` dict; `main()`/`build()` signature for `--no-geometry`)
- Test: `tests/freight_planner/test_viz_timeline_build.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/freight_planner/test_viz_timeline_build.py
def test_build_bakes_geometry_and_depots(tmp_path, monkeypatch):
    from freight_planner import viz_timeline_build as vtb
    run = _mk_run(tmp_path, [_trunk_row(vehicles="TB1", feasible="TB1")])
    monkeypatch.setattr(vtb, "bake",
                        lambda pairs, osrm_url=None: {"K": [[52.0, 0.0], [52.1, 0.1]]})
    data = vtb.build(run)
    assert data["geom"] == {"K": [[52.0, 0.0], [52.1, 0.1]]}
    assert any(d["name"] == "CB22" for d in data["depots"])
    assert all({"name", "lat", "lon"} <= set(d) for d in data["depots"])


def test_build_no_geometry_omits_geom(tmp_path):
    from freight_planner import viz_timeline_build as vtb
    run = _mk_run(tmp_path, [_trunk_row(vehicles="TB1", feasible="TB1")])
    data = vtb.build(run, geometry=False)
    assert data.get("geom", {}) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 python -B -m pytest tests/freight_planner/test_viz_timeline_build.py -q -k "geometry or depots"`
Expected: FAIL — `build()` has no `geometry` kwarg / no `geom` key.

- [ ] **Step 3: Write minimal implementation**

In `viz_timeline_build.py` add imports near the top:

```python
from freight_planner.shared.config import DEPOT_ANCHORS
from freight_planner.viz_geometry import route_pairs, bake
```

Change `build`'s signature:

```python
def build(run_dir: Path, delta: int = 60, only_day: str | None = None,
          geometry: bool = True, osrm_url: str | None = None) -> dict:
```

Replace the final `return {...}` of `build()` with (keep `meta`/`days` exactly as they are, add `depots` always and `geom` when enabled):

```python
    depots = [{"name": str(name), "lat": float(a[0]), "lon": float(a[1])}
              for name, a in DEPOT_ANCHORS.items()]
    geom: dict = {}
    if geometry:
        anchors = {name: (float(a[0]), float(a[1])) for name, a in DEPOT_ANCHORS.items()}
        geom = bake(route_pairs(day_list, anchors), osrm_url=osrm_url)
    return {
        "meta": {"days": days, "delta": delta, "delta_r1": DELTA_R1, "t0": T0, "t1": T1,
                 "note": "Per-epoch snapshots — the plan exactly as it stood at each clock T "
                         "(seed continuous; the noon re-opt reshuffles the uncommitted tail; "
                         "committed stops only delay). Page days with the arrows."},
        "days": day_list,
        "depots": depots,
        "geom": geom,
    }
```

Add the CLI flag in `main()` (next to `--html`) and pass it through `build`/`write_dashboard`:

```python
    ap.add_argument("--no-geometry", dest="geometry", action="store_false",
                    help="skip OSRM road-geometry baking (fast rebuild; map straight-lines)")
```

Update the `build(...)` call in `main()` and in `write_dashboard()` to forward `geometry` and `osrm_url` (add matching kwargs to `write_dashboard`, default `geometry=True, osrm_url=None`).

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 python -B -m pytest tests/freight_planner/test_viz_timeline_build.py -q`
Expected: PASS (all timeline-build tests, old + 2 new).

- [ ] **Step 5: Full regression**

Run: `PYTHONHASHSEED=0 python -B -m pytest tests/ -q`
Expected: PASS (805+; was 803 + 4 geometry + 2 build = new total). Fix any fixture that assumed the old `build()` return.

- [ ] **Step 6: Real build + payload inspection**

Run: `PYTHONHASHSEED=0 python -B -m freight_planner.viz_timeline_build --run-dir freight_planner/runs_deptfloor/2026-01/2026-01-12_to_2026-01-13 --out /tmp/map.json --html /tmp/map.html`
Then: `python -B -c "import json; d=json.load(open('/tmp/map.json')); print('geom legs:', len(d['geom']), '| depots:', [x['name'] for x in d['depots']])"`
Expected: non-zero geom legs (OSRM up) or 0 with a logged miss (OSRM down); depot names listed.

- [ ] **Step 7: Checkpoint** — Phase 1 complete: payload now carries `geom` + `depots`, `--no-geometry` omits geom, suite green.

---

## Phase 2 — Map overlay + static route render (template JS)

> JS phases are verified by build + headless screenshot + JS parse-check, not pytest (browser rendering). Keep every helper small and named so the diff is legible.

### Task 4: Leaflet map pane, click-to-open overlay, focused-vehicle route at one epoch

**Files:**
- Modify: `freight_planner/viz_timeline_template.html` (add a `<div id="map">` overlay + Leaflet `<link>`/`<script>` CDN tags in `<head>`; a JS block after the existing gantt script)

- [ ] **Step 1: Add Leaflet assets + map container.** In `<head>` add the Leaflet 1.9 CSS/JS CDN `<link>`/`<script>` (allowed — local file target). Add an overlay container that is hidden by default:

```html
<div id="mapWrap" style="display:none;position:fixed;inset:0;z-index:50;background:#0b0e16">
  <div id="map" style="position:absolute;top:0;left:0;right:0;bottom:150px"></div>
  <div id="mapGantt" style="position:absolute;left:0;right:0;bottom:0;height:150px"></div>
  <button id="mapClose" style="position:absolute;top:10px;right:12px;z-index:60">✕ board</button>
</div>
```

- [ ] **Step 2: Port the marker/shape helpers** from `viz_app.py` (`_shapeCss`, `_isCollect`, `_isLoad`, `stopIconNumbered`) into the new JS block, adapted to the timeline `jobs[i].ty` values (`pickup`/`delivery`/`direct`/`trunk`): pickup→circle (border-radius:50%), delivery→square, direct→diamond (rotate 45°), trunk→teal. Map the vehicle hue via a per-vehicle color function (reuse the board's existing palette function).

- [ ] **Step 3: Reconstruct a vehicle's route at epoch `e` from `snaps` + `geom`.** Add:

```javascript
const _depotByName = {}; (DATA.depots||[]).forEach(d=>{_depotByName[d.name]=d;});
function depotOf(name){ return _depotByName[name] || null; }   // vehicle.home -> {name,lat,lon}
function legGeom(a, b){                    // a,b = [lat,lon]; return road polyline or straight
  const k = a[0].toFixed(5)+','+a[1].toFixed(5)+'|'+b[0].toFixed(5)+','+b[1].toFixed(5);
  return DATA.geom[k] || [a, b];
}
function stopCoord(day, veh, stop){        // stop = [jobIdx,...]; -2/-1 => depot
  const ji = stop[0];
  if(ji === -2 || ji === -1){ const d = depotOf(veh.home); return d ? [d.lat,d.lon] : null; }
  const j = day.jobs[ji]; return (j && j.lat && j.lon) ? [j.lat, j.lon] : null;
}
function routePolyline(day, veh, e){       // concatenated road geometry for the whole snap
  const snap = veh.snaps[e] || [], pts = [];
  let prev = null;
  for(const st of snap){
    const c = stopCoord(day, veh, st); if(!c) continue;
    if(prev) legGeom(prev, c).forEach(p=>pts.push(p));
    prev = c;
  }
  return pts;
}
```

- [ ] **Step 4: Open-on-click + render.** Clicking a vehicle lane in the gantt sets `focusVeh`, shows `#mapWrap`, calls `map.invalidateSize()`, draws depot markers (from `DATA.depots`, yellow square divIcon per viz_app), draws the focused route polyline (`routePolyline`) in the vehicle hue, and one `stopIconNumbered` marker per real stop (`jobIdx>=0`) with a popup (WT#, pc, arrive/depart from `jobs`/`snap`, leg type, committed state). `#mapClose` hides the overlay.

- [ ] **Step 5: Verify** — build against the smoke run and screenshot:

Run: `PYTHONHASHSEED=0 python -B -m freight_planner.viz_timeline_build --run-dir freight_planner/runs_deptfloor/2026-01/2026-01-12_to_2026-01-13 --out /tmp/map.json --html /tmp/map.html`
Then capture a headless screenshot of `/tmp/map.html` after a simulated vehicle-lane click (reuse the project's existing headless-screenshot helper). Confirm: map shows dark tiles, depot pins, a road-snapped route, numbered shaped stops. Parse-check the template (JS syntax) before finishing.

- [ ] **Step 6: Checkpoint** — Phase 2 renders a static focused route on a real map.

---

## Phase 3 — Master clock (plan valid at t)

### Task 5: Gantt playhead drives which epoch's plan the map shows

**Files:**
- Modify: `freight_planner/viz_timeline_template.html`

- [ ] **Step 1: Add a master-clock state `tNow`** (minutes-of-day) and a `epochAt(day, t)` helper returning the latest epoch index with `day.snapAt[e] <= t` (or 0 before the seed):

```javascript
function epochAt(day, t){
  let e = 0;
  for(let i=0;i<day.snapAt.length;i++){ if(day.snapAt[i] <= t) e = i; else break; }
  return e;
}
```

- [ ] **Step 2: Render the map from `epochAt(day, tNow)`** instead of a fixed epoch, so the focused route re-forms as `tNow` crosses each snapshot boundary. Redraw route + markers on `tNow` change (clear the previous Leaflet layers first — keep a `layerGroup` and `.clearLayers()`).

- [ ] **Step 3: Wire the gantt's existing time axis as the scrubber** — a vertical playhead line at `tNow`; dragging it / clicking the axis updates `tNow` and triggers a map redraw. The bottom `#mapGantt` strip shows the focused vehicle's gantt row with the same playhead.

- [ ] **Step 4: Verify** — build the smoke run; screenshot at two `tNow` values straddling the noon warm epoch; confirm the focused route visibly changes (uncommitted tail re-forms) across the boundary. Parse-check.

- [ ] **Step 5: Checkpoint** — the map is now epoch-dynamic under one master clock.

---

## Phase 4 — Vehicle simulator (moving marker)

### Task 6: Interpolate the truck's planned position along the committed route

**Files:**
- Modify: `freight_planner/viz_timeline_template.html`

- [ ] **Step 1: Add position interpolation.** For the focused vehicle at `tNow`, over the committed sub-route (stops with `committed==1`, plus their depot connectors), find the active leg and interpolate:

```javascript
function truckPos(day, veh, e, t){
  const snap = (veh.snaps[e]||[]).filter(s=>s[3]===1 || s[0]===-2 || s[0]===-1);
  for(let i=0;i<snap.length-1;i++){
    const s=snap[i], n=snap[i+1];
    const dep=s[2], arr=n[1];                 // depart this stop -> arrive next
    if(t>=s[1] && t<=dep) return stopCoord(day,veh,s);      // servicing this stop
    if(t>dep && t<arr){                                     // driving s -> n
      const a=stopCoord(day,veh,s), b=stopCoord(day,veh,n); if(!a||!b) return null;
      const line=legGeom(a,b), f=(t-dep)/Math.max(1,arr-dep);
      return line[Math.min(line.length-1, Math.round(f*(line.length-1)))];
    }
  }
  return null;
}
```

- [ ] **Step 2: Draw the truck marker** (a distinct divIcon — filled triangle/chevron in the vehicle hue) at `truckPos`, updated on every `tNow` change.

- [ ] **Step 3: Add a play button** that advances `tNow` on `requestAnimationFrame` at a configurable minutes-per-second, updating the route (per Phase 3) and marker; pause/stop resets to manual scrub.

- [ ] **Step 4: Verify** — build the smoke run; capture 3 screenshots across a play sweep for one vehicle (e.g. FJ72XFF on 2026-01-13); confirm the marker sits on the road geometry and advances with `tNow`. Parse-check.

- [ ] **Step 5: Checkpoint** — the simulated truck moves along the planned route.

---

## Phase 5 — Internal-vs-committed overlay + fleet context

### Task 7: Two-route overlay, opacity toggle, "show others faintly"

**Files:**
- Modify: `freight_planner/viz_timeline_template.html`

- [ ] **Step 1: Split the focused render into two polylines** from the same `snap`: `committed` (stops `committed==1` + connectors) and `internal` (all stops). Draw both in the vehicle hue; committed = solid `opacity 0.95`, internal-tail = dashed (`dashArray:'5,7'`) `opacity 0.4`.

- [ ] **Step 2: Add a foreground toggle** (`committed` ⟷ `internal`): the chosen layer goes `opacity 0.95`/solid and `bringToFront()`, the other drops to `opacity 0.25`. Default foreground = committed.

- [ ] **Step 3: Add "show others faintly"** — when on, draw every non-focused vehicle's committed route at `epochAt(day,tNow)` in grey `opacity 0.15`, non-interactive (no markers), behind the focused route — the overlap/abnormality lens.

- [ ] **Step 4: Verify** — build the smoke run; screenshot with (a) committed foreground, (b) internal foreground, (c) "show others" on; confirm same-hue/different-opacity overlays and the faint fleet backdrop. Parse-check `node --check`-style.

- [ ] **Step 5: Full regression + republish check**

Run: `PYTHONHASHSEED=0 python -B -m pytest tests/ -q` (expect green).
Rebuild `timeline.html` for `runs_deptfloor`; confirm the auto-emit path (`run_rolling._emit_timeline` → `write_dashboard`) still produces a valid page with the map.

- [ ] **Step 6: Checkpoint** — feature complete.

---

## Docs to update after implementation (final task, not a code phase)

- `freight_planner/README_DYNAMIC.md` §6 (board paragraph) — the map overlay, clock, simulator, internal/committed toggle.
- `freight_planner/PIPELINE.md` §15 (evolving-plan board bullet) — map + baked OSRM geometry.
- `freight_planner/README.md` "Anatomy of a run folder" — `timeline.html` now includes the map.
- `freight_planner/QUEST_LOG.md` — a DONE entry.
- Memory: update `per-epoch-plan-snapshots` (the "NEXT: the MAP" note is now shipped).

## Self-review notes (coverage against the spec)

- Spec §Build-time data flow → Tasks 1–3. §Map view → Task 4. §Time model → Tasks 5–6. §Internal-vs-committed → Task 7. §Encoding table → Tasks 4 (shape/number/hue) + 5–7 (opacity/dash). §Error handling → Task 2 (OSRM miss omit), Task 4 (`legGeom` straight-line fallback), `--no-geometry` (Task 3). §Testing → Phase 1 pytest + JS build/screenshot per phase + full-suite gates in Tasks 3/7.
- Names are consistent across tasks: `route_pairs`/`bake` (Py), `legGeom`/`stopCoord`/`routePolyline`/`epochAt`/`truckPos`/`focusVeh`/`tNow` (JS).
