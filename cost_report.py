"""§6.4 cost decomposition emitter — matches the objective (Eq 3).

Takes the per (vehicle, day) objective-cost rows from
``alns.cost_decomposition_from_solution`` and writes:
  * ``cost_decomposition.csv`` — one row per (vehicle, day), every term separate;
  * ``cost_by_type.csv`` — aggregated per vehicle type;
  * ``06b_cost_decomposition.md`` — the per-type table for the chapter.

km_road is the physical (reported) distance; km_phantom is the out-of-area RANKING
penalty km, kept in a SEPARATE column and never folded into reported km or fuel (§5.2).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

_TERMS = ["km_road", "km_phantom", "fuel_cost", "maintenance_cost", "driver_floor_cost",
          "overtime_premium", "evening_late_premium", "lateness_cost", "range_overage_cost",
          "total_cost"]


def _dt(ts):
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


def build_committed_cost_decomposition(route_stops_df, objective_rows: list[dict] | None = None) -> list[dict]:
    """Cost decomposition anchored on the COMMITTED route_stops geometry (physical km),
    so it reconciles EXACTLY with the headline plan km / km_by_type (§6.3a) and shares
    the SAME (vehicle,day)->type grouping. Every objective term (Eq 3) is recomputed on
    the committed plan: fuel = fuel_rate * physical km; driver floor + overtime from the
    committed trip-span chains; evening late premium from the trip clocks; delivery
    lateness = TARDINESS_COEF * minutes_late^POWER over the day's committed deliveries;
    daily-range overage on physical km. ``km_phantom`` (out-of-area RANKING penalty km,
    an OBJECTIVE-only quantity that never enters reported km or fuel, §5.2) is carried
    verbatim from ``objective_rows`` matched by (vehicle,day) when supplied, else 0."""
    from freight_planner import config as _cfg
    from freight_planner.vehicle_cost import (
        driver_hourly_gbp, fuel_cost_per_km, maintenance_cost_per_km, road_cost_per_km,
        guaranteed_shift_hours, overtime_cost_enabled, vehicle_day_cost_enabled,
    )
    rs = route_stops_df
    if rs is None or rs.empty or "leg_km" not in rs.columns:
        return []
    ot_mult = float(_cfg.OT_DUTY_MULTIPLIER)
    floor = guaranteed_shift_hours()
    gap_h = float(_cfg.SPLIT_SHIFT_GAP_H)
    lbase, lramp, lstart = float(_cfg.LATE_PREMIUM_BASE), float(_cfg.LATE_RAMP_PER_HOUR), float(_cfg.LATE_PREMIUM_START_HOUR)
    range_cap = _cfg.DAILY_RANGE_SOFT_KM
    dc_on, ot_on = vehicle_day_cost_enabled(), overtime_cost_enabled()
    # km_phantom and lateness come from the objective per (v,d): phantom is objective-only,
    # and lateness uses the evaluator's convex penalty against each job's TIGHT deadline —
    # route_stops.minutes_late is a different (often all-day-window) basis and would grossly
    # over-count. Both matched by (vehicle,day); committed veh-days with no objective match
    # (e.g. tour days) take 0.
    phantom_by_vd, lateness_by_vd = {}, {}
    for r in (objective_rows or []):
        key = (str(r.get("vehicle_id")), str(r.get("service_date")))
        phantom_by_vd[key] = float(r.get("km_phantom", 0.0) or 0.0)
        lateness_by_vd[key] = float(r.get("lateness_cost", 0.0) or 0.0)

    rows: list[dict] = []
    for (vid, sd), g in rs.groupby(["vehicle_id", "service_date"]):
        vt = str(g["vehicle_type"].iloc[0])
        km_road = float(g["leg_km"].sum())
        # committed trip spans -> duty chains (split-shift idle unpaid)
        spans = []
        for _ti, tg in g.groupby("trip_index"):
            dep = tg.loc[tg["stop_type"] == "depot_start", "planned_depart"]
            ret = tg.loc[tg["stop_type"] == "depot_return", "planned_arrive"]
            s = _dt(dep.iloc[0]) if len(dep) else None
            e = _dt(ret.iloc[0]) if len(ret) else None
            if s is not None and e is not None and e > s:
                spans.append((s, e))
        if not spans:
            # Multi-day TOUR day: depot_start (day 1) and depot_return (day N) land in
            # different (vehicle, service_date) groups, so no same-day trip span forms and
            # the tour's labour-intensive driver time would be priced £0. Pay each tour day
            # from its own activity clock (first->last stamped stop that day). (audit #2)
            times = [t for col in ("planned_depart", "planned_arrive")
                     for t in (_dt(v) for v in g[col].tolist()) if t is not None]
            if len(times) >= 2:
                lo, hi = min(times), max(times)
                if hi > lo:
                    spans.append((lo, hi))
        spans.sort()
        chains, cs, ce = [], None, None
        for s, e in spans:
            if ce is not None and (s - ce).total_seconds() / 3600.0 < gap_h:
                ce = max(ce, e)
            else:
                if cs is not None:
                    chains.append((cs, ce))
                cs, ce = s, e
        if cs is not None:
            chains.append((cs, ce))
        wh = sum((e - s).total_seconds() / 3600.0 for s, e in chains)
        # evening unsocial-hours premium (closed form per trip span)
        late_h = 0.0
        for s, e in spans:
            anchor = s.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=lstart)
            t0 = max(0.0, (s - anchor).total_seconds() / 3600.0)
            t1 = max(0.0, (e - anchor).total_seconds() / 3600.0)
            if t1 > t0:
                late_h += lbase * (t1 - t0) + (lramp / 2.0) * (t1 * t1 - t0 * t0)
        hourly = driver_hourly_gbp(vt)
        fuel = fuel_cost_per_km(vt) * km_road
        maint = maintenance_cost_per_km(vt) * km_road      # R&M/tyres per-km layer (2026-07-25)
        if dc_on and ot_on and wh > 0.0:
            floor_cost = hourly * max(floor, wh)
            overtime = hourly * (ot_mult - 1.0) * max(0.0, wh - floor)
            evening = hourly * late_h
        elif dc_on and wh > 0.0:
            floor_cost, overtime, evening = hourly * max(floor, wh), 0.0, 0.0
        else:
            floor_cost = overtime = evening = 0.0
        lateness = float(lateness_by_vd.get((str(vid), str(sd)), 0.0))
        cap = range_cap.get(vt.lower())
        rng = road_cost_per_km(vt) * max(0.0, km_road - float(cap)) if cap is not None else 0.0
        total = fuel + maint + floor_cost + overtime + evening + lateness + rng
        rows.append({
            "vehicle_id": str(vid), "service_date": str(sd), "vehicle_type": vt,
            "km_road": round(km_road, 2),
            "km_phantom": round(phantom_by_vd.get((str(vid), str(sd)), 0.0), 2),
            "fuel_cost": round(fuel, 2), "maintenance_cost": round(maint, 2),
            "driver_floor_cost": round(floor_cost, 2),
            "overtime_premium": round(overtime, 2), "evening_late_premium": round(evening, 2),
            "lateness_cost": round(lateness, 2), "range_overage_cost": round(rng, 2),
            "total_cost": round(total, 2),
        })
    return rows


def cost_by_type(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["vehicle_type", "vehicle_days", *_TERMS])
    agg = (df.groupby("vehicle_type")[_TERMS].sum().reset_index())
    counts = df.groupby("vehicle_type").size().rename("vehicle_days").reset_index()
    out = counts.merge(agg, on="vehicle_type", how="right")
    total = {"vehicle_type": "ALL", "vehicle_days": int(out["vehicle_days"].sum())}
    for t in _TERMS:
        total[t] = float(out[t].sum())
    return pd.concat([out, pd.DataFrame([total])], ignore_index=True)


def cost_decomposition_md(by_type: pd.DataFrame) -> str:
    lines = [
        "# Cost decomposition (committed plan, Eq 3) — §6.4",
        "",
        "Per-type sum of each objective term (GBP), computed on the WINDOW-CLIPPED committed "
        "route_stops geometry so `km_road` reconciles EXACTLY with the headline committed-route "
        "km and `km_by_type.csv` "
        "(§6.3a) and shares the same (vehicle,day)->type grouping. `km_phantom` = out-of-area "
        "RANKING penalty km, an objective-only quantity EXCLUDED from reported km and fuel (§5.2).",
        "",
        "Separately scheduled trunk km and trunk costs are excluded from this table.",
        "",
        "| type | veh-days | km_road | km_phantom | fuel | maintenance | floor | overtime | evening-late | lateness | range | **total** |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in by_type.itertuples(index=False):
        lines.append(
            f"| {r.vehicle_type} | {int(r.vehicle_days)} | {r.km_road:,.0f} | {r.km_phantom:,.0f} | "
            f"{r.fuel_cost:,.0f} | {r.maintenance_cost:,.0f} | {r.driver_floor_cost:,.0f} | {r.overtime_premium:,.0f} | "
            f"{r.evening_late_premium:,.0f} | {r.lateness_cost:,.0f} | {r.range_overage_cost:,.0f} | "
            f"**{r.total_cost:,.0f}** |")
    return "\n".join(lines) + "\n"


def write_cost_decomposition(rows: list[dict], out_dir, reports_dir=None) -> None:
    # out_dir / reports_dir are RunPaths routers in prod (route .csv->csv/, .md->reports/)
    # and plain Paths in tests; use them DIRECTLY — wrapping in Path() strips the routing.
    if reports_dir is None:
        reports_dir = out_dir
    per_vd = pd.DataFrame(rows, columns=["vehicle_id", "service_date", "vehicle_type", *_TERMS])
    per_vd.to_csv(out_dir / "cost_decomposition.csv", index=False)
    by_type = cost_by_type(rows)
    by_type.to_csv(out_dir / "cost_by_type.csv", index=False)
    (reports_dir / "06b_cost_decomposition.md").write_text(cost_decomposition_md(by_type), encoding="utf-8")
