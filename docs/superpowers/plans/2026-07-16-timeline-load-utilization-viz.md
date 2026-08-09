# Timeline Load-Utilization Viz Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show an order's pallets + weight in its hover tooltip (both board and map modes), and in map mode draw two step-line charts — pallet-utilization and weight-utilization over the day — behind the transparent order blocks in the timeline strip.

**Architecture:** The build ([viz_timeline_build.py](../../../freight_planner/viz_timeline_build.py)) bakes per-order `pallets`/`kg` and per-vehicle `capP`/`capKg` into the JSON. The tooltip (`jobTipHTML`, one function for both modes) gains two rows. A new pure `loadProfile(snap, jobs)` in [viz_timeline_maplogic.cjs](../../../freight_planner/viz_timeline_maplogic.cjs) (Node-tested) computes the running load; `drawStrip` renders it as two step-lines on a shared 0–100% scale under the blocks.

**Tech Stack:** Python (pandas) build; browser Canvas 2D + a `.cjs` map-logic module (Node `--test`); Playwright for a visual check.

**Repo note:** not committing unless asked — use **no-commit checkpoints** (run tests + pause) in place of `git commit` steps. Commands run from `ZECURE-Phase2-main/BackEnd/logistics/` with `PY="E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe"` and `node` on PATH.

---

### Task 1: Bake per-order pallets/kg + per-vehicle capacity into the JSON

**Files:**
- Modify: `freight_planner/viz_timeline_build.py` (add reads near line 299; extend the `j` dict line 439; extend the vehicle dict line 490)

- [ ] **Step 1: Read plan_full + capacity, build lookup maps** — in `build()`, right after `rs = pd.read_csv(plan / "route_stops.csv")` (line 299):

```python
    # per-order pallets/weight for the tooltip + the JS load profile (plan_full has the
    # per-leg load; route_stops only carries the cumulative load_*_after). Located by rglob
    # so it works under either run-folder layout; absent -> empty (fields default to 0).
    def _first(name):
        return next(iter(run_dir.rglob(name)), None)
    order_load: dict[str, tuple] = {}
    pf_path = _first("plan_full.csv")
    if pf_path is not None:
        pf = pd.read_csv(pf_path)
        for r in pf.itertuples(index=False):
            oid = str(getattr(r, "order_id", ""))[:8]
            p = float(getattr(r, "pallets", 0.0) or 0.0)
            kg = float(getattr(r, "weight_kg", 0.0) or 0.0)
            if oid and oid not in order_load:
                order_load[oid] = (p, kg)
    # per-vehicle capacity for utilization = load / capacity
    veh_cap: dict[str, tuple] = {}
    tc_path = _first("trip_capacity_utilization.csv")
    if tc_path is not None:
        tc = pd.read_csv(tc_path)
        for r in tc.itertuples(index=False):
            vid = str(getattr(r, "vehicle_id", ""))
            cp = float(getattr(r, "capacity_pallets", 0.0) or 0.0)
            ck = float(getattr(r, "capacity_kg", 0.0) or 0.0)
            if vid:
                veh_cap[vid] = (max(veh_cap.get(vid, (0.0, 0.0))[0], cp),
                                max(veh_cap.get(vid, (0.0, 0.0))[1], ck))
```

- [ ] **Step 2: Thread the maps into `_build_day`** — `_build_day` is called at line 361 and defined at line 389. Add `order_load` and `veh_cap` as parameters. Change the call (line 361):

```python
    day_list = [_build_day(d, snap, static, vmeta, names, created, plan, reports,
                           tour_segs, trunk_by_day, only_day, geometry, osrm_url,
                           order_load, veh_cap)
                for d in days]
```

and the signature (line 389) — append `order_load, veh_cap` after its current last parameter (keep all existing params in order):

```python
def _build_day(day, snap, static, vmeta, names, created, plan, reports, tour_segs,
               trunk_by_day, only_day, geometry, osrm_url, order_load, veh_cap):
```

(If the current signature differs, add the two params to the end and pass them at the one call site — do not reorder existing args.)

- [ ] **Step 3: Add pallets/kg to the job dict** — in `_build_day`, extend the `j` dict (line 439):

