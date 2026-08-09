# Flow-Aware Staging-Depot Resolution — Design

**Date:** 2026-07-01
**Status:** Approved (design), pending implementation plan
**Author:** brainstormed with stakeholder

## Problem

An order's "where is the freight staged" is decided by a single field, `source_depot`,
set in [`freight_planner/legs.py`](../../../freight_planner/legs.py) from
`assign_depot(postcode)` ([`cambridge/scope.py:104`](../../../../cambridge/scope.py)).
`assign_depot` is a postcode-prefix territory map that returns `CB22`, `BEDFORD`,
`STOKE`, or **`OVERFLOW`** for anything outside a depot's territory.

For `OVERFLOW` freight, two downstream sites replace the missing depot with
`nearest_depot(work_point)` — the geographically closest depot **anchor**:

- tour bucketing, [`freight_planner/tour_plan.py:224-227`](../../../freight_planner/tour_plan.py)
- consolidation `_depot_of`, [`freight_planner/tours.py:380-382`](../../../freight_planner/tours.py)

`DEPOT_ANCHORS` includes the **Stoke ST4 satellite** (52.97°N), which is the
northernmost anchor. So **any out-of-territory delivery in Scotland or the North
resolves to Stoke** purely on proximity — even though the freight never lands there
and no Stoke vehicle serves that lane.

### Worked example (the trigger)

The consolidated Scotland tour `TOUR:…:2026-01-13` mixes two orders:

| order | flow | anchor pc | today's `source_depot` | correct |
|---|---|---|---|---|
| `0f36bf4b` (ML6, Airdrie) | FULL_FLEET | **origin** ST | STOKE | ✅ (freight really collected at Stoke) |
| `fbcb92a2` (EH48, Bathgate) | PL_IMPORT | **delivery** EH48 | OVERFLOW → nearest → **STOKE** | ❌ (import lands at a member depot, not the ST4 yard) |

The import is staged at a single-vehicle customer yard with no dock, and the whole
tour is mislabelled Stoke-anchored.

### Root cause

`source_depot` conflates two distinct ideas: **where the freight physically is**
(needed for the ledger, load-stops, honest km) and **which depot's vehicle should
run it** (a routing preference). For in-territory work they coincide; for `OVERFLOW`
far work they diverge, and the `nearest_depot` fallback silently optimises the second
while corrupting the first. The fallback is also **flow-blind** — it discards the
flow information (`PL_IMPORT` lands via the B37 Palletline gateway; `PL_EXPORT`/
`FULL_FLEET` originate in the field) that the rest of the pipeline tracks.

## Evidence (January 2026 telematics + verified legs)

Analysed the five "Stoke" vehicles (`B29BAL`, `C29BAL`, `Y888AUK`, `BU69XGK`,
`BX67ZFV`) against `supatrak_telematics_cleaned_20260101_to_20260131.csv` and
`planning_agent/verified_legs.csv` joined to the Qargo orders:

- **"Stoke" is ~1.5 vehicles, not 5.** Only `BX67ZFV` is truly ST4-anchored (30/31
  overnights); `BU69XGK` is half ST4 / half CB22. The other three overnight at
  **CB22** — they are Duxford Midlands-corridor tractors.
- **ST4 8HP works like a simplified single-vehicle base** (a customer yard, no dock).
  WS13 Lichfield recurs as a mid-corridor layover, not a depot.
- **The fleet is collection-dominated:** PL_EXPORT 163 (55%), FULL_FLEET 123 (41%),
  PL_IMPORT **8** (3%). It is a collection/trunking arm feeding the B37 hub.
- **The work is origin-driven from a single ST shipper:** PL_EXPORT origins are
  **ST in 157/163 (96%)**; FULL_FLEET origins are **ST in 111/123 (90%)**.
  Destinations spray across the Midlands/North (LE·M·B·L·YO·LS…) but origins are ST.
- **No Stoke vehicle serves Scotland** (no G/EH/ML/KA/PA/DG in any footprint).

### Consequences for the design

1. **Do not widen Stoke's territory.** Territory (`assign_depot`) keys on the
   **origin** for the flows Stoke actually runs, and those origins are already
   `ST = STOKE`. Widening Stoke to own DE/LE/WS would only newly capture *imports
   delivering into* that corridor and stage them at the dockless ST4 yard — i.e. it
   would *re-create* the bug on the delivery side. Stoke's genuine ST-origin work
   already resolves correctly and is left untouched.
