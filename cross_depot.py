"""Milestone 6: cross-depot resource allocation — accounting.

The greedy seed and ALNS already *do* cross-depot allocation: a job whose home
depot has no feasible vehicle is offered to other depots' vehicles, and because
each vehicle's route starts at its own depot, geography already prices cross-depot
work higher (a CB22 truck serving Bedford freight drives a longer route than a
Bedford truck would). What was missing is making that explicit and reported.

This module classifies each plan assignment as same- or cross-depot and measures
the implied repositioning distance (home depot anchor -> the served territory's
depot anchor), so the validation/KPI output can show cross-depot assignments and
repositioning km (Milestone 6 acceptance).

Depot ownership stays a cost/preference, not a wall — consistent with the
groupage reality of the network.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

from freight_planner.shared.config import DEPOT_ANCHORS
from freight_planner.route_costs import road_km


def assignment_kind(vehicle_home_depot: str, job_source_depot: str) -> str:
    home = str(vehicle_home_depot or "")
    territory = str(job_source_depot or "")
    if not territory or territory == home:
        return "SAME"
    return "CROSS"


def reposition_km(home_depot: str, territory_depot: str) -> float:
    """Road distance to send a vehicle from its home depot to a territory depot.

    Zero when same depot, or when either anchor is unknown (e.g. OVERFLOW work
    that belongs to no single depot)."""
    home = str(home_depot or "")
    territory = str(territory_depot or "")
    if home == territory:
        return 0.0
    a = DEPOT_ANCHORS.get(home)
    b = DEPOT_ANCHORS.get(territory)
    if a is None or b is None:
        return 0.0
    return road_km(a[0], a[1], b[0], b[1])


@dataclass
class CrossDepotReport:
    total_assignments: int
    same_depot_assignments: int
    cross_depot_assignments: int
    cross_depot_km: float
    repositioning_km: float
    cross_routes: int
    by_flow: dict = field(default_factory=dict)  # (home_depot, territory) -> count


def cross_depot_report(selected: pd.DataFrame, candidates: pd.DataFrame,
                       route_totals: dict | None = None) -> CrossDepotReport:
    if selected is None or selected.empty:
        return CrossDepotReport(0, 0, 0, 0.0, 0.0, 0, {})

    source_by_leg: dict[str, str] = {}
    if candidates is not None and not candidates.empty and "source_depot" in candidates.columns:
        source_by_leg = dict(zip(candidates["leg_id"].astype(str), candidates["source_depot"].astype(str)))

    rows = selected.copy()
    rows["territory"] = rows["leg_id"].astype(str).map(source_by_leg).fillna("")
    rows["kind"] = [assignment_kind(h, t) for h, t in
                    zip(rows["vehicle_home_depot"].astype(str), rows["territory"])]

    cross = rows[rows["kind"] == "CROSS"]
    if route_totals and not cross.empty:
        # full driven km on routes that carry cross-depot work (includes the return leg)
        cross_km = float(sum(float(route_totals.get(str(rid), 0.0))
                             for rid in cross["route_id"].astype(str).unique()))
    else:
        cross_km = float(cross["planned_km"].astype(float).sum()) if not cross.empty else 0.0

    repositioning = 0.0
    cross_routes = 0
    if not cross.empty:
        for _route_id, grp in cross.groupby("route_id"):
            home = str(grp["vehicle_home_depot"].iloc[0])
            territories = grp["territory"][grp["territory"].astype(bool)]
            territory = territories.mode().iloc[0] if not territories.mode().empty else ""
            repositioning += reposition_km(home, territory)
            cross_routes += 1

    by_flow = dict(Counter(
        (str(h), str(t)) for h, t in zip(cross["vehicle_home_depot"], cross["territory"])
    )) if not cross.empty else {}

    return CrossDepotReport(
        total_assignments=int(len(rows)),
        same_depot_assignments=int((rows["kind"] == "SAME").sum()),
        cross_depot_assignments=int(len(cross)),
        cross_depot_km=cross_km,
        repositioning_km=repositioning,
        cross_routes=cross_routes,
        by_flow=by_flow,
    )


def cross_depot_report_md(report: CrossDepotReport) -> str:
    lines = [
        "# Cross-Depot Allocation Report",
        "",
        f"- total assignments: {report.total_assignments}",
        f"- same-depot assignments: {report.same_depot_assignments}",
        f"- cross-depot assignments: {report.cross_depot_assignments}",
        f"- cross-depot routes: {report.cross_routes}",
        f"- cross-depot planned km: {report.cross_depot_km:,.0f}",
        f"- repositioning km (home depot -> territory): {report.repositioning_km:,.0f}",
        "",
        "## Cross-Depot Flows (home depot -> territory)",
        "",
        "```text",
    ]
    if report.by_flow:
        for (home, territory), count in sorted(report.by_flow.items(), key=lambda kv: -kv[1]):
            lines.append(f"{home:>10} -> {territory:<10} {count}")
    else:
        lines.append("(none)")
    lines += ["```", ""]
    return "\n".join(lines)
