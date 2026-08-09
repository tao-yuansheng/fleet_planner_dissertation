# Week-to-Week State Handover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chain consecutive weekly `freight_planner` runs so week N+1 opens from week N's plan end-state — carrying in-flight vehicles, excluding already-delivered spill orders, and seeding staged freight at the depot the prior plan actually left it.

**Architecture:** A new `freight_planner/handover.py` module owns a small JSON artifact (`handover.json`). The producer derives it from the finished plan's final freight state; the consumer applies it at four points in `run_alns.py` — exclude served orders, patch vehicle availability, correct staged-freight depots, and always re-emit. Empty/absent handover = today's cold-start behavior.

**Tech Stack:** Python 3, pandas, dataclasses, pytest. No new dependencies.

**Standing rules for the executor:** This project makes **NO git commits** — skip every "Commit" step; the repo is not under git. Validation runs (executing `run_alns.py` end-to-end) are performed **inline by the controller, never by a subagent**. Tests live under `tests/freight_planner/`.

---

## Reference facts (already verified against the codebase)

- Driver: `freight_planner/run_alns.py::main()`. Args parsed at lines 131–159; `start`/`end` are `datetime.date`. Key lines: `legs_df` (~201), `vehicle_df = vehicle_states_frame(start)` (~202), `demand_df` (~220), `build_initial_freight_states(...)` (~222), ALNS `avail_overrides=trunk_avail_overrides` (~264), `combined_selected = list(imp.selected) + tour_records` (~318), outputs written under `with runlog.stage("write outputs")` (~320), `plan_dir` defined at ~172.
- `selected_plan_frame(records)` (in `freight_planner/plan_schema.py`) → DataFrame with columns incl. `vehicle_id, vehicle_home_depot, service_date, sequence, order_id, leg_kind, destination_node, planned_arrive, planned_depart, freight_state_after`.
- Freight state constants (`freight_planner/freight_ledger.py`): `FREIGHT_AT_DEPOT = "AT_DEPOT"`, `FREIGHT_DELIVERED = "DELIVERED"`.
- `demand` records (`freight_planner/demand.py`) have `order_id, freight_id, pallets, weight_kg`.
- `build_initial_freight_states(demand, legs, planning_start)` (`freight_planner/state.py:40`) sets `initial_state` per order; `AT_DEPOT_OR_HUB_PENDING` is the prestaged sentinel consumed by `FreightLedger.from_initial_states`.
- A pickup leg dated before `planning_start` is already hard-blocked in `jobs.py:142` (`PRODUCES_DEPOT_FREIGHT`, no successor), so seeding staged freight does NOT cause double-collection.

---

## File structure

- **Create** `freight_planner/handover.py` — dataclasses, JSON load/save, `build_handover` (producer), consumer helpers (`apply_exclusion`, `apply_availability`, `staged_depot_map`). One responsibility: the handover artifact and its application.
- **Modify** `freight_planner/state.py` — `build_initial_freight_states` gains an optional `staged_overrides` param.
- **Modify** `freight_planner/run_alns.py` — `--handover-in` arg, load, four touch points, producer emit.
- **Create** `tests/freight_planner/test_handover.py` — unit + integration tests.

---

### Task 1: Handover dataclasses + JSON load/save

**Files:**
- Create: `freight_planner/handover.py`
- Test: `tests/freight_planner/test_handover.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/freight_planner/test_handover.py
from pathlib import Path

from freight_planner.handover import (
    Handover, VehicleAvailability, StagedFreight, load_handover, save_handover,
)


def test_empty_handover_is_empty():
    h = Handover.empty()
    assert h.is_empty()
    assert h.delivered_order_ids == set()
    assert h.vehicle_availability == []
    assert h.staged_freight == []


def test_load_missing_file_returns_empty():
    h = load_handover(None)
    assert h.is_empty()
    h2 = load_handover("does/not/exist.json")
    assert h2.is_empty()


def test_handover_json_round_trip(tmp_path: Path):
    h = Handover(
        produced_by_start="2026-01-12",
        produced_by_end="2026-01-17",
        vehicle_availability=[VehicleAvailability("N88GNW", "2026-01-19T14:30:00", "BEDFORD")],
        delivered_order_ids={"o1", "o2"},
        staged_freight=[StagedFreight("o3", "f3", "CB22", "2026-01-17T16:00:00", 12.0, 8400.0)],
    )
    p = tmp_path / "handover.json"
    save_handover(h, p)
    back = load_handover(p)
    assert not back.is_empty()
    assert back.produced_by_start == "2026-01-12"
    assert back.delivered_order_ids == {"o1", "o2"}
    assert back.vehicle_availability[0].vehicle_id == "N88GNW"
    assert back.vehicle_availability[0].available_from == "2026-01-19T14:30:00"
    assert back.staged_freight[0].depot == "CB22"
    assert back.staged_freight[0].pallets == 12.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_handover.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'freight_planner.handover'`.

