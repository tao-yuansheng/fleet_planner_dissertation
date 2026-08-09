"""Build the immutable input used by the W0 day-ahead oracle comparator."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from freight_planner.paths import DEFAULT_ENRICHED
from freight_planner.visibility import COLLECT_FLOWS, build_order_meta, visible_order_ids


DEFAULT_ORACLE = (
    DEFAULT_ENRICHED.parent
    / "enriched_orders_2026-01_2026-02_DAY_AHEAD_ORACLE.parquet"
)
DEFAULT_SUMMARY = DEFAULT_ORACLE.with_suffix(".summary.json")
W0_START = date(2026, 2, 16)
W0_END = date(2026, 2, 22)


def floor_creation_to_booking_midnight(
    frame: pd.DataFrame,
) -> tuple[pd.DataFrame, dict]:
    """Preserve each creation stamp, then floor it to its UTC booking date."""
    if "timestamp_created" not in frame.columns:
        raise ValueError("source is missing timestamp_created")
    if "timestamp_created_original" in frame.columns:
        raise ValueError("source already contains timestamp_created_original")

    out = frame.copy()
    out["timestamp_created_original"] = out["timestamp_created"]
    parsed = pd.to_datetime(out["timestamp_created"], errors="coerce", utc=True)
    floored = parsed.dt.floor("D")
    non_null = parsed.notna()
    changed = non_null & parsed.ne(floored)
    out["timestamp_created"] = floored
    stats = {
        "rows": int(len(out)),
        "non_null_created": int(non_null.sum()),
        "changed": int(changed.sum()),
        "already_midnight": int((non_null & ~changed).sum()),
    }
    return out, stats


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def daily_visibility_census(
    qargo_df: pd.DataFrame,
    demand_df: pd.DataFrame,
    start: date,
    end: date,
) -> list[dict]:
    """Prove daily oracle visibility without revealing later booking dates."""
    if "timestamp_created_original" not in qargo_df.columns:
        raise ValueError("oracle data is missing timestamp_created_original")

    meta = build_order_meta(qargo_df, demand_df)
    fcol = "corrected_flow" if "corrected_flow" in demand_df.columns else "flow"
    flows = (
        demand_df[["order_id", fcol]]
        .assign(order_id=lambda frame: frame["order_id"].astype(str))
        .drop_duplicates("order_id")
    )
    collect_ids = set(
        flows.loc[flows[fcol].astype(str).isin(COLLECT_FLOWS), "order_id"]
    )
    original = qargo_df[["order_id", "timestamp_created_original"]].copy()
    original["order_id"] = original["order_id"].astype(str)
    original = original.drop_duplicates("order_id")
    original["booking_date"] = pd.to_datetime(
        original["timestamp_created_original"], errors="coerce", utc=True
    ).dt.date
    booking_date = dict(zip(original["order_id"], original["booking_date"]))

    rows: list[dict] = []
    day = start
    while day <= end:
        visible = visible_order_ids(meta, datetime.combine(day, datetime.min.time()))
        today = {oid for oid in collect_ids if booking_date.get(oid) == day}
        future = {
            oid for oid in collect_ids
            if pd.notna(booking_date.get(oid)) and day < booking_date[oid] <= end
        }
        missing = sorted(today - visible)
        early = sorted(future & visible)
        rows.append({
            "date": day.isoformat(),
            "collections_booked_that_date": len(today),
            "collections_visible_at_midnight": len(today & visible),
            "missing_collection_ids": missing,
            "future_collection_ids_visible_early": early,
        })
        day += timedelta(days=1)

    failures = [
        row for row in rows
        if row["missing_collection_ids"] or row["future_collection_ids_visible_early"]
    ]
    if failures:
        raise ValueError(f"visibility census failed: {failures}")
    return rows


def _temporary_sibling(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def build_oracle_file(
    source: Path,
    output: Path,
    summary_path: Path,
    start: date,
    end: date,
    *,
    force: bool = False,
    include_visibility_census: bool = False,
) -> dict:
    """Transform and atomically publish an oracle parquet plus provenance."""
    source = Path(source)
    output = Path(output)
    summary_path = Path(summary_path)
    if source.resolve() == output.resolve():
        raise ValueError("source and oracle output paths must differ")
    if not source.exists():
        raise FileNotFoundError(source)

    source_hash = sha256_file(source)
    existing = output.exists() or summary_path.exists()
    if existing and not force:
        raise FileExistsError(
            f"oracle output already exists: {output} or {summary_path}")
    if existing and force:
        if not summary_path.exists():
            raise ValueError("cannot force replacement without existing summary")
        prior = json.loads(summary_path.read_text(encoding="utf-8"))
        if prior.get("source_sha256") != source_hash:
            raise ValueError("existing oracle summary does not match current source")

    frame = pd.read_parquet(source)
    transformed, stats = floor_creation_to_booking_midnight(frame)
    metadata = {
        "kind": "W0_day_ahead_oracle",
        "source": str(source.resolve()),
        "output": str(output.resolve()),
        "source_sha256": source_hash,
        "source_rows": int(len(frame)),
        "output_rows": int(len(transformed)),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "transformation": stats,
    }
    if include_visibility_census:
        from freight_planner.demand import build_demand_records

        demand = pd.DataFrame(
            record.to_dict()
            for record in build_demand_records(transformed, start, end)
        )
        metadata["daily_visibility_census"] = daily_visibility_census(
            transformed, demand, start, end
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    output_tmp = _temporary_sibling(output)
    summary_tmp = _temporary_sibling(summary_path)
    for tmp in (output_tmp, summary_tmp):
        if tmp.exists():
            tmp.unlink()
    try:
        transformed.to_parquet(output_tmp, index=False)
        if len(pd.read_parquet(output_tmp, columns=["order_id"])) != len(frame):
            raise RuntimeError("oracle parquet row count does not match source")
        summary_tmp.write_text(
            json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        output_tmp.replace(output)
        summary_tmp.replace(summary_path)
    finally:
        for tmp in (output_tmp, summary_tmp):
            if tmp.exists():
                tmp.unlink()
    return metadata


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_ENRICHED)
    parser.add_argument("--output", type=Path, default=DEFAULT_ORACLE)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--start", type=date.fromisoformat, default=W0_START)
    parser.add_argument("--end", type=date.fromisoformat, default=W0_END)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    metadata = build_oracle_file(
        args.source,
        args.output,
        args.summary,
        args.start,
        args.end,
        force=args.force,
        include_visibility_census=True,
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
