"""Q8 CLI: build the persisted enriched orders dataset (Jan+Feb).

Joins `freight_planner/data/verified_legs.csv` onto the raw orders so the verified
physical leg travels with each order as an explicit column. The planner can then
load this enriched file in backtest mode instead of inferring the leg per run.

Prerequisite: `verified_legs.csv` must cover the order window. Regenerate it for
new months with:

    python -B -m freight_planner.tools.verify_legs \\
        --qargo data/Input/orders/qargo_20260101_to_20260131.parquet \\
        --qargo data/Input/orders/qargo_20260201_to_20260228.parquet

Run from BackEnd/logistics:

    $env:PYTHONDONTWRITEBYTECODE='1'
    E:\\BEAT\\ZECURE-Phase2-main\\.venv-1\\Scripts\\python.exe -B -m freight_planner.build_enriched
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from freight_planner.enrich import build_enriched_orders
from freight_planner.paths import (
    DEFAULT_ENRICHED,
    DEFAULT_FEB_QARGO,
    DEFAULT_QARGO,
    DEFAULT_VERIFIED_LEGS,
)


def _load_orders(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    return pd.read_csv(path)


def _coverage(enriched: pd.DataFrame) -> str:
    total = len(enriched)
    has_leg = enriched["verified_leg"].astype(str).str.len().gt(0)
    matched = int(has_leg.sum())
    corrected = int(enriched["goods_weight_correction_reason"].astype(str).str.len().gt(0).sum())
    lines = [
        f"orders:            {total}",
        f"with verified leg: {matched} ({matched / total * 100:.1f}%)" if total else "with verified leg: 0",
        f"without leg:       {total - matched}",
        f"weight corrections:{corrected:>8}",
        "",
        "leg distribution:",
        enriched.loc[has_leg, "verified_leg"].value_counts().to_string() or "(none)",
        "",
        "confidence distribution:",
        enriched.loc[has_leg, "verified_confidence"].value_counts().to_string() or "(none)",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the enriched orders dataset (orders + verified leg).")
    parser.add_argument("--qargo", action="append", default=None,
                        help="Order file(s); repeat for multiple. Default: Jan + Feb 2026.")
    parser.add_argument("--verified-legs", default=str(DEFAULT_VERIFIED_LEGS))
    parser.add_argument("--out", default=str(DEFAULT_ENRICHED))
    args = parser.parse_args(argv)

    qargo_paths = [Path(p) for p in (args.qargo or [DEFAULT_QARGO, DEFAULT_FEB_QARGO])]
    orders = pd.concat([_load_orders(p) for p in qargo_paths], ignore_index=True)
    verified = pd.read_csv(args.verified_legs)

    enriched = build_enriched_orders(orders, verified)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    enriched.to_parquet(out_path, index=False)

    print(f"Enriched orders written: {out_path}")
    print(f"  sources: {[p.name for p in qargo_paths]}")
    print(_coverage(enriched))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
