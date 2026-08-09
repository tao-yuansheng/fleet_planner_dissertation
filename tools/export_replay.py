"""Export a self-contained HTML fleet-replay page for a given day.

The output is a single .html file — open it in any browser, no server needed.

Usage:
    # from BackEnd/logistics/
    python operational_analysis/export_replay.py --date 2026-01-07
    python operational_analysis/export_replay.py --date 2026-01-07 --mode circuit --circuit Bedford
    python operational_analysis/export_replay.py --date 2026-01-07 --mode vehicle --vehicle HX17CUA
    python operational_analysis/export_replay.py --date 2026-01-07 --step 5 --no-open
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import webbrowser
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from freight_planner.shared import fleet_replay_data as frd
from freight_planner.shared.paths import LOGISTICS_ROOT

COLOR_BY_TYPE: dict[str, str] = {
    "Tractor Unit": "#1f77b4",
    "Lorry": "#2ca02c",
    "Rigid Truck": "#2ca02c",
    "Mini Truck": "#2ca02c",
    "Service Van": "#2ca02c",  # vans fold into the rigid category
}
DEFAULT_COLOR = "#888888"


# ---------------------------------------------------------------------------
# Qargo / orders helpers
# ---------------------------------------------------------------------------

_EXCLUDE_STATUSES = {"cancelled", "quote", "template", "planned", "to_plan"}

# FC / hub facility-code mappings live in the single source of truth in
# simulation.postcode_resolver. Imported here so replay resolves the same codes
# as the dispatch pipeline; add new codes there, not here.
from freight_planner.shared.postcode_resolver import (
    FC_CODE_ALIASES as _FC_CODE_ALIASES,
    NON_STANDARD_PCS as _NON_STANDARD_PCS,
)

# Palletline hub postcode — artics stop here overnight to collect import loads.
_PALLETLINE_HUB_PC = "B377HB"


def _std_reg(val) -> str:
    """Normalise a Qargo vehicle registration: strip whitespace, uppercase, strip trailing '2' artefacts."""
    if pd.isna(val):
        return ""
    s = re.sub(r"\s+", "", str(val).strip()).upper()
    cleaned = s.rstrip("2")
    return cleaned if cleaned else s


def _split_regs(val) -> list[str]:
    """Split a comma-separated Qargo vehicle cell into cleaned registration strings."""
    if pd.isna(val):
        return []
    return [r for r in (_std_reg(p) for p in str(val).split(",")) if r]


def _month_qargo_path(d: date) -> Path:
    """Return the qargo Excel path for the month containing `d`."""
    first = d.replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    last = next_month - timedelta(days=1)
    orders_dir = LOGISTICS_ROOT / "data" / "Input" / "orders"
    return orders_dir / f"qargo_{first:%Y%m%d}_to_{last:%Y%m%d}.xlsx"


def _veh_pc_at(ts: pd.Timestamp, vname: str, veh_day_pings: dict,
               n_days: int, overall_start: pd.Timestamp) -> str:
    """Return normalised postcode of vehicle nearest in time to ts, or ''."""
    day_idx = int((ts.normalize() - overall_start.normalize()).days)
    if day_idx < 0 or day_idx >= n_days:
        return ""
    pings = veh_day_pings.get(day_idx, {}).get(vname)
    if pings is None or pings.empty:
        return ""
    arr = pings["LocalTime"].values
    idx = int(np.searchsorted(arr, np.datetime64(ts), side="right") - 1)
    if idx < 0:
        return ""
    return re.sub(r"\s+", "", str(pings.iloc[idx].get("Location_Postcode") or "")).upper()


def _trip_num(trip_id: str) -> int:
    """Return numeric part of 'Trip-NNNNNN' for ordering, or 0."""
    m = re.search(r"(\d+)$", trip_id)
    return int(m.group(1)) if m else 0


def build_manifests(
    d_start: date,
    d_end: date,
    vehicle_names: list[str],
    overall_start: pd.Timestamp,
    step_td: pd.Timedelta,
    n_steps: int,
    veh_day_pings: dict,  # {day_idx: {vname: DataFrame}}
    days: list[date],
    depots: list,
) -> dict[str, list[dict]]:
    """Build per-vehicle trip manifests from qargo data.

    Returns {vehicle_name: [trip, ...]} where each trip is:
      {trip_id, load_step, is_export, orders: [{id, dest_step, postcode}]}

    Trip identity rule: the trip ID shared across multiple orders for the same
    vehicle = our vehicle's trip. Unique trip IDs are Palletline/per-consignment
    legs (ignored). Single-order fallback: first trip ID.

    Direction & timestamp:
      Palletline export  → origin_timestamp_local / origin_postal_code;
                           orders stay on manifest until end of day.
      Palletline import / direct single-vehicle
                         → destination_timestamp_local / destination_postal_code;
                           orders tick off individually as delivered.
      Direct multi-vehicle (both trip IDs shared, no Palletline subcontractor):
        smaller Trip-N = collection leg (origin_timestamp, origin_postcode);
        larger  Trip-M = delivery leg  (destination_timestamp, destination_postcode).
        Vehicle assignment: check which vehicle's telematics postcode at
        origin_timestamp matches origin_postcode → that vehicle did the collection.
        Remaining vehicle(s) get the delivery leg.
        Fallback if telematics inconclusive: resource_tractor → collection,
        resource_rigid → delivery.
    """
    from collections import defaultdict, Counter

    # ── Load qargo Excel(s) ──────────────────────────────────────────────────
    months_seen: set[str] = set()
    qargo_frames: list[pd.DataFrame] = []
    for d in [d_start + timedelta(days=i) for i in range((d_end - d_start).days + 1)]:
        key = f"{d.year}-{d.month:02d}"
        if key in months_seen:
            continue
        months_seen.add(key)
        path = _month_qargo_path(d)
        if not path.exists():
            print(f"   [manifest] qargo file not found: {path.name} — skipping")
            continue
        print(f"   [manifest] loading {path.name}")
        try:
            df = pd.read_excel(path, engine="openpyxl")
        except Exception as e:
            print(f"   [manifest] could not read {path.name}: {e}")
            continue
        qargo_frames.append(df)

    if not qargo_frames:
        return {}

    qargo = pd.concat(qargo_frames, ignore_index=True)

    if "status" in qargo.columns:
        qargo = qargo[~qargo["status"].str.strip().str.lower().isin(_EXCLUDE_STATUSES)]

    dest_col = "destination_timestamp_local"
    orig_col = "origin_timestamp_local"
    if dest_col not in qargo.columns:
        print("   [manifest] destination_timestamp_local column missing — skipping manifests")
        return {}
    qargo[dest_col] = pd.to_datetime(qargo[dest_col], errors="coerce")
    has_orig = orig_col in qargo.columns
    if has_orig:
        qargo[orig_col] = pd.to_datetime(qargo[orig_col], errors="coerce")

    veh_set = set(vehicle_names)
    n_days = len(days)
    date_end_ts = overall_start + pd.Timedelta(days=n_days)

    # ── Palletline hub departure steps (for artic load events) ───────────────
    pl_depot = next((d for d in depots if d.kind == "palletline"), None)
    pl_dep_steps: dict[str, list[int]] = {v: [] for v in vehicle_names}
    if pl_depot is not None:
        _BBOX = 0.05
        for _di, day_pings_by_veh in veh_day_pings.items():
            for vname, veh_pings in day_pings_by_veh.items():
                if vname not in veh_set:
                    continue
                if not (
                    (veh_pings["Latitude"].between(pl_depot.lat - _BBOX, pl_depot.lat + _BBOX)) &
                    (veh_pings["Longitude"].between(pl_depot.lon - _BBOX, pl_depot.lon + _BBOX))
                ).any():
                    continue
                for visit in frd.depot_visits(veh_pings, pl_depot):
                    if visit.departed is not None:
                        dep_step = int(max(0, min(n_steps - 1,
                            (visit.departed - overall_start) / step_td)))
                        pl_dep_steps[vname].append(dep_step)

    # ── Pass 1: collect order entries per vehicle ─────────────────────────────
    # Each entry: {
    #   "row": row, "trip_ids": [...], "is_export": bool,
    #   "forced_trip": str|None,   # set for direct multi-vehicle split
    #   "forced_role": str|None,   # "collection" | "delivery"
    # }
    veh_entries: dict[str, list[dict]] = defaultdict(list)

    for _, row in qargo.iterrows():
        subcon = str(row.get("resource_subcontractor") or "").lower()
        is_pl_export = "export" in subcon
        is_pl_import = "import" in subcon
        is_palletline = is_pl_export or is_pl_import

        trip_ids = [p.strip() for p in re.split(r"[|,]", str(row.get("shipment_names") or "")) if p.strip()]
        if not trip_ids:
            continue

        # Gather fleet vehicles from both resource columns (order: tractor then rigid)
        tractors = [r for r in _split_regs(row.get("resource_tractor")) if r in veh_set] if "resource_tractor" in qargo.columns else []
        rigids   = [r for r in _split_regs(row.get("resource_rigid"))   if r in veh_set] if "resource_rigid"   in qargo.columns else []
        all_vehs = list(dict.fromkeys(tractors + rigids))
        if not all_vehs:
            continue

        # Date-range filter using the relevant leg's timestamp
        if is_pl_export and has_orig:
            ref_ts = row.get(orig_col)
        else:
            ref_ts = row.get(dest_col)
        if pd.isna(ref_ts):
            continue
        ref_ts = pd.Timestamp(ref_ts)
        if not (overall_start <= ref_ts < date_end_ts):
            continue

        # ── Direct multi-vehicle: split into collection + delivery legs ────
        if not is_palletline and len(all_vehs) > 1 and len(trip_ids) >= 2:
            sorted_trips = sorted(trip_ids, key=_trip_num)
            collect_trip  = sorted_trips[0]
            delivery_trip = sorted_trips[-1]

            orig_ts  = row.get(orig_col)  if has_orig else None
            orig_pc  = re.sub(r"\s+", "", str(row.get("origin_postal_code") or "")).upper()

            # Telematics postcode match to find collection vehicle
            collect_vehs:  list[str] = []
            delivery_vehs: list[str] = []
            if not pd.isna(orig_ts):
                orig_ts_p = pd.Timestamp(orig_ts)
                for v in all_vehs:
                    veh_pc = _veh_pc_at(orig_ts_p, v, veh_day_pings, n_days, overall_start)
                    if veh_pc and veh_pc == orig_pc:
                        collect_vehs.append(v)
                    else:
                        delivery_vehs.append(v)

            # Fallback: tractor → collection, rigid → delivery
            if not collect_vehs:
                collect_vehs  = tractors if tractors else all_vehs
                delivery_vehs = rigids   if rigids   else []

            for v in collect_vehs:
                veh_entries[v].append({"row": row, "trip_ids": [collect_trip],
                                       "is_export": False, "forced_role": "collection"})
            for v in delivery_vehs:
                veh_entries[v].append({"row": row, "trip_ids": [delivery_trip],
                                       "is_export": False, "forced_role": "delivery"})
            continue

        # ── Normal: single vehicle or Palletline leg ──────────────────────
        for veh in all_vehs:
            veh_entries[veh].append({"row": row, "trip_ids": trip_ids,
                                     "is_export": is_pl_export, "forced_role": None})

    # ── Pass 2: identify shared trips, build manifest per vehicle ─────────────
    manifests: dict[str, list[dict]] = {}

    for vname, entries in veh_entries.items():
        # Frequency count to find our vehicle's trips (shared = ours)
        trip_count: Counter = Counter()
        for e in entries:
            for tid in e["trip_ids"]:
                trip_count[tid] += 1
        our_trips: set[str] = {tid for tid, cnt in trip_count.items() if cnt > 1}

        # Group orders: key = (trip_id, is_export, role)
        trip_order_map: dict[tuple, list] = defaultdict(list)
        for e in entries:
            row, trip_ids, is_export, role = e["row"], e["trip_ids"], e["is_export"], e["forced_role"]
            our_tid = next((t for t in trip_ids if t in our_trips), None)
            if our_tid is None:
                our_tid = trip_ids[0] if trip_ids else "unknown"
            effective_role = role or ("collection" if is_export else "delivery")
            trip_order_map[(our_tid, is_export, effective_role)].append(row)

        veh_manifest: list[dict] = []
        for (trip_id, is_export, role), rows in trip_order_map.items():
            orders_out: list[dict] = []
            for r in rows:
                if role == "collection":
                    ts     = r.get(orig_col) if has_orig else None
                    pc_raw = str(r.get("origin_postal_code") or "")
                    tick_step = n_steps - 1   # collection stays on manifest all day
                else:
                    ts     = r.get(dest_col)
                    pc_raw = str(r.get("destination_postal_code") or "")
                    tick_step = None

                if pd.isna(ts):
                    continue
                ts = pd.Timestamp(ts)
                actual_step = int(max(0, min(n_steps - 1, (ts - overall_start) / step_td)))
                if tick_step is None:
                    tick_step = actual_step

                pc = re.sub(r"\s+", "", pc_raw).upper()
                pc = _FC_CODE_ALIASES.get(pc, pc)  # resolve FC/hub codes to real postcodes
                if pc in _NON_STANDARD_PCS:
                    pc = pc_raw

                orders_out.append({
                    "id":           str(r.get("name") or r.get("order_id") or ""),
                    "dest_step":    tick_step,
                    "_actual_step": actual_step,  # for load_step calc; stripped before export
                    "postcode":     pc,
                    "is_export":    is_export or role == "collection",
                })

            if not orders_out:
                continue

            orders_sorted = sorted(orders_out, key=lambda o: o["_actual_step"])
            veh_manifest.append({
                "trip_id":   trip_id,
                "is_export": is_export or role == "collection",
                "load_step": None,
                "orders":    orders_sorted,
            })

        manifests[vname] = veh_manifest

    # ── Assign load_step, strip internal fields ───────────────────────────────
    for vname, trips in manifests.items():
        dep_steps = sorted(pl_dep_steps.get(vname, []))
        for trip in trips:
            orders_sorted = trip["orders"]
            if not orders_sorted:
                continue
            first_actual = orders_sorted[0]["_actual_step"]

            if trip["is_export"]:
                # Load step = actual collection/origin timestamp
                trip["load_step"] = first_actual
            elif dep_steps:
                candidates = [s for s in dep_steps if s <= first_actual]
                trip["load_step"] = candidates[-1] if candidates else 0
            else:
                trip["load_step"] = max(0, first_actual - int(pd.Timedelta(hours=2) / step_td))

            for o in orders_sorted:
                o.pop("_actual_step", None)

    return manifests


def _daily_route_cap(n_vehicles: int) -> int:
    """Route points per vehicle per day. Scales down for large fleets."""
    return max(200, min(400, 20000 // max(1, n_vehicles)))


# ---------------------------------------------------------------------------
# Data builder
# ---------------------------------------------------------------------------

def build_replay_data(d_start: date, d_end: date, vehicle_names: list[str], step_minutes: int) -> dict:
    """Serialise one or more days of telematics into a JSON-ready dict for the HTML template."""
    days = [d_start + timedelta(days=i) for i in range((d_end - d_start).days + 1)]
    day_dfs = [frd.load_day(d) for d in days]
    day_df = pd.concat(day_dfs, ignore_index=True) if len(day_dfs) > 1 else day_dfs[0]
    meta_df = frd.load_vehicles()
    depots = frd.load_depots()

    overall_start = pd.Timestamp(d_start)
    n_steps = len(days) * 24 * 60 // step_minutes
    steps_ts = pd.date_range(start=overall_start, periods=n_steps, freq=f"{step_minutes}min")
    # Single-day keeps HH:MM; multi-day adds day abbreviation so you know where you are
    if len(days) == 1:
        time_labels = [t.strftime("%H:%M") for t in steps_ts]
    else:
        time_labels = [t.strftime("%a %H:%M") for t in steps_ts]
    step_td = pd.Timedelta(minutes=step_minutes)
    day_boundaries = [int((pd.Timestamp(d) - overall_start) / step_td) for d in days]
    daily_cap = _daily_route_cap(len(vehicle_names))
    # Gap cut scales with sampling density so sparse daily caps don't sever trips.
    # 3× expected inter-point gap, clamped to [15, 90] minutes.
    expected_gap_min = (24 * 60) / max(1, daily_cap)
    _GAP_CUT = pd.Timedelta(minutes=max(15, min(90, int(3 * expected_gap_min))))

    # ── Vehicle data ────────────────────────────────────────────────────────
    vehicles_out: dict[str, dict] = {}
    for name in vehicle_names:
        pings = (
            day_df[day_df["AssetName"] == name]
            .sort_values("LocalTime")
            .reset_index(drop=True)
        )
        if pings.empty:
            continue

        meta_row = meta_df[meta_df["AssetName"] == name]
        asset_type = str(meta_row.iloc[0]["AssetType"]) if not meta_row.empty else "Unknown"
        circuit = str(meta_row.iloc[0].get("CircuitName", "") or "") if not meta_row.empty else ""
        color = COLOR_BY_TYPE.get(asset_type, DEFAULT_COLOR)

        # Route: subsample per day so each day gets a fair share of points.
        # A flat subsample across the whole period gives overnight parked pings
        # most of the budget, leaving actual trips with almost no points.
        route_parts = []
        for ddf in day_dfs:
            dp = ddf[ddf["AssetName"] == name].sort_values("LocalTime").reset_index(drop=True)
            if dp.empty:
                continue
            if len(dp) > daily_cap:
                sel = np.linspace(0, len(dp) - 1, daily_cap, dtype=int)
                route_parts.append(dp.iloc[sel])
            else:
                route_parts.append(dp)
        if not route_parts:
            continue
        route_pings = pd.concat(route_parts).reset_index(drop=True)

        # Map each route ping to a step index so JS can do binary-search cuts
        route_step_indices = (
            ((route_pings["LocalTime"] - overall_start) / step_td)
            .astype(int)
            .clip(0, n_steps - 1)
            .tolist()
        )

        # Mark gaps > _GAP_CUT between consecutive route pings so JS splits
        # the polyline — prevents straight lines across cities or overnight.
        times_arr = route_pings["LocalTime"].values
        gap_after = [False] * len(route_pings)
        for _i in range(len(times_arr) - 1):
            if pd.Timestamp(times_arr[_i + 1]) - pd.Timestamp(times_arr[_i]) > _GAP_CUT:
                gap_after[_i] = True

        # Positions stored as parallel arrays (not per-step objects) so JSON is
        # ~3× smaller: no repeated key names, driver/postcode deduplicated by index.
        pings_c = pings.copy()
        pings_c["_step"] = (
            ((pings_c["LocalTime"] - overall_start) / step_td)
            .astype(int)
            .clip(0, n_steps - 1)
        )
        pos_df = pings_c.groupby("_step").last().reset_index()

        drv_keys: list[str] = []
        drv_map:  dict[str, int] = {}
        pc_keys:  list[str] = []
        pc_map:   dict[str, int] = {}
        pos_steps:  list[int]   = []
        pos_lats:   list[float] = []
        pos_lons:   list[float] = []
        pos_igns:   list[int]   = []
        pos_speeds: list[int]   = []
        pos_drvs:   list[int]   = []
        pos_pcs:    list[int]   = []

        for _, r in pos_df.iterrows():
            drv = str(r.get("AssetDriver") or "")
            pc  = str(r.get("Location_Postcode") or "")
            if drv not in drv_map:
                drv_map[drv] = len(drv_keys); drv_keys.append(drv)
            if pc not in pc_map:
                pc_map[pc] = len(pc_keys); pc_keys.append(pc)
            pos_steps.append(int(r["_step"]))
            pos_lats.append(round(float(r["Latitude"]), 4))
            pos_lons.append(round(float(r["Longitude"]), 4))
            pos_igns.append(1 if r.get("Ignition", False) else 0)
            pos_speeds.append(int(round(float(r.get("GPSSpeed") or 0))))
            pos_drvs.append(drv_map[drv])
            pos_pcs.append(pc_map[pc])

        vehicles_out[name] = {
            "color": color,
            "type": asset_type,
            "circuit": circuit,
            "route": {
                "step_indices": route_step_indices,
                "lats": route_pings["Latitude"].tolist(),
                "lons": route_pings["Longitude"].tolist(),
                "gap_after": gap_after,
            },
            "positions": {
                "steps":    pos_steps,
                "lats":     pos_lats,
                "lons":     pos_lons,
                "igns":     pos_igns,
                "speeds":   pos_speeds,
                "drv_keys": drv_keys,
                "drvs":     pos_drvs,
                "pc_keys":  pc_keys,
                "pcs":      pos_pcs,
            },
        }

    # ── Ignition-off stops (ON→OFF transitions) — aggregated by ~100m grid ──
    # Collect raw events first, then cluster so overlapping stops show a count.
    raw_stops: list[dict] = []
    for name in vehicle_names:
        pings = (
            day_df[day_df["AssetName"] == name]
            .sort_values("LocalTime")
            .reset_index(drop=True)
        )
        if pings.empty:
            continue
        ign = pings["Ignition"].astype(bool)
        off_transitions = (~ign) & ign.shift(1, fill_value=False)
        for _, r in pings[off_transitions].iterrows():
            raw_stops.append({
                "vehicle": name,
                "lat": float(r["Latitude"]),
                "lon": float(r["Longitude"]),
                "ts": r["LocalTime"],               # full timestamp for step calc
                "time": r["LocalTime"].strftime("%a %H:%M") if len(days) > 1 else r["LocalTime"].strftime("%H:%M"),
                "postcode": str(r.get("Location_Postcode") or ""),
            })

    # Group by 3-decimal-place grid (≈70-110 m cells); compute centroid + count.
    grid: dict[tuple, list] = {}
    for s in raw_stops:
        key = (round(s["lat"], 3), round(s["lon"], 3))
        grid.setdefault(key, []).append(s)

    stops_out: list[dict] = []
    for cell in grid.values():
        lat = sum(s["lat"] for s in cell) / len(cell)
        lon = sum(s["lon"] for s in cell) / len(cell)
        sorted_cell = sorted(cell, key=lambda x: x["ts"])
        times = [s["time"] for s in sorted_cell]
        # Per-vehicle stop counts so JS can recompute visible count when filter changes
        vehicle_counts: dict[str, int] = {}
        for s in cell:
            vehicle_counts[s["vehicle"]] = vehicle_counts.get(s["vehicle"], 0) + 1
        earliest_ts = min(s["ts"] for s in cell)
        earliest_step = int(max(0, min(n_steps - 1, (earliest_ts - overall_start) / step_td)))
        stops_out.append({
            "lat": lat,
            "lon": lon,
            "vehicle_counts": vehicle_counts,
            "times": times,
            "postcode": cell[0]["postcode"],
            "first_step": earliest_step,
        })

    # ── Depot data — visits stored per day for the paginated popup ──────────
    # Pre-index vehicle pings per day to avoid repeated DataFrame filtering.
    _veh_day_pings: dict[int, dict[str, pd.DataFrame]] = {}
    for _di, _ddf in enumerate(day_dfs):
        _veh_day_pings[_di] = {}
        for _vn in vehicle_names:
            _sub = _ddf[_ddf["AssetName"] == _vn]
            if not _sub.empty:
                _veh_day_pings[_di][_vn] = _sub

    depots_out: list[dict] = []
    for depot in depots:
        _BBOX = 0.05   # ~5.5 km generous buffer; eliminates most depot_visits calls
        visits_by_day: dict[str, list] = {}
        for _di, (d, ddf) in enumerate(zip(days, day_dfs)):
            day_visits: list[dict] = []
            for vname in vehicle_names:
                veh_pings = _veh_day_pings[_di].get(vname)
                if veh_pings is None:
                    continue
                # Skip expensive haversine if no ping is within bbox of this depot
                if not (
                    (veh_pings["Latitude"].between(depot.lat - _BBOX, depot.lat + _BBOX)) &
                    (veh_pings["Longitude"].between(depot.lon - _BBOX, depot.lon + _BBOX))
                ).any():
                    continue
                for v in frd.depot_visits(veh_pings, depot):
                    dwell_str = None
                    if v.dwell is not None:
                        h = int(v.dwell.total_seconds() // 3600)
                        m = int((v.dwell.total_seconds() % 3600) // 60)
                        dwell_str = f"{h}h{m:02d}m"
                    day_visits.append({
                        "vehicle": v.vehicle,
                        "arrived": f"{v.arrived:%H:%M}" if v.arrived else None,
                        "departed": f"{v.departed:%H:%M}" if v.departed else None,
                        "dwell": dwell_str,
                        "is_stop": v.is_stop,
                    })
            visits_by_day[d.isoformat()] = day_visits

        depots_out.append({
            "name": depot.name,
            "lat": depot.lat,
            "lon": depot.lon,
            "kind": depot.kind,
            "visits_by_day": visits_by_day,
        })

    # ── Manifests — per-vehicle trip/order schedule from qargo ──────────────
    print("Building order manifests from qargo…")
    manifests = build_manifests(
        d_start, d_end, vehicle_names,
        overall_start, step_td, n_steps,
        _veh_day_pings, days, depots,
    )
    for vname, trips in manifests.items():
        if vname in vehicles_out:
            vehicles_out[vname]["manifest"] = trips

    return {
        "date_start": d_start.isoformat(),
        "date_end": d_end.isoformat(),
        "dates": [d.isoformat() for d in days],
        "day_boundaries": day_boundaries,
        "step_minutes": step_minutes,
        "time_steps": time_labels,
        "vehicles": vehicles_out,
        "depots": depots_out,
        "stops": stops_out,
    }


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Fleet Replay — {date_range}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { display: flex; flex-direction: column; height: 100vh; font-family: system-ui, -apple-system, sans-serif; font-size: 13px; background: #111; }
#main { display: flex; flex: 1; overflow: hidden; min-height: 0; }

/* ── Sidebar ── */
#sidebar {
  width: 240px; flex-shrink: 0; min-height: 0;
  background: #1a1d2e; color: #ddd;
  display: flex; flex-direction: column; overflow: hidden;
  box-shadow: 2px 0 8px rgba(0,0,0,.5);
}
#sidebar-header { padding: 14px 14px 10px; border-bottom: 1px solid #2a2e45; }
#sidebar-header h2 { font-size: 14px; font-weight: 600; color: #fff; }
#sidebar-header .sub { font-size: 11px; color: #666; margin-top: 2px; }

/* ── Controls ── */
#controls { padding: 12px 14px; border-bottom: 1px solid #2a2e45; }
.ctrl-label { font-size: 10px; color: #666; text-transform: uppercase; letter-spacing: .6px; margin-bottom: 3px; }
.time-display { font-size: 28px; font-weight: 700; color: #fff; letter-spacing: 3px; margin-bottom: 8px; font-variant-numeric: tabular-nums; }
#timeline { width: 100%; margin: 4px 0 8px; accent-color: #4a9eff; cursor: pointer; }
.btn-row { display: flex; gap: 5px; margin-bottom: 10px; }
button {
  flex: 1; padding: 6px 0; border: none; border-radius: 5px;
  background: #252840; color: #ccc; cursor: pointer; font-size: 12px;
  transition: background .15s;
}
button:hover { background: #2e3460; color: #fff; }
button.active { background: #4a9eff; color: #fff; }
.speed-row { display: flex; align-items: center; gap: 6px; }
.speed-row label { font-size: 10px; color: #666; white-space: nowrap; text-transform: uppercase; letter-spacing: .5px; }
#speed { flex: 1; accent-color: #4a9eff; cursor: pointer; }
#speed-val { font-size: 11px; color: #fff; min-width: 36px; text-align: right; font-variant-numeric: tabular-nums; }

/* ── Postcode search ── */
#pin-section { padding: 10px 14px; border-bottom: 1px solid #2a2e45; }
#pin-input-row { display: flex; gap: 5px; }
#pin-input {
  flex: 1; background: #252840; border: 1px solid #333; border-radius: 4px;
  color: #fff; font-size: 12px; padding: 5px 8px;
}
#pin-input::placeholder { color: #555; }
#pin-btn { flex-shrink: 0; padding: 5px 10px; border-radius: 4px; font-size: 12px; }
#pin-status { font-size: 10px; color: #888; margin-top: 4px; min-height: 14px; }

/* ── Filters ── */
#filter-section { padding: 10px 14px; border-bottom: 1px solid #2a2e45; }
#filter-section h3 { font-size: 10px; color: #666; margin-bottom: 6px; text-transform: uppercase; letter-spacing: .6px; }
.filter-row { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 5px; }
.filter-chip {
  flex: none; padding: 3px 7px; border-radius: 10px; font-size: 10px;
  background: #252840; color: #aaa; border: 1px solid #333; cursor: pointer;
  transition: background .12s, color .12s;
}
.filter-chip:hover { background: #2e3460; color: #fff; }
.filter-chip.active { background: #4a9eff; color: #fff; border-color: #4a9eff; }
.filter-all-row { display: flex; gap: 4px; margin-bottom: 6px; }
.filter-all-row button { flex: 1; padding: 4px 0; font-size: 11px; border-radius: 4px; }

/* ── Legend ── */
#legend { padding: 10px 14px; flex: 1; overflow-y: auto; border-bottom: 1px solid #2a2e45; }
#legend h3 { font-size: 10px; color: #666; margin-bottom: 6px; text-transform: uppercase; letter-spacing: .6px; }
.veh-item { display: flex; align-items: center; gap: 7px; padding: 3px 0; cursor: default; }
.veh-item input[type=checkbox] { cursor: pointer; accent-color: var(--vc); flex-shrink: 0; }
.veh-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.veh-name { font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #ccc; }

/* ── Depots ── */
#depot-panel { padding: 10px 14px; }
#depot-panel h3 { font-size: 10px; color: #666; margin-bottom: 6px; text-transform: uppercase; letter-spacing: .6px; }
.depot-item { padding: 4px 0; border-bottom: 1px solid #222; cursor: pointer; font-size: 11px; color: #aaa; }
.depot-item:hover { color: #fff; }
.depot-dot { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 5px; vertical-align: middle; }

/* ── Stops toggle ── */
#stops-toggle { padding: 8px 14px; border-top: 1px solid #2a2e45; display: flex; align-items: center; gap: 7px; }
#stops-toggle label { font-size: 11px; color: #aaa; cursor: pointer; display: flex; align-items: center; gap: 7px; }
#stops-toggle input { accent-color: #8e44ad; cursor: pointer; }
.stop-x { display: inline-block; width: 10px; height: 10px; position: relative; flex-shrink: 0; }
.stop-x::before, .stop-x::after {
  content: ''; position: absolute; background: #8e44ad; border-radius: 1px;
  width: 2px; height: 10px; left: 4px; top: 0;
}
.stop-x::after { transform: rotate(90deg); }

/* ── Keyboard hint ── */
#kb-hint { padding: 8px 14px; font-size: 10px; color: #444; border-top: 1px solid #2a2e45; }

/* ── Map ── */
#map { flex: 1; }

/* ── Bottom timeline bar ── */
#timeline-bar {
  background: #1a1d2e; border-top: 2px solid #2a2e45;
  padding: 6px 24px 10px; flex-shrink: 0; user-select: none;
}
#tl-ticks { position: relative; height: 30px; margin-bottom: 2px; }
.day-tick {
  position: absolute; transform: translateX(-50%);
  display: flex; flex-direction: column; align-items: center;
  pointer-events: none;
}
.day-tick .dn { font-size: 9px; color: #555; line-height: 1.3; text-transform: uppercase; letter-spacing: .3px; }
.day-tick .dd { font-size: 12px; font-weight: 700; color: #888; line-height: 1.2; }
.day-tick .dl { width: 1px; height: 6px; background: #3a3e55; margin-top: 3px; }
.day-tick.weekend .dn, .day-tick.weekend .dd { color: #5b8ecf; }
.day-tick.today .dn, .day-tick.today .dd { color: #4a9eff; }
#timeline {
  width: 100%; accent-color: #4a9eff; cursor: pointer;
  height: 6px; -webkit-appearance: none; appearance: none;
  background: transparent;
}
#timeline::-webkit-slider-runnable-track {
  height: 4px; border-radius: 2px; background: #2a2e45;
}
#timeline::-webkit-slider-thumb {
  -webkit-appearance: none; width: 16px; height: 16px;
  border-radius: 50%; background: #4a9eff; border: 2px solid #fff;
  box-shadow: 0 1px 4px rgba(0,0,0,.5); margin-top: -6px; cursor: pointer;
}
#timeline::-moz-range-track { height: 4px; border-radius: 2px; background: #2a2e45; }
#timeline::-moz-range-thumb {
  width: 16px; height: 16px; border-radius: 50%;
  background: #4a9eff; border: 2px solid #fff; cursor: pointer;
}

/* ── Leaflet popup ── */
.leaflet-popup-content { font-size: 12px; line-height: 1.5; max-height: 320px; overflow-y: auto; }
.leaflet-popup-content-wrapper { border-radius: 6px; }
.popup-table { border-collapse: collapse; width: 100%; margin-top: 6px; font-size: 11px; }
.popup-table th { background: #f0f4ff; padding: 3px 7px; font-weight: 600; text-align: left; }
.popup-table td { padding: 3px 7px; }
.popup-table tr:nth-child(even) td { background: #f8f8f8; }
</style>
</head>
<body>

<div id="main">
<div id="sidebar">
  <div id="sidebar-header">
    <h2>Fleet Replay</h2>
    <div class="sub" id="date-label"></div>
  </div>

  <div id="controls">
    <div class="ctrl-label">Time</div>
    <div class="time-display" id="time-display">00:00</div>
    <div class="btn-row">
      <button id="btn-prev" title="Step back (←)">◀</button>
      <button id="btn-play" title="Play / Pause (Space)">▶ Play</button>
      <button id="btn-next" title="Step forward (→)">▶</button>
    </div>
    <div class="btn-row">
      <button id="btn-daily" class="active" title="Show only today's route and stops" style="flex:1;font-size:10px">Daily</button>
      <button id="btn-accum" title="Accumulate all routes and stops since day 1" style="flex:1;font-size:10px">All time</button>
    </div>
    <div class="speed-row">
      <label>Speed</label>
      <input type="range" id="speed" min="1" max="30" value="4" step="1">
      <span id="speed-val">4 fps</span>
    </div>
  </div>

  <div id="pin-section">
    <div class="ctrl-label">Pin postcode</div>
    <div id="pin-input-row">
      <input type="text" id="pin-input" placeholder="e.g. IP6 0LW">
      <button id="pin-btn">Pin</button>
    </div>
    <div id="pin-status"></div>
  </div>

  <div id="filter-section">
    <h3>Filter vehicles</h3>
    <div class="filter-all-row">
      <button id="btn-all">All</button>
      <button id="btn-none">None</button>
    </div>
    <div id="filter-type" class="filter-row"></div>
    <div id="filter-circuit" class="filter-row"></div>
  </div>

  <div id="legend">
    <h3>Vehicles</h3>
    <div id="veh-list"></div>
  </div>

  <div id="depot-panel">
    <h3>Depots</h3>
    <div id="depot-list"></div>
  </div>

  <div id="stops-toggle">
    <label>
      <input type="checkbox" id="show-stops" checked>
      <span class="stop-x"></span>
      Engine-off stops
    </label>
  </div>

  <div id="kb-hint">Space: play/pause &nbsp; ← →: step</div>
</div>

<div id="map"></div>
</div><!-- #main -->

<div id="timeline-bar">
  <div id="tl-ticks"></div>
  <input type="range" id="timeline" min="0" step="1" value="0">
</div>

<script>
const DATA = __REPLAY_DATA__;

// ── Map init ───────────────────────────────────────────────────────────────
const map = L.map('map', { preferCanvas: true }).setView([52.2, -0.5], 9);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 19,
}).addTo(map);

// ── State ──────────────────────────────────────────────────────────────────
let stepIdx = 0;
const nSteps = DATA.time_steps.length;
let playing = false;
let playTimer = null;
let fps = 4;
let followVehicle = null;
let lastDayIdx = -1;

// ── DOM ────────────────────────────────────────────────────────────────────
const timeDisplay = document.getElementById('time-display');
const timeline    = document.getElementById('timeline');
const btnPlay     = document.getElementById('btn-play');
const btnPrev     = document.getElementById('btn-prev');
const btnNext     = document.getElementById('btn-next');
const speedSlider = document.getElementById('speed');
const speedVal    = document.getElementById('speed-val');
const dateLabel   = document.getElementById('date-label');
const vehListEl   = document.getElementById('veh-list');
const depotListEl = document.getElementById('depot-list');
const pinInput    = document.getElementById('pin-input');
const pinBtn      = document.getElementById('pin-btn');
const pinStatus   = document.getElementById('pin-status');

timeline.max = nSteps - 1;
const dateRange = DATA.date_start === DATA.date_end ? DATA.date_start : `${DATA.date_start} → ${DATA.date_end}`;
dateLabel.textContent = dateRange + (DATA.step_minutes > 1 ? ` · ${DATA.step_minutes}-min steps` : '');

// ── Day tick marks on the bottom timeline ──────────────────────────────────
(function buildTicks() {
  const ticksEl = document.getElementById('tl-ticks');
  if (DATA.dates.length <= 1) return; // single day — no ticks needed
  const total = nSteps - 1;
  DATA.dates.forEach((dateStr, i) => {
    const boundaryStep = DATA.day_boundaries[i];
    const pct = (boundaryStep / total) * 100;
    const d = new Date(dateStr + 'T00:00:00');
    const dow = d.getDay(); // 0=Sun, 6=Sat
    const isWeekend = dow === 0 || dow === 6;
    const dayName = d.toLocaleDateString('en-GB', { weekday: 'short' }); // Mon
    const dayNum  = d.getDate();                                          // 7
    const tick = document.createElement('div');
    tick.className = 'day-tick' + (isWeekend ? ' weekend' : '');
    tick.style.left = pct + '%';
    tick.innerHTML =
      `<span class="dn">${dayName}</span>` +
      `<span class="dd">${dayNum}</span>` +
      `<div class="dl"></div>`;
    ticksEl.appendChild(tick);
  });
})();

// ── Helpers ────────────────────────────────────────────────────────────────
function upperBound(arr, val) {
  let lo = 0, hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    arr[mid] <= val ? (lo = mid + 1) : (hi = mid);
  }
  return lo;
}

function lowerBound(arr, val) {
  let lo = 0, hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    arr[mid] < val ? (lo = mid + 1) : (hi = mid);
  }
  return lo;
}

// Daily vs accumulated mode — daily shows only current day's route/stops.
let dailyMode = true;

// Track last stop-cluster rebuild state so we only rebuild when new stops activate.
let _stopRebuildMax = -1;
let _stopRebuildDay = -1;

function maybeRebuildStops(idx) {
  if (!stopsShowing) return;
  const dayIdx = currentDayIdx(idx);
  if (dayIdx !== _stopRebuildDay) {
    // Day changed — always rebuild (daily mode clears old stops)
    rebuildStopCluster();
    _stopRebuildMax = idx;
    _stopRebuildDay = dayIdx;
    return;
  }
  if (idx <= _stopRebuildMax) return;  // stepped backward or same — no new stops possible
  // Check if any stop crossed its activation threshold since last rebuild
  let hasNew = false;
  for (let i = 0; i < allStopMarkers.length; i++) {
    const fs = allStopMarkers[i]._firstStep;
    if (fs > _stopRebuildMax && fs <= idx) { hasNew = true; break; }
  }
  _stopRebuildMax = idx;
  if (hasNew) rebuildStopCluster();
}

document.getElementById('btn-daily').addEventListener('click', () => {
  dailyMode = true;
  document.getElementById('btn-daily').classList.add('active');
  document.getElementById('btn-accum').classList.remove('active');
  _stopRebuildMax = -1; _stopRebuildDay = -1;
  renderStep(stepIdx);
});
document.getElementById('btn-accum').addEventListener('click', () => {
  dailyMode = false;
  document.getElementById('btn-accum').classList.add('active');
  document.getElementById('btn-daily').classList.remove('active');
  _stopRebuildMax = -1; _stopRebuildDay = -1;
  renderStep(stepIdx);
});

function makeCircleIcon(color, size = 12, following = false, badge = 0) {
  const ring = following
    ? `box-shadow:0 0 0 3px ${color},0 0 0 5px white,0 1px 8px rgba(0,0,0,.6)`
    : `box-shadow:0 1px 5px rgba(0,0,0,.5)`;
  const badgeHtml = badge > 0
    ? `<div style="position:absolute;top:-5px;right:-5px;min-width:14px;height:14px;border-radius:7px;background:#e67e22;border:1.5px solid #fff;color:#fff;font-size:9px;font-weight:700;display:flex;align-items:center;justify-content:center;padding:0 2px;line-height:1">${badge < 100 ? badge : '99+'}</div>`
    : '';
  return L.divIcon({
    html: `<div style="position:relative;width:${size}px;height:${size}px">` +
      `<div style="width:${size}px;height:${size}px;border-radius:50%;background:${color};border:2px solid rgba(255,255,255,.9);${ring}"></div>` +
      badgeHtml +
      `</div>`,
    className: '', iconSize: [size, size], iconAnchor: [size/2, size/2],
  });
}

// Return the last recorded position at or before step idx, or null if none yet.
// Positions are stored as parallel arrays; binary search on positions.steps (already sorted).
function lastKnownPos(v, idx) {
  const p = v.positions;
  const steps = p.steps;
  let lo = 0, hi = steps.length - 1, result = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (steps[mid] <= idx) { result = mid; lo = mid + 1; }
    else hi = mid - 1;
  }
  if (result < 0) return null;
  return {
    lat:      p.lats[result],
    lon:      p.lons[result],
    ignition: p.igns[result] === 1,
    speed:    p.speeds[result],
    driver:   p.drv_keys[p.drvs[result]],
    postcode: p.pc_keys[p.pcs[result]],
  };
}

// ── Manifest helpers ───────────────────────────────────────────────────────
// Returns {total, remaining, delivered, trip_id} for a vehicle at a given step,
// or null if the vehicle has no manifest or hasn't loaded yet.
function getManifest(v, idx) {
  if (!v.manifest || v.manifest.length === 0) return null;
  // Find the active trip: load_step <= idx and at least one order's dest_step > idx
  // or the trip whose load_step is closest to idx (most recent loaded).
  let best = null;
  for (const trip of v.manifest) {
    if (trip.load_step === null || trip.load_step > idx) continue;
    if (best === null || trip.load_step > best.load_step) best = trip;
  }
  if (!best) return null;
  const orders = best.orders;
  const delivered = orders.filter(o => o.dest_step <= idx).length;
  return {
    total:     orders.length,
    remaining: best.is_export ? 0 : orders.length - delivered,
    delivered,
    trip_id:   best.trip_id,
    is_export: best.is_export,
    orders,
  };
}

// Build an HTML table of manifest orders for the popup.
function manifestHtml(manifest, idx) {
  if (!manifest) return '';
  const isExport = manifest.is_export;
  const label = isExport ? 'Collections' : 'Deliveries';
  const statusLabel = isExport
    ? `<span style="color:#e67e22">${manifest.total} collected</span>`
    : `<span style="color:#2a9d2a">${manifest.delivered} delivered</span> / ${manifest.total}`;
  const rows = manifest.orders.map(o => {
    const done = o.dest_step <= idx;
    const style = (done && !isExport) ? 'color:#888;text-decoration:line-through' : '';
    const collectedStyle = (isExport && done) ? 'color:#e67e22' : '';
    return `<tr><td style="${style}${collectedStyle}">${o.id}</td><td style="${style}${collectedStyle}">${o.postcode || '—'}</td></tr>`;
  }).join('');
  return `<br><hr style="margin:5px 0;border-color:#ddd">` +
    `<b style="font-size:11px">${label} · ${manifest.trip_id || '—'} · ${statusLabel}</b>` +
    `<table class="popup-table" style="margin-top:4px">` +
    `<tr><th>Order</th><th>Postcode</th></tr>${rows}</table>`;
}

// ── Vehicle layers ─────────────────────────────────────────────────────────
const vehicleVisible = {};
const routeLines = {};
const posMarkers = {};
const vehicles = DATA.vehicles;
const vehNames = Object.keys(vehicles);

vehNames.forEach(name => {
  const v = vehicles[name];
  vehicleVisible[name] = true;

  routeLines[name] = L.polyline([], {
    color: v.color, weight: 2.5, opacity: 0.45,
  }).addTo(map);

  posMarkers[name] = L.marker([0, 0], {
    icon: makeCircleIcon(v.color, 14),
    opacity: 0,
    zIndexOffset: 200,
  }).bindPopup('', { maxWidth: 260 }).addTo(map);

  posMarkers[name].on('click', () => {
    followVehicle = (followVehicle === name) ? null : name;
    renderStep(stepIdx);
  });

  // Legend row
  const item = document.createElement('div');
  item.className = 'veh-item';
  item.style.setProperty('--vc', v.color);
  item.innerHTML =
    `<input type="checkbox" checked data-veh="${name}">` +
    `<div class="veh-dot" style="background:${v.color}"></div>` +
    `<span class="veh-name" title="${name} · ${v.type}${v.circuit ? ' · ' + v.circuit : ''}">${name}</span>`;
  item.querySelector('input').addEventListener('change', e => {
    vehicleVisible[name] = e.target.checked;
    if (!e.target.checked) {
      routeLines[name].setLatLngs([]);
      posMarkers[name].setOpacity(0);
      rebuildStopCluster();
    } else {
      renderStep(stepIdx);
    }
    syncFilterChips();
  });
  vehListEl.appendChild(item);

});

// ── Filter helpers ─────────────────────────────────────────────────────────
function shortCircuit(c) {
  return c
    .replace('*Recently Released Vehicles', 'Recent')
    .replace('*Subscription Expired', 'Expired')
    .replace('Bedford', 'Bed').replace('Duxford', 'Dux').replace('St Ives', 'StI')
    .replace(' - Artic', '-Art').replace(' - Rigid', '-Rig').replace(' - Service', '-Svc');
}

function setVehicleVisible(name, checked) {
  vehicleVisible[name] = checked;
  const cb = document.querySelector(`input[data-veh="${name}"]`);
  if (cb) cb.checked = checked;
  if (!checked) { routeLines[name].setLatLngs([]); posMarkers[name].setOpacity(0); }
}

function applyAll(checked) {
  vehNames.forEach(n => setVehicleVisible(n, checked));
  if (checked) renderStep(stepIdx); else rebuildStopCluster();
  syncFilterChips();
}

function toggleGroup(pred) {
  const group = vehNames.filter(pred);
  const allOn = group.every(n => vehicleVisible[n]);
  group.forEach(n => setVehicleVisible(n, !allOn));
  if (!allOn) renderStep(stepIdx); else rebuildStopCluster();
  syncFilterChips();
}

function syncFilterChips() {
  document.querySelectorAll('.filter-chip[data-type]').forEach(btn => {
    const t = btn.dataset.type;
    const group = vehNames.filter(n => vehicles[n].type === t);
    btn.classList.toggle('active', group.length > 0 && group.every(n => vehicleVisible[n]));
  });
  document.querySelectorAll('.filter-chip[data-circuit]').forEach(btn => {
    const c = btn.dataset.circuit;
    const group = vehNames.filter(n => vehicles[n].circuit === c);
    btn.classList.toggle('active', group.length > 0 && group.every(n => vehicleVisible[n]));
  });
}

// Build filter chips from data present in this export
(function buildFilters() {
  const typeEl    = document.getElementById('filter-type');
  const circuitEl = document.getElementById('filter-circuit');

  const typeLabels = {
    'Tractor Unit': 'Artic', 'Lorry': 'Lorry',
    'Rigid Truck': 'Rigid', 'Mini Truck': 'Mini', 'Service Van': 'Van',
  };
  const types    = [...new Set(vehNames.map(n => vehicles[n].type))].sort();
  const circuits = [...new Set(vehNames.map(n => vehicles[n].circuit).filter(Boolean))].sort();

  types.forEach(t => {
    const btn = document.createElement('button');
    btn.className = 'filter-chip active';
    btn.dataset.type = t;
    btn.textContent = typeLabels[t] || t;
    btn.title = t;
    btn.addEventListener('click', () => { toggleGroup(n => vehicles[n].type === t); });
    typeEl.appendChild(btn);
  });

  circuits.forEach(c => {
    const btn = document.createElement('button');
    btn.className = 'filter-chip active';
    btn.dataset.circuit = c;
    btn.textContent = shortCircuit(c);
    btn.title = c;
    btn.addEventListener('click', () => { toggleGroup(n => vehicles[n].circuit === c); });
    circuitEl.appendChild(btn);
  });

  document.getElementById('btn-all').addEventListener('click',  () => applyAll(true));
  document.getElementById('btn-none').addEventListener('click', () => applyAll(false));
})();

// ── Follow mode ────────────────────────────────────────────────────────────
// User dragging the map cancels follow; renderStep will redraw icons without ring
map.on('dragstart', () => { followVehicle = null; });

// ── Depot markers ──────────────────────────────────────────────────────────
const depotMarkerList = [];

function currentDayIdx(idx) {
  const b = DATA.day_boundaries;
  let d = 0;
  for (let i = 1; i < b.length; i++) { if (idx >= b[i]) d = i; else break; }
  return d;
}

function renderDepotPopup(depIdx, dateStr) {
  const dep = DATA.depots[depIdx];
  const marker = depotMarkerList[depIdx];
  const isZF = dep.kind === 'zeefleet';
  const dates = DATA.dates;
  const di = dates.indexOf(dateStr);
  const visits = dep.visits_by_day[dateStr] || [];

  const d = new Date(dateStr + 'T00:00:00');
  const dayLabel = d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });

  const btnBase = 'padding:2px 10px;font-size:11px;border:1px solid #ccc;border-radius:3px;background:#f5f5f5;cursor:pointer';
  const btnDis  = btnBase + ';opacity:.3;cursor:default';
  const canPrev = di > 0, canNext = di < dates.length - 1;

  const navHtml = dates.length > 1
    ? `<div style="display:flex;align-items:center;gap:6px;margin:7px 0 4px">
        <button style="${canPrev ? btnBase : btnDis}"
          ${canPrev ? `onclick="renderDepotPopup(${depIdx},'${dates[di-1]}')"` : 'disabled'}>◀</button>
        <span style="flex:1;text-align:center;font-weight:600;font-size:12px">${dayLabel}</span>
        <button style="${canNext ? btnBase : btnDis}"
          ${canNext ? `onclick="renderDepotPopup(${depIdx},'${dates[di+1]}')"` : 'disabled'}>▶</button>
      </div>`
    : `<div style="font-weight:600;font-size:12px;margin:5px 0 3px">${dayLabel}</div>`;

  const visitsHtml = visits.length === 0
    ? '<em style="color:#888">No visits on this date</em>'
    : `<table class="popup-table">
        <tr><th>Vehicle</th><th>Arr.</th><th>Dep.</th><th>Dwell</th><th>Stop</th></tr>
        ${visits.map(v =>
          `<tr><td>${v.vehicle}</td><td>${v.arrived ?? '—'}</td><td>${v.departed ?? '—'}</td>
           <td>${v.dwell ?? '—'}</td><td>${v.is_stop ? '✓' : '<span style="color:#aaa">thru</span>'}</td></tr>`
        ).join('')}
      </table>`;

  const html =
    `<b>${dep.name}</b><br>
     <span style="color:#888;font-size:11px">${isZF ? 'ZEEFleet depot' : 'Palletline hub'}</span>
     ${navHtml}${visitsHtml}`;

  marker.getPopup().setContent(html);
  if (marker.isPopupOpen()) marker.getPopup().update();
}

DATA.depots.forEach((dep, depIdx) => {
  const isZF = dep.kind === 'zeefleet';
  const bgColor = isZF ? '#c0392b' : '#7f0000';

  const depotIcon = L.divIcon({
    html: `<div style="
      width:22px;height:22px;border-radius:4px;
      background:${bgColor};border:2px solid rgba(255,255,255,.85);
      display:flex;align-items:center;justify-content:center;
      color:#fff;font-weight:700;font-size:12px;
      box-shadow:0 2px 6px rgba(0,0,0,.6)
    ">${isZF ? 'Z' : 'P'}</div>`,
    className: '', iconSize: [22, 22], iconAnchor: [11, 11],
  });

  const marker = L.marker([dep.lat, dep.lon], { icon: depotIcon, zIndexOffset: 1000 })
    .bindPopup('', { maxWidth: 440 })
    .addTo(map);

  depotMarkerList.push(marker);

  // Populate popup with the current replay day when opened
  marker.on('popupopen', () => {
    renderDepotPopup(depIdx, DATA.dates[currentDayIdx(stepIdx)]);
  });

  // Sidebar item — click to fly to depot
  const item = document.createElement('div');
  item.className = 'depot-item';
  item.innerHTML = `<span class="depot-dot" style="background:${bgColor}"></span>${dep.name}`;
  item.addEventListener('click', () => map.flyTo([dep.lat, dep.lon], 14));
  depotListEl.appendChild(item);
});

// ── Render a step ──────────────────────────────────────────────────────────
function renderStep(idx) {
  const label = DATA.time_steps[idx];
  timeDisplay.textContent = label;
  timeline.value = idx;

  // Auto-update depot popup and stop cluster on day change
  const newDayIdx = currentDayIdx(idx);
  if (newDayIdx !== lastDayIdx) {
    lastDayIdx = newDayIdx;
    depotMarkerList.forEach((m, depIdx) => {
      if (m.isPopupOpen()) renderDepotPopup(depIdx, DATA.dates[newDayIdx]);
    });
    if (dailyMode) { rebuildStopCluster(); _stopRebuildMax = idx; _stopRebuildDay = newDayIdx; }
  }

  vehNames.forEach(name => {
    if (!vehicleVisible[name]) return;
    const v = vehicles[name];

    // Growing route — daily mode: current day only; accumulated: all days to idx.
    const dayStart = dailyMode ? DATA.day_boundaries[currentDayIdx(idx)] : 0;
    const cutLo = dailyMode ? lowerBound(v.route.step_indices, dayStart) : 0;
    const cutHi = upperBound(v.route.step_indices, idx);
    const segments = [];
    let seg = [];
    for (let i = cutLo; i < cutHi; i++) {
      seg.push([v.route.lats[i], v.route.lons[i]]);
      if (v.route.gap_after[i]) { segments.push(seg); seg = []; }
    }
    if (seg.length > 0) segments.push(seg);
    routeLines[name].setLatLngs(segments);

    // Position marker — use last known position; red dot when ignition off
    const pos = lastKnownPos(v, idx);
    const manifest = getManifest(v, idx);
    const isFollowed = followVehicle === name;
    if (pos) {
      const markerColor = pos.ignition ? v.color : '#e74c3c';
      const badge = manifest ? manifest.remaining : 0;
      posMarkers[name].setIcon(makeCircleIcon(markerColor, 14, isFollowed, badge));
      posMarkers[name].setLatLng([pos.lat, pos.lon]).setOpacity(1);
      posMarkers[name].setPopupContent(
        `<b>${name}</b>${isFollowed ? ' <span style="color:#4a9eff;font-size:10px">● following</span>' : ''}` +
        `<br><span style="color:#888;font-size:11px">${v.type}${v.circuit ? ' · ' + v.circuit : ''}</span>` +
        `<br>Time: ${DATA.time_steps[idx]}` +
        `<br>Speed: ${pos.speed} mph` +
        `<br>Ignition: <b style="color:${pos.ignition ? '#2a9d2a' : '#e74c3c'}">${pos.ignition ? 'ON' : 'OFF'}</b>` +
        `<br>Driver: ${pos.driver || '—'}` +
        `<br>Postcode: ${pos.postcode || '—'}` +
        manifestHtml(manifest, idx)
      );
      if (isFollowed) {
        map.panTo([pos.lat, pos.lon], { animate: false });
      }
    } else {
      posMarkers[name].setOpacity(0);  // before this vehicle's first ping of the day
    }
  });

  maybeRebuildStops(idx);
}

// ── Engine-off stop markers — zoom-adaptive via markercluster ─────────────
function makeStopIcon(count) {
  if (count === 1) {
    return L.divIcon({
      html: `<svg width="12" height="12" viewBox="0 0 12 12" xmlns="http://www.w3.org/2000/svg">
        <line x1="2" y1="2" x2="10" y2="10" stroke="#8e44ad" stroke-width="2.5" stroke-linecap="round"/>
        <line x1="10" y1="2" x2="2"  y2="10" stroke="#8e44ad" stroke-width="2.5" stroke-linecap="round"/>
      </svg>`,
      className: '', iconSize: [12, 12], iconAnchor: [6, 6],
    });
  }
  const size = Math.round(Math.min(36, 16 + 4 * Math.sqrt(count)));
  const fs   = size < 22 ? 9 : size < 28 ? 11 : 13;
  return L.divIcon({
    html: `<div style="
      width:${size}px;height:${size}px;border-radius:50%;
      background:#8e44ad;border:2px solid white;
      display:flex;align-items:center;justify-content:center;
      color:#fff;font-weight:700;font-size:${fs}px;
      box-shadow:0 1px 5px rgba(0,0,0,.55);line-height:1
    ">${count < 100 ? count : '99+'}</div>`,
    className: '', iconSize: [size, size], iconAnchor: [size/2, size/2],
  });
}

// Cluster bubble: darker purple, sums underlying stop counts (not raw marker count)
// Wrapped in try/catch so CDN failure falls back to a plain layer group instead of
// crashing the entire script (which would prevent all event listeners from attaching).
let stopCluster;
try {
  stopCluster = L.markerClusterGroup({
    iconCreateFunction(cluster) {
      let total = 0;
      cluster.getAllChildMarkers().forEach(m => { total += m._visCount || 1; });
      const size = Math.round(Math.min(44, 20 + 4 * Math.sqrt(total)));
      const fs   = size < 26 ? 10 : size < 34 ? 12 : 14;
      return L.divIcon({
        html: `<div style="
          width:${size}px;height:${size}px;border-radius:50%;
          background:#5b2078;border:2.5px solid white;
          display:flex;align-items:center;justify-content:center;
          color:#fff;font-weight:700;font-size:${fs}px;
          box-shadow:0 2px 8px rgba(0,0,0,.6);line-height:1
        ">${total < 1000 ? total : '999+'}</div>`,
        className: '', iconSize: [size, size], iconAnchor: [size/2, size/2],
      });
    },
    maxClusterRadius: 60,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
    zoomToBoundsOnClick: true,
    animate: true,
  });
} catch (_) {
  stopCluster = L.layerGroup();   // no zoom-adaptive clustering, but everything else works
}

// Build all stop markers once — rebuildStopCluster handles which ones are active
const allStopMarkers = DATA.stops.map(s => {
  const total = Object.values(s.vehicle_counts).reduce((a, b) => a + b, 0);
  const vList = Object.keys(s.vehicle_counts).sort().join(', ');
  const tList = s.times.length > 8 ? s.times.slice(0, 8).join(', ') + '…' : s.times.join(', ');
  const m = L.marker([s.lat, s.lon], { icon: makeStopIcon(total), zIndexOffset: 50 })
    .bindPopup(
      `<b>Engine-off stop${total > 1 ? 's' : ''}</b>` +
      (total > 1 ? ` <span style="color:#8e44ad">×${total}</span>` : '') +
      `<br>Vehicle${Object.keys(s.vehicle_counts).length > 1 ? 's' : ''}: ${vList}` +
      `<br>Time${s.times.length > 1 ? 's' : ''}: ${tList}` +
      (s.postcode ? `<br>Postcode: ${s.postcode}` : ''),
      { maxWidth: 260 }
    );
  m._vehicleCounts = s.vehicle_counts;
  m._firstStep = s.first_step;
  m._visCount = total;
  return m;
});

let stopsShowing = false;

// Called on vehicle filter change — NOT on every playback frame.
// Uses addLayers() batch API so the cluster spatial index is only rebuilt once.
function rebuildStopCluster() {
  if (!stopsShowing) return;
  const dayStart = dailyMode ? DATA.day_boundaries[currentDayIdx(stepIdx)] : 0;
  const toAdd = [];
  allStopMarkers.forEach(m => {
    // In daily mode show only stops that occurred within the current day up to now.
    if (m._firstStep > stepIdx) return;
    if (dailyMode && m._firstStep < dayStart) return;
    let vis = 0;
    for (const [v, cnt] of Object.entries(m._vehicleCounts)) {
      if (vehicleVisible[v]) vis += cnt;
    }
    if (vis === 0) return;
    m._visCount = vis;
    m.setIcon(makeStopIcon(vis));
    toAdd.push(m);
  });
  stopCluster.clearLayers();
  stopCluster.addLayers(toAdd);
}

document.getElementById('show-stops').addEventListener('change', e => {
  stopsShowing = e.target.checked;
  if (stopsShowing) { _stopRebuildMax = -1; _stopRebuildDay = -1; stopCluster.addTo(map); rebuildStopCluster(); }
  else              { map.removeLayer(stopCluster); }
});

stopsShowing = true;
stopCluster.addTo(map);
rebuildStopCluster();

// ── Auto-fit map bounds ────────────────────────────────────────────────────
// Use reduce instead of Math.min/max(...arr) — spread crashes for large fleets
// where the route array can exceed JS engine argument-count limits (~65k).
(function initBounds() {
  let minLat = Infinity, maxLat = -Infinity, minLon = Infinity, maxLon = -Infinity;
  vehNames.forEach(n => {
    vehicles[n].route.lats.forEach(v => { if (v < minLat) minLat = v; if (v > maxLat) maxLat = v; });
    vehicles[n].route.lons.forEach(v => { if (v < minLon) minLon = v; if (v > maxLon) maxLon = v; });
  });
  DATA.depots.forEach(d => {
    if (d.lat < minLat) minLat = d.lat; if (d.lat > maxLat) maxLat = d.lat;
    if (d.lon < minLon) minLon = d.lon; if (d.lon > maxLon) maxLon = d.lon;
  });
  if (minLat !== Infinity) {
    map.fitBounds([[minLat, minLon], [maxLat, maxLon]], { padding: [30, 30] });
  }
})();

renderStep(0);

// ── Playback ───────────────────────────────────────────────────────────────
function stopPlay() {
  playing = false;
  clearInterval(playTimer);
  playTimer = null;
  btnPlay.textContent = '▶ Play';
  btnPlay.classList.remove('active');
}

function startPlay() {
  if (stepIdx >= nSteps - 1) stepIdx = 0;
  playing = true;
  btnPlay.textContent = '⏸ Pause';
  btnPlay.classList.add('active');
  playTimer = setInterval(() => {
    stepIdx++;
    renderStep(stepIdx);
    if (stepIdx >= nSteps - 1) stopPlay();
  }, 1000 / fps);
}

btnPlay.addEventListener('click', () => playing ? stopPlay() : startPlay());
btnPrev.addEventListener('click', () => {
  stopPlay(); stepIdx = Math.max(0, stepIdx - 1); renderStep(stepIdx);
});
btnNext.addEventListener('click', () => {
  stopPlay(); stepIdx = Math.min(nSteps - 1, stepIdx + 1); renderStep(stepIdx);
});

timeline.addEventListener('input', () => {
  stopPlay();
  stepIdx = parseInt(timeline.value, 10);
  renderStep(stepIdx);
});

speedSlider.addEventListener('input', () => {
  fps = parseInt(speedSlider.value, 10);
  speedVal.textContent = fps + ' fps';
  if (playing) { clearInterval(playTimer); startPlay(); }
});

document.addEventListener('keydown', e => {
  if (e.target === pinInput) return;
  if (e.code === 'Space')      { e.preventDefault(); playing ? stopPlay() : startPlay(); }
  if (e.code === 'ArrowLeft')  { stopPlay(); stepIdx = Math.max(0, stepIdx - 1); renderStep(stepIdx); }
  if (e.code === 'ArrowRight') { stopPlay(); stepIdx = Math.min(nSteps - 1, stepIdx + 1); renderStep(stepIdx); }
});

// ── Postcode pin ───────────────────────────────────────────────────────────
let pinMarker = null;

async function resolvePin() {
  const raw = pinInput.value.trim();
  if (!raw) return;
  pinStatus.textContent = 'Looking up…';

  // Try postcodes.io
  try {
    const r = await fetch(`https://api.postcodes.io/postcodes/${encodeURIComponent(raw)}`);
    if (r.ok) {
      const body = await r.json();
      if (body.status === 200 && body.result) {
        const { latitude: lat, longitude: lon } = body.result;
        dropPin(lat, lon, raw.toUpperCase());
        pinStatus.textContent = `Pinned: ${raw.toUpperCase()} (${lat.toFixed(4)}, ${lon.toFixed(4)})`;
        return;
      }
    }
  } catch (_) { /* network error — try lat,lon fallback */ }

  // Fallback: manual lat,lon entry
  const parts = raw.split(',');
  if (parts.length === 2) {
    const lat = parseFloat(parts[0]), lon = parseFloat(parts[1]);
    if (!isNaN(lat) && !isNaN(lon)) {
      dropPin(lat, lon, raw);
      pinStatus.textContent = `Pinned: ${lat.toFixed(4)}, ${lon.toFixed(4)}`;
      return;
    }
  }

  pinStatus.textContent = '⚠ Not found. Try lat,lon (e.g. 52.117,1.097).';
}

function dropPin(lat, lon, label) {
  if (pinMarker) map.removeLayer(pinMarker);
  const icon = L.divIcon({
    html: `<div style="
      width:16px;height:16px;border-radius:50%;
      background:#9b59b6;border:2px solid white;
      box-shadow:0 2px 6px rgba(0,0,0,.6)
    "></div>`,
    className: '', iconSize: [16, 16], iconAnchor: [8, 8],
  });
  pinMarker = L.marker([lat, lon], { icon, zIndexOffset: 500 })
    .bindPopup(`<b>${label}</b><br>${lat.toFixed(5)}, ${lon.toFixed(5)}`)
    .addTo(map)
    .openPopup();
  map.flyTo([lat, lon], Math.max(map.getZoom(), 12));
}

pinBtn.addEventListener('click', resolvePin);
pinInput.addEventListener('keydown', e => { if (e.key === 'Enter') resolvePin(); });
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------

def export(
    d_start: date,
    d_end: date,
    vehicle_names: list[str],
    step_minutes: int,
    output_path: Path,
    open_browser: bool = True,
) -> Path:
    n_days = (d_end - d_start).days + 1
    date_range = d_start.isoformat() if n_days == 1 else f"{d_start.isoformat()} to {d_end.isoformat()}"
    print(f"Loading telematics for {date_range}…")
    data = build_replay_data(d_start, d_end, vehicle_names, step_minutes)
    n_vehicles = len(data["vehicles"])
    n_steps = len(data["time_steps"])
    print(f"  {n_vehicles} vehicles · {n_days} day(s) · {n_steps} time steps ({step_minutes}-min)")

    data_json = json.dumps(data, separators=(",", ":"))
    html = _HTML_TEMPLATE.replace("__REPLAY_DATA__", data_json).replace("{date_range}", date_range)

    output_path.write_text(html, encoding="utf-8")
    size_kb = output_path.stat().st_size // 1024
    size_warn = "  ⚠  Large file — may load slowly in browser; try --step 5" if size_kb > 50_000 else ""
    print(f"Written: {output_path}  ({size_kb:,} KB){size_warn}")

    if open_browser:
        webbrowser.open(output_path.as_uri())

    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export a self-contained fleet-replay HTML file.")
    p.add_argument("--date", required=True, help="Date to replay, or start date for multi-day (YYYY-MM-DD)")
    p.add_argument("--date-end", default=None, help="End date for multi-day replay (YYYY-MM-DD); omit for single day")
    p.add_argument(
        "--mode", choices=["all", "circuit", "vehicle"], default="all",
        help="Vehicle selection mode (default: all)",
    )
    p.add_argument("--circuit", nargs="+", default=None, metavar="CIRCUIT", help="Circuit name(s) (required when --mode circuit; quote names with spaces)")
    p.add_argument("--vehicle", default=None, help="Vehicle asset name (required when --mode vehicle)")
    p.add_argument(
        "--step", type=int, default=1, metavar="MINUTES",
        help="Time step in minutes (default: 1). Higher = faster build, coarser animation.",
    )
    p.add_argument("--output", default=None, help="Output .html path (default: auto-named in cwd)")
    p.add_argument("--no-open", action="store_true", help="Don't open the file in a browser after export")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    try:
        d_start = date.fromisoformat(args.date)
    except ValueError:
        print(f"ERROR: invalid date '{args.date}' — use YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)

    if args.date_end:
        try:
            d_end = date.fromisoformat(args.date_end)
        except ValueError:
            print(f"ERROR: invalid --date-end '{args.date_end}' — use YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
        if d_end < d_start:
            print("ERROR: --date-end must be on or after --date", file=sys.stderr)
            sys.exit(1)
    else:
        d_end = d_start

    meta = frd.load_vehicles()
    by_circuit = frd.vehicles_by_circuit()

    if args.mode == "all":
        vehicle_names = sorted(meta["AssetName"].tolist())
        tag = "all"
    elif args.mode == "circuit":
        if not args.circuit:
            print("ERROR: --circuit is required when --mode circuit", file=sys.stderr)
            sys.exit(1)
        vehicle_names = []
        for c in args.circuit:
            if c not in by_circuit:
                available = ", ".join(sorted(by_circuit.keys()))
                print(f"ERROR: circuit '{c}' not found. Available: {available}", file=sys.stderr)
                sys.exit(1)
            vehicle_names.extend(by_circuit[c])
        vehicle_names = sorted(set(vehicle_names))
        tag = "+".join(c.replace(" ", "_") for c in args.circuit)
    else:  # vehicle
        if not args.vehicle:
            print("ERROR: --vehicle is required when --mode vehicle", file=sys.stderr)
            sys.exit(1)
        vehicle_names = [args.vehicle]
        tag = args.vehicle

    if args.output:
        out = Path(args.output)
    else:
        exports_dir = LOGISTICS_ROOT / "fleet_replay_exports"
        exports_dir.mkdir(exist_ok=True)
        date_slug = d_start.isoformat() if d_end == d_start else f"{d_start.isoformat()}_to_{d_end.isoformat()}"
        out = exports_dir / f"fleet_replay_{date_slug}_{tag}.html"

    export(
        d_start=d_start,
        d_end=d_end,
        vehicle_names=vehicle_names,
        step_minutes=args.step,
        output_path=out,
        open_browser=not args.no_open,
    )


if __name__ == "__main__":
    main()