```python
        pk = order_load.get(oid[:8], (0.0, 0.0))
        j = {"o": oid[:8], "nm": names.get(oid, ""), "pc": pc, "pco": pco, "ty": ty,
             "lat": round(lat, 5), "lon": round(lon, 5),
             "bk": round(_bk(created.get(oid), day), 1),
             "new": int(leg in new_legs),
             "pallets": round(pk[0], 1), "kg": round(pk[1])}
```

- [ ] **Step 4: Add capacity to the vehicle dict** — in the vehicle loop (line 490), add `capP`/`capKg`:

```python
        cap = veh_cap.get(vid, (0.0, 0.0))
        vehicles.append({"id": vid, "type": vm.get("type", ""), "home": vm.get("home", ""),
                         "tour": bool(tour_segs.get((day, vid))), "intraday": grew, "snaps": snaps,
                         "tsegs": tour_segs.get((day, vid), []),
                         "capP": round(cap[0], 1), "capKg": round(cap[1])})
```

- [ ] **Step 5: Build the timeline and verify the fields are present**

Run: `$PY -m freight_planner.viz_app --run freight_planner/run_osrm_on --html` (or the project's usual timeline-build entrypoint for a run), then:
Run: `grep -o '"capP":[0-9.]*' freight_planner/run_osrm_on/**/timeline*.html | head` and `grep -o '"pallets":[0-9.]*' freight_planner/run_osrm_on/**/timeline*.html | head`
Expected: non-empty matches (the baked fields appear in the embedded JSON). If the build entrypoint/flag differs, use the one in `README_DYNAMIC.md`/`viz_app.py --help`.

- [ ] **Step 6: CHECKPOINT** — pause; confirm the JSON carries `pallets`, `kg`, `capP`, `capKg`.

---

### Task 2: Pallets + weight rows in the order tooltip (both modes)

**Files:**
- Modify: `freight_planner/viz_timeline_template.html` (`jobTipHTML`, line 665-681)

- [ ] **Step 1: Add the two rows** — in `jobTipHTML`, insert after the "scheduled" row (line 678), before the "drive · dwell" row:

```javascript
      `<div class="row"><span>pallets<\span><span>${j.pallets>0?j.pallets:"—"}</span></div>`+
      `<div class="row"><span>weight<\span><span>${j.kg>0?Number(j.kg).toLocaleString()+" kg":"—"}</span></div>`+
```

(Match the existing `<\span>`/`<\div>` escaping style already used in this template string.)

- [ ] **Step 2: Rebuild + eyeball** — rebuild the timeline (Task 1 Step 5 command). Open the HTML, hover an order in board mode and in map mode.
Expected: the tooltip now shows `pallets` and `weight` lines in both modes. (Screenshot verification is consolidated in Task 5.)

---

### Task 3: `loadProfile` in maplogic.cjs (Node-tested)

**Files:**
- Modify: `freight_planner/viz_timeline_maplogic.cjs` (add function inside the IIFE; add to the returned object ~line 271)
- Test: `tests/freight_planner/maplogic.test.cjs`

- [ ] **Step 1: Write the failing test** — append to `tests/freight_planner/maplogic.test.cjs`:

```javascript
test('loadProfile: deliveries leave depot loaded and drop; pickups rise; direct is a transient bump; trip ends empty', () => {
  const jobs = [
    { ty: 'delivery', pallets: 6, kg: 3000 },   // 0
    { ty: 'pickup',   pallets: 4, kg: 2000 },   // 1
    { ty: 'direct',   pallets: 2, kg: 1000 },   // 2
  ];
  // one trip: depot(-2 dep=60) -> deliver job0 (dep=90) -> pickup job1 (dep=120) -> direct job2 (arr=140,dep=150) -> depot(-1 dep=180)
  const snap = [[-2,50,60,0,0,0],[0,80,90,0,0,0],[1,110,120,0,0,0],[2,140,150,0,0,0],[-1,170,180,0,0,0]];
  const prof = ML.loadProfile(snap, jobs);
  // start: only the delivery is pre-loaded -> 6 pallets
  assert.deepStrictEqual(prof[0], { t: 60, p: 6, kg: 3000 });
  assert.deepStrictEqual(prof[1], { t: 90, p: 0, kg: 0 });        // delivered
  assert.deepStrictEqual(prof[2], { t: 120, p: 4, kg: 2000 });    // picked up
  assert.deepStrictEqual(prof[3], { t: 140, p: 6, kg: 3000 });    // direct boarded (bump)
  assert.deepStrictEqual(prof[4], { t: 150, p: 4, kg: 2000 });    // direct delivered
  assert.deepStrictEqual(prof[prof.length-1], { t: 180, p: 0, kg: 0 }); // depot: empty
});

test('loadProfile: empty snap (tour day / no rows) returns []', () => {
  assert.deepStrictEqual(ML.loadProfile([], []), []);
});
```

- [ ] **Step 2: Run it, expect failure**

Run: `node --test tests/freight_planner/maplogic.test.cjs`
Expected: FAIL — `ML.loadProfile is not a function`.

- [ ] **Step 3: Implement `loadProfile`** — inside the IIFE in `viz_timeline_maplogic.cjs` (before the `return {…}`):

```javascript
  // Running load over the day for the map strip's utilization lines. Per trip (reset at
  // each depot marker): deliveries ride out of the depot pre-loaded and drop off; pickups
  // rise and ride to the depot; a direct carry is a transient bump (boards at arrive, drops
  // at depart). Returns step points { t, p, kg } = load AFTER time t. Capacity-agnostic.
  function loadProfile(snap, jobs) {
    const out = [];
    if (!snap || !snap.length) return out;
    // group stop-indices by trip index (stop[4]), preserving order
    const trips = new Map();
    for (const s of snap) { const tr = s[4]; if (!trips.has(tr)) trips.set(tr, []); trips.get(tr).push(s); }
    for (const stops of trips.values()) {
      const js = stops.filter(s => s[0] >= 0);
      let p = 0, kg = 0;
      for (const s of js) { const j = jobs[s[0]] || {}; if (j.ty === 'delivery') { p += (j.pallets||0); kg += (j.kg||0); } }
      const startT = (stops[0] && stops[0][2] != null) ? stops[0][2] : (js[0] ? js[0][1] : null);
      if (startT != null) out.push({ t: startT, p, kg });
      for (const s of js) {
        const j = jobs[s[0]] || {}, arr = s[1], dep = s[2];
        if (j.ty === 'delivery') { p -= (j.pallets||0); kg -= (j.kg||0); if (dep != null) out.push({ t: dep, p, kg }); }
        else if (j.ty === 'pickup') { p += (j.pallets||0); kg += (j.kg||0); if (dep != null) out.push({ t: dep, p, kg }); }
        else if (j.ty === 'direct') {
          if (arr != null) out.push({ t: arr, p: p + (j.pallets||0), kg: kg + (j.kg||0) });
          if (dep != null) out.push({ t: dep, p, kg });
        }
      }
      const ret = stops.find(s => s[0] === -1);
      if (ret && ret[2] != null) out.push({ t: ret[2], p: 0, kg: 0 });   // depot: offloaded
    }
    return out;
  }
```

Add `loadProfile` to the returned object (line ~271-273), e.g. after `tourDayNodes`:

```javascript
  return { hasCoord, pairKey, legGeom, depotCoord, stopCoords, routePolyline,
           commitFlags, committedSnap, committedTimedNodes, routeTimedNodes,
           segmentsFromNodes, routeSegments, posAlongNodes, truckPos, tourDayNodes, loadProfile };
```

(Copy the existing return list verbatim and append `, loadProfile` — do not drop any name.)

- [ ] **Step 4: Run tests, expect pass**

Run: `node --test tests/freight_planner/maplogic.test.cjs`
Expected: PASS (all existing tests + the two new ones).

- [ ] **Step 5: CHECKPOINT** — pause; maplogic tests green.

---

### Task 4: Render the two utilization lines in `drawStrip` + theme colors

**Files:**
- Modify: `freight_planner/viz_timeline_template.html` — CSS tokens (~line 9, 19, 27, 34), the `P` palette (~line 305), and `drawStrip` (insert after line 882, before the block loop at 884)

- [ ] **Step 1: Add the two color tokens** — append to each of the four palette blocks (`:root` light line 9, dark-media line 19, `[data-theme="light"]` line 27, `[data-theme="dark"]` line 34) the same way the existing `--pickup/--delivery/...` tokens are declared. Light (lines 9 and 27):

```css
    --util-pal:#2a9d8f; --util-wt:#d9822b;
```

Dark (lines 19 and 34):

```css
    --util-pal:#54c4b6; --util-wt:#f0a24f;
```

- [ ] **Step 2: Surface them into the JS palette** — in the `P` object (~line 305, where `firm:g("--firm"),tent:g("--tent")…` are read), add:

```javascript
    utilPal:g("--util-pal"), utilWt:g("--util-wt"),
```

- [ ] **Step 3: Draw the lines under the blocks** — in `drawStrip`, immediately after `const flags=MAPLOGIC.commitFlags(...)` (line 882) and **before** the `for(let i=0;...)` block loop (line 884):

```javascript
      // load-utilization lines UNDER the (transparent) blocks: two step curves on a shared
      // 0-100% scale across the block band. capP/capKg come from the vehicle; 0 => skip.
      (function(){
        const prof=MAPLOGIC.loadProfile(snap,jobs); if(prof.length<2) return;
        const yOf=u=>by+bh*(1-Math.max(0,Math.min(1,u)));
        // faint 100% ceiling + 0/50/100 ticks
        sctx.strokeStyle=P.hair2; sctx.lineWidth=1; sctx.setLineDash([2,3]);
        sctx.beginPath();sctx.moveTo(sx(T0),yOf(1));sctx.lineTo(sx(T1),yOf(1));sctx.stroke();sctx.setLineDash([]);
        sctx.fillStyle=P.faint; sctx.font="8px "+mono(); sctx.textAlign="left";
        for(const u of [0,.5,1]) sctx.fillText(Math.round(u*100)+"%", sx(T0)+2, yOf(u));
        // staircase: hold each level until the next time, then step to the new level
        const step=(key,cap,color)=>{ if(!cap) return;
          sctx.strokeStyle=color; sctx.lineWidth=1.5; sctx.globalAlpha=.9; sctx.beginPath();
          sctx.moveTo(sx(prof[0].t), yOf(prof[0][key]/cap));
          for(let k=1;k<prof.length;k++){ const x=sx(prof[k].t);
            sctx.lineTo(x, yOf(prof[k-1][key]/cap));    // hold previous level to this time
            sctx.lineTo(x, yOf(prof[k][key]/cap)); }    // then step to the new level
          sctx.stroke(); sctx.globalAlpha=1; };
        step("p", v.capP, P.utilPal);
        step("kg", v.capKg, P.utilWt);
      })();
```

- [ ] **Step 4: Rebuild + verify no errors** — rebuild the timeline (Task 1 Step 5). Open the HTML, switch to map mode, select a vehicle with load.
Expected: two step-lines (teal = pallets, amber = weight) visible behind the transparent blocks, rising at pickups and falling at deliveries, with a faint 100% ceiling and 0/50/100% ticks. No console errors.

- [ ] **Step 5: CHECKPOINT** — pause for a look.

---

### Task 5: Playwright screenshot verification (both themes) + full test sweep

**Files:** none (verification only)

- [ ] **Step 1: Node + Python test sweep**

Run: `node --test tests/freight_planner/maplogic.test.cjs` and `$PY -m pytest tests/freight_planner -q`
Expected: all green (the Python change is the build; maplogic is the new function).

- [ ] **Step 2: Playwright screenshots** — using the project's existing Playwright screenshot harness (see how the map-dashboard screenshots were taken — `map-dashboard` spec/plan under `docs/superpowers/`), capture map mode for `run_osrm_on` with a loaded vehicle selected, in **light and dark**. If no harness script exists, a minimal one: load the built `timeline*.html` via `file://`, click the map/mode toggle, select a vehicle, screenshot; repeat with `data-theme="dark"` stamped on `:root`.

- [ ] **Step 3: Verify the screenshots show:** the two utilization step-lines legible *through* the transparent blocks in both themes; the tooltip shows pallets/weight (hover capture or a second shot). Iterate colors (Task 4 Step 1 values) if either line is hard to read against the blocks.

- [ ] **Step 4: CHECKPOINT** — present the screenshots for sign-off.

---

## Notes / out of scope
- Board mode gets the tooltip fields but **not** the lines (map-mode, selected-vehicle only).
- Tour-day lanes (no snap rows) draw **no** load lines in v1 (`loadProfile([])→[]` guards it); the tour block rendering is unchanged.
- Directs are a transient bump, not a separately drawn origin sub-point.
