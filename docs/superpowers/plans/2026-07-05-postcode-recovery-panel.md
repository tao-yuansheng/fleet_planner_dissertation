# Postcode-Recovery Panel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use `- [ ]`. NO git commits (standing rule) — replace "commit" steps with "run the test file green".

**Goal:** Surface every planned stop whose postcode was recovered by the geocode chain (repaired / outcode-fallback / terminated) in a tiered "Postcode recoveries" panel in the trip viz, so the operator can manually verify each fix.

**Architecture:** Viz-side only. Classify each stop's postcode from the already-loaded `postcode_cache.json` (`source`/`precision`); collect a whole-plan list in `build_plan_data`; render a new bottom-left sidebar section. No pipeline change, no re-run.

**Tech Stack:** Python 3.12, pandas, pytest; the viz HTML/JS template string in `viz_app.py`. Spec: `docs/superpowers/specs/2026-07-05-postcode-recovery-panel-design.md`.

---

## File Structure
- `freight_planner/viz_app.py` — ADD `re` import; `_space_pc`, `_geocode_recovery`, `_collect_recoveries` helpers; wire into `build_plan_data` (capture full stops, compute recoveries, add to summary + return); add sidebar `#rec-sec` div + a render IIFE + a summary row.
- `tests/freight_planner/test_viz_app_recoveries.py` — NEW (pure-helper tests).

---

## Task 1: Classifier + spacer helpers

**Files:** Modify `freight_planner/viz_app.py`; Test `tests/freight_planner/test_viz_app_recoveries.py`

- [ ] **Step 1: Write failing tests**

Create `tests/freight_planner/test_viz_app_recoveries.py`:

```python
from __future__ import annotations

from freight_planner.viz_app import _space_pc, _geocode_recovery


def test_space_pc_inserts_uk_space():
    assert _space_pc("AL109BS") == "AL10 9BS"
    assert _space_pc("SG8") == "SG8"          # outward-only, unchanged
    assert _space_pc("") == ""


def test_geocode_recovery_repaired():
    cache = {"AL109B5": {"lat": 1, "lon": 2, "source": "postcodes.io (repaired AL109BS)"}}
    assert _geocode_recovery("AL10 9B5", cache) == {
        "method": "repaired", "risk": "high", "resolved_to": "AL10 9BS"}


def test_geocode_recovery_outcode():
    cache = {"SG8": {"source": "postcodes.io/outcodes", "precision": "outcode_district", "postcode": "SG8"}}
    r = _geocode_recovery("SG8", cache)
    assert r["method"] == "outcode" and r["risk"] == "high" and "SG8" in r["resolved_to"]


def test_geocode_recovery_terminated_is_low_risk():
    cache = {"AL71RR": {"source": "postcodes.io/terminated", "postcode": "AL7 1RR"}}
    r = _geocode_recovery("AL7 1RR", cache)
    assert r["method"] == "terminated" and r["risk"] == "low"


def test_geocode_recovery_exact_legacy_and_empty_return_none():
    assert _geocode_recovery("CB1 1AA", {"CB11AA": {"source": "postcodes.io", "precision": "postcode_unit"}}) is None
    assert _geocode_recovery("CB1 1AA", {"CB11AA": [52.0, 0.1]}) is None   # legacy list entry
    assert _geocode_recovery("MISS", {}) is None                           # not in cache
    assert _geocode_recovery("", {}) is None
```

- [ ] **Step 2: Run to verify fail**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_viz_app_recoveries.py -q`
Expected: FAIL — `ImportError: cannot import name '_space_pc'`.

- [ ] **Step 3: Add `import re` (module level)**

In `freight_planner/viz_app.py`, after line 22 (`import json`) add:
```python
import re
```

- [ ] **Step 4: Implement the helpers**

Add near the other module helpers in `freight_planner/viz_app.py` (e.g. just above `def _build_validation`):

```python
def _space_pc(compact: str) -> str:
    """Re-insert the single UK postcode space: 'AL109BS' -> 'AL10 9BS'. An
    outward-only or non-unit string is returned unchanged."""
    c = str(compact or "").strip().upper().replace(" ", "")
    if len(c) >= 5 and c[-3].isdigit() and c[-2:].isalpha():
        return f"{c[:-3]} {c[-3:]}"
    return c


