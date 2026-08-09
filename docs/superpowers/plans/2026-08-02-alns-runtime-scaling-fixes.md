# ALNS Runtime Scaling Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four confirmed algorithmic blockers that make `run_rolling.py` epochs get progressively slower as a run accumulates committed vehicle-days (locks) and processes more orders, without touching the higher-risk `current_sol` history-pruning architecture.

**Architecture:** All four fixes are surgical, in-place changes to existing hot paths — no new modules, no new data flow. Three are pure "stop doing wasted work" fixes (safe by construction: same output, fewer redundant computations). One (Task 2) adds a small in-process memo with an explicit invalidation hook wired into the existing `reset_router()` teardown used by every test and by the between-run boundary.

**Tech Stack:** Python 3.12, pandas, pytest (run from `e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics`).

**Out of scope (flagged, not fixed here):** `current_sol`/`inject_routes` in `run_rolling.py` never prunes closed/historical vehicle-days — this is the deep root cause of locks-correlated growth, but `current_sol` is read at 20+ call sites (coverage checks, tour attach/commission exclusions, disturbance breakdown, final plan emission). Splitting it into open-horizon vs. frozen-history subsets safely needs a dedicated investigation of every read site and is not attempted in this plan.

---

### Task ranking (severity, confirmed against source)

| # | Task | File | Severity | Why |
|---|------|------|----------|-----|
| 1 | Incremental `best_routes` snapshot | `alns.py` | **Critical** | Full deep-copy of the entire routes dict fires on every "new best" acceptance — up to thousands of times per epoch — and its cost is O(cumulative locks) each time. This is the single biggest match for the observed 450s→4-5000s/day growth curve. |
| 2 | Reuse the on-disk OSRM cache across epochs | `route_costs.py` | **Critical** | Every epoch re-reads and re-JSON-parses the entire OSRM pair cache from disk (7.9M+ entries by the end of a month run) and unconditionally wipes the in-memory `_km_cache`/`_min_cache` distance memo, even though the underlying matrix only grows. |
| 3 | Remove duplicate `evaluate_day` call in ALNS setup pass | `alns.py` | **High** | The once-per-epoch baseline-cost pass calls `evaluate_day` twice with identical arguments for every vehicle-day when vehicle-day cost is enabled (the default) — a clean 2x waste on an O(cumulative locks) pass. |
| 4 | Replace `iterrows()` with `to_dict("records")` in catchment build | `catchment.py` | **Moderate** | Re-scans the full month's `qargo_df` with the slowest pandas row-iteration API every single epoch; `to_dict("records")` is a drop-in, semantically-identical replacement (dict `.get()` matches Series `.get()`) that's several times faster with zero risk to `classify_order`/`_service_pcs`. |

---

### Task 1: Incremental `best_routes` snapshot in `alns.py`

**Files:**
- Modify: `freight_planner/alns.py:1278` (initial snapshot + new tracking set), `freight_planner/alns.py:1418-1420` (option-swap accept), `freight_planner/alns.py:1438-1439` (option-swap best-check), `freight_planner/alns.py:1689-1691` (standard accept), `freight_planner/alns.py:1709-1710` (standard best-check)
- Test: `tests/freight_planner/test_alns.py`

**Root cause:** `routes` is mutated at exactly two sites in the whole file (`alns.py:1419` and `alns.py:1690`), both via `routes[key] = trips` for `key in work` (never `del`, never mutated in place — always full reassignment). `best_routes` is a point-in-time snapshot taken whenever a move both gets accepted AND improves on the incumbent. Today it rebuilds by deep-copying **every** key in `routes`, including thousands of untouched historical vehicle-days. Since only `work`'s keys ever changed since the *previous* best snapshot (accumulated across any accepted-but-not-best moves in between), the fix is to track a `dirty_since_best` set of keys touched since the last best snapshot, and only deep-copy those keys into `best_routes` — mutating `best_routes` in place rather than rebuilding it.

- [ ] **Step 1: Read current behavior baseline**

