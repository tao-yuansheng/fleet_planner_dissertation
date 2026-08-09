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
from freight_planner.output_layout import artifact_dir
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
    p = plan_dir / "02_kpi_summary.md"
    if not p.exists():                       # pre-2026-07-16 runs
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
            if loader is not None:
                km = vehicle_actuals.actual_km_by_vehicle(d, prefer_odometer=True, loader=loader)
            else:
                km = vehicle_actuals.actual_km_by_vehicle(d, prefer_odometer=True)
        except Exception:
            continue
        for asset, v in km.items():
            out[(_norm(asset), d.isoformat())] = float(v)
    return out


def _load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def window_days(window: str) -> list[date]:
    """The Mon..Sat operating days of a ``<start>_to_<end>[__suffix]`` window."""
    start_s, _, rest = window.partition("_to_")
    s, e = date.fromisoformat(start_s[:10]), date.fromisoformat(rest[:10])
    return [s + timedelta(days=i) for i in range((e - s).days + 1)]


def week_reality(window: str, plan_dir: Path, kpi: dict,
                 *, loader: Callable | None = None) -> dict:
    """A KPI row plus odometer + both gap columns (best-effort telematics)."""
    row = {"window": window, **kpi}
    days = window_days(window)
    fleet = {_norm(v) for v in vehicle_states_frame(days[0])["vehicle_id"]}
    odo_vd = odometer_by_vehicle_day(days, loader=loader)
    odo6d = sum(km for (vid, _), km in odo_vd.items() if vid in fleet)
    row["odo6d"] = round(odo6d, 0) if odo6d else None
    row["gap_naive"] = gap_pct(kpi.get("combined_km"), odo6d)
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
        row["plan_matched"] = round(plan_matched, 0)
        row["odo_matched"] = round(odo_matched, 0)
        row["gap_matched"] = gap_pct(plan_matched, odo_matched)
    else:
        row["plan_matched"] = row["odo_matched"] = None
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
    tot = {k: 0.0 for k in ("plan_km", "trunk_km", "combined_km", "odo6d", "plan_matched", "odo_matched")}
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
        gm=_fmt(gap_pct(tot["plan_matched"], tot["odo_matched"]), "pct")))
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
        # dual-layout: a current window routes joins via csv/md/root (RunPaths);
        # a legacy window keeps its files under plan/
        plan_dir = artifact_dir(month_dir / w)
        if isinstance(plan_dir, Path):
            plan_dir = month_dir / w / "plan"
        plan_dirs[w] = plan_dir
        rows.append(week_reality(w, plan_dir, parse_kpi(plan_dir), loader=loader))
    hops = []
    for prev, cur in zip(windows, windows[1:]):
        producer_path = plan_dirs[prev] / "handover.json"
        producer = _load_json(producer_path)
        manifest = _load_json(month_dir / cur / "run_manifest.json")
        hops.append(handover_hop(prev, cur, producer, manifest, producer_path=str(producer_path)))
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
