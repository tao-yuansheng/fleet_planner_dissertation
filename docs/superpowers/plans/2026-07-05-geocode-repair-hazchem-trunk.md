# Geocode Structural Repair + Hazchem LE10 Trunk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> **STANDING RULES:** NO git commands ever. Tests from
> `e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics` with `python -m pytest`.

**Goal:** (A) resolve the BAD_GEOCODE tail with a UK-postcode-structure repair +
outcode-centroid fallback; (B) model the verified nightly CB22↔LE10 Hazchem trunk
alongside the B37 services.

**Stakeholder design (2026-07-05 conversation — no spec doc):**
- Geocode: use the UK postcode STRUCTURE (regex) to know which character is wrong.
  Inward part is always `digit letter letter`; outward is `area letters + district
  digits(+letter)`. A letter in a digit slot (O/I/S/B) or digit in a letter slot
  (0/1/5/8) is repaired by the visual-twin substitution AT THAT POSITION ONLY, then
  verified against live+terminated. Structurally-valid-but-unissued units (PE19 0UL
  — real sector, 404 both endpoints) and district-only values (SG8, CO10) fall back
  to the postcodes.io `/outcodes/{outcode}` centroid, cached with an honest
  `precision: "outcode_district"` marker.
- Trunk: telematics-verified — LE10 3BS gets ~1 CB22 tractor per weeknight (20
  reg-nights in Jan, 98% of pings 18:00-06:00, ALL visitors CB22-homed; Bedford:
  zero — hazchem origins are 679 CB22-territory vs 34 Bedford-ish of 1,623/month).
  Model: a second trunk service keyed on the leg `hub` field (`LE10_HUB`), CB22
  ONLY, `trips = max(1, ceil(max(imp, exp)/52))` on nights with ANY hazchem
  freight; shares the tractor-draw rotation (one trip per tractor-night total).

**Verified code facts:**
- `freight_planner/geocode.py`: chain is cache → live postcodes.io → terminated
  (`_lookup(url, source)` helper, `geocode()` at :156, `is_non_standard` guard,
  `_NETWORK` flag via `set_network_enabled`). Cache entries carry
  lat/lon/source/precision/postcode. Tests in tests/freight_planner/test_geocode.py
  monkeypatch `_lookup` — no network in tests.
- BAD_GEOCODE is decided by `legs.geocode_ok(pc, cache)` → `geocode.geocode_ok` →
  the same chain, so fixing geocode() fixes the pipeline end-to-end.
- Known cases: `MK43 OYL`→`MK43 0YL` (cached, valid), `AL10 9B5`→`AL10 9BS`
  (cached, valid), `MK41 9JJ`/`PE19 0UL` (404 live+terminated; sectors exist),
  `SG8`/`CO10` (district-only). Outcode centroids verified live for all four.
- Leg rows carry `hub` ("B37_HUB"/"LE10_HUB", set by `legs.hub_for_row` — hazchem
  in subcontractor/import-type → LE10_HUB); candidate rows DO NOT carry hub yet —
  add the passthrough in `jobs.py` exactly as `flow` is carried (field on
  CandidateJobRecord + `hub=str(row.get("hub") or "")` — see jobs.py:32/:179 for
  the flow pattern).
- `trunk.py`: TrunkNight(depot, night, import_pallets, export_pallets, trips, km);
  `trunk_schedule(candidates, window_start, window_end, roundtrip_km)`;
  `draw_tractors(nights, vehicle_df, reserved)` with LRU rotation; NaN-guarded
  `_pallets`; `_EPS`; weeknight filter; TRUNK_DEPOTS=(BEDFORD, CB22).
- `tour_plan.py`: B37_LATLON constant; `roundtrip_km` computed per depot via
  `2*road_km(*DEPOT_ANCHORS[d], *B37_LATLON)`; schedule+draw between tour
  reservation and daily seed.
- LE10 3BS coordinates: geocode cache has it (it is `legs.HUB_POSTCODE[LE10_HUB]`);
  pin the constant in a test against `DEFAULT_POSTCODE_CACHE` (load, compare, 1e-3
  tolerance).
- Reporting: run_alns trunk log block (~:287-306); trunk_schedule.csv written by
  reports.write_reports; viz_app `_build_trunk()` reads it and ships depot lines
  with geometry to a single `hub` latlon — needs per-row hub now.
- Suite baseline: 416 green (tests/freight_planner) + 76 (tests/cambridge/test_scope.py).

---

### Task A: Geocode structural repair + outcode fallback (TDD)

**Files:** modify `freight_planner/geocode.py`; append tests to
`tests/freight_planner/test_geocode.py`.

- [ ] **A.1 Failing tests** (monkeypatch `_lookup` per the file's existing style; the
  fake lookup answers only exact known postcodes and the outcode URL):

```python
def test_repair_letter_in_digit_slot():
    # "MK43 OYL": inward "OYL" has O in the digit slot -> try "MK43 0YL" -> valid
def test_repair_digit_in_letter_slot():
    # "AL10 9B5": inward "9B5" has 5 in a letter slot -> "AL10 9BS" -> valid
def test_repair_only_at_violating_positions():
    # a VALID postcode containing O/S in legitimate letter slots is never mutated
    # (e.g. "SO16 0AS" queried as-is, no substitution attempts)
def test_unissued_unit_falls_back_to_outcode_centroid():
    # "PE19 0UL": structurally valid, live+terminated 404 -> /outcodes/PE19
    # centroid, cached with precision "outcode_district"
def test_district_only_input_uses_outcode():
    # "SG8" -> outcode centroid directly (no unit lookups attempted)
def test_repair_result_cached_under_original_key():
    # second call for "MK43 OYL" hits cache, zero lookups (count fake calls)
def test_network_off_behaves_as_before():
    # set_network_enabled(False): cache-only, no repair/outcode attempts
```

