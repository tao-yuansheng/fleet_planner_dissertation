"""Nightly hub trunk as fixed scheduled services: B37 (T1) + LE10 hazchem (T2).

Pure sizing + draw bookkeeping over the candidate frame and vehicle master —
no routing, no ledger. Round-trip km is injected by the caller (computed via
real road distances); this module only multiplies trips by that figure.

EXPORT-ONLY (2026-07-24): the trunk carries only NETWORK-EXPORT freight up to
the hub. Network IMPORT arrives at the depot via an invisible hub resource we do
not own or model (imports are treated as spawning at the depot), so PL_IMPORT
never sizes a trunk trip. The tractor still round-trips and returns empty (empty
running is real), so km is unchanged; only the trip count is export-driven.
Spec: docs/superpowers/specs/2026-07-04-night-trunk-service-design.md;
LE10 hazchem trunk: docs/superpowers/plans/2026-07-05-geocode-repair-hazchem-trunk.md
"""
from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta

import pandas as pd

from freight_planner import config as _config
from freight_planner.config import TRUNK_DAY_DEPOTS, TRUNK_DECK_PALLETS, TRUNK_DEPOTS, TRUNK_NEXT_DAY_START

_EPS = 1e-6  # float tolerance on the trip-count ceil boundary (same discipline as shuttle.py)

B37_HUB = "B37_HUB"
LE10_HUB = "LE10_HUB"

# LE10 3BS (hazchem) trunk is modelled CB22-only regardless of a row's own depot
# field: telematics shows ALL LE10 3BS night visitors are CB22-homed tractors (20
# reg-nights in Jan, 98% of pings 18:00-06:00) and Bedford hazchem origin volume is
# noise (34 of 1,623 monthly hazchem-tagged legs vs 679 CB22-territory) -- see plan
# docs/superpowers/plans/2026-07-05-geocode-repair-hazchem-trunk.md.
LE10_FORCED_DEPOT = "CB22"

# Export may originate from a day-trunk depot (Stoke: same-day run to the hub, no
# night trunk) as well as the night-trunk depots.
_EXPORT_DEPOTS = frozenset(TRUNK_DEPOTS) | frozenset(TRUNK_DAY_DEPOTS)
_DAY_DEPOTS = frozenset(TRUNK_DAY_DEPOTS)


@dataclass(frozen=True)
class TrunkNight:
    depot: str
    night: str            # departure date ISO (freight collected day N goes up night N)
    export_pallets: float  # pallets collected INTO depot on day N (export = the only trunk load)
    trips: int             # ceil(export_pallets / TRUNK_DECK_PALLETS)
    km: float              # trips * roundtrip_km[(depot, hub)]
    hub: str = B37_HUB     # "B37_HUB" (default/legacy) or "LE10_HUB" (CB22-only hazchem)
    day_trunk: bool = False  # True = same-day run from a TRUNK_DAY_DEPOTS depot (Stoke), no night trunk
    vehicles: tuple = ()   # tractors PICKED by draw_tractors (draw-preference order) — gap 5:
                           # the draw is a real per-vehicle assignment, not bookkeeping
    feasible: tuple = ()   # the eligible pool the draw chose from (same order)


@dataclass
class TrunkPlan:
    nights: list = field(default_factory=list)
    draws: dict = field(default_factory=dict)            # (vehicle_id, night_iso) -> True
    avail_overrides: dict = field(default_factory=dict)  # (vehicle_id, next_day_iso) -> TRUNK_NEXT_DAY_START
    shortfalls: list = field(default_factory=list)       # (depot, night, missing_trips)
    total_km: float = 0.0
    total_trips: int = 0


def _trips(pallets: float) -> int:
    return math.ceil(max(0.0, pallets) / TRUNK_DECK_PALLETS - _EPS)