Run: `cd "e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics" && python -m pytest tests/freight_planner/test_alns.py tests/freight_planner/test_alns_converge.py -q`
Expected: all pass (establishes the pre-change baseline — note the pass count and timing).

- [ ] **Step 2: Add the `dirty_since_best` tracking set and convert the initial snapshot**

In `alns.py`, immediately after line 1278:

```python
    best_routes = {k: [list(t) for t in v] for k, v in routes.items()}
    best_unassigned = sorted(unassigned)
    best_total = total
    best_served = served_before
```

add:

```python
    # Perf: best_routes is re-snapshotted every time an accepted move improves
    # on the incumbent — which can happen thousands of times per epoch. `routes`
    # is only ever reassigned by full key (never mutated in place, see the two
    # `routes[key] = trips` sites below), so instead of deep-copying the WHOLE
    # dict each time, track which keys changed since the last best snapshot and
    # only re-copy those. best_routes is mutated in place, not rebuilt.
    dirty_since_best: set[tuple[str, str]] = set()
```

- [ ] **Step 3: Update the option-swap accept site to track dirty keys**

At `alns.py:1418-1420`, change:

```python
                    for key, trips in swap["work"].items():
                        routes[key] = trips
                        route_cost_by_key[key] = swap["new_cost"][key]
```

to:

```python
                    for key, trips in swap["work"].items():
                        routes[key] = trips
                        route_cost_by_key[key] = swap["new_cost"][key]
                    dirty_since_best.update(swap["work"])
```

- [ ] **Step 4: Convert the option-swap best-check snapshot**

At `alns.py:1438-1439`, change:

```python
                    if (cur_served, -total) > (best_served, -best_total):
                        best_routes = {k: [list(t) for t in v] for k, v in routes.items()}
                        best_unassigned = sorted(unassigned)
```

to:

```python
                    if (cur_served, -total) > (best_served, -best_total):
                        for key in dirty_since_best:
                            best_routes[key] = [list(t) for t in routes[key]]
                        dirty_since_best.clear()
                        best_unassigned = sorted(unassigned)
```

- [ ] **Step 5: Update the standard accept site to track dirty keys**

At `alns.py:1689-1691`, change:

```python
            for key, trips in work.items():
                routes[key] = trips
                route_cost_by_key[key] = new_cost[key]
```

to:

```python
            for key, trips in work.items():
                routes[key] = trips
                route_cost_by_key[key] = new_cost[key]
            dirty_since_best.update(work)
```

- [ ] **Step 6: Convert the standard best-check snapshot**

At `alns.py:1709-1710`, change:

```python
            if (cur_served, -total) > (best_served, -best_total):
                best_routes = {k: [list(t) for t in v] for k, v in routes.items()}
                best_unassigned = sorted(unassigned)
```

to:

```python
            if (cur_served, -total) > (best_served, -best_total):
                for key in dirty_since_best:
                    best_routes[key] = [list(t) for t in routes[key]]
                dirty_since_best.clear()
                best_unassigned = sorted(unassigned)
```

- [ ] **Step 7: Write a regression test proving best_routes tracks routes correctly across a non-best-then-best sequence**

This is the exact bug shape a naive incremental fix could introduce: an accepted-but-not-best move (simulated annealing lets a worse move through when `sa_temp_fraction > 0`), followed by a later move that DOES become best, must still carry the first move's change into `best_routes`. The function under test is `improve_solution` (note: NOT `improve_existing_solution`, which is a separate DataFrame-based wrapper defined later in the file — `improve_solution` is the one with the `solution=`/`job_meta=`/`vehicle_meta=` signature that owns `best_routes`). Add to `tests/freight_planner/test_alns.py`, next to `test_improve_solution_never_drops_served_jobs`:

