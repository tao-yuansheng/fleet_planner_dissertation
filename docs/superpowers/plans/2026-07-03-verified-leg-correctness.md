# Verified-Leg Correctness Fix — Implementation Plan

> **SHIPPED 2026-07-03, with a calibration change decided during the measurement run
> (Task 7):** MATCH_RADIUS_M was set to **500m** (not 250m — the measurement showed 250m
> rejected ~45% of legitimate offset stops at postcode-centroid geocodes). And **Task 5's
> `_should_demote_full_fleet` predicate was SUPERSEDED**: instead of "keep FULL_FLEET on a
> one-end match unless provably solo-elsewhere," the shipped rule is **two-end-only** —
> FULL_FLEET is asserted only when telematics confirms BOTH ends; a one-end match records
> just that single leg. (Rationale: promoting one-end/trust-the-booking cases to FULL_FLEET
> modelled them as directs and pushed combined km to +24% vs odometer; two-end-only keeps
> the ~391 hard-evidence directs and lands combined at +8%/+7%.) So WT254741 ships as
> **DELIVERY**, not FULL_FLEET. The Task 5 section below is retained as historical record;
> the current tests assert the two-end-only behaviour.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.
> **STANDING RULES:** NO git commits, ever (no `git add`/`git commit`). Run tests from
> `e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics` with `python -m pytest`. There are no
> "commit" steps in this plan by design; each task ends by verifying the suite is green.

**Goal:** Make the telematics leg-verifier match stops to order endpoints by GPS distance
(not an asymmetric postcode prefix) and stop demoting FULL_FLEET bookings from
non-observation at shared shippers, then rebuild `verified_legs.csv` with a preserved
baseline and an order-by-order diff.

**Architecture:** All logic lives in `planning_agent/verify_legs.py`. We (1) carry per-ping
lat/lon into the telematics indexes, (2) add a distance-first match primitive with a
symmetric sector-postcode fallback, (3) precompute per-(endpoint, date) order counts to
classify endpoints as shared/unique, (4) gate substitute matches to unique endpoints, and
(5) restrict FULL_FLEET demotion to a strict, provable single-vehicle case. Consumers
(`cambridge/verified_legs.py`, `freight_planner/enrich.py`) are unchanged — same CSV schema.

**Tech Stack:** Python, pandas, numpy, pytest. Source of truth spec:
`docs/superpowers/specs/2026-07-03-verified-leg-correctness-design.md`.

---

## Context the engineer needs

- `planning_agent/verify_legs.py` classifies each order's leg. The decision stack in
  `classify_leg` is: (1) assigned-vehicle telematics match at both/one end, (2) substitute
  fleet vehicle match, (3) structural shipment-vs-vehicle rule, (4) distance/api inference.
- **Current index shapes** (these change in Task 1):
  - `build_indexes(telem)` returns `(vidx, garr, depot_sectors)` where
    `vidx[reg] = (times: np.datetime64[], pcns: str[])` and
    `garr = (g_time, g_pcn, g_reg)`.
- **Current match helpers** (change in Task 1–2):
  - `_pc_matches(order_pc, telem_pc, depot_area) -> bool` uses `order_pc.startswith(telem_pc)`.
  - `_stopped_at(times, pcns, order_pc, depot_area, ts, window_min) -> np.datetime64|None`.
  - `_any_fleet_at(garr, order_pc, depot_area, ts, exclude_regs, window_min) -> (reg, time)`.
  - Call sites in `classify_leg`: `_stopped_at(*vidx[r], o_pc, o_depot, o_ts, o_win)` and
    `_any_fleet_at(garr, o_pc, o_depot, o_ts, set(regs), o_win)`.
- **Coords:** `_cached_coords(pc_str, cache)` returns a dict `{"lat","lon"}` or a
  `(lat, lon)` tuple or `None`. `resolve_fc_alias` normalises FC/hub codes first. The
  existing `_distance_pred` shows the exact usage pattern.
- **Postcode normalise:** `_norm(pc)` (order side, resolves FC alias) and `_norm_raw(pc)`
  (telematics side) both return compact uppercase; `_sector_len(pc)` = `max(3, len(outward)+1)`.
- **Existing tests** in `tests/planning_agent/test_verify_legs.py` include
  `test_pc_matches_requires_sector_precision`, `test_pc_matches_short_outward_sector_is_four_chars`,
  `test_build_indexes_keeps_short_outward_sector_ping` (this one unpacks `vidx["X8RNW"]` as a
  2-tuple and MUST be updated in Task 1), and `test_stopped_at_returns_matched_time` (updated
  in Task 2).
- **Constants to add** near the other module constants (after `DEPOT_RADIUS_M`):
  ```python
  MATCH_RADIUS_M = 250.0        # GPS distance that counts as "stopped at" an endpoint
  SHARED_MIN_ORDERS = 5         # >= this many distinct orders/day at an endpoint => shared
  ```

## File structure

- **Modify only** `planning_agent/verify_legs.py` — matching helpers, index builder,
  shared-ness precompute, `classify_leg`, and `main()` (baseline snapshot + counts wiring).
- **Create** `planning_agent/diff_verified_legs.py` — standalone order-by-order diff script.
- **Modify** `tests/planning_agent/test_verify_legs.py` — update the two behaviour-encoding
  tests noted above; add new tests per task.
- **Create** `tests/planning_agent/test_diff_verified_legs.py` — diff script tests.

