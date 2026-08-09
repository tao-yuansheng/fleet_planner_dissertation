"""Pre-routing option resolver — TRUNK-vs-HUBDROP only.

The DIRECT-vs-XDOCK resolver (static ratio ρ) was removed 2026-07-23: that choice
is now ENDOGENOUS — both option groups flow into the optimizer and the seed + ALNS
pick the mode on real routed cost (see freight_planner/option_mutex.py and the
OptionSwap operator in alns.py; the choice is reported by option_report.py).

What remains here is the PL_EXPORT TRUNK-vs-HUBDROP resolver, a genuine
system-distance pre-decision (the scheduled depot->hub trunk is not in routed km,
so it cannot be priced by the router) that is orthogonal to DIRECT/XDOCK.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from freight_planner.shared.config import DEPOT_ANCHORS
from freight_planner import geocode
from freight_planner.route_costs import road_km


def _coords(pc: str, cache: dict):
    return geocode.coords(str(pc or ""), cache)


DEFAULT_HUBDROP_RATIO = 1.0


@dataclass
class HubDropChoice:
    order_id: str
    chosen: str        # TRUNK | HUBDROP
    rejected: str
    trunk_km: float    # system km: customer->depot + depot->hub
    hubdrop_km: float  # system km: customer->hub + hub->depot
    reason: str        # cost | default_no_depot | default_no_geocode


def resolve_hub_drop(
    candidates: pd.DataFrame,
    postcode_cache: dict,
    ratio: float = DEFAULT_HUBDROP_RATIO,
) -> tuple[pd.DataFrame, list[HubDropChoice]]:
    """Pick TRUNK vs HUBDROP per PL_EXPORT order *before* routing (Milestone 7b).

    The scheduled depot->hub trunk is not in routed km, so the benefit of a
    hub-drop is a *system*-distance argument: it avoids hauling the freight back
    to depot only for the trunk to carry it to the hub. With both worlds ending at
    the depot the comparison reduces to whether the customer is closer to the hub
    than to its depot. Conservative by design: we keep the scheduled TRUNK default
    unless the hub-drop is strictly cheaper in system km (``ratio`` = 1.0), and we
    keep TRUNK whenever the depot anchor or a geocode is missing.
    """
    if candidates is None or candidates.empty or "option_set" not in candidates.columns:
        return candidates, []

    optional = candidates[candidates["option_set"].astype(str) != ""]
    if optional.empty:
        return candidates, []

    choices: list[HubDropChoice] = []
    drop_leg_ids: set[str] = set()

    for order_id, grp in optional.groupby("option_set"):
        trunk_rows = grp[grp["option_group"] == "TRUNK"]
        hubdrop_rows = grp[grp["option_group"] == "HUBDROP"]
        if trunk_rows.empty or hubdrop_rows.empty:
            continue  # not a hub-drop option set (e.g. DIRECT/XDOCK) -> leave alone

        pickup = trunk_rows[trunk_rows["leg_kind"] == "CUSTOMER_PICKUP"]
        origin_pc = str(pickup.iloc[0]["service_pc"]) if not pickup.empty else ""
        hub_pc = str(hubdrop_rows.iloc[0]["service_pc"])
        depot = str(pickup.iloc[0]["source_depot"]) if not pickup.empty else ""

        customer = _coords(origin_pc, postcode_cache)
        hub = _coords(hub_pc, postcode_cache)
        depot_anchor = DEPOT_ANCHORS.get(depot)

        if depot_anchor is None:
            chosen, reason, trunk_km, hubdrop_km = "TRUNK", "default_no_depot", 0.0, 0.0
        elif customer is None or hub is None:
            chosen, reason, trunk_km, hubdrop_km = "TRUNK", "default_no_geocode", 0.0, 0.0
        else:
            trunk_km = (road_km(customer[0], customer[1], depot_anchor[0], depot_anchor[1])
                        + road_km(depot_anchor[0], depot_anchor[1], hub[0], hub[1]))
            hubdrop_km = (road_km(customer[0], customer[1], hub[0], hub[1])
                          + road_km(hub[0], hub[1], depot_anchor[0], depot_anchor[1]))
            chosen = "HUBDROP" if hubdrop_km < ratio * trunk_km else "TRUNK"
            reason = "cost"

        rejected = "TRUNK" if chosen == "HUBDROP" else "HUBDROP"
        drop_leg_ids.update(str(x) for x in grp[grp["option_group"] == rejected]["leg_id"])
        choices.append(HubDropChoice(str(order_id), chosen, rejected,
                                     float(trunk_km), float(hubdrop_km), reason))

    chosen_df = candidates[~candidates["leg_id"].astype(str).isin(drop_leg_ids)].copy()
    return chosen_df, choices


def hub_drop_choices_md(choices: list[HubDropChoice]) -> str:
    hubdrop = sum(1 for c in choices if c.chosen == "HUBDROP")
    trunk = sum(1 for c in choices if c.chosen == "TRUNK")
    by_default = sum(1 for c in choices if c.reason != "cost")
    system_saved = sum(c.trunk_km - c.hubdrop_km for c in choices if c.chosen == "HUBDROP")
    lines = [
        "# Opportunistic Hub-Drop Choices (PL_EXPORT)",
        "",
        f"- export option sets resolved: {len(choices)}",
        f"- chose HUBDROP: {hubdrop}",
        f"- chose TRUNK (default): {trunk}",
        f"- resolved by default (no geocode/depot): {by_default}",
        f"- system km saved by hub-drops: {system_saved:,.0f}",
        "",
    ]
    return "\n".join(lines)