def _pallets(row: dict) -> float:
    """Pallet count with NaN treated as 0.0 (NaN is truthy, so `or 0.0` lets it
    through and one NaN row would poison the whole (depot, night) accumulator:
    the slot sum goes nan, max(0.0, nan) = 0.0, and the night silently drops).
    Same guard pattern as shuttle.py."""
    p = row.get("pallets")
    if p is None or pd.isna(p):
        return 0.0
    return float(p)


def _is_weeknight(night_iso: str) -> bool:
    d = date.fromisoformat(night_iso)
    return d.weekday() < 5  # Mon=0 .. Fri=4


def _next_weeknight(night_iso: str) -> str:
    """The next Mon-Fri date on/after ``night_iso`` (decision-audit #15,
    2026-07-26): a weekend collection still commits and serves, its freight
    just has no weeknight trunk to carry it home yet."""
    d = date.fromisoformat(night_iso)
    while not _is_weeknight(d.isoformat()):
        d += timedelta(days=1)
    return d.isoformat()


def trunk_schedule(candidates: pd.DataFrame, window_start: str, window_end: str,
                    roundtrip_km: dict) -> list:
    """Derive per-(hub, depot, night) trunk trips from PL_EXPORT candidate rows.

    export_pal(D, N): PL_EXPORT CUSTOMER_PICKUP legs collected into depot D on
    day N, night charged = service_date. Trips size on export ALONE — network
    imports arrive via the invisible hub resource (see module docstring) and are
    ignored here entirely.
    Only TRUNK_DEPOTS; weeknight (Mon-Fri) departures only; nights strictly
    before window_start are dropped (pre-window prestaging already covers
    them).

    Grouping is by (hub, depot, night). Rows carrying hub == LE10_HUB are
    FORCED to depot LE10_FORCED_DEPOT ("CB22") regardless of their own
    source_depot/target_depot field -- see LE10_FORCED_DEPOT docstring above
    for the telematics evidence. All other hub values (B37_HUB, or "" for
    older/non-hub rows, which defaults to B37_HUB on the emitted TrunkNight)
    keep today's per-depot behavior, restricted to TRUNK_DEPOTS.

    ``roundtrip_km`` is keyed by (depot, hub) tuples.
    """
    if candidates is None or len(candidates) == 0:
        return []
    # window_end kept for signature symmetry; candidates only cover the window,
    # no upper-bound filter needed.
    start = date.fromisoformat(str(window_start))
    totals: dict[tuple[str, str, str], float] = {}   # (hub, depot, night) -> export pallets

    def _bump(hub: str, depot: str, night_iso: str, pallets: float) -> None:
        if hub == LE10_HUB:
            depot = LE10_FORCED_DEPOT
        else:
            hub = B37_HUB
            # export may day-trunk from a TRUNK_DAY_DEPOTS depot as well.
            if depot not in _EXPORT_DEPOTS:
                return
        if not _is_weeknight(night_iso):
            # A Fri->Sat/Sun collection still commits and serves (the ledger
            # has no truck-schedule concept), but this function used to just
            # drop its pallets here -- no trunk, no shortfall, the export
            # silently vanished from every night's capacity accounting. Roll
            # it onto the next weeknight instead, so the volume that actually
            # needs to leave still sizes a real trunk trip (decision-audit #15,
            # 2026-07-26).
            night_iso = _next_weeknight(night_iso)
        if date.fromisoformat(night_iso) < start:
            return
        totals[(hub, depot, night_iso)] = totals.get((hub, depot, night_iso), 0.0) + pallets

    for row in candidates.to_dict("records"):
        flow = str(row.get("flow", ""))
        kind = str(row.get("leg_kind", ""))
        hub = str(row.get("hub", "") or "")
        pallets = _pallets(row)
        day = str(row.get("service_date", ""))[:10]
        if not day:
            continue
        # Export only: PL_IMPORT is delivered to the depot by the invisible hub and
        # never trunked by us (module docstring, 2026-07-24).
        if flow == "PL_EXPORT" and kind == "CUSTOMER_PICKUP":
            depot = str(row.get("target_depot", "") or "")
            night_iso = day
            _bump(hub, depot, night_iso, pallets)

    nights: list[TrunkNight] = []
    for (hub, depot, night_iso), exp in sorted(totals.items()):
        trips = _trips(exp)
        if trips <= 0:
            continue
        if (depot, hub) not in roundtrip_km:
            # Real trips priced at 0 km would silently vanish from the km
            # accounting (free trunk capacity) -- degrade loudly, not quietly.
            warnings.warn(
                f"trunk: no roundtrip_km for ({depot!r}, {hub!r}) — {trips} trips priced at 0 km")
        km = trips * float(roundtrip_km.get((depot, hub), 0.0))
        nights.append(TrunkNight(depot=depot, night=night_iso, export_pallets=exp,
                                  trips=trips, km=km, hub=hub,
                                  day_trunk=(depot in _DAY_DEPOTS)))
    return nights