---

### Task 1: Carry lat/lon into the indexes; symmetric sector `_pc_matches`

**Files:**
- Modify: `planning_agent/verify_legs.py` (`build_indexes`, `_pc_matches`, constants)
- Test: `tests/planning_agent/test_verify_legs.py`

- [ ] **Step 1: Write the failing test for the symmetric sector match**

Add to the test file:

```python
def test_pc_matches_same_sector_different_unit():
    # The WT254741 bug: telematics reverse-geocoded the delivery to SS6 7UA, the order
    # is SS6 7NG. Same sector (SS6 7), different unit. The OLD startswith logic rejected
    # it; sector-symmetric matching accepts it.
    assert V._pc_matches("SS67NG", "SS67UA", depot_area=False) is True
    assert V._pc_matches("SS67NG", "SS67UA", depot_area=True) is False  # depot needs first-5 equal
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/planning_agent/test_verify_legs.py::test_pc_matches_same_sector_different_unit -v`
Expected: FAIL (`assert False is True`) — startswith rejects SS67UA.

- [ ] **Step 3: Make `_pc_matches` compare at sector precision symmetrically**

Replace the body of `_pc_matches` (currently the `startswith` version) with:

```python
def _pc_matches(order_pc: str, telem_pc: str, depot_area: bool) -> bool:
    """Does a telematics stop postcode confirm the order's point? Sector-precision,
    symmetric: both are truncated to the order's SECTOR (outward + first inward digit)
    and compared for equality, provided the telematics code is at least sector-length.
    A depot-area point still needs a first-5 (finer) match so a depot stop can't
    masquerade as a neighbour in the same outward."""
    if not order_pc or not telem_pc:
        return False
    if depot_area:
        return len(telem_pc) >= 5 and order_pc[:5] == telem_pc[:5]
    n = _sector_len(order_pc)
    return len(telem_pc) >= n and order_pc[:n] == telem_pc[:n]
```

- [ ] **Step 4: Run the whole match-test group to verify green**

Run: `python -m pytest tests/planning_agent/test_verify_legs.py -k pc_matches -v`
Expected: PASS for `test_pc_matches_requires_sector_precision`,
`test_pc_matches_short_outward_sector_is_four_chars`, and the new
`test_pc_matches_same_sector_different_unit`.
(Rationale: all three prior assertions hold under symmetric truncation — verified: CB22
len4<5→False, CB224 equal→True, KA65 equal→True, KA6 len3<4→False.)

- [ ] **Step 5: Update `test_build_indexes_keeps_short_outward_sector_ping` for the new tuple shape**

The index now carries coords. Change the unpack in that existing test from:

```python
    _times, pcns = vidx["X8RNW"]
```
to:
```python
    _times, pcns, _lats, _lons = vidx["X8RNW"]
```

- [ ] **Step 6: Add lat/lon arrays to `build_indexes`**

In `build_indexes`, after `t = t.sort_values("LocalTime")`, add float coord arrays and
extend both the per-vehicle index and the global arrays:

```python
    t["lat"] = pd.to_numeric(t["Latitude"], errors="coerce")
    t["lon"] = pd.to_numeric(t["Longitude"], errors="coerce")

    vidx = {}
    for reg, g in t.groupby("reg"):
        vidx[reg] = (
            g["LocalTime"].to_numpy("datetime64[ns]"),
            g["pcn"].to_numpy(),
            g["lat"].to_numpy(dtype=float),
            g["lon"].to_numpy(dtype=float),
        )
    g_time = t["LocalTime"].to_numpy("datetime64[ns]")
    g_pcn = t["pcn"].to_numpy()
    g_reg = t["reg"].to_numpy()
    g_lat = t["lat"].to_numpy(dtype=float)
    g_lon = t["lon"].to_numpy(dtype=float)
```

Then change the global-arrays return from `(g_time, g_pcn, g_reg)` to
`(g_time, g_pcn, g_reg, g_lat, g_lon)`. Leave the depot-sector block unchanged (it already
computes its own `lat_a`/`lon_a`).

- [ ] **Step 7: Run the index test to verify green**

Run: `python -m pytest tests/planning_agent/test_verify_legs.py::test_build_indexes_keeps_short_outward_sector_ping -v`
Expected: PASS (4-tuple unpack, `"KA65" in set(pcns)`).

- [ ] **Step 8: Add the two new constants**

After `DEPOT_RADIUS_M = 350.0` add:

```python
MATCH_RADIUS_M = 250.0        # GPS distance that counts as "stopped at" an endpoint
SHARED_MIN_ORDERS = 5         # >= this many distinct orders/day at an endpoint => shared
```

- [ ] **Step 9: Verify the file still imports and the suite is green so far**

Run: `python -m pytest tests/planning_agent/test_verify_legs.py -v`
Expected: PASS. NOTE: `_stopped_at`/`_any_fleet_at` still take the old positional args and
are still called with `*vidx[r]` (now a 4-tuple) — this WILL break `test_stopped_at_returns_matched_time`
and the classify path. That is fixed in Task 2. If the runner stops at that one test,
that is expected; proceed to Task 2. (If you prefer a fully-green gate, run
`python -m pytest tests/planning_agent/test_verify_legs.py -k "pc_matches or build_indexes" -v`
which must be fully green here.)

---

### Task 2: Distance-first match in `_stopped_at` and `_any_fleet_at`

