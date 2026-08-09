# Postcode-Recovery Panel in the Trip Viz — Design

**Date:** 2026-07-05
**Status:** Approved (design)
**Scope:** `freight_planner/viz_app.py` only. No pipeline change, no re-run.

## Problem

The geocode chain silently *recovers* postcodes that would otherwise be
`BAD_GEOCODE`: it substitutes visual-twin characters (structural repair), falls
back to a district centroid (outcode), or resolves a retired unit (terminated).
These recoveries are invisible in the plan and the viz, so a wrong fix — a repaired
postcode that points at a different address, or an outcode centroid kilometres from
the real drop — cannot be caught. The operator needs to *see every recovery and
manually verify it*.

## Decisions (from brainstorming)

- **Surface all recoveries, tiered by risk** — outcode + repaired (HIGH, need
  scrutiny) prominent; terminated (LOW, retired units resolve to real coords) below.
- **Viz-side classification from the postcode cache.** The recovery signal already
  lives in `postcode_cache.json` per entry (`source`/`precision`), and
  `build_plan_data` already loads that cache ([viz_app.py:392]). Zero pipeline
  change; works on every existing plan the moment the viz is regenerated.
- **FC-code aliases are out of scope** — a curated, deterministic mapping
  (`FC_CODE_ALIASES`), not a guess to verify.
- **Whole-plan scope** — the recoveries list spans the whole plan dir, consistent
  with the existing unassigned panel (which is not date-filtered).

## The recovery signal (already in the cache)

`geocode.geocode` records how each postcode resolved in its cache entry
(`{lat, lon, source, precision, postcode?}`):

| method | cache signal | risk | example |
|---|---|---|---|
| repaired | `source` contains `"repaired X"` | HIGH | `AL10 9B5 → AL10 9BS` |
| outcode | `precision == "outcode_district"` (or `source` has `"outcodes"`) | HIGH | `SG8 → SG8 centroid` |
| terminated | `source` contains `"terminated"` | LOW | `AL7 1RR` (retired unit) |
| exact / legacy-no-source | anything else | — | not a recovery |

Jan 12–17 plan magnitude: 4 outcode, 2 repaired, 22 terminated (28 stops).

## Components (all in `viz_app.py`)

### 1. Classifier — `_geocode_recovery(pc, cache) -> dict | None` (pure)

```python
def _geocode_recovery(pc, cache):
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

`_space_pc(compact)` re-inserts the single UK space (`compact[:-3] + " " + compact[-3:]`
when ≥ 5 chars and the tail is a valid inward code; else returns the input).

### 2. Data — `_collect_recoveries(cust_df, cache, name_map) -> list[dict]` (pure, testable)

Iterate the customer stops. For each, classify `service_pc` and (for two-point moves)
`collect_pc`. Dedupe by `(order_id, raw_pc)`. Each entry:
`{order_id, order_name, raw_pc, method, resolved_to, risk, lat, lon}`.
`build_plan_data` calls it over the customer-stop rows (it already has the cust frame,
the loaded `_cache`, and `name_map`), sets `data["recoveries"]` and a summary count
`summary["recoveries"] = {"high": h, "low": l}`.

### 3. Render — "Postcode recoveries (N)" section in the bottom-left panel

A new `<div id="recoveries">` directly under the existing `#unassigned` div. JS
splits `DATA.recoveries` into `high` and `low`:
- **⚠ needs check** (outcode + repaired) — warning colour; row =
  `<method badge> <order8> · <raw> → <resolved_to>`.
- **· retired units** (terminated) — muted grey, under a divider.
- Header shows the count; the summary block gains a "Postcode recoveries" line
  (`h needs-check + l retired`).
Follows the existing unassigned-panel markup/scroll pattern
([viz_app.py:522, 731-738]).

## Data flow

```
route_stops.csv (service_pc/collect_pc, order_id, lat/lon)
postcode_cache.json (source/precision)           ┐
   build_plan_data ──_collect_recoveries()──► data["recoveries"] ──► #recoveries panel
```

## Testing (TDD)

- **`_geocode_recovery`**: repaired entry → `{method:"repaired", resolved_to:"AL10 9BS"}`;
  outcode entry (`precision:"outcode_district"`) → `{method:"outcode"}`; terminated →
  `{method:"terminated", risk:"low"}`; exact unit → `None`; legacy list/`None` entry →
  `None`; empty pc → `None`.
- **`_space_pc`**: `"AL109BS" → "AL10 9BS"`; `"SG8" → "SG8"` (no inward, unchanged).
- **`_collect_recoveries`**: a small stops DataFrame + cache dict → the right entries,
  deduped by `(order_id, raw_pc)`, `order_name` filled from `name_map`, two-point
  `collect_pc` classified too.
- Render is verified by regenerating a viz and eyeballing (JS template string — not
  unit-tested), consistent with the rest of `viz_app`.

## Validation

Regenerate the Jan 12–17 (or Jan 16) viz; confirm the panel shows 2 repaired + 4
outcode under "needs check" and 22 terminated under "retired units", each with a
readable `raw → resolved_to`. Cross-check one repaired (`AL10 9B5 → AL10 9BS`) and one
outcode (`PE19 0UL → PE19 centroid`) by eye.

## Non-goals (YAGNI)

- No `geocode_method` column in `route_stops.csv` (would need a pipeline re-run).
- No FC-alias surfacing (curated mapping, not a fix to verify).
- No map click-to-pan from a recovery row (nice follow-up, not v1).
- No re-geocoding — the cache is read as-is (whatever resolved the plan).