def _returns_too_late(route_end_by_vid, vid: str, cutoff) -> bool:
    """True when ``vid``'s same-day committed route ends AFTER ``cutoff`` (so it cannot
    physically make the trunk depart). Unknown/unparseable route-end => not too late
    (fail-open: never manufacture a shortfall from missing data). (decision-audit #8)"""
    if cutoff is None or not route_end_by_vid:
        return False
    raw = route_end_by_vid.get(str(vid))
    if not raw:
        return False
    try:
        return datetime.fromisoformat(str(raw)) > cutoff
    except (TypeError, ValueError):
        return False


def draw_tractors(nights: list, vehicle_df: pd.DataFrame, reserved,
                  route_end_by_vid: dict | None = None) -> TrunkPlan:
    """Draw trips tractors per night from the depot's tractor pool.

    Rotation: least-recently-drawn first; never-drawn tractors break ties by
    vehicle_id sort (deterministic). Skips any tractor reserved for a
    multiday tour on the trunk night itself or on the following day (its
    next-day availability would conflict with the trunk's own delay), and any
    tractor ALREADY drawn tonight for another service (one trip per
    tractor-night: a tractor cannot run B37 and LE10 the same night, so under
    a constrained pool the second service correctly registers a shortfall
    instead of silently overwriting the (vid, night) draw entry).

    ``route_end_by_vid`` (vid -> same-day committed route-end ISO) additionally
    excludes a tractor still out on a customer route that ends AFTER the night trunk's
    prep cutoff (``TRUNK_DEPART_HOUR`` − ``TRUNK_PREP_MARGIN_H``) — it cannot make the
    depart, and drawing it would fabricate an impossible assignment (decision-audit #8).
    When the exclusion empties the pool, an honest shortfall is recorded. Day trunks and
    a missing map are un-gated (fail-open). Shortfall (trips > available tractors) is
    recorded per (depot, night, missing) but the trips still count toward totals/km —
    the freight physically moves in reality.
    """
    reserved = reserved or set()
    plan = TrunkPlan()
    pools: dict[str, list[str]] = {}
    last_drawn: dict[str, int] = {}
    draw_seq = 0

    def _pool(depot: str) -> list[str]:
        if depot not in pools:
            rows = vehicle_df[(vehicle_df["vehicle_type"].astype(str) == "tractor")
                               & (vehicle_df["home_depot"].astype(str) == depot)]
            vids = sorted(str(v) for v in rows["vehicle_id"])
            pools[depot] = vids
            for vid in vids:
                last_drawn[vid] = -1
        return pools[depot]

    # n.hub in the sort key for standalone determinism (trunk_schedule already
    # emits (hub, depot, night)-sorted, but don't rely on input order + stable sort).
    for night in sorted(nights, key=lambda n: (n.night, n.depot, n.hub)):
        pool = _pool(night.depot)
        next_day_iso = (date.fromisoformat(night.night) + timedelta(days=1)).isoformat()
        # night trunks must be made after daily ops finish: a tractor whose route ends
        # after TRUNK_DEPART_HOUR - TRUNK_PREP_MARGIN_H cannot make it (audit #8). Day
        # trunks run at day-close and are not gated here.
        cutoff = None
        if route_end_by_vid and not getattr(night, "day_trunk", False):
            from freight_planner.shared.config import TRUNK_DEPART_HOUR, TRUNK_PREP_MARGIN_H
            cutoff = (datetime.combine(date.fromisoformat(night.night), time(0))
                      + timedelta(hours=float(TRUNK_DEPART_HOUR) - float(TRUNK_PREP_MARGIN_H)))
        eligible = [vid for vid in pool
                    if (vid, night.night) not in reserved
                    and (vid, next_day_iso) not in reserved
                    and (vid, night.night) not in plan.draws  # one trip per tractor-night
                    and not _returns_too_late(route_end_by_vid, vid, cutoff)]
        eligible.sort(key=lambda vid: (last_drawn[vid], vid))
        needed = night.trips
        drawn = eligible[:needed]
        missing = needed - len(drawn)
        for vid in drawn:
            plan.draws[(vid, night.night)] = True
            # Next-day availability hold is OFF by default (telematics: trunk artics
            # run full customer days too — see config.TRUNK_NEXT_DAY_HOLD). Both the
            # daily start-time (combined_avail_overrides) and the repair busy-set
            # (tour_plan daily_busy) key off avail_overrides, so not emitting it here
            # frees the tractor for a full next day everywhere at once.
            if _config.TRUNK_NEXT_DAY_HOLD and not night.day_trunk:
                plan.avail_overrides[(vid, next_day_iso)] = TRUNK_NEXT_DAY_START
            last_drawn[vid] = draw_seq
            draw_seq += 1
        if missing > 0:
            plan.shortfalls.append((night.depot, night.night, missing))
        plan.nights.append(replace(night, vehicles=tuple(drawn), feasible=tuple(eligible)))
        plan.total_km += night.km
        plan.total_trips += night.trips

    return plan