def _geocode_recovery(pc, cache: dict) -> dict | None:
    """Classify how a postcode resolved, from its cache entry. Returns a recovery
    descriptor {method, risk, resolved_to} for a repaired / outcode / terminated
    resolution, else None (exact unit, legacy no-source entry, miss, or empty)."""
    if not pc:
        return None
    entry = cache.get(geocode.postcode_key(pc))
    if not isinstance(entry, dict):
        return None
    source = str(entry.get("source", ""))
    if "repaired" in source:
        m = re.search(r"repaired ([A-Z0-9]+)", source)
        return {"method": "repaired", "risk": "high",
                "resolved_to": _space_pc(m.group(1)) if m else "?"}
    if str(entry.get("precision", "")) == "outcode_district" or "outcodes" in source:
        oc = str(entry.get("postcode", "") or geocode.postcode_key(pc))
        return {"method": "outcode", "risk": "high", "resolved_to": f"{oc} district centroid"}
    if "terminated" in source:
        return {"method": "terminated", "risk": "low",
                "resolved_to": str(entry.get("postcode", "") or pc)}
    return None
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/freight_planner/test_viz_app_recoveries.py -q`
Expected: PASS (5 tests).

---

## Task 2: `_collect_recoveries` + wire into `build_plan_data`

**Files:** Modify `freight_planner/viz_app.py`; Test `tests/freight_planner/test_viz_app_recoveries.py`

- [ ] **Step 1: Write failing test**

Append to `tests/freight_planner/test_viz_app_recoveries.py`:

```python
def test_collect_recoveries_dedupes_tiers_and_skips_exact():
    import pandas as pd
    from freight_planner.viz_app import _collect_recoveries
    cache = {
        "AL109B5": {"source": "postcodes.io (repaired AL109BS)"},
        "SG8": {"source": "postcodes.io/outcodes", "precision": "outcode_district", "postcode": "SG8"},
        "CB11AA": {"source": "postcodes.io", "precision": "postcode_unit"},
    }
    df = pd.DataFrame([
        {"order_id": "o1", "service_pc": "AL10 9B5", "lat": 1.0, "lon": 2.0,
         "collect_pc": "", "collect_lat": None, "collect_lon": None},
        {"order_id": "o1", "service_pc": "AL10 9B5", "lat": 1.0, "lon": 2.0,
         "collect_pc": "", "collect_lat": None, "collect_lon": None},   # duplicate
        {"order_id": "o2", "service_pc": "SG8", "lat": 3.0, "lon": 4.0,
         "collect_pc": "", "collect_lat": None, "collect_lon": None},
        {"order_id": "o3", "service_pc": "CB1 1AA", "lat": 5.0, "lon": 6.0,
         "collect_pc": "", "collect_lat": None, "collect_lon": None},   # exact -> skip
    ])
    recs = _collect_recoveries(df, cache, {"o1": "WT-A", "o2": "WT-B"})
    assert len(recs) == 2                                   # dedup'd, exact skipped
    assert {r["method"] for r in recs} == {"repaired", "outcode"}
    assert all(r["risk"] == "high" for r in recs)
    assert {r["order_name"] for r in recs} == {"WT-A", "WT-B"}
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/freight_planner/test_viz_app_recoveries.py::test_collect_recoveries_dedupes_tiers_and_skips_exact -q`
Expected: FAIL — `cannot import name '_collect_recoveries'`.

- [ ] **Step 3: Implement `_collect_recoveries`**

Add below `_geocode_recovery` in `freight_planner/viz_app.py`:

```python
def _collect_recoveries(cust_df, cache: dict, name_map: dict) -> list[dict]:
    """Whole-plan list of stops whose postcode was recovered. Scans each customer
    stop's service_pc and (two-point) collect_pc, deduped by (order_id, raw_pc),
    sorted high-risk first. Each entry carries order/name/raw/method/resolved_to/
    risk/lat/lon so the operator can eyeball the fix."""
    seen: set = set()
    out: list[dict] = []
    for r in cust_df.itertuples(index=False):
        oid = str(getattr(r, "order_id", "") or "")
        pairs = (
            (getattr(r, "service_pc", None), getattr(r, "lat", None), getattr(r, "lon", None)),
            (getattr(r, "collect_pc", None), getattr(r, "collect_lat", None), getattr(r, "collect_lon", None)),
        )
        for pc, lat, lon in pairs:
            pc = str(pc or "").strip()
            if not pc or pc.lower() == "nan":
                continue
            key = (oid, pc.upper())
            if key in seen:
                continue
            rec = _geocode_recovery(pc, cache)
            if rec is None:
                continue
            seen.add(key)
            out.append({"order_id": oid, "order_name": name_map.get(oid, ""),
                        "raw_pc": pc, "lat": _f(lat), "lon": _f(lon), **rec})
    out.sort(key=lambda x: (x["risk"] != "high", x["method"], x["order_id"]))
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/freight_planner/test_viz_app_recoveries.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Wire into `build_plan_data`**

(a) Capture the full (pre-date-filter) stops. In `freight_planner/viz_app.py`, immediately after line 279 `df = pd.read_csv(plan_dir / "route_stops.csv")` insert:
```python
    all_stops = df   # full plan (recoveries are whole-plan, not date-filtered)
```

(b) Compute recoveries where `_cache` is available. Find the line
`_cache = geocode.load_cache(DEFAULT_POSTCODE_CACHE)` (~line 392) and directly after the
`unassigned` list is built (just before `dates = sorted(...)`, ~line 414) insert:
```python
    _cust_all = (all_stops[all_stops["stop_type"].astype(str).str.contains("CUSTOMER|DIRECT", case=False, na=False)]
                 if "stop_type" in all_stops.columns else all_stops.iloc[0:0])
    recoveries = _collect_recoveries(_cust_all, _cache, name_map)
```

