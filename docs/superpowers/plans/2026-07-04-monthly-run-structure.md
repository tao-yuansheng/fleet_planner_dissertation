# Monthly Run Structure + January Backtest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. NO git commits (standing rule) — the "commit" steps are replaced by "run the full test file green".

**Goal:** Flatten planner output into one browsable month-grouped tree
(`runs/<YYYY-MM>/<window>/…`), add a `run_month` orchestrator that runs a
handover-chained sequence of windows, and a `month_summary` rollup that tracks km
and verifies the handover chain — then run all of January.

**Architecture:** A tiny pure helper (`flat_window_label`) changes only how
`run_alns` composes its output path; `output_layout.run_dirs` is unchanged.
`run_month.py` shells `run_alns` per window (so single-week runs still work) and
chains `--handover-in`. `month_summary.py` is pure-parse + best-effort telematics,
emitting one markdown rollup. Historical `out*` dirs are untouched.

**Tech Stack:** Python 3.12, pandas, pytest, argparse, subprocess. Spec:
`docs/superpowers/specs/2026-07-04-monthly-run-structure-design.md`.

---

## File Structure

- `freight_planner/output_layout.py` — ADD `flat_window_label` (pure). Unchanged otherwise.
- `freight_planner/paths.py` — `DEFAULT_OUT_DIR` → `.../freight_planner/runs`.
- `freight_planner/run_alns.py` — MODIFY lines 177–186: month-grouped base +
  `flat_window_label`; add `handover_in` to the run manifest.
- `freight_planner/month_summary.py` — NEW. Pure KPI parse, telematics odometer/gap,
  handover-chain check, markdown render, CLI.
- `freight_planner/run_month.py` — NEW. Orchestrator CLI (subprocess run_alns + viz + rollup).
- `tests/freight_planner/test_output_layout.py` — ADD `flat_window_label` tests.
- `tests/freight_planner/test_month_summary.py` — NEW.
- `tests/freight_planner/test_run_month.py` — NEW (pure helpers only).

---

## Task 1: Flattened output path

**Files:**
- Modify: `freight_planner/output_layout.py`
- Modify: `freight_planner/paths.py:10`
- Modify: `freight_planner/run_alns.py:177-186`
- Test: `tests/freight_planner/test_output_layout.py`

- [ ] **Step 1: Write failing tests for `flat_window_label`**

Append to `tests/freight_planner/test_output_layout.py`:

```python
from freight_planner.output_layout import flat_window_label


def test_flat_window_label_default_is_clean():
    assert flat_window_label(date(2026, 1, 12), date(2026, 1, 17),
                             "forward_structural", "planning_window") == "2026-01-12_to_2026-01-17"


def test_flat_window_label_suffixes_non_default_mode_and_basis():
    assert flat_window_label(date(2026, 1, 12), date(2026, 1, 17),
                             "raw_api", "service_date") == "2026-01-12_to_2026-01-17__raw_api__service_date"


def test_flat_window_label_suffixes_only_non_default_basis():
    assert flat_window_label(date(2026, 1, 12), date(2026, 1, 17),
                             "forward_structural", "service_date") == "2026-01-12_to_2026-01-17__service_date"
```