- [ ] **Step 3: Write minimal implementation**

```python
# freight_planner/handover.py
"""Week-to-week state handover artifact.

Week N's run emits ``handover.json``; week N+1 consumes it so its opening state
is week N's plan end-state: in-flight vehicles stay out, already-delivered spill
orders are excluded, and staged freight is seeded at the depot the prior plan
left it. Empty/absent handover == cold start (unchanged single-week behavior).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class VehicleAvailability:
    vehicle_id: str
    available_from: str  # ISO datetime the vehicle is free again
    at_node: str         # where it ends (home depot under whole-tour ownership)


@dataclass(frozen=True)
class StagedFreight:
    order_id: str
    freight_id: str
    depot: str
    ready_time: str
    pallets: float
    weight_kg: float


@dataclass
class Handover:
    produced_by_start: str = ""
    produced_by_end: str = ""
    vehicle_availability: list[VehicleAvailability] = field(default_factory=list)
    delivered_order_ids: set[str] = field(default_factory=set)
    staged_freight: list[StagedFreight] = field(default_factory=list)

    @classmethod
    def empty(cls) -> "Handover":
        return cls()

    def is_empty(self) -> bool:
        return (
            not self.vehicle_availability
            and not self.delivered_order_ids
            and not self.staged_freight
        )

    def to_json_dict(self) -> dict:
        return {
            "produced_by": {"start": self.produced_by_start, "end": self.produced_by_end},
            "vehicle_availability": [asdict(v) for v in self.vehicle_availability],
            "delivered_order_ids": sorted(self.delivered_order_ids),
            "staged_freight": [asdict(s) for s in self.staged_freight],
        }

    @classmethod
    def from_json_dict(cls, d: dict) -> "Handover":
        pb = d.get("produced_by", {}) or {}
        return cls(
            produced_by_start=str(pb.get("start", "") or ""),
            produced_by_end=str(pb.get("end", "") or ""),
            vehicle_availability=[
                VehicleAvailability(str(v.get("vehicle_id", "")), str(v.get("available_from", "")), str(v.get("at_node", "")))
                for v in d.get("vehicle_availability", []) or []
            ],
            delivered_order_ids={str(x) for x in d.get("delivered_order_ids", []) or []},
            staged_freight=[
                StagedFreight(
                    str(s.get("order_id", "")), str(s.get("freight_id", "")),
                    str(s.get("depot", "")), str(s.get("ready_time", "")),
                    float(s.get("pallets", 0.0) or 0.0), float(s.get("weight_kg", 0.0) or 0.0),
                )
                for s in d.get("staged_freight", []) or []
            ],
        )


def save_handover(handover: Handover, path: str | Path) -> None:
    Path(path).write_text(json.dumps(handover.to_json_dict(), indent=2), encoding="utf-8")


def load_handover(path: str | Path | None) -> Handover:
    if not path:
        return Handover.empty()
    p = Path(path)
    if not p.exists():
        return Handover.empty()
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return Handover.empty()
    return Handover.from_json_dict(json.loads(text))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_handover.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit** — SKIP (no git in this project).

---

### Task 2: `build_handover` producer

**Files:**
- Modify: `freight_planner/handover.py`
- Test: `tests/freight_planner/test_handover.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/freight_planner/test_handover.py
import datetime
import pandas as pd

from freight_planner.handover import build_handover


def _plan_frame(rows):
    cols = ["vehicle_id", "vehicle_home_depot", "service_date", "sequence",
            "order_id", "leg_kind", "destination_node", "planned_arrive",
            "planned_depart", "freight_state_after"]
    return pd.DataFrame(rows, columns=cols)


def _demand_frame(rows):
    return pd.DataFrame(rows, columns=["order_id", "freight_id", "pallets", "weight_kg"])


