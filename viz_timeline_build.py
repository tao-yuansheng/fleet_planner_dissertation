"""Preprocess a dynamic run into the compact MULTI-DAY snapshot JSON the timeline
app embeds. Part B (spec 2026-07-12-per-epoch-plan-snapshots-design.md).

The run now persists the LIVE plan at every decision epoch (reports/plan_snapshots.csv,
Part A). This builder turns that into, per day: a static ``jobs`` lookup (attributes
that don't change across epochs — postcode, coords, type, name, booked) plus, per
vehicle, one compact stop array **per epoch** ``[jobIdx, arrive, depart, committed,
trip]``. The app then renders the plan AS IT STOOD at any clock T (the snapshot from
the last epoch <= T): the 00:00 midnight seed plan shows continuous, the noon re-opt visibly
reshuffles the uncommitted tail, and committed stops only ever delay. Days are paged
left/right. Times are minutes-from-midnight.

Depot legs are exact: each trip carries its recorded route_start (depot departure,
jobIdx -2) and route_end (depot return arrival, jobIdx -1) from the snapshot rows.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from freight_planner.output_layout import RunPaths
from freight_planner.shared.config import DEPOT_ANCHORS
from freight_planner.viz_geometry import route_pairs, bake

T0, T1 = 0, 1440            # 00:00 .. 24:00 in minutes (full day, so overnight trunks fit)
DELTA_R1 = 90               # commit-freeze lead (kept for the frontier band)

# Trunk timing model (trunk_schedule.csv carries only dates + pallet/trip/km totals; times
# are synthesized). Anchored on the user's operating times + telematics: outbound leaves the
# depot 19:30 once the day's collections are consolidated (hub arrival ~20:45 in the GPS
# traces), return arrives the depot 04:30. A Stoke DAY-trunk is a same-day out-and-back.
_TRUNK_OUT_DEPART = 19.5 * 60      # 19:30 depot departure (overnight outbound)
_TRUNK_RET_ARRIVE = 4.5 * 60       # 04:30 depot return arrival (overnight return, next morning)
_TRUNK_DAY_DEPART = 14.0 * 60      # 14:00 same-day departure (Stoke day-trunk)
_TRUNK_HANDOVER = 30.0             # hub turnaround before the return leg
_TRUNK_SPEED_KMH = 60.0            # HGV motorway avg for the depot<->hub leg
_TRUNK_CONSOLIDATE = 30.0          # depot time to consolidate after the last collection returns

_TYPE = {"customer_pickup": "pickup", "customer_delivery": "delivery",
         "direct_customer_move": "direct", "depot_start": "depot_start",
         "depot_return": "depot_return", "depot_load": "depot_load"}
_LEGKIND_TYPE = {"CUSTOMER_PICKUP": "pickup", "CUSTOMER_DELIVERY": "delivery",
                 "DIRECT_CUSTOMER_MOVE": "direct", "HUB_DROP": "direct"}


def _min(ts) -> float | None:
    """'YYYY-MM-DD HH:MM:SS[.ffffff]' -> minutes from midnight of ITS day, or None."""
    s = str(ts or "").strip()
    if len(s) < 16 or s.lower() == "nan":
        return None
    try:
        hh, mm = int(s[11:13]), int(s[14:16])
        ss = int(s[17:19]) if len(s) >= 19 and s[17:19].isdigit() else 0
        return hh * 60 + mm + ss / 60.0
    except ValueError:
        return None


def _cell(v) -> str:
    """A CSV cell as a clean display string: '' for NaN / None / 'nan' / empty."""
    s = str(v).strip() if v is not None else ""
    return "" if s.lower() == "nan" else s


def _leg(job_id) -> str:
    s = str(job_id)
    return s[4:] if s.startswith("JOB:") else s


def _bk(ts, day: str) -> float:
    """Booked minutes on ``day``: 0 if booked before today, HH:MM if today, T1 if later."""
    if ts is None or pd.isna(ts):
        return 0.0
    dd = ts.strftime("%Y-%m-%d")
    if dd < day:
        return 0.0
    if dd > day:
        return float(T1)
    return ts.hour * 60 + ts.minute + ts.second / 60.0


def _trunk_legs(plan: Path, days: list[str], latest_return: dict) -> dict[str, list]:
    """Per-day trunk legs synthesized from trunk_schedule.csv. Overnight trunks (Bedford,
    CB22) split into an OUTBOUND leg on night N (export pallets up to the hub, 19:30 depart)
    and a RETURN leg on morning N+1 (EMPTY — export-only trunk, 2026-07-24; imports arrive
    via the invisible hub, 04:30 depot arrival). A Stoke DAY-trunk is a same-day out-and-back.
    Drive time = per-trip roundtrip km / 2 / speed.

    Gap 5 (user rule 2026-07-14): the schedule now names its tractors (``vehicles``,
    draw order). Each named trip becomes ONE leg carrying ``vid`` — the board renders
    it on that tractor's own lane. Trips beyond the named list (shortfall, or a legacy
    csv with no vehicles column) collapse into one unnamed aggregate leg that falls
    back to the separate trunk section."""
    try:
        from freight_planner.config import TRUNK_DAY_DEPOTS
        day_depots = set(TRUNK_DAY_DEPOTS)
    except Exception:
        day_depots = {"STOKE"}
    out: dict[str, list] = {d: [] for d in days}
    p = plan / "trunk_schedule.csv"
    if not p.exists():
        return out
    ts = pd.read_csv(p)
    for r in ts.itertuples(index=False):
        depot, hub = str(r.depot), str(getattr(r, "hub", "B37_HUB"))
        night = str(r.night)[:10]
        trips = int(getattr(r, "trips", 0) or 0)
        if trips <= 0:
            continue
        exp = float(getattr(r, "export_pallets", 0.0) or 0.0)
        drive = (float(getattr(r, "km", 0.0) or 0.0) / trips) / 2.0 / _TRUNK_SPEED_KMH * 60.0
        nxt = (pd.Timestamp(night) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        vids = [v for v in _cell(getattr(r, "vehicles", "")).split(";") if v][:trips]
        extra = trips - len(vids)          # unnamed remainder (shortfall / legacy csv)
        # Export-only trunk (2026-07-24): out-leg carries the export share, the
        # return leg runs EMPTY (imports arrive via the invisible hub, not our trunk).
        exp_share, ret_share = exp / trips, 0.0

        def _emit(day_key: str, dir_: str, s: float, e: float, pal_share: float,
                  day_trunk: int) -> None:
            if day_key not in out:
                return
            base = {"depot": depot, "hub": hub, "dir": dir_, "day_trunk": day_trunk,
                    "s": round(s, 1), "e": round(e, 1)}
            for v in vids:
                out[day_key].append({**base, "vid": v, "trips": 1, "pal": round(pal_share)})
            if extra > 0:
                out[day_key].append({**base, "trips": extra, "pal": round(pal_share * extra)})

        # trunk-wait rule: a trunk cannot leave before its last network collection is back at
        # the depot and consolidated. wait = latest depot-return + consolidation buffer.
        rback = latest_return.get((depot, night))
        wait = (rback + _TRUNK_CONSOLIDATE) if rback is not None else None
        if depot in day_depots:                              # same-day out-and-back
            dep = max(_TRUNK_DAY_DEPART, wait) if wait is not None else _TRUNK_DAY_DEPART
            ah = dep + drive; dh = ah + _TRUNK_HANDOVER; ab = dh + drive
            _emit(night, "out", dep, ah, exp_share, 1)
            # the return leg back to Stoke is always shown (empty running is real)
            _emit(night, "return", dh, ab, ret_share, 1)
        else:                                                # overnight: out tonight, back tomorrow AM
            odep = max(_TRUNK_OUT_DEPART, wait) if wait is not None else _TRUNK_OUT_DEPART
            _emit(night, "out", odep, odep + drive, exp_share, 0)
            _emit(nxt, "return", _TRUNK_RET_ARRIVE - drive, _TRUNK_RET_ARRIVE, ret_share, 0)
    return out


def _tour_segments(rs: pd.DataFrame, days: list[str]) -> dict:
    """{(day, vehicle_id) -> [seg]} for multi-day tour work (is_tour rows).

    Tours are seed-committed and IMMUTABLE — they live outside the per-epoch
    snapshot stream (current_sol), so without this overlay a tour vehicle's
    lane goes silently blank on its tour days. Timed customer stops become
    per-day bars (split at midnight); the timeless depot_start/depot_return
    rows mark the tour's first/last day so the board can place the depart ▸ /
    return ◂ markers honestly (a return day with no timed stop gets a
    ret-marker seg instead of a fabricated time)."""
    if "is_tour" not in rs.columns:
        return {}
    t = rs[rs["is_tour"] == True]  # noqa: E712  (CSV bools)
    out: dict = {}
    for rid, g in t.groupby("route_id"):
        vid = str(g.iloc[0]["vehicle_id"])
        dep_day = ret_day = None
        stops = []
        for r in g.itertuples(index=False):
            st = str(r.stop_type)
            day = str(r.service_date)[:10]
            if st == "depot_start":
                dep_day = day
            elif st == "depot_return":
                ret_day = day
            else:
                a, dp = _min(r.planned_arrive), _min(r.planned_depart)
                if a is None:
                    continue
                # driving + pre-arrival break BEFORE this stop: the bar must
                # cover it or a 12h drive arriving 17:46 renders as an evening
                # sliver (2026-07-20). Absent column (old CSVs) -> 0 = old look.
                lead = (float(getattr(r, "drive_minutes", 0.0) or 0.0)
                        + float(getattr(r, "break_minutes_before", 0.0) or 0.0))
                stops.append((day, a, dp if dp is not None else a, st, r, lead))
        stops.sort(key=lambda x: (x[0], x[1]))
        if not stops:
            continue
        tour_days = sorted({d for d, *_ in stops} | ({dep_day} if dep_day else set())
                           | ({ret_day} if ret_day else set()))
        n = len(tour_days)
        first_day = dep_day or tour_days[0]
        prev_dp: dict[str, float] = {}   # per-day: previous stop's departure
        for day, a, dp, st, r, lead in stops:
            s0 = max(0.0, a - lead, prev_dp.get(day, 0.0))
            prev_dp[day] = dp
            seg = {"s": round(s0, 1), "e": round(max(dp, a + 4), 1),
                   "ty": _TYPE.get(st, "direct"),
                   "o": _cell(getattr(r, "order_id", ""))[:8],
                   "pc": _cell(getattr(r, "service_pc", "")),
                   "k": tour_days.index(day) + 1, "n": n,
                   "d1": int(day == first_day and a == min(x[1] for x in stops if x[0] == day)),
                   "cont": int(day != (ret_day or tour_days[-1]))}
            if day in days:
                out.setdefault((day, vid), []).append(seg)
        if ret_day and ret_day in days and not any(d == ret_day for d, *_ in stops):
            out.setdefault((ret_day, vid), []).append(
                {"ret": 1, "k": n, "n": n})
    return out


def _num(v) -> float:
    """A coordinate cell as a float, with NaN/blank -> 0.0 (the 'no geocode' sentinel)."""
    try:
        f = float(v)
        return 0.0 if f != f else f     # NaN
    except (TypeError, ValueError):
        return 0.0


def _tour_day_routes(rs: pd.DataFrame, days: list[str], leg_load: dict | None = None) -> dict:
    """{(day, vehicle_id) -> that day's tour leg} with COORDINATES, for the map.

    Tours live outside the per-epoch snapshot stream, so the map (which reconstructs
    routes from ``snaps``) needs the tour geometry directly — and it must be SPLIT BY
    DAY so today's map shows only today's leg. A multi-day tour is ONE journey the
    truck never breaks to return home: day 1 leaves the depot and STAYS OUT; a later
    day RESUMES from where it parked overnight (the previous day-with-stops' last stop),
    never teleporting to a depot; only the final day returns to the depot. Only the days
    a vehicle is actually on tour get an entry, so ordinary local-work days stay clean.

    Each entry: ``{k, n, startDepot, endDepot, depot:[lat,lon]|None, resume:[lat,lon]|None,
    stops:[{lat,lon,ty,clat,clon,arr,dep,o,pc}]}`` (arr/dep = minutes from midnight)."""
    if "is_tour" not in rs.columns:
        return {}
    t = rs[rs["is_tour"] == True]  # noqa: E712
    out: dict = {}
    for _rid, g in t.groupby("route_id"):
        vid = str(g.iloc[0]["vehicle_id"])
        depot: list | None = None
        stops_by_day: dict[str, list] = {}
        overnight_by_day: dict[str, list[dict]] = {}
        depot_start_t: float | None = None
        depot_return_t: float | None = None
        dep_day = ret_day = None
        for r in g.itertuples(index=False):
            st = str(r.stop_type)
            day = str(r.service_date)[:10]
            lat, lon = _num(getattr(r, "lat", 0.0)), _num(getattr(r, "lon", 0.0))
            if st == "depot_start":
                dep_day = day
                depot_start_t = _min(getattr(r, "planned_depart", None))
                if not (lat == 0 and lon == 0):
                    depot = [round(lat, 5), round(lon, 5)]
                continue
            if st == "depot_return":
                ret_day = day
                depot_return_t = _min(getattr(r, "planned_arrive", None))
                if depot is None and not (lat == 0 and lon == 0):
                    depot = [round(lat, 5), round(lon, 5)]
                continue
            if st == "tour_overnight":
                # a mid-leg sleep point (MULTIDAY_MIDLEG_OVERNIGHT), dated the day
                # it resumes — never a service stop
                if not (lat == 0 and lon == 0):
                    a, dp = _min(r.planned_arrive), _min(r.planned_depart)
                    overnight_by_day.setdefault(day, []).append({
                        "coord": [round(lat, 5), round(lon, 5)],
                        "arr": a, "dep": dp,
                    })
                # A DIRECT origin reached exactly at the day boundary is emitted
                # as a dual-purpose overnight row. It carries a real collection
                # dwell/order/load, so preserve that physical stop on the map.
                # Blank overnight rows remain invisible sleep markers.
                order_id = _cell(getattr(r, "order_id", ""))
                leg_id = _cell(getattr(r, "leg_id", ""))
                if (order_id or leg_id) and not (lat == 0 and lon == 0):
                    a, dp = _min(r.planned_arrive), _min(r.planned_depart)
                    stops_by_day.setdefault(day, []).append((a if a is not None else 0.0, {
                        "lat": round(lat, 5), "lon": round(lon, 5), "ty": "pickup",
                        "clat": 0.0, "clon": 0.0,
                        "arr": round(a, 1) if a is not None else None,
                        "dep": round(dp, 1) if dp is not None else None,
                        "o": order_id[:8],
                        "pc": (_cell(getattr(r, "collect_pc", ""))
                               or _cell(getattr(r, "service_pc", ""))),
                        "pco": "",
                        "lp": round(_num(getattr(r, "load_pallets_after", 0.0)), 1),
                        "lkg": round(_num(getattr(r, "load_kg_after", 0.0))),
                        "p": round(_num((leg_load or {}).get(leg_id, (0.0, 0.0))[0]), 1),
                        "kg": round(_num((leg_load or {}).get(leg_id, (0.0, 0.0))[1])),
                    }))
                continue
            if lat == 0 and lon == 0:
                continue
            a, dp = _min(r.planned_arrive), _min(r.planned_depart)
            stops_by_day.setdefault(day, []).append((a if a is not None else 0.0, {
                "lat": round(lat, 5), "lon": round(lon, 5), "ty": _TYPE.get(st, "direct"),
                "clat": round(_num(getattr(r, "collect_lat", 0.0)), 5),
                "clon": round(_num(getattr(r, "collect_lon", 0.0)), 5),
                "arr": round(a, 1) if a is not None else None,
                "dep": round(dp, 1) if dp is not None else None,
                "o": _cell(getattr(r, "order_id", ""))[:8],
                "pc": _cell(getattr(r, "service_pc", "")),
                "pco": _cell(getattr(r, "collect_pc", "")),   # direct carry: origin -> pc
                # load-utilization step lines on tour days (2026-07-16): the load
                # AFTER this stop + the leg's own quantity (to derive the before)
                "lp": round(_num(getattr(r, "load_pallets_after", 0.0)), 1),
                "lkg": round(_num(getattr(r, "load_kg_after", 0.0))),
                "p": round(_num((leg_load or {}).get(str(getattr(r, "leg_id", "")), (0.0, 0.0))[0]), 1),
                "kg": round(_num((leg_load or {}).get(str(getattr(r, "leg_id", "")), (0.0, 0.0))[1])),
            }))
        for d in stops_by_day:
            stops_by_day[d].sort(key=lambda x: x[0])
        for events in overnight_by_day.values():
            events.sort(key=lambda event: (event["arr"] is None,
                                           event["arr"] if event["arr"] is not None else 0.0))
        tour_days = sorted(set(stops_by_day)
                           | set(overnight_by_day)
                           | ({dep_day} if dep_day else set())
                           | ({ret_day} if ret_day else set()))
        if not tour_days:
            continue
        n = len(tour_days)
        prev_last: list | None = None     # coord the truck parked at overnight, carried across days
        for i, day in enumerate(tour_days):
            day_stops = [s for _a, s in stops_by_day.get(day, [])]
            start_depot = (i == 0)
            events = overnight_by_day.get(day, [])
            morning = next((event for event in events
                            if event["arr"] is None or event["arr"] <= 12 * 60), None)
            evening = next((event for event in reversed(events)
                            if event["arr"] is not None and event["arr"] > 12 * 60), None)
            # a mid-leg sleep resumes from the tour_overnight coordinate; the
            # stop-boundary fallback stays the previous day's last stop
            resume_event = None if start_depot else (morning or (events[0] if events else None))
            resume = (None if start_depot else
                      (list(resume_event["coord"]) if resume_event else
                       (list(prev_last) if prev_last else None)))
            # where this day's driving actually ENDS: the NEXT tour day's mid-leg
            # sleep point, if it has one (so the polyline extends past the last stop)
            park_event = evening or (events[-1] if start_depot and events else None)
            if park_event is None and i + 1 < n:
                next_events = overnight_by_day.get(tour_days[i + 1], [])
                park_event = next_events[0] if next_events else None
            park = list(park_event["coord"]) if park_event and i < n - 1 else None
            start_t = (depot_start_t if start_depot else
                       (resume_event["dep"] if resume_event else None))
            end_t = (depot_return_t if i == n - 1 else
                     (park_event["arr"] if park_event else None))
            if day in days:
                out[(day, vid)] = {
                    "k": i + 1, "n": n,
                    "startDepot": bool(start_depot and depot is not None),
                    "endDepot": bool(i == n - 1 and depot is not None),
                    "depot": list(depot) if depot else None,
                    "resume": resume,
                    "park": park,
                    "startT": round(start_t, 1) if start_t is not None else None,
                    "endT": round(end_t, 1) if end_t is not None else None,
                    "stops": day_stops,
                }
            if day_stops:
                prev_last = [day_stops[-1]["lat"], day_stops[-1]["lon"]]
    return out


def _run_files(run_dir: Path):
    """Dual-layout access to a window dir's artifacts: the current layout
    (csv/ + md/ + root, one RunPaths router serves both names) or the legacy
    plan/ + reports/ split (plain Paths — joins hit the files directly)."""
    run_dir = Path(run_dir)
    if (run_dir / "csv").is_dir():
        router = RunPaths(run_dir)
        return router, router
    return run_dir / "plan", run_dir / "reports"


def build(run_dir: Path, delta: int = 60, only_day: str | None = None,
         geometry: bool = True, osrm_url: str | None = None) -> dict:
    plan, reports = _run_files(run_dir)
    rs = pd.read_csv(plan / "route_stops.csv")
    # per-leg pallets/weight for the tooltip + the JS load profile (plan_full has the per-leg
    # load; route_stops only carries the cumulative load_*_after). Keyed by leg_id (a split
    # order carries different pallets per leg). Located by rglob so it works under either
    # run-folder layout; absent -> empty (fields default to 0).
    def _first(name):
        return next(iter(run_dir.rglob(name)), None)
    leg_load: dict[str, tuple] = {}
    flow_of_leg: dict[str, str] = {}
    pf_path = _first("plan_full.csv")
    if pf_path is not None:
        pf = pd.read_csv(pf_path)
        for r in pf.itertuples(index=False):
            lid = str(getattr(r, "leg_id", ""))
            # _num, not `or 0.0`: NaN is truthy, and quantity-less rows (DEPOT_LOAD,
            # commissioned tour stops) carry NaN pallets/weight in plan_full.
            p = _num(getattr(r, "pallets", 0.0))
            kg = _num(getattr(r, "weight_kg", 0.0))
            if lid and lid not in leg_load:
                leg_load[lid] = (p, kg)
            fl = _cell(getattr(r, "flow", ""))
            if lid and fl and lid not in flow_of_leg:   # all-routes flow filter
                flow_of_leg[lid] = fl
    # per-vehicle capacity for utilization = load / capacity
    veh_cap: dict[str, tuple] = {}
    tc_path = _first("trip_capacity_utilization.csv")
    if tc_path is not None:
        tc = pd.read_csv(tc_path)
        for r in tc.itertuples(index=False):
            vid = str(getattr(r, "vehicle_id", ""))
            cp = float(getattr(r, "capacity_pallets", 0.0) or 0.0)
            ck = float(getattr(r, "capacity_kg", 0.0) or 0.0)
            if vid:
                veh_cap[vid] = (max(veh_cap.get(vid, (0.0, 0.0))[0], cp),
                                max(veh_cap.get(vid, (0.0, 0.0))[1], ck))
    snap_path = reports / "plan_snapshots.csv"
    if not snap_path.exists():
        raise SystemExit(f"no plan_snapshots.csv in {reports} — the run must be "
                         f"produced with per-epoch snapshots (Part A, run_rolling).")
    snap = pd.read_csv(snap_path)
    snap = snap[snap["service_date"].astype(str) != ""].copy()
    # TRUNK rows (trunk_snapshot_rows, 2026-07-21) are analysis rows for the
    # momentum figures — on the board they'd become ghost jobs at (0,0); trunk
    # lanes render from trunk_schedule.csv instead.
    if "leg_kind" in snap.columns:
        snap = snap[snap["leg_kind"].astype(str) != "TRUNK"].copy()
    snap["leg"] = snap["job_id"].map(_leg)

    # FLOW fallback for runs without plan_full.csv: a manifest that carries a
    # flow column (fixtures / legacy layouts).
    mf_path = None if flow_of_leg else _first("plan_manifest_new.csv")
    if mf_path is not None:
        mf = pd.read_csv(mf_path)
        if "flow" in mf.columns:
            for lid, fl in zip(mf["leg_id"].astype(str), mf["flow"].astype(str)):
                if lid and fl and fl != "nan" and lid not in flow_of_leg:
                    flow_of_leg[lid] = fl

    # names + created (booking time) from qargo
    names: dict[str, str] = {}
    created: dict[str, "pd.Timestamp"] = {}
    qpath = _find_qargo(run_dir)
    if qpath is not None:
        q = pd.read_parquet(qpath) if qpath.suffix == ".parquet" else pd.read_csv(qpath)
        if "name" in q.columns:
            names = dict(zip(q["order_id"].astype(str), q["name"].fillna("").astype(str)))
        cr = pd.to_datetime(q["timestamp_created"], errors="coerce", utc=True).dt.tz_localize(None)
        created = {str(o): t for o, t in zip(q["order_id"].astype(str), cr) if pd.notna(t)}

    # static per-leg attributes from the FINAL route_stops (stable across epochs).
    # Include direct_customer_move: it is a real customer leg (origin collect_pc -> dest
    # service_pc) that was previously excluded, so directs fell back to showing an order id.
    _st = rs["stop_type"].astype(str)
    cust = rs[_st.str.startswith("customer") | _st.str.startswith("direct")]
    static: dict[str, dict] = {}
    for r in cust.itertuples(index=False):
        static[str(r.leg_id)] = {
            "o": _cell(getattr(r, "order_id", "")), "pc": _cell(getattr(r, "service_pc", "")),
            "pco": _cell(getattr(r, "collect_pc", "")),   # origin postcode: a DIRECT carry shows pco -> pc
            "ty": _TYPE.get(str(r.stop_type), str(r.stop_type)),
            "lat": float(getattr(r, "lat", 0.0) or 0.0), "lon": float(getattr(r, "lon", 0.0) or 0.0),
            # a DIRECT carry's collect ORIGIN (its deliver dest is in lat/lon) — the map
            # draws collect -> deliver; 0.0 when absent (non-direct legs)
            "clat": float(getattr(r, "collect_lat", 0.0) or 0.0),
            "clon": float(getattr(r, "collect_lon", 0.0) or 0.0),
        }

    # per-vehicle meta (type / home depot for lane grouping / tour flag)
    vmeta: dict[str, dict] = {}
    for r in rs.itertuples(index=False):
        vid = str(r.vehicle_id)
        m = vmeta.setdefault(vid, {"type": _cell(getattr(r, "vehicle_type", "")),
                                   "home": _cell(getattr(r, "vehicle_home_depot", "")),
                                   "tour": False})
        if bool(getattr(r, "is_tour", False)):
            m["tour"] = True

    days = sorted(snap["service_date"].astype(str).str[:10].unique())
    if only_day:
        days = [d for d in days if d == only_day]
    tour_segs = _tour_segments(rs, days)
    # latest network-collection return per (home depot, day): a trunk cannot depart before its
    # consolidated freight is back at the depot (trunk-wait rule, user 2026-07-13).
    latest_return: dict = {}
    for r in rs[rs["stop_type"].astype(str) == "depot_return"].itertuples(index=False):
        m = _min(getattr(r, "planned_arrive", ""))
        if m is None:
            continue
        k = (str(getattr(r, "vehicle_home_depot", "")), str(getattr(r, "service_date", ""))[:10])
        latest_return[k] = max(latest_return.get(k, 0.0), m)
    trunk_by_day = _trunk_legs(plan, days, latest_return)
    # Only legs in the FINAL committed plan may appear on the board/map. Endogenous
    # DIRECT/XDOCK (2026-07-23) leaves losing-alternative legs in early-epoch
    # snapshots (a DIRECT the seed placed then the search swapped to XDOCK); those
    # never reach route_stops, so without this filter they render as coordless
    # "direct" ghosts — inflating the direct count and drawing no route. Committed
    # legs are frozen and thus always in route_stops, so nothing real is dropped.
    final_legs = set(rs["leg_id"].astype(str)) if "leg_id" in rs.columns else None
    # Corrected depot departure per (first committed stop) leg. When an endogenous
    # option-swap drops a trip's leading stops at commit, the snapshot route_start
    # still belongs to those now-gone stops (W88RNW: 06:05, the dropped DIRECT's start)
    # while the first surviving stop is hours later — an impossible depot idle. Derive
    # the real departure from the committed geometry: first-stop arrival minus its
    # drive-in. Keyed by that leg so the board/map can re-anchor the depot leg.
    first_stop_dep: dict = {}
    if not rs.empty and {"drive_minutes", "trip_index", "leg_id"} <= set(rs.columns):
        _real = rs[rs["stop_type"].astype(str).isin(
            ("customer_pickup", "customer_delivery", "direct_customer_move"))]
        for _key, _g in _real.groupby(["vehicle_id", "trip_index"]):
            _fr = _g.sort_values("sequence").iloc[0]
            _a = _min(_fr.get("planned_arrive"))
            if _a is not None:
                first_stop_dep[str(_fr.get("leg_id"))] = _a - float(_fr.get("drive_minutes") or 0.0)
    # Corrected depot RETURN arrival per (vehicle, trip) — symmetric to first_stop_dep.
    # When an option-swap drops a trip's TRAILING far leg at commit, the snapshot
    # route_end still reflects the long return from that now-gone stop (an 8 km return
    # "arriving" 3h45m after the last stop; FJ72XFF/Y88RNW 2026-07-24). The per-epoch
    # timeline stops are already filtered to committed legs, so the bar ends at the last
    # SURVIVING stop but route_end doesn't — re-anchor the return to the committed
    # geometry route_stops carries. Keyed by (vehicle_id, trip_index).
    # Keyed by (vehicle_id, service_date, trip_index): trip_index repeats across days
    # (B29BAL has trip 1 on Feb-2 AND Feb-3) and _min() is minutes-from-midnight-of-ITS-day,
    # so the day MUST be part of the key or the wrong day's arrival leaks in.
    last_stop_ret: dict = {}
    if not rs.empty and {"trip_index", "vehicle_id", "service_date"} <= set(rs.columns):
        _dr = rs[rs["stop_type"].astype(str) == "depot_return"]
        for _key, _g in _dr.groupby(["vehicle_id", "service_date", "trip_index"]):
            _ra = _min(_g.sort_values("sequence").iloc[-1].get("planned_arrive"))
            if _ra is not None:
                last_stop_ret[(str(_key[0]), str(_key[1])[:10], int(_key[2]))] = _ra
    # Committed arrival/departure per customer leg, straight from route_stops — the
    # authoritative post-commit geometry. rebuild_daily_routes_after_drop (route_seed.py,
    # 2026-07-28) can re-time a SURVIVING stop once a leading leg is dropped at commit, so
    # the last snapshot's own arrival for that leg can go stale (WT262812/Y90RNW: snapshot
    # said 18:03, the committed rebuild moved it to 12:00 once its leading DIRECT went to
    # another vehicle). Combining the STALE stop time with the fold-bar's drive-in (which
    # already re-anchors to this same committed geometry via first_stop_dep) produced a
    # nonsense multi-hour "drive". Keyed by leg_id like first_stop_dep/last_stop_ret.
    final_stop_time: dict = {}
    if not rs.empty and "leg_id" in rs.columns:
        _real2 = rs[rs["stop_type"].astype(str).isin(
            ("customer_pickup", "customer_delivery", "direct_customer_move"))]
        for _r in _real2.itertuples(index=False):
            final_stop_time[str(_r.leg_id)] = (_min(getattr(_r, "planned_arrive", "")),
                                                _min(getattr(_r, "planned_depart", "")))
    day_list = [_build_day(d, snap, static, vmeta, names, created, plan, reports,
                           tour_segs, trunk_by_day.get(d, []), leg_load, veh_cap,
                           flow_of_leg, final_legs, first_stop_dep, last_stop_ret,
                           final_stop_time)
                for d in days]
    # attach each vehicle's tour route FOR THAT DAY (tours live outside snaps and are
    # split by day, so the map draws only the leg of the journey that belongs to the day
    # in view). Absent on ordinary local-work days, so they render as normal routes.
    tour_day_routes = _tour_day_routes(rs, days, leg_load)
    for day in day_list:
        for veh in day.get("vehicles", []):
            td = tour_day_routes.get((day["day"], str(veh["id"])))
            if td:
                veh["tourDay"] = td
    depots = [{"name": str(name), "lat": float(a[0]), "lon": float(a[1])}
              for name, a in DEPOT_ANCHORS.items()]
    geom: dict = {}
    if geometry:
        anchors = {name: (float(a[0]), float(a[1])) for name, a in DEPOT_ANCHORS.items()}
        geom = bake(route_pairs(day_list, anchors), osrm_url=osrm_url)
    return {
        "meta": {"days": days, "delta": delta, "delta_r1": DELTA_R1, "t0": T0, "t1": T1,
                 "note": "Per-epoch snapshots — the plan exactly as it stood at each clock T "
                         "(seed continuous; the noon re-opt reshuffles the uncommitted tail; "
                         "committed stops only delay). Page days with the arrows."},
        "days": day_list,
        "depots": depots,
        "geom": geom,
    }


def _build_day(day, snap, static, vmeta, names, created, plan, reports, tour_segs,
               trunk_legs_today, leg_load, veh_cap, flow_of_leg=None, final_legs=None,
               first_stop_dep=None, last_stop_ret=None, final_stop_time=None) -> dict:
    d = snap[snap["service_date"].astype(str).str[:10] == day].copy()
    d["em"] = d["epoch"].map(_min)
    ek = d.drop_duplicates("epoch")[["epoch", "em", "epoch_kind"]].sort_values("em")
    epoch_iso = list(ek["epoch"].astype(str))
    snapAt = [round(float(m), 1) for m in ek["em"]]
    snapKind = list(ek["epoch_kind"].astype(str))

    # jobs: one entry per distinct leg in the day, indexed. Keep only legs in the
    # final committed plan (final_legs) so transient superseded option legs from
    # early-epoch snapshots do not become coordless "direct" ghosts (2026-07-23).
    first = d.drop_duplicates("leg").set_index("leg")
    if final_legs is not None:
        first = first[first.index.astype(str).isin(final_legs)]
    legs = list(first.index)
    jidx = {leg: i for i, leg in enumerate(legs)}

    # per-leg placement across epochs -> new (first PLACED after the day's seed, i.e. a
    # dynamically-inserted order = red dot) + reopt (its vehicle/trip/arrival CHANGED vs the
    # previous epoch it appeared in = re-optimised = green dot). Red is per-order (persistent);
    # green is per-epoch; both show at once when an inserted order is later moved.
    seed_ep = epoch_iso[0] if epoch_iso else None
    place: dict = {}
    for r in d.itertuples(index=False):
        if int(getattr(r, "sequence", -1)) < 0:
            continue
        place.setdefault(r.leg, {})[str(r.epoch)] = (
            str(r.vehicle_id), int(r.trip_index), _min(r.planned_arrive))
    new_legs: set = set()
    reopt: dict = {}
    for leg, ep_map in place.items():
        present = [e for e in epoch_iso if e in ep_map]
        if present and present[0] != seed_ep:
            new_legs.add(leg)
        prev = None
        for e in present:
            cur = ep_map[e]
            if prev is not None and cur != prev:
                reopt[(leg, e)] = 1
            prev = cur

    jobs = []
    for leg in legs:
        st = static.get(leg)
        row = first.loc[leg]
        oid = (st["o"] if st and st["o"] else _cell(row["order_id"]))
        ty = st["ty"] if st else _LEGKIND_TYPE.get(str(row["leg_kind"]), "pickup")
        pc = st["pc"] if st else ""
        pco = st.get("pco", "") if st else ""
        lat = st["lat"] if st else 0.0
        lon = st["lon"] if st else 0.0
        clat = st.get("clat", 0.0) if st else 0.0
        clon = st.get("clon", 0.0) if st else 0.0
        pk = leg_load.get(str(leg), (0.0, 0.0))
        j = {"o": oid[:8], "nm": names.get(oid, ""), "pc": pc, "pco": pco, "ty": ty,
             "fl": str((flow_of_leg or {}).get(str(leg), "")),
             "lat": round(lat, 5), "lon": round(lon, 5),
             "bk": round(_bk(created.get(oid), day), 1),
             "new": int(leg in new_legs),
             "pallets": round(pk[0], 1), "kg": round(pk[1])}
        if ty == "direct" and clat and clon:          # collect origin for the map's direct legs
            j["clat"], j["clon"] = round(clat, 5), round(clon, 5)
        jobs.append(j)

    vehicles = []
    for vid, g in d.groupby("vehicle_id"):
        vm = vmeta.get(vid, {})
        by_epoch = {ei: sub for ei, sub in g.groupby("epoch")}
        snaps = []
        last_ei = epoch_iso[-1] if epoch_iso else None
        for ei in epoch_iso:
            stops: list = []
            ge = by_epoch.get(ei)
            if ge is not None:
                trips = []
                for tr, tg in ge.groupby("trip_index"):
                    tgs = tg.sort_values("sequence")
                    # skip null-time stops: an infeasible/unschedulable park has no arrival, so
                    # it would otherwise render at the left edge (the "3am" ghosts). It is not a
                    # real schedule entry — it belongs in the unassigned count, not on the clock.
                    real = [r for r in tgs.itertuples(index=False) if _min(r.planned_arrive) is not None]
                    cust = []
                    for r in real:
                        if r.leg not in jidx:
                            continue
                        _a, _dp = _min(r.planned_arrive), _min(r.planned_depart)
                        # Only the LAST snapshot epoch is reconciled against route_stops.
                        # Every earlier epoch is genuine history -- "the plan exactly as it
                        # stood at each clock T" (module docstring) -- and must keep showing
                        # what was actually true then, even if a later commit-time rebuild
                        # (route_seed.rebuild_daily_routes_after_drop, 2026-07-28) re-timed the
                        # stop. Applying the final time everywhere made a stop that only moved
                        # DURING the final rebuild look like it had already jumped there at its
                        # very first (mid-day) appearance -- itself indistinguishable from a
                        # 90-min freeze violation on the board (W88RNW/2026-02-02: reported by
                        # user as a same-day false alarm; the true floor -- floor_at_place in
                        # stop_provenance.csv -- was never actually violated).
                        if ei == last_ei:
                            _fa, _fd = (final_stop_time or {}).get(str(r.leg), (None, None))
                            _a = _fa if _fa is not None else _a
                            _dp = _fd if _fd is not None else _dp
                        cust.append((jidx[r.leg], _a, _dp, int(getattr(r, "committed", 0) or 0)))
                    if cust:
                        r0 = tgs.iloc[0]
                        # Did the trip's FIRST real stop survive the final-plan filter? When an
                        # endogenous option-swap moves this trip's leading stops onto other
                        # vehicles, their (superseded) legs are dropped here — but this snapshot's
                        # route_start still belongs to those now-hidden stops. Folding it onto the
                        # first SURVIVING stop draws a phantom hours-long "drive from 06:05"
                        # (WT262797/AL7 2026-07-24). Flag it so the depot fold is skipped.
                        leading_dropped = bool(real) and str(real[0].leg) not in jidx
                        trips.append((int(tr), cust,
                                      _min(r0.get("route_start")), _min(r0.get("route_end")),
                                      leading_dropped))
                trips.sort(key=lambda t: t[0])
                for (tri, cust, rs, re, leading_dropped) in trips:
                    fa = cust[0][1]
                    # depot -> first stop: the recorded depot departure (route_start) folds into
                    # the first order's coloured box. When leading stops were dropped by an
                    # option-swap at commit, route_start belongs to those gone stops (stale 06:05),
                    # so re-anchor the departure to the committed geometry: first surviving stop's
                    # arrival minus its real drive-in. The truck still leaves the DEPOT, just at the
                    # right afternoon time — not a 9h phantom drive and not a mid-route "open place".
                    fold_start = rs
                    if leading_dropped:
                        fold_start = (first_stop_dep or {}).get(str(legs[cust[0][0]]))
                    if fold_start is not None and fa is not None and fold_start < fa:
                        stops.append([-2, round(fold_start, 1), round(fa, 1), 0, tri, 0])
                    for (ji, a, dp, cf) in cust:
                        stops.append([ji, round(a, 1) if a is not None else None,
                                      round(dp, 1) if dp is not None else None, cf, tri,
                                      reopt.get((legs[ji], ei), 0)])   # 6th = re-optimised this epoch
                    # last stop -> depot: the committed return arrival from route_stops, NOT the
                    # snapshot route_end — which, when an option-swap dropped this trip's trailing
                    # far leg at commit, still carries the long return from that now-hidden stop
                    # (cust already ends at the last SURVIVING stop). Mirrors first_stop_dep's
                    # re-anchor of the depot departure (2026-07-24).
                    ld = cust[-1][2]
                    re_c = (last_stop_ret or {}).get((str(vid), day, int(tri)), re)
                    if re_c is not None and ld is not None and re_c > ld:
                        stops.append([-1, round(ld, 1), round(re_c, 1), 0, tri, 0])
            snaps.append(stops)
        grew = bool(snaps) and len(snaps[-1]) > len(snaps[0])
        # the tour flag is PER DAY: a vehicle is "on tour" only on the days it actually
        # tours (has tour segments that day), not on its ordinary local-work days. This is
        # what puts the ⛺ symbol / tour route on the right days only.
        cap = veh_cap.get(vid, (0.0, 0.0))
        # Idle vehicle: every snapshot leg it carried was transient (superseded option
        # legs / never committed), so it has no committed stop on any epoch this day.
        # Don't draw an empty lane for it (it would show home-less as "(no depot)").
        # A vehicle that only runs a night trunk or is out on tour has no day stops
        # either, but the trunk / tour fallbacks below re-add its lane, so skipping
        # here is safe.
        if not any(snaps) and not tour_segs.get((day, vid)):
            continue
        vehicles.append({"id": vid, "type": vm.get("type", ""), "home": vm.get("home", ""),
                         "tour": bool(tour_segs.get((day, vid))), "intraday": grew, "snaps": snaps,
                         "tsegs": tour_segs.get((day, vid), []),
                         "capP": round(cap[0], 1), "capKg": round(cap[1])})
    # a vehicle out on TOUR all day has no snapshot rows — give it a lane anyway
    seen = {v["id"] for v in vehicles}
    for (d2, vid), segs in tour_segs.items():
        if d2 == day and vid not in seen:
            vm = vmeta.get(vid, {})
            cap = veh_cap.get(vid, (0.0, 0.0))
            vehicles.append({"id": vid, "type": vm.get("type", ""), "home": vm.get("home", ""),
                             "tour": True, "intraday": False,
                             "snaps": [[] for _ in epoch_iso], "tsegs": segs,
                             "capP": round(cap[0], 1), "capKg": round(cap[1])})

    # trunk runs on the ASSIGNED tractor's own lane (gap 5): named legs attach to
    # the vehicle; a tractor idle all day still gets a lane for its night run.
    # Unnamed legs (shortfall remainder / legacy csv) stay for the fallback section.
    named: dict[str, list] = {}
    day_trunks: list = []
    for leg in trunk_legs_today:
        if leg.get("vid"):
            named.setdefault(str(leg["vid"]), []).append(leg)
        else:
            day_trunks.append(leg)
    for v in vehicles:
        legs2 = named.pop(v["id"], None)
        if legs2:
            v["trunks"] = legs2
    for vid, legs2 in sorted(named.items()):
        vm = vmeta.get(vid, {})
        cap = veh_cap.get(vid, (0.0, 0.0))
        vehicles.append({"id": vid, "type": vm.get("type", "tractor"),
                         "home": vm.get("home", "") or str(legs2[0].get("depot", "")),
                         "tour": False, "intraday": False,
                         "snaps": [[] for _ in epoch_iso], "tsegs": [], "trunks": legs2,
                         "capP": round(cap[0], 1), "capKg": round(cap[1])})

    # depot-grouped ordering (user 2026-07-13): home depot first, then vehicle type
    # (tractor -> rigid -> van, user 2026-07-20), then id; blanks last. The app groups
    # consecutive rows under a depot header, so this is the lane order.
    _type_rank = {"tractor": 0, "rigid": 1, "van": 2}
    vehicles.sort(key=lambda v: (
        v["home"] or "~",
        _type_rank.get(str(v.get("type", "")).lower(), 9),
        str(v.get("type", "")).lower(),
        v["id"]))
    return {
        "day": day, "epochs": _epochs(plan / "rolling_manifest.json", reports / "micro_passes.csv", day),
        "snapAt": snapAt, "snapKind": snapKind, "jobs": jobs, "vehicles": vehicles,
        "trunks": day_trunks,
    }


def _find_qargo(run_dir: Path) -> Path | None:
    run_dir = Path(run_dir).resolve()
    mf = run_dir / "run_manifest.json"
    if mf.exists():
        q = json.loads(mf.read_text()).get("qargo", "")
        for base in (run_dir.parents[3], Path.cwd()):
            p = (base / q)
            if q and p.exists():
                return p
    for p in (run_dir.parents[3] / "data/Input/orders").glob("qargo_*.parquet"):
        return p
    return None


def _epochs(manifest: Path, micro_csv: Path, day: str) -> list[dict]:
    out: list[dict] = []
    if manifest.exists():
        anchors = json.loads(manifest.read_text()).get("anchors", [])
        for a in anchors:
            e = str(a.get("epoch", ""))
            if e.startswith(day):
                m = _min(e)
                if m is not None:
                    out.append({"m": m, "kind": "seed" if m <= T0 + 1 else "warm"})
    if micro_csv.exists():
        mp = pd.read_csv(micro_csv)
        for r in mp.itertuples(index=False):
            at = str(getattr(r, "at", ""))
            if at.startswith(day):
                m = _min(at)
                if m is not None:
                    out.append({"m": m, "kind": "micro", "ins": int(getattr(r, "inserted", 0) or 0),
                                "fail": int(getattr(r, "failed", 0) or 0)})
    out.append({"m": T1, "kind": "close"})
    return sorted(out, key=lambda e: e["m"])


def render_page(payload: str) -> str:
    """Wrap the timeline template around an embedded JSON payload. The template
    is page CONTENT (authored for a wrapping host); give the local file a real
    document shell so any browser renders it standalone. The pure map-logic
    module (viz_timeline_maplogic.cjs) is inlined verbatim in place of the
    __MAPLOGIC__ marker, so the map's route math ships in the self-contained
    file yet stays unit-testable in Node from its own source."""
    here = Path(__file__).parent
    tmpl = (here / "viz_timeline_template.html").read_text(encoding="utf-8")
    maplogic = (here / "viz_timeline_maplogic.cjs").read_text(encoding="utf-8")
    body = tmpl.replace("__MAPLOGIC__", maplogic).replace("__DATA__", payload)
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "</head>\n<body>\n" + body + "\n</body>\n</html>\n")


def write_dashboard(run_dir: Path, out_html: Path, delta: int = 60,
                    only_day: str | None = None, geometry: bool = True,
                    osrm_url: str | None = None) -> Path:
    """Build the timeline payload for a run and emit the self-contained
    dashboard page — the hook run_rolling calls to auto-drop timeline.html at
    the run root."""
    data = build(Path(run_dir), delta, only_day=only_day, geometry=geometry, osrm_url=osrm_url)
    payload = json.dumps(data, separators=(",", ":"))
    out_html = Path(out_html)
    out_html.write_text(render_page(payload), encoding="utf-8")
    return out_html


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", required=True,
                    help="a window dir (current csv|md|root layout, or legacy plan/+reports/)")
    ap.add_argument("--day", default=None, help="optional: build only this day")
    ap.add_argument("--delta", type=int, default=60)
    ap.add_argument("--out", required=True)
    ap.add_argument("--html", default=None,
                    help="also emit the self-contained dashboard: the JSON is embedded "
                         "into viz_timeline_template.html (open the file in a browser)")
    ap.add_argument("--no-geometry", dest="geometry", action="store_false",
                    help="skip OSRM road-geometry baking (fast rebuild; map straight-lines)")
    args = ap.parse_args()
    data = build(Path(args.run_dir), args.delta, only_day=args.day, geometry=args.geometry)
    payload = json.dumps(data, separators=(",", ":"))
    Path(args.out).write_text(payload, encoding="utf-8")
    nday = len(data["days"])
    nsnap = sum(len(dd["snapAt"]) for dd in data["days"])
    nveh = sum(len(dd["vehicles"]) for dd in data["days"])
    print(f"timeline: {nday} days, {nsnap} snapshots, {nveh} vehicle-days -> {args.out}")
    if args.html:
        Path(args.html).write_text(render_page(payload), encoding="utf-8")
        print(f"timeline: dashboard -> {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