(c) Add to the `summary` dict (the block starting `summary = {` ~line 423), after the `"accounting_legs": ...` line:
```python
        "recoveries": {"high": sum(1 for r in recoveries if r["risk"] == "high"),
                       "low": sum(1 for r in recoveries if r["risk"] != "high")},
```

(d) Add to the returned dict (the block `return { "window": ... }` ~line 447), after the `"unassigned": unassigned,` line:
```python
        "recoveries": recoveries,
```

- [ ] **Step 6: Full viz-app tests stay green**

Run: `python -m pytest tests/freight_planner/test_viz_app_recoveries.py tests/freight_planner/test_viz_app_validation.py -q`
Expected: PASS.

---

## Task 3: Render the sidebar panel + regenerate

**Files:** Modify `freight_planner/viz_app.py` (HTML template + JS)

- [ ] **Step 1: Add the panel div**

In the `_HTML` template, after line 522 (`<div id="unassigned" class="scroll" style="max-height:150px"></div></div>`) insert:
```html
  <div class="sec" id="rec-sec" style="max-height:200px"><div class="lbl">Postcode recoveries <span id="rec-count" class="muted" style="float:right"></span></div>
    <div id="recoveries" class="scroll" style="max-height:150px"></div></div>
```

- [ ] **Step 2: Add a summary row**

In the summary IIFE innerHTML (~line 732), after the `Unplanned legs` row string, add (inside the same backtick template, before the closing `` `; ``):
```javascript
   <div class=row><span class=k>Postcode recoveries <span class=muted>(verify)</span></span><span class=v style="color:${s.recoveries&&s.recoveries.high?'#e67e22':'#9aa6c8'}">${s.recoveries?s.recoveries.high:0}${s.recoveries&&s.recoveries.low?' (+'+s.recoveries.low+')':''}</span></div>
```

- [ ] **Step 3: Add the render IIFE**

Immediately after the summary+unassigned IIFE closes (the `})();` at ~line 748) insert:
```javascript
// ── postcode recoveries (verify geocode fixes) ──
(function(){ const recs=DATA.recoveries||[];
  const hi=recs.filter(r=>r.risk==='high'), lo=recs.filter(r=>r.risk!=='high');
  document.getElementById('rec-count').textContent = hi.length + (lo.length?` (+${lo.length} retired)`:'');
  const col={outcode:'#e74c3c',repaired:'#e67e22',terminated:'#6878a0'};
  const row=r=>`<div class="ua"><span class=o>${(r.order_id||'').slice(0,8)}</span> <span class=badge style="background:${col[r.method]||'#6878a0'}">${r.method}</span> ${r.order_name||''}<br><span class=r>${r.raw_pc} → ${r.resolved_to}</span></div>`;
  const el=document.getElementById('recoveries');
  el.innerHTML = (hi.length+lo.length)
     ? (hi.length?`<div class=muted style="padding:3px 4px">— needs check —</div>`+hi.map(row).join(''):'')
       + (lo.length?`<div class=muted style="padding:5px 4px;border-top:1px solid #1d2336">— retired units (real coords, low risk) —</div>`+lo.map(row).join(''):'')
     : '<div class=muted>None</div>';
})();
```

- [ ] **Step 4: Regenerate the Jan-16 maps + eyeball**

Run:
```bash
cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics
python -m freight_planner.viz_app --plan-dir freight_planner/runs_exp_viz/2026-01/2026-01-12_to_2026-01-17/plan --date 2026-01-16 --out freight_planner/compare_jan16/jan16_optimized.html
python -m freight_planner.viz_app --plan-dir freight_planner/runs/2026-01/2026-01-12_to_2026-01-17/plan --date 2026-01-16 --out freight_planner/compare_jan16/jan16_120s.html
```
Expected: both regenerate with no error. Confirm the panel: `Postcode recoveries 2 (+22 retired)` for the optimized plan (2 repaired + 4 outcode under "needs check", 22 terminated under "retired units"). Wait — the header count is high-risk (6) + `(+22 retired)`. Verify one repaired (`AL10 9B5 → AL10 9BS`) and one outcode (`PE19 0UL → PE19 district centroid`) render readably.

- [ ] **Step 5: Full regression**

Run: `python -m pytest tests/freight_planner/ -q`
Expected: PASS (all).

---

## Self-review notes
- **Spec coverage:** classifier w/ 3 methods + risk (T1); whole-plan collect, deduped, wired to summary+return (T2); tiered sidebar section + summary row + regenerate (T3); FC excluded (classifier only reads cache source/precision, never FC map); no pipeline change. All covered.
- **Type consistency:** `_geocode_recovery` returns `{method,risk,resolved_to}`; `_collect_recoveries` spreads it + adds `order_id,order_name,raw_pc,lat,lon`; JS reads `r.risk/r.method/r.order_id/r.order_name/r.raw_pc/r.resolved_to` and `s.recoveries.high/.low` — consistent.
- **YAGNI:** no map click-to-pan, no FC aliases, no re-geocode.
- `_f` and `geocode` and `name_map` and `_cache` are all already defined/available at the wiring points.