def test_build_handover_detects_delivered_staged_and_inflight():
    # Window Mon..Sat
    ws = datetime.date(2026, 1, 12)
    we = datetime.date(2026, 1, 17)
    plan = _plan_frame([
        # delivered order (pickup then delivery, both in window)
        ["V1", "CB22", "2026-01-13", 1, "oDEL", "CUSTOMER_PICKUP", "CB22", "2026-01-13T09:00:00", "2026-01-13T09:10:00", "AT_DEPOT"],
        ["V1", "CB22", "2026-01-14", 2, "oDEL", "CUSTOMER_DELIVERY", "CUST", "2026-01-14T11:00:00", "2026-01-14T11:20:00", "DELIVERED"],
        # staged order (picked up, never delivered)
        ["V2", "BEDFORD", "2026-01-16", 1, "oSTG", "CUSTOMER_PICKUP", "BEDFORD", "2026-01-16T15:00:00", "2026-01-16T15:10:00", "AT_DEPOT"],
        # in-flight vehicle: a job dated AFTER Saturday
        ["V3", "STOKE", "2026-01-19", 1, "oINF", "CUSTOMER_DELIVERY", "CUST2", "2026-01-19T13:00:00", "2026-01-19T13:20:00", "DELIVERED"],
    ])
    demand = _demand_frame([
        ["oDEL", "oDEL", 5.0, 3000.0],
        ["oSTG", "oSTG", 12.0, 8400.0],
        ["oINF", "oINF", 8.0, 5000.0],
    ])
    h = build_handover(plan, demand, ws, we)
    assert h.produced_by_start == "2026-01-12"
    assert h.produced_by_end == "2026-01-17"
    assert "oDEL" in h.delivered_order_ids
    assert "oINF" in h.delivered_order_ids
    assert "oSTG" not in h.delivered_order_ids
    # staged
    assert [s.order_id for s in h.staged_freight] == ["oSTG"]
    stg = h.staged_freight[0]
    assert stg.depot == "BEDFORD"
    assert stg.pallets == 12.0
    assert stg.weight_kg == 8400.0
    assert stg.ready_time == "2026-01-16T15:10:00"  # pickup depart
    # in-flight vehicle detected (service_date 01-19 > Saturday 01-17), home node = home depot
    inflight = {v.vehicle_id: v for v in h.vehicle_availability}
    assert "V3" in inflight
    assert inflight["V3"].at_node == "STOKE"
    assert inflight["V3"].available_from == "2026-01-19T13:20:00"  # last arrive
    assert "V1" not in inflight and "V2" not in inflight  # home within window
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_handover.py::test_build_handover_detects_delivered_staged_and_inflight -v`
Expected: FAIL with `ImportError: cannot import name 'build_handover'`.

- [ ] **Step 3: Write minimal implementation**

Add to `freight_planner/handover.py` (imports at top: `import datetime`, `import pandas as pd`; and `from freight_planner.freight_ledger import FREIGHT_AT_DEPOT, FREIGHT_DELIVERED`):

```python
def _end_stamp(row: pd.Series) -> str:
    """Best available end timestamp for a plan row: arrive, else depart, else EOD."""
    for col in ("planned_arrive", "planned_depart"):
        v = str(row.get(col) or "")
        if v:
            return v
    sd = str(row.get("service_date") or "")
    return f"{sd[:10]}T23:59:00" if sd else ""


