from __future__ import annotations

import math

import pandas as pd

from freight_planner.shared.config import MAX_DRIVING_H_PER_DAY
from freight_planner.config import MAX_DUTY_H_PER_DAY, SPLIT_SHIFT_GAP_H


_DAILY_DRIVE_LIMIT_MINUTES = MAX_DRIVING_H_PER_DAY * 60.0
_DAILY_DUTY_LIMIT_MINUTES = MAX_DUTY_H_PER_DAY * 60.0

# Duty (drive + service + wait) columns, added to the vehicle-day frame only when
# a route_stops_df is supplied. Absent by design for callers that pass drive-only.
_DUTY_COLUMNS = [
    "duty_minutes", "duty_limit_minutes",
    "duty_utilization_pct", "duty_headroom_minutes",
]


def _safe_float(value) -> float:
    try:
        if value is None or value == "" or pd.isna(value):
            return 0.0
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _pct(numerator: float, denominator: float) -> float:
    if denominator <= 0.0:
        return 0.0
    return numerator / denominator * 100.0


def _fmt_pct(value: float) -> str:
    if math.isnan(value):
        return "0.0%"
    return f"{value:.1f}%"


def _summary_stats(series: pd.Series) -> dict[str, float]:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return {"avg": 0.0, "median": 0.0, "p90": 0.0, "max": 0.0}
    return {
        "avg": float(clean.mean()),
        "median": float(clean.median()),
        "p90": float(clean.quantile(0.9)),
        "max": float(clean.max()),
    }


def _tour_day_rows(rid: str, grp: pd.DataFrame, route_totals: dict) -> list[dict]:
    """One row per emitted tour day; evaluator return segments are already split."""
    vid = str(grp["vehicle_id"].iloc[0])
    by_day = {str(d): g for d, g in grp.groupby(grp["service_date"].astype(str))}
    days = sorted(by_day)
    minutes = {d: float(g["planned_drive_minutes"].astype(float).sum()) for d, g in by_day.items()}
    freight_kinds = {"CUSTOMER_PICKUP", "CUSTOMER_DELIVERY",
                     "DIRECT_CUSTOMER_MOVE", "HUB_DROP", "DEPOT_LOAD"}
    jobs = {d: (int(g["leg_kind"].astype(str).isin(freight_kinds).sum())
                if "leg_kind" in g.columns else int(len(g)))
            for d, g in by_day.items()}
    return [{
        "route_id": rid, "vehicle_id": vid, "service_date": d, "is_tour": True,
        "trip_count": 1, "job_count": int(jobs.get(d, 0)),
        "planned_drive_minutes": float(minutes[d]),
        "drive_limit_minutes": _DAILY_DRIVE_LIMIT_MINUTES,
        "drive_utilization_pct": _pct(float(minutes[d]), _DAILY_DRIVE_LIMIT_MINUTES),
    } for d in days]


def _endpoints(sub: pd.DataFrame, col: str, how: str) -> dict[tuple[str, str], pd.Timestamp]:
    if sub.empty or col not in sub.columns:
        return {}
    ts = pd.to_datetime(sub[col].astype(str), errors="coerce", format="mixed")
    tmp = pd.DataFrame({
        "vehicle_id": sub["vehicle_id"].astype(str).to_numpy(),
        "service_date": sub["service_date"].astype(str).to_numpy(),
        "ts": ts.to_numpy(),
    }).dropna(subset=["ts"])
    if tmp.empty:
        return {}
    agg = tmp.groupby(["vehicle_id", "service_date"])["ts"].agg(how)
    return {(str(k[0]), str(k[1])): pd.Timestamp(v) for k, v in agg.items()}