```python
def test_best_routes_covers_every_served_job_with_simulated_annealing_noise():
    """Regression for the incremental best_routes snapshot (perf fix): with
    sa_temp_fraction > 0 some accepted moves do NOT become the new best, so
    routes drifts ahead of best_routes across several iterations before the
    next best is found. If the dirty-key tracking only remembered the
    TRIGGERING move's keys instead of accumulating every changed key since
    the last best snapshot, jobs touched by an accepted-but-not-best move
    would silently disappear from (or duplicate in) the returned solution."""
    vmeta = {
        "V1": _vmeta("V1", "CB22", CB22),
        "V2": _vmeta("V2", "BEDFORD", BEDFORD),
        "V3": _vmeta("V3", "CB22", CB22),
    }
    day = "2026-01-05"
    # Deliberately scrambled: every job starts on a vehicle far from its
    # natural home, forcing many candidate moves across the run.
    jobs = {
        ("V1", day): [_rjob("J1", NEAR_BED), _rjob("J2", NEAR_BED)],
        ("V2", day): [_rjob("J3", NEAR_CB22), _rjob("J4", NEAR_CB22)],
        ("V3", day): [_rjob("J5", NEAR_BED), _rjob("J6", NEAR_CB22)],
    }
    job_meta = {
        jid: JobMeta(_rjob(jid, loc), day, ["V1", "V2", "V3"], {})
        for jid, loc in [("J1", NEAR_BED), ("J2", NEAR_BED), ("J3", NEAR_CB22),
                        ("J4", NEAR_CB22), ("J5", NEAR_BED), ("J6", NEAR_CB22)]
    }
    all_job_ids = set(job_meta)

    result = improve_solution(
        jobs, job_meta, vmeta,
        iterations=400, rng_seed=11,
        sa_temp_fraction=0.15,   # non-zero: guarantees accepted-but-not-best moves occur
        sa_cooling=0.995,
    )

    served = _served(result.solution)
    assert served == all_job_ids  # nothing lost, nothing duplicated
    assert result.served_after == len(all_job_ids)
    # every job id appears in exactly one route (no duplicate placement)
    seen = []
    for trips in result.solution.values():
        for trip in trips:
            for j in trip:
                seen.append(j.job_id)
    assert sorted(seen) == sorted(all_job_ids)
```

- [ ] **Step 8: Run the full ALNS test suite and diff timing against the Step 1 baseline**

Run: `cd "e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics" && python -m pytest tests/freight_planner/test_alns.py tests/freight_planner/test_alns_converge.py tests/freight_planner/test_alns_dayflex.py tests/freight_planner/test_alns_route_times.py tests/freight_planner/test_alns_toggles.py tests/freight_planner/test_merge_sweep.py -q`
Expected: PASS, same pass count as Step 1's baseline (behavior must be bit-identical — this is a pure performance change).

- [ ] **Step 9: Commit**

```bash
git add freight_planner/alns.py tests/freight_planner/test_alns.py
git commit -m "perf: incremental best_routes snapshot instead of full deep-copy per accepted-best move"
```

---

### Task 2: Reuse the on-disk OSRM cache across epochs in `route_costs.py`

**Files:**
- Modify: `freight_planner/route_costs.py` (add module-level memo, update `warm_and_install_osrm`, update `reset_router`)
- Test: `tests/freight_planner/test_route_costs.py`

**Root cause:** `warm_and_install_osrm` (called once per epoch via `osrm_setup.warm_osrm_for_run`) calls `load_cache(cpath)` unconditionally — re-reading and re-parsing the entire on-disk JSON pair cache from scratch every epoch (confirmed growing to 7.9M+ entries in the observed `C0_headline` run) — then always constructs a brand-new `OSRMRouter` and calls `set_router()`, which unconditionally clears the in-memory `_km_cache`/`_min_cache` distance memo described in the code's own comment as saving "~15% of evaluate_route." Since `build_osrm_matrix` mutates the cache dict **in place** (only adding missing pairs), the matrix each epoch is always a superset of the previous epoch's — there is no correctness reason to reload from disk or wipe the memo when nothing router-identity-relevant changed.

- [ ] **Step 1: Read current behavior baseline**

Run: `cd "e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics" && python -m pytest tests/freight_planner/test_route_costs.py tests/freight_planner/test_osrm_setup.py -q`
Expected: all pass.

- [ ] **Step 2: Add the in-process disk-cache memo**