- [ ] **Step 2: Run to verify fail**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_output_layout.py -q`
Expected: FAIL — `ImportError: cannot import name 'flat_window_label'`.

- [ ] **Step 3: Implement `flat_window_label`**

Append to `freight_planner/output_layout.py`:

```python
def flat_window_label(
    start: date, end: date, mode: str, basis: str,
    *, default_mode: str = "forward_structural", default_basis: str = "planning_window",
) -> str:
    """Window folder label for the flattened month-grouped layout.

    The common workflow (forward_structural + planning_window) gets a clean
    ``<start>_to_<end>`` label. A non-default mode/basis is appended as a ``__suffix``
    so it can never collide with the default run into the same window folder.
    """
    label = window_label(start, end)
    suffix = ""
    if mode != default_mode:
        suffix += f"__{mode}"
    if basis != default_basis:
        suffix += f"__{basis}"
    return label + suffix
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/freight_planner/test_output_layout.py -q`
Expected: PASS (all, including the 5 pre-existing).

- [ ] **Step 5: Point `DEFAULT_OUT_DIR` at `runs`**

In `freight_planner/paths.py` line 10, change:

```python
DEFAULT_OUT_DIR = LOGISTICS_ROOT / "freight_planner" / "out"
```
to:
```python
DEFAULT_OUT_DIR = LOGISTICS_ROOT / "freight_planner" / "runs"
```

- [ ] **Step 6: Wire the flattened base into `run_alns`**

In `freight_planner/run_alns.py`, replace lines 177–179:

```python
    out_dir = Path(args.out_dir) / args.responsibility_mode / args.date_basis
    window = window_label(start, end)
    _inputs_dir, plan_dir, reports_dir = run_dirs(out_dir, window)
```
with:
```python
    # Flattened, month-grouped layout: <out-dir>/<YYYY-MM>/<window>/{inputs,plan,reports}.
    # mode/basis live in run_manifest.json, not the path (suffixed onto the window only
    # when non-default so they can never collide with the default run).
    out_dir = Path(args.out_dir) / f"{start:%Y-%m}"
    window = flat_window_label(start, end, args.responsibility_mode, args.date_basis)
    _inputs_dir, plan_dir, reports_dir = run_dirs(out_dir, window)
```

Update the import on line 33 to include the new helper:
```python
from freight_planner.output_layout import flat_window_label, run_dirs, window_label, write_run_manifest
```

Add `handover_in` to the manifest payload (after line 186 `"date_basis": args.date_basis,`):
```python
        "handover_in": str(args.handover_in) if args.handover_in else None,
```

- [ ] **Step 7: Full output-layout tests + a run_alns import smoke**

Run: `python -m pytest tests/freight_planner/test_output_layout.py tests/freight_planner/test_run_alns_validation.py -q`
Expected: PASS. If `test_run_alns_validation.py` asserts an old path substring, update that assertion to the flattened `runs/<YYYY-MM>/<window>` form (only if it fails).

---

## Task 2: `month_summary` — KPI parse + km table

**Files:**
- Create: `freight_planner/month_summary.py`
- Test: `tests/freight_planner/test_month_summary.py`

- [ ] **Step 1: Write failing tests for KPI parse + gap arithmetic**

Create `tests/freight_planner/test_month_summary.py`:

```python
from __future__ import annotations

from datetime import date

from freight_planner.month_summary import parse_kpi_text, gap_pct, odometer_by_vehicle_day


KPI = """# KPI Summary

- window: 2026-01-12 to 2026-01-17

## Assignment

- assigned orders: 2373 (99.9% of in-universe)

## Resources & Distance

- vehicle-days: 313 daily + 57 tour
- planned km: 95,463

## Demand Accounting (denominator)

- in-universe orders (planning denominator): 2375

## Fixed trunk service (double-deck 52 pal)

- trunk km: 9,717
- combined: plan 95,463 + trunk 9,717 = 105,180 km
"""


def test_parse_kpi_text_pulls_km_and_assignment():
    r = parse_kpi_text(KPI)
    assert r["in_universe"] == 2375
    assert r["assigned"] == 2373
    assert r["assignment_rate"] == 99.9
    assert r["plan_km"] == 95463.0
    assert r["trunk_km"] == 9717.0
    assert r["combined_km"] == 105180.0
    assert r["vehicle_days"] == 370  # 313 + 57


def test_gap_pct_handles_zero_denominator():
    assert gap_pct(105.0, 100.0) == 5.0
    assert gap_pct(100.0, 0.0) is None
    assert gap_pct(100.0, None) is None


