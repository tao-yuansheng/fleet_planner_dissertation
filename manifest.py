"""Milestone 9: operator-readable manifest from the selected plan.

Produces four views that together account for every physical movement:
  * ``plan_manifest_new`` 鈥?one row per movement, bucketed as ROUTED (a real
    dispatched leg), ACCOUNTING (trunk / accounting-only, not a routed job), or
    UNASSIGNED (a runnable job that could not be served, with its reason);
  * ``unassigned_jobs`` 鈥?the rejected jobs enriched with order/leg context;
  * ``vehicle_routes`` 鈥?per-route summary (stops, km, drive minutes, tour flag);
  * ``depot_inventory_timeline`` 鈥?freight in/out and running balance per depot.

An accounting-only row is never counted as a routed job, and every order with a
leg lands in exactly one bucket of the manifest.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from freight_planner.shared.config import DEPOT_ANCHORS
from freight_planner.route_costs import drive_minutes, road_km, road_minutes, statutory_breaks

ROUTED_KINDS = {"CUSTOMER_PICKUP", "CUSTOMER_DELIVERY", "DIRECT_CUSTOMER_MOVE", "HUB_DROP"}
ACCOUNTING_KINDS = {"INBOUND_TRUNK", "OUTBOUND_TRUNK", "ACCOUNTING_ONLY"}

MANIFEST_KIND = {
    "CUSTOMER_PICKUP": "customer_pickup",
    "CUSTOMER_DELIVERY": "customer_delivery",
    "DIRECT_CUSTOMER_MOVE": "direct_customer_move",
    "HUB_DROP": "hub_drop",
    "INBOUND_TRUNK": "inbound_trunk",
    "OUTBOUND_TRUNK": "outbound_trunk",
    "ACCOUNTING_ONLY": "accounting_only",
}

_MANIFEST_COLUMNS = ["order_id", "leg_id", "job_id", "manifest_kind", "status",
                     "vehicle_id", "route_id", "trip_id", "trip_index", "service_date", "planned_km", "reason"]


def _kind(leg_kind: str) -> str:
    return MANIFEST_KIND.get(str(leg_kind), str(leg_kind).lower())


def build_plan_manifest(selected_df: pd.DataFrame, legs_df: pd.DataFrame,
                        rejected, candidate_df: pd.DataFrame,
                        route_totals: dict | None = None) -> pd.DataFrame:
    rows: list[dict] = []
    served_leg_ids: set[str] = set()
    served_job_ids: set[str] = set()
    route_leg_km: dict[str, float] = {}
    route_meta: dict[str, tuple[str, str]] = {}  # route_id -> (vehicle_id, service_date)

    if selected_df is not None and not selected_df.empty:
        for r in selected_df.itertuples(index=False):
            served_leg_ids.add(str(getattr(r, "leg_id", "")))
            served_job_ids.add(str(getattr(r, "job_id", "")))
            rid = str(getattr(r, "route_id", ""))
            route_leg_km[rid] = route_leg_km.get(rid, 0.0) + float(getattr(r, "planned_km", 0.0) or 0.0)
            route_meta.setdefault(rid, (str(getattr(r, "vehicle_id", "")), str(getattr(r, "service_date", ""))))
            rows.append({
                "order_id": str(getattr(r, "order_id", "")),
                "leg_id": str(getattr(r, "leg_id", "")),
                "job_id": str(getattr(r, "job_id", "")),
                "manifest_kind": _kind(getattr(r, "leg_kind", "")),
                "status": "ROUTED",
                "vehicle_id": str(getattr(r, "vehicle_id", "")),
                "route_id": str(getattr(r, "route_id", "")),
                "trip_id": str(getattr(r, "trip_id", "") or ""),
                "trip_index": int(getattr(r, "trip_index", 0) or 0),
                "service_date": str(getattr(r, "service_date", "")),
                "planned_km": float(getattr(r, "planned_km", 0.0) or 0.0),
                "reason": "",
            })

    # the return-to-depot leg has no order; emit one explicit movement per trip
    # so manifest km reconciles with evaluator/KPI totals.
    if selected_df is not None and not selected_df.empty:
        group_cols = ["route_id", "trip_index"] if "trip_index" in selected_df.columns else ["route_id"]
        route_group_counts = selected_df.groupby("route_id")["trip_index"].nunique().to_dict() if "trip_index" in selected_df.columns else {}
        groups = selected_df.groupby(group_cols, sort=False)
        for key, grp in groups:
            if isinstance(key, tuple) and len(key) > 1:
                rid, trip_index = str(key[0]), int(key[1] or 0)
            elif isinstance(key, tuple):
                rid, trip_index = str(key[0]), 0
            else:
                rid, trip_index = str(key), 0
            trip_key = f"{rid}#T{trip_index}"
            leg_km = float(grp["planned_km"].astype(float).sum())
            if trip_key in (route_totals or {}):
                total = float(route_totals[trip_key])
            elif rid in (route_totals or {}) and (not route_group_counts or int(route_group_counts.get(rid, 1)) == 1):
                total = float(route_totals[rid])
            else:
                total = leg_km
            ret_km = total - leg_km
            if abs(ret_km) < 1e-9:
                continue
            first = grp.iloc[0]
            rows.append({
                "order_id": "", "leg_id": "", "job_id": "", "manifest_kind": "depot_return",
                "status": "ROUTED", "vehicle_id": str(first.get("vehicle_id", "")),
                "route_id": rid, "trip_id": f"{rid}#T{trip_index}", "trip_index": trip_index,
                "service_date": str(first.get("service_date", "")),
                "planned_km": ret_km, "reason": "",
            })

    if legs_df is not None and not legs_df.empty:
        acc = legs_df[legs_df["leg_kind"].isin(ACCOUNTING_KINDS)]
        for r in acc.itertuples(index=False):
            leg_id = str(getattr(r, "leg_id", ""))
            if leg_id in served_leg_ids:
                continue
            rows.append({
                "order_id": str(getattr(r, "order_id", "")),
                "leg_id": leg_id,
                "job_id": "",
                "manifest_kind": _kind(getattr(r, "leg_kind", "")),
                "status": "ACCOUNTING",
                "vehicle_id": "",
                "route_id": "",
                "trip_id": "",
                "trip_index": 0,
                "service_date": str(getattr(r, "service_date", "")),
                "planned_km": 0.0,
                "reason": str(getattr(r, "result_state", "") or ""),
            })

    records = candidate_df.to_dict("records") if candidate_df is not None and not candidate_df.empty else []
    cand_by_job = {str(c.get("job_id")): c for c in records}
    for rj in rejected or []:
        job_id = str(getattr(rj, "job_id", ""))
        if job_id in served_job_ids:
            continue
        c = cand_by_job.get(job_id, {})
        served_job_ids.add(job_id)
        rows.append({
            "order_id": str(c.get("order_id", "")),
            "leg_id": str(c.get("leg_id", "")),
            "job_id": job_id,
            "manifest_kind": _kind(c.get("leg_kind", "")),
            "status": "UNASSIGNED",
            "vehicle_id": "",
            "route_id": "",
            "trip_index": 0,
            "service_date": str(c.get("service_date", "")),
            "planned_km": 0.0,
            "reason": str(getattr(rj, "reason", "")),
        })

    # candidates that never became runnable (massive, bad geocode, no capable
    # vehicle, ...) are physically unassignable -- account for them too
    for c in records:
        blocker = str(c.get("hard_blocker", "") or "")
        job_id = str(c.get("job_id", ""))
        if not blocker or job_id in served_job_ids:
            continue
        rows.append({
            "order_id": str(c.get("order_id", "")),
            "leg_id": str(c.get("leg_id", "")),
            "job_id": job_id,
            "manifest_kind": _kind(c.get("leg_kind", "")),
            "status": "BLOCKED",
            "vehicle_id": "",
            "route_id": "",
            "trip_index": 0,
            "service_date": str(c.get("service_date", "")),
            "planned_km": 0.0,
            "reason": blocker,
        })

    return pd.DataFrame(rows, columns=_MANIFEST_COLUMNS)


def build_unassigned(rejected, candidate_df: pd.DataFrame,
                     selected_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Return genuine operator-facing misses only.

    Raw optimiser rejections also contain losing DIRECT/XDOCK alternatives,
    already-selected jobs retained in a stale rejection pool, and work deferred
    beyond the window.  Those remain available to KPI/choice accounting but do
    not belong in ``unassigned_jobs.csv``.
    """
    cols = ["job_id", "order_id", "leg_id", "leg_kind", "service_pc", "source_depot", "reason"]
    records = candidate_df.to_dict("records") if candidate_df is not None and not candidate_df.empty else []
    cand_by_job = {str(c.get("job_id")): c for c in records}
    selected_ids = (set(selected_df["job_id"].astype(str))
                    if selected_df is not None and not selected_df.empty
                    and "job_id" in selected_df.columns else set())

    # A rejected job in an unchosen option group is not a miss.  Keep incomplete
    # legs from the CHOSEN group visible, but remove all rival groups once any
    # group in the same option set has been selected.
    losing_option_ids: set[str] = set()
    if records and selected_ids:
        candidates = pd.DataFrame(records)
        if {"job_id", "option_set", "option_group"} <= set(candidates.columns):
            candidates["job_id"] = candidates["job_id"].astype(str)
            candidates["option_set"] = candidates["option_set"].fillna("").astype(str)
            candidates["option_group"] = candidates["option_group"].fillna("").astype(str)
            for option_set, group in candidates[candidates["option_set"].ne("")].groupby("option_set"):
                chosen_groups = set(group.loc[group["job_id"].isin(selected_ids), "option_group"])
                if chosen_groups:
                    losing_option_ids |= set(
                        group.loc[~group["option_group"].isin(chosen_groups), "job_id"])

    selected_collection_orders: set[str] = set()
    selected_direct_orders: set[str] = set()
    if selected_df is not None and not selected_df.empty and "order_id" in selected_df.columns:
        for row in selected_df.to_dict("records"):
            kind = str(row.get("leg_kind", "")).upper()
            job_id = str(row.get("job_id", ""))
            if kind in {"CUSTOMER_PICKUP", "DIRECT_CUSTOMER_MOVE", "HUB_DROP"} or (
                    kind == "CUSTOMER_DELIVERY" and job_id.rsplit(":", 1)[-1].startswith("DIR")):
                selected_collection_orders.add(str(row.get("order_id", "")).split("#", 1)[0])
            if kind == "DIRECT_CUSTOMER_MOVE" or (
                    kind == "CUSTOMER_DELIVERY" and job_id.rsplit(":", 1)[-1].startswith("DIR")):
                selected_direct_orders.add(str(row.get("order_id", "")).split("#", 1)[0])
    rows = []
    seen: set[str] = set()

    def _append(job_id: str, reason: str, c: dict | None = None) -> None:
        candidate = c or cand_by_job.get(job_id, {})
        order_id = str(candidate.get("order_id", ""))
        if not order_id and job_id.startswith("ORDER:"):
            order_id = job_id[6:]
        elif not order_id and job_id.startswith("JOB:"):
            order_id = job_id[4:].rsplit(":", 1)[0]
        if (job_id in seen or job_id in selected_ids or job_id in losing_option_ids
                or str(reason) == "DUE_BEYOND_WINDOW"
                or (not candidate and order_id in selected_direct_orders)
                or (job_id.startswith("ORDER:") and order_id in selected_collection_orders)):
            return
        seen.add(job_id)
        c = c or cand_by_job.get(job_id, {})
        rows.append({
            "job_id": job_id,
            # an ORDER:<id> ledger pseudo-row has no candidate — carry its order id
            "order_id": order_id,
            "leg_id": str(c.get("leg_id", "")),
            "leg_kind": str(c.get("leg_kind", "")),
            "service_pc": str(c.get("service_pc", "")),
            "source_depot": str(c.get("source_depot", "")),
            "reason": str(reason),
        })

    for rj in rejected or []:
        job_id = str(getattr(rj, "job_id", ""))
        _append(job_id, str(getattr(rj, "reason", "")))

    # Include pre-routing blockers (massive, bad geocode, no capable vehicle, ...)
    # so the operator-facing miss file matches the manifest's BLOCKED rows.
    for c in records:
        blocker = str(c.get("hard_blocker", "") or "")
        if blocker:
            _append(str(c.get("job_id", "")), blocker, c)

    # An order can arrive twice: an ORDER:<id> ledger row (its routing outcome)
    # AND a JOB:<leg> row (its physical reason, with leg detail). The job row
    # wins — e83b62b5 was double-counted as NO_FEASIBLE_ROUTE +
    # MASSIVE_UNSUPPORTED (2026-07-16).
    job_orders = {r["order_id"] for r in rows
                  if r["order_id"] and not str(r["job_id"]).startswith("ORDER:")}
    rows = [r for r in rows
            if not (str(r["job_id"]).startswith("ORDER:") and r["order_id"] in job_orders)]
    # A pre-window collection is satisfied HISTORY, not a miss: the order is
    # reframed as a prestaged in-window delivery (2026-07-16), so its blocked
    # pickup row must not pollute the operator miss file.
    rows = [r for r in rows if r["reason"] != "BEFORE_PLANNING_START"]
    return pd.DataFrame(rows, columns=cols)


