from __future__ import annotations

from collections import Counter
from pathlib import Path

import pandas as pd

CUSTOMER_LEG_KINDS = ["CUSTOMER_PICKUP", "CUSTOMER_DELIVERY", "DIRECT_CUSTOMER_MOVE"]


def _table(counter: Counter) -> str:
    if not counter:
        return "(none)\n"
    width = max(len(str(k)) for k in counter)
    return "\n".join(f"{str(k):<{width}}  {v}" for k, v in counter.most_common()) + "\n"


def _group_table(df: pd.DataFrame, cols: list[str]) -> str:
    if df.empty:
        return "(none)"
    return df.groupby(cols, dropna=False).size().reset_index(name="count").sort_values(cols).to_string(index=False)


def write_validation_report(
    demand: pd.DataFrame,
    legs: pd.DataFrame,
    out_path: Path,
    vehicles: pd.DataFrame | None = None,
    candidates: pd.DataFrame | None = None,
    compatibility: pd.DataFrame | None = None,
    options: pd.DataFrame | None = None,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dispatchable = legs[legs["dispatchable"] == True] if not legs.empty else legs
    customer = legs[legs["leg_kind"].isin(CUSTOMER_LEG_KINDS)] if not legs.empty else legs
    vehicles = pd.DataFrame() if vehicles is None else vehicles
    candidates = pd.DataFrame() if candidates is None else candidates
    compatibility = pd.DataFrame() if compatibility is None else compatibility
    options = pd.DataFrame() if options is None else options
    runnable_candidates = candidates[candidates.get("hard_blocker", pd.Series(dtype=str)).fillna("").eq("")] if not candidates.empty else candidates
    ok_compatibility = compatibility[compatibility.get("compatibility_status", pd.Series(dtype=str)).eq("OK")] if not compatibility.empty else compatibility

    lines: list[str] = []
    lines.append("# Phase 0 Validation Report\n")
    lines.append("## Volume\n")
    lines.append(f"- demand records: {len(demand)}")
    lines.append(f"- movement legs: {len(legs)}")
    lines.append(f"- customer-facing legs: {len(customer)}")
    lines.append(f"- dispatchable customer legs: {len(dispatchable)}")
    lines.append(f"- vehicles: {len(vehicles)}")
    lines.append(f"- candidate jobs: {len(candidates)}")
    lines.append(f"- runnable candidate jobs: {len(runnable_candidates)}")
    lines.append(f"- vehicle-job compatibility pairs: {len(compatibility)}")
    lines.append(f"- OK compatibility pairs: {len(ok_compatibility)}")
    lines.append(f"- job option summaries: {len(options)}")
    lines.append("")

    lines.append("## Demand By Responsibility\n")
    lines.append("```text")
    lines.append(_table(Counter(demand.get("responsibility_shape", []))).rstrip())
    lines.append("```\n")

    lines.append("## Demand By Exclusion\n")
    exclusions = Counter(x if isinstance(x, str) and x else "IN_SCOPE" for x in demand.get("exclusion_reason", []))
    lines.append("```text")
    lines.append(_table(exclusions).rstrip())
    lines.append("```\n")

    lines.append("## Vehicle Pool By Depot And Type\n")
    lines.append("```text")
    lines.append(_group_table(vehicles, ["home_depot", "vehicle_type"]))
    lines.append("```\n")

    lines.append("## Movement Legs By Kind\n")
    lines.append("```text")
    lines.append(_table(Counter(legs.get("leg_kind", []))).rstrip())
    lines.append("```\n")

    lines.append("## Movement Legs By Planner Status\n")
    lines.append("```text")
    lines.append(_table(Counter(legs.get("planner_status", []))).rstrip())
    lines.append("```\n")

    lines.append("## Candidate Jobs By Hard Blocker\n")
    if not candidates.empty:
        blockers = Counter(x if isinstance(x, str) and x else "OK" for x in candidates.get("hard_blocker", []))
        lines.append("```text")
        lines.append(_table(blockers).rstrip())
        lines.append("```\n")
    else:
        lines.append("(none)\n")

    lines.append("## Candidate Jobs By Dependency Type\n")
    if not candidates.empty and "dependency_type" in candidates.columns:
        deps = Counter(x if isinstance(x, str) and x else "NONE" for x in candidates.get("dependency_type", []))
        lines.append("```text")
        lines.append(_table(deps).rstrip())
        lines.append("```\n")
    else:
        lines.append("(none)\n")

    lines.append("## Vehicle-Job Compatibility By Status\n")
    if not compatibility.empty and "compatibility_status" in compatibility.columns:
        statuses = Counter(x if isinstance(x, str) and x else "UNKNOWN" for x in compatibility.get("compatibility_status", []))
        lines.append("```text")
        lines.append(_table(statuses).rstrip())
        lines.append("```\n")
    else:
        lines.append("(none)\n")

    lines.append("## OK Compatibility By Depot Relation\n")
    if not ok_compatibility.empty:
        relation = Counter("same_depot" if bool(v) else "cross_depot" for v in ok_compatibility.get("same_depot", []))
        lines.append("```text")
        lines.append(_table(relation).rstrip())
        lines.append("```\n")
    else:
        lines.append("(none)\n")

    lines.append("## Job Options By Status\n")
    if not options.empty and "option_status" in options.columns:
        option_statuses = Counter(x if isinstance(x, str) and x else "UNKNOWN" for x in options.get("option_status", []))
        lines.append("```text")
        lines.append(_table(option_statuses).rstrip())
        lines.append("```\n")
    else:
        lines.append("(none)\n")

    lines.append("## Dispatchable Customer Legs By Service Date\n")
    if not dispatchable.empty:
        day_counts = Counter(dispatchable.get("service_date", []))
        lines.append("```text")
        lines.append(_table(day_counts).rstrip())
        lines.append("```\n")
    else:
        lines.append("(none)\n")

    lines.append("## Known Integrity Checks\n")
    pl_export_pickups = legs[(legs["flow"] == "PL_EXPORT") & (legs["leg_kind"] == "CUSTOMER_PICKUP")]
    ff_pickups = legs[(legs["flow"] == "FULL_FLEET") & (legs["leg_kind"] == "CUSTOMER_PICKUP")]
    ff_deliveries = legs[(legs["flow"] == "FULL_FLEET") & (legs["leg_kind"] == "CUSTOMER_DELIVERY")]
    lines.append(f"- PL_EXPORT customer pickup legs: {len(pl_export_pickups)}")
    lines.append(f"- FULL_FLEET crossdock pickup legs: {len(ff_pickups)}")
    lines.append(f"- FULL_FLEET crossdock delivery legs: {len(ff_deliveries)}")
    missing_geo = legs[legs["planner_status"] == "BAD_GEOCODE"]
    lines.append(f"- BAD_GEOCODE legs: {len(missing_geo)}")
    massive = legs[legs["planner_status"] == "MASSIVE_UNSUPPORTED"]
    lines.append(f"- MASSIVE_UNSUPPORTED legs: {len(massive)}")
    if not candidates.empty:
        no_capable = candidates[candidates["hard_blocker"].eq("NO_CAPABLE_VEHICLE")]
        missing_window = candidates[candidates["hard_blocker"].eq("MISSING_WINDOW")]
        lines.append(f"- NO_CAPABLE_VEHICLE candidate jobs: {len(no_capable)}")
        lines.append(f"- MISSING_WINDOW candidate jobs: {len(missing_window)}")
    if not compatibility.empty:
        no_ok_jobs = len(set(candidates[candidates.get("hard_blocker", pd.Series(dtype=str)).fillna("").eq("")]["leg_id"].astype(str)) - set(ok_compatibility.get("leg_id", pd.Series(dtype=str)).astype(str)))
        lines.append(f"- runnable jobs with no OK compatibility pair: {no_ok_jobs}")
    if not options.empty and "option_status" in options.columns:
        cross_only = options[options["option_status"].eq("CROSS_DEPOT_ONLY")]
        lines.append(f"- runnable jobs with only cross-depot options: {len(cross_only)}")
    lines.append("")

    if not missing_geo.empty:
        lines.append("## BAD_GEOCODE Samples\n")
        lines.append("```text")
        lines.append(missing_geo[["order_name", "leg_kind", "service_pc", "flow"]].head(20).to_string(index=False))
        lines.append("```\n")

    if not massive.empty:
        lines.append("## MASSIVE_UNSUPPORTED Samples\n")
        lines.append("```text")
        lines.append(massive[["order_name", "leg_kind", "service_pc", "flow", "pallets", "weight_kg"]].head(20).to_string(index=False))
        lines.append("```\n")

    out_path.write_text("\n".join(lines), encoding="utf-8")
