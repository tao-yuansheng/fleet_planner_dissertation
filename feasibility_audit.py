"""§6.1 believability floor: feasibility-violation counts over the COMMITTED plan.

Every committed route already passed ``routing_adapter.evaluate_day`` (infeasible
days are never selected), so these counts are expected to read 0 — this audit
re-derives them from the emitted committed artifacts so the chapter can *show* the
floor rather than assert it. Broken out per the outline: capacity · duty(13h) ·
drive(10h) · window.

Window note: the delivery model is soft (convex tardiness) — a late delivery is
FEASIBLE, penalised not rejected (see §6.2 timeliness). A hard-window *infeasibility*
is a committed delivery placed outside a RANGE window; the committed plan respects
those by construction (cross-ref the non-anticipation / route-backdating audits), so
``window`` reads 0. ``deliveries_past_deadline`` is reported alongside for
transparency — it is the soft-late count, an upper bound that contains the (empty)
hard-breach set.
"""
from __future__ import annotations

import pandas as pd

_TOL = 0.5  # pct tolerance so a route sitting exactly at 100% cap is not a "violation"


def _over(series, limit_pct: float = 100.0) -> int:
    s = pd.to_numeric(series, errors="coerce")
    return int((s > limit_pct + _TOL).sum())


def build_feasibility_audit(vehicle_day_util: pd.DataFrame,
                            trip_capacity_util: pd.DataFrame,
                            route_stops: pd.DataFrame) -> dict:
    vdu = vehicle_day_util if vehicle_day_util is not None else pd.DataFrame()
    tcu = trip_capacity_util if trip_capacity_util is not None else pd.DataFrame()
    rs = route_stops if route_stops is not None else pd.DataFrame()

    # The 10h drive / 13h duty caps bind PER DAY for TOURS too — a tour day cannot book 22h
    # of driving. vehicle_day_utilization already reports drive/duty PER tour-day (the return
    # residual is split onto its own day, see utilization._tour_day_rows), so the audit checks
    # EVERY veh-day (audit #3, 2026-07-26: excluding is_tour here hid 306%-of-cap tour days
    # behind a "0 violations / OK"). If a tour still books >100% on one day, that is a real
    # accounting/split defect the control MUST surface, not suppress.
    drive = _over(vdu["drive_utilization_pct"]) if ("drive_utilization_pct" in vdu.columns and not vdu.empty) else 0
    duty = _over(vdu["duty_utilization_pct"]) if ("duty_utilization_pct" in vdu.columns and not vdu.empty) else None
    cap = 0
    if not tcu.empty:
        pallet = _over(tcu["pallet_utilization_pct"]) if "pallet_utilization_pct" in tcu.columns else 0
        kg = _over(tcu["kg_utilization_pct"]) if "kg_utilization_pct" in tcu.columns else 0
        cap = pallet + kg
    past_deadline = 0
    if not rs.empty and "minutes_late" in rs.columns:
        ml = pd.to_numeric(rs["minutes_late"], errors="coerce")
        past_deadline = int((ml > 0).sum())
    return {
        "capacity": cap,
        "duty_13h": duty,
        "drive_10h": drive,
        "window": _window_violations(rs),
        "mixed_tour_daily": _mixed_tour_daily_violations(rs),
        "deliveries_past_deadline": past_deadline,   # soft-late (see §6.2), upper bound on window breaches
    }


def _mixed_tour_daily_violations(route_stops: pd.DataFrame) -> int:
    """Count vehicle-days containing both tour and ordinary daily work."""
    if (route_stops is None or route_stops.empty
            or "vehicle_id" not in route_stops.columns
            or "service_date" not in route_stops.columns):
        return 0
    if "is_tour" in route_stops.columns:
        raw = route_stops["is_tour"]
        is_tour = raw.map(
            lambda value: value if isinstance(value, bool)
            else str(value).strip().lower() in {"1", "true", "yes"})
    elif "route_id" in route_stops.columns:
        is_tour = route_stops["route_id"].astype(str).str.startswith("TOUR:")
    else:
        return 0
    tagged = route_stops[["vehicle_id", "service_date"]].copy()
    tagged["is_tour"] = is_tour.astype(bool)
    return int(tagged.groupby(["vehicle_id", "service_date"], dropna=False)["is_tour"]
               .nunique().gt(1).sum())