- [ ] **A.2 Implement.** In geocode.py:
  - `_UK_RE = re.compile(r"^([A-Z]{1,2})([0-9][0-9A-Z]?) ?([0-9])([A-Z]{2})$")`
    (outward area letters, district, inward digit, inward letters) plus an
    outward-only form for district inputs `^[A-Z]{1,2}[0-9][0-9A-Z]?$`.
  - `_structural_repairs(pc) -> list[str]`: parse with a LENIENT pattern that
    allows the twin characters in each slot, then substitute per-position using
    `_TO_DIGIT = {"O":"0","I":"1","S":"5","B":"8"}` / `_TO_LETTER` (inverse) ONLY
    at positions whose character class violates the strict pattern; return the
    repaired candidates (usually one). No repair when the strict pattern already
    matches.
  - `_lookup_outcode(outcode)` against `https://api.postcodes.io/outcodes/{outcode}`
    → cache_entry with `precision="outcode_district"`, source "postcodes.io/outcodes".
  - Extend `geocode()`: cache → live → terminated → (if strict-invalid) repaired
    candidates via live→terminated → (if still unresolved and outward parses)
    outcode centroid → give up. Every resolution cached under the ORIGINAL key
    (source field records the path, e.g. "postcodes.io (repaired MK43 0YL)").
    Respect `_NETWORK` and `is_non_standard` exactly as today.
- [ ] **A.3** All test_geocode green; full tests/freight_planner green (416+new).
  NOTE: `tests/test_postcode_resolver.py` (old simulation module) is pre-existing
  drift — do not touch it.

### Task B: Hazchem LE10 trunk (TDD)

**Files:** modify `freight_planner/jobs.py` (hub passthrough),
`freight_planner/trunk.py`, `freight_planner/tour_plan.py`,
`freight_planner/run_alns.py` (log line), `freight_planner/viz_app.py` (per-row
hub); tests: test_jobs* (hub passthrough), test_trunk.py, test_tour_plan.py.

- [ ] **B.1 jobs.py:** `hub` passthrough (mirror `flow`). Test: candidate row
  carries the leg's hub.
- [ ] **B.2 trunk.py (TDD):**
  - `TrunkNight` gains `hub: str = "B37_HUB"`.
  - `trunk_schedule` groups by (hub, depot, night): rows with hub == "LE10_HUB"
    route to a single CB22 service (depot forced to "CB22" REGARDLESS of the
    row's own depot — telematics: CB22-only; comment the evidence); all other
    hub values (B37_HUB or empty) keep today's per-depot behavior.
    LE10 trips = `max(1, ceil(max(imp, exp)/52 - eps))` when the night has ANY
    hazchem pallets > 0 (the service runs nightly at min one tractor); km uses
    `roundtrip_km[("CB22", "LE10_HUB")]` — CHANGE the roundtrip_km contract to be
    keyed by (depot, hub) tuples (update B37 call sites accordingly).
  - `draw_tractors` unchanged in logic (nights now include LE10 entries; the
    shared LRU + one-trip-per-tractor-night already prevents a tractor doing B37
    and LE10 the same night — add a test proving it: CB22 night with 1 B37 trip +
    1 LE10 trip and two tractors -> two DIFFERENT tractors drawn).
  - Tests: LE10 grouping/forcing to CB22, min-1 sizing (5 pallets -> 1 trip),
    ceil above 52, zero-hazchem night -> no LE10 entry, mixed-night draw
    distinctness, hub in TrunkNight rows.
- [ ] **B.3 tour_plan.py:** add `LE10_LATLON` constant (value from the postcode
  cache entry for "LE10 3BS" — pin with a test comparing against
  DEFAULT_POSTCODE_CACHE at 1e-3); build `roundtrip_km = {("BEDFORD","B37_HUB"):
  ..., ("CB22","B37_HUB"): ..., ("CB22","LE10_HUB"): 2*road_km(*DEPOT_ANCHORS["CB22"],
  *LE10_LATLON)}`. Integration test: fixture with hazchem-hub candidates at CB22
  -> result.trunk has an LE10 night.
- [ ] **B.4 reporting/viz:** trunk_schedule.csv gains `hub` column (reports.py
  writer + its test); run_alns log block groups by hub:
  `trunk: B37: BEDFORD 16/5n 3,859 km, CB22 17/5n 5,361 km | LE10: CB22 5/5n 1,100 km`
  (adjust the existing per-depot aggregation to per (hub, depot)); viz_app
  `_build_trunk` emits per-(depot, hub) entries each with its own hub latlon
  (map "B37_HUB"->(52.4666,-1.7226), "LE10_HUB"->LE10_LATLON) and geometry;
  tooltip "TRUNK {depot}→{hub_short}: ...". KPI totals unchanged (they sum all
  services).
- [ ] **B.5** Full suites green; FP_ALNS_CONSERVE smoke on test_alns.

### Task C: Validation (controller inline — NOT a subagent)

- [ ] Rerun wk1+wk2 once each. Expect: BAD_GEOCODE tail 0 (watch coverage tick up
  ~2 orders/wk); LE10 line ≈5 trips/~1.1k km per week; B37 lines unchanged
  (~within noise); combined-vs-reality restated; regenerate both trip apps and
  verify the LE10 line renders.