def build_handover(
    selected_df: pd.DataFrame,
    demand_df: pd.DataFrame,
    window_start: datetime.date,
    window_end: datetime.date,
) -> Handover:
    h = Handover(produced_by_start=window_start.isoformat(), produced_by_end=window_end.isoformat())
    if selected_df is None or selected_df.empty:
        return h

    df = selected_df.copy()
    df["order_id"] = df["order_id"].astype(str)
    df["freight_state_after"] = df.get("freight_state_after", "").astype(str)

    # --- delivered: any leg reached DELIVERED ---
    delivered = set(df.loc[df["freight_state_after"] == FREIGHT_DELIVERED, "order_id"])
    h.delivered_order_ids = {o for o in delivered if o}

    # --- staged: reached AT_DEPOT, never DELIVERED ---
    at_depot = set(df.loc[df["freight_state_after"] == FREIGHT_AT_DEPOT, "order_id"])
    staged_ids = {o for o in (at_depot - delivered) if o}
    dmd = demand_df.copy() if demand_df is not None else pd.DataFrame()
    if not dmd.empty:
        dmd["order_id"] = dmd["order_id"].astype(str)
        dmd = dmd.drop_duplicates("order_id").set_index("order_id")
    for oid in sorted(staged_ids):
        pk = df[(df["order_id"] == oid) & (df["freight_state_after"] == FREIGHT_AT_DEPOT)]
        pk = pk.sort_values("sequence")
        depot = str(pk.iloc[-1].get("destination_node") or "") if not pk.empty else ""
        ready = _end_stamp(pk.iloc[-1]) if not pk.empty else ""
        drow = dmd.loc[oid] if (not dmd.empty and oid in dmd.index) else None
        h.staged_freight.append(StagedFreight(
            order_id=oid,
            freight_id=str(drow.get("freight_id") if drow is not None else oid) or oid,
            depot=depot,
            ready_time=ready,
            pallets=float(drow.get("pallets") if drow is not None else 0.0),
            weight_kg=float(drow.get("weight_kg") if drow is not None else 0.0),
        ))

    # --- in-flight vehicles: any job dated after Saturday (window_end) ---
    sd = pd.to_datetime(df["service_date"], errors="coerce").dt.date
    late_mask = sd.notna() & (sd > window_end)
    for vid, grp in df[late_mask].groupby("vehicle_id"):
        grp2 = df[df["vehicle_id"] == vid].copy()
        grp2["_sd"] = pd.to_datetime(grp2["service_date"], errors="coerce")
        grp2 = grp2.sort_values(["_sd", "sequence"])
        last = grp2.iloc[-1]
        home = str(last.get("vehicle_home_depot") or "") or str(last.get("destination_node") or "")
        h.vehicle_availability.append(VehicleAvailability(
            vehicle_id=str(vid),
            available_from=_end_stamp(last),
            at_node=home,
        ))
    return h
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_handover.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit** — SKIP (no git).

---

### Task 3: Consumer helpers — exclusion, availability, staged map

**Files:**
- Modify: `freight_planner/handover.py`
- Test: `tests/freight_planner/test_handover.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/freight_planner/test_handover.py
from freight_planner.handover import apply_exclusion, apply_availability, staged_depot_map


def test_apply_exclusion_drops_delivered_orders():
    legs = pd.DataFrame({"order_id": ["a", "b", "c"], "leg_kind": ["x", "y", "z"]})
    demand = pd.DataFrame({"order_id": ["a", "b", "c"], "pallets": [1, 2, 3]})
    h = Handover(delivered_order_ids={"b"})
    legs2, demand2 = apply_exclusion(legs, demand, h)
    assert set(legs2["order_id"]) == {"a", "c"}
    assert set(demand2["order_id"]) == {"a", "c"}


def test_apply_availability_patches_only_late_known_vehicles():
    import datetime
    ws = datetime.date(2026, 1, 19)
    veh = pd.DataFrame({
        "vehicle_id": ["V1", "V2"],
        "available_from": ["2026-01-19T06:00:00", "2026-01-19T06:00:00"],
    })
    h = Handover(vehicle_availability=[
        VehicleAvailability("V1", "2026-01-19T14:00:00", "BEDFORD"),   # later than window open -> patch
        VehicleAvailability("V2", "2026-01-18T20:00:00", "CB22"),      # before window open -> ignore
        VehicleAvailability("V9", "2026-01-20T00:00:00", "STOKE"),     # unknown reg -> skip, no crash
    ])
    veh2, overrides = apply_availability(veh, h, ws)
    row = {r.vehicle_id: r.available_from for r in veh2.itertuples(index=False)}
    assert row["V1"] == "2026-01-19T14:00:00"
    assert row["V2"] == "2026-01-19T06:00:00"  # unchanged
    assert overrides == {"V1": "2026-01-19T14:00:00"}


def test_staged_depot_map_builds_lookup():
    h = Handover(staged_freight=[
        StagedFreight("o1", "o1", "CB22", "2026-01-17T16:00:00", 5, 3000),
        StagedFreight("o2", "o2", "BEDFORD", "2026-01-16T15:00:00", 8, 5000),
    ])
    m = staged_depot_map(h)
    assert m == {"o1": ("CB22", "2026-01-17T16:00:00"), "o2": ("BEDFORD", "2026-01-16T15:00:00")}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_handover.py -k "exclusion or availability or staged_depot" -v`