def clip_route_stops_to_window(route_stops_df: pd.DataFrame | None,
                               start_iso: str, end_iso: str) -> pd.DataFrame:
    """Rows whose ``service_date`` falls in the inclusive window [start, end].

    The HEADLINE plan aggregates (plan km, veh-days, driver-hours) must be measured
    over the SAME days as the incumbent/telematics side, which is built over
    [start, end] inclusive (``incumbent_actuals.build_incumbent_actuals``). A tour
    that returns the day AFTER the window contributes real geometry on that later
    day (a return leg, a late delivery) which, summed unclipped, inflates the plan
    against a window-capped reality — the +6k km "gap" that was largely an accounting
    artifact (decision-audit #1, 2026-07-26). route_stops.csv itself is written
    UNCLIPPED so the map/replay keeps the beyond-window tail; only the KPI aggregates
    clip. ISO date strings sort lexicographically, so a plain string compare is exact."""
    if route_stops_df is None or route_stops_df.empty or "service_date" not in route_stops_df.columns:
        return route_stops_df if route_stops_df is not None else pd.DataFrame()
    sd = route_stops_df["service_date"].astype(str)
    return route_stops_df[(sd >= str(start_iso)) & (sd <= str(end_iso))]


def geom_route_totals(route_stops_df: pd.DataFrame | None) -> dict[str, float]:
    """Committed per-route km from the route_stops GEOMETRY -- the single source of
    truth that replaces the phantom evaluator ``route_totals`` in every downstream
    report (manifest, vehicle_routes, utilization, selected_plan export, cross-depot,
    KPI). Emits BOTH a route-level key (``route_id``) and per-trip keys
    (``route_id#T<trip>``) so multi-trip routes keep every return leg (consumers key
    the return residual on the trip and drop it otherwise)."""
    out: dict[str, float] = {}
    if route_stops_df is None or route_stops_df.empty or "leg_km" not in route_stops_df.columns:
        return out
    for rid, v in route_stops_df.groupby("route_id")["leg_km"].sum().items():
        out[str(rid)] = float(v)
    if "trip_index" in route_stops_df.columns:
        for (rid, ti), v in route_stops_df.groupby(["route_id", "trip_index"])["leg_km"].sum().items():
            try:
                out[f"{rid}#T{int(ti)}"] = float(v)
            except (ValueError, TypeError):
                pass
    return out