def test_odometer_by_vehicle_day_uses_injected_loader():
    import pandas as pd

    def fake_loader(d):
        # one asset, two pings 100 km apart by haversine (no odometer column)
        return pd.DataFrame({
            "AssetName": ["AB12CDE", "AB12CDE"],
            "LocalTime": pd.to_datetime([f"{d} 08:00", f"{d} 12:00"]),
            "Latitude": [52.0, 52.9], "Longitude": [0.0, 0.0],
        })

    out = odometer_by_vehicle_day([date(2026, 1, 12)], loader=fake_loader)
    assert ("AB12CDE", "2026-01-12") in out
    assert out[("AB12CDE", "2026-01-12")] > 90.0  # ~100 km haversine
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/freight_planner/test_month_summary.py -q`
Expected: FAIL — module `freight_planner.month_summary` does not exist.

- [ ] **Step 3: Implement the pure parse + telematics helpers**

Create `freight_planner/month_summary.py`:

```python
"""Month rollup for a set of handover-chained weekly runs.

Scans ``runs/<month>/<window>/`` and emits ``runs/<month>/month_summary.md``:
a km table (plan-side month trend + honest odometer gap, per thread (a)) and a
handover-continuity table (did week N+1 consume exactly week N's end-state?).
Pure parse + best-effort telematics; a failed telematics day degrades to n/a.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd

from freight_planner import vehicle_actuals
from freight_planner.vehicles import vehicle_states_frame


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_kpi_text(text: str) -> dict:
    """Pull km + assignment numbers out of a kpi_summary.md body."""
    out: dict = {}
    m = re.search(r"in-universe orders \(planning denominator\):\s*([\d,]+)", text)
    if m:
        out["in_universe"] = int(m.group(1).replace(",", ""))
    m = re.search(r"assigned orders:\s*([\d,]+)\s*\(([\d.]+)%", text)
    if m:
        out["assigned"] = int(m.group(1).replace(",", ""))
        out["assignment_rate"] = float(m.group(2))
    m = re.search(r"planned km:\s*([\d,]+(?:\.\d+)?)", text)
    if m:
        out["plan_km"] = _num(m.group(1))
    m = re.search(r"trunk km:\s*([\d,]+(?:\.\d+)?)", text)
    if m:
        out["trunk_km"] = _num(m.group(1))
    m = re.search(r"combined:.*?=\s*([\d,]+(?:\.\d+)?)\s*km", text)
    out["combined_km"] = _num(m.group(1)) if m else (
        out.get("plan_km", 0.0) + out.get("trunk_km", 0.0))
    m = re.search(r"vehicle-days:\s*([\d,]+)\s*daily\s*\+\s*([\d,]+)\s*tour", text)
    if m:
        out["vehicle_days"] = int(m.group(1).replace(",", "")) + int(m.group(2).replace(",", ""))
    return out


def parse_kpi(plan_dir: Path) -> dict:
    p = plan_dir / "kpi_summary.md"
    return parse_kpi_text(p.read_text(encoding="utf-8")) if p.exists() else {}


def gap_pct(numer: float | None, denom: float | None) -> float | None:
    if not numer or not denom:
        return None
    return round((numer / denom - 1.0) * 100.0, 1)


def _norm(v) -> str:
    return str(v or "").replace(" ", "").upper()


def odometer_by_vehicle_day(
    days: list[date], *, loader: Callable | None = None,
) -> dict[tuple[str, str], float]:
    """{(norm_vehicle, iso_day): odometer_km} over ``days`` (best-effort; a failed
    day is skipped). Uses the true odometer (miles->km) with haversine fallback."""
    out: dict[tuple[str, str], float] = {}
    for d in days:
        try:
            km = (vehicle_actuals.actual_km_by_vehicle(d, prefer_odometer=True, loader=loader)
                  if loader else vehicle_actuals.actual_km_by_vehicle(d, prefer_odometer=True))
        except Exception:
            continue
        for asset, v in km.items():
            out[(_norm(asset), d.isoformat())] = float(v)
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/freight_planner/test_month_summary.py -q`
Expected: PASS.

---

## Task 3: `month_summary` — week reality row + handover chain + render

**Files:**
- Modify: `freight_planner/month_summary.py`
- Test: `tests/freight_planner/test_month_summary.py`

- [ ] **Step 1: Write failing tests for handover-chain + render**

Append to `tests/freight_planner/test_month_summary.py`:

```python
from freight_planner.month_summary import handover_hop, render_markdown


def test_handover_hop_match_when_consumer_took_producer_delivered():
    producer = {"delivered_order_ids": ["A", "B", "C"],
                "vehicle_availability": [{"vehicle_id": "V1"}]}
    consumer_manifest = {"handover_in": "/x/2026-01-05_to_2026-01-10/plan/handover.json"}
    hop = handover_hop("2026-01-05_to_2026-01-10", "2026-01-12_to_2026-01-17",
                       producer, consumer_manifest, producer_path="/x/2026-01-05_to_2026-01-10/plan/handover.json")
    assert hop["prev_delivered"] == 3
    assert hop["in_flight"] == 1
    assert hop["match"] is True


def test_handover_hop_mismatch_when_consumer_points_elsewhere():
    producer = {"delivered_order_ids": ["A"], "vehicle_availability": []}
    consumer_manifest = {"handover_in": "/x/SOMEWHERE_ELSE/handover.json"}
    hop = handover_hop("w1", "w2", producer, consumer_manifest,
                       producer_path="/x/2026-01-05_to_2026-01-10/plan/handover.json")
    assert hop["match"] is False


def test_render_markdown_has_both_gap_columns():
    rows = [{"window": "2026-01-12_to_2026-01-17", "in_universe": 2375, "assignment_rate": 99.9,
             "plan_km": 95463.0, "trunk_km": 9717.0, "combined_km": 105180.0,
             "odo6d": 89571.0, "gap_naive": 17.4, "gap_matched": 8.8}]
    hops = [{"from": "w0", "to": "w1", "prev_delivered": 46, "in_handover_delivered": 46,
             "in_flight": 18, "match": True}]
    md = render_markdown("2026-01", rows, hops)
    assert "gap naive" in md and "gap matched" in md
    assert "89,571" in md
    assert "handover" in md.lower()
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/freight_planner/test_month_summary.py -q`
Expected: FAIL — `handover_hop`, `render_markdown` not defined.

- [ ] **Step 3: Implement hop + render + the telematics week row + CLI**

Append to `freight_planner/month_summary.py`:

```python
def _load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def window_days(window: str) -> list[date]:
    """The 6 (or fewer) Mon..Sat operating days of a ``<start>_to_<end>`` window."""
    start_s, end_s = window[:10], window[15:25]
    s, e = date.fromisoformat(start_s), date.fromisoformat(end_s)
    return [s + timedelta(days=i) for i in range((e - s).days + 1)]


def week_reality(window: str, plan_dir: Path, kpi: dict,
                 *, loader: Callable | None = None) -> dict:
    """Add odometer + both gap columns to a parsed KPI row (best-effort telematics)."""
    row = {"window": window, **kpi}
    days = window_days(window)
    fleet = {_norm(v) for v in vehicle_states_frame(days[0])["vehicle_id"]}
    odo_vd = odometer_by_vehicle_day(days, loader=loader)
    # window odometer, our-fleet only (the 89,571/92,789 quantity)
    odo6d = sum(km for (vid, _), km in odo_vd.items() if vid in fleet)
    row["odo6d"] = round(odo6d, 0) if odo6d else None
    row["gap_naive"] = gap_pct(kpi.get("combined_km"), odo6d)
    # matched (vehicle,day): plan km on veh-days that have odometer vs that odometer
    vr_path = plan_dir / "vehicle_routes.csv"
    if vr_path.exists() and odo_vd:
        vr = pd.read_csv(vr_path)
        vr["vid"] = vr["vehicle_id"].map(_norm)
        vr["sd"] = vr["service_date"].astype(str).str[:10]
        plan_matched = odo_matched = 0.0
        for r in vr.itertuples(index=False):
            key = (r.vid, r.sd)
            if key in odo_vd:
                plan_matched += float(r.planned_km)
                odo_matched += odo_vd[key]
        row["gap_matched"] = gap_pct(plan_matched, odo_matched)
    else:
        row["gap_matched"] = None
    return row


def handover_hop(from_win: str, to_win: str, producer: dict, consumer_manifest: dict,
                 *, producer_path: str) -> dict:
    """One chain link: did the consumer week consume exactly the producer's handover?"""
    consumed = str(consumer_manifest.get("handover_in") or "")
    delivered = list(producer.get("delivered_order_ids") or [])
    return {
        "from": from_win, "to": to_win,
        "prev_delivered": len(delivered),
        "in_handover_delivered": len(delivered) if consumed else 0,
        "in_flight": len(producer.get("vehicle_availability") or []),
        "match": bool(consumed) and Path(consumed).as_posix() == Path(producer_path).as_posix(),
    }