**Files:**
- Modify: `planning_agent/verify_legs.py` (`_stopped_at`, `_any_fleet_at`, add `_endpoint_coords`, `_within_m`)
- Test: `tests/planning_agent/test_verify_legs.py`

- [ ] **Step 1: Write failing tests for distance-first matching**

```python
def test_stopped_at_matches_by_gps_distance_within_radius():
    # Ping is 147m from the endpoint but its reverse-geocoded postcode is a DIFFERENT
    # unit in the same sector. Distance-first matching accepts it.
    times = np.array(["2026-01-13T06:49"], dtype="datetime64[ns]")
    pcns = np.array(["SS67UA"])                       # wrong unit
    lats = np.array([51.590088]); lons = np.array([0.568119 + 0.0021])  # ~147 m east
    hit = V._stopped_at(times, pcns, lats, lons, "SS67NG", (51.590088, 0.568119),
                        False, pd.Timestamp("2026-01-13 06:49"), 120)
    assert hit == np.datetime64("2026-01-13T06:49")

def test_stopped_at_rejects_far_gps_even_if_postcode_string_close():
    times = np.array(["2026-01-13T06:49"], dtype="datetime64[ns]")
    pcns = np.array(["ZZ999"])
    lats = np.array([53.0]); lons = np.array([-2.0])   # far away
    hit = V._stopped_at(times, pcns, lats, lons, "SS67NG", (51.590088, 0.568119),
                        False, pd.Timestamp("2026-01-13 06:49"), 120)
    assert hit is None

def test_stopped_at_falls_back_to_sector_when_no_coords():
    # endpoint coords None (geocode gap) OR ping coords NaN -> sector postcode fallback
    times = np.array(["2026-01-15T10:00"], dtype="datetime64[ns]")
    pcns = np.array(["CB224"])
    lats = np.array([np.nan]); lons = np.array([np.nan])
    hit = V._stopped_at(times, pcns, lats, lons, "CB224PS", None,
                        False, pd.Timestamp("2026-01-15 10:00"), 120)
    assert hit == np.datetime64("2026-01-15T10:00")
```

Update the OLD test `test_stopped_at_returns_matched_time` to the new signature (coords
present, sector fallback path since we pass endpoint coords None):

```python
def test_stopped_at_returns_matched_time():
    times = np.array(["2026-01-15T10:00", "2026-01-15T10:30"], dtype="datetime64[ns]")
    pcns = np.array(["CB224", "ZZ999"])
    lats = np.array([np.nan, np.nan]); lons = np.array([np.nan, np.nan])
    hit = V._stopped_at(times, pcns, lats, lons, "CB224PS", None, False,
                        pd.Timestamp("2026-01-15 10:15"), 120)
    assert hit == np.datetime64("2026-01-15T10:00")
    assert V._stopped_at(times, pcns, lats, lons, "XX111", None, False,
                        pd.Timestamp("2026-01-15 10:15"), 120) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/planning_agent/test_verify_legs.py -k stopped_at -v`
Expected: FAIL (signature mismatch / `_stopped_at` takes old positional args).

- [ ] **Step 3: Add coord helpers and rewrite the two matchers**

Add near `_haversine_km`:

```python
def _within_m(lat1, lon1, lat2, lon2, radius_m: float) -> bool:
    """True when the two points are within radius_m metres (haversine)."""
    if lat1 is None or lat2 is None:
        return False
    if any(pd.isna(v) for v in (lat1, lon1, lat2, lon2)):
        return False
    return _haversine_km(lat1, lon1, lat2, lon2) * 1000.0 <= radius_m


def _endpoint_coords(pc, cache):
    """(lat, lon) for an order endpoint postcode via the postcode cache, or None."""
    co = _cached_coords(str(resolve_fc_alias(str(pc or "").strip())), cache)
    if co is None:
        return None
    return (co["lat"], co["lon"]) if isinstance(co, dict) else (co[0], co[1])
```

Rewrite `_stopped_at` to take ping coord arrays + endpoint coords, distance-first with
sector fallback:

```python
def _stopped_at(times, pcns, lats, lons, order_pc, endpoint_co, depot_area, ts, window_min):
    """Matched stop TIME if the vehicle was stopped at the endpoint within window_min of
    ts, else None. Primary: GPS distance <= MATCH_RADIUS_M to endpoint_co. Fallback (when
    endpoint_co is None or a ping has no coords): symmetric sector postcode match."""
    if pd.isna(ts):
        return None
    t0 = np.datetime64(pd.Timestamp(ts))
    w = np.timedelta64(int(window_min), "m")
    m = (times >= t0 - w) & (times <= t0 + w)
    if not m.any():
        return None
    mt, mp, mla, mlo = times[m], pcns[m], lats[m], lons[m]
    for i in range(len(mt)):
        if endpoint_co is not None and _within_m(endpoint_co[0], endpoint_co[1],
                                                 mla[i], mlo[i], MATCH_RADIUS_M):
            return mt[i]
        # fallback: sector postcode when distance unavailable/failed
        if (endpoint_co is None or pd.isna(mla[i])) and order_pc and _pc_matches(
                order_pc, mp[i], depot_area):
            return mt[i]
    return None
```

Rewrite `_any_fleet_at` to take the extended global arrays + endpoint coords:

```python
def _any_fleet_at(garr, order_pc, endpoint_co, depot_area, ts, exclude_regs, window_min):
    """(reg, stop_time) of a non-assigned fleet vehicle stopped at the endpoint within
    window_min of ts, else (None, None). Distance-first with sector fallback, mirroring
    _stopped_at."""
    g_time, g_pcn, g_reg, g_lat, g_lon = garr
    if pd.isna(ts):
        return None, None
    t0 = np.datetime64(pd.Timestamp(ts))
    w = np.timedelta64(int(window_min), "m")
    idxs = np.where((g_time >= t0 - w) & (g_time <= t0 + w))[0]
    for i in idxs:
        if g_reg[i] in exclude_regs:
            continue
        if endpoint_co is not None and _within_m(endpoint_co[0], endpoint_co[1],
                                                 g_lat[i], g_lon[i], MATCH_RADIUS_M):
            return g_reg[i], g_time[i]
        if (endpoint_co is None or pd.isna(g_lat[i])) and order_pc and _pc_matches(
                order_pc, g_pcn[i], depot_area):
            return g_reg[i], g_time[i]
    return None, None
```

- [ ] **Step 4: Run the stopped_at tests to verify green**

Run: `python -m pytest tests/planning_agent/test_verify_legs.py -k stopped_at -v`
Expected: PASS (all four).

- [ ] **Step 5: Update `classify_leg` call sites to the new signatures**

`classify_leg` currently computes `o_pc`, `d_pc`, `o_depot`, `d_depot`. Add endpoint coords
right after those:

```python
    o_co = _endpoint_coords(row.get("origin_postal_code"), cache)
    d_co = _endpoint_coords(row.get("destination_postal_code"), cache)
```

Change the Step-1 hit comprehensions from `_stopped_at(*vidx[r], o_pc, o_depot, o_ts, o_win)`
to pass the coords explicitly:

```python
    o_hits = [t for r in tracked
              if (t := _stopped_at(*vidx[r], o_pc, o_co, o_depot, o_ts, o_win)) is not None]
    d_hits = [t for r in tracked
              if (t := _stopped_at(*vidx[r], d_pc, d_co, d_depot, d_ts, d_win)) is not None]
```

(`*vidx[r]` now expands to `times, pcns, lats, lons`, so the following positional args are
`order_pc, endpoint_co, depot_area, ts, window`.)

Change the Step-2 substitute calls from
`_any_fleet_at(garr, o_pc, o_depot, o_ts, set(regs), o_win)` to:

```python
    sub_o, t_o = _any_fleet_at(garr, o_pc, o_co, o_depot, o_ts, set(regs), o_win)
    sub_d, t_d = _any_fleet_at(garr, d_pc, d_co, d_depot, d_ts, set(regs), d_win)
```

- [ ] **Step 6: Run the full verify-legs suite to verify green**

Run: `python -m pytest tests/planning_agent/test_verify_legs.py -v`
Expected: PASS (all tests). The classify path now compiles with the new matchers.

---

### Task 3: Endpoint shared-ness precompute

**Files:**
- Modify: `planning_agent/verify_legs.py` (add `build_endpoint_order_counts`, `_endpoint_is_shared`)
- Test: `tests/planning_agent/test_verify_legs.py`

- [ ] **Step 1: Write failing tests**

```python
def test_build_endpoint_order_counts_counts_distinct_orders_per_day():
    df = pd.DataFrame([
        # three distinct orders ex CB9 8QP on the 12th
        {"order_id": "a", "origin_postal_code": "CB9 8QP", "origin_timestamp_local": "2026-01-12 09:00",
         "destination_postal_code": "SS6 7NG", "destination_timestamp_local": "2026-01-13 07:00"},
        {"order_id": "b", "origin_postal_code": "CB9 8QP", "origin_timestamp_local": "2026-01-12 10:00",
         "destination_postal_code": "AL10 9AX", "destination_timestamp_local": "2026-01-12 15:00"},
        {"order_id": "c", "origin_postal_code": "CB9 8QP", "origin_timestamp_local": "2026-01-12 11:00",
         "destination_postal_code": "B37 7WN", "destination_timestamp_local": "2026-01-12 16:00"},
    ])
    counts = V.build_endpoint_order_counts(df)
    assert counts[("CB98QP", "2026-01-12")] == 3
    assert counts[("SS67NG", "2026-01-13")] == 1

def test_endpoint_is_shared_threshold():
    counts = {("CB98QP", "2026-01-12"): 5, ("SS67NG", "2026-01-13"): 1}
    assert V._endpoint_is_shared("CB9 8QP", "2026-01-12", counts) is True    # >= 5
    assert V._endpoint_is_shared("SS6 7NG", "2026-01-13", counts) is False   # unique
    assert V._endpoint_is_shared("ZZ9 9ZZ", "2026-01-12", counts) is False   # unseen -> unique
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/planning_agent/test_verify_legs.py -k endpoint -v`
Expected: FAIL (`AttributeError: build_endpoint_order_counts`).

- [ ] **Step 3: Implement the precompute + predicate**

Add near the classification helpers:

```python
def build_endpoint_order_counts(df: pd.DataFrame) -> dict:
    """{(compact_postcode, iso_date): distinct-order count} for every endpoint (origin and
    destination) across the frame. An endpoint's date is that side's anchor date. Used to
    tell a shared shipper (many orders/day) from a unique customer."""
    counts: dict = {}
    seen: dict = {}
    for side in ("origin", "destination"):
        pc = df[f"{side}_postal_code"].map(_norm)
        dt = pd.to_datetime(df[f"{side}_timestamp_local"], errors="coerce").dt.date.astype(str)
        oid = df["order_id"].astype(str)
        for p, d, o in zip(pc, dt, oid):
            if not p or d == "NaT":
                continue
            key = (p, d)
            bucket = seen.setdefault(key, set())
            if o not in bucket:
                bucket.add(o)
                counts[key] = counts.get(key, 0) + 1
    return counts


def _endpoint_is_shared(pc, iso_date, counts, threshold: int = SHARED_MIN_ORDERS) -> bool:
    """True when >= threshold distinct orders use this endpoint on this date."""
    return counts.get((_norm(pc), str(iso_date)), 0) >= threshold
```

- [ ] **Step 4: Run to verify green**

Run: `python -m pytest tests/planning_agent/test_verify_legs.py -k endpoint -v`
Expected: PASS (both).

---

### Task 4: Substitute gating + thread counts into `classify_leg`

**Files:**
- Modify: `planning_agent/verify_legs.py` (`classify_leg` signature + Step 2 gating; `main` wiring)
- Test: `tests/planning_agent/test_verify_legs.py`

- [ ] **Step 1: Write a failing test — substitute at a shared origin is ignored**

This test drives `classify_leg` with a hand-built index. Keep it small: a substitute vehicle
is present at the (shared) origin but nowhere at the destination; with gating, Step 2 yields
nothing and the row falls through to the structural tier.

```python
def _mk_garr(rows):
    # rows: list of (reg, iso_time, pcn, lat, lon)
    import numpy as np
    t = np.array([np.datetime64(r[1]) for r in rows], dtype="datetime64[ns]")
    return (t,
            np.array([r[2] for r in rows]),
            np.array([r[0] for r in rows]),
            np.array([r[3] for r in rows], dtype=float),
            np.array([r[4] for r in rows], dtype=float))

def test_substitute_at_shared_origin_is_ignored(monkeypatch):
    # classify_leg derives the flow internally via classify_order; pin it so the test is
    # isolated from classify_order's field logic. Row is a Series (as from df.iterrows()).
    monkeypatch.setattr(V, "classify_order", lambda r: "FULL_FLEET")
    row = pd.Series({
        "order_id": "x", "name": "X", "origin_postal_code": "CB9 8QP",
        "destination_postal_code": "SS6 7NG",
        "origin_timestamp_local": "2026-01-12 14:45",
        "destination_timestamp_local": "2026-01-13 06:49",
        "resource_tractor": "N8GNW",  # assigned, but NOT in vidx (untracked here)
    })
    cache = {}  # no coords -> sector fallback path
    vidx = {}   # assigned vehicle untracked
    # a substitute fleet vehicle sits at the shared origin sector
    garr = _mk_garr([("Y88RNW", "2026-01-12 14:45", "CB98QP", np.nan, np.nan)])
    counts = {("CB98QP", "2026-01-12"): 9, ("SS67NG", "2026-01-13"): 1}  # origin shared
    out = V.classify_leg(row, cache, vidx, garr, set(), counts)
    assert out["method"] != "telematics_substitute"     # the shared-origin substitute was ignored
    assert out["leg"] != "COLLECTION" or out["confidence"] != "MEDIUM"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/planning_agent/test_verify_legs.py::test_substitute_at_shared_origin_is_ignored -v`
Expected: FAIL (`classify_leg` takes 5 args, not 6 — `counts` unexpected).

- [ ] **Step 3: Add `counts` param and gate the substitute step**

Change the signature:

```python
def classify_leg(row, cache, vidx, garr, depot_sectors, counts) -> dict:
```

Compute per-endpoint shared flags where the other locals are set (after `o_co`/`d_co`):

```python
    o_date = _svc_date(o_ts) or _first_date_str(row, "origin")
    d_date = _svc_date(d_ts) or _first_date_str(row, "destination")
    o_shared = _endpoint_is_shared(row.get("origin_postal_code"), o_date, counts)
    d_shared = _endpoint_is_shared(row.get("destination_postal_code"), d_date, counts)
```

Add the small date helper near `_svc_date`:

```python
def _first_date_str(row, side: str) -> str:
    ts = pd.to_datetime(row.get(f"{side}_timestamp_local"), errors="coerce")
    return "" if pd.isna(ts) else str(ts.date())
```

In Step 2, ignore a substitute hit whose endpoint is shared (replace the
`sub_o, t_o = ...` / `sub_d, t_d = ...` usage so a shared-endpoint hit is dropped BEFORE the
leg decision):

```python
    sub_o, t_o = _any_fleet_at(garr, o_pc, o_co, o_depot, o_ts, set(regs), o_win)
    sub_d, t_d = _any_fleet_at(garr, d_pc, d_co, d_depot, d_ts, set(regs), d_win)
    if o_shared:
        sub_o, t_o = None, None      # presence at a shared origin isn't order-specific
    if d_shared:
        sub_d, t_d = None, None      # presence at a shared destination isn't order-specific
    if sub_o or sub_d:
        ...                          # existing leg-decision block, unchanged
```

- [ ] **Step 4: Update `main()` to build counts and pass them**

In `main`, after `elig = _eligible(df)` (and after any sampling), build counts on the
eligible frame and thread them into the classify loop:

```python
    counts = build_endpoint_order_counts(elig)
    ...
    rows = [classify_leg(r, cache, vidx, garr, depot_sectors, counts) for _, r in elig.iterrows()]
```