def geom_route_drive_totals(route_stops_df: pd.DataFrame | None) -> dict[str, float]:
    """Exact emitted drive minutes keyed by route and route-trip."""
    out: dict[str, float] = {}
    if (route_stops_df is None or route_stops_df.empty
            or "drive_minutes" not in route_stops_df.columns):
        return out
    for rid, value in route_stops_df.groupby("route_id")["drive_minutes"].sum().items():
        out[str(rid)] = float(value)
    if "trip_index" in route_stops_df.columns:
        for (rid, trip_index), value in route_stops_df.groupby(
                ["route_id", "trip_index"])["drive_minutes"].sum().items():
            try:
                out[f"{rid}#T{int(trip_index)}"] = float(value)
            except (ValueError, TypeError):
                pass
    return out


def build_vehicle_routes(selected_df: pd.DataFrame,
                         route_totals: dict | None = None,
                         route_stops: pd.DataFrame | None = None) -> pd.DataFrame:
    cols = ["route_id", "vehicle_id", "vehicle_home_depot", "is_tour",
            "service_date", "stops", "planned_km", "planned_drive_minutes"]
    if selected_df is None or selected_df.empty:
        return pd.DataFrame(columns=cols)
    g = selected_df.groupby("route_id", sort=True)
    out = g.agg(
        vehicle_id=("vehicle_id", "first"),
        vehicle_home_depot=("vehicle_home_depot", "first"),
        service_date=("service_date", "min"),
        stops=("job_id", "count"),
        planned_km=("planned_km", "sum"),
        planned_drive_minutes=("planned_drive_minutes", "sum"),
    ).reset_index()
    out["is_tour"] = out["route_id"].astype(str).str.startswith("TOUR:")
    # Prefer the COMMITTED per-stop geometry (route_stops) over the evaluator's
    # route_totals: the two agree for every well-formed route, but a stale start
    # coordinate on the seed epoch can inflate route_totals into a phantom
    # out-and-back (e.g. N88WTL 863 km for a 4 km Bedford-local delivery). The
    # committed geometry is what the map and plan_full actually draw, so it is the
    # authoritative distance. route_stops leg_km already includes the depot_return
    # leg, so it drops straight into the existing residual-return-drive machinery.
    drive_totals: dict[str, float] = {}
    if route_stops is not None and not route_stops.empty and "leg_km" in route_stops.columns:
        route_totals = (
            route_stops.groupby("route_id")["leg_km"].sum().astype(float).to_dict()
        )
        if "drive_minutes" in route_stops.columns:
            drive_totals = (
                route_stops.groupby("route_id")["drive_minutes"].sum().astype(float).to_dict()
            )
    if route_totals:
        # report the full route km (includes the return-to-depot leg) and add the
        # residual return drive time so route duration accounting stays consistent
        # with the distance accounting.
        full_km = []
        full_drive = []
        for rid, km, dm in zip(out["route_id"], out["planned_km"], out["planned_drive_minutes"]):
            total_km = float(route_totals.get(str(rid), km))
            kmf = float(km or 0.0)
            residual_km = max(0.0, total_km - kmf)
            # return leg at the route's effective pace (min/km); == residual/50*60 under
            # constant speed (bit-identical flag-off), OSRM pace under the flag.
            pace = (float(dm) / kmf) if (kmf > 0 and dm) else (60.0 / 50.0)
            full_km.append(total_km)
            full_drive.append(
                float(drive_totals[str(rid)])
                if str(rid) in drive_totals
                else float(dm or 0.0) + residual_km * pace
            )
        out["planned_km"] = full_km
        out["planned_drive_minutes"] = full_drive
    return out[cols]


