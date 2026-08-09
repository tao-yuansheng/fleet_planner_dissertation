# Depot Pinning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the cross-depot teleport (A1 gate failure): every daily pickup/delivery leg is emitted with `depot_bound` = its depot label, enforced by the existing `DEPOT_BOUND` evaluator constraint, so freight is only served by vehicles homed where it physically sits.

**Architecture:** Emission-site stamping in `legs.py` (one `_pinned()` helper + kwargs at 8 `_leg()` call sites), gated by new `config.DEPOT_PINNING` (default ON, `--no-depot-pinning`). The two ledger sites that infer "collocated reclassified delivery" from `depot_bound` presence re-key on the `:DIR`-tail identity. No evaluator/candidate/route_seed changes — all plumbing shipped with the collocated rule.

**Tech Stack:** Python, pytest, pandas. Spec: `docs/superpowers/specs/2026-07-17-depot-pinning-design.md`.

---

### Task 1: Config knob

**Files:** Modify `freight_planner/config.py` (after `COLLOCATED_STAGING_MIN`, ~line 30)

- [x] **Step 1: Add the knob** (no test — config constants are asserted by Task 2's default-on test)

```python
DEPOT_PINNING: bool = True  # every daily pickup/delivery is emitted with depot_bound = its depot
                            # label (pickup -> target_depot, delivery -> source_depot), enforced by
                            # the DEPOT_BOUND evaluator gate: freight is served from where it
                            # physically sits; inter-depot movement rides priced trunks only
                            # (2026-07-17 A1: 130/618 legs teleported, worst-case 12.4% km).
                            # --no-depot-pinning = legacy free-assignment emission.
```

### Task 2: Emission stamping in legs.py (TDD)

**Files:** Modify `freight_planner/legs.py`; Test `tests/freight_planner/test_options_legs.py`

- [x] **Step 1: Write failing tests** (append to test_options_legs.py)

```python
# --- Depot pinning (2026-07-17, A1 teleport fix) -----------------------------------
# Freight is served from where it physically sits: pickups bind to the depot the
# freight must LAND at (target_depot), deliveries to the depot it RESTS at
# (source_depot). DIRECT / HUB_DROP never touch a depot and stay unbound.

def test_depot_pinning_defaults_on():
    from freight_planner import config
    assert config.DEPOT_PINNING is True


def _bound_invariant(legs):
    for leg in legs:
        if leg.leg_kind == "CUSTOMER_PICKUP":
            assert leg.depot_bound == leg.target_depot != "", leg.leg_id
        elif leg.leg_kind == "CUSTOMER_DELIVERY":
            assert leg.depot_bound == leg.source_depot != "", leg.leg_id
        else:
            assert leg.depot_bound == "", leg.leg_id


def test_pinned_same_day_xdock_pair_carries_depot_bound():
    qargo = pd.DataFrame([_row()])
    legs = build_movement_leg_records(qargo, [_ff_demand()], _cache())
    assert sorted(l.leg_id.rsplit(":", 1)[1] for l in legs) == ["DIR", "XC", "XD"]
    _bound_invariant(legs)


def test_pinned_multi_day_xdock_pair_carries_depot_bound():
    qargo = pd.DataFrame([_row(destination_timestamp_local="2026-01-09 13:00:00",
                               destination_requested_start_timestamp_local="2026-01-09 13:00:00")])
    legs = build_movement_leg_records(qargo, [_ff_demand(deliver="2026-01-09")], _cache())
    _bound_invariant(legs)


def test_pinning_flag_off_restores_unbound_emission(monkeypatch):
    import freight_planner.config as cfg
    monkeypatch.setattr(cfg, "DEPOT_PINNING", False)
    qargo = pd.DataFrame([_row()])
    legs = build_movement_leg_records(qargo, [_ff_demand()], _cache())
    assert all(l.depot_bound == "" for l in legs)


def test_collocated_bind_survives_pinning_off(monkeypatch):
    # the collocated single-delivery bind is governed by ITS flag, not by pinning
    import freight_planner.config as cfg
    monkeypatch.setattr(cfg, "DEPOT_PINNING", False)
    qargo = pd.DataFrame([_collocated_row()])
    (leg,) = build_movement_leg_records(qargo, [_collocated_demand()], _collocated_cache())
    assert leg.depot_bound == "STOKE"
```

Plus the same `_bound_invariant` applied to PL_EXPORT / PL_IMPORT / LOCAL flows using the fixtures already in `tests/freight_planner/test_legs_staging.py` (add there if its fixtures fit better):

```python
def test_pinned_export_and_local_flows_carry_depot_bound():
    # PL_EXPORT :C binds to the depot its outbound trunk departs from; the HUBDROP
    # option and trunk legs stay unbound. LOCAL_COLLECT :C / LOCAL_DELIVER :D bind.
    ...  # build via test_legs_staging fixtures; assert _bound_invariant over emitted legs
```

- [x] **Step 2: Run to verify RED** — `pytest tests/freight_planner/test_options_legs.py -q` → new tests fail (`depot_bound == ''`).

- [x] **Step 3: Implement** — in legs.py add next to `_collocated_with_depot`:

```python
def _pinned(depot: str) -> str:
    """depot_bound under DEPOT_PINNING (2026-07-17): freight may only be served by
    vehicles homed where it physically sits; '' when the flag is off or no label."""
    return str(depot or "") if getattr(_fp_config(), "DEPOT_PINNING", True) else ""
```

Then stamp the 8 sites (kwargs only; collocated site untouched):
- PL_IMPORT `:D` (~349): `depot_bound=_pinned(dest_depot)`
- PL_EXPORT `:C` (~371): `depot_bound=_pinned(origin_depot)`
- FULL_FLEET multi-day `:C` (~436): `depot_bound=_pinned(origin_depot)`
- FULL_FLEET multi-day `:D` (~447): `depot_bound=_pinned(origin_depot)`
- FULL_FLEET same-day `:XC` (~502): `depot_bound=_pinned(origin_depot)`
- FULL_FLEET same-day `:XD` (~516): `depot_bound=_pinned(origin_depot)`
- LOCAL_COLLECT `:C` (~533): `depot_bound=_pinned(origin_depot)`
- LOCAL_DELIVER `:D` (~549): `depot_bound=_pinned(dest_depot)`

- [x] **Step 4: Run to verify GREEN** — same command; also `pytest tests/freight_planner/test_legs_staging.py -q` (the Stoke pin test asserts the collocated shape still).

### Task 3: Ledger re-key on the :DIR identity (TDD)

**Files:** Modify `freight_planner/run_rolling.py:503-512` and `:628-632`; Test `tests/freight_planner/test_dynamic_loop.py`, `tests/freight_planner/test_structural_fixes.py`

- [x] **Step 1: Write failing tests**

test_dynamic_loop.py:

```python
def test_pinned_ordinary_delivery_is_not_collection_satisfying():
    # Under DEPOT_PINNING every delivery carries depot_bound; only the :DIR
    # reclassification (the order's ONLY leg) discharges a collection.
    from types import SimpleNamespace
    from freight_planner.run_rolling import _collection_satisfying_job
    xd = SimpleNamespace(leg_kind="CUSTOMER_DELIVERY", job_id="JOB:O1:XD", depot_bound="CB22")
    d = SimpleNamespace(leg_kind="CUSTOMER_DELIVERY", job_id="JOB:O1:D", depot_bound="CB22")
    dirleg = SimpleNamespace(leg_kind="CUSTOMER_DELIVERY", job_id="JOB:O1:DIR", depot_bound="STOKE")
    assert not _collection_satisfying_job(xd)
    assert not _collection_satisfying_job(d)
    assert _collection_satisfying_job(dirleg)
```

test_structural_fixes.py (mirror of the existing bound-inclusion test):

```python
def test_serviceable_collect_ids_ignores_pinned_ordinary_deliveries():
    # a FULL_FLEET order whose delivery is merely PINNED (bound, :XD tail) must not
    # enter the collection universe on the delivery's account — only via its pickup
    import pandas as pd
    from freight_planner.run_rolling import serviceable_collect_ids
    legs_df = pd.DataFrame([
        {"order_id": "O1", "leg_id": "O1:XD", "leg_kind": "CUSTOMER_DELIVERY", "depot_bound": "CB22"},
        {"order_id": "O2", "leg_id": "O2:DIR", "leg_kind": "CUSTOMER_DELIVERY", "depot_bound": "STOKE"},
    ])
    out = serviceable_collect_ids({"O1": "FULL_FLEET", "O2": "FULL_FLEET"},
                                  {"O1", "O2"}, legs_df)
    assert out == {"O2"}
```

- [x] **Step 2: Run to verify RED** — both new tests fail (O1 counted today).

- [x] **Step 3: Implement**

`_collection_satisfying_job` (replace line 512 and update docstring):

```python
    if kind != "CUSTOMER_DELIVERY":
        return False
    tail = str(getattr(j, "job_id", "")).rsplit(":", 1)[-1]
    return tail.startswith("DIR")
```

`serviceable_collect_ids` (replace the `bound` block, keep the comment updated):

```python
    bound: set = set()
    if "leg_id" in legs_df.columns:
        tail = legs_df["leg_id"].astype(str).str.rsplit(":", n=1).str[-1]
        bound = set(oid[lk.eq("CUSTOMER_DELIVERY") & tail.str.startswith("DIR")])
```

- [x] **Step 4: Run to verify GREEN** — plus the neighbouring existing tests:
`pytest tests/freight_planner/test_dynamic_loop.py tests/freight_planner/test_structural_fixes.py -q`

### Task 4: Evaluator pickup-bound pin (characterization)

**Files:** Test `tests/freight_planner/test_routing_adapter.py`

- [x] **Step 1: Add the pin** (the DEPOT_BOUND loop is kind-agnostic — expected GREEN immediately; this pins pickups stay gated):

```python
def test_depot_bound_pickup_refused_on_foreign_vehicle():
    # pinning (2026-07-17): a bound PICKUP is only carried by target-depot vehicles
    ...  # mirror test_depot_bound_delivery_* with leg_kind CUSTOMER_PICKUP
```

- [x] **Step 2: Run** — GREEN (characterization; if RED, the evaluator loop is kind-filtered and must be fixed).

### Task 5: CLI flags in both runners (TDD)

**Files:** Modify `freight_planner/run_rolling.py` (parser ~1737, `_apply_vehicle_day_cost_flags` ~1767) and `freight_planner/run_alns.py` (same pattern); Test `tests/freight_planner/test_vehicle_day_cost.py`

- [x] **Step 1: Failing tests** (mirror the existing collocated CLI tests in that file, for both runners):

```python
def test_rolling_cli_depot_pinning_flag_maps_to_config(...):
    # --no-depot-pinning -> config.DEPOT_PINNING False; absent -> untouched
```

- [x] **Step 2: RED**, **Step 3: implement**:

```python
    parser.add_argument("--depot-pinning", action=argparse.BooleanOptionalAction, default=None,
                        help="serve every pickup/delivery only with vehicles homed at its freight's "
                             "depot (default: config, ON since 2026-07-17). --no-depot-pinning = "
                             "legacy free assignment (the teleport ablation)")
```

and in both `_apply` helpers:

```python
    if getattr(args, "depot_pinning", None) is not None:
        _fp_cfg.DEPOT_PINNING = bool(args.depot_pinning)
```

- [x] **Step 4: GREEN.**

### Task 6: Full suite + docs

- [x] `python -m pytest tests/freight_planner -q` → expect 929 + ~10 new, 0 failures. Fix any legacy test that enshrined bound-presence semantics (adjust only if its intent was the collocated identity, which the :DIR key preserves).
- [x] PIPELINE.md: extend the C16 DEPOT_BOUND constraint row (now universal under pinning, not collocated-only).
- [x] RULES.md: add the pinning corollary under the depot-hold rule ("a leg may only be served by a vehicle homed at the depot where its freight rests; inter-depot movement rides priced trunks").
- [x] DESIGN_LOG.md: dated entry (A1 evidence → static pinning decision → accounting re-key).
- [x] experiments/FINAL_CAMPAIGN.md: A1 status → FIXED, pending probe.

### Task 7: Probe + acceptance (spec §Acceptance)

- [x] Reproduce run_collocated2's CLI (read its `run_manifest.json`) into `run_pinned`, background (~27 min).
- [x] Re-run the A1 analysis script against run_pinned → **0 spatial violations** required.
- [x] Deltas vs run_collocated2: ledger (453/0/0 →?), combined km, vehicle-days, unassigned count/reasons. Report honestly — km is EXPECTED to rise.


---

## Execution record (2026-07-17, inline)

All tasks executed in order, TDD throughout. Suite 929 -> **940 green**.
Deviations from plan:
1. Task 3's planned NEW test `test_serviceable_collect_ids_ignores_pinned_ordinary_deliveries`
   was folded into the EXISTING `test_serviceable_collect_ids_includes_depot_bound_deliveries`
   (its intent was the collocated identity; it now pins both directions: :DIR in,
   pinned :XD out). Same for `test_collected_orders_today_counts_bound_delivery`
   (job ids re-tailed to JOB:*:DIR / :XD / :D). The direct predicate unit test
   `test_pinned_ordinary_delivery_is_not_collection_satisfying` was added as planned.
2. Task 4's pickup-bound characterization was GREEN immediately as predicted
   (DEPOT_BOUND loop is kind-agnostic).
Probe: run_pinned launched (Jan 12-13, seed 0, iterations 10000, defaults) —
acceptance = 0 spatial violations via the A1 script + ledger completeness.