def _trip_endpoints(route_stops_df: pd.DataFrame, stop_type: str, col: str,
                    how: str) -> dict[tuple[str, str, str], pd.Timestamp]:
    """Same as ``_endpoints`` but keyed by (vehicle_id, service_date, trip_index),
    so a multi-trip vehicle-day yields one endpoint PER TRIP rather than one
    collapsed across all of them. Rows with no ``trip_index`` column (pre-existing
    single-trip callers) fall back to one implicit trip per (vehicle, date)."""
    sub = route_stops_df[route_stops_df["stop_type"].astype(str).eq(stop_type)]
    if sub.empty or col not in sub.columns:
        return {}
    ts = pd.to_datetime(sub[col].astype(str), errors="coerce", format="mixed")
    trip = sub["trip_index"].astype(str) if "trip_index" in sub.columns else "0"
    tmp = pd.DataFrame({
        "vehicle_id": sub["vehicle_id"].astype(str).to_numpy(),
        "service_date": sub["service_date"].astype(str).to_numpy(),
        "trip_index": (trip.to_numpy() if hasattr(trip, "to_numpy")
                       else [trip] * len(sub)),
        "ts": ts.to_numpy(),
    }).dropna(subset=["ts"])
    if tmp.empty:
        return {}
    agg = tmp.groupby(["vehicle_id", "service_date", "trip_index"])["ts"].agg(how)
    return {(str(k[0]), str(k[1]), str(k[2])): pd.Timestamp(v) for k, v in agg.items()}