Expected: FAIL with `ImportError: cannot import name 'apply_exclusion'`.

- [ ] **Step 3: Write minimal implementation**

Add to `freight_planner/handover.py`:

```python
def apply_exclusion(
    legs_df: pd.DataFrame, demand_df: pd.DataFrame, handover: Handover
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Drop already-delivered orders from both frames so the next week does not
    re-plan spill deliveries that the prior plan's tour tail already served."""
    served = handover.delivered_order_ids
    if not served:
        return legs_df, demand_df
    legs2 = legs_df
    if legs_df is not None and not legs_df.empty and "order_id" in legs_df.columns:
        legs2 = legs_df[~legs_df["order_id"].astype(str).isin(served)].copy()
    demand2 = demand_df
    if demand_df is not None and not demand_df.empty and "order_id" in demand_df.columns:
        demand2 = demand_df[~demand_df["order_id"].astype(str).isin(served)].copy()
    return legs2, demand2


def apply_availability(
    vehicle_df: pd.DataFrame, handover: Handover, window_start: datetime.date
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Patch ``available_from`` for vehicles still in flight at window open.

    Only vehicles present in the fleet AND whose handover availability is later
    than the window's opening midnight are held back. Returns the patched frame
    and an ``{vehicle_id: available_from}`` override dict for ALNS-time gating.
    """
    overrides: dict[str, str] = {}
    if not handover.vehicle_availability or vehicle_df is None or vehicle_df.empty:
        return vehicle_df, overrides
    known = set(vehicle_df["vehicle_id"].astype(str))
    open_iso = f"{window_start.isoformat()}T00:00:00"
    for va in handover.vehicle_availability:
        if va.vehicle_id not in known:
            continue  # fleet regenerated / reg retired -> skip defensively
        if not va.available_from or va.available_from <= open_iso:
            continue  # home before the window opens -> free, no override
        overrides[va.vehicle_id] = va.available_from
    if not overrides:
        return vehicle_df, overrides
    veh = vehicle_df.copy()
    veh["available_from"] = veh.apply(
        lambda r: overrides.get(str(r["vehicle_id"]), r["available_from"]), axis=1
    )
    return veh, overrides


def staged_depot_map(handover: Handover) -> dict[str, tuple[str, str]]:
    """``{order_id: (depot, ready_time)}`` for seeding staged freight at the depot
    the prior plan actually left it, overriding the historical-leg inference."""
    return {s.order_id: (s.depot, s.ready_time) for s in handover.staged_freight}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_handover.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit** — SKIP (no git).

---

### Task 4: `build_initial_freight_states` staged override

**Files:**
- Modify: `freight_planner/state.py:40-136`
- Test: `tests/freight_planner/test_handover.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/freight_planner/test_handover.py
from freight_planner.state import build_initial_freight_states


def _min_demand_legs_for_order(order_id, deliver_depot):
    demand = pd.DataFrame([{
        "order_id": order_id, "order_name": "N", "responsibility_shape": "FULL_END_TO_END",
        "responsibility_source": "verified", "exclusion_reason": "",
        "origin_pc": "CB9 8QP", "collect_timestamp": "2026-01-16T09:00:00",
    }])
    legs = pd.DataFrame([{
        "order_id": order_id, "freight_id": order_id, "leg_kind": "CUSTOMER_DELIVERY",
        "dispatchable": True, "planner_status": "DISPATCHABLE", "source_depot": "CB22",
        "service_pc": "SS6 7NG", "freight_ready_time": "2026-01-20T08:00:00",
        "service_date": "2026-01-20",
    }])
    return demand, legs


