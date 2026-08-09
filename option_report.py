"""Endogenous DIRECT/XDOCK choice report (2026-07-23).

Replaces the static resolver's decision record. The mode is no longer decided up
front, so the choice is READ BACK from the final selected plan: for each option
set, whichever option_group's legs appear in the plan is the chosen mode.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class EndogenousChoice:
    order_id: str      # == option_set
    chosen: str        # DIRECT | XDOCK | UNSERVED
    rejected: str      # comma-joined other group(s)


def endogenous_option_choices(selected_df: pd.DataFrame,
                              candidate_df: pd.DataFrame) -> list[EndogenousChoice]:
    """Per option set, the group whose legs made it into the selected plan."""
    if candidate_df is None or candidate_df.empty or "option_set" not in candidate_df.columns:
        return []
    opt = candidate_df[candidate_df["option_set"].astype(str).ne("")]
    if opt.empty:
        return []
    selected_legs = (set(selected_df["leg_id"].astype(str))
                     if selected_df is not None and not selected_df.empty
                     and "leg_id" in selected_df.columns else set())
    choices: list[EndogenousChoice] = []
    for oset, grp in opt.groupby("option_set"):
        groups = sorted(set(grp["option_group"].astype(str)))
        if not (set(groups) & {"DIRECT", "XDOCK"}):
            continue  # TRUNK/HUBDROP export set — reported by resolve_hub_drop, not here
        chosen = ""
        for g in groups:
            g_legs = set(grp[grp["option_group"].astype(str).eq(g)]["leg_id"].astype(str))
            if g_legs & selected_legs:
                chosen = g
                break
        rejected = ",".join(g for g in groups if g != chosen)
        choices.append(EndogenousChoice(order_id=str(oset),
                                        chosen=chosen or "UNSERVED", rejected=rejected))
    return choices


def endogenous_option_choices_md(choices: list[EndogenousChoice]) -> str:
    direct = sum(1 for c in choices if c.chosen == "DIRECT")
    xdock = sum(1 for c in choices if c.chosen == "XDOCK")
    unserved = sum(1 for c in choices if c.chosen == "UNSERVED")
    lines = [
        "# Direct vs Crossdock Choices (endogenous)",
        "",
        "The mode is chosen by the seed and ALNS on real routed cost, then read back",
        "from the selected plan (no static ratio).",
        "",
        f"- option sets resolved: {len(choices)}",
        f"- chose DIRECT: {direct}",
        f"- chose XDOCK: {xdock}",
        f"- unserved: {unserved}",
        "",
    ]
    return "\n".join(lines)
