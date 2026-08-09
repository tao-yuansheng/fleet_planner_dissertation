"""Milestone 3 CLI: build planner inputs for a window and run the seed planner.

Writes the first real selected plan (`selected_plan_seed.csv`) plus a rejection
table and a short coverage summary that can be compared to old-manifest coverage.

Run from BackEnd/logistics:

    $env:PYTHONDONTWRITEBYTECODE='1'
    E:\\BEAT\\ZECURE-Phase2-main\\.venv-1\\Scripts\\python.exe -B \\
        -m freight_planner.run_seed --start 2026-01-05 --end 2026-01-10
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
from pathlib import Path

import pandas as pd

from freight_planner import geocode
from freight_planner.output_layout import run_dirs, window_label
from freight_planner.build_phase0 import _load_cache, _load_qargo, _parse_date
from freight_planner.compatibility import vehicle_job_compatibility_frame
from freight_planner.date_basis import VALID_BASIS, align_demand_to_legs, filter_demand_by_basis, filter_legs_by_basis
from freight_planner.demand import (
    FORWARD_STRUCTURAL,
    RESPONSIBILITY_MODES,
    build_demand_records,
)
from freight_planner.jobs import candidate_jobs_frame
from freight_planner.legs import build_movement_leg_records
from freight_planner.paths import DEFAULT_OUT_DIR, DEFAULT_POSTCODE_CACHE, DEFAULT_QARGO
from freight_planner.seed_planner import run_seed_plan, seed_plan_frame, seed_rejection_frame
from freight_planner.state import build_initial_freight_states
from freight_planner.vehicles import vehicle_states_frame


def _summary(start: date, end: date, candidates: pd.DataFrame, result) -> str:
    runnable = candidates[candidates.get("hard_blocker", pd.Series(dtype=str)).fillna("").eq("")] if not candidates.empty else candidates
    selected = len(result.selected)
    rejected = len(result.rejected)
    denom = selected + rejected
    rate = (selected / denom * 100.0) if denom else 0.0
    reasons = Counter(r.reason for r in result.rejected)
    lines = [
        "# Seed Plan Summary",
        "",
        f"- window: {start} to {end}",
        f"- runnable candidate jobs: {len(runnable)}",
        f"- selected jobs: {selected}",
        f"- rejected jobs: {rejected}",
        f"- seed assignment rate (selected / runnable): {rate:.1f}%",
        "",
        "## Rejections By Reason",
        "",
        "```text",
    ]
    lines += [f"{reason:<28} {count}" for reason, count in reasons.most_common()] or ["(none)"]
    lines += ["```", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the freight-planner seed planner for a window.")
    parser.add_argument("--start", required=True, help="Start date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date, YYYY-MM-DD")
    parser.add_argument("--qargo", default=str(DEFAULT_QARGO))
    parser.add_argument("--postcode-cache", default=str(DEFAULT_POSTCODE_CACHE))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--date-basis", choices=sorted(VALID_BASIS), default="planning_window")
    parser.add_argument("--responsibility-mode", choices=sorted(RESPONSIBILITY_MODES), default=FORWARD_STRUCTURAL)
    args = parser.parse_args(argv)

    start = _parse_date(args.start)
    end = _parse_date(args.end)
    out_dir = Path(args.out_dir) / args.responsibility_mode / args.date_basis
    _inputs_dir, _plan_dir, reports_dir = run_dirs(out_dir, window_label(start, end))  # superseded baseline -> reports/

    cache_path = Path(args.postcode_cache)
    qargo_df = _load_qargo(Path(args.qargo))
    postcode_cache = _load_cache(cache_path)

    demand_records = build_demand_records(qargo_df, start, end, responsibility_mode=args.responsibility_mode)
    leg_records = build_movement_leg_records(qargo_df, demand_records, postcode_cache)
    legs_df = filter_legs_by_basis(pd.DataFrame([r.to_dict() for r in leg_records]), start, end, args.date_basis)

    vehicle_df = vehicle_states_frame(start)
    candidate_df = candidate_jobs_frame(legs_df, vehicle_df, start)
    compatibility_df = vehicle_job_compatibility_frame(candidate_df, vehicle_df, postcode_cache)
    demand_df_all = pd.DataFrame([r.to_dict() for r in demand_records])
    if args.date_basis == "service_date":
        demand_df = align_demand_to_legs(demand_df_all, legs_df)
    else:
        demand_df = filter_demand_by_basis(demand_df_all, start, end, args.date_basis)
        demand_df = align_demand_to_legs(demand_df, legs_df) if not legs_df.empty else demand_df
    state_records = build_initial_freight_states(demand_df, legs_df, planning_start=start)
    freight_states_df = pd.DataFrame([r.to_dict() for r in state_records])

    result = run_seed_plan(candidate_df, vehicle_df, compatibility_df, freight_states_df)

    plan_path = reports_dir / "selected_plan_seed.csv"
    rejected_path = reports_dir / "seed_rejected_jobs.csv"
    summary_path = reports_dir / "seed_plan_summary.md"
    seed_plan_frame(result).to_csv(plan_path, index=False)
    seed_rejection_frame(result).to_csv(rejected_path, index=False)
    summary_path.write_text(_summary(start, end, candidate_df, result), encoding="utf-8")
    geocode.save_cache(postcode_cache, cache_path)  # persist any newly geocoded postcodes

    print(f"Seed plan built for {start} to {end} ({args.responsibility_mode}, {args.date_basis})")
    print(f"  selected plan:  {plan_path}")
    print(f"  rejected jobs:  {rejected_path}")
    print(f"  summary:        {summary_path}")
    print(f"  selected:       {len(result.selected)}")
    print(f"  rejected:       {len(result.rejected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