2. **The fix is one lever:** out-of-territory **deliveries** must stage at a real,
   resourced gateway instead of the nearest satellite anchor.
3. Narrowing the `OVERFLOW` fallback to `{CB22, BEDFORD}` loses **no** Stoke work,
   because all real Stoke work arrives via `assign_depot = STOKE` (territory), never
   via the `OVERFLOW` fallback. The fallback only ever fires for genuinely
   out-of-all-territory points (far), where Stoke should not stage anyway.

## Design

### Component: `resolve_staging_depot` (new, in `cambridge/scope.py`)

A pure helper — the single place a postcode becomes a **real** staging depot.
`assign_depot` is unchanged (still the territory authority, still returns `OVERFLOW`).

```python
GATEWAY_DEPOTS = ("CB22", "BEDFORD")   # capable dock/trunk gateways;
                                       # ST4 satellite + empty ST_IVES are NOT eligible

def resolve_staging_depot(pc, *, is_delivery_anchor, lat=None, lon=None) -> str:
    d = assign_depot(pc)
    if d != "OVERFLOW":
        return d                         # in-territory → unchanged (incl. STOKE)
    if is_delivery_anchor:
        return "CB22"                    # dock gateway, capability-primary
    if lat is not None and lon is not None:
        return _nearest_gateway(lat, lon)  # collecting vehicle's nearest capable base
    return "CB22"                        # safe default when coords unknown
```

`_nearest_gateway(lat, lon)` = `nearest_depot(lat, lon, anchors={k: DEPOT_ANCHORS[k]
for k in GATEWAY_DEPOTS})` — nearest of CB22/Bedford only.

**Rule rationale.** The branch is on *anchor semantics*, not flow name:

- A **delivery** anchor (import last-mile) needs a handling dock → `CB22` (biggest,
  most trucks — stakeholder's capability call; CB22/Bedford are ~40 km apart so
  distance is negligible on a 500 km+ run).
- A **collection** anchor (export / full-fleet pickup) needs the nearest capable
  base to the collection point → nearest of `{CB22, BEDFORD}`.

### Wiring: `freight_planner/legs.py`

Replace the two module-level lines that compute the depots
([`legs.py:273-274`](../../../freight_planner/legs.py)):

```python
origin_depot = depot_for_pc(origin_pc)   # was: assign_depot(clean_pc(origin_pc))
dest_depot   = depot_for_pc(dest_pc)
```

with resolver calls:

```python
o_lat, o_lon = latlon(origin_pc, postcode_cache)          # hoisted to top
origin_depot = resolve_staging_depot(origin_pc, is_delivery_anchor=False,
                                     lat=o_lat, lon=o_lon)
dest_depot   = resolve_staging_depot(dest_pc,   is_delivery_anchor=True)
```

Every existing `source_depot=…` / `target_depot=…` site keeps its **exact current
variable** — this is a pure value swap, not a re-think of any site. (`source_depot`
is `origin_depot` for FULL_FLEET/PL_EXPORT/LOCAL_COLLECT legs and `dest_depot` for
PL_IMPORT/LOCAL_DELIVER legs; `target_depot` also uses `dest_depot` on the FULL_FLEET
DIRECT/DELIVERY legs, but `target_depot` is only ledger-critical on pickups, where it
equals `origin_depot`.) Because `resolve_staging_depot` returns exactly what
`assign_depot` returned for every in-territory postcode, **in-territory behaviour is
byte-identical**; only postcodes that were `OVERFLOW` change to a real gateway.
Net effect: legs never emit `"OVERFLOW"` as a dispatchable staging depot again.

Notes:
- `resolve_staging_depot` receives an **already-cleaned** postcode (legs.py cleans
  `origin_pc`/`dest_pc` via `clean_pc` before this point), and calls `assign_depot`
  directly — no double-clean. Unit tests pass clean postcodes.
- `dest_depot` needs no coords: a delivery anchor always resolves to `CB22` on
  `OVERFLOW`, so `_nearest_gateway` is never called for it.
- The `o_lat, o_lon` hoist replaces the several per-branch `latlon(origin_pc, …)`
  calls already present; the postcode cache makes it free.

### Wiring: `freight_planner/tour_plan.py` (one line)

Today the tour bucketer trusts `source_depot` **only for FULL_FLEET**
([`tour_plan.py:224`](../../../freight_planner/tour_plan.py)):