def _fmt(v, kind="int"):
    if v is None:
        return "n/a"
    if kind == "int":
        return f"{v:,.0f}"
    if kind == "pct":
        return f"{v:+.1f}%"
    return str(v)


def render_markdown(month: str, rows: list[dict], hops: list[dict]) -> str:
    L = [f"# Month Summary — {month}", "",
         "## KM tracking (per window)", "",
         "| window | in-univ | assigned % | plan km | trunk km | combined | odo (6d) | gap naive¹ | gap matched² |",
         "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    tot = {k: 0.0 for k in ("plan_km", "trunk_km", "combined_km", "odo6d")}
    for r in rows:
        L.append("| {w} | {iu} | {ar} | {pk} | {tk} | {ck} | {od} | {gn} | {gm} |".format(
            w=r["window"], iu=_fmt(r.get("in_universe")),
            ar=(f"{r.get('assignment_rate')}%" if r.get("assignment_rate") is not None else "n/a"),
            pk=_fmt(r.get("plan_km")), tk=_fmt(r.get("trunk_km")), ck=_fmt(r.get("combined_km")),
            od=_fmt(r.get("odo6d")), gn=_fmt(r.get("gap_naive"), "pct"), gm=_fmt(r.get("gap_matched"), "pct")))
        for k in tot:
            tot[k] += (r.get(k) or 0.0)
    L.append("| **month total** |  |  | {pk} | {tk} | {ck} | {od} | {gn} | {gm} |".format(
        pk=_fmt(tot["plan_km"]), tk=_fmt(tot["trunk_km"]), ck=_fmt(tot["combined_km"]),
        od=_fmt(tot["odo6d"]), gn=_fmt(gap_pct(tot["combined_km"], tot["odo6d"]), "pct"),
        gm="—"))
    L += ["", "¹ fleet-wide, incl. out-of-scope reality + multi-day tail — a known artifact, not real inefficiency.",
          "² honest (vehicle,day)-matched gap (thread a) — the number we trust.", "",
          "## Handover continuity (per hop)", "",
          "| from → to | prev delivered | consumed | in-flight | match |",
          "|---|---:|---:|---:|:---:|"]
    for h in hops:
        L.append("| {f} → {t} | {pd} | {ih} | {inf} | {m} |".format(
            f=h["from"], t=h["to"], pd=h["prev_delivered"], ih=h["in_handover_delivered"],
            inf=h["in_flight"], m=("✓" if h["match"] else "✗")))
    return "\n".join(L) + "\n"


def build_month_summary(month_dir: Path, *, loader: Callable | None = None) -> Path:
    """Scan month_dir/<window>/ folders (chronological), write month_summary.md."""
    windows = sorted(
        p.name for p in month_dir.iterdir()
        if p.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}_to_\d{4}-\d{2}-\d{2}", p.name))
    rows, plan_dirs = [], {}
    for w in windows:
        plan_dir = month_dir / w / "plan"
        plan_dirs[w] = plan_dir
        rows.append(week_reality(w, plan_dir, parse_kpi(plan_dir), loader=loader))
    hops = []
    for prev, cur in zip(windows, windows[1:]):
        producer_path = plan_dirs[prev] / "handover.json"
        producer = _load_json(producer_path)
        manifest = _load_json(month_dir / cur / "run_manifest.json")
        hops.append(handover_hop(prev, cur, producer, manifest,
                                 producer_path=str(producer_path)))
    out = month_dir / "month_summary.md"
    out.write_text(render_markdown(month_dir.name, rows, hops), encoding="utf-8")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Month rollup for handover-chained runs")
    ap.add_argument("--month-dir", required=True, help="e.g. freight_planner/runs/2026-01")
    args = ap.parse_args(argv)
    path = build_month_summary(Path(args.month_dir))
    print(f"month summary -> {path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/freight_planner/test_month_summary.py -q`
Expected: PASS (all).

---

## Task 4: `run_month` orchestrator

**Files:**
- Create: `freight_planner/run_month.py`
- Test: `tests/freight_planner/test_run_month.py`

- [ ] **Step 1: Write failing tests for the pure helpers**

Create `tests/freight_planner/test_run_month.py`:

```python
from __future__ import annotations

from datetime import date
from pathlib import Path

from freight_planner.run_month import parse_window, plan_dir_for


def test_parse_window_splits_on_colon():
    assert parse_window("2026-01-12:2026-01-17") == (date(2026, 1, 12), date(2026, 1, 17))


def test_plan_dir_for_matches_flattened_layout():
    root = Path("/r")
    pd_ = plan_dir_for(root, date(2026, 1, 12), date(2026, 1, 17),
                       "forward_structural", "planning_window")
    assert pd_ == root / "2026-01" / "2026-01-12_to_2026-01-17" / "plan"
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/freight_planner/test_run_month.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `run_month`**

Create `freight_planner/run_month.py`:

```python
"""Run a handover-chained sequence of weekly windows into one month folder.

Shells ``run_alns`` per window (so a single week stays independently runnable),
wiring each week's ``--handover-in`` to the prior week's emitted handover.json.
After the chain, emits per-week viz and the month rollup. NO planner logic here.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

from freight_planner.demand import FORWARD_STRUCTURAL
from freight_planner.month_summary import build_month_summary
from freight_planner.output_layout import flat_window_label
from freight_planner.paths import DEFAULT_OUT_DIR


def parse_window(s: str) -> tuple[date, date]:
    a, b = s.split(":")
    return date.fromisoformat(a), date.fromisoformat(b)


def plan_dir_for(out_root: Path, start: date, end: date, mode: str, basis: str) -> Path:
    win = flat_window_label(start, end, mode, basis)
    return out_root / f"{start:%Y-%m}" / win / "plan"


def _run(cmd: list[str]) -> int:
    print("»", " ".join(cmd), flush=True)
    return subprocess.run(cmd).returncode


def main(argv=None):
    ap = argparse.ArgumentParser(description="Handover-chained month run")
    ap.add_argument("--windows", nargs="+", required=True, help="START:END ... in order")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--time-budget", type=float, default=120.0)
    ap.add_argument("--responsibility-mode", default=FORWARD_STRUCTURAL)
    ap.add_argument("--date-basis", default="planning_window")
    ap.add_argument("--no-viz", dest="viz", action="store_false")
    ap.add_argument("--summary-only", action="store_true",
                    help="skip running; just (re)build the month rollup for the windows' month")
    args = ap.parse_args(argv)

    out_root = Path(args.out_dir)
    windows = [parse_window(w) for w in args.windows]
    months = {f"{s:%Y-%m}" for s, _ in windows}

    if not args.summary_only:
        prev_handover: Path | None = None
        for start, end in windows:
            plan_dir = plan_dir_for(out_root, start, end, args.responsibility_mode, args.date_basis)
            cmd = [sys.executable, "-m", "freight_planner.run_alns",
                   "--start", start.isoformat(), "--end", end.isoformat(),
                   "--out-dir", str(out_root), "--time-budget", str(args.time_budget),
                   "--responsibility-mode", args.responsibility_mode, "--date-basis", args.date_basis]
            if prev_handover is not None:
                cmd += ["--handover-in", str(prev_handover)]
            if _run(cmd) != 0:
                print(f"!! run failed for {start}..{end}; stopping chain", file=sys.stderr)
                return 1
            prev_handover = plan_dir / "handover.json"
            if args.viz:
                _run([sys.executable, "-m", "freight_planner.viz_app",
                      "--plan-dir", str(plan_dir), "--with-actuals"])

    for m in sorted(months):
        path = build_month_summary(out_root / m)
        print(f"month summary -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run to verify pass + confirm viz CLI flags**

Run: `python -m pytest tests/freight_planner/test_run_month.py -q`
Expected: PASS.

Then confirm the viz invocation matches the real `viz_app` CLI:
Run: `python -m freight_planner.viz_app --help`
Expected: shows `--plan-dir` and `--with-actuals` (or the actuals flag name). If the
flag differs (e.g. `--validate`), fix the `_run([... viz_app ...])` line in Step 3 to
match, and re-run the test.

---

## Task 5: Smoke test + full January run (inline validation — controller, not a subagent)

- [ ] **Step 1: Full unit suite green**

Run: `python -m pytest tests/freight_planner/test_output_layout.py tests/freight_planner/test_month_summary.py tests/freight_planner/test_run_month.py -q`
Expected: PASS.

- [ ] **Step 2: Single-week smoke (Jan 5–10, cold start)**

Run:
```bash
python -m freight_planner.run_month --windows 2026-01-05:2026-01-10 --time-budget 120
```
Expected: creates `freight_planner/runs/2026-01/2026-01-05_to_2026-01-10/{inputs,plan,reports}`,
a `plan/handover.json`, a `reports/trip_app.html`, and `runs/2026-01/month_summary.md`
with one KM row. Verify the plan dir path is the flattened form (no
`forward_structural/planning_window` in it).

- [ ] **Step 3: Full chain (all 5 windows, background)**

Run (background — ~20–40 min):
```bash
python -m freight_planner.run_month \
  --windows 2026-01-01:2026-01-03 2026-01-05:2026-01-10 2026-01-12:2026-01-17 \
            2026-01-19:2026-01-24 2026-01-26:2026-01-31 \
  --time-budget 120
```
Expected: 5 run dirs under `runs/2026-01/`, each with `plan/handover.json`; the
Jan 5–10 dir is reused/overwritten cleanly.

- [ ] **Step 4: Verify the month rollup**

Read `freight_planner/runs/2026-01/month_summary.md`. Confirm:
- KM table has 5 window rows + a month-total row; `plan km` tracks across the month;
  `gap matched` is single-digit-ish and `gap naive` is clearly labelled as the artifact.
- Handover table has 4 hops, **every `match` = ✓** (each week consumed exactly the
  prior week's handover.json).
- Per-week coverage (from each `kpi_summary.md`) stays ≥99.9%.

Report the month km trend + handover-chain result as a stakeholder line.

---

## Self-review notes

- **Spec coverage:** flatten (T1), DEFAULT_OUT_DIR (T1), non-default suffix guard (T1),
  orchestrator + handover chain (T4), rollup km table w/ both gaps (T2–T3), handover
  continuity (T3), smoke-then-full run (T5), historical dirs untouched (no task deletes
  them). All covered.
- **YAGNI:** no multi-month abstraction; `run_month` takes an explicit window list.
- **Type consistency:** `flat_window_label(start,end,mode,basis)` signature identical in
  output_layout, run_alns (T1), run_month.plan_dir_for (T4). `parse_kpi`/`week_reality`/
  `handover_hop`/`render_markdown`/`build_month_summary` names consistent T2↔T3↔T4.
```
