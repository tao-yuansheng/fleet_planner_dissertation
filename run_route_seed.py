"""Milestone 4 integration CLI: run the greedy multi-stop constructive seed.

Unlike `run_seed.py` (one-job-per-vehicle, artificial 100% coverage), this packs
real per-day routes with cumulative load, time-window, and shift constraints
binding 鈥?so the reported coverage is physically meaningful.

Run from BackEnd/logistics:

    $env:PYTHONDONTWRITEBYTECODE='1'
    E:\\BEAT\\ZECURE-Phase2-main\\.venv-1\\Scripts\\python.exe -B \\
        -m freight_planner.run_route_seed --start 2026-01-05 --end 2026-01-10
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
from pathlib import Path

import pandas as pd

from freight_planner.build_phase0 import _load_cache, _load_qargo, _parse_date
from freight_planner.compatibility import vehicle_job_compatibility_frame
from freight_planner.cross_depot import cross_depot_report, cross_depot_report_md
from freight_planner.date_basis import VALID_BASIS, align_demand_to_legs, filter_demand_by_basis, filter_legs_by_basis
from freight_planner import geocode
from freight_planner.demand import FORWARD_STRUCTURAL, RESPONSIBILITY_MODES, build_demand_records
from freight_planner.jobs import candidate_jobs_frame
from freight_planner.legs import build_movement_leg_records
from freight_planner.options_resolver import (
    hub_drop_choices_md,
    resolve_hub_drop,
)
from freight_planner.option_report import (
    endogenous_option_choices,
    endogenous_option_choices_md,
)
from freight_planner.paths import DEFAULT_OUT_DIR, DEFAULT_POSTCODE_CACHE, DEFAULT_QARGO
from freight_planner.plan_schema import plan_ledger_violations, selected_plan_export_frame, selected_plan_frame
from freight_planner.output_layout import run_dirs, window_label
from freight_planner.plan_validation import temporal_violations
from freight_planner.reports import write_reports
from freight_planner.tour_plan import run_multiday_seed_plan
from freight_planner.state import build_initial_freight_states
from freight_planner.vehicles import vehicle_states_frame


def _route_totals(res) -> dict[str, float]:
    totals: dict[str, float] = {}
    for (vid, day), ev in res.routes.items():
        route_id = f"ROUTE:{vid}:{day}"
        totals[route_id] = float(ev.total_km)
        for idx, trip_ev in enumerate(getattr(ev, "trip_evaluations", ()) or (), start=1):
            totals[f"{route_id}#T{idx}"] = float(trip_ev.total_km)
    totals.update({
        f"TOUR:{ta.vehicle_id}:{ta.start_date}": float(ta.evaluation.total_km)
        for ta in res.tours
    })
    return totals


def _summary(start: date, end: date, candidates: pd.DataFrame, res) -> str:
    runnable = candidates[candidates.get("hard_blocker", pd.Series(dtype=str)).fillna("").eq("")] if not candidates.empty else candidates
    selected, rejected = len(res.selected), len(res.rejected)
    denom = selected + rejected
    rate = (selected / denom * 100.0) if denom else 0.0
    reasons = Counter(r.reason for r in res.rejected)
    daily_km = sum(ev.total_km for ev in res.routes.values())
    tour_km = sum(ta.evaluation.total_km for ta in res.tours)
    daily_vehicle_days = len(res.routes)
    tour_vehicle_days = sum(ta.days for ta in res.tours)
    tour_jobs = sum(len(ta.jobs) for ta in res.tours)
    lines = [
        "# Route Seed Summary (greedy multi-stop + multiday tours)",
        "",
        f"- window: {start} to {end}",
        f"- runnable candidate jobs: {len(runnable)}",
        f"- selected jobs: {selected}",
        f"- rejected jobs: {rejected}",
        f"- seed assignment rate: {rate:.1f}%",
        f"- multiday tours: {len(res.tours)} ({tour_jobs} jobs)",
        f"- vehicle-days used: {daily_vehicle_days} daily + {tour_vehicle_days} tour",
        f"- total planned km: {daily_km + tour_km:,.0f} ({daily_km:,.0f} daily + {tour_km:,.0f} tour)",
        "",
        "## Rejections By Reason",
        "",
        "```text",
    ]
    lines += [f"{reason:<24} {count}" for reason, count in reasons.most_common()] or ["(none)"]
    lines += ["```", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the greedy multi-stop constructive seed planner.")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--qargo", default=str(DEFAULT_QARGO))
    parser.add_argument("--postcode-cache", default=str(DEFAULT_POSTCODE_CACHE))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--date-basis", choices=sorted(VALID_BASIS), default="planning_window")
    parser.add_argument("--responsibility-mode", choices=sorted(RESPONSIBILITY_MODES), default=FORWARD_STRUCTURAL)
    args = parser.parse_args(argv)

    start, end = _parse_date(args.start), _parse_date(args.end)
    out_dir = Path(args.out_dir) / args.responsibility_mode / args.date_basis
    _inputs_dir, plan_dir, reports_dir = run_dirs(out_dir, window_label(start, end))

    cache_path = Path(args.postcode_cache)
    qargo_df = _load_qargo(Path(args.qargo))
    postcode_cache = _load_cache(cache_path)

    demand_records = build_demand_records(qargo_df, start, end, responsibility_mode=args.responsibility_mode)
    leg_records = build_movement_leg_records(qargo_df, demand_records, postcode_cache)
    legs_all_df = pd.DataFrame([r.to_dict() for r in leg_records])  # incl trunk/accounting for the manifest
    legs_df = filter_legs_by_basis(legs_all_df, start, end, args.date_basis)

    vehicle_df = vehicle_states_frame(start)
    candidate_all = candidate_jobs_frame(legs_df, vehicle_df, start)
    # Endogenous DIRECT/XDOCK (2026-07-23): both groups flow to the seed, which
    # chooses the mode by real insertion cost; read back from the plan below.
    candidate_df = candidate_all
    candidate_df, hub_drop_choices = resolve_hub_drop(candidate_df, postcode_cache)
    compatibility_df = vehicle_job_compatibility_frame(candidate_df, vehicle_df, postcode_cache)
    demand_df_all = pd.DataFrame([r.to_dict() for r in demand_records])
    if args.date_basis == "service_date":
        demand_df = align_demand_to_legs(demand_df_all, legs_df)
    else:
        demand_df = filter_demand_by_basis(demand_df_all, start, end, args.date_basis)
        demand_df = align_demand_to_legs(demand_df, legs_df) if not legs_df.empty else demand_df
    freight_states_df = pd.DataFrame([r.to_dict() for r in build_initial_freight_states(demand_df, legs_df, planning_start=start)])

    res = run_multiday_seed_plan(candidate_df, vehicle_df, compatibility_df, freight_states_df, start)

    route_totals = _route_totals(res)
    violations = plan_ledger_violations(res.selected, candidate_df)
    selected_df = selected_plan_frame(res.selected)
    tviol = temporal_violations(selected_df)
    xreport = cross_depot_report(selected_df, candidate_df, route_totals=route_totals)

    plan_path = plan_dir / "selected_plan_route_seed.csv"
    rejected_path = reports_dir / "route_seed_rejected_jobs.csv"
    summary_path = reports_dir / "route_seed_summary.md"
    cross_path = reports_dir / "cross_depot_report.md"
    selected_plan_export_frame(res.selected, route_totals).to_csv(plan_path, index=False)
    pd.DataFrame([{"job_id": r.job_id, "reason": r.reason} for r in res.rejected],
                 columns=["job_id", "reason"]).to_csv(rejected_path, index=False)
    summary_path.write_text(_summary(start, end, candidate_df, res), encoding="utf-8")
    cross_path.write_text(cross_depot_report_md(xreport), encoding="utf-8")
    (reports_dir / "option_choices.md").write_text(
        endogenous_option_choices_md(endogenous_option_choices(selected_df, candidate_df)),
        encoding="utf-8")
    (reports_dir / "hub_drop_choices.md").write_text(hub_drop_choices_md(hub_drop_choices), encoding="utf-8")
    if not tviol.empty:
        tviol.to_csv(reports_dir / "temporal_violations.csv", index=False)

    daily_km_total = sum(ev.total_km for ev in res.routes.values())
    tour_km_total = sum(ta.evaluation.total_km for ta in res.tours)
    kpi_report, fully_accounted = write_reports(
        plan_dir, start=start, end=end, demand_df=demand_df, legs_all_df=legs_all_df,
        candidate_df=candidate_df, compatibility_df=compatibility_df, vehicle_df=vehicle_df,
        selected_df=selected_df, rejected=res.rejected, tours=res.tours,
        planned_km=daily_km_total + tour_km_total, cross_depot_km=xreport.repositioning_km,
        phantom_deliveries=len(violations),
        route_totals=route_totals,
    )
    geocode.save_cache(postcode_cache, cache_path)  # persist any newly geocoded postcodes

    direct = sum(1 for c in option_choices if c.chosen == "DIRECT")
    xdock = sum(1 for c in option_choices if c.chosen == "XDOCK")
    hubdrop = sum(1 for c in hub_drop_choices if c.chosen == "HUBDROP")
    trunk = sum(1 for c in hub_drop_choices if c.chosen == "TRUNK")
    print(f"Route seed built for {start} to {end} ({args.responsibility_mode}, {args.date_basis})")
    print(f"  same-day FF options: DIRECT {direct} / XDOCK {xdock}")
    print(f"  PL_EXPORT options:   HUBDROP {hubdrop} / TRUNK {trunk}")
    daily_km = sum(ev.total_km for ev in res.routes.values())
    tour_km = sum(ta.evaluation.total_km for ta in res.tours)
    tour_jobs = sum(len(ta.jobs) for ta in res.tours)
    print(f"  selected:        {len(res.selected)}")
    print(f"  rejected:        {len(res.rejected)}")
    print(f"  multiday tours:  {len(res.tours)} ({tour_jobs} jobs)")
    print(f"  vehicle-days:    {len(res.routes)} daily + {sum(ta.days for ta in res.tours)} tour")
    print(f"  total km:        {daily_km + tour_km:,.0f} ({daily_km:,.0f} daily + {tour_km:,.0f} tour)")
    print(f"  cross-depot assignments: {xreport.cross_depot_assignments} "
          f"(repositioning {xreport.repositioning_km:,.0f} km)")
    print(f"  ledger violations (must be 0):   {len(violations)}")
    print(f"  temporal violations (must be 0): {len(tviol)}")
    print(f"  in-universe orders: {kpi_report.in_universe_orders} | "
          f"assigned {kpi_report.assigned_orders} ({kpi_report.order_assignment_rate:.1f}%)")
    print(f"  every order accounted in manifest: {fully_accounted}")
    print(f"  plan dir:        {plan_dir}")
    print(f"  reports dir:     {reports_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