def _chain_duty_minutes(route_stops_df: pd.DataFrame) -> dict[tuple[str, str], float]:
    """Duty span per (vehicle_id, service_date), CHAIN-aware: a depot gap between
    two trips >= ``SPLIT_SHIFT_GAP_H`` ends a chain (routing_adapter.evaluate_day's
    real 13h-cap check, spec 2026-07-16 -- a held evening trip on a morning vehicle
    is legal while any single working stretch stays <= 13h). Reports the LONGEST
    chain's span, the one figure that actually binds against the cap; a naive
    first-depart-to-last-return span across the whole idle-gap-included day
    over-counts and produces false 13h-cap violations (confirmed against a real
    W0_baseline run, 2026-07-29: X90RNW/2026-02-16 ran two trips 4h14m apart, each
    comfortably under cap, reported as one 13.6h shift)."""
    depart = _trip_endpoints(route_stops_df, "depot_start", "planned_depart", "min")
    arrive = _trip_endpoints(route_stops_df, "depot_return", "planned_arrive", "max")
    by_day: dict[tuple[str, str], list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
    for key in set(depart) & set(arrive):
        v, d, _trip = key
        s, e = depart[key], arrive[key]
        if e > s:
            by_day.setdefault((v, d), []).append((s, e))
    out: dict[tuple[str, str], float] = {}
    for key, intervals in by_day.items():
        intervals.sort()
        chain_start, chain_end = intervals[0]
        best = 0.0
        for s, e in intervals[1:]:
            if (s - chain_end).total_seconds() / 3600.0 >= float(SPLIT_SHIFT_GAP_H):
                best = max(best, (chain_end - chain_start).total_seconds() / 60.0)
                chain_start, chain_end = s, e
            else:
                chain_end = max(chain_end, e)
        best = max(best, (chain_end - chain_start).total_seconds() / 60.0)
        out[key] = best
    return out


def _duty_minutes_by_key(route_stops_df: pd.DataFrame) -> dict[tuple[str, str], float]:
    """Depot-to-depot duty span (minutes) per (vehicle_id, service_date), chain-
    aware across split shifts (see ``_chain_duty_minutes``). A key with no
    resolvable endpoint pair maps to NaN (duty is unknowable)."""
    if (route_stops_df is None or route_stops_df.empty
            or "stop_type" not in route_stops_df.columns):
        return {}
    chained = _chain_duty_minutes(route_stops_df)
    # day-activity clock (first->last stamped stop) per (vehicle, service_date), the
    # fallback for a TOUR day that has no same-day depot pair — its depot_start is on day 1
    # and depot_return on day N, so both would otherwise be NaN and duty is never evaluated
    # for tours (audit #3, 2026-07-26). Daily routes have a clean pair, so this never
    # overrides them (the depot span == the activity span there).
    lo = _endpoints(route_stops_df, "planned_arrive", "min"); lo2 = _endpoints(route_stops_df, "planned_depart", "min")
    hi = _endpoints(route_stops_df, "planned_arrive", "max"); hi2 = _endpoints(route_stops_df, "planned_depart", "max")
    act_lo = {k: min(v for v in (lo.get(k), lo2.get(k)) if v is not None)
              for k in set(lo) | set(lo2)}
    act_hi = {k: max(v for v in (hi.get(k), hi2.get(k)) if v is not None)
              for k in set(hi) | set(hi2)}
    out: dict[tuple[str, str], float] = {}
    for key in set(chained) | set(act_lo):
        if key in chained:
            out[key] = chained[key]
        elif key in act_lo and key in act_hi and act_hi[key] > act_lo[key]:
            out[key] = (act_hi[key] - act_lo[key]).total_seconds() / 60.0   # tour-day activity span
        else:
            out[key] = math.nan
    return out


def _attach_duty_columns(df: pd.DataFrame, route_stops_df: pd.DataFrame) -> pd.DataFrame:
    """Append duty columns keyed on (vehicle_id, service_date). Rows without both
    depot endpoints carry NaN across every duty column."""
    duty = _duty_minutes_by_key(route_stops_df)
    df = df.copy()
    minutes = [
        duty.get((str(v), str(d)), math.nan)
        for v, d in zip(df["vehicle_id"], df["service_date"])
    ]
    df["duty_minutes"] = minutes
    df["duty_limit_minutes"] = _DAILY_DUTY_LIMIT_MINUTES
    df["duty_utilization_pct"] = [
        math.nan if math.isnan(m) else _pct(m, _DAILY_DUTY_LIMIT_MINUTES)
        for m in minutes
    ]
    df["duty_headroom_minutes"] = [
        math.nan if math.isnan(m) else max(0.0, _DAILY_DUTY_LIMIT_MINUTES - m)
        for m in minutes
    ]
    return df


def build_vehicle_day_utilization(
    selected_df: pd.DataFrame,
    route_totals: dict | None = None,
    route_stops_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    cols = [
        "route_id", "vehicle_id", "service_date", "is_tour",
        "trip_count", "job_count", "planned_drive_minutes",
        "drive_limit_minutes", "drive_utilization_pct",
    ]
    if selected_df is None or selected_df.empty:
        empty = pd.DataFrame(columns=cols)
        if route_stops_df is not None:
            for c in _DUTY_COLUMNS:
                empty[c] = pd.Series(dtype=float)
        return empty

    route_totals = route_totals or {}
    grouped = selected_df.groupby("route_id", sort=True)
    rows: list[dict] = []
    for route_id, grp in grouped:
        rid = str(route_id)
        if rid.startswith("TOUR:"):
            rows.extend(_tour_day_rows(rid, grp, route_totals))
            continue
        planned_drive = float(grp["planned_drive_minutes"].astype(float).sum())
        exact_drive = None
        if (route_stops_df is not None and not route_stops_df.empty
                and {"route_id", "drive_minutes"} <= set(route_stops_df.columns)):
            route_stop_rows = route_stops_df[
                route_stops_df["route_id"].astype(str).eq(rid)]
            if not route_stop_rows.empty:
                exact_drive = float(
                    pd.to_numeric(route_stop_rows["drive_minutes"], errors="coerce")
                    .fillna(0.0).sum()
                )
        if exact_drive is not None:
            planned_drive = exact_drive
        elif rid in route_totals:
            selected_km = float(grp["planned_km"].astype(float).sum())
            residual_km = max(0.0, float(route_totals[rid]) - selected_km)
            # return-to-depot leg at the vehicle-day's effective pace (min/km): reduces
            # to residual/50*60 under the constant-speed model (bit-identical flag-off),
            # uses the OSRM pace under USE_OSRM_DURATIONS.
            pace = (planned_drive / selected_km) if (selected_km > 0 and planned_drive > 0) else (60.0 / 50.0)
            planned_drive += residual_km * pace
        rows.append({
            "route_id": rid,
            "vehicle_id": str(grp["vehicle_id"].iloc[0]),
            "service_date": str(grp["service_date"].iloc[0]),
            "is_tour": bool(rid.startswith("TOUR:")),
            "trip_count": int(grp["trip_index"].nunique()) if "trip_index" in grp.columns else 1,
            "job_count": int(len(grp)),
            "planned_drive_minutes": planned_drive,
            "drive_limit_minutes": _DAILY_DRIVE_LIMIT_MINUTES,
            "drive_utilization_pct": _pct(planned_drive, _DAILY_DRIVE_LIMIT_MINUTES),
        })
    df = pd.DataFrame(rows, columns=cols)
    if route_stops_df is not None:
        df = _attach_duty_columns(df, route_stops_df)
    return df


def build_trip_capacity_utilization(
    selected_df: pd.DataFrame,
    candidate_df: pd.DataFrame,
    vehicle_df: pd.DataFrame,
) -> pd.DataFrame:
    cols = [
        "route_id", "trip_index", "vehicle_id", "service_date", "vehicle_type",
        "capacity_pallets", "capacity_kg",
        "peak_pallets", "peak_kg",
        "pallet_utilization_pct", "kg_utilization_pct",
        "stop_count",
    ]
    if selected_df is None or selected_df.empty:
        return pd.DataFrame(columns=cols)

    candidate_by_job = {}
    if candidate_df is not None and not candidate_df.empty:
        candidate_by_job = {str(r.get("job_id", "")): r for r in candidate_df.to_dict("records")}

    vehicle_caps = {}
    if vehicle_df is not None and not vehicle_df.empty:
        for row in vehicle_df.to_dict("records"):
            vehicle_caps[str(row.get("vehicle_id", ""))] = {
                "vehicle_type": str(row.get("vehicle_type", "") or ""),
                "capacity_pallets": _safe_float(row.get("capacity_pallets")),
                "capacity_kg": _safe_float(row.get("capacity_kg")),
            }

    group_cols = ["route_id", "trip_index"] if "trip_index" in selected_df.columns else ["route_id"]
    rows: list[dict] = []
    for key, grp in selected_df.groupby(group_cols, sort=True):
        if isinstance(key, tuple) and len(key) > 1:
            route_id, trip_index = str(key[0]), int(key[1] or 0)
        elif isinstance(key, tuple):
            route_id, trip_index = str(key[0]), 0
        else:
            route_id, trip_index = str(key), 0
        grp = grp.sort_values("sequence")
        vehicle_id = str(grp["vehicle_id"].iloc[0])
        caps = vehicle_caps.get(vehicle_id, {})
        cap_p = _safe_float(caps.get("capacity_pallets"))
        cap_kg = _safe_float(caps.get("capacity_kg"))

        delivery_p = 0.0
        delivery_kg = 0.0
        jobs: list[tuple[str, float, float]] = []
        for row in grp.itertuples(index=False):
            job = candidate_by_job.get(str(getattr(row, "job_id", "")), {})
            pallets = _safe_float(job.get("pallets"))
            kg = _safe_float(job.get("weight_kg"))
            leg_kind = str(getattr(row, "leg_kind", "") or "")
            jobs.append((leg_kind, pallets, kg))
            if leg_kind == "CUSTOMER_DELIVERY":
                delivery_p += pallets
                delivery_kg += kg

        running_p = delivery_p
        running_kg = delivery_kg
        peak_p = running_p
        peak_kg = running_kg
        for leg_kind, pallets, kg in jobs:
            if leg_kind == "CUSTOMER_DELIVERY":
                running_p -= pallets
                running_kg -= kg
            elif leg_kind == "CUSTOMER_PICKUP":
                running_p += pallets
                running_kg += kg
                peak_p = max(peak_p, running_p)
                peak_kg = max(peak_kg, running_kg)
            else:
                peak_p = max(peak_p, running_p + pallets)
                peak_kg = max(peak_kg, running_kg + kg)

        rows.append({
            "route_id": route_id,
            "trip_index": trip_index,
            "vehicle_id": vehicle_id,
            "service_date": str(grp["service_date"].iloc[0]),
            "vehicle_type": str(caps.get("vehicle_type", "") or ""),
            "capacity_pallets": cap_p,
            "capacity_kg": cap_kg,
            "peak_pallets": peak_p,
            "peak_kg": peak_kg,
            "pallet_utilization_pct": _pct(peak_p, cap_p),
            "kg_utilization_pct": _pct(peak_kg, cap_kg),
            "stop_count": int(len(grp)),
        })

    return pd.DataFrame(rows, columns=cols)


def utilization_summary_md(
    vehicle_day_df: pd.DataFrame,
    trip_df: pd.DataFrame,
) -> str:
    daily = vehicle_day_df[
        vehicle_day_df["is_tour"].astype(bool).eq(False)
    ] if vehicle_day_df is not None and not vehicle_day_df.empty else pd.DataFrame()
    drive_stats = _summary_stats(daily["drive_utilization_pct"]) if not daily.empty else _summary_stats(pd.Series(dtype=float))
    trip_p_stats = _summary_stats(trip_df["pallet_utilization_pct"]) if trip_df is not None and not trip_df.empty else _summary_stats(pd.Series(dtype=float))
    trip_kg_stats = _summary_stats(trip_df["kg_utilization_pct"]) if trip_df is not None and not trip_df.empty else _summary_stats(pd.Series(dtype=float))

    daily_high = int((daily["drive_utilization_pct"] >= 90.0).sum()) if not daily.empty else 0
    daily_low = int((daily["drive_utilization_pct"] < 50.0).sum()) if not daily.empty else 0
    trip_p_high = int((trip_df["pallet_utilization_pct"] >= 90.0).sum()) if trip_df is not None and not trip_df.empty else 0
    trip_kg_high = int((trip_df["kg_utilization_pct"] >= 90.0).sum()) if trip_df is not None and not trip_df.empty else 0

    lines = [
        "# Utilization Summary",
        "",
        "## Driving Window Utilization",
        "",
        f"- daily vehicle-days assessed: {len(daily)}",
        f"- average drive utilization: {_fmt_pct(drive_stats['avg'])} of the {MAX_DRIVING_H_PER_DAY:g}h limit",
        f"- median drive utilization: {_fmt_pct(drive_stats['median'])}",
        f"- p90 drive utilization: {_fmt_pct(drive_stats['p90'])}",
        f"- max drive utilization: {_fmt_pct(drive_stats['max'])}",
        f"- vehicle-days at or above 90%: {daily_high}",
        f"- vehicle-days below 50%: {daily_low}",
        "",
    ]

    duty_present = (
        vehicle_day_df is not None and not vehicle_day_df.empty
        and "duty_utilization_pct" in vehicle_day_df.columns
    )
    if duty_present:
        duty_util = (
            pd.to_numeric(daily["duty_utilization_pct"], errors="coerce").dropna()
            if not daily.empty else pd.Series(dtype=float)
        )
        duty_stats = _summary_stats(duty_util)
        duty_high = int((duty_util >= 90.0).sum())
        duty_over = int((duty_util > 100.0).sum())
        lines += [
            "## Duty Window Utilization",
            "",
            f"- duty vehicle-days assessed: {len(duty_util)}",
            f"- average duty utilization: {_fmt_pct(duty_stats['avg'])} of the {MAX_DUTY_H_PER_DAY:g}h cap",
            f"- median duty utilization: {_fmt_pct(duty_stats['median'])}",
            f"- p90 duty utilization: {_fmt_pct(duty_stats['p90'])}",
            f"- max duty utilization: {_fmt_pct(duty_stats['max'])}",
            f"- vehicle-days at or above 90%: {duty_high}",
            f"- vehicle-days over the {MAX_DUTY_H_PER_DAY:g}h cap: {duty_over}",
            "",
        ]

    lines += [
        "## Trip Capacity Utilization",
        "",
        f"- trips assessed: {0 if trip_df is None else len(trip_df)}",
        f"- average pallet utilization: {_fmt_pct(trip_p_stats['avg'])}",
        f"- median pallet utilization: {_fmt_pct(trip_p_stats['median'])}",
        f"- p90 pallet utilization: {_fmt_pct(trip_p_stats['p90'])}",
        f"- average weight utilization: {_fmt_pct(trip_kg_stats['avg'])}",
        f"- median weight utilization: {_fmt_pct(trip_kg_stats['median'])}",
        f"- p90 weight utilization: {_fmt_pct(trip_kg_stats['p90'])}",
        f"- trips at or above 90% pallet capacity: {trip_p_high}",
        f"- trips at or above 90% weight capacity: {trip_kg_high}",
        "",
    ]
    return "\n".join(lines)