- [ ] **Step 5: Run the substitute-gating test + full suite**

Run: `python -m pytest tests/planning_agent/test_verify_legs.py -v`
Expected: PASS (all, including the new gating test).

---

### Task 5: FULL_FLEET demotion trigger (strict, provable single-vehicle)

**Files:**
- Modify: `planning_agent/verify_legs.py` (`classify_leg` Step 1 one-end branch; add `_should_demote_full_fleet`)
- Test: `tests/planning_agent/test_verify_legs.py`

- [ ] **Step 1: Write failing tests for the demotion predicate**

```python
def test_full_fleet_multivehicle_one_leg_stays_full():
    # WT254741 shape: 2 vehicles booked, only delivery confirmed. Never demote.
    assert V._should_demote_full_fleet(
        flow="FULL_FLEET", powered=2, confirmed_leg="DELIVERY",
        confirmed_unique=True, other_unique=True,
        vehicle_tracked=True, vehicle_elsewhere_at_other=True) is False

def test_full_fleet_solo_provably_elsewhere_demotes():
    # 1 vehicle booked, tracked, confirmed at one unique endpoint, and provably NOT at the
    # other unique endpoint at that leg's time -> demote to the confirmed leg.
    assert V._should_demote_full_fleet(
        flow="FULL_FLEET", powered=1, confirmed_leg="COLLECTION",
        confirmed_unique=True, other_unique=True,
        vehicle_tracked=True, vehicle_elsewhere_at_other=True) is True

def test_full_fleet_solo_untracked_stays_full():
    assert V._should_demote_full_fleet(
        flow="FULL_FLEET", powered=1, confirmed_leg="COLLECTION",
        confirmed_unique=True, other_unique=True,
        vehicle_tracked=False, vehicle_elsewhere_at_other=False) is False

def test_full_fleet_solo_other_end_shared_stays_full():
    # other endpoint is a shared shipper -> non-observation is meaningless -> keep FULL_FLEET
    assert V._should_demote_full_fleet(
        flow="FULL_FLEET", powered=1, confirmed_leg="DELIVERY",
        confirmed_unique=True, other_unique=False,
        vehicle_tracked=True, vehicle_elsewhere_at_other=True) is False

def test_non_full_fleet_flow_never_uses_demotion_predicate():
    assert V._should_demote_full_fleet(
        flow="PL_IMPORT", powered=1, confirmed_leg="DELIVERY",
        confirmed_unique=True, other_unique=True,
        vehicle_tracked=True, vehicle_elsewhere_at_other=True) is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/planning_agent/test_verify_legs.py -k should_demote -v`
Expected: FAIL (`AttributeError: _should_demote_full_fleet`).

- [ ] **Step 3: Implement the predicate**

```python
def _should_demote_full_fleet(flow, powered, confirmed_leg, confirmed_unique,
                              other_unique, vehicle_tracked, vehicle_elsewhere_at_other) -> bool:
    """A FULL_FLEET booking with ONE leg confirmed stays FULL_FLEET unless ALL hold:
    single vehicle booked, it was tracked, BOTH endpoints are unique customers, and the
    tracked vehicle was provably elsewhere (not within MATCH_RADIUS_M) at the other endpoint
    at that leg's time. Only then is it a genuine solo direct where we did just one leg."""
    if flow != "FULL_FLEET":
        return False
    return (powered == 1 and vehicle_tracked and confirmed_unique
            and other_unique and vehicle_elsewhere_at_other)
```

- [ ] **Step 4: Run to verify green**

Run: `python -m pytest tests/planning_agent/test_verify_legs.py -k should_demote -v`
Expected: PASS (all five).

- [ ] **Step 5: Wire the predicate into Step 1's one-end branch**

In `classify_leg` Step 1, the current code is:

```python
    at_o, at_d = bool(o_hits), bool(d_hits)
    if at_o or at_d:
        leg = "FULL_FLEET" if (at_o and at_d) else "COLLECTION" if at_o else "DELIVERY"
        svc = min(o_hits) if at_o else min(d_hits)
        return {**base, "leg": leg, "confidence": "HIGH", "method": "telematics_assigned",
                "matched_vehicle": ",".join(tracked), "service_date": _svc_date(svc)}
```

Replace the one-end case so a FULL_FLEET booking only demotes when the strict predicate says
so; otherwise it stays FULL_FLEET:

```python
    at_o, at_d = bool(o_hits), bool(d_hits)
    if at_o and at_d:
        return {**base, "leg": "FULL_FLEET", "confidence": "HIGH", "method": "telematics_assigned",
                "matched_vehicle": ",".join(tracked), "service_date": _svc_date(min(o_hits))}
    if at_o or at_d:
        single = "COLLECTION" if at_o else "DELIVERY"
        svc = min(o_hits) if at_o else min(d_hits)
        confirmed_unique = (not o_shared) if at_o else (not d_shared)
        other_unique = (not d_shared) if at_o else (not o_shared)
        # was the tracked vehicle provably at the OTHER endpoint? (if so, not "elsewhere")
        other_pc, other_co, other_depot, other_ts, other_win = (
            (d_pc, d_co, d_depot, d_ts, d_win) if at_o else (o_pc, o_co, o_depot, o_ts, o_win))
        at_other = any(_stopped_at(*vidx[r], other_pc, other_co, other_depot, other_ts, other_win)
                       is not None for r in tracked)
        demote = _should_demote_full_fleet(
            flow=flow, powered=powered, confirmed_leg=single,
            confirmed_unique=confirmed_unique, other_unique=other_unique,
            vehicle_tracked=bool(tracked), vehicle_elsewhere_at_other=not at_other)
        if flow == "FULL_FLEET" and not demote:
            leg = "FULL_FLEET"
        else:
            leg = single
        return {**base, "leg": leg, "confidence": "HIGH", "method": "telematics_assigned",
                "matched_vehicle": ",".join(tracked), "service_date": _svc_date(svc)}
```

