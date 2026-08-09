"""Milestone 9: write the operator-facing manifest + KPI outputs for a run.

Shared by the seed and ALNS CLIs so both emit the same five files and the same
denominator. Returns the KPI report and whether every windowed order with a leg
landed in the manifest (the spec's full-accounting check).
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from freight_planner.kpi import (
    KpiReport, build_kpi, delivery_timeliness_md, kpi_summary_md, relabel_beyond_window)
from freight_planner.trunk import trunk_schedule_frame
from freight_planner.manifest import (
    build_depot_inventory_timeline,
    build_plan_manifest,
    build_route_stops,
    build_unassigned,
    build_vehicle_routes,
    clip_route_stops_to_window,
    geom_route_totals,
)
from freight_planner.utilization import (
    build_trip_capacity_utilization,
    build_vehicle_day_utilization,
    utilization_summary_md,
)


def write_reports(
    out_dir: Path,
    *,
    start: date,
    end: date,
    demand_df: pd.DataFrame,
    legs_all_df: pd.DataFrame,
    candidate_df: pd.DataFrame,
    compatibility_df: pd.DataFrame,
    vehicle_df: pd.DataFrame,
    selected_df: pd.DataFrame,
    rejected,
    tours,
    planned_km: float,
    cross_depot_km: float,
    phantom_deliveries: int,
    route_totals: dict | None = None,
    route_times: dict | None = None,
    trunk=None,
    shuttle_job_ids=None,
    handover_order_ids=None,
    route_stops_df: pd.DataFrame | None = None,
) -> tuple[KpiReport, bool]:
    # One relabel before ANY emitter, so manifest / unassigned_jobs.csv / KPI /
    # universe report all agree: a job whose service window opens after the last
    # window day is DUE_BEYOND_WINDOW (staged handover), not NO_FEASIBLE_ROUTE.
    rejected = relabel_beyond_window(rejected, candidate_df, end.isoformat())
    build_unassigned(rejected, candidate_df, selected_df=selected_df).to_csv(
        out_dir / "unassigned_jobs.csv", index=False)
    build_depot_inventory_timeline(selected_df, candidate_df).to_csv(
        out_dir / "depot_inventory_timeline.csv", index=False)
    # route_stops is the SINGLE SOURCE OF TRUTH for committed distance: daily return
    # legs are real road geometry, tours carry their multi-day distance. Every km
    # report below (manifest, vehicle_routes, utilization, KPI planned_km) derives
    # from this one geometry so the CSVs and the markdown always agree, and the
    # phantom evaluator route_totals never reaches an output number. The caller may
    # pass a prebuilt route_stops_df (so selected_plan_alns / cross-depot upstream
    # share the exact same geometry); otherwise build it here. route_totals is used
    # only to source tours' multi-day residual return.
    if route_stops_df is None:
        tour_return_dates = {}
        for ta in tours or []:
            rid = f"TOUR:{ta.vehicle_id}:{ta.start_date}"
            end_d = date.fromisoformat(str(ta.start_date)) + timedelta(days=max(0, int(ta.days) - 1))
            tour_return_dates[rid] = end_d.isoformat()
        route_stops_df = build_route_stops(
            selected_df, candidate_df, compatibility_df, vehicle_df, route_totals,
            tour_return_dates=tour_return_dates, route_times=route_times)
    route_stops_df.to_csv(out_dir / "route_stops.csv", index=False)
    geom_totals = geom_route_totals(route_stops_df)
    # planned_km reported from the committed geometry, not the optimizer's own
    # km_after (which carried the seed-epoch phantom). WINDOW-CLIPPED to [start,end]
    # so the headline matches the incumbent side (also window-capped); the full
    # route_stops.csv above keeps any beyond-window tour-return tail for the map.
    # (decision-audit #1, 2026-07-26)
    _rs_window = clip_route_stops_to_window(route_stops_df, start.isoformat(), end.isoformat())
    planned_km = float(_rs_window["leg_km"].sum()) if (
        _rs_window is not None and not _rs_window.empty) else float(planned_km)

    manifest = build_plan_manifest(selected_df, legs_all_df, rejected, candidate_df, geom_totals)
    manifest.to_csv(out_dir / "plan_manifest_new.csv", index=False)
    build_vehicle_routes(selected_df, geom_totals, route_stops=route_stops_df).to_csv(
        out_dir / "vehicle_routes.csv", index=False)

    vehicle_day_util = build_vehicle_day_utilization(selected_df, geom_totals, route_stops_df=route_stops_df)
    trip_capacity_util = build_trip_capacity_utilization(selected_df, candidate_df, vehicle_df)
    vehicle_day_util.to_csv(out_dir / "vehicle_day_utilization.csv", index=False)
    trip_capacity_util.to_csv(out_dir / "trip_capacity_utilization.csv", index=False)

    # §6.1 believability floor: re-derive the feasibility-violation counts (capacity /
    # duty-13h / drive-10h / window) from the committed artifacts — expected all 0.
    from freight_planner.feasibility_audit import build_feasibility_audit, feasibility_audit_md
    _feas = build_feasibility_audit(vehicle_day_util, trip_capacity_util, route_stops_df)
    pd.DataFrame([_feas]).to_csv(out_dir / "feasibility_audit.csv", index=False)
    (out_dir / "09_feasibility_audit.md").write_text(feasibility_audit_md(_feas), encoding="utf-8")

    if trunk is not None and trunk.nights:
        trunk_schedule_frame(trunk.nights).to_csv(out_dir / "trunk_schedule.csv", index=False)

    if shuttle_job_ids:
        pd.DataFrame({"job_id": sorted(shuttle_job_ids)}).to_csv(
            out_dir / "shuttle_jobs.csv", index=False)

    report = build_kpi(start.isoformat(), end.isoformat(), demand_df, candidate_df,
                       selected_df, rejected, tours, planned_km, cross_depot_km, phantom_deliveries,
                       trunk_km=float(trunk.total_km) if trunk is not None else 0.0,
                       trunk_trips=int(trunk.total_trips) if trunk is not None else 0,
                       trunk_shortfall_nights=len(trunk.shortfalls) if trunk is not None else 0,
                       handover_order_ids=handover_order_ids)
    # (plan_stability_md below is appended by the ROLLING finalize, which owns the
    # churn rows — the static path has no epochs, so no stability section.)
    kpi_md = kpi_summary_md(report).rstrip()
    timeliness_md = delivery_timeliness_md(route_stops_df)
    if timeliness_md:
        kpi_md = f"{kpi_md}\n\n{timeliness_md.rstrip()}"
    util_md = utilization_summary_md(vehicle_day_util, trip_capacity_util).strip()
    # Numbered operator reports, most important first (user rule 2026-07-16):
    # 01 service (dynamic path) / 02 kpi / 03 fleet / 04 tours / 05 universe /
    # 06 choices / 07 alns / 08 dictionary.
    (out_dir / "02_kpi_summary.md").write_text(f"{kpi_md}\n\n{util_md}\n", encoding="utf-8")
    fleet_lines = [util_md, "", "## Fleet usage (vehicles per day)", ""]
    fleet_n = int(vehicle_df["vehicle_id"].nunique()) if (
        vehicle_df is not None and not vehicle_df.empty) else 0
    if not vehicle_day_util.empty:
        for d, n in vehicle_day_util.groupby("service_date")["vehicle_id"].nunique().items():
            fleet_lines.append(f"- {d}: {int(n)} of {fleet_n} vehicles used")
    else:
        fleet_lines.append("- (no vehicle-days)")
    (out_dir / "03_fleet_utilization.md").write_text("\n".join(fleet_lines) + "\n", encoding="utf-8")

    tour_lines = ["# Tour report", ""]
    born = {}
    if selected_df is not None and not selected_df.empty and "route_id" in selected_df.columns:
        tsel = selected_df[selected_df["route_id"].astype(str).str.startswith("TOUR:")]
        if not tsel.empty and "assignment_reason" in tsel.columns:
            born = tsel.groupby("route_id")["assignment_reason"].agg(
                lambda x: ",".join(sorted({str(v) for v in x if str(v)}))).to_dict()
    for ta in tours or []:
        rid = f"TOUR:{ta.vehicle_id}:{ta.start_date}"
        ev = ta.evaluation
        stops = [j for j in ta.jobs if str(getattr(j, "leg_kind", "")) != "DEPOT_LOAD"]
        loads = len(ta.jobs) - len(stops)
        tour_lines.append(
            f"- **{rid}** — depot {getattr(ta, 'depot', '') or '?'}, {int(ta.days)} day(s), "
            f"{float(ev.total_km):.0f} km, {len(stops)} stop(s)"
            + (f", {loads} depot-load call(s)" if loads else "")
            + f" — born: {born.get(rid, 'SEED')}")
    if len(tour_lines) == 2:
        tour_lines.append("- (no tours this window)")
    (out_dir / "04_tour_report.md").write_text("\n".join(tour_lines) + "\n", encoding="utf-8")

    uni_lines = ["# Universe report", "",
                 "What the coverage denominator includes and excludes.", ""]
    if candidate_df is not None and not candidate_df.empty:
        hb = candidate_df.assign(_hb=candidate_df.get("hard_blocker").fillna("").astype(str))
        blocked = hb[hb["_hb"].ne("")]
        routed_parents: set[str] = set()
        if selected_df is not None and not selected_df.empty and "order_id" in selected_df.columns:
            routed_parents = {str(o).split("#", 1)[0]
                              for o in selected_df["order_id"].dropna()}
        uni_lines += ["## Blocked legs by reason", ""]
        if blocked.empty:
            uni_lines.append("- (none)")
        else:
            for reason, grp in blocked.groupby("_hb"):
                uni_lines.append(f"- {reason}: {len(grp)} leg(s) / "
                                 f"{grp['order_id'].nunique()} order(s)")
                if reason == "BEFORE_PLANNING_START":
                    # the RECEIPT of the pre-window rule, not a miss: these
                    # collections are assumed done and reframed as deliveries
                    pre_ids = {str(o).split("#", 1)[0] for o in grp["order_id"].dropna()}
                    uni_lines.append(
                        "  - by design: collections assumed collected before the window "
                        "(freight staged at the source depot); "
                        f"deliveries routed for {len(pre_ids & routed_parents)}/{len(pre_ids)} order(s)")
        if "freight_id" in candidate_df.columns:
            fid = candidate_df["freight_id"].astype(str)
            split_orders = candidate_df.loc[fid.str.contains("#S", regex=False), "order_id"].nunique()
            uni_lines += ["", f"## Split orders (over the single-vehicle ceiling): {int(split_orders)}"]
        if "dependency_type" in candidate_df.columns:
            pre = candidate_df[candidate_df["dependency_type"].astype(str).eq("PRESTAGED_DELIVERY")]
            uni_lines += ["", "## Prestaged deliveries (freight starts at a depot: "
                              "network imports + pre-window collections): "
                              f"{pre['order_id'].nunique()} order(s)"]
    (out_dir / "05_universe_report.md").write_text("\n".join(uni_lines) + "\n", encoding="utf-8")

    # full-accounting check: every windowed order with a leg appears in the manifest
    def _parent_order_id(value) -> str:
        return str(value).split("#S", 1)[0]

    leg_orders = (
        {_parent_order_id(v) for v in legs_all_df["order_id"]}
        if (legs_all_df is not None and not legs_all_df.empty) else set()
    )
    manifest_orders = (
        {_parent_order_id(v) for v in manifest["order_id"] if str(v)}
        if not manifest.empty else set()
    )
    fully_accounted = leg_orders <= manifest_orders
    return report, fully_accounted



def plan_stability_md(churn_rows: list[dict], beta: float) -> str:
    """'## Plan stability (disturbance)' section for 02_kpi_summary.md — the anchor a
    beta sweep is read against (2026-07-17). Two views, one table: churn %% is the
    coarse operational number (share of comparable uncommitted jobs whose VEHICLE-DAY
    changed between anchors); the disturbance columns are the OBJECTIVE's own quantity
    at each warm re-opt vs its warm-start reference (imminence-weighted; reassignment
    x1, in-place resequence x0.5, new arrivals free) — at beta = 0 that score is the
    free-reshuffle baseline, and raising beta should push it down while km rises."""
    def _cell(v):
        return "-" if v in ("", None) else v

    lines = [
        "## Plan stability (disturbance)",
        "",
        f"Warm re-opt objective = cost + beta * disturbance; this run: beta = {float(beta):g}"
        + (" (pure cost - the free-reshuffle baseline)." if float(beta) == 0.0 else "."),
        "Churn % = comparable uncommitted jobs whose vehicle-day changed since the previous",
        "anchor (jobs on launched vehicle-days are committed and excluded). Disturbance = the",
        "objective's own score per warm re-opt vs its warm-start reference: imminence-weighted,",
        "reassignment x1, resequence x0.5, new arrivals free.",
        "",
        "| epoch | kind | uncommitted | comparable | moved | churn % | reseq | disturbance | weighted base | disturbance % |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in churn_rows or []:
        lines.append(
            f"| {str(r.get('epoch', ''))[:16]} | {r.get('kind', '')} | {r.get('uncommitted_jobs', '')} "
            f"| {r.get('comparable', '')} | {r.get('moved', '')} | {r.get('churn_pct', '')} "
            f"| {_cell(r.get('resequenced'))} | {_cell(r.get('disturbance_score'))} "
            f"| {_cell(r.get('weighted_comparable'))} | {_cell(r.get('disturbance_pct'))} |")
    warm = [r for r in (churn_rows or []) if str(r.get("kind", "")) == "warm"]
    if warm:
        n = len(warm)
        mean_churn = sum(float(r.get("churn_pct") or 0.0) for r in warm) / n
        total = sum(float(r.get("disturbance_score") or 0.0) for r in warm)
        base = sum(float(r.get("weighted_comparable") or 0.0) for r in warm)
        pct = (100.0 * total / base) if base else 0.0
        lines += ["", f"- {n} warm re-opt(s): mean churn {mean_churn:.1f}%; total disturbance "
                      f"{total:.2f} over weighted base {base:.2f} ({pct:.1f}%)."]
    else:
        lines += ["", "- no warm re-opts in this window (nothing for beta to act on)."]
    return "\n".join(lines) + "\n"
