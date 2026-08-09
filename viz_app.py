"""Rich custom-Leaflet HTML map for the freight_planner pipeline.

A self-contained .html (open in any browser, no server) built off a run's
period-scoped ``plan/`` folder. Improves on the old cambridge plan-vs-actual map:

  * **Trip-focus** — click a trip (map or list) and every other trip fades to a
    low opacity, so the selected one stands out (not just a bounding box).
  * **Rich sidebar** — per-trip card with the new pipeline's data (drive- and
    capacity-utilisation, km, stop/job counts, freight-state transitions), a run
    summary, and an unassigned-with-reasons panel.
  * **Filters** — day, vehicle search, trip type (direct/crossdock/tour), and a
    drive-utilisation band.
  * **Plan-vs-actual** (mode ``compare``) — telematics overlay with actual
    engine-off stops matched to planned destinations [added in a later stage].

Coordinates come straight from ``route_stops.csv`` (no geocoding). Routes are
OSRM road-snapped (``--no-osrm`` for straight lines).
"""
from __future__ import annotations

import argparse
import json
import re
from functools import lru_cache
from pathlib import Path

import pandas as pd

from freight_planner.shared.config import DEPOT_ANCHORS
from freight_planner import geocode
from freight_planner.output_layout import RunPaths, artifact_dir
from freight_planner.paths import DEFAULT_POSTCODE_CACHE
from freight_planner.shared.routing import DEFAULT_OSRM_URL, get_route_geometry

# B37 7HB (Palletline national hub) -- same constant as freight_planner.tour_plan.B37_LATLON,
# duplicated here so viz_app doesn't need to import tour_plan (which pulls in the whole
# multiday-tour orchestration module just for one coordinate pair).
B37_LATLON = (52.4666, -1.7226)
# LE10 3BS (hazchem trunk hub) -- same value as freight_planner.tour_plan.LE10_LATLON,
# duplicated here for the same reason as B37_LATLON above.
LE10_LATLON = (52.540784, -1.413558)
_HUB_LATLON = {"B37_HUB": B37_LATLON, "LE10_HUB": LE10_LATLON}

_PALETTE = [
    "#4a9eff", "#2ca02c", "#e6724b", "#b07cff", "#f0c419", "#16c0c0", "#ff6fae",
    "#9acd32", "#e07b39", "#5f9e6e", "#c0504d", "#8064a2",
    "#4bacc6", "#f79646", "#7f9a3a", "#d16ba5",
]
_DIRECT_KINDS = {"direct_customer_move"}
_CUSTOMER_KINDS = {"customer_delivery", "customer_pickup", *_DIRECT_KINDS}


def _vehicle_color(vehicle_id: str, order: dict[str, int]) -> str:
    return _PALETTE[order.setdefault(vehicle_id, len(order)) % len(_PALETTE)]


def _has(lat, lon) -> bool:
    return pd.notna(lat) and pd.notna(lon)