```python
if str(_g(row, "flow", "")) == "FULL_FLEET" and src in DEPOT_ANCHORS:
    depot = src
else:
    depot = nearest_depot(c[0], c[1])[0]
```

Drop the flow condition so any real anchor is trusted (imports now carry a real
gateway):

```python
depot = src if src in DEPOT_ANCHORS else nearest_depot(c[0], c[1])[0]
```

### No change: `freight_planner/tours.py` `_depot_of`

`_depot_of` ([`tours.py:382`](../../../freight_planner/tours.py)) already reads
`d if d in anchors else nearest_depot(...)`. Once `source_depot` is always real, its
`nearest_depot` branch stops firing on its own; keep it as a safety net.

## Data flow after the change (Scotland tour)

| order | anchor | after | tour outcome |
|---|---|---|---|
| EH48 (import) | delivery, OVERFLOW | **CB22** | CB22-primary tour (with KA1/KA6) |
| ML6 (FF) | origin ST | **STOKE** (unchanged) | genuine Stoke **load-stop** |

`resolve_cluster` sees `delivery_depots = {CB22, STOKE}`, picks `CB22` primary (most
pallets), and emits a real Stoke load-stop for ML6 only — the physically correct
picture: the CB22 vehicle collects ML6 at Stoke en route north.

## Edge cases

- **Origin geocode fails / no coords** → collection anchor returns `CB22` default
  (BAD_GEOCODE is already hard-blocked upstream; this is defensive).
- **Empty postcode** → `assign_depot` returns `OVERFLOW` → gateway rule applies.
- **ST/TF/CW origin** → `assign_depot = STOKE` (real) → returned as-is; Stoke work
  preserved.
- **ST_IVES** → never returned by `assign_depot`, never in `GATEWAY_DEPOTS`; cannot
  become a staging depot (matches today).
- **Daily (non-tour) OVERFLOW** → now stages at a real gateway in the ledger instead
  of the virtual `"OVERFLOW"` string; daily OVERFLOW jobs are near-a-depot by
  definition, so gateway ≈ nearest and the ledger stays consistent.

## Testing (TDD)

**Unit — `resolve_staging_depot`:**
- EH48 (Scotland) delivery anchor → `CB22`
- ST-area origin (collection) → `STOKE` (unchanged)
- CB origin → `CB22` (unchanged)
- MK delivery (in-territory import) → `BEDFORD` (unchanged)
- far origin OVERFLOW, coords nearer Bedford → `BEDFORD`; nearer CB22 → `CB22`
- OVERFLOW collection with no coords → `CB22`

**Integration:**
- Synthetic Scotland PL_IMPORT → leg `source_depot == "CB22"` (not `OVERFLOW`)
- `tour_plan` buckets a non-FULL_FLEET import at its real `src` (regression for the
  dropped flow condition)

**End-to-end (validation on 2026-01-12…17):**
- EH48 no longer stages at Stoke; the Scotland consolidated tour anchors CB22 with a
  Stoke load-stop for ML6
- coverage stays ≥ 99.3% (no regression)
- report km delta; assert no new `NO_FEASIBLE_TOUR`

## Scope / files

- `cambridge/scope.py` — add `GATEWAY_DEPOTS`, `_nearest_gateway`,
  `resolve_staging_depot`; `assign_depot` unchanged.
- `freight_planner/legs.py` — hoist origin coords; swap the two depot computations.
- `freight_planner/tour_plan.py` — drop the FULL_FLEET-only condition (one line).
- Tests: `tests/cambridge/test_scope*` (or new `test_staging_depot`),
  `tests/freight_planner/test_legs*`, `tests/freight_planner/test_tour_plan*`.
- `freight_planner/QUEST_LOG.md` + memory note.

Out of scope (deliberately): widening Stoke territory; splitting `source_depot` into
`staging_depot` vs `run_from_depot`; the eval-anchor-vs-vehicle-home mismatch (H14).

## Deferred

- **Origin-side gateway = CB22-for-everything.** If capability pressure (mass
  `NO_FEASIBLE_TOUR`/unassigned from far-origin OVERFLOW concentrating on one gateway)
  appears, collapse the collection-side rule to `CB22` too. Not now — genuine
  far-origin OVERFLOW is empirically ~nil (96% of collections originate at ST).

## Constraints

- **No `git commit` this session** (standing stakeholder instruction) — write files
  only, including this spec.
- Viz regeneration, if any, is `trip_app` (`viz_app.py`) only; skip the folium maps.