(For a non-FULL_FLEET flow the `else` branch keeps the observed single leg exactly as before,
because `_should_demote_full_fleet` returns False for non-FULL_FLEET and the `if flow ==
"FULL_FLEET"` guard is not taken — so `leg = single`.)

- [ ] **Step 6: Add an integration test proving the WT254741 shape stays FULL_FLEET**

```python
def test_wt254741_shape_stays_full_fleet(monkeypatch):
    # Assigned N8GNW confirmed at the unique delivery (via GPS distance); origin is a shared
    # shipper; 2 vehicles booked -> must stay FULL_FLEET, not demote to DELIVERY.
    monkeypatch.setattr(V, "classify_order", lambda r: "FULL_FLEET")
    row = pd.Series({
        "order_id": "wt", "name": "WT254741",
        "origin_postal_code": "CB9 8QP", "destination_postal_code": "SS6 7NG",
        "origin_timestamp_local": "2026-01-12 14:45",
        "destination_timestamp_local": "2026-01-13 06:49",
        "resource_tractor": "N8GNW, TA70WTL",
        "shipment_names": "Trip-1, Trip-2",
    })
    cache = {"SS6 7NG": {"lat": 51.590088, "lon": 0.568119}}
    # N8GNW stopped 147 m from SS6 7NG at the delivery time; nothing at the origin
    t = np.array([np.datetime64("2026-01-13 06:49")], dtype="datetime64[ns]")
    vidx = {"N8GNW": (t, np.array(["SS67UA"]),
                      np.array([51.590088]), np.array([0.568119 + 0.0021]))}
    garr = (np.array([], dtype="datetime64[ns]"), np.array([]), np.array([]),
            np.array([], dtype=float), np.array([], dtype=float))
    counts = {("CB98QP", "2026-01-12"): 9, ("SS67NG", "2026-01-13"): 1}
    out = V.classify_leg(row, cache, vidx, garr, set(), counts)
    assert out["leg"] == "FULL_FLEET"
    assert out["method"] == "telematics_assigned"
```

- [ ] **Step 7: Run the full suite green**

Run: `python -m pytest tests/planning_agent/test_verify_legs.py -v`
Expected: PASS (all, incl. `test_wt254741_shape_stays_full_fleet`).

---

### Task 6: Baseline snapshot + order-by-order diff script

**Files:**
- Create: `planning_agent/diff_verified_legs.py`
- Modify: `planning_agent/verify_legs.py` (`main`: snapshot the existing CSV before writing)
- Test: `tests/planning_agent/test_diff_verified_legs.py`

- [ ] **Step 1: Snapshot the baseline in `main` before overwriting**

At the top of `main`, before anything writes `OUT_CSV`, copy an existing CSV aside once:

```python
    if OUT_CSV.exists():
        baseline = OUT_CSV.with_name("verified_legs.before_gpsmatch.csv")
        if not baseline.exists():
            import shutil
            shutil.copy2(OUT_CSV, baseline)   # copy2 preserves mtime
            print(f"  snapshotted baseline -> {baseline.name}")
```

