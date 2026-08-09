"""Incumbent (historical) actuals for the §6.3a comparator.

The carrier kept NO historical *plan*, so the incumbent baseline is the fleet's
real telematics record over the window: odometer km (the ground-truth driven
distance), the count of vehicle-days that actually moved, and driver duty-hours
proxied by the moving-ping span. Delivery punctuality comes from the order record:
the recorded destination time is compared with the same explicit customer
window/deadline used to score the generated plan.

Everything here restricts to OUR fleet (``VEHICLE_DEPOT_MAP``); telematics assets
outside the fleet are ignored so the comparison is like-for-like with the plan.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

import pandas as pd

from freight_planner.shared.config import VEHICLE_DEPOT_MAP
from freight_planner.shared.scope import _delivery_window_policy
from freight_planner.vehicle_actuals import (
    actual_duty_by_vehicle,
    actual_km_by_vehicle,
)


def _days(start: date, end: date) -> list[date]:
    n = (end - start).days
    return [start + timedelta(days=i) for i in range(n + 1)]


def _historical_delivery_timeliness(
    orders_df: pd.DataFrame,
    start: date,
    end: date,
    delivery_order_ids: set[str],
) -> dict:
    """Compare actual destination times with explicit customer deadlines."""
    orders = orders_df.copy()
    orders["order_id"] = orders["order_id"].astype(str)
    orders = orders[orders["order_id"].isin({str(x) for x in delivery_order_ids})]
    orders = orders.drop_duplicates("order_id", keep="last")

    actual = pd.to_datetime(orders.get("destination_timestamp_local"), errors="coerce")
    destination_date = pd.to_datetime(orders.get("destination_date"), errors="coerce")
    service_day = actual.dt.date.where(actual.notna(), destination_date.dt.date)
    orders = orders.loc[
        service_day.map(lambda d: d is not None and not pd.isna(d) and start <= d <= end)
    ].copy()

    rows: list[dict] = []
    for _, row in orders.iterrows():
        policy = _delivery_window_policy(row)
        actual_ts = pd.to_datetime(row.get("destination_timestamp_local"), errors="coerce")
        late_minutes = None
        if policy.hardness in {"hard_slot", "soft_deadline"} and pd.notna(actual_ts):
            late_minutes = max(
                0.0,
                (actual_ts.to_pydatetime() - policy.raw_window[1]).total_seconds() / 60.0,
            )
        rows.append({
            "hardness": policy.hardness,
            "actual_missing": pd.isna(actual_ts),
            "late_minutes": late_minutes,
        })

    detail = pd.DataFrame(rows, columns=["hardness", "actual_missing", "late_minutes"])
    explicit = detail[detail["hardness"].isin({"hard_slot", "soft_deadline"})]
    measured = pd.to_numeric(explicit["late_minutes"], errors="coerce").dropna()
    late_values = measured[measured > 0]
    return {
        "delivery_obligations": int(len(detail)),
        "explicit_windows": int(len(explicit)),
        "date_only_excluded": int((detail["hardness"] == "date_only").sum()),
        "untimed_unknown_excluded": int((detail["hardness"] == "unknown").sum()),
        "missing_actual_timestamps": int(explicit["actual_missing"].sum()),
        "on_time": int((measured <= 0).sum()),
        "late": int((measured > 0).sum()),
        "average_late_minutes": round(float(late_values.mean()), 1) if len(late_values) else 0.0,
        "median_late_minutes": round(float(late_values.median()), 1) if len(late_values) else 0.0,
        "p90_late_minutes": round(float(late_values.quantile(0.9)), 1) if len(late_values) else 0.0,
        "maximum_late_minutes": round(float(late_values.max()), 1) if len(late_values) else 0.0,
    }


def build_incumbent_actuals(start: date, end: date,
                            fleet: set[str] | None = None,
                            min_km: float = 1.0,
                            loader: Callable[[date], pd.DataFrame] | None = None,
                            orders_df: pd.DataFrame | None = None,
                            delivery_order_ids: set[str] | None = None) -> dict:
    """Telematics actuals for our fleet over ``[start, end]`` (inclusive).

    ``per_day`` rows carry (date, odometer_km, vehicle_days, duty_hours). Odometer
    prefers the true CANbus odometer (miles->km), haversine-of-pings as fallback.

    The plan-vs-incumbent comparison is at FLEET-TOTAL level (all plan vehicles vs
    all telematics vehicles), NOT a per-registration match. A per-(vehicle,day) match
    was removed 2026-07-24: the solver never reproduces reality's vehicle-to-order
    assignment, so pairing plan-reg-X against telematics-reg-X compares unrelated
    routes and — because reality uses more vehicles — silently drops in-universe km
    done by regs that appear on only one side. The caveat on the fleet-total gap is
    that the incumbent side includes some OUT-OF-UNIVERSE fleet work (subcontract /
    local jobs we do not plan) that telematics km cannot be cleanly decomposed to
    exclude; this is stated in the report, not papered over with a fragile match.
    """
    fleet = set(VEHICLE_DEPOT_MAP) if fleet is None else set(fleet)
    _kw = {"loader": loader} if loader is not None else {}
    per_day: list[dict] = []
    for d in _days(start, end):
        km_by = actual_km_by_vehicle(d, prefer_odometer=True, **_kw)
        duty_by = actual_duty_by_vehicle(d, **_kw)
        odo_km = sum(k for v, k in km_by.items() if v in fleet and k >= min_km)
        vds = sum(1 for v, k in km_by.items() if v in fleet and k >= min_km)
        duty_h = sum(h for v, h in duty_by.items() if v in fleet)
        per_day.append({"date": d.isoformat(), "odometer_km": round(odo_km, 1),
                        "vehicle_days": int(vds), "duty_hours": round(duty_h, 1)})
    df = pd.DataFrame(per_day, columns=["date", "odometer_km", "vehicle_days", "duty_hours"])
    result = {
        "fleet_size": len(fleet),
        "total_odometer_km": round(float(df["odometer_km"].sum()), 1),
        "total_vehicle_days": int(df["vehicle_days"].sum()),
        "total_duty_hours": round(float(df["duty_hours"].sum()), 1),
        "per_day": df,
    }
    if orders_df is not None and delivery_order_ids is not None:
        result["delivery_timeliness"] = _historical_delivery_timeliness(
            orders_df, start, end, delivery_order_ids)
    return result


def incumbent_actuals_md(inc: dict, plan_km: float | None = None,
                         plan_trunk_km: float = 0.0,
                         plan_vehicle_days: int | None = None,
                         plan_driver_hours: float | None = None,
                         plan_delivery_timeliness: dict | None = None) -> str:
    """Markdown for the incumbent-actuals report, with plan-vs-incumbent Δ lines
    when the plan-side figures are supplied."""
    lines = [
        "# Incumbent actuals (telematics) — §6.3a comparator",
        "",
        "Incumbent = the fleet's real telematics record (no historical carrier plan exists).",
        f"- fleet size (regs): {inc['fleet_size']}",
        f"- total incumbent odometer km (whole fleet, window): {inc['total_odometer_km']:,.0f}",
        f"- incumbent vehicle-days (moved > {1} km): {inc['total_vehicle_days']}",
        f"- incumbent driver-hours (moving-ping span): {inc['total_duty_hours']:,.0f}",
    ]

    def _delta(plan, inc_val, label, fmt):
        if plan is None or not inc_val:
            return None
        return (f"- {label}: plan {fmt.format(plan)} vs incumbent {fmt.format(inc_val)} "
                f"= {(plan / inc_val - 1) * 100:+.1f}% ({fmt.format(plan - inc_val)})")

    full_plan_km = (None if plan_km is None
                    else float(plan_km) + float(plan_trunk_km or 0.0))
    deltas = [
        _delta(full_plan_km, inc["total_odometer_km"], "km", "{:,.0f}"),
        _delta(plan_vehicle_days, inc["total_vehicle_days"], "vehicle-days", "{:,.0f}"),
        _delta(plan_driver_hours, inc["total_duty_hours"],
               ("driver-hours (route plan only; trunk duty excluded)"
                if plan_trunk_km else "driver-hours"), "{:,.0f}"),
    ]
    deltas = [d for d in deltas if d]
    if deltas:
        mileage_basis = []
        if plan_km is not None and plan_trunk_km:
            mileage_basis = [
                f"- plan mileage basis: route plan {plan_km:,.0f} + scheduled trunk "
                f"{plan_trunk_km:,.0f} = full plan {full_plan_km:,.0f} km",
            ]
        lines += ["", "## Plan vs incumbent (fleet totals, whole window)",
                  *mileage_basis, *deltas,
                  "",
                  "CAVEAT: this is a fleet-TOTAL comparison (all plan vehicles vs all telematics "
                  "vehicles), NOT a per-registration match — the solver does not reproduce reality's "
                  "vehicle-to-order assignment. The incumbent total also includes some OUT-OF-UNIVERSE "
                  "fleet work (subcontract / local jobs we do not plan) that cannot be cleanly removed "
                  "from telematics km, so it modestly favours the plan on the km line."]
    timeliness = inc.get("delivery_timeliness")
    if timeliness is not None and plan_delivery_timeliness is not None:
        plan_t = plan_delivery_timeliness
        plan_n = plan_t["on_time"] + plan_t["late"]
        inc_n = timeliness["on_time"] + timeliness["late"]

        def _count_pct(count: int, total: int) -> str:
            return f"{count} ({100.0 * count / total:.1f}%)" if total else f"{count} (n/a)"

        lines += [
            "",
            "## Delivery timeliness: plan vs incumbent",
            "",
            "| measure | plan | incumbent |",
            "|---|---:|---:|",
            f"| delivery obligations/completions | "
            f"{plan_t['delivery_obligations']} | {timeliness['delivery_obligations']} |",
            f"| explicit windows/deadlines assessed | "
            f"{plan_t['explicit_windows']} | {timeliness['explicit_windows']} |",
            f"| date-only placeholders excluded | "
            f"{plan_t['date_only_excluded']} | {timeliness['date_only_excluded']} |",
            f"| on-time | {_count_pct(plan_t['on_time'], plan_n)} | "
            f"{_count_pct(timeliness['on_time'], inc_n)} |",
            f"| late | {_count_pct(plan_t['late'], plan_n)} | "
            f"{_count_pct(timeliness['late'], inc_n)} |",
            f"| average late minutes | {plan_t['average_late_minutes']:.1f} | "
            f"{timeliness['average_late_minutes']:.1f} |",
            f"| median late minutes | {plan_t['median_late_minutes']:.1f} | "
            f"{timeliness['median_late_minutes']:.1f} |",
            f"| p90 late minutes | {plan_t['p90_late_minutes']:.1f} | "
            f"{timeliness['p90_late_minutes']:.1f} |",
            f"| maximum late minutes | {plan_t['maximum_late_minutes']:.1f} | "
            f"{timeliness['maximum_late_minutes']:.1f} |",
            "",
            "Basis: both columns count each consignment once against explicit customer "
            "slots or deadlines. Split plan deliveries use the final part's arrival. "
            "Midnight/date-only placeholders are excluded.",
            "",
            "The plan column covers completed planned deliveries in the window; the "
            "incumbent column covers in-universe historical delivery obligations whose "
            "actual destination date falls in the window.",
        ]
    elif timeliness is not None:
        measured = timeliness["on_time"] + timeliness["late"]
        on_time_pct = 100.0 * timeliness["on_time"] / measured if measured else 0.0
        late_pct = 100.0 * timeliness["late"] / measured if measured else 0.0
        lines += [
            "",
            "## Historical delivery timeliness",
            "",
            f"- in-universe delivery obligations: {timeliness['delivery_obligations']}",
            f"- explicit windows/deadlines: {timeliness['explicit_windows']}",
            f"- date-only placeholders excluded: {timeliness['date_only_excluded']}",
        ]
        if timeliness["untimed_unknown_excluded"]:
            lines.append(
                f"- other untimed records excluded: {timeliness['untimed_unknown_excluded']}")
        lines += [
            f"- explicit-window records missing an actual destination timestamp: "
            f"{timeliness['missing_actual_timestamps']}",
            f"- on-time (by the deadline): {timeliness['on_time']} ({on_time_pct:.1f}%)",
            f"- late: {timeliness['late']} ({late_pct:.1f}%)",
        ]
        if timeliness["late"]:
            lines.append(
                "- late minutes — "
                f"avg {timeliness['average_late_minutes']:.0f}, "
                f"median {timeliness['median_late_minutes']:.0f}, "
                f"p90 {timeliness['p90_late_minutes']:.0f}, "
                f"max {timeliness['maximum_late_minutes']:.0f}")
        lines += [
            "",
            "Basis: actual destination timestamps are compared only with explicit customer "
            "slots or deadlines. Midnight/date-only placeholders are not treated as timed "
            "service promises.",
        ]
    lines += ["", "## Per day", "",
              "| date | odometer km | veh-days | driver-hours |",
              "|---|---|---|---|"]
    for r in inc["per_day"].itertuples(index=False):
        lines.append(f"| {r.date} | {r.odometer_km:,.0f} | {r.vehicle_days} | {r.duty_hours:,.0f} |")
    return "\n".join(lines) + "\n"