ROUTE_STOP_COLUMNS = [
    "route_id", "vehicle_id", "vehicle_home_depot", "vehicle_type", "is_tour", "service_date",
    "trip_index", "sequence", "stop_type", "order_id", "leg_id", "node", "service_pc",
    "lat", "lon", "collect_lat", "collect_lon", "collect_pc",
    "planned_arrive", "planned_depart", "break_minutes_before", "drive_minutes", "leg_km",
    "load_pallets_after", "load_kg_after",
    # K2 audit ledger: the candidate's nominal (historical) due date, and how
    # many days before it this delivery is served ("" on non-delivery rows).
    "due_date", "days_early",
    # Soft delivery windows (2026-07-18): the customer's TIGHT deadline and minutes
    # the delivery arrived past it ("" on non-delivery rows; 0 = on time).
    "window_hardness", "deadline", "minutes_late",
]


def _opt(value):
    try:
        if value is None or value == "" or pd.isna(value):
            return np.nan
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan


def build_route_stops(
    selected_df: pd.DataFrame,
    candidate_df: pd.DataFrame,
    compatibility_df: pd.DataFrame,
    vehicle_df: pd.DataFrame,
    route_totals: dict | None = None,
    tour_return_dates: dict[str, str] | None = None,
    route_times: dict | None = None,
) -> pd.DataFrame:
    """Map-ready per-stop route table.

    For every route, emits the ordered sequence ``depot_start -> stops... ->
    depot_return`` with each stop's lat/lon, postcode, leg type, planned times,
    leg distance and load 鈥?everything a visualization needs to draw the route.
    Two-point legs (direct move / hub-drop) also carry the collection point
    (``collect_lat``/``collect_lon``).

    ``tour_return_dates`` maps route_id -> ISO date of the tour's final day; it
    is used for the synthesized ``depot_return`` row of multi-day tours so that
    row carries the date the vehicle actually gets back, not the tour's start
    date. If a route_id is absent, tours fall back to the max per-stop
    service_date in the group; daily routes keep the route's single date."""
    if selected_df is None or selected_df.empty:
        return pd.DataFrame(columns=ROUTE_STOP_COLUMNS)

    leg_coord: dict[str, tuple[float, float]] = {}
    if compatibility_df is not None and not compatibility_df.empty:
        comp = compatibility_df
        if "compatibility_status" in comp.columns:
            comp = comp[comp["compatibility_status"].astype(str).eq("OK")]
        for r in comp.itertuples(index=False):
            leg = str(getattr(r, "leg_id", ""))
            if leg and leg not in leg_coord:
                leg_coord[leg] = (_opt(getattr(r, "service_lat", None)),
                                  _opt(getattr(r, "service_lon", None)))

    cand = {str(c.get("leg_id")): c for c in candidate_df.to_dict("records")} \
        if candidate_df is not None and not candidate_df.empty else {}
    veh = {}
    vtype: dict[str, str] = {}
    if vehicle_df is not None and not vehicle_df.empty:
        for r in vehicle_df.itertuples(index=False):
            vid_key = str(getattr(r, "vehicle_id", ""))
            veh[vid_key] = (
                _opt(getattr(r, "current_lat", None)), _opt(getattr(r, "current_lon", None)))
            vtype[vid_key] = str(getattr(r, "vehicle_type", "") or "")

    rows: list[dict] = []
    group_cols = ["route_id", "trip_index"] if "trip_index" in selected_df.columns else ["route_id"]
    route_group_counts = selected_df.groupby("route_id")["trip_index"].nunique().to_dict() if "trip_index" in selected_df.columns else {}
    # Break entitlement belongs to the vehicle-day, not an individual depot
    # loop. Carry the replay state across trip groups so a break already placed
    # by the evaluator resets the same driving clock used for the return leg.
    dsb_by_vehicle_day: dict[tuple[str, str], float] = {}
    for key, grp in selected_df.groupby(group_cols, sort=True):
        if isinstance(key, tuple) and len(key) > 1:
            route_id, trip_index = str(key[0]), int(key[1] or 0)
        elif isinstance(key, tuple):
            route_id, trip_index = str(key[0]), 0
        else:
            route_id, trip_index = str(key), 0
        g = grp.sort_values("sequence")
        first = g.iloc[0]
        vid = str(first["vehicle_id"])
        home = str(first["vehicle_home_depot"])
        sdate = str(first["service_date"])
        is_tour = str(route_id).startswith("TOUR:")
        vlat, vlon = veh.get(vid, (np.nan, np.nan))

        def _row(**kw):
            base = dict(route_id=str(route_id), vehicle_id=vid, vehicle_home_depot=home,
                        vehicle_type=vtype.get(vid, ""),
                        is_tour=is_tour, service_date=sdate, trip_index=trip_index,
                        order_id="", leg_id="", node="", service_pc="",
                        lat=np.nan, lon=np.nan, collect_lat=np.nan, collect_lon=np.nan, collect_pc="",
                        planned_arrive="", planned_depart="", break_minutes_before=0.0,
                        drive_minutes=0.0, leg_km=0.0,
                        load_pallets_after=0.0, load_kg_after=0.0,
                        due_date="", days_early="", window_hardness="",
                        deadline="", minutes_late="")
            base.update(kw)
            rows.append(base)

        # Depot departure. A correctly-timed route_start already equals the first
        # committed stop's arrival minus its drive-in (+ pre-arrival break), so we trust
        # route_times by default. Two cases derive it from the geometry instead:
        #   * tours carry no route_times entry (original 2026-07-20 backfill), and
        #   * route_times is STALE — an endogenous option-swap stripped this trip's
        #     ORIGINAL leading stops after route_times was computed, leaving a departure
        #     hours before the first committed stop's drive-in could reach it (W88RNW:
        #     06:05 for a 15:44 first stop, an impossible ~9h depot idle). Re-anchor it.
        # Both require a real drive-in on the first stop; without one, route_times stands.
        _rt = (route_times or {}).get(f"{route_id}#T{trip_index}")
        dep_time = _rt[0] if _rt else ""
        _a = pd.to_datetime(str(first.get("planned_arrive", "") or ""), errors="coerce")
        _drive = float(first.get("planned_drive_minutes", 0.0) or 0.0)
        if pd.notna(_a) and _drive > 0.0:
            _derived = _a - pd.Timedelta(minutes=_drive + float(first.get("break_minutes_before", 0.0) or 0.0))
            _rt_dt = pd.to_datetime(dep_time, errors="coerce") if dep_time else None
            if not dep_time and is_tour:
                dep_time = str(_derived)
            elif _rt_dt is not None and _rt_dt < _derived - pd.Timedelta(minutes=1):
                dep_time = str(_derived)
        _row(sequence=0, stop_type="depot_start", node=home, lat=vlat, lon=vlon,
             planned_depart=dep_time)
        last_seq = 0
        leg_km_sum = 0.0
        # For TOURS, per-stop planned_km is not populated (0), so the whole tour distance
        # would otherwise dump onto the residual depot_return row (V888GNW: one 2,764 km
        # leg). Decompose each tour leg as real consecutive-stop geometry instead, tracking
        # the previous stop's coords (starting at the home depot). (decision-audit #3)
        prev_lat, prev_lon = vlat, vlon
        # EU 561/2006 drive-since-break accumulator, replayed over the day's legs so the
        # return leg can charge the break the evaluator does but the geometry-derived return
        # time dropped (audit #6). HGV only (vans exempt).
        _is_hgv = str(vtype.get(vid, "")).lower() in ("tractor", "rigid")
        dsb_key = (vid, sdate)
        dsb = float(dsb_by_vehicle_day.get(dsb_key, 0.0))
        has_explicit_return = bool(g["leg_kind"].astype(str).eq("DEPOT_RETURN").any())
        for s in g.itertuples(index=False):
            leg = str(getattr(s, "leg_id", ""))
            c = cand.get(leg, {})
            lat, lon = leg_coord.get(leg, (np.nan, np.nan))
            if (pd.isna(lat) or pd.isna(lon)) and c.get("service_lat") is not None:
                # synthetic legs (repaired DIRECTs) have no compatibility row;
                # their coords ride on the candidate dict
                lat, lon = _opt(c.get("service_lat")), _opt(c.get("service_lon"))
            if str(getattr(s, "leg_kind", "")) == "DEPOT_LOAD":  # no order leg -> depot anchor
                _anchor = DEPOT_ANCHORS.get(str(getattr(s, "destination_node", "")
                                                or getattr(s, "origin_node", "")))
                if _anchor:
                    lat, lon = _anchor
            if str(getattr(s, "leg_kind", "")) == "TOUR_OVERNIGHT":
                # mid-leg sleep point: coords self-carried in the node (OVERNIGHT@lat,lon)
                _n = str(getattr(s, "destination_node", "") or getattr(s, "origin_node", ""))
                if _n.startswith("OVERNIGHT@"):
                    try:
                        lat, lon = (float(x) for x in _n[len("OVERNIGHT@"):].split(","))
                    except ValueError:
                        pass
            if str(getattr(s, "leg_kind", "")) == "DEPOT_RETURN":
                lat, lon = vlat, vlon
            last_seq = int(getattr(s, "sequence", 0))
            stop_km = float(getattr(s, "planned_km", 0.0) or 0.0)
            if (is_tour and stop_km <= 1e-6 and pd.notna(lat) and pd.notna(lon)
                    and pd.notna(prev_lat) and pd.notna(prev_lon)):
                stop_km = road_km(float(prev_lat), float(prev_lon), float(lat), float(lon))
            if pd.notna(lat) and pd.notna(lon):
                prev_lat, prev_lon = lat, lon
            leg_km_sum += stop_km
            if _is_hgv:   # accumulate drive-since-break over the forward legs (audit #6)
                _, dsb = statutory_breaks(dsb, float(getattr(s, "planned_drive_minutes", 0.0) or 0.0))
            served_date = str(getattr(s, "service_date", "") or sdate)[:10]
            due = str(c.get("service_date", "") or "")[:10]
            days_early = ""
            deadline = ""
            minutes_late = ""
            window_hardness = ""
            _is_delivery = str(getattr(s, "leg_kind", "")) == "CUSTOMER_DELIVERY"
            if due and served_date and _is_delivery:
                try:  # K2 audit: how many days before the historical due date
                    days_early = (date.fromisoformat(due) - date.fromisoformat(served_date)).days
                except ValueError:
                    days_early = ""
            if _is_delivery:
                # Soft delivery windows (2026-07-18): minutes past the TIGHT customer
                # deadline (raw_window_end on the candidate). 0 = on time; "" when the
                # order carried no stated window.
                deadline = str(c.get("raw_window_end", "") or "")
                window_hardness = str(c.get("window_hardness", "") or "")
                arr = str(getattr(s, "planned_arrive", "") or "")
                if deadline and arr:
                    _a = pd.to_datetime(arr, errors="coerce")
                    _d = pd.to_datetime(deadline, errors="coerce")
                    if pd.notna(_a) and pd.notna(_d):
                        minutes_late = max(0.0, (_a - _d).total_seconds() / 60.0)
            _row(sequence=last_seq, stop_type=str(getattr(s, "leg_kind", "")).lower(),
                 service_date=str(getattr(s, "service_date", "") or sdate),
                 due_date=due, days_early=days_early,
                 window_hardness=window_hardness,
                 deadline=deadline, minutes_late=minutes_late,
                 order_id=str(getattr(s, "order_id", "")), leg_id=leg,
                 node=str(getattr(s, "destination_node", "") or getattr(s, "origin_node", "")),
                 service_pc=str(c.get("service_pc", "")), lat=lat, lon=lon,
                 collect_lat=_opt(c.get("origin_lat")), collect_lon=_opt(c.get("origin_lon")),
                 collect_pc=str(c.get("origin_pc", "") or ""),
                 planned_arrive=str(getattr(s, "planned_arrive", "")),
                 planned_depart=str(getattr(s, "planned_depart", "")),
                 break_minutes_before=float(getattr(s, "break_minutes_before", 0.0) or 0.0),
                 drive_minutes=float(getattr(s, "planned_drive_minutes", 0.0) or 0.0),
                 leg_km=stop_km,
                 load_pallets_after=float(getattr(s, "load_pallets_after", 0.0) or 0.0),
                 load_kg_after=float(getattr(s, "load_kg_after", 0.0) or 0.0))
        if has_explicit_return:
            if _is_hgv:
                dsb_by_vehicle_day[dsb_key] = dsb
            continue
        trip_key = f"{route_id}#T{trip_index}"
        # Daily route: the return-to-depot leg is REAL road geometry from the last
        # stop back to the home depot. Deriving it from the evaluator's route_totals
        # residual let a phantom evaluator total (seed epoch) inflate the return into
        # an impossible out-and-back (Feb-2 R888RNW: 11 km real return booked as
        # 562 km), which then polluted every downstream km report. Geometry keeps the
        # CSVs and markdown consistent and physically true. Tours keep the
        # route_totals residual: their multi-day distance is carried on this leg.
        ret_min = 0.0
        ret_break = 0.0
        ret_arrive = (_rt[1] if _rt else "")
        # Return leg = real geometry from the last stop back to the home depot, for BOTH
        # daily routes AND tours (2026-07-26): tours previously took the route_totals
        # residual (total - sum of the 0-km legs = the WHOLE tour on one row); now their
        # legs are decomposed geometrically above, so the return is just last->home like a
        # daily route. The residual survives only as a coords-missing fallback.
        if pd.notna(lat) and pd.notna(lon) and pd.notna(vlat) and pd.notna(vlon):
            ret_km = road_km(float(lat), float(lon), float(vlat), float(vlon))
            # Return TIME from the same emitted geometry as the km, NOT route_end.
            # route_end comes from improvement.solution, which still carries option
            # legs that drop_superseded_option_legs removed from the EMITTED plan
            # (endogenous DIRECT/XDOCK). A vehicle whose far DIRECT leg was superseded
            # keeps that leg's long return in route_end, so an 8 km emitted return
            # would be stamped hours late (Feb-2 FJ72XFF: 7 km return "arriving" 3h45m
            # after the last stop, which also inflated its duty via the depot span).
            # depart(last emitted stop) + drive(last -> depot) is self-consistent and
            # immune to the pre/post-drop divergence.
            ret_min = road_minutes(float(lat), float(lon), float(vlat), float(vlon),
                                   vtype.get(vid, ""))
            # EU break owed while driving the return home (audit #6): the evaluator charges
            # it (routing_adapter) but the geometry-derived return time dropped it, so a
            # >4.5h day showed 0 break on the runsheet and understated duty. HGV only.
            if _is_hgv:
                ret_break, dsb = statutory_breaks(dsb, ret_min)
                dsb_by_vehicle_day[dsb_key] = dsb
            _last_dep = pd.to_datetime(str(getattr(s, "planned_depart", "") or ""), errors="coerce")
            if pd.notna(_last_dep):
                ret_arrive = str(_last_dep + pd.Timedelta(minutes=ret_min + ret_break))
        else:
            # Tours: multi-day distance/time is carried on this leg via route_totals /
            # route_end (their return is not a single same-day geometry leg).
            if route_totals and trip_key in route_totals:
                total_km = float(route_totals[trip_key])
            elif route_totals and route_id in route_totals and (not route_group_counts or int(route_group_counts.get(route_id, 1)) == 1):
                total_km = float(route_totals[route_id])
            else:
                total_km = leg_km_sum
            ret_km = total_km - leg_km_sum
        return_date = ((tour_return_dates or {}).get(str(route_id))
                       or (str(g["service_date"].astype(str).max()) if is_tour else sdate))
        _row(sequence=last_seq + 1, stop_type="depot_return", node=home, lat=vlat, lon=vlon,
             service_date=return_date, leg_km=max(0.0, ret_km),
             drive_minutes=ret_min, break_minutes_before=ret_break, planned_arrive=ret_arrive)

    return pd.DataFrame(rows, columns=ROUTE_STOP_COLUMNS)