def trunk_schedule_frame(nights: list) -> pd.DataFrame:
    """The emitted trunk_schedule.csv shape: one row per (hub, depot, night) with
    the PICKED tractors (semicolon-joined, draw-preference order) and the feasible
    pool the draw chose from. A shortfall night simply names fewer vehicles than
    trips — the missing count stays in the shortfall report."""
    return pd.DataFrame(
        [{"depot": n.depot, "night": n.night,
          "export_pallets": n.export_pallets, "trips": n.trips, "km": n.km,
          "hub": n.hub,
          "vehicles": ";".join(getattr(n, "vehicles", ()) or ()),
          "feasible": ";".join(getattr(n, "feasible", ()) or ())}
         for n in nights])


# Short label for each hub in the run_alns.py log line, e.g. "trunk: B37: ... | LE10: ...".
_HUB_SHORT = {B37_HUB: "B37", LE10_HUB: "LE10"}


def trunk_log_summary(nights: list) -> str:
    """Format the per-run trunk log line, grouped by hub then depot.

    e.g. "B37: BEDFORD 16 trips / 5 nights 3,859 km, CB22 17 trips / 5 nights
    5,361 km | LE10: CB22 5 trips / 5 nights 1,100 km". A hub section is
    omitted entirely when it has no nights (e.g. no LE10 hazchem freight this
    run). Empty input -> empty string.
    """
    if not nights:
        return ""
    by_hub: dict[str, dict[str, list]] = {}
    for n in nights:
        by_hub.setdefault(n.hub, {}).setdefault(n.depot, []).append(n)

    sections = []
    for hub in sorted(by_hub, key=lambda h: _HUB_SHORT.get(h, h)):
        by_depot = by_hub[hub]
        depot_parts = ", ".join(
            f"{depot} {sum(n.trips for n in depot_nights)} trips / {len(depot_nights)} nights "
            f"{sum(n.km for n in depot_nights):,.0f} km"
            for depot, depot_nights in sorted(by_depot.items())
        )
        sections.append(f"{_HUB_SHORT.get(hub, hub)}: {depot_parts}")
    return " | ".join(sections)