def _f(v, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return None
    return round(f, 2)


def _txt(v) -> str:
    return "" if v is None or (isinstance(v, float) and v != v) else str(v)


def _trip_waypoints(rows: pd.DataFrame) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for r in rows.itertuples(index=False):
        if str(getattr(r, "stop_type", "")) in _DIRECT_KINDS and _has(getattr(r, "collect_lat", None), getattr(r, "collect_lon", None)):
            pts.append((float(r.collect_lat), float(r.collect_lon)))
        if _has(getattr(r, "lat", None), getattr(r, "lon", None)):
            pts.append((float(r.lat), float(r.lon)))
    return pts


def _geometry(waypoints, use_osrm, osrm_url):
    if use_osrm and len(waypoints) >= 2:
        snapped = get_route_geometry(waypoints, osrm_url)
        if snapped:
            return snapped
    return [[la, lo] for la, lo in waypoints]


def _trip_type(stop_types: set[str], is_tour: bool) -> str:
    """Coarse label for a trip's stop mix. NOTE: "MIXED" (does both pickups and
    deliveries) is deliberately *not* called "XDOCK" — XDOCK is a freight-routing
    term (collect-to-depot / deliver-from-depot, FULL_FLEET only) and has nothing to
    do with a single trip's stop composition. PL_EXPORT collections that ride a
    delivery trip land in MIXED; they are collected then trunked, not crossdocked."""
    if is_tour:
        return "TOUR"
    if stop_types & _DIRECT_KINDS:
        return "DIRECT"
    if {"customer_pickup", "customer_delivery"} <= stop_types:
        return "MIXED"
    if "customer_pickup" in stop_types:
        return "COLLECT"
    return "DELIVER"


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


def _parse_kpi(plan_dir: Path) -> dict:
    """Pull the run-level assignment numbers from kpi_summary.md (authoritative)."""
    p = plan_dir / "02_kpi_summary.md"
    if not p.exists():                       # pre-2026-07-16 runs
        p = plan_dir / "kpi_summary.md"
    out: dict = {}
    if not p.exists():
        return out
    import re
    txt = p.read_text(encoding="utf-8")
    m = re.search(r"in-universe orders \(planning denominator\):\s*([\d,]+)", txt)
    if m:
        out["in_universe"] = int(m.group(1).replace(",", ""))
    m = re.search(r"assigned orders:\s*([\d,]+)\s*\(([\d.]+)%", txt)
    if m:
        out["assigned"] = int(m.group(1).replace(",", ""))
        out["assignment_rate"] = float(m.group(2))
    m = re.search(r"planned km:\s*([\d,]+(?:\.\d+)?)", txt)
    if m:
        out["plan_km"] = float(m.group(1).replace(",", ""))
    return out


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _space_pc(compact: str) -> str:
    """Re-insert the single UK postcode space: 'AL109BS' -> 'AL10 9BS'. An
    outward-only or non-unit string is returned unchanged."""
    c = str(compact or "").strip().upper().replace(" ", "")
    if len(c) >= 5 and c[-3].isdigit() and c[-2:].isalpha():
        return f"{c[:-3]} {c[-3:]}"
    return c


def _geocode_recovery(pc, cache: dict) -> dict | None:
    """Classify how a postcode resolved, from its cache entry. Returns a recovery
    descriptor {method, risk, resolved_to} for a repaired / outcode / terminated
    resolution, else None (exact unit, legacy no-source entry, miss, or empty)."""
    if not pc:
        return None
    entry = cache.get(geocode.postcode_key(pc))
    if not isinstance(entry, dict):
        return None
    source = str(entry.get("source", ""))
    if "repaired" in source:
        m = re.search(r"repaired ([A-Z0-9]+)", source)
        return {"method": "repaired", "risk": "high",
                "resolved_to": _space_pc(m.group(1)) if m else "?"}
    if str(entry.get("precision", "")) == "outcode_district" or "outcodes" in source:
        oc = str(entry.get("postcode", "") or geocode.postcode_key(pc))
        return {"method": "outcode", "risk": "high", "resolved_to": f"{oc} district centroid"}
    if "terminated" in source:
        return {"method": "terminated", "risk": "low",
                "resolved_to": str(entry.get("postcode", "") or pc)}
    return None


def _collect_recoveries(cust_df, cache: dict, name_map: dict) -> list[dict]:
    """Whole-plan list of stops whose postcode was recovered. Scans each customer
    stop's service_pc and (two-point) collect_pc, deduped by (order_id, raw_pc),
    sorted high-risk first. Each entry carries order/name/raw/method/resolved_to/
    risk/lat/lon so the operator can eyeball the fix."""
    seen: set = set()
    out: list[dict] = []
    for r in cust_df.itertuples(index=False):
        oid = str(getattr(r, "order_id", "") or "")
        pairs = (
            (getattr(r, "service_pc", None), getattr(r, "lat", None), getattr(r, "lon", None)),
            (getattr(r, "collect_pc", None), getattr(r, "collect_lat", None), getattr(r, "collect_lon", None)),
        )
        for pc, lat, lon in pairs:
            pc = str(pc or "").strip()
            if not pc or pc.lower() == "nan":
                continue
            key = (oid, pc.upper())
            if key in seen:
                continue
            rec = _geocode_recovery(pc, cache)
            if rec is None:
                continue
            seen.add(key)
            out.append({"order_id": oid, "order_name": name_map.get(oid, ""),
                        "raw_pc": pc, "lat": _f(lat), "lon": _f(lon), **rec})
    out.sort(key=lambda x: (x["risk"] != "high", x["method"], x["order_id"]))
    return out


def _planned_duty_hours(df: pd.DataFrame) -> dict:
    """Planned on-duty hours per vehicle over the frame's days: last depot_return
    arrive − first depot_start depart, summed across that vehicle's days."""
    if df.empty or "stop_type" not in df.columns:
        return {}
    starts = df[df["stop_type"] == "depot_start"]
    ends = df[df["stop_type"] == "depot_return"]
    out: dict[str, float] = {}
    key = ["vehicle_id", "service_date"] if "service_date" in df.columns else ["vehicle_id"]
    s = (starts.assign(_t=pd.to_datetime(starts["planned_depart"], errors="coerce"))
         .dropna(subset=["_t"]).groupby(key)["_t"].min())
    e = (ends.assign(_t=pd.to_datetime(ends["planned_arrive"], errors="coerce"))
         .dropna(subset=["_t"]).groupby(key)["_t"].max())
    for k in s.index.intersection(e.index):
        vid = str(k[0] if isinstance(k, tuple) else k)
        hours = (e[k] - s[k]) / pd.Timedelta(hours=1)
        if hours > 0:
            out[vid] = out.get(vid, 0.0) + float(hours)
    return out


def _build_validation(df: pd.DataFrame, day: str, actuals: dict, fleet_active: int, metrics: dict,
                      run_meta: dict | None = None, actuals_duty: dict | None = None) -> dict:
    """Validation panel over the days in view — the WHOLE window by default, a single
    day when --date filtered ``df``. Planned and actual sides always share that scope.

    ``actuals`` = {vehicle_id: actual_km summed over the days in view};
    ``fleet_active`` = actual vehicle-DAYS (fleet vehicles moving >=1 km, summed per day);
    ``metrics`` = parsed validation_metrics.json (may be empty);
    ``run_meta`` = parsed run_manifest.json (time budget / seed; may be empty)."""
    planned = df.groupby("vehicle_id")["leg_km"].sum() if not df.empty else pd.Series(dtype=float)
    per_vehicle, no_tel = [], []
    fleet_actual = 0.0
    planned_have, planned_no = 0.0, 0.0   # split planned by telematics availability for a fair gap
    for v, pk in planned.items():
        vid = str(v)
        a = actuals.get(vid)
        per_vehicle.append({"vehicle": vid, "planned_km": round(float(pk), 1),
                            "actual_km": (round(float(a), 1) if a is not None else None)})
        if a is not None:
            fleet_actual += float(a)
            planned_have += float(pk)
        else:
            no_tel.append(vid)
            planned_no += float(pk)
    planned_duty = _planned_duty_hours(df)
    ad = actuals_duty or {}
    _matched_duty = [v for v in planned_duty if v in ad]
    fleet_planned_duty = round(sum(planned_duty[v] for v in _matched_duty), 1) if _matched_duty else None
    fleet_actual_duty = round(sum(ad[v] for v in _matched_duty), 1) if _matched_duty else None
    return {
        "day": day,
        "fleet": {
            # planned_km is the COMPARABLE figure: only vehicles that also have telematics,
            # so it lines up with actual_km. Planned km on telematics-dark vehicles is split out.
            "planned_km": round(planned_have, 1),
            "planned_km_no_telematics": round(planned_no, 1),
            "actual_km": round(fleet_actual, 1),
            # true vehicle-DAYS in the days in view: distinct (vehicle, service_date)
            # pairs of the plan view, so it compares with the per-day actual count
            # (frames without a date column fall back to the distinct-vehicle count)
            "planned_veh_days": (int(df[["vehicle_id", "service_date"]].drop_duplicates().shape[0])
                                 if "service_date" in df.columns else len(planned)),
            "actual_veh_days": int(fleet_active),  # fleet vehicles moving >=1 km, summed per day
            "no_telematics": no_tel,
            "planned_duty_h": fleet_planned_duty,
            "actual_duty_h": fleet_actual_duty,
        },
        "optimizer": {**{k: metrics.get(k) for k in ("committed_km", "seed_km", "alns_km", "seed_cost", "alns_cost", "moves")},
                      "time_budget_s": (run_meta or {}).get("time_budget_s"),
                      "seed": (run_meta or {}).get("seed")},
        "per_vehicle": per_vehicle,
    }


def _stop_coverage(df: pd.DataFrame, visited: set) -> dict:
    """Fleet-level postcode coverage: of the outward codes we planned to stop at, how
    many did the fleet actually stop in (telematics)? Vehicle-agnostic by design —
    matched at outward-code granularity (the robust common form). ``visited`` is the
    set of outward codes from ``vehicle_actuals.visited_postcodes``."""
    from freight_planner.vehicle_actuals import normalize_pc
    cust = df[df["stop_type"].isin(_CUSTOMER_KINDS)] if "stop_type" in df.columns else df
    pcs = cust["service_pc"] if "service_pc" in cust.columns else pd.Series(dtype=str)
    planned = {normalize_pc(p) for p in pcs.dropna()}
    planned.discard("")
    matched = planned & visited
    return {
        "planned_districts": len(planned),
        "visited_by_fleet": len(matched),
        "coverage_pct": round(100 * len(matched) / len(planned), 1) if planned else None,
        "missed": sorted(planned - visited)[:50],
    }


def _read_shuttle_job_ids(plan_dir: Path) -> set[str]:
    df = _read_csv(plan_dir / "shuttle_jobs.csv")
    return set(df["job_id"].astype(str)) if not df.empty else set()


def _hub_short(hub: str) -> str:
    """"B37_HUB" -> "B37", "LE10_HUB" -> "LE10" (tooltip label)."""
    return hub[:-4] if hub.endswith("_HUB") else hub


def _build_trunk(plan_dir: Path, *, use_osrm: bool, osrm_url: str) -> dict:
    """Aggregate trunk_schedule.csv (one row per depot-night-hub) into a
    per-(depot, hub) summary for the sidebar/legend, plus a road-snapped
    geometry per depot -> hub leg (computed once here, shipped in the payload
    -- same pattern as trip geometries, so the client never has to call OSRM
    itself). Each entry uses its OWN hub's latlon (B37 vs LE10) -- a CB22 row
    can appear twice, once per hub, with different destinations and totals.
    Empty dict (falsy) when the schedule file is absent or has no rows --
    callers render nothing. Older trunk_schedule.csv files with no `hub`
    column are handled gracefully: every row defaults to "B37_HUB"."""
    df = _read_csv(plan_dir / "trunk_schedule.csv")
    if df.empty:
        return {}
    if "hub" not in df.columns:
        df = df.copy()
        df["hub"] = "B37_HUB"
    else:
        df = df.copy()
        df["hub"] = df["hub"].fillna("B37_HUB").replace("", "B37_HUB")
    depots = []
    total_km = 0.0
    total_trips = 0
    for (depot, hub), g in df.groupby(["depot", "hub"], sort=True):
        trips = int(g["trips"].sum())
        km = float(g["km"].sum())
        nights = int(len(g))
        total_km += km
        total_trips += trips
        anchor = DEPOT_ANCHORS.get(str(depot))
        hub_latlon = _HUB_LATLON.get(str(hub), B37_LATLON)
        lat, lon = (anchor if anchor is not None else (None, None))
        geom = _geometry([(lat, lon), hub_latlon], use_osrm, osrm_url) if anchor is not None else []
        depots.append({
            "depot": str(depot), "hub": str(hub), "hub_short": _hub_short(str(hub)),
            "lat": lat, "lon": lon,
            "hub_lat": hub_latlon[0], "hub_lon": hub_latlon[1],
            "trips": trips, "km": round(km, 1), "nights": nights,
            "geom": geom,
        })
    return {
        "depots": depots,
        "total_km": round(total_km, 1),
        "total_trips": total_trips,
    }


@lru_cache(maxsize=1)
def _order_name_map() -> dict:
    """order_id -> human order name (e.g. 'WT253752'), from the qargo order files, so
    trip stops show the readable name instead of the UUID. Robust to missing files."""
    from freight_planner.paths import DEFAULT_FEB_QARGO, DEFAULT_QARGO
    out: dict = {}
    for p in (DEFAULT_QARGO, DEFAULT_FEB_QARGO):
        try:
            q = pd.read_parquet(p, columns=["order_id", "name"])
        except Exception:
            continue
        for oid, nm in zip(q["order_id"].astype(str), q["name"].astype(str)):
            if oid:
                out[oid] = nm
    return out


def build_plan_data(
    plan_dir: Path | str,
    *,
    service_date: str | None = None,
    use_osrm: bool = True,
    osrm_url: str = DEFAULT_OSRM_URL,
    with_actuals: bool = False,
) -> dict:
    # accepts a current-layout WINDOW dir (joins route via csv/md/root) or a
    # legacy plan/ folder (plain Path — joins hit the files directly)
    plan_dir = artifact_dir(Path(plan_dir))
    df = pd.read_csv(plan_dir / "route_stops.csv")
    all_stops = df   # full plan (recoveries are whole-plan, not date-filtered)
    name_map = _order_name_map()
    if service_date:
        df = df[df["service_date"].astype(str) == service_date]
    if df.empty:
        raise ValueError(f"no route stops match date={service_date!r}")

    actuals: dict = {}
    actuals_duty: dict = {}
    per_day_actuals: dict[str, dict] = {}
    fleet_active: int = 0
    visited: set = set()
    # nominal window from the run manifest: the scorecard compares plan vs actual on
    # exactly these days — tour tails spill onto later days where the fleet is already
    # doing the NEXT window's work, so tail days must stay out of the fleet comparison
    run_meta = _read_json(plan_dir.parent / "run_manifest.json")
    _all_days = sorted(str(x)[:10] for x in df["service_date"].astype(str).unique())
    win_lo = str(run_meta.get("start") or (_all_days[0] if _all_days else ""))[:10]
    win_hi = str(run_meta.get("end") or (_all_days[-1] if _all_days else ""))[:10]
    if with_actuals:
        from datetime import date as _date
        from freight_planner import vehicle_actuals
        from freight_planner.vehicles import vehicle_states_frame
        # load telematics for EVERY plan day (trip popups stay day-correct on tail days)
        # but accumulate the fleet comparison over nominal-window days only;
        # telematics-dark days contribute zero
        for vday in _all_days:
            d = _date.fromisoformat(vday)
            try:
                day_km = vehicle_actuals.actual_km_by_vehicle(d, prefer_odometer=True)  # true odometer km
                day_visited = vehicle_actuals.visited_postcodes(d)
            except Exception:
                day_km, day_visited = {}, set()
            per_day_actuals[vday] = day_km
            if not (win_lo <= vday <= win_hi):
                continue
            visited |= day_visited
            # actual vehicle-DAYS: fleet vehicles that moved >=1 km that day (fleet-wide,
            # excludes non-fleet assets and is independent of which vehicles we planned)
            fleet_ids = set(vehicle_states_frame(d)["vehicle_id"].astype(str))
            fleet_active += len({v for v, km in day_km.items() if km >= 1.0} & fleet_ids)
            for v, km in day_km.items():
                actuals[v] = actuals.get(v, 0.0) + float(km)
            try:
                day_duty = vehicle_actuals.actual_duty_by_vehicle(d)
            except Exception:
                day_duty = {}
            for v, h in day_duty.items():
                actuals_duty[v] = actuals_duty.get(v, 0.0) + float(h)

    veh_util = _read_csv(plan_dir / "vehicle_day_utilization.csv")
    trip_util = _read_csv(plan_dir / "trip_capacity_utilization.csv")
    vu = {str(r.route_id): r for r in veh_util.itertuples(index=False)} if not veh_util.empty else {}
    tu = {(str(r.route_id), int(r.trip_index)): r for r in trip_util.itertuples(index=False)} if not trip_util.empty else {}
    shuttle_ids = _read_shuttle_job_ids(plan_dir)

    color_order: dict[str, int] = {}
    trips: list[dict] = []
    for (rid, tidx), g in df.groupby(["route_id", "trip_index"], sort=True):
        g = g.sort_values("sequence")
        wpts = _trip_waypoints(g)
        if len(wpts) < 2:
            continue
        vid = str(g["vehicle_id"].iloc[0])
        sdate = str(g["service_date"].iloc[0])
        is_tour = bool(g["is_tour"].iloc[0]) if "is_tour" in g.columns else False
        stop_types = set(g["stop_type"].astype(str))
        # include the cross-depot load-stop so it draws a marker at the depot it collects from
        cust = g[g["stop_type"].isin({*_CUSTOMER_KINDS, "depot_load"})]
        # One route_stops row can be TWO physical stops: a two-point DIRECT/HUB-DROP
        # move drives depot -> COLLECT -> DELIVER, so emit the collection point as its
        # own visible stop (marker + list row) instead of hiding it behind the
        # delivery postcode. The drive distance belongs to reaching the collection.
        stops = []
        n = 0
        for r in cust.itertuples(index=False):
            two_point = str(r.stop_type) in _DIRECT_KINDS and _has(getattr(r, "collect_lat", None), getattr(r, "collect_lon", None))
            if two_point:
                n += 1
                stops.append({
                    "n": n, "seq": int(r.sequence), "type": str(r.stop_type), "part": "collect",
                    "order_id": str(getattr(r, "order_id", "") or ""),
                    "order_name": name_map.get(str(getattr(r, "order_id", "") or ""), ""),
                    "pc": str(getattr(r, "collect_pc", "") or ""),
                    "lat": _f(r.collect_lat), "lon": _f(r.collect_lon),
                    "arrive": "", "depart": "",
                    "leg_km": _f(getattr(r, "leg_km", 0.0)),  # depot/prev -> collection drive
                    "pal_after": None, "kg_after": None,
                    "fs_before": "", "fs_after": "",
                })
            n += 1
            stops.append({
                "n": n, "seq": int(r.sequence), "type": str(r.stop_type),
                "part": ("deliver" if two_point else ""),
                "order_id": _txt(getattr(r, "order_id", "")),
                "order_name": name_map.get(_txt(getattr(r, "order_id", "")), ""),
                "pc": _txt(getattr(r, "service_pc", "")) or _txt(getattr(r, "node", "")),
                "lat": _f(r.lat), "lon": _f(r.lon),
                "arrive": str(getattr(r, "planned_arrive", "") or "")[:16],
                "depart": str(getattr(r, "planned_depart", "") or "")[:16],
                "leg_km": (0.0 if two_point else _f(getattr(r, "leg_km", 0.0))),
                "pal_after": _f(getattr(r, "load_pallets_after", 0.0)),
                "kg_after": _f(getattr(r, "load_kg_after", 0.0)),
                "fs_before": str(getattr(r, "freight_state_before", "") or ""),
                "fs_after": str(getattr(r, "freight_state_after", "") or ""),
            })
        vr = vu.get(str(rid))
        tr = tu.get((str(rid), int(tidx)))
        depot_row = g[g["stop_type"] == "depot_start"]
        depot = (float(depot_row["lat"].iloc[0]), float(depot_row["lon"].iloc[0])) if not depot_row.empty and _has(depot_row["lat"].iloc[0], depot_row["lon"].iloc[0]) else None
        trip_leg_ids = {f"JOB:{lid}" for lid in cust["leg_id"].dropna().astype(str)} if "leg_id" in cust.columns else set()
        is_shuttle = bool(shuttle_ids & trip_leg_ids)
        trips.append({
            "id": f"{rid}#T{int(tidx)}", "vehicle": vid, "date": sdate,
            # the vehicle's telematics km on THIS trip's day (not the window total)
            "actual_km": (round(float(per_day_actuals[sdate[:10]][vid]), 1)
                          if vid in per_day_actuals.get(sdate[:10], {}) else None),
            "trip_index": int(tidx), "is_tour": is_tour,
            "shuttle": is_shuttle,
            "depot": str(g["vehicle_home_depot"].iloc[0]) if "vehicle_home_depot" in g.columns else "",
            "depot_ll": depot,
            "color": _vehicle_color(vid, color_order),
            "vtype": (str(g["vehicle_type"].iloc[0]).lower()
                      if "vehicle_type" in g.columns else ""),
            "type": _trip_type(stop_types, is_tour),
            "geom": _geometry(wpts, use_osrm, osrm_url),
            "stops": stops,
            "km": _f(float(g["leg_km"].fillna(0).sum())),
            "n_stops": len(stops),
            "n_jobs": int(getattr(vr, "job_count", len(stops))) if vr is not None else len(stops),
            "drive_util": _f(getattr(vr, "drive_utilization_pct", 0.0)) if vr is not None else None,
            "pal_util": _f(getattr(tr, "pallet_utilization_pct", 0.0)) if tr is not None else None,
            "kg_util": _f(getattr(tr, "kg_utilization_pct", 0.0)) if tr is not None else None,
            "peak_pal": _f(getattr(tr, "peak_pallets", 0.0)) if tr is not None else None,
            "cap_pal": _f(getattr(tr, "capacity_pallets", 0.0)) if tr is not None else None,
        })

    depots = df[df["stop_type"].isin(["depot_start", "depot_return"])].drop_duplicates(subset=["node", "lat", "lon"])
    depot_list = [{"name": str(r.node), "lat": _f(r.lat), "lon": _f(r.lon)}
                  for r in depots.itertuples(index=False) if _has(r.lat, r.lon)]

    unassigned_df = _read_csv(plan_dir / "unassigned_jobs.csv")
    _cache = geocode.load_cache(DEFAULT_POSTCODE_CACHE)
    # A row here is a LEG that was not planned — but its ORDER may still be served
    # (BEFORE_PLANNING_START prestaged deliveries, REPAIRED_DIRECT supersessions).
    # Tag those so the panel separates true misses from accounting rows.
    served_orders = set(df["order_id"].dropna().astype(str))
    unassigned = []
    for r in (unassigned_df.itertuples(index=False) if not unassigned_df.empty else []):
        pc = str(getattr(r, "service_pc", "") or "")
        ll = geocode.coords(pc, _cache)
        oid = str(getattr(r, "order_id", "") or "")
        unassigned.append({
            "order_id": oid,
            "leg_kind": str(getattr(r, "leg_kind", "") or ""),
            "pc": pc,
            "depot": str(getattr(r, "source_depot", "") or ""),
            "reason": str(getattr(r, "reason", "") or ""),
            "served": oid in served_orders,
            "lat": _f(ll[0]) if ll else None,
            "lon": _f(ll[1]) if ll else None,
        })
    unassigned.sort(key=lambda u: (u["served"], u["reason"], u["order_id"]))

    _cust_all = (all_stops[all_stops["stop_type"].astype(str).str.contains("CUSTOMER|DIRECT", case=False, na=False)]
                 if "stop_type" in all_stops.columns else all_stops.iloc[0:0])
    recoveries = _collect_recoveries(_cust_all, _cache, name_map)

    dates = sorted(df["service_date"].astype(str).unique().tolist())
    kpi = _parse_kpi(plan_dir)
    trunk = _build_trunk(plan_dir, use_osrm=use_osrm, osrm_url=osrm_url)
    val_metrics = _read_json(plan_dir / "validation_metrics.json")
    # trunk_km: validation_metrics.json is authoritative (it's the run's own accounting);
    # fall back to the schedule sum so the scorecard still shows something if that
    # file is stale/missing but trunk_schedule.csv is present (e.g. a hand-built copy).
    trunk_km = float(val_metrics["trunk_km"]) if "trunk_km" in val_metrics else float(trunk.get("total_km", 0.0) or 0.0)
    plan_km = float(kpi.get("plan_km") or 0.0)
    summary = {
        "trips": len(trips),
        "vehicles": int(df["vehicle_id"].nunique()),
        "stops": int(df[df["stop_type"].isin(_CUSTOMER_KINDS)].shape[0]),
        "km": _f(float(df["leg_km"].fillna(0).sum())),
        "tours": sum(1 for t in trips if t["is_tour"]),
        "unassigned": sum(1 for u in unassigned if not u["served"]),
        "accounting_legs": sum(1 for u in unassigned if u["served"]),
        "recoveries": {"high": sum(1 for r in recoveries if r["risk"] == "high"),
                       "low": sum(1 for r in recoveries if r["risk"] != "high")},
        "dates": dates,
        # run-level (window-wide) assignment, from the official KPI report
        "in_universe": kpi.get("in_universe"),
        "assigned": kpi.get("assigned"),
        "assignment_rate": kpi.get("assignment_rate"),
        # combined-km scorecard: optimizer plan km + fixed nightly trunk km
        "plan_km": _f(plan_km),
        "trunk_km": _f(trunk_km),
        "combined_km": _f(plan_km + trunk_km),
    }
    validation = None
    if with_actuals:
        if service_date:
            win_df, spill_km = df, 0.0
            day_label = str(service_date)
        else:
            win_mask = df["service_date"].astype(str).str[:10].between(win_lo, win_hi)
            win_df = df[win_mask]
            spill_km = float(df.loc[~win_mask, "leg_km"].fillna(0).sum())
            day_label = f"{win_lo} → {win_hi} · {win_df['service_date'].nunique()} days"
        validation = _build_validation(win_df, day_label, actuals, fleet_active, val_metrics,
                                        run_meta, actuals_duty=actuals_duty)
        validation["fleet"]["spill_km"] = round(spill_km, 1)
        # trunk km belongs on the planned side of the fleet gap: the nightly trunk is
        # real mandated driving and its artics' odometer sits in the actual side
        validation["fleet"]["trunk_km"] = round(trunk_km, 1)
        validation["coverage"] = _stop_coverage(win_df, visited)

    return {
        "window": plan_dir.parent.name,
        "service_date": service_date or "",
        "dates": dates,
        "trips": trips,
        "depots": depot_list,
        "unassigned": unassigned,
        "recoveries": recoveries,
        "summary": summary,
        "validation": validation,
        "trunk": trunk,
        "mode": "trips",
    }


_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{height:100vh;font-family:system-ui,-apple-system,sans-serif;font-size:13px;background:#10131c;color:#dde}
#app{display:flex;height:100vh;overflow:hidden}
#left{width:300px;flex-shrink:0;background:#161a28;display:flex;flex-direction:column;overflow:hidden;box-shadow:2px 0 8px rgba(0,0,0,.5);z-index:500}
#right{width:250px;flex-shrink:0;background:#161a28;display:flex;flex-direction:column;overflow:hidden;box-shadow:-2px 0 8px rgba(0,0,0,.5);z-index:500}
#map{flex:1}
.hd{padding:12px 14px;border-bottom:1px solid #28304a}
.hd h2{font-size:14px;color:#fff}.hd .sub{font-size:11px;color:#7180a8;margin-top:2px}
.sec{padding:10px 14px;border-bottom:1px solid #232a40;overflow-y:auto}
.lbl{font-size:9px;color:#6878a0;text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px}
.row{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #1d2336;font-size:12px}
.row:last-child{border-bottom:none}.row .k{color:#9aa6c8}.row .v{color:#fff;font-weight:600;font-variant-numeric:tabular-nums}
.scroll{flex:1;overflow-y:auto}
.tcard{display:none}
.tcard.on{display:block}
.bar{height:6px;border-radius:3px;background:#262e44;overflow:hidden;margin-top:2px}
.bar > i{display:block;height:100%;border-radius:3px}
.chips{display:flex;flex-wrap:wrap;gap:4px}
.chip{padding:3px 8px;border-radius:11px;font-size:10px;background:#222a40;color:#9aa6c8;border:1px solid #2c3550;cursor:pointer}
.chip.on{background:#4a9eff;color:#fff;border-color:#4a9eff}
input.search{width:100%;padding:5px 7px;background:#10131c;border:1px solid #2c3550;border-radius:4px;color:#dde;font-size:11px;margin-bottom:6px;outline:none}
.trow{display:flex;align-items:center;gap:7px;padding:5px 4px;border-bottom:1px solid #1d2336;cursor:pointer;border-radius:3px}
.trow:hover{background:#1d2438}.trow.sel{background:#1a3a60}
.dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.tname{font-size:11px;color:#cdd;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.tmeta{font-size:9px;color:#7888b0;flex-shrink:0}
.btn{padding:5px 8px;border:none;border-radius:4px;background:#222a40;color:#bcc;cursor:pointer;font-size:11px}
.btn:hover{background:#2c3760;color:#fff}.btn.wide{width:100%}
.ua{padding:5px 4px;border-bottom:1px solid #1d2336;font-size:10px}
.ua .o{color:#e9b;font-weight:600}.ua .r{color:#e67e22}
.ua.acc{opacity:.5}.ua.acc .r{color:#6a9955}
.badge{background:#1d3a2a;color:#2ecc71;border-radius:3px;padding:0 4px;font-size:9px}
.shuttle-pill{background:#3a2a10;color:#f39c12;border-radius:9px;padding:1px 6px;font-size:9px;font-weight:600;letter-spacing:.3px}
#scorecard{font-size:10px;color:#9aa6c8;padding:2px 14px 8px}
#scorecard b{color:#dde}
.stbl{width:100%;border-collapse:collapse;font-size:10px;margin-top:4px}
.stbl td{padding:2px 4px;border-bottom:1px solid #1d2336;color:#bcc}
.stbl .t{color:#7888b0}
.leaflet-popup-content{font-size:12px;line-height:1.5}
.muted{color:#6878a0;font-size:10px}
#legend{position:absolute;left:312px;bottom:16px;z-index:600;background:rgba(22,26,40,.92);border:1px solid #2c3550;border-radius:6px;padding:9px 11px;box-shadow:0 2px 10px rgba(0,0,0,.5)}
#legend .lg{display:flex;align-items:center;gap:6px;font-size:10px;color:#cdd;padding:1px 0}
#legend .sh{display:inline-flex;align-items:center;justify-content:center;width:13px;height:13px;background:rgba(16,19,28,.85);border:2px solid #9aa6c8;flex-shrink:0}
#legend .sh.num{background:#9aa6c8;border-color:#fff;border-radius:50%;color:#fff;font-size:9px;font-weight:700}
</style></head><body>
<div id="app">
 <div id="left">
  <div class="hd"><h2 id="title">Trips</h2><div class="sub" id="subtitle"></div></div>
  <div id="scorecard" style="display:none"></div>
  <div class="sec"><div class="lbl">Run summary</div><div id="summary"></div></div>
  <div class="sec" id="val-sec" style="display:none"><div class="lbl">Validation · planned vs actual</div><div id="validation"></div></div>
  <div class="sec scroll" id="trip-card-sec"><div class="lbl">Selected trip <span id="clear" class="muted" style="float:right;cursor:pointer">clear ✕</span></div>
    <div id="trip-card"><div class="muted">Click a trip on the map or list to inspect it. Click again to deselect.</div></div>
  </div>
  <div class="sec" id="ua-sec" style="max-height:200px"><div class="lbl">Unassigned <span id="ua-count" class="muted" style="float:right"></span></div>
    <div id="unassigned" class="scroll" style="max-height:150px"></div></div>
  <div class="sec" id="rec-sec" style="max-height:200px"><div class="lbl">Postcode recoveries <span id="rec-count" class="muted" style="float:right"></span></div>
    <div id="recoveries" class="scroll" style="max-height:150px"></div></div>
 </div>
 <div id="map"></div>
 <div id="legend">
   <div class="lbl" style="margin-bottom:5px">Legend</div>
   <div class="lg"><span class="sh" style="border-radius:50%"></span>Delivery</div>
   <div class="lg"><span class="sh" style="transform:rotate(45deg);border-radius:1px"></span>Collection / pickup</div>
   <div class="lg"><span class="sh" style="border-radius:2px"></span>Direct move</div>
   <div class="lg"><span class="sh" style="background:#ffd34d;border-color:#fff;border-radius:2px"></span>Depot</div>
   <div class="lg"><span class="sh" style="background:#10131c;border:2px solid #ffd34d;border-radius:2px"></span>Cross-depot load-stop</div>
   <div class="lg"><span class="sh num">3</span>Stop visit order (selected trip)</div>
   <div class="lg"><span style="display:inline-block;width:16px;height:0;border-top:3px solid #4a9eff;margin-right:6px;vertical-align:middle"></span>Trip route · click to focus</div>
   <div class="lg" id="legend-trunk" style="display:none"><span style="display:inline-block;width:16px;height:0;border-top:3px dashed #f39c12;margin-right:6px;vertical-align:middle"></span>Nightly trunk · depot → B37 hub</div>
 </div>
 <div id="right">
  <div class="hd"><div class="lbl" style="margin:0">Filters</div></div>
  <div class="sec" id="day-sec" style="display:none"><div class="lbl">Day</div><div id="days" class="chips"></div></div>
  <div class="sec"><div class="lbl">Vehicle</div><input id="q" class="search" placeholder="search reg…">
    <div class="lbl" style="margin-top:6px">Postcode pin</div><input id="pcq" class="search" placeholder="find a postcode… e.g. G52 / KA1">
    <div class="lbl">Trip type</div><div id="types" class="chips"></div>
    <div class="lbl" style="margin-top:8px">Drive utilisation</div><div id="util" class="chips"></div>
    <div class="lbl" style="margin-top:8px">Layers</div>
    <div class="chips">
      <span class="chip on" id="chip-trunk">Trunk</span>
      <span class="chip" id="chip-shuttle">Shuttles only</span>
    </div>
    <div class="lbl" style="margin-top:8px">Colour by</div>
    <div class="chips">
      <span class="chip on" id="cm-vehicle" onclick="colorMode='vehicle';applyColorMode()">Vehicle</span>
      <span class="chip" id="cm-type" onclick="colorMode='type';applyColorMode()">Type</span>
    </div>
    <div class="chips" id="legend-type" style="display:none;margin-top:4px">
      <span class="chip" style="border-color:#e74c3c;color:#e74c3c">tractor</span>
      <span class="chip" style="border-color:#4a9eff;color:#4a9eff">rigid</span>
      <span class="chip" style="border-color:#2ecc71;color:#2ecc71">van</span>
    </div>
    <div style="display:flex;gap:5px;margin-top:8px"><button class="btn" style="flex:1" id="f-all">All</button><button class="btn" style="flex:1" id="f-none">None</button></div>
  </div>
  <div class="sec scroll" style="flex:1"><div class="lbl">Trips <span id="trip-count" class="muted" style="float:right"></span></div><div id="trip-list"></div></div>
 </div>
</div>
<script>
const DATA = __DATA__;
const ACTIVE=0.92, DIM=0.05, W_BASE=3, W_SEL=5;
const map = L.map('map',{preferCanvas:false}).setView([52.4,-0.6],8);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  {attribution:'© OpenStreetMap, © CARTO',maxZoom:19,subdomains:'abcd'}).addTo(map);

document.getElementById('title').textContent = DATA.mode==='compare'?'Plan vs Actual':'Planned trips';
document.getElementById('subtitle').textContent = DATA.window + (DATA.service_date?` · ${DATA.service_date}`:'');

const allBounds = [];

// depots
DATA.depots.forEach(d=>L.marker([d.lat,d.lon],{icon:L.divIcon({className:'',html:'<div style="width:13px;height:13px;background:#ffd34d;border:2px solid #fff;border-radius:3px;box-shadow:0 1px 4px #000"></div>',iconSize:[13,13],iconAnchor:[6,6]})}).bindTooltip('Depot '+d.name).addTo(map));

// ── nightly trunk (T1 B37 hub + T2 LE10 hazchem hub fixed services) ──
const trunkLayers = [];
let trunkOn = true;
(function(){
  const tr = DATA.trunk;
  if(!tr || !tr.depots || !tr.depots.length) return;
  document.getElementById('legend-trunk').style.display = 'flex';
  tr.depots.forEach(d=>{
    if(!d.geom || d.geom.length<2) return;
    const line = L.polyline(d.geom, {color:'#f39c12', weight:5, opacity:0.85, dashArray:'10,8'}).addTo(map);
    const hubShort = d.hub_short || 'B37';
    line.bindTooltip(`TRUNK ${d.depot}→${hubShort}: ${d.trips} trips / ${d.km.toLocaleString()} km`, {permanent:true, direction:'center', className:'', opacity:0.9});
    trunkLayers.push(line);
    d.geom.forEach(p=>allBounds.push(p));
  });
})();
function applyTrunkVisibility(){ trunkLayers.forEach(l=>{ const el=l.getElement&&l.getElement(); if(el) el.style.display = trunkOn?'':'none'; l.setStyle({opacity: trunkOn?0.85:0}); const tt=l.getTooltip&&l.getTooltip(); if(tt){ const te=tt.getElement&&tt.getElement(); if(te) te.style.display = trunkOn?'':'none'; } }); }
document.getElementById('chip-trunk').onclick=function(){ trunkOn=!trunkOn; this.classList.toggle('on',trunkOn); applyTrunkVisibility(); };

// ── trips ──
const tripLayers = {};   // id -> {line, markers, t}
const TYPE_COLORS={tractor:'#e74c3c',rigid:'#4a9eff',van:'#2ecc71'};
let colorMode='vehicle';
// grey = unknown/absent vtype (e.g. plans generated before the vehicle_type column)
function colorOf(t){ return colorMode==='type' ? (TYPE_COLORS[t.vtype]||'#8892a8') : t.color; }
function applyColorMode(){
  for(const Lr of Object.values(tripLayers)){
    const c=colorOf(Lr.t);
    Lr.line.setStyle({color:c});
    Lr.markers.forEach(m=>{ m._color=c; });
  }
  document.getElementById('legend-type').style.display = colorMode==='type'?'flex':'none';
  document.getElementById('cm-vehicle').classList.toggle('on',colorMode==='vehicle');
  document.getElementById('cm-type').classList.toggle('on',colorMode==='type');
  applySelection();   // re-icons markers via _normalMarkers/_selectMarkers with the new _color
  renderList();        // rebuild trip-list dots with the new colour
}
// shape by leg kind: circle = delivery, diamond = collection/pickup, square = direct move.
// A two-point DIRECT move's COLLECT half draws as a collection (diamond), DELIVER half as a square.
function _isCollect(s){ return s.part==='collect' || s.type==='customer_pickup'; }
function _isLoad(s){ return s.type==='depot_load'; }   // cross-depot load-stop at a depot
function _shapeCss(s){
  if(_isLoad(s)) return 'border-radius:2px';           // square, like a depot
  if(_isCollect(s)) return 'transform:rotate(45deg);border-radius:1px';
  if(s.type==='direct_customer_move') return 'border-radius:2px';
  return 'border-radius:50%';
}
function stopIconHollow(s,color){   // default: small hollow shape, no number
  const col=_isLoad(s)?'#ffd34d':color;   // depot-yellow for a load-stop
  return L.divIcon({className:'',html:`<div style="width:11px;height:11px;background:rgba(16,19,28,.85);border:2px solid ${col};${_shapeCss(s)}"></div>`,iconSize:[13,13],iconAnchor:[6.5,6.5]});
}
function stopIconNumbered(s,color){  // when its trip is selected: filled + visit-order number
  const rot = _isCollect(s), col=_isLoad(s)?'#ffd34d':color;
  return L.divIcon({className:'',html:`<div style="width:20px;height:20px;background:${col};border:2px solid #fff;${_shapeCss(s)};box-shadow:0 1px 5px rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center"><span style="color:${_isLoad(s)?'#111':'#fff'};font-size:11px;font-weight:700;line-height:1;${rot?'transform:rotate(-45deg)':''}">${s.n||''}</span></div>`,iconSize:[20,20],iconAnchor:[10,10]});
}
let lastTripClick=0;   // suppress the map-click-clear that bubbles after a layer click
DATA.trips.forEach(t=>{
  const line = L.polyline(t.geom,{color:t.color,weight:W_BASE,opacity:ACTIVE}).addTo(map);
  line.on('click',()=>{lastTripClick=Date.now();toggleTrip(t.id,true);});
  const markers = t.stops.filter(s=>s.lat&&s.lon).map(s=>{
    const m = L.marker([s.lat,s.lon],{icon:stopIconHollow(s,t.color)}).addTo(map);
    m._s=s; m._color=t.color;
    m.bindPopup(`<b>#${s.n} ${t.vehicle}</b> · ${t.id.split('#').pop()}`+(s.order_name?` · <b>${s.order_name}</b>`:'')+`<br><span class=muted>${s.type}${s.part?' · '+s.part:''}</span><br>${s.pc||'—'}<br>arr ${s.arrive||'—'} · dep ${s.depart||'—'}<br>leg ${s.leg_km} km · on board ${s.pal_after} pal / ${s.kg_after} kg`+(s.fs_after?`<br>state → ${s.fs_after}`:''));
    m.on('click',()=>{lastTripClick=Date.now();toggleTrip(t.id,true);});
    return m;
  });
  tripLayers[t.id]={line,markers,t};
  t.geom.forEach(p=>allBounds.push(p));
});
if(allBounds.length) map.fitBounds(allBounds,{padding:[30,30]});

// ── postcode pin: type a postcode (full or outward) to drop a pin on the map ──
const _OW=/^[A-Z]{1,2}[0-9][A-Z0-9]?/;
const _pcIndex=(function(){
  const m={};
  const add=(pc,lat,lon)=>{ if(!pc||!lat||!lon) return; const k=String(pc).toUpperCase().replace(/\s+/g,''); if(!(k in m)) m[k]=[lat,lon]; const ow=k.match(_OW); if(ow&&!(ow[0] in m)) m[ow[0]]=[lat,lon]; };
  DATA.trips.forEach(t=>t.stops.forEach(s=>add(s.pc,s.lat,s.lon)));
  (DATA.depots||[]).forEach(d=>add(d.name,d.lat,d.lon));
  (DATA.unassigned||[]).filter(u=>!u.served).forEach(u=>add(u.pc,u.lat,u.lon));
  return m;
})();
let _pcPin=null;
document.getElementById('pcq').addEventListener('input',function(e){
  if(_pcPin){map.removeLayer(_pcPin);_pcPin=null;}
  const q=String(e.target.value||'').toUpperCase().replace(/\s+/g,'');
  if(!q) return;
  let ll=_pcIndex[q];
  if(!ll){ const ow=q.match(_OW); if(ow) ll=_pcIndex[ow[0]]; }
  if(!ll){ const k=Object.keys(_pcIndex).find(x=>x.startsWith(q)); if(k) ll=_pcIndex[k]; }
  if(!ll) return;
  _pcPin=L.marker(ll,{icon:L.divIcon({className:'',html:'<div style="font-size:24px;line-height:1;filter:drop-shadow(0 1px 2px #000)">📍</div>',iconSize:[24,24],iconAnchor:[12,24]})}).addTo(map)
        .bindTooltip('postcode '+e.target.value.toUpperCase(),{permanent:true,direction:'top',offset:[0,-20]}).openTooltip();
  map.setView(ll,Math.max(map.getZoom(),9));
});

// ── selection / focus-dimming (single-select, mutually exclusive) ──
const selected = new Set();
function _normalMarkers(Lr){ Lr.markers.forEach(m=>{ m.setIcon(stopIconHollow(m._s,m._color)); m.setOpacity(0.95); }); }
function _selectMarkers(Lr){ Lr.markers.forEach(m=>{ m.setIcon(stopIconNumbered(m._s,m._color)); m.setOpacity(1); }); }
function _hideMarkers(Lr){ Lr.markers.forEach(m=>m.setOpacity(0)); }
function toggleTrip(id,zoom){
  // mutually exclusive: clicking a trip selects only it; clicking it again clears.
  if(selected.has(id)) selected.clear(); else { selected.clear(); selected.add(id); }
  applySelection();
  if(zoom&&selected.has(id)) map.fitBounds(tripLayers[id].line.getBounds(),{padding:[50,50],maxZoom:12});
}
function applySelection(){
  const anySel = selected.size>0;
  for(const [id,Lr] of Object.entries(tripLayers)){
    if(!visible(id)){ Lr.line.setStyle({opacity:0}); _hideMarkers(Lr); continue; }
    if(!anySel){ Lr.line.setStyle({opacity:ACTIVE,weight:W_BASE}); _normalMarkers(Lr); }
    else if(selected.has(id)){ Lr.line.setStyle({opacity:1,weight:W_SEL}); _selectMarkers(Lr); Lr.line.bringToFront(); }
    else { Lr.line.setStyle({opacity:DIM,weight:W_BASE}); _hideMarkers(Lr); }  // unselected: faint line, stops hidden
  }
  document.querySelectorAll('.trow').forEach(r=>r.classList.toggle('sel',selected.has(r.dataset.id)));
  renderCard();
}
document.getElementById('clear').onclick=()=>{selected.clear();applySelection();};
map.on('click',()=>{ if(Date.now()-lastTripClick<60) return; if(selected.size){selected.clear();applySelection();} });

// ── selected-trip card ──
function bar(pct,color){ if(pct==null) return '<span class=muted>—</span>'; const c=pct>=90?'#e74c3c':pct>=70?'#f1c40f':'#2ecc71'; return `${pct}%<div class=bar><i style="width:${Math.min(100,pct)}%;background:${color||c}"></i></div>`; }
function renderCard(){
  const el=document.getElementById('trip-card');
  const ids=[...selected];
  if(ids.length!==1){ el.innerHTML = ids.length>1?`<div class=muted>${ids.length} trips selected. Select one to see details.</div>`:'<div class="muted">Click a trip on the map or list to inspect it. Click again to deselect.</div>'; return; }
  const t=tripLayers[ids[0]].t;
  const stops = t.stops.map(s=>{let ty=s.type.replace('customer_','').replace('_customer_move','').replace('depot_load','load @');if(s.part)ty+=' '+s.part;return `<tr><td class=t>${s.n}</td><td>${ty}</td><td>${s.order_name||'—'}</td><td>${s.pc||'—'}</td><td>${s.leg_km}km</td></tr>`;}).join('');
  el.innerHTML=`
   <div class="row"><span class=k>Trip</span><span class=v style="font-size:10px">${t.id.split(':').slice(1).join(':')}${t.shuttle?' <span class="shuttle-pill">SHUTTLE</span>':''}</span></div>
   <div class="row"><span class=k>Vehicle · type</span><span class=v><span class=dot style="display:inline-block;background:${colorOf(t)}"></span> ${t.vehicle} · ${t.type}</span></div>
   <div class="row"><span class=k>Date</span><span class=v>${t.date}</span></div>
   <div class="row"><span class=k>Distance</span><span class=v>${t.km} km</span></div>
   ${t.actual_km!=null?`<div class="row"><span class=k>Actual km <span class=muted>(veh-day)</span></span><span class=v>${t.actual_km} km</span></div>`:''}
   <div class="row"><span class=k>Stops · jobs</span><span class=v>${t.n_stops} · ${t.n_jobs}</span></div>
   <div class="row"><span class=k>Drive util</span><span class=v>${bar(t.drive_util)}</span></div>
   <div class="row"><span class=k>Pallet util</span><span class=v>${bar(t.pal_util)}</span></div>
   <div class="row"><span class=k>Weight util</span><span class=v>${bar(t.kg_util)}</span></div>
   <table class=stbl><tr><td class=t>#</td><td class=t>type</td><td class=t>order</td><td class=t>postcode</td><td class=t>leg</td></tr>${stops}</table>`;
}

// ── summary + unassigned ──
(function(){ const s=DATA.summary;
  const ar = s.assignment_rate;
  const arColor = ar==null?'#9aa6c8':ar>=95?'#2ecc71':ar>=85?'#f1c40f':'#e74c3c';
  const arRow = ar!=null ? `<div class=row><span class=k>Assignment rate <span class=muted>(run)</span></span><span class=v style="color:${arColor};font-size:14px">${ar}%</span></div>
   <div class=row><span class=k>Assigned / in-universe</span><span class=v>${(s.assigned||0).toLocaleString()} / ${(s.in_universe||0).toLocaleString()}</span></div>` : '';
  document.getElementById('summary').innerHTML=
  arRow +
  `<div class=row><span class=k>Trips · vehicles</span><span class=v>${s.trips} · ${s.vehicles}</span></div>
   <div class=row><span class=k>Stops</span><span class=v>${s.stops}</span></div>
   <div class=row><span class=k>Planned km</span><span class=v>${s.km.toLocaleString()}</span></div>
   <div class=row><span class=k>Multiday tours</span><span class=v>${s.tours}</span></div>
   <div class=row><span class=k>Missed orders <span class=muted>(run)</span></span><span class=v style="color:${s.unassigned? '#e67e22':'#2ecc71'}">${s.unassigned}</span></div>
   <div class=row><span class=k>Unplanned legs <span class=muted>(order served)</span></span><span class=v style="color:#9aa6c8">${s.accounting_legs||0}</span></div>
   <div class=row><span class=k>Postcode recoveries <span class=muted>(verify)</span></span><span class=v style="color:${s.recoveries&&s.recoveries.high?'#e67e22':'#9aa6c8'}">${s.recoveries?s.recoveries.high:0}${s.recoveries&&s.recoveries.low?' (+'+s.recoveries.low+')':''}</span></div>`;
  const ua=document.getElementById('unassigned');
  const miss=(DATA.unassigned||[]).filter(u=>!u.served), acc=(DATA.unassigned||[]).filter(u=>u.served);
  document.getElementById('ua-count').textContent = miss.length + (acc.length? ` (+${acc.length} served)` : '');
  const row=u=>`<div class="ua${u.served?' acc':''}"><span class=o>${(u.order_id||'').slice(0,8)}</span> · ${u.pc||'—'} <span class=muted>${u.leg_kind}</span>${u.served?' <span class=badge>order served</span>':''}<br><span class=r>${u.reason}</span></div>`;
  ua.innerHTML = (miss.length+acc.length)? miss.slice(0,200).map(row).join('')
      + (acc.length? `<div class=muted style="padding:5px 4px;border-bottom:1px solid #1d2336">— legs below belong to orders that ARE served (prestaged / repaired) —</div>` + acc.slice(0,200).map(row).join('') : '')
      : '<div class=muted>None</div>';
  // combined-km scorecard: plan + fixed nightly trunk. Only shown when there's a
  // trunk contribution to report (trunk_km>0) -- otherwise it's a redundant
  // restatement of "Planned km" above.
  if(s.trunk_km>0){
    const sc=document.getElementById('scorecard');
    sc.style.display='';
    sc.innerHTML=`plan <b>${s.plan_km.toLocaleString()}</b> + trunk <b>${s.trunk_km.toLocaleString()}</b> = <b>${s.combined_km.toLocaleString()}</b> km`;
  }
})();

// ── postcode recoveries (verify geocode fixes) ──
(function(){ const recs=DATA.recoveries||[];
  const hi=recs.filter(r=>r.risk==='high'), lo=recs.filter(r=>r.risk!=='high');
  document.getElementById('rec-count').textContent = hi.length + (lo.length?` (+${lo.length} retired)`:'');
  const col={outcode:'#e74c3c',repaired:'#e67e22',terminated:'#6878a0'};
  const row=r=>`<div class="ua"><span class=o>${(r.order_id||'').slice(0,8)}</span> <span class=badge style="background:${col[r.method]||'#6878a0'}">${r.method}</span> ${r.order_name||''}<br><span class=r>${r.raw_pc} → ${r.resolved_to}</span></div>`;
  const el=document.getElementById('recoveries');
  el.innerHTML = (hi.length+lo.length)
     ? (hi.length?`<div class=muted style="padding:3px 4px">— needs check —</div>`+hi.map(row).join(''):'')
       + (lo.length?`<div class=muted style="padding:5px 4px;border-top:1px solid #1d2336">— retired units (real coords, low risk) —</div>`+lo.map(row).join(''):'')
     : '<div class=muted>None</div>';
})();

// ── validation scorecard (planned vs actual + seed→ALNS) ──
(function(){ const v=DATA.validation; if(!v) return;
  document.getElementById('val-sec').style.display='';
  const f=v.fleet, o=v.optimizer;
  const plannedC=(f.planned_km||0)+(f.trunk_km||0);
  const pct=(plannedC&&f.actual_km!=null)?Math.round((f.actual_km-plannedC)/plannedC*100):null;
  const R=(k,val)=>`<div class=row><span class=k>${k}</span><span class=v>${val}</span></div>`;
  let h=R('Window', v.day||'—')
    + R('Planned km <span class=muted>(matched veh)</span>', (f.planned_km||0).toLocaleString())
    + (f.trunk_km?R('+ trunk km <span class=muted>(nightly service)</span>', f.trunk_km.toLocaleString()):'')
    + R('Actual km <span class=muted>(odometer)</span>', f.actual_km!=null?f.actual_km.toLocaleString():'—')
    + R('Δ plan+trunk→actual', pct!=null?`<span style="color:${pct>0?'#e67e22':'#2ecc71'}">${pct>0?'+':''}${pct}%</span>`:'—')
    + R('Vehicle-days <span class=muted>plan/actual</span>', `${f.planned_veh_days} / ${f.actual_veh_days}`)
    + ((f.planned_duty_h!=null||f.actual_duty_h!=null)
        ? R('Duty hours <span class=muted>plan/actual</span>',
            `${f.planned_duty_h!=null?f.planned_duty_h.toLocaleString():'—'} / ${f.actual_duty_h!=null?f.actual_duty_h.toLocaleString():'—'}`)
        : '');
  h+=R('Planned km <span class=muted>(no telematics)</span>', (f.planned_km_no_telematics||0).toLocaleString());
  if(f.no_telematics&&f.no_telematics.length) h+=`<div class=muted style="padding:2px 0 0">${f.no_telematics.length} veh w/o telematics — excluded from Δ</div>`;
  if(f.spill_km) h+=`<div class=muted style="padding:2px 0 0">${f.spill_km.toLocaleString()} km planned on tail days beyond the window — excluded from Δ</div>`;
  h+=`<div class=muted style="padding:2px 0 0">odometer includes non-order fleet movement — Δ is fleet-level context, not the matched gap</div>`;
  const c=v.coverage;
  if(c&&c.coverage_pct!=null){ const cc=c.coverage_pct>=90?'#2ecc71':c.coverage_pct>=75?'#f1c40f':'#e74c3c';
    h+=R('Stop coverage <span class=muted>(postcode)</span>', `<span style="color:${cc}">${c.coverage_pct}%</span>`)
     + `<div class=muted style="padding:2px 0 0">${c.visited_by_fleet}/${c.planned_districts} planned districts visited by fleet</div>`;
  }
  if(o&&o.seed_km!=null){ h+=`<div class=lbl style="margin-top:9px">Optimizer · seed→ALNS <span class=muted>(objective, search-space)</span></div>`
    + (o.committed_km!=null?R('committed plan km', `<b>${o.committed_km.toLocaleString()}</b> <span class=muted>· matches KPI</span>`):'')
    + R('objective km', `${o.seed_km.toLocaleString()} → ${o.alns_km.toLocaleString()}`)
    + R('cost £', `${o.seed_cost.toLocaleString()} → ${o.alns_cost.toLocaleString()}`)
    + R('moves', o.moves);
    if(o.time_budget_s!=null) h+=R('time budget', `${o.time_budget_s}s`+(o.seed!=null?` <span class=muted>· seed ${o.seed}</span>`:'')); }
  document.getElementById('validation').innerHTML=h;
})();

// ── filters ──
const TYPES=[...new Set(DATA.trips.map(t=>t.type))].sort();
const filt={date:null,q:'',types:new Set(TYPES),util:null,shuttleOnly:false};
function visible(id){ const t=tripLayers[id].t;
  if(filt.date && t.date!==filt.date) return false;
  if(filt.q && !t.vehicle.toLowerCase().includes(filt.q)) return false;
  if(!filt.types.has(t.type)) return false;
  if(filt.util){ const u=t.drive_util||0; if(filt.util==='lo'&&u>=50)return false; if(filt.util==='mid'&&(u<50||u>80))return false; if(filt.util==='hi'&&u<=80)return false; }
  if(filt.shuttleOnly && !t.shuttle) return false;
  return true;
}
function applyFilters(){ for(const id of selected) if(!visible(id)) selected.delete(id); applySelection(); renderList(); }
// day chips
if(DATA.dates.length>1){ document.getElementById('day-sec').style.display='';
  const dc=document.getElementById('days'); ['all',...DATA.dates].forEach(d=>{ const c=document.createElement('span'); c.className='chip'+(d==='all'?' on':''); c.textContent=d==='all'?'all':d.slice(5); c.onclick=()=>{filt.date=d==='all'?null:d; dc.querySelectorAll('.chip').forEach(x=>x.classList.remove('on')); c.classList.add('on'); applyFilters();}; dc.appendChild(c); }); }
// type chips
const tc=document.getElementById('types'); TYPES.forEach(ty=>{ const c=document.createElement('span'); c.className='chip on'; c.textContent=ty; c.onclick=()=>{ if(filt.types.has(ty)){filt.types.delete(ty);c.classList.remove('on');}else{filt.types.add(ty);c.classList.add('on');} applyFilters(); }; tc.appendChild(c); });
// util chips
const uc=document.getElementById('util'); [['lo','<50%'],['mid','50-80%'],['hi','>80%']].forEach(([k,lab])=>{ const c=document.createElement('span'); c.className='chip'; c.textContent=lab; c.onclick=()=>{ filt.util=filt.util===k?null:k; uc.querySelectorAll('.chip').forEach(x=>x.classList.remove('on')); if(filt.util)c.classList.add('on'); applyFilters(); }; uc.appendChild(c); });
document.getElementById('q').oninput=e=>{filt.q=e.target.value.trim().toLowerCase();applyFilters();};
document.getElementById('f-all').onclick=()=>{filt.types=new Set(TYPES);tc.querySelectorAll('.chip').forEach(x=>x.classList.add('on'));applyFilters();};
document.getElementById('f-none').onclick=()=>{filt.types.clear();tc.querySelectorAll('.chip').forEach(x=>x.classList.remove('on'));applyFilters();};
document.getElementById('chip-shuttle').onclick=function(){ filt.shuttleOnly=!filt.shuttleOnly; this.classList.toggle('on',filt.shuttleOnly); applyFilters(); };

// ── trip list ──
function renderList(){ const el=document.getElementById('trip-list'); const vis=DATA.trips.filter(t=>visible(t.id));
  document.getElementById('trip-count').textContent=`${vis.length}/${DATA.trips.length}`;
  el.innerHTML=''; vis.forEach(t=>{ const r=document.createElement('div'); r.className='trow'+(selected.has(t.id)?' sel':''); r.dataset.id=t.id;
    r.innerHTML=`<span class=dot style="background:${colorOf(t)}"></span><span class=tname title="${t.id}">${t.vehicle} <span class=tmeta>${t.type}</span>${t.shuttle?' <span class="shuttle-pill">S</span>':''}</span><span class=tmeta>${t.km}km · ${t.drive_util!=null?t.drive_util+'%':''}</span>`;
    r.onclick=()=>toggleTrip(t.id,true);
    r.onmouseenter=()=>{ if(!selected.size && visible(t.id)){ tripLayers[t.id].line.setStyle({weight:W_SEL}); tripLayers[t.id].line.bringToFront(); } };
    r.onmouseleave=()=>{ if(!selected.size) tripLayers[t.id].line.setStyle({weight:W_BASE}); };
    el.appendChild(r); });
}
renderList(); applySelection();
</script></body></html>"""


def render_html(data: dict, out_html: Path) -> Path:
    title = ("Plan vs Actual" if data.get("mode") == "compare" else "Planned trips") + " — " + data.get("window", "")
    html = _HTML.replace("__TITLE__", title).replace("__DATA__", json.dumps(data))
    out_html = Path(out_html)
    out_html.write_text(html, encoding="utf-8")
    return out_html


def build_app(
    plan_dir: Path | str,
    out_html: Path | str | None = None,
    *,
    service_date: str | None = None,
    use_osrm: bool = True,
    osrm_url: str = DEFAULT_OSRM_URL,
    with_actuals: bool = False,
) -> Path:
    plan_dir = artifact_dir(Path(plan_dir))
    data = build_plan_data(plan_dir, service_date=service_date, use_osrm=use_osrm,
                           osrm_url=osrm_url, with_actuals=with_actuals)
    if out_html is None:
        suffix = f"_{service_date}" if service_date else ""
        suffix += "_validate" if with_actuals else ""
        if isinstance(plan_dir, RunPaths):
            out_html = plan_dir / f"trip_app{suffix}.html"   # html -> run root
        else:
            reports = Path(plan_dir).parent / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            out_html = reports / f"trip_app{suffix}.html"
    out = render_html(data, out_html)
    print(f"trip app: {len(data['trips'])} trips, {len(data['unassigned'])} unassigned -> {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Rich custom-Leaflet trip map for the freight_planner pipeline.")
    p.add_argument("--plan-dir", required=True, help="a run's plan/ folder (has route_stops.csv)")
    p.add_argument("--date", default=None, help="filter to one service_date")
    p.add_argument("--out", default=None)
    p.add_argument("--no-osrm", action="store_true", help="straight lines instead of OSRM road-snapping")
    p.add_argument("--osrm-url", default=DEFAULT_OSRM_URL)
    p.add_argument("--validate", action="store_true",
                   help="overlay actual telematics km + seed->ALNS delta (single-day validation scorecard)")
    args = p.parse_args(argv)
    build_app(args.plan_dir, args.out, service_date=args.date, use_osrm=not args.no_osrm,
              osrm_url=args.osrm_url, with_actuals=args.validate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