def build_depot_inventory_timeline(selected_df: pd.DataFrame,
                                   candidate_df: pd.DataFrame) -> pd.DataFrame:
    cols = ["depot", "date", "freight_in", "freight_out", "net", "running_balance"]
    if selected_df is None or selected_df.empty:
        return pd.DataFrame(columns=cols)

    depots = {}
    if candidate_df is not None and not candidate_df.empty:
        for c in candidate_df.to_dict("records"):
            depots[str(c.get("leg_id"))] = (str(c.get("source_depot", "")), str(c.get("target_depot", "")))

    events: dict[tuple[str, str], list[int]] = {}  # (depot, date) -> [in, out]
    for r in selected_df.itertuples(index=False):
        leg_kind = str(getattr(r, "leg_kind", ""))
        date = str(getattr(r, "service_date", ""))
        src, tgt = depots.get(str(getattr(r, "leg_id", "")), ("", ""))
        if leg_kind == "CUSTOMER_PICKUP" and tgt:
            events.setdefault((tgt, date), [0, 0])[0] += 1
        elif leg_kind == "CUSTOMER_DELIVERY" and src:
            events.setdefault((src, date), [0, 0])[1] += 1

    rows = [{"depot": d, "date": dt, "freight_in": io[0], "freight_out": io[1], "net": io[0] - io[1]}
            for (d, dt), io in events.items()]
    df = pd.DataFrame(rows, columns=["depot", "date", "freight_in", "freight_out", "net"])
    if df.empty:
        return pd.DataFrame(columns=cols)
    df = df.sort_values(["depot", "date"]).reset_index(drop=True)
    df["running_balance"] = df.groupby("depot")["net"].cumsum()
    return df[cols]
