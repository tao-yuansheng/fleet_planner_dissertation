from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

import pandas as pd


@dataclass(frozen=True)
class LedgerViolation:
    violation_type: str
    leg_id: str
    order_id: str
    order_name: str
    required_leg_id: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def validate_selected_jobs(candidates: pd.DataFrame, selected_leg_ids: Iterable[str]) -> list[LedgerViolation]:
    if candidates.empty:
        return []
    selected = {str(v) for v in selected_leg_ids}
    violations: list[LedgerViolation] = []
    for row in candidates.itertuples(index=False):
        leg_id = str(getattr(row, "leg_id", "") or "")
        if leg_id not in selected:
            continue
        dependency_type = str(getattr(row, "dependency_type", "") or "")
        predecessor = str(getattr(row, "predecessor_leg_id", "") or "")
        if dependency_type == "REQUIRES_PRIOR_PICKUP" and predecessor not in selected:
            violations.append(LedgerViolation(
                violation_type="MISSING_PREDECESSOR_PICKUP",
                leg_id=leg_id,
                order_id=str(getattr(row, "order_id", "") or ""),
                order_name=str(getattr(row, "order_name", "") or ""),
                required_leg_id=predecessor,
                message="Delivery candidate selected before its pickup candidate is selected.",
            ))
    return violations


def drop_orphan_deliveries(records, candidates: pd.DataFrame):
    """Drop selected deliveries whose required pickup is not also selected — the
    delivery-before-pickup violations. Returns ``(kept_records, dropped_leg_ids)``.

    Enforces what ``validate_selected_jobs`` today only DETECTS: an orphan delivery
    is freight delivered that was never collected (a dynamic-loop artifact when a
    pickup fails across epochs while its delivery survives). A no-op when there are
    no violations, so the full-knowledge static plan is bit-identical."""
    selected = {str(getattr(r, "leg_id", "") or "") for r in records}
    drop = {v.leg_id for v in validate_selected_jobs(candidates, selected)}
    if not drop:
        return list(records), set()
    return [r for r in records if str(getattr(r, "leg_id", "") or "") not in drop], drop


_TOUR_FREIGHT_KINDS = {"CUSTOMER_DELIVERY", "CUSTOMER_PICKUP", "DIRECT_CUSTOMER_MOVE", "HUB_DROP"}


def drop_freightless_tours(records):
    """Prune a TOUR whose freight legs were all dropped, leaving only scaffolding.

    A tour's ``TOUR_OVERNIGHT`` / ``__RETURN__`` / depot rows carry no ``leg_id``, so the
    leg-keyed drops (superseded options, orphan deliveries) skip them: when a tour's only
    freight leg is superseded, the scaffolding survives and the empty tour still books
    vehicle-days, subsistence and km for zero freight (audit #1, 2026-07-26 — W88GNW 912 km,
    X888WSM 1,381 km). Group by ``route_id``; a ``TOUR:`` route with no freight-carrying leg
    (delivery / pickup / direct / hub-drop) is dropped whole. Daily routes and non-empty
    tours are untouched. Returns ``(kept_records, dropped_route_ids)``."""
    by_route: dict[str, list] = {}
    for r in records:
        by_route.setdefault(str(getattr(r, "route_id", "") or ""), []).append(r)
    dropped = {
        rid for rid, rs in by_route.items()
        if rid.startswith("TOUR:") and not any(
            str(getattr(r, "leg_kind", "") or "").upper() in _TOUR_FREIGHT_KINDS for r in rs)
    }
    if not dropped:
        return list(records), set()
    return [r for r in records if str(getattr(r, "route_id", "") or "") not in dropped], dropped


def selected_option_conflicts(records, candidates: pd.DataFrame) -> set[str]:
    """Return option sets whose surviving records contain DIRECT and XDOCK legs."""
    if candidates is None or candidates.empty or "option_set" not in candidates.columns:
        return set()
    selected = {str(getattr(r, "leg_id", "") or "") for r in records}
    chosen = candidates[
        candidates["leg_id"].astype(str).isin(selected)
        & candidates["option_set"].fillna("").astype(str).ne("")
    ]
    conflicts: set[str] = set()
    for option_set, group in chosen.groupby("option_set"):
        groups = set(group["option_group"].fillna("").astype(str))
        if {"DIRECT", "XDOCK"} <= groups:
            conflicts.add(str(option_set))
    return conflicts