def _window_violations(route_stops: pd.DataFrame) -> int:
    """Committed customer deliveries served BEFORE the customer-open floor
    (``CUSTOMER_DAY_START``, 08:00) — a hard-window infeasibility the daily side enforces
    but tours (05:00 clock anchor) ignore. Was hardcoded 0 (audit #3, 2026-07-26). Rows
    without a parseable arrival are not counted (unknowable, not a breach)."""
    if (route_stops is None or route_stops.empty
            or "stop_type" not in route_stops.columns or "planned_arrive" not in route_stops.columns):
        return 0
    from freight_planner.shared.config import CUSTOMER_DAY_START
    deliv = route_stops[route_stops["stop_type"].astype(str) == "customer_delivery"]
    if deliv.empty:
        return 0
    arr = pd.to_datetime(deliv["planned_arrive"].astype(str), errors="coerce", format="mixed")
    tod = arr.dt.hour * 60 + arr.dt.minute
    floor = CUSTOMER_DAY_START.hour * 60 + CUSTOMER_DAY_START.minute
    return int((tod.notna() & (tod < floor)).sum())


def augment_with_dynamic_audits(plan_dir, non_anticipativity: int, route_backdating: int,
                                option_conflicts: int = 0) -> None:
    """Fold the DYNAMIC correctness audits (non-anticipativity, no-backdating, option
    conflicts — which run after the emit stage and otherwise land only in the log) into
    the same structured output as the feasibility counts, so all FOUR audit families read
    0 in one place (§6.1). Rewrites feasibility_audit.csv with the extra columns and
    appends a "Dynamic audits" section to 09_feasibility_audit.md.

    ``option_conflicts`` (added 2026-07-28, see ledger.drop_superseded_option_legs) counts
    option sets left with BOTH a DIRECT and an XDOCK leg selected because the losing side
    was already watermark-committed to a driver -- a real, possible non-zero event that
    previously reached only the runlog text stream, so a real occurrence would never show
    up in a §6.1 table built straight from this CSV. Defaults to 0 for callers that don't
    pass it (the static path, or any pre-existing caller)."""
    na, bd = int(non_anticipativity or 0), int(route_backdating or 0)
    oc = int(option_conflicts or 0)
    try:
        base = pd.read_csv(plan_dir / "feasibility_audit.csv").iloc[0].to_dict()
    except (FileNotFoundError, OSError, pd.errors.EmptyDataError, IndexError):
        base = {"capacity": 0, "duty_13h": 0, "drive_10h": 0, "window": 0,
                "mixed_tour_daily": 0}
    base["non_anticipativity"] = na
    base["route_backdating"] = bd
    base["option_conflicts"] = oc
    pd.DataFrame([base]).to_csv(plan_dir / "feasibility_audit.csv", index=False)
    hard = sum(int(base.get(k, 0) or 0) for k in
               ("capacity", "duty_13h", "drive_10h", "window", "mixed_tour_daily")) + na + bd + oc
    ok = "[OK]" if hard == 0 else "[VIOLATIONS]"
    extra = (
        "\n## Dynamic audits (non-anticipation & commitment)\n\n"
        f"- non-anticipativity violations: {na}\n"
        f"- commitment / no-backdating violations: {bd}\n"
        f"- option conflicts (committed DIRECT+XDOCK both survived, driver already committed): {oc}\n"
        f"- => ALL FOUR audit families, total violations: {hard} {ok}\n")
    try:
        p = plan_dir / "09_feasibility_audit.md"
        p.write_text(p.read_text(encoding="utf-8").rstrip() + "\n" + extra, encoding="utf-8")
    except (FileNotFoundError, OSError):
        pass


def feasibility_audit_md(audit: dict) -> str:
    duty = audit.get("duty_13h")
    duty_s = "n/a (duty column absent)" if duty is None else f"{duty}"
    hard_total = ((audit.get("capacity", 0) or 0)
                  + (audit.get("drive_10h", 0) or 0)
                  + (audit.get("window", 0) or 0)
                  + (audit.get("mixed_tour_daily", 0) or 0)
                  + (0 if duty is None else duty))
    ok = "[OK]" if hard_total == 0 else "[VIOLATIONS]"
    return "\n".join([
        "# Feasibility audit (committed plan) — §6.1 believability floor",
        "",
        f"- capacity violations (trip > 100% pallet or weight): {audit.get('capacity', 0)}",
        f"- duty violations (veh-day > 13h): {duty_s}",
        f"- drive violations (veh-day > 10h): {audit.get('drive_10h', 0)}",
        f"- window violations (committed delivery outside a hard range window): {audit.get('window', 0)}",
        f"- mixed tour/daily vehicle-days: {audit.get('mixed_tour_daily', 0)}",
        f"- => hard-constraint violations total: {hard_total} {ok}",
        "",
        f"- (context) deliveries past their tight deadline — SOFT-feasible, priced not rejected: "
        f"{audit.get('deliveries_past_deadline', 0)} (see §6.2 delivery timeliness)",
    ]) + "\n"