(Only snapshots once, so re-runs don't clobber the true pre-change baseline.)

- [ ] **Step 2: Write failing tests for the diff builder**

```python
import pandas as pd
import planning_agent.diff_verified_legs as D

def test_diff_flags_changed_rows_and_context():
    old = pd.DataFrame([
        {"order_id": "wt", "order_name": "WT254741", "api_flow": "FULL_FLEET",
         "leg": "COLLECTION", "confidence": "MEDIUM", "method": "telematics_substitute"},
        {"order_id": "u", "order_name": "U", "api_flow": "PL_IMPORT",
         "leg": "DELIVERY", "confidence": "HIGH", "method": "telematics_assigned"},
    ])
    new = pd.DataFrame([
        {"order_id": "wt", "order_name": "WT254741", "api_flow": "FULL_FLEET",
         "leg": "FULL_FLEET", "confidence": "HIGH", "method": "telematics_assigned"},
        {"order_id": "u", "order_name": "U", "api_flow": "PL_IMPORT",
         "leg": "DELIVERY", "confidence": "HIGH", "method": "telematics_assigned"},
    ])
    diff = D.build_diff(old, new)
    wt = diff[diff["order_id"] == "wt"].iloc[0]
    assert wt["old_leg"] == "COLLECTION" and wt["new_leg"] == "FULL_FLEET"
    assert bool(wt["changed"]) is True
    assert bool(diff[diff["order_id"] == "u"].iloc[0]["changed"]) is False

def test_transition_matrix_counts():
    old = pd.DataFrame([{"order_id": "a", "leg": "COLLECTION"},
                        {"order_id": "b", "leg": "COLLECTION"}])
    new = pd.DataFrame([{"order_id": "a", "leg": "FULL_FLEET"},
                        {"order_id": "b", "leg": "COLLECTION"}])
    tm = D.transition_matrix(old, new)
    assert tm.loc["COLLECTION", "FULL_FLEET"] == 1
    assert tm.loc["COLLECTION", "COLLECTION"] == 1
```

- [ ] **Step 3: Run to verify they fail**

Run: `python -m pytest tests/planning_agent/test_diff_verified_legs.py -v`
Expected: FAIL (module missing).

- [ ] **Step 4: Implement the diff module**

```python
"""Order-by-order comparison of two verified_legs snapshots (baseline vs rebuilt).

    python planning_agent/diff_verified_legs.py \
        --old planning_agent/verified_legs.before_gpsmatch.csv \
        --new planning_agent/verified_legs.csv \
        --out planning_agent/verified_legs_diff.csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

_COLS = ["leg", "confidence", "method"]


def build_diff(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    o = old.set_index(old["order_id"].astype(str))
    n = new.set_index(new["order_id"].astype(str))
    ids = list(dict.fromkeys(list(o.index) + list(n.index)))
    rows = []
    for oid in ids:
        orow = o.loc[oid] if oid in o.index else None
        nrow = n.loc[oid] if oid in n.index else None
        rec = {
            "order_id": oid,
            "order_name": (nrow if nrow is not None else orow).get("order_name", ""),
            "api_flow": (nrow if nrow is not None else orow).get("api_flow", ""),
        }
        for c in _COLS:
            rec[f"old_{c}"] = "" if orow is None else str(orow.get(c, "") or "")
            rec[f"new_{c}"] = "" if nrow is None else str(nrow.get(c, "") or "")
        rec["changed"] = any(rec[f"old_{c}"] != rec[f"new_{c}"] for c in _COLS)
        rows.append(rec)
    return pd.DataFrame(rows)


def transition_matrix(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    o = old.set_index(old["order_id"].astype(str))["leg"]
    n = new.set_index(new["order_id"].astype(str))["leg"]
    j = pd.DataFrame({"old": o, "new": n}).dropna()
    return pd.crosstab(j["old"], j["new"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--out", default="planning_agent/verified_legs_diff.csv")
    args = ap.parse_args()
    old = pd.read_csv(args.old, dtype=str)
    new = pd.read_csv(args.new, dtype=str)
    diff = build_diff(old, new)
    diff.to_csv(args.out, index=False)
    print(f"rows: {len(diff)} | changed: {int(diff['changed'].sum())}")
    print("\n=== leg transition matrix (old rows x new cols) ===")
    print(transition_matrix(old, new).to_string())
    print("\n=== method transitions (top 15) ===")
    mt = diff.groupby(["old_method", "new_method"]).size().sort_values(ascending=False)
    print(mt.head(15).to_string())
    print(f"\n-> {args.out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the diff tests green**

Run: `python -m pytest tests/planning_agent/test_diff_verified_legs.py -v`
Expected: PASS (both).

- [ ] **Step 6: Full planning_agent suite green**

Run: `python -m pytest tests/planning_agent/ -v`
Expected: PASS.

---

### Task 7: Rebuild + measurement (controller inline — NOT a subagent)

This task is run by the controller, not delegated. It produces the audit the stakeholder asked
for and must be reviewed before the enriched parquet is rebuilt.

- [ ] **Step 1: Regenerate `verified_legs.csv` (Jan+Feb) with the new logic**

Run (this snapshots the baseline on first run, then rebuilds):

```
python -B planning_agent/verify_legs.py --qargo data/Input/orders/qargo_20260101_to_20260131.parquet --qargo data/Input/orders/qargo_20260201_to_20260228.parquet
```

Confirm the console prints `snapshotted baseline -> verified_legs.before_gpsmatch.csv` and a
new leg/method distribution.

- [ ] **Step 2: Produce the order-by-order diff**

```
python -B planning_agent/diff_verified_legs.py --old planning_agent/verified_legs.before_gpsmatch.csv --new planning_agent/verified_legs.csv --out planning_agent/verified_legs_diff.csv
```

Record: total changed, the leg transition matrix, how many of the 960 FULL_FLEET→single
demotions reverted to FULL_FLEET, and how many `telematics_substitute` COLLECTIONs dissolved.
Spot-check WT254741 in the diff — it MUST show `old_leg=COLLECTION new_leg=FULL_FLEET`.

- [ ] **Step 3: Sanity-review the largest transition buckets**

Eyeball ~10 orders from the biggest old→new buckets (especially anything that moved INTO
FULL_FLEET) to confirm the changes are defensible. This is the gate before touching enriched.

- [ ] **Step 4: Rebuild the enriched parquet ONLY after the diff is accepted**

The existing `freight_planner/data/enriched_orders_2026-01_2026-02.parquet` stays untouched
until Steps 2–3 are reviewed. Then:

```
python -B -m freight_planner.build_enriched
```

- [ ] **Step 5: Forward measurement — one run each wk1/wk2**

Run the standard weekly validation (per existing session practice) and record coverage +
combined-km vs the current baselines. Hypothesis: genuine full-fleet directs replacing
collect-to-depot half-orders reduce modelled km. Report results as they land; a km change
with better structure is a stakeholder conversation, not a silent revert.

- [ ] **Step 6: Full test sweep**

Run: `python -m pytest tests/planning_agent/ tests/freight_planner/ -q`
Expected: PASS. Note the new counts in QUEST_LOG when recording the session.
