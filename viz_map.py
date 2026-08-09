"""Self-contained folium HTML maps for the freight_planner pipeline.

Two distinct views, both written as a single ``.html`` (open in any browser, no
server) off a run's period-scoped ``plan/`` folder:

  * ``build_trip_map`` — per-trip *planned* view. Every trip (a depot loop, keyed
    by ``trip_id = route_id#T{trip_index}``) is its own toggleable layer, so you can
    isolate and inspect specific trips. Reads ``route_stops.csv`` only — coordinates
    are already in the plan, so no geocoding is needed.

  * ``build_plan_vs_actual_map`` — the old planned-vs-actual comparison ported to the
    new pipeline: planned routes plus the telematics GPS overlay, to spot real movement
    with no matching planned trip (trips that slipped through). [added next]

Road geometry is snapped via OSRM (``simulation.routing.get_route_geometry``) with a
straight-line fallback when OSRM is unreachable.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from freight_planner.output_layout import RunPaths, artifact_dir
from freight_planner.shared.routing import DEFAULT_OSRM_URL, get_route_geometry

# A spread of visually distinct colors; vehicles cycle through these.
_PALETTE = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#46f0f0", "#f032e6",
    "#bcf60c", "#fabebe", "#008080", "#9a6324", "#800000", "#aaffc3", "#808000",
    "#000075", "#e6beff", "#ff6f00", "#1b5e20", "#6a1b9a", "#00838f",
]
_DEPOT_COLOR = "#222222"
_DIRECT_KINDS = {"direct_customer_move"}


def _vehicle_color(vehicle_id: str, order: dict[str, int]) -> str:
    return _PALETTE[order.setdefault(vehicle_id, len(order)) % len(_PALETTE)]


def _has_coord(lat, lon) -> bool:
    return pd.notna(lat) and pd.notna(lon)


def _trip_waypoints(rows: pd.DataFrame) -> list[tuple[float, float]]:
    """Ordered (lat, lon) waypoints for one trip. Direct moves carry the freight from
    the depot via the collection point to the drop, so insert collect before drop."""
    pts: list[tuple[float, float]] = []
    for r in rows.itertuples(index=False):
        if str(getattr(r, "stop_type", "")) in _DIRECT_KINDS and _has_coord(
            getattr(r, "collect_lat", None), getattr(r, "collect_lon", None)
        ):
            pts.append((float(r.collect_lat), float(r.collect_lon)))
        if _has_coord(getattr(r, "lat", None), getattr(r, "lon", None)):
            pts.append((float(r.lat), float(r.lon)))
    return pts


def _geometry(waypoints: list[tuple[float, float]], use_osrm: bool, osrm_url: str) -> list[list[float]]:
    if use_osrm and len(waypoints) >= 2:
        snapped = get_route_geometry(waypoints, osrm_url)
        if snapped:
            return snapped
    return [[lat, lon] for lat, lon in waypoints]


def _stop_popup(r) -> str:
    rows = [
        ("trip", f"{r.route_id}#T{int(r.trip_index)}"),
        ("stop", f"{int(r.sequence)} · {r.stop_type}"),
        ("order", str(getattr(r, "order_id", "") or "")),
        ("postcode", str(getattr(r, "service_pc", "") or getattr(r, "node", "") or "")),
        ("arrive", str(getattr(r, "planned_arrive", "") or "")),
        ("depart", str(getattr(r, "planned_depart", "") or "")),
        ("pallets after", str(getattr(r, "load_pallets_after", "") or "")),
        ("leg km", f"{float(getattr(r, 'leg_km', 0.0) or 0.0):.1f}"),
    ]
    body = "".join(f"<tr><td style='color:#888'>{k}</td><td>{v}</td></tr>" for k, v in rows)
    return f"<table style='font:12px sans-serif'>{body}</table>"


def build_trip_map(
    plan_dir: Path | str,
    out_html: Path | str | None = None,
    *,
    use_osrm: bool = True,
    osrm_url: str = DEFAULT_OSRM_URL,
    service_date: str | None = None,
    vehicle: str | None = None,
) -> Path:
    """Render a per-trip planned map. Each trip is a toggleable layer (LayerControl).

    Filter with ``service_date`` and/or ``vehicle`` to keep a week's worth of trips
    readable. Returns the written HTML path.
    """
    import folium

    plan_dir = artifact_dir(Path(plan_dir))   # window dir (current) or plan/ (legacy)
    df = pd.read_csv(plan_dir / "route_stops.csv")
    if service_date:
        df = df[df["service_date"].astype(str) == service_date]
    if vehicle:
        df = df[df["vehicle_id"].astype(str) == vehicle]
    if df.empty:
        raise ValueError(f"no route stops match (date={service_date!r}, vehicle={vehicle!r})")

    stop_pts = df[df["stop_type"].isin(["customer_delivery", "customer_pickup", *_DIRECT_KINDS])]
    lat0 = float(stop_pts["lat"].astype(float).mean()) if not stop_pts.empty else float(df["lat"].astype(float).mean())
    lon0 = float(stop_pts["lon"].astype(float).mean()) if not stop_pts.empty else float(df["lon"].astype(float).mean())
    m = folium.Map(location=[lat0, lon0], zoom_start=8, tiles="cartodbpositron", control_scale=True)

    # depot markers (one per distinct depot coord)
    depots = df[df["stop_type"].isin(["depot_start", "depot_return"])].drop_duplicates(subset=["node", "lat", "lon"])
    depot_fg = folium.FeatureGroup(name="◆ depots", show=True)
    for r in depots.itertuples(index=False):
        if _has_coord(r.lat, r.lon):
            folium.Marker(
                [float(r.lat), float(r.lon)], tooltip=f"Depot {r.node}",
                icon=folium.Icon(color="black", icon="home", prefix="fa"),
            ).add_to(depot_fg)
    depot_fg.add_to(m)

    color_order: dict[str, int] = {}
    n_trips = 0
    for (rid, tidx), g in df.groupby(["route_id", "trip_index"], sort=True):
        g = g.sort_values("sequence")
        wpts = _trip_waypoints(g)
        if len(wpts) < 2:
            continue
        vid = str(g["vehicle_id"].iloc[0])
        color = _vehicle_color(vid, color_order)
        sdate = str(g["service_date"].iloc[0])
        trip_id = f"{rid}#T{int(tidx)}"
        is_tour = bool(g["is_tour"].iloc[0]) if "is_tour" in g.columns else False
        label = f"{'⛟' if is_tour else '▸'} {vid} · {sdate} · T{int(tidx)}"
        fg = folium.FeatureGroup(name=label, show=True)

        folium.PolyLine(
            _geometry(wpts, use_osrm, osrm_url), color=color, weight=3, opacity=0.8,
            tooltip=trip_id,
        ).add_to(fg)
        for r in g.itertuples(index=False):
            st = str(getattr(r, "stop_type", ""))
            if st in _DIRECT_KINDS and _has_coord(getattr(r, "collect_lat", None), getattr(r, "collect_lon", None)):
                folium.CircleMarker(
                    [float(r.collect_lat), float(r.collect_lon)], radius=4, color=color,
                    fill=True, fill_opacity=0.6, tooltip=f"collect · {getattr(r, 'order_id', '')}",
                ).add_to(fg)
            if st in ("depot_start", "depot_return") or not _has_coord(getattr(r, "lat", None), getattr(r, "lon", None)):
                continue
            folium.CircleMarker(
                [float(r.lat), float(r.lon)], radius=6, color=color, fill=True, fill_opacity=0.9,
                popup=folium.Popup(_stop_popup(r), max_width=260),
                tooltip=f"{int(r.sequence)} · {st}",
            ).add_to(fg)
        fg.add_to(m)
        n_trips += 1

    folium.LayerControl(collapsed=False).add_to(m)

    if out_html is None:
        suffix = f"_{service_date}" if service_date else ""
        suffix += f"_{vehicle}" if vehicle else ""
        if isinstance(plan_dir, RunPaths):
            out_html = plan_dir / f"trip_map{suffix}.html"   # html -> run root
        else:
            reports = plan_dir.parent / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            out_html = reports / f"trip_map{suffix}.html"
    out_html = Path(out_html)
    m.save(str(out_html))
    print(f"trip map: {n_trips} trips -> {out_html}")
    return out_html


def _actual_traces(service_date: str, fleet: set[str]) -> dict:
    """Telematics GPS traces for one date, restricted to vehicles in ``fleet``
    (so out-of-scope assets don't clutter the missing-trip signal)."""
    from datetime import date

    from freight_planner.shared import fleet_replay_data as frd

    day = frd.load_day(date.fromisoformat(service_date))
    present = [a for a in day["AssetName"].dropna().astype(str).unique() if a in fleet]
    return frd.prepare_vehicle_traces(day, present)


def build_plan_vs_actual_map(
    plan_dir: Path | str,
    service_date: str,
    out_html: Path | str | None = None,
    *,
    use_osrm: bool = True,
    osrm_url: str = DEFAULT_OSRM_URL,
) -> Path:
    """Planned routes (solid) vs actual telematics (dashed) for one date.

    The point is to catch trips that slipped through: a vehicle in our fleet that
    *moved* (telematics) but had no planned trip that day shows up in a highlighted
    "actual — NOT planned" layer. Vehicle identity is plan ``vehicle_id`` == telematics
    ``AssetName``. Fleet scope = every vehicle the planner uses across the run window.
    """
    import folium

    plan_dir = artifact_dir(Path(plan_dir))   # window dir (current) or plan/ (legacy)
    allstops = pd.read_csv(plan_dir / "route_stops.csv")
    fleet = set(allstops["vehicle_id"].dropna().astype(str))
    day_plan = allstops[allstops["service_date"].astype(str) == service_date]
    planned_vehicles = set(day_plan["vehicle_id"].dropna().astype(str))
    traces = _actual_traces(service_date, fleet)

    pts_for_center = day_plan[day_plan["lat"].notna()]
    lat0 = float(pts_for_center["lat"].astype(float).mean()) if not pts_for_center.empty else 52.2
    lon0 = float(pts_for_center["lon"].astype(float).mean()) if not pts_for_center.empty else 0.1
    m = folium.Map(location=[lat0, lon0], zoom_start=8, tiles="cartodbpositron", control_scale=True)

    color_order: dict[str, int] = {}

    # planned routes — solid, one layer per vehicle (all its trips that day)
    planned_fg = folium.FeatureGroup(name="planned (solid)", show=True)
    for vid, g in day_plan.groupby("vehicle_id", sort=True):
        color = _vehicle_color(str(vid), color_order)
        for _tidx, trip in g.groupby("trip_index"):
            wpts = _trip_waypoints(trip.sort_values("sequence"))
            if len(wpts) >= 2:
                folium.PolyLine(
                    _geometry(wpts, use_osrm, osrm_url), color=color, weight=3,
                    opacity=0.85, tooltip=f"planned {vid}",
                ).add_to(planned_fg)
    planned_fg.add_to(m)

    # actual GPS — dashed; matched vs unplanned
    matched_fg = folium.FeatureGroup(name="actual — matched (dashed)", show=True)
    unplanned_fg = folium.FeatureGroup(name="⚠ actual — NOT planned (dashed)", show=True)
    n_unplanned = 0
    for name, tr in traces.items():
        pts = [[float(la), float(lo)] for la, lo in zip(tr.rendered["Latitude"], tr.rendered["Longitude"])]
        if len(pts) < 2:
            continue
        if name in planned_vehicles:
            color, group = _vehicle_color(name, color_order), matched_fg
        else:
            color, group, n_unplanned = "#d00000", unplanned_fg, n_unplanned + 1
        folium.PolyLine(pts, color=color, weight=2.5, opacity=0.8, dash_array="6,8",
                        tooltip=f"actual {name}").add_to(group)
        folium.CircleMarker(pts[-1], radius=5, color=color, fill=True, fill_opacity=0.9,
                            tooltip=f"{name} last ping").add_to(group)
    matched_fg.add_to(m)
    unplanned_fg.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    if out_html is None:
        if isinstance(plan_dir, RunPaths):
            out_html = plan_dir / f"plan_vs_actual_{service_date}.html"   # html -> run root
        else:
            reports = plan_dir.parent / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            out_html = reports / f"plan_vs_actual_{service_date}.html"
    out_html = Path(out_html)
    m.save(str(out_html))
    print(f"plan-vs-actual {service_date}: planned {len(planned_vehicles)} veh, "
          f"actual-in-fleet {len(traces)}, unplanned-but-moved {n_unplanned} -> {out_html}")
    return out_html


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render freight_planner HTML maps from a run's plan/ folder.")
    parser.add_argument("--plan-dir", required=True, help="path to a run's plan/ folder (has route_stops.csv)")
    parser.add_argument("--mode", choices=["trips", "compare"], default="trips",
                        help="trips = per-trip planned view; compare = planned vs actual telematics (one date)")
    parser.add_argument("--date", default=None, help="service_date; filter (trips) or required day (compare)")
    parser.add_argument("--vehicle", default=None, help="filter to one vehicle_id (trips mode)")
    parser.add_argument("--out", default=None, help="output .html path")
    parser.add_argument("--no-osrm", action="store_true", help="skip OSRM road-snapping (straight lines)")
    parser.add_argument("--osrm-url", default=DEFAULT_OSRM_URL)
    args = parser.parse_args(argv)

    if args.mode == "compare":
        if not args.date:
            parser.error("--mode compare requires --date")
        build_plan_vs_actual_map(
            args.plan_dir, args.date, args.out,
            use_osrm=not args.no_osrm, osrm_url=args.osrm_url,
        )
    else:
        build_trip_map(
            args.plan_dir, args.out,
            use_osrm=not args.no_osrm, osrm_url=args.osrm_url,
            service_date=args.date, vehicle=args.vehicle,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