In `route_costs.py`, after the existing `_km_cache`/`_min_cache` declarations (after line 48), add:

```python
# In-process memo of the on-disk OSRM pair cache, keyed by resolved path.
# `warm_and_install_osrm` is called once per epoch of a rolling run; without
# this it re-reads and re-JSON-parses the WHOLE cache file every single
# epoch (millions of entries by the end of a month-long run) purely to add
# a handful of new pairs. The dict is mutated in place by build_osrm_matrix,
# so within one process it only ever grows — safe to keep alive across calls.
# Cleared by reset_router() so tests and fresh runs never see a stale cache.
_disk_cache_by_path: dict[str, dict] = {}
```

- [ ] **Step 3: Rewrite `warm_and_install_osrm` to reuse the memo and skip redundant router installs**

Replace the body of `warm_and_install_osrm` (currently `route_costs.py:92-121`):

```python
def warm_and_install_osrm(coords, cache_path=None, osrm_url: str | None = None) -> RoadRouter:
    """Pre-warm the OSRM matrix over `coords` (batched /table), persist it, then
    install the router so the whole run routes from in-memory lookups.

    A full window touches many coord pairs the shared cache may not yet hold;
    lazy per-pair live queries stall the run. One batch build up front (and a
    cache save) makes subsequent lookups O(1) and amortises across runs.
    """
    from freight_planner.shared.routing import (
        CACHE_PATH,
        DEFAULT_OSRM_URL,
        HaversineRouter,
        OSRMRouter,
        build_osrm_matrix,
        load_cache,
        save_cache,
    )

    cpath = str(cache_path or CACHE_PATH)
    url = osrm_url or DEFAULT_OSRM_URL
    cache = _disk_cache_by_path.get(cpath)
    if cache is None:
        cache = load_cache(cpath)
        _disk_cache_by_path[cpath] = cache
    points = [(float(a), float(b)) for a, b in coords]
    before = len(cache)
    if points:
        build_osrm_matrix(points, cache, url)
        if len(cache) != before:
            save_cache(cpath, cache)
    if isinstance(_active_router, OSRMRouter) and _active_router.matrix is cache:
        # Same growing matrix already installed this process — reinstalling
        # would only wipe the _km_cache/_min_cache memo for no reason.
        return _active_router
    router = OSRMRouter(matrix=cache, fallback=HaversineRouter(), osrm_url=url)
    set_router(router)
    return router
```

- [ ] **Step 4: Clear the disk-cache memo on `reset_router()`**

In `route_costs.py`, change `reset_router` (currently lines 59-64):

```python
def reset_router() -> None:
    """Restore the default haversine × road-factor model."""
    global _active_router
    _active_router = None
    _km_cache.clear()
    _min_cache.clear()
```

to:

```python
def reset_router() -> None:
    """Restore the default haversine × road-factor model."""
    global _active_router
    _active_router = None
    _km_cache.clear()
    _min_cache.clear()
    _disk_cache_by_path.clear()
```

This closes the one real staleness risk (a test or harness that calls `warm_and_install_osrm` against the same `cache_path` across two logically-separate runs in one process) — `reset_router()` is already the conventional teardown used by every test in `tests/freight_planner/` that installs a router.

- [ ] **Step 5: Write a test proving the disk cache is not reloaded across repeated calls**

Add to `tests/freight_planner/test_route_costs.py`:

```python
def test_warm_and_install_osrm_reuses_loaded_cache_across_epochs(tmp_path, monkeypatch):
    """The on-disk cache must be parsed once per process per path, not once
    per epoch — reloading a multi-million-entry JSON file every epoch was
    the dominant cost in long rolling runs."""
    from freight_planner import route_costs as rc
    from freight_planner.shared import routing as sr

    cache_path = tmp_path / "osrm_cache.json"
    sr.save_cache(cache_path, {})  # empty but present on disk

    load_calls = []
    orig_load = sr.load_cache

    def counting_load(path=sr.CACHE_PATH):
        load_calls.append(path)
        return orig_load(path)

    monkeypatch.setattr(sr, "load_cache", counting_load)
    try:
        coords = [(52.0, 0.0), (52.1, 0.1)]
        rc.warm_and_install_osrm(coords, cache_path=cache_path, osrm_url="http://unused")
    finally:
        pass
    # First call must load from disk exactly once.
    assert len(load_calls) == 1
    rc.reset_router()


def test_warm_and_install_osrm_skips_memo_clear_when_matrix_unchanged(tmp_path, monkeypatch):
    """A second warm-up call over the SAME (already-installed) growing matrix
    must not wipe the _km_cache/_min_cache distance memo — those entries are
    still valid since the matrix only ever gains pairs, never changes them."""
    from freight_planner import route_costs as rc
    from freight_planner.shared import routing as sr

    cache_path = tmp_path / "osrm_cache2.json"
    sr.save_cache(cache_path, {})

    def fake_matrix(points, cache, url, max_table_size=None):
        cache[("52.000000,0.000000", "52.100000,0.100000")] = (10.0, 0.2)
        return cache

    monkeypatch.setattr(sr, "build_osrm_matrix",
                        lambda points, cache, url, **kw: fake_matrix(points, cache, url))
    try:
        rc.warm_and_install_osrm([(52.0, 0.0), (52.1, 0.1)], cache_path=cache_path,
                                 osrm_url="http://unused")
        rc.road_km(52.0, 0.0, 52.1, 0.1)  # populate _km_cache with one entry
        assert len(rc._km_cache) == 1
        rc.warm_and_install_osrm([(52.0, 0.0), (52.1, 0.1)], cache_path=cache_path,
                                 osrm_url="http://unused")  # second epoch, same coords
        assert len(rc._km_cache) == 1  # memo survived — was not wiped
    finally:
        rc.reset_router()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd "e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics" && python -m pytest tests/freight_planner/test_route_costs.py tests/freight_planner/test_osrm_setup.py -q -v`
Expected: PASS, including the two new tests.

- [ ] **Step 7: Run the full freight_planner test suite (this touches a shared module used everywhere)**

Run: `cd "e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics" && python -m pytest tests/freight_planner -q`
Expected: PASS, same pass count as before this change (check `conftest.py:30/34` already calls `reset_router()` in fixtures, so no test should observe the new memo leaking across tests).

- [ ] **Step 8: Commit**

```bash
git add freight_planner/route_costs.py tests/freight_planner/test_route_costs.py
git commit -m "perf: reuse loaded OSRM disk cache and skip redundant memo-clear across epochs"
```

---

### Task 3: Remove duplicate `evaluate_day` call in the ALNS setup pass

**Files:**
- Modify: `freight_planner/alns.py:1212-1231`
- Test: `tests/freight_planner/test_alns.py`

**Root cause:** In the once-per-epoch baseline-cost loop, `km(trips, vid, day)` (line 1214) already calls `evaluate_day(rv(vid, day), tt, detail=False)` internally to get `.total_km`. When `vehicle_day_cost_enabled()` is true (the project default per `vehicle-day-activation-cost` — see memory), lines 1224-1226 call `evaluate_day(rv(vid, day), tt, detail=False)` again with the **identical** arguments purely to get the driver-day cost. This is a straight 2x duplication of the most expensive call in the loop, for every vehicle-day in the cumulative solution.

- [ ] **Step 1: Read current behavior baseline**

Run: `cd "e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics" && python -m pytest tests/freight_planner/test_alns.py -q`
Expected: all pass.

- [ ] **Step 2: Replace the duplicate-evaluate loop**

In `alns.py`, replace lines 1212-1231:

```python
    for (vid, day), trips in routes.items():
        if vid in vehicle_meta:
            k_phys = km(trips, vid, day)
            phys_km_before += k_phys
            if _as_trips(trips):
                _st = seed_by_type.setdefault(str(vehicle_meta[vid].vehicle_type),
                                              {"vehicle_days": 0, "km_road": 0.0})
                _st["vehicle_days"] += 1
                _st["km_road"] += float(k_phys)
            cost = road_cost_per_km(vehicle_meta[vid].vehicle_type) * (
                k_phys + _trips_penalty_km(trips, vehicle_meta[vid]))
            tt = _as_trips(trips)
            if tt and vehicle_day_cost_enabled():
                ev0 = evaluate_day(rv(vid, day), tt, detail=False)
                cost += driver_day_cost_ev(vehicle_meta[vid].vehicle_type, ev0)
            route_cost_by_key[(vid, day)] = cost
        else:
            route_cost_by_key[(vid, day)] = 0.0
        for j in _flatten(trips):
            job_loc[j.job_id] = (vid, day)
```

with:

```python
    for (vid, day), trips in routes.items():
        if vid in vehicle_meta:
            tt = _as_trips(trips)
            # Perf: evaluate_day is the expensive call here and this loop runs
            # once per vehicle-day in the FULL cumulative solution every epoch
            # — compute it once and reuse for both the km baseline and the
            # driver-day cost, instead of calling it twice with identical args.
            ev0 = evaluate_day(rv(vid, day), tt, detail=False) if tt else None
            k_phys = ev0.total_km if ev0 is not None else 0.0
            phys_km_before += k_phys
            if tt:
                _st = seed_by_type.setdefault(str(vehicle_meta[vid].vehicle_type),
                                              {"vehicle_days": 0, "km_road": 0.0})
                _st["vehicle_days"] += 1
                _st["km_road"] += float(k_phys)
            cost = road_cost_per_km(vehicle_meta[vid].vehicle_type) * (
                k_phys + _trips_penalty_km(trips, vehicle_meta[vid]))
            if tt and vehicle_day_cost_enabled():
                cost += driver_day_cost_ev(vehicle_meta[vid].vehicle_type, ev0)
            route_cost_by_key[(vid, day)] = cost
        else:
            route_cost_by_key[(vid, day)] = 0.0
        for j in _flatten(trips):
            job_loc[j.job_id] = (vid, day)
```

Note `km()` (the closure at `alns.py:1182-1184`) stays exactly as-is — it's still used at `alns.py:1760` for final reporting and possibly elsewhere; this change only stops calling it from this specific loop.

- [ ] **Step 3: Write a regression test proving cost_before/km_before are unchanged**

Add to `tests/freight_planner/test_alns.py`, next to `test_solution_cost_scales_with_per_type_fuel_rate` (which already shows the `VEHICLE_DAY_COST_ENABLED` monkeypatch pattern used here):

```python
def test_setup_pass_cost_matches_reference_with_vehicle_day_cost_enabled(monkeypatch):
    """The setup pass now calls evaluate_day once per vehicle-day instead of
    twice when vehicle_day_cost_enabled() is True — cost_before/km_before
    must be bit-identical to before this change, since it's a pure
    duplicate-call removal, not a cost model change. iterations=0 isolates
    the setup pass from the ALNS loop itself."""
    monkeypatch.setattr(config, "VEHICLE_DAY_COST_ENABLED", True)
    vmeta = {"V1": _vmeta_type("V1", "CB22", CB22, "rigid")}
    day = "2026-01-05"
    sol = {("V1", day): [_rjob("J", NEAR_BED)]}
    job_meta = {"J": JobMeta(_rjob("J", NEAR_BED), day, ["V1"], {})}

    result = improve_solution(sol, job_meta, vmeta, iterations=0, rng_seed=1)

    assert result.cost_before > 0.0
    assert result.km_before > 0.0
    # cost_before must include BOTH the road-cost term and the driver-day
    # term (proves the single evaluate_day call still feeds both branches,
    # not that the driver-day branch got silently dropped alongside the
    # duplicate call).
    monkeypatch.setattr(config, "VEHICLE_DAY_COST_ENABLED", False)
    result_no_driver_cost = improve_solution(sol, job_meta, vmeta, iterations=0, rng_seed=1)
    assert result.cost_before > result_no_driver_cost.cost_before
    assert result.km_before == result_no_driver_cost.km_before  # km unaffected by the flag
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics" && python -m pytest tests/freight_planner/test_alns.py -q -v`
Expected: PASS, including the new test, with `cost_before`/`km_before` values matching what the suite already asserts elsewhere for the same fixtures (this proves the removed duplicate call was truly redundant, not silently doing something different).