def drop_superseded_option_legs(records, candidates: pd.DataFrame,
                                 committed_leg_ids: set[str] | None = None):
    """Enforce the DIRECT/XDOCK option invariant on the FINAL plan: a freight may
    carry legs from at most ONE option group. The rolling dispatcher commits option
    legs across separate passes/epochs/ledgers, so a freight can end up with both a
    DIRECT leg and an XDOCK pickup selected (its XDOCK delivery having stranded).

    Returns ``(kept_records, dropped_leg_ids, conflicted_option_sets)``. Keeps the
    group that DELIVERS the freight (a DIRECT move, or an XDOCK delivery) and drops
    the other group's selected legs — dropping the phantom collection rather than
    leaving freight both collected-to-depot and moved direct. A no-op when no option
    set has both groups selected, so single-mode and static plans are unchanged.

    ``committed_leg_ids`` (optional): legs ever watermark-committed (locked to a
    driver) during dispatch. A group that would be dropped but holds a committed leg
    is NEVER dropped — silently reassigning an already-promised job to a different
    vehicle breaks the freeze guarantee the whole commit-floor mechanism exists to
    provide (R888GNW/2026-02-02, 2026-07-28: a committed DIRECT collect leg vanished
    from its route, freight resurfacing on an unrelated vehicle via XDOCK). Such
    option_sets are returned as ``conflicted_option_sets`` for the caller to
    surface — the option invariant stays violated rather than being silently
    "fixed" at a driver's expense."""
    if candidates is None or candidates.empty or "option_set" not in candidates.columns:
        return list(records), set(), set()
    selected = {str(getattr(r, "leg_id", "") or "") for r in records}
    opt = candidates[candidates["option_set"].astype(str) != ""]
    if opt.empty:
        return list(records), set(), set()

    committed = committed_leg_ids or set()
    drop: set[str] = set()
    conflicts: set[str] = set()
    for _oset, grp in opt.groupby("option_set"):
        by_group: dict[str, list[tuple[str, str]]] = {}
        for row in grp.itertuples(index=False):
            lid = str(getattr(row, "leg_id", "") or "")
            if lid in selected:
                og = str(getattr(row, "option_group", "") or "")
                by_group.setdefault(og, []).append((lid, str(getattr(row, "leg_kind", "") or "")))
        if "DIRECT" in by_group and "XDOCK" in by_group:
            xdock_delivers = any(k == "CUSTOMER_DELIVERY" for _, k in by_group["XDOCK"])
            direct_delivers = any(k == "DIRECT_CUSTOMER_MOVE" for _, k in by_group["DIRECT"])
            if xdock_delivers:             # XDOCK completed the delivery -> DIRECT is the stray
                losing = by_group["DIRECT"]
            elif direct_delivers:          # DIRECT delivered it -> the XDOCK legs are redundant
                losing = by_group["XDOCK"]
            else:                          # neither delivered: keep DIRECT side, drop XDOCK
                losing = by_group["XDOCK"]
            losing_ids = {lid for lid, _ in losing}
            if losing_ids & committed:
                conflicts.add(str(_oset))
            else:
                drop.update(losing_ids)
    if not drop:
        return list(records), set(), conflicts
    return [r for r in records if str(getattr(r, "leg_id", "") or "") not in drop], drop, conflicts


LEDGER_VIOLATION_COLUMNS = [
    "violation_type",
    "leg_id",
    "order_id",
    "order_name",
    "required_leg_id",
    "message",
]


def ledger_violations_frame(candidates: pd.DataFrame, selected_leg_ids: Iterable[str]) -> pd.DataFrame:
    rows = [v.to_dict() for v in validate_selected_jobs(candidates, selected_leg_ids)]
    return pd.DataFrame(rows, columns=LEDGER_VIOLATION_COLUMNS)