def test_staged_override_forces_depot_and_ready_time():
    demand, legs = _min_demand_legs_for_order("oX", "CB22")
    # Without override: inference seeds delivery depot CB22 from the leg.
    base = build_initial_freight_states(demand, legs, planning_start=None)
    assert base[0].initial_depot == "CB22"
    # With override: prior plan staged it at BEDFORD, ready earlier.
    ov = {"oX": ("BEDFORD", "2026-01-17T16:00:00")}
    got = build_initial_freight_states(demand, legs, planning_start=None, staged_overrides=ov)
    rec = got[0]
    assert rec.initial_state == "AT_DEPOT_OR_HUB_PENDING"
    assert rec.initial_depot == "BEDFORD"
    assert rec.initial_node == "BEDFORD"
    assert rec.ready_time == "2026-01-17T16:00:00"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_handover.py::test_staged_override_forces_depot_and_ready_time -v`
Expected: FAIL with `TypeError: build_initial_freight_states() got an unexpected keyword argument 'staged_overrides'`.

- [ ] **Step 3: Write minimal implementation**

Change the signature of `build_initial_freight_states` in `freight_planner/state.py:40-42` from:

```python
def build_initial_freight_states(
    demand: pd.DataFrame, legs: pd.DataFrame, planning_start: date | None = None
) -> list[FreightStateRecord]:
```

to:

```python
def build_initial_freight_states(
    demand: pd.DataFrame, legs: pd.DataFrame, planning_start: date | None = None,
    staged_overrides: dict[str, tuple[str, str]] | None = None,
) -> list[FreightStateRecord]:
```

Then, in the `for freight_id in freight_ids:` loop, immediately **before** the
`out.append(FreightStateRecord(` call (currently `state.py:123`), insert:

```python
            if staged_overrides and order_id in staged_overrides:
                ov_depot, ov_ready = staged_overrides[order_id]
                initial_state = "AT_DEPOT_OR_HUB_PENDING"
                initial_depot = str(ov_depot)
                initial_node = str(ov_depot) or "DEPOT"
                ready_time = str(ov_ready) or ready_time
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_handover.py -v`
Expected: PASS (all).

- [ ] **Step 5: Run the existing state tests for regression**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/ -k state -v`
Expected: PASS (no regressions — the new param defaults to `None`).

- [ ] **Step 6: Commit** — SKIP (no git).

---

### Task 5: Wire the handover into `run_alns.py`

**Files:**
- Modify: `freight_planner/run_alns.py` (arg parsing ~131–159; touch points ~201–222, ~264; producer emit in `with runlog.stage("write outputs")` ~320)

This task has no new unit test of its own (it is glue over already-tested helpers); it is verified by the Task 6 integration test and the controller's inline end-to-end run. Follow each edit exactly.

- [ ] **Step 1: Import the handover module**

Near the other `from freight_planner...` imports (around `run_alns.py:40`, next to `from freight_planner.state import build_initial_freight_states`), add:

```python
from freight_planner.handover import (
    build_handover, load_handover, save_handover,
    apply_exclusion, apply_availability, staged_depot_map,
)
```

- [ ] **Step 2: Add the `--handover-in` argument**

After the `--out-dir` argument (`run_alns.py:135`), add:

```python
    parser.add_argument("--handover-in", default=None,
                        help="path to a prior week's handover.json (opening state); omit for cold start")
```

- [ ] **Step 3: Load the handover once, right after args are parsed**

Immediately after `args = parser.parse_args(argv)` (find it near the top of `main`), add:

```python
    handover = load_handover(args.handover_in)
```

- [ ] **Step 4: Patch vehicle availability + exclude served legs (line ~201-205)**

`candidate_all = candidate_jobs_frame(legs_df, ...)` at `run_alns.py:208` consumes
`legs_df` before demand is built, so leg-level exclusion must happen here at line 201.
Replace the block at `run_alns.py:201-205`:

```python
        legs_df = filter_legs_by_basis(legs_all_df, start, end, args.date_basis)
        vehicle_df = vehicle_states_frame(start)
        fleet_types = dict(zip(vehicle_df["vehicle_id"].astype(str), vehicle_df["vehicle_type"].astype(str)))
        catchment = build_vehicle_catchment(qargo_df, postcode_cache, fleet_types=fleet_types)
        vehicle_df["catchment_km"] = vehicle_df["vehicle_id"].astype(str).map(catchment).fillna(0.0)
```

with:

```python
        legs_df = filter_legs_by_basis(legs_all_df, start, end, args.date_basis)
        if handover.delivered_order_ids and not legs_df.empty:
            legs_df = legs_df[~legs_df["order_id"].astype(str).isin(handover.delivered_order_ids)].copy()
            runlog.log(f"handover: excluded {len(handover.delivered_order_ids)} orders already delivered by prior week")
        vehicle_df = vehicle_states_frame(start)
        vehicle_df, handover_overrides = apply_availability(vehicle_df, handover, start)
        if handover_overrides:
            runlog.log(f"handover: {len(handover_overrides)} vehicles held in-flight at window open")
        fleet_types = dict(zip(vehicle_df["vehicle_id"].astype(str), vehicle_df["vehicle_type"].astype(str)))
        catchment = build_vehicle_catchment(qargo_df, postcode_cache, fleet_types=fleet_types)
        vehicle_df["catchment_km"] = vehicle_df["vehicle_id"].astype(str).map(catchment).fillna(0.0)
```

- [ ] **Step 5: Exclude served orders from demand + seed staged freight (line ~216-222)**

Replace the block at `run_alns.py:216-222`:

```python
        demand_df_all = pd.DataFrame([r.to_dict() for r in demand_records])
        if args.date_basis == "service_date":
            demand_df = align_demand_to_legs(demand_df_all, legs_df)
        else:
            demand_df = filter_demand_by_basis(demand_df_all, start, end, args.date_basis)
            demand_df = align_demand_to_legs(demand_df, legs_df) if not legs_df.empty else demand_df
        freight_states_df = pd.DataFrame([r.to_dict() for r in build_initial_freight_states(demand_df, legs_df, planning_start=start)])
```

with (legs are already excluded in Step 4, so only demand needs it here — `apply_exclusion`
is idempotent and safe on the already-filtered legs):

```python
        demand_df_all = pd.DataFrame([r.to_dict() for r in demand_records])
        if args.date_basis == "service_date":
            demand_df = align_demand_to_legs(demand_df_all, legs_df)
        else:
            demand_df = filter_demand_by_basis(demand_df_all, start, end, args.date_basis)
            demand_df = align_demand_to_legs(demand_df, legs_df) if not legs_df.empty else demand_df
        _, demand_df = apply_exclusion(legs_df, demand_df, handover)
        freight_states_df = pd.DataFrame([r.to_dict() for r in build_initial_freight_states(
            demand_df, legs_df, planning_start=start, staged_overrides=staged_depot_map(handover))])
```

- [ ] **Step 6: Pass handover availability overrides into ALNS**

At `run_alns.py:240`, merge the handover overrides with the trunk overrides:

```python
    trunk_plan = getattr(seed, "trunk", None)
    trunk_avail_overrides = trunk_plan.avail_overrides if trunk_plan is not None else None
    combined_avail_overrides = dict(trunk_avail_overrides or {})
    combined_avail_overrides.update(handover_overrides)
```

Then at `run_alns.py:264` change `avail_overrides=trunk_avail_overrides,` to:

```python
                avail_overrides=(combined_avail_overrides or None),
```

- [ ] **Step 7: Emit the handover at the end of the run**

Inside `with runlog.stage("write outputs"):`, right after `selected_plan_export_frame(combined_selected, route_totals).to_csv(plan_path, index=False)` (`run_alns.py:337`), add:

```python
        out_handover = build_handover(selected_df, demand_df, start, end)
        save_handover(out_handover, plan_dir / "handover.json")
        runlog.log(
            f"handover: emitted {len(out_handover.vehicle_availability)} in-flight vehicles, "
            f"{len(out_handover.delivered_order_ids)} delivered, {len(out_handover.staged_freight)} staged "
            f"-> {plan_dir / 'handover.json'}")
```

(`selected_df` is already computed at `run_alns.py:332`; `demand_df`, `start`, `end`, `plan_dir` are all in scope.)

- [ ] **Step 8: Smoke-check the module imports and CLI parse**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -c "from freight_planner.run_alns import main; import freight_planner.handover; print('imports OK')"`
Expected: prints `imports OK` with no traceback.

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m freight_planner.run_alns --help`
Expected: help text includes `--handover-in`.

- [ ] **Step 9: Commit** — SKIP (no git).

---

### Task 6: Integration test — one-run emit → next-run consume

**Files:**
- Test: `tests/freight_planner/test_handover.py`

This exercises the real chain logic (produce → save → load → apply) at the function
level, without invoking the full `run_alns` pipeline (which needs OSRM + Qargo data
and is the controller's inline validation, not a unit test).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/freight_planner/test_handover.py
def test_chain_emit_then_consume(tmp_path):
    import datetime
    ws1 = datetime.date(2026, 1, 12); we1 = datetime.date(2026, 1, 17)
    ws2 = datetime.date(2026, 1, 19)
    # Week 1 plan: delivers oDEL on the Monday tail (spill), stages oSTG, V3 in-flight.
    plan1 = _plan_frame([
        ["V3", "STOKE", "2026-01-19", 1, "oDEL", "CUSTOMER_DELIVERY", "CUST", "2026-01-19T10:00:00", "2026-01-19T10:20:00", "DELIVERED"],
        ["V2", "BEDFORD", "2026-01-16", 1, "oSTG", "CUSTOMER_PICKUP", "BEDFORD", "2026-01-16T15:00:00", "2026-01-16T15:10:00", "AT_DEPOT"],
    ])
    demand1 = _demand_frame([["oDEL", "oDEL", 5.0, 3000.0], ["oSTG", "oSTG", 12.0, 8400.0]])
    h1 = build_handover(plan1, demand1, ws1, we1)
    p = tmp_path / "handover.json"
    save_handover(h1, p)

    # Week 2 opening state consumes it.
    h = load_handover(p)
    # (a) spill order excluded from week-2 demand + legs
    legs2 = pd.DataFrame({"order_id": ["oDEL", "oNEW"], "leg_kind": ["CUSTOMER_DELIVERY", "CUSTOMER_PICKUP"]})
    demand2 = pd.DataFrame({"order_id": ["oDEL", "oNEW"], "pallets": [5, 9]})
    legs2, demand2 = apply_exclusion(legs2, demand2, h)
    assert set(demand2["order_id"]) == {"oNEW"}
    assert set(legs2["order_id"]) == {"oNEW"}
    # (b) in-flight vehicle held back Monday
    veh2 = pd.DataFrame({"vehicle_id": ["V3", "V2"], "available_from": ["2026-01-19T06:00:00", "2026-01-19T06:00:00"]})
    veh2, overrides = apply_availability(veh2, h, ws2)
    assert overrides == {"V3": "2026-01-19T10:20:00"}
    # (c) staged freight seeds at the prior plan's depot
    assert staged_depot_map(h) == {"oSTG": ("BEDFORD", "2026-01-16T15:10:00")}
```

- [ ] **Step 2: Run test to verify it fails, then passes**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_handover.py::test_chain_emit_then_consume -v`
Expected: since Tasks 1–3 are done, this should PASS immediately. If any assertion fails, fix the helper that produced the wrong value (do not weaken the assertion).

- [ ] **Step 3: Run the full handover test module**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_handover.py -v`
Expected: PASS (all).

- [ ] **Step 4: Run the whole freight_planner suite for regressions**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/ -q`
Expected: all pass (previous count + the new handover tests).

- [ ] **Step 5: Commit** — SKIP (no git).

---

## Post-implementation validation (controller runs inline — NOT a subagent)

After all tasks pass, the controller (not a subagent) runs the real chain end-to-end:

1. Run week 1 (cold start), confirming a `handover.json` is emitted:
   `python -m freight_planner.run_alns --start 2026-01-12 --end 2026-01-17 --out-dir freight_planner/out_wk1 ...`
2. Run week 2 consuming week 1's handover:
   `python -m freight_planner.run_alns --start 2026-01-19 --end 2026-01-24 --out-dir freight_planner/out_wk2 --handover-in freight_planner/out_wk1/forward_structural/planning_window/2026-01-12_to_2026-01-17/plan/handover.json ...`
3. Confirm: the ~45 double-delivered orders no longer appear in both plans; the ~18 in-flight vehicles show reduced Monday availability; coverage stays ~99.9%/100%. Report the km delta as a **stakeholder line** — a km change with better boundary correctness is a conversation, not a silent revert. Snapshot before/after.

---

## Self-review notes (author)

- **Spec coverage:** artifact schema (Task 1), producer from final state (Task 2), three consumer helpers (Task 3), staged override in state.py (Task 4), four touch points + emit + `--handover-in` (Task 5), integration chain + cold-start (Task 6, plus `test_load_missing_file_returns_empty` for cold start). Validation section mirrors the spec.
- **Cold-start guarantee:** `--handover-in` omitted → `load_handover(None)` → `Handover.empty()` → every `apply_*` is a no-op and `staged_depot_map` is `{}`; `build_initial_freight_states(..., staged_overrides={})` behaves as before.
- **Type consistency:** `Handover`, `VehicleAvailability`, `StagedFreight`, `build_handover`, `load_handover`, `save_handover`, `apply_exclusion`, `apply_availability`, `staged_depot_map`, and `staged_overrides` names are identical across all tasks.