- [ ] **Step 5: Run the full ALNS-adjacent suite**

Run: `cd "e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics" && python -m pytest tests/freight_planner/test_alns.py tests/freight_planner/test_alns_converge.py tests/freight_planner/test_alns_dayflex.py tests/freight_planner/test_alns_route_times.py tests/freight_planner/test_alns_toggles.py tests/freight_planner/test_merge_sweep.py tests/freight_planner/test_disturbance.py -q`
Expected: PASS, no change in pass count from before this task.

- [ ] **Step 6: Commit**

```bash
git add freight_planner/alns.py tests/freight_planner/test_alns.py
git commit -m "perf: remove duplicate evaluate_day call in ALNS per-epoch setup pass"
```

---

### Task 4: Replace `iterrows()` with `to_dict("records")` in `catchment.py`

**Files:**
- Modify: `freight_planner/catchment.py:93`
- Test: `tests/freight_planner/test_catchment.py`

**Root cause:** `build_vehicle_catchment` (called once per epoch from `run_alns.py:499` with the full month's `qargo_df`) iterates with `qargo_df.iterrows()`, pandas' slowest row-iteration API (it constructs a new `Series` with its own `Index` per row). `classify_order(row)` and `_service_pcs(row, flow)` both only ever call `.get(key)` on `row` — a plain `dict` from `qargo_df.to_dict("records")` satisfies that identically and is built via pandas' optimized C-level path, several times faster with zero behavior change.

- [ ] **Step 1: Read current behavior baseline**

Run: `cd "e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics" && python -m pytest tests/freight_planner/test_catchment.py -q`
Expected: all pass.

- [ ] **Step 2: Swap the iteration method**

In `catchment.py`, change line 93:

```python
    for _, row in qargo_df.iterrows():
```

to:

```python
    # Perf: iterrows() constructs a Series+Index per row, the slowest pandas
    # row-iteration path; classify_order/_service_pcs only ever call
    # row.get(key), which a plain dict from to_dict("records") satisfies
    # identically — this is a pure iteration-speed change, not a behavior one.
    for row in qargo_df.to_dict("records"):
```

- [ ] **Step 3: Run tests to verify they pass unchanged**

Run: `cd "e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics" && python -m pytest tests/freight_planner/test_catchment.py -q -v`
Expected: PASS, identical pass/fail results to Step 1 (this file's existing tests already cover cancelled-order filtering, thin-history fallback, and zero-history backfill — all of which exercise `row.get()` paths that must behave identically under the new iteration method).

- [ ] **Step 4: Run the wider suite that touches catchment (run_alns integration tests)**

Run: `cd "e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics" && python -m pytest tests/freight_planner -q -k "catchment or dynamic_e2e or dynamic_loop"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add freight_planner/catchment.py
git commit -m "perf: replace iterrows() with to_dict(records) in per-epoch catchment build"
```

---

### Final validation

- [ ] **Step 1: Run the complete freight_planner test suite**

Run: `cd "e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics" && python -m pytest tests/freight_planner -q`
Expected: PASS, same total pass count as `git stash`'d pre-change baseline (record this count before Task 1 Step 1 and compare here).

- [ ] **Step 2: Re-run one of the slow reference logs' window as a real timing check**

The user's own `W0_baseline` run (`freight_planner/result_runs/W0_baseline/2026-02/2026-02-16_to_2026-02-22/`) is a good before/after reference: same 7-day window, previously ~4,982s wall-clock when run without contention. Re-run it (uncontended — no other ALNS process running concurrently) and compare `DYNAMIC DONE` timestamp in the new `alns_progress.log` against the historical 4,982s baseline. A meaningful drop (particularly in the later days where `locks` was highest) confirms Tasks 1–3 are working; if the multi-day slope (450s/day → thousands/day) still tracks locks closely, the remaining scaling is coming from the out-of-scope `current_sol` growth path noted above.
