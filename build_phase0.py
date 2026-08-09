from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from freight_planner import geocode
from freight_planner.compatibility import vehicle_job_compatibility_frame
from freight_planner.output_layout import flat_window_label, run_base, window_label, write_run_manifest
from freight_planner.date_basis import (
    VALID_BASIS,
    align_demand_to_legs,
    filter_demand_by_basis,
    filter_legs_by_basis,
)
from freight_planner.demand import FORWARD_STRUCTURAL, RESPONSIBILITY_MODES, build_demand_records
from freight_planner.enrich import apply_order_load_corrections
from freight_planner.jobs import candidate_jobs_frame
from freight_planner.ledger import ledger_violations_frame
from freight_planner.options import job_options_frame
from freight_planner.legs import build_movement_leg_records
from freight_planner.paths import DEFAULT_OUT_DIR, DEFAULT_POSTCODE_CACHE, DEFAULT_QARGO
from freight_planner.reconcile import reconcile_manifest
from freight_planner.state import build_initial_freight_states
from freight_planner.validate import write_validation_report
from freight_planner.vehicles import vehicle_states_frame


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _load_cache(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _load_qargo(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        frame = pd.read_parquet(path)
    elif path.suffix.lower() in {".xlsx", ".xls"}:
        frame = pd.read_excel(path)
    else:
        frame = pd.read_csv(path)
    return apply_order_load_corrections(frame)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Phase 0 canonical freight-planner tables.")
    parser.add_argument("--start", required=True, help="Start date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date, YYYY-MM-DD")
    parser.add_argument("--qargo", default=str(DEFAULT_QARGO), help="Qargo parquet/xlsx/csv path")
    parser.add_argument("--postcode-cache", default=str(DEFAULT_POSTCODE_CACHE), help="postcode_cache.json path")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Output directory")
    parser.add_argument("--manifest", default="", help="Optional existing plan manifest CSV for reconciliation")
    parser.add_argument(
        "--date-basis",
        choices=sorted(VALID_BASIS),
        default="planning_window",
        help="Date basis for emitted tables",
    )
    parser.add_argument(
        "--responsibility-mode",
        choices=sorted(RESPONSIBILITY_MODES),
        default=FORWARD_STRUCTURAL,
        help=(
            "forward_structural avoids historical telematics/resource hindsight; "
            "backtest_verified uses verified_legs.csv to validate against history"
        ),
    )
    args = parser.parse_args(argv)

    start = _parse_date(args.start)
    end = _parse_date(args.end)
    qargo_path = Path(args.qargo)
    cache_path = Path(args.postcode_cache)
    # Flattened, month-grouped layout (matches run_alns): <out-dir>/<YYYY-MM>/<window>/.
    # mode/basis live in run_manifest.json, suffixed onto the window only when non-default.
    out_dir = Path(args.out_dir) / f"{start:%Y-%m}"
    window = flat_window_label(start, end, args.responsibility_mode, args.date_basis)
    # The spine keeps its own inputs/ folder; planner runs no longer create one
    # (run_dirs routes csv/md — the spine's flat CSV set stays together here).
    inputs_dir = run_base(out_dir, window) / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    write_run_manifest(out_dir, window, {
        "runner": "build_phase0",
        "window": window,
        "start": str(start),
        "end": str(end),
        "responsibility_mode": args.responsibility_mode,
        "date_basis": args.date_basis,
        "qargo": str(args.qargo),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })

    qargo_df = _load_qargo(qargo_path)
    postcode_cache = _load_cache(cache_path)

    demand_records = build_demand_records(qargo_df, start, end, responsibility_mode=args.responsibility_mode)
    leg_records = build_movement_leg_records(qargo_df, demand_records, postcode_cache)

    demand_df_all = pd.DataFrame([r.to_dict() for r in demand_records])
    legs_df_all = pd.DataFrame([r.to_dict() for r in leg_records])
    legs_df = filter_legs_by_basis(legs_df_all, start, end, args.date_basis)
    if args.date_basis == "service_date":
        demand_df = align_demand_to_legs(demand_df_all, legs_df)
    else:
        demand_df = filter_demand_by_basis(demand_df_all, start, end, args.date_basis)
        demand_df = align_demand_to_legs(demand_df, legs_df) if not legs_df.empty else demand_df

    vehicle_df = vehicle_states_frame(start)
    candidate_df = candidate_jobs_frame(legs_df, vehicle_df, start)
    compatibility_df = vehicle_job_compatibility_frame(candidate_df, vehicle_df, postcode_cache)
    options_df = job_options_frame(candidate_df, compatibility_df)
    runnable_leg_ids = candidate_df[candidate_df.get("hard_blocker", pd.Series(dtype=str)).fillna("").eq("")]["leg_id"] if not candidate_df.empty else []
    ledger_violation_df = ledger_violations_frame(candidate_df, runnable_leg_ids)
    state_records = build_initial_freight_states(demand_df, legs_df, planning_start=start)
    states_df = pd.DataFrame([r.to_dict() for r in state_records])

    demand_path = inputs_dir / "demand_records.csv"
    legs_path = inputs_dir / "movement_legs.csv"
    responsibility_path = inputs_dir / "verified_responsibility.csv"
    vehicle_path = inputs_dir / "vehicle_states.csv"
    candidate_path = inputs_dir / "candidate_jobs.csv"
    compatibility_path = inputs_dir / "vehicle_job_compatibility.csv"
    options_path = inputs_dir / "job_options.csv"
    states_path = inputs_dir / "freight_states_initial.csv"
    ledger_violation_path = inputs_dir / "ledger_violations_all_runnable.csv"
    report_path = inputs_dir / "validation_report.md"
    reconcile_path = inputs_dir / "manifest_reconciliation.md"

    demand_df.to_csv(demand_path, index=False)
    legs_df.to_csv(legs_path, index=False)
    vehicle_df.to_csv(vehicle_path, index=False)
    candidate_df.to_csv(candidate_path, index=False)
    compatibility_df.to_csv(compatibility_path, index=False)
    options_df.to_csv(options_path, index=False)
    states_df.to_csv(states_path, index=False)
    ledger_violation_df.to_csv(ledger_violation_path, index=False)

    responsibility_cols = [
        "order_id", "order_name", "raw_flow", "corrected_flow",
        "responsibility_shape", "responsibility_source", "exclusion_reason",
        "historical_resources",
    ]
    if not demand_df.empty:
        demand_df[responsibility_cols].to_csv(responsibility_path, index=False)
    else:
        pd.DataFrame(columns=responsibility_cols).to_csv(responsibility_path, index=False)

    write_validation_report(demand_df, legs_df, report_path, vehicle_df, candidate_df, compatibility_df, options_df)
    if args.manifest:
        manifest_df = pd.read_csv(args.manifest)
        reconcile_manifest(legs_df, manifest_df, reconcile_path)

    geocode.save_cache(postcode_cache, cache_path)  # persist any newly geocoded postcodes

    print(f"Phase 0 built for {start} to {end} ({args.responsibility_mode}, {args.date_basis})")
    print(f"  demand:         {demand_path}")
    print(f"  responsibility: {responsibility_path}")
    print(f"  movement legs:  {legs_path}")
    print(f"  vehicle states: {vehicle_path}")
    print(f"  candidate jobs: {candidate_path}")
    print(f"  compatibility:  {compatibility_path}")
    print(f"  job options:    {options_path}")
    print(f"  freight states: {states_path}")
    print(f"  ledger check:   {ledger_violation_path}")
    print(f"  report:         {report_path}")
    if args.manifest:
        print(f"  reconciliation: {reconcile_path}")
    print(f"  demand rows:    {len(demand_df)}")
    print(f"  movement legs:  {len(legs_df)}")
    print(f"  vehicles:       {len(vehicle_df)}")
    print(f"  candidate jobs: {len(candidate_df)}")
    print(f"  compat pairs:   {len(compatibility_df)}")
    print(f"  job options:    {len(options_df)}")
    print(f"  ledger issues:  {len(ledger_violation_df)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
