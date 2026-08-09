"""Run the W0 daily day-ahead oracle with one midnight planning epoch."""
from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

from freight_planner import run_rolling
from freight_planner.day_ahead_oracle import (
    DEFAULT_ORACLE,
    DEFAULT_SUMMARY,
    W0_END,
    W0_START,
    sha256_file,
)
from freight_planner.paths import DEFAULT_ENRICHED


PACKAGE_ROOT = Path(__file__).resolve().parent
RESULTS_ROOT = PACKAGE_ROOT / "result_runs"
W0_CACHE_ROOT = RESULTS_ROOT / "_overnight_isolation" / "W0"
W0_POSTCODE_CACHE = W0_CACHE_ROOT / "postcode_cache.json"
W0_OSRM_CACHE = W0_CACHE_ROOT / "osrm_cache.json"
DEFAULT_OUT_DIR = RESULTS_ROOT / "W0_static_oracle"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--iterations", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--delta-r1-min", type=int, default=90)
    parser.add_argument("--converge-pct", type=float, default=0.15)
    parser.add_argument("--converge-window", type=int, default=500)
    parser.add_argument("--converge-min-iters", type=int, default=1500)
    return parser.parse_args(argv)


def build_run_argv(args: argparse.Namespace) -> list[str]:
    """Build the fixed daily-oracle invocation accepted by run_rolling."""
    return [
        "--start", W0_START.isoformat(),
        "--end", W0_END.isoformat(),
        "--out-dir", str(args.out_dir),
        "--iterations", str(args.iterations),
        "--seed", str(args.seed),
        "--delta-r1-min", str(args.delta_r1_min),
        "--epochs", "00:00",
        "--micro-every-min", "0",
        "--converge-pct", str(args.converge_pct),
        "--converge-window", str(args.converge_window),
        "--converge-min-iters", str(args.converge_min_iters),
        "--postcode-cache", str(W0_POSTCODE_CACHE),
        "--osrm-cache", str(W0_OSRM_CACHE),
    ]


def validate_oracle_artifacts(
    source_path: Path = DEFAULT_ENRICHED,
    oracle_path: Path = DEFAULT_ORACLE,
    summary_path: Path = DEFAULT_SUMMARY,
) -> dict:
    """Reject missing, stale, wrongly windowed, or visibility-invalid inputs."""
    for path in (source_path, oracle_path, summary_path):
        if not Path(path).exists():
            raise FileNotFoundError(path)
    summary = json.loads(Path(summary_path).read_text(encoding="utf-8"))
    if summary.get("source_sha256") != sha256_file(Path(source_path)):
        raise ValueError("oracle source fingerprint does not match the live combined input")
    if summary.get("start") != W0_START.isoformat() or summary.get("end") != W0_END.isoformat():
        raise ValueError("oracle summary has the wrong W0 date window")
    census = summary.get("daily_visibility_census")
    expected_dates = [
        (W0_START + timedelta(days=offset)).isoformat()
        for offset in range((W0_END - W0_START).days + 1)
    ]
    if not isinstance(census, list) or [row.get("date") for row in census] != expected_dates:
        raise ValueError("oracle summary is missing the complete daily visibility census")
    for row in census:
        if row.get("missing_collection_ids") or row.get("future_collection_ids_visible_early"):
            raise ValueError(f"oracle visibility census contains violations: {row}")
    return summary


def run_oracle(
    args: argparse.Namespace,
    *,
    source_path: Path = DEFAULT_ENRICHED,
    oracle_path: Path = DEFAULT_ORACLE,
    summary_path: Path = DEFAULT_SUMMARY,
) -> int:
    validate_oracle_artifacts(source_path, oracle_path, summary_path)
    for cache in (W0_POSTCODE_CACHE, W0_OSRM_CACHE):
        if not cache.exists():
            raise FileNotFoundError(cache)
    previous = run_rolling.DEFAULT_ENRICHED
    try:
        run_rolling.DEFAULT_ENRICHED = Path(oracle_path)
        return run_rolling.main(build_run_argv(args))
    finally:
        run_rolling.DEFAULT_ENRICHED = previous


def main(argv: list[str] | None = None) -> int:
    return run_oracle(_parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
