"""Build-time OSRM road-geometry baking for the map dashboard.

Routes on the map are reconstructed in the browser from the per-epoch ``snaps``
already in the timeline payload; this module bakes the road polyline for each
UNIQUE consecutive coordinate-pair those snaps traverse (depot connectors
included) so the browser needs no live OSRM. Geometry is directional and
epoch-independent, so each pair is fetched once and cached to disk.
"""
from __future__ import annotations

import json
from pathlib import Path

from freight_planner.shared.routing import coord_key, get_route_geometry
from freight_planner.shared.paths import LOGISTICS_ROOT

Coord = tuple[float, float]
Pair = tuple[Coord, Coord]

GEOM_CACHE_PATH = LOGISTICS_ROOT / "data" / "Output" / "osrm_geometry_cache.json"
GEOM_MAX_POINTS = 40   # per leg; keeps the payload small, still road-shaped


def _depot_coord(vehicle: dict, depots: dict) -> Coord | None:
    anchor = depots.get(str(vehicle.get("home", "")))
    if anchor is None:
        return None
    return (float(anchor[0]), float(anchor[1]))


def _has_coord(lat, lon) -> bool:
    # lon can legitimately be 0 (Greenwich); the "no geocode" sentinel is (0, 0)
    return lat is not None and lon is not None and not (lat == 0 and lon == 0)


def _stop_coords(jobs: list, depot: Coord | None, stop) -> list[Coord]:
    """Ordered coords a stop contributes — mirrors MAPLOGIC.stopCoords in the browser:
    depot connectors -> the home anchor; a DIRECT carry -> collect origin then deliver
    dest (so both the depot->origin and origin->dest legs get baked)."""
    ji = stop[0]
    if ji == -2 or ji == -1:
        return [depot] if depot is not None else []
    if not (0 <= ji < len(jobs)):
        return []
    j = jobs[ji]
    out: list[Coord] = []
    if j.get("ty") == "direct" and _has_coord(j.get("clat"), j.get("clon")):
        out.append((float(j["clat"]), float(j["clon"])))
    if _has_coord(j.get("lat"), j.get("lon")):
        out.append((float(j["lat"]), float(j["lon"])))
    return out


def route_pairs(days: list[dict], depots: dict) -> set[Pair]:
    """Every distinct directional (from, to) coordinate leg any vehicle traverses
    across every epoch snapshot, depot connectors resolved to the vehicle's home
    anchor and direct carries expanded to collect->deliver. Deduped."""
    pairs: set[Pair] = set()
    for day in days:
        jobs = day.get("jobs", [])
        for veh in day.get("vehicles", []):
            depot = _depot_coord(veh, depots)
            for snap in veh.get("snaps", []):
                seq: list[Coord] = []
                for stop in snap:
                    seq.extend(_stop_coords(jobs, depot, stop))
                for a, b in zip(seq, seq[1:]):
                    if a != b:
                        pairs.add((a, b))
            # this day's TOUR leg (outside the snapshot stream, split by day): it STARTS at
            # the depot (day 1) or the overnight-resume point (later days), threads the day's
            # stops (direct carries expanded collect->deliver), and RETURNS to the depot only
            # on the final day. Mirrors MAPLOGIC.tourDayNodes so the same legs get baked.
            td = veh.get("tourDay")
            if td:
                tdepot = tuple(td["depot"]) if td.get("depot") else None
                seq = []
                if td.get("startDepot") and tdepot is not None:
                    seq.append(tdepot)
                elif td.get("resume") and _has_coord(td["resume"][0], td["resume"][1]):
                    seq.append((float(td["resume"][0]), float(td["resume"][1])))
                for s in td.get("stops", []):
                    if s.get("ty") == "direct" and _has_coord(s.get("clat"), s.get("clon")):
                        seq.append((float(s["clat"]), float(s["clon"])))
                    if _has_coord(s.get("lat"), s.get("lon")):
                        seq.append((float(s["lat"]), float(s["lon"])))
                if td.get("endDepot") and tdepot is not None:
                    seq.append(tdepot)
                elif td.get("park") and _has_coord(td["park"][0], td["park"][1]):
                    # mid-leg overnight: the day's driving ends at the sleep point
                    # (mirrors tourDayNodes' park node, 2026-07-22)
                    seq.append((float(td["park"][0]), float(td["park"][1])))
                for a, b in zip(seq, seq[1:]):
                    if a != b:
                        pairs.add((a, b))
    return pairs


def _pair_key(a: Coord, b: Coord) -> str:
    return f"{coord_key(a[0], a[1])}|{coord_key(b[0], b[1])}"


def _load(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path: Path, cache: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f)


def bake(pairs: set[Pair], osrm_url: str | None = None,
         cache_path: Path = GEOM_CACHE_PATH) -> dict:
    """Return {pair_key: [[lat,lon],...]} for each pair, fetching misses from OSRM
    and persisting them. A pair OSRM cannot route is OMITTED (the browser
    straight-lines it). Never raises on OSRM failure."""
    cache = _load(cache_path)
    dirty = False
    out: dict = {}
    kwargs = {"osrm_url": osrm_url} if osrm_url else {}
    for a, b in pairs:
        k = _pair_key(a, b)
        if k not in cache:
            try:
                line = get_route_geometry([a, b], max_points=GEOM_MAX_POINTS, **kwargs)
            except Exception:
                line = None
            if line:
                cache[k] = line
                dirty = True
            else:
                continue
        if cache.get(k):
            out[k] = cache[k]
    if dirty:
        _save(cache_path, cache)
    return out
