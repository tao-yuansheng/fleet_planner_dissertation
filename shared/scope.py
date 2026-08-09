"""Cambridge order classification and scope filter."""
import math
import re as _re
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from typing import Literal, Optional, Tuple, Any

import pandas as pd

from freight_planner.shared.postcode_resolver import (
    coords_from_cache_entry, geocode_postcode, FC_CODE_ALIASES,
)


# FC / hub codes that appear in Qargo postcode fields but are not valid UK
# postcodes are resolved via the single source of truth in
# simulation.postcode_resolver (imported as FC_CODE_ALIASES). Add new codes
# there, not here.
_FC_CODE_ALIASES = FC_CODE_ALIASES


def _postcode_key(pc: str) -> str:
    """Normalise a postcode/cache key for matching."""
    return _re.sub(r'\s+', '', str(pc or '').strip().upper())


def _resolve_fc_postcode(pc: str) -> str:
    """Resolve an FC/hub code to its real UK postcode, or return pc unchanged."""
    raw = str(pc or '').strip().upper()
    return _FC_CODE_ALIASES.get(_postcode_key(raw), raw)


def _cached_coords(pc: str, postcode_cache: dict) -> Optional[tuple]:
    """Return cached coordinates, accepting spaced or compact postcode keys.

    This deliberately does not geocode. It is used downstream of scoping where
    a missing cache entry should remain a routing/data issue, not mutate state.
    """
    if not pc:
        return None
    resolved = _resolve_fc_postcode(pc)
    compact = _postcode_key(resolved)
    candidates = [resolved]
    if compact != resolved:
        candidates.append(compact)
    for key in candidates:
        entry = postcode_cache.get(key)
        if entry is not None:
            return coords_from_cache_entry(entry)
    return None


def _lookup_coords(pc: str, postcode_cache: dict) -> Optional[tuple]:
    """Return (lat, lon) for a postcode, geocoding and caching on first miss.

    Handles both list [lat, lon] and dict {'lat':..,'lon':..} cache formats.
    Uses postcodes.io as the primary exact postcode-unit lookup; pgeocode is
    deliberately not used for routing because it collapses many UK postcode
    units onto coarse locality centroids.
    """
    if not pc:
        return None
    pc = _resolve_fc_postcode(pc)  # map FC/hub codes to real postcodes first
    cached = _cached_coords(pc, postcode_cache)
    if cached is not None:
        return cached
    return geocode_postcode(pc, postcode_cache)


from freight_planner.shared.plan_types import OrderClass
from freight_planner.shared.verified_legs import corrected_flow
from freight_planner.shared.config import (
    CB22_DEPOT_ANCHOR,
    BEDFORD_DEPOT_ANCHOR,
    STOKE_DEPOT_ANCHOR,
    CATCHMENT_RADIUS_KM,
    CB22_RIGIDS,
    CB22_TRACTORS,
    SERVICE_LEVEL_WINDOW_HOURS,
    DEFAULT_WINDOW_HOURS,
    CUSTOMER_DAY_START,
    OPERATING_DAY_START,
    OPERATING_DAY_END,
    TRUNK_DEPART_HOUR,
    TRUNK_PREP_MARGIN_H,
    ROAD_DISTANCE_FACTOR,
    AVG_SPEED_KMH,
    customer_service_minutes,
    TRACTOR_TYPE_SHIFT_START,
    TRACTOR_TYPE_SHIFT_END,
)

# LOCAL_DELIVER / LOCAL_COLLECT: single-leg non-network orders (formerly
# partial-fleet/unknown) brought into scope from telematics-verified legs. They
# are serviced locally with NO hub trunk (LOCAL_DELIVER = depot->customer, freight
# pre-staged; LOCAL_COLLECT = customer->depot pickup).
FlowTag = Literal['PL_IMPORT', 'FULL_FLEET', 'PL_EXPORT', 'LOCAL_DELIVER', 'LOCAL_COLLECT']
WindowHardness = Literal['hard_slot', 'soft_deadline', 'date_only', 'pickup_window', 'unknown']


def assign_depot(postcode: Optional[str]) -> str:
    """Map a postcode to the depot that owns it based on territory rules.

    Returns one of 'CB22', 'BEDFORD', 'ST_IVES', or 'OVERFLOW'.
    OVERFLOW means the postcode is outside any depot's defined territory
    and will be allocated by capacity on the day.
    """
    from freight_planner.shared.config import (
        BEDFORD_TERRITORY_PREFIXES, CB22_TERRITORY_PREFIXES,
        STOKE_TERRITORY_PREFIXES,
        BEDFORD_SG_DISTRICTS, CB22_SG_DISTRICTS,
        BEDFORD_PE_DISTRICTS, CB22_PE_DISTRICTS,
    )
    if not postcode or not str(postcode).strip():
        return 'OVERFLOW'
    district = str(postcode).strip().split()[0].upper()
    m = _re.match(r'^([A-Z]+)', district)
    prefix = m.group(1) if m else ''

    if prefix in STOKE_TERRITORY_PREFIXES:
        return 'STOKE'
    if prefix in BEDFORD_TERRITORY_PREFIXES:
        return 'BEDFORD'
    if prefix in CB22_TERRITORY_PREFIXES:
        return 'CB22'
    if prefix == 'SG':
        if district in BEDFORD_SG_DISTRICTS:
            return 'BEDFORD'
        if district in CB22_SG_DISTRICTS:
            return 'CB22'
        return 'OVERFLOW'
    if prefix == 'PE':
        if district in BEDFORD_PE_DISTRICTS:
            return 'BEDFORD'
        if district in CB22_PE_DISTRICTS:
            return 'CB22'
        return 'OVERFLOW'
    return 'OVERFLOW'


# -- Staging-depot resolution ------------------------------------------------
# assign_depot() returns OVERFLOW for any postcode outside a depot territory.
# resolve_staging_depot() turns that OVERFLOW into a *real* gateway depot so
# freight never stages at a virtual location or the geographically-nearest anchor
# (which for anything northern is the dockless Stoke ST4 satellite). The gateways
# are the capable member depots that trunk the B37 Palletline hub; the ST4
# satellite and the (empty) ST_IVES yard are deliberately NOT eligible.
# Dock gateways: where a Palletline import physically lands (needs a dock). The
# dockless Stoke ST4 satellite and the empty ST_IVES yard are NOT delivery gateways.
GATEWAY_DEPOTS: Tuple[str, ...] = ("CB22", "BEDFORD")
# Collection bases: any base a vehicle can run out from to collect (no dock needed),
# so the Stoke satellite IS eligible — it is a legitimate northern collection base
# (its tractors roam the north and trunk the B37 hub).
COLLECTION_BASES: Tuple[str, ...] = ("CB22", "BEDFORD", "STOKE")

_STAGING_ANCHORS = {
    "CB22": CB22_DEPOT_ANCHOR,
    "BEDFORD": BEDFORD_DEPOT_ANCHOR,
    "STOKE": STOKE_DEPOT_ANCHOR,
}

# Spoke rule (2026-07-20): a spoke depot only STAGES freight whose delivery is
# within its radius (haversine km; ~150 road km). Stoke's 5 tractors keep the
# regional lanes (LE19/Manchester); far deliveries cross-dock through the
# CB22/BEDFORD hub fleets — the verified real pattern (Feb: 72% of ST-origin
# FULL_FLEET served by hub vehicles, all far-south lanes two-vehicle
# cross-docked). Hubs are absent = unbounded; empty dict = feature off.
SPOKE_DELIVERY_RADIUS_KM: dict = {"STOKE": 120.0}

# Collection-side scarcity guard (decision-audit #5, 2026-07-26): the delivery cap
# above gates only the DELIVERY end, so a delivery near a spoke let it win the
# combined-deadhead argmin even when the COLLECTION sat deep in a hub's backyard —
# pinning a scarce spoke tractor (Stoke: 5) to a ~200 km collection to save ~2 km.
# A spoke may win only if its combined deadhead beats the best NON-spoke base by more
# than this margin (the scarcity premium of committing a spoke tractor). Empirically
# the mis-assignments save <=33 km while genuine northern spoke work saves 100s of km,
# so the gap is wide; 75 km sits cleanly between them.
SPOKE_SCARCITY_MARGIN_KM: float = 75.0


def _nearest_base(among: Tuple[str, ...], lat: float, lon: float) -> str:
    """Return the nearest of the given depot bases to a point."""
    return min(among,
              key=lambda d: _haversine_km(_STAGING_ANCHORS[d][0], _STAGING_ANCHORS[d][1], lat, lon))


def combined_staging_depot(o_lat: float, o_lon: float, d_lat: float, d_lon: float) -> str:
    """FULL_FLEET (2026-07-20): the base minimising the COMBINED collect+deliver
    deadhead. We own both legs and there is no inter-depot trunk for full-fleet, so
    staging both ends at a single depot is teleport-safe — and the depot nearest the
    (collection, delivery) PAIR beats the one nearest either endpoint alone when the
    two are in different regions (e.g. a Yorkshire collection with a Cambs delivery
    stages at central Bedford, not the collection-nearest Stoke).

    Spoke cap (two-sided): bases in SPOKE_DELIVERY_RADIUS_KM are only eligible when
    the delivery point is within their radius (the km-greedy argmin is capacity-blind
    and would otherwise funnel national deliveries onto Stoke's 5 tractors); AND a spoke
    only wins the argmin when it beats the best NON-spoke base by more than
    SPOKE_SCARCITY_MARGIN_KM, so a delivery near the spoke can't drag a scarce spoke
    tractor to a far collection in a hub's backyard for a trivial km saving (#5)."""
    def _combined(b: str) -> float:
        a_lat, a_lon = _STAGING_ANCHORS[b]
        return _haversine_km(a_lat, a_lon, o_lat, o_lon) + _haversine_km(a_lat, a_lon, d_lat, d_lon)
    bases = tuple(b for b in COLLECTION_BASES
                  if _haversine_km(_STAGING_ANCHORS[b][0], _STAGING_ANCHORS[b][1], d_lat, d_lon)
                  <= SPOKE_DELIVERY_RADIUS_KM.get(b, float("inf"))) or COLLECTION_BASES
    best = min(bases, key=_combined)
    # collection-side scarcity guard: a spoke keeps the tour only if it is meaningfully
    # cheaper than the best hub; else the hub (ample fleet) stages both legs.
    if best in SPOKE_DELIVERY_RADIUS_KM:
        hubs = [b for b in bases if b not in SPOKE_DELIVERY_RADIUS_KM]
        if hubs:
            best_hub = min(hubs, key=_combined)
            if _combined(best_hub) - _combined(best) <= SPOKE_SCARCITY_MARGIN_KM:
                best = best_hub
    return best


def resolve_staging_depot(pc: Optional[str], *, is_delivery_anchor: bool,
                          lat: Optional[float] = None,
                          lon: Optional[float] = None) -> str:
    """Map an (already-cleaned) postcode to a real staging depot.

    In-territory postcodes return their owning depot unchanged (incl. STOKE).
    OVERFLOW freight resolves to a real base instead of a virtual/nearest anchor:
      * a delivery anchor (import last-mile needs a dock) -> CB22 (capability-primary
        gateway; the dockless Stoke ST4 satellite is never a delivery base);
      * a collection anchor (no dock needed) -> the nearest COLLECTION base, which
        includes the Stoke satellite (a legitimate northern collection base).
    """
    depot = assign_depot(pc)
    if depot != 'OVERFLOW':
        return depot
    if is_delivery_anchor:
        return 'CB22'
    if lat is not None and lon is not None:
        return _nearest_base(COLLECTION_BASES, lat, lon)
    return 'CB22'


def _normalise_reg(reg: str) -> str:
    """Normalise a single vehicle registration string.

    Strips trailing trailer-index suffix (e.g. 'N888RNW 2' ->'N888RNW'),
    then collapses internal spaces ('P888 RNW' ->'P888RNW').
    Order matters: suffix strip must run before space-collapse.
    """
    reg = reg.strip().upper()
    reg = _re.sub(r'\s+\d+$', '', reg)   # strip trailing space+digit(s)
    reg = _re.sub(r'\s+', '', reg)        # collapse remaining spaces
    return reg


def _split_regs(raw: str) -> list[str]:
    """Split a comma-joined registration field and normalise each part.

    Returns a list of non-empty normalised registrations.
    e.g. 'LN67SWJ, T88RNW' ->['LN67SWJ', 'T88RNW']
    e.g. 'N888RNW 2' ->['N888RNW']
    """
    if not raw:
        return []
    return [_normalise_reg(p) for p in raw.split(',') if _normalise_reg(p)]



def _split_csv_values(raw) -> list[str]:
    """Split a comma-joined Qargo text field into stripped non-empty values."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return []
    return [p.strip() for p in str(raw).split(',') if p.strip() and p.strip().lower() != 'nan']


def _shipment_trip_count(row: pd.Series) -> int:
    return len(_split_csv_values(row.get('shipment_names')))


def _powered_vehicle_regs(row: pd.Series) -> set[str]:
    """Return unique powered fleet vehicle registrations on the row.

    Trailers and drawbar trailers are deliberately excluded because they are
    not independent vehicles for the trip-count comparison.
    """
    regs: set[str] = set()
    for col in ('resource_rigid', 'resource_tractor', 'resource_van'):
        raw = row.get(col)
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            continue
        raw_str = str(raw).strip()
        if not raw_str or raw_str.lower() in ('nan', 'none', '<na>'):
            continue
        regs.update(_split_regs(raw_str))
    return regs


def _powered_vehicle_reg_list(row: pd.Series) -> list[str]:
    """Return powered registrations in Qargo order, preserving first occurrence."""
    regs: list[str] = []
    seen: set[str] = set()
    for col in ('resource_rigid', 'resource_tractor', 'resource_van'):
        raw = row.get(col)
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            continue
        raw_str = str(raw).strip()
        if not raw_str or raw_str.lower() in ('nan', 'none', '<na>'):
            continue
        for reg in _split_regs(raw_str):
            if reg and reg not in seen:
                seen.add(reg)
                regs.append(reg)
    return regs


def _movement_unit_count(row: pd.Series, flow: Optional[FlowTag]) -> int:
    """Number of dispatchable units for one commercial order.

    Split only when Qargo gives enough physical evidence. Multiple shipment
    names plus multiple powered vehicles is the normal full-fleet multi-unit
    shape. A multi-shipment row with one powered vehicle is split only when the
    parent load cannot fit one artic, because otherwise it may simply be one
    local leg plus a non-local continuation.
    """
    if flow != 'FULL_FLEET':
        return 1
    ships = _shipment_trip_count(row)
    powered = len(_powered_vehicle_regs(row))
    try:
        pallets = float(row.get('goods_pallet_spaces') or 0.0)
    except (TypeError, ValueError):
        pallets = 0.0
    try:
        kg = float(row.get('goods_weight') or 0.0)
    except (TypeError, ValueError):
        kg = 0.0
    over_single_artic = pallets > 26 or kg > 26000
    if ships > 1 and powered > 1:
        return max(ships, powered)
    if ships > 1 and over_single_artic:
        return ships
    return 1


def _movement_unit_id(order_id: str, unit_idx: int, units: int) -> str:
    """Stable child id used by routing/manifest for split movement units."""
    return order_id if units <= 1 else f'{order_id}#U{unit_idx}'


def _unit_share(value, units: int) -> float:
    try:
        return float(value or 0.0) / max(1, units)
    except (TypeError, ValueError):
        return 0.0

def is_partial_fleet_unknown(row: pd.Series) -> bool:
    """True when Qargo shows a partial fleet leg but not the missing flow.

    After Palletline/Hazchem classification has failed, a multi-trip shipment
    with exactly one powered fleet vehicle means our fleet handled only one leg
    of the physical movement. Qargo does not tell us whether that leg is the
    collection or delivery in forward mode, so the dispatcher must skip it.
    """
    return _shipment_trip_count(row) >= 2 and len(_powered_vehicle_regs(row)) == 1


def _is_structural_full_fleet(row: pd.Series) -> bool:
    trip_count = _shipment_trip_count(row)
    if trip_count <= 0:
        return False
    return trip_count == len(_powered_vehicle_regs(row))


def _build_trip_maps(qargo_df: pd.DataFrame) -> tuple[dict, dict]:
    """Build two lookup dicts from single-vehicle Qargo rows.

    Returns:
        trip_to_vehicle: trip_id ->normalised registration (from single-reg rows only)
        trip_to_dest_areas: trip_id ->set of 2-char destination postcode prefixes
    """
    trip_to_vehicle: dict[str, str] = {}
    trip_to_dest_areas: dict[str, set] = {}
    for _, row in qargo_df.iterrows():
        raw_rigid = str(row.get('resource_rigid', '') or '').strip()
        raw_tractor = str(row.get('resource_tractor', '') or '').strip()
        # Only use rows with a single unambiguous vehicle
        if ',' in raw_rigid or ',' in raw_tractor:
            continue
        vehicle = _normalise_reg(raw_rigid) if raw_rigid and raw_rigid.lower() != 'nan' else ''
        if not vehicle:
            vehicle = _normalise_reg(raw_tractor) if raw_tractor and raw_tractor.lower() != 'nan' else ''
        if not vehicle:
            continue
        dest_pc = str(row.get('destination_postal_code', '') or '').strip()[:2].upper()
        for trip in str(row.get('shipment_names', '') or '').split(','):
            trip = trip.strip()
            if not trip:
                continue
            trip_to_vehicle.setdefault(trip, vehicle)
            if dest_pc:
                trip_to_dest_areas.setdefault(trip, set()).add(dest_pc)
    return trip_to_vehicle, trip_to_dest_areas


def classify_order(row: pd.Series) -> Optional[FlowTag]:
    """Classify a Qargo order row into a Cambridge flow tag.

    Returns 'PL_IMPORT' for orders Palletline brought in (delivery only),
    'FULL_FLEET' for orders we own end-to-end (collection + delivery),
    'PL_EXPORT' for outbound Palletline collections (we collect from local
        shippers and deliver to the Palletline hub for onward distribution),
    or None for orders out of scope (hazmat, sub-only, ambiguous).
    """
    transport = str(row.get('transport_service', '') or '')
    if 'Specialist Movement' in transport or 'Crane Hire' in transport:
        return None

    import_type = row.get('order_import_integration_type')
    import_type_str = '' if import_type is None or (isinstance(import_type, float) and pd.isna(import_type)) else str(import_type)
    sub = row.get('resource_subcontractor')
    sub_str = '' if sub is None or (isinstance(sub, float) and pd.isna(sub)) else str(sub)

    if import_type_str == 'PALLETLINE' and 'import from API' in sub_str:
        return 'PL_IMPORT'

    if import_type_str == 'PALLETLINE' and not sub_str:
        # No subcontractor: Palletline commissioned us for direct delivery.
        return 'FULL_FLEET'

    if 'Palletline (export to API)' in sub_str:
        return 'PL_EXPORT'

    # Hazchem Network operates identically to Palletline (collect ->trunk to hub ->network
    # delivers onward) but for hazardous shipments, using the LE10 3BS Hinckley hub.
    if import_type_str == 'HAZCHEM':
        if 'export to API' in sub_str:
            return 'PL_EXPORT'
        return 'PL_IMPORT'  # all other HAZCHEM are last-mile inbound deliveries

    if 'Hazchem (export to API)' in sub_str:
        return 'PL_EXPORT'

    # Non-network work is classified from Qargo's physical structure instead of
    # fixed subcontractor names. If each shipment trip has one powered fleet
    # vehicle, our fleet handled the movement end-to-end. If a multi-trip row
    # has only one powered vehicle, it is an in-universe partial fleet leg whose
    # collect/deliver direction is not available in forward mode.
    if import_type_str in ('MANUAL', 'CLARUS', ''):
        if is_partial_fleet_unknown(row):
            return None
        if _is_structural_full_fleet(row):
            return 'FULL_FLEET'
        # Legacy/minimal rows may not carry shipment_names. Preserve the old
        # no-subcontractor interpretation only when trip structure is absent.
        if not sub_str and _shipment_trip_count(row) == 0:
            return 'FULL_FLEET'

    return None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate great-circle distance in kilometers using the Haversine formula."""
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _postcode_prefix(pc: str) -> str:
    """Extract outward area code (first 1-2 letters) from postcode."""
    pc = pc.strip().upper()
    return ''.join(c for c in pc[:2] if c.isalpha())


MAX_LOCAL_ROUNDTRIP_ROAD_KM = 400.0
LOCAL_FEASIBILITY_BUFFER_MIN = 30.0


def _nearest_depot_roundtrip_road_km(stop_lat: float, stop_lon: float) -> float:
    depot_distances = (
        _haversine_km(CB22_DEPOT_ANCHOR[0], CB22_DEPOT_ANCHOR[1], stop_lat, stop_lon),
        _haversine_km(BEDFORD_DEPOT_ANCHOR[0], BEDFORD_DEPOT_ANCHOR[1], stop_lat, stop_lon),
    )
    one_way_road_km = min(depot_distances) * ROAD_DISTANCE_FACTOR
    return one_way_road_km * 2.0


def _tractor_day_distance_feasible(stop_lat: float, stop_lon: float) -> bool:
    """Return whether a stop is inside the same-day tractor distance envelope."""
    return _nearest_depot_roundtrip_road_km(stop_lat, stop_lon) <= MAX_LOCAL_ROUNDTRIP_ROAD_KM

def _same_day_local_feasible(
    stop_lat: float,
    stop_lon: float,
    pallets: float,
    window: Tuple[datetime, datetime],
) -> bool:
    """Return whether a remote stop is realistic as same-day local work."""
    roundtrip_road_km = _nearest_depot_roundtrip_road_km(stop_lat, stop_lon)
    if roundtrip_road_km > MAX_LOCAL_ROUNDTRIP_ROAD_KM:
        return False

    speed = max(float(AVG_SPEED_KMH or 1.0), 1.0)
    drive_min = (roundtrip_road_km / speed) * 60.0
    service_min = customer_service_minutes("tractor")
    duty_min = drive_min + service_min + LOCAL_FEASIBILITY_BUFFER_MIN

    day = window[0].date()
    shift_start = datetime.combine(day, TRACTOR_TYPE_SHIFT_START)
    shift_end = datetime.combine(day, TRACTOR_TYPE_SHIFT_END)
    usable_start = max(shift_start, window[0])
    usable_end = min(shift_end, window[1])
    if usable_end <= usable_start:
        return False
    return duty_min <= (usable_end - usable_start).total_seconds() / 60.0



@dataclass
class WindowPolicy:
    raw_window: Tuple[datetime, datetime]
    effective_window: Tuple[datetime, datetime]
    hardness: str
    reason: str


@dataclass
class OrderFeasibility:
    rigid_same_day_feasible: bool = False
    tractor_same_day_feasible: bool = False
    same_day_feasible_depots: list[str] = field(default_factory=list)
    requires_multiday: bool = False
    reason: str = ''
    nearest_depot_roundtrip_km: float = 0.0
    estimated_drive_min: float = 0.0
    estimated_service_min: float = 0.0
    usable_window_min: float = 0.0
    window_hardness: str = 'unknown'
def in_cambridge_scope(row: pd.Series, flow: FlowTag,
                       postcode_cache: dict) -> bool:
    """Decide whether a classified order is in Cambridge's scope.

    PL_IMPORT: assign_depot(dest_pc) must return a known depot (CB22/BEDFORD/ST_IVES).
        OVERFLOW means the destination belongs to a different Palletline member.

    FULL_FLEET / PL_EXPORT: origin postcode must be geocodable (in the postcode
        cache).  No distance cap is applied ->if the order is in Qargo it is a
        real customer waiting to be served.  Route feasibility is enforced by
        the VRPTW engine at planning time.
    """
    if flow == 'PL_IMPORT':
        dest_pc = str(row.get('destination_postal_code', '')).strip().upper()
        # Accept any destination that falls within any depot's territory.
        # assign_depot() is the authoritative territory map; OVERFLOW means the
        # destination belongs to a different Palletline member, not us.
        return assign_depot(dest_pc) in ('CB22', 'BEDFORD', 'ST_IVES', 'STOKE')

    # FULL_FLEET and PL_EXPORT: origin must be geocodable; no distance cap.
    origin_pc = str(row.get('origin_postal_code', '')).strip().upper()
    return _lookup_coords(origin_pc, postcode_cache) is not None


def _served_postcodes(row: pd.Series, flow: FlowTag) -> list:
    """The endpoint postcode(s) the fleet physically serves: origin for a
    collection, destination for a delivery, both for full-fleet. Used to screen
    out orders whose served endpoint will not geocode (the un-routable class)."""
    o = str(row.get('origin_postal_code', '') or '').strip().upper()
    d = str(row.get('destination_postal_code', '') or '').strip().upper()
    if flow in ('PL_EXPORT', 'LOCAL_COLLECT'):
        return [o]
    if flow == 'FULL_FLEET':
        return [o, d]
    return [d]   # PL_IMPORT, LOCAL_DELIVER


def served_endpoint_unroutable(row: pd.Series, flow: FlowTag,
                               postcode_cache: dict) -> bool:
    """True when an endpoint the fleet must serve will not geocode even after a
    lookup, so the leg cannot be routed (DUBLIN/CARLOW/garbled-postcode class).
    Independent of the fleet-confirmed override: a recorded vehicle does not make
    a non-geocoding destination routable."""
    if flow is None:
        return False
    return any(_lookup_coords(pc, postcode_cache) is None
               for pc in _served_postcodes(row, flow))


def in_tractor_scope(row: pd.Series, flow: FlowTag,
                     postcode_cache: dict) -> bool:
    """Scope check for tractor-assigned PL_EXPORT long-haul collections.

    Allows origins up to TRACTOR_CATCHMENT_RADIUS_KM from CB22 ->significantly
    larger than the rigid CATCHMENT_RADIUS_KM. Only applies to PL_EXPORT.

    Returns False for all non-PL_EXPORT flows so callers can use it
    unconditionally without a flow guard.
    """
    if flow != 'PL_EXPORT':
        return False
    from freight_planner.shared.config import TRACTOR_CATCHMENT_RADIUS_KM
    origin_pc = str(row.get('origin_postal_code', '')).strip().upper()
    origin_coords = _cached_coords(origin_pc, postcode_cache)
    if origin_coords is None:
        return False
    if isinstance(origin_coords, dict):
        olat, olon = origin_coords['lat'], origin_coords['lon']
    else:
        olat, olon = origin_coords
    dlat, dlon = CB22_DEPOT_ANCHOR
    return _haversine_km(olat, olon, dlat, dlon) <= TRACTOR_CATCHMENT_RADIUS_KM


@dataclass
class ScopedOrder:
    order_id: str
    name: str
    flow: FlowTag
    origin_pc: Optional[str]              # None for PL_IMPORT
    destination_pc: str
    weight_kg: float
    pallets: float
    delivery_window: Tuple[datetime, datetime]
    # PL_IMPORT/FULL_FLEET: delivery window at destination.
    # PL_EXPORT: collection window at origin (when shipper is available to hand over freight).
    collection_window: Optional[Tuple[datetime, datetime]]
    effective_dispatch_window: Optional[Tuple[datetime, datetime]] = None
    window_hardness: str = 'unknown'
    feasibility: Optional[OrderFeasibility] = None
    time_window_value: Optional[str] = None  # Qargo destination_time_window_value (e.g. '09:00 - 12:00')
    requested_start_raw: Optional[datetime] = None  # raw destination_requested_start_timestamp_local from Qargo (preserves 00:00 for date-only entries)
    resource_rigid: Optional[str] = None
    resource_tractor: Optional[str] = None
    shipment_names: Optional[str] = None    # raw Qargo shipment_names (pipe/comma-separated Trip-IDs)
    subcontractor: Optional[str] = None    # raw resource_subcontractor; used to route PL_EXPORT to B37 vs LE10
    stop_type: Literal['delivery', 'pickup'] = 'delivery'
    # 'pickup' for PL_EXPORT: the stop is at origin_pc (shipper collection point).
    # 'delivery' for PL_IMPORT and FULL_FLEET: stop is at destination_pc.
    depot_id: str = 'CB22'

    # Two-phase planning fields
    order_class: OrderClass = OrderClass.LOCAL
    # LOCAL  ->same-day out-and-back from depot
    # TOUR   ->multi-day vehicle deployment (OVERFLOW destinations)
    # TRUNK  ->flows through B37/LE10 hub (PL_EXPORT pickups)

    timestamp_created: Optional[datetime] = None
    # When the order became visible in Qargo (from 'timestamp_created' column).
    # Used by Phase 1 to determine how far ahead each order can be planned.

    @property
    def delivery_date(self):
        """Calendar date on which this order is due for delivery/pickup."""
        from datetime import date as _date
        return self.delivery_window[0].date()

    @property
    def release_time(self) -> datetime:
        """Earliest a vehicle may load this order.

        PL_IMPORT: goods arrive at the depot via the unmodelled "invisible hub"
            resource we do not own (treated as spawning at the depot) — they are NOT
            carried on our night trunk (which is export-only). Conservative depot
            release at 06:00 on delivery day.
        All other flows: the order is loadable from timestamp_created (or
            immediately if timestamp_created is absent).
        """
        if self.flow == 'PL_IMPORT':
            from datetime import date as _date
            return datetime.combine(self.delivery_date, time(6, 0))
        if self.timestamp_created is not None:
            return self.timestamp_created
        return self.delivery_window[0]


def _parse_twv(twv, date_anchor) -> 'Tuple[datetime, datetime] | None':
    """Parse destination_time_window_value into (window_start, window_end).

    Three-case priority (mirrors on_time.py Case A semantics):
      'HH:MM - HH:MM'  start < end  ->real slot; vehicle must arrive in [start, end]
      'HH:MM - HH:MM'  start == end ->point-in-time deadline; window opens at OPERATING_DAY_START
      'HH:MM' / 'HH:MM:SS'         ->deadline only; window opens at OPERATING_DAY_START
    Returns None when the value is absent or unparseable.
    """
    if not twv or pd.isna(twv):
        return None
    s = str(twv).strip()
    if not s or s in ('nan', 'None', '0', '00:00:00', '00:00'):
        return None

    def _t(raw: str) -> 'time | None':
        segs = raw.strip().split(':')
        try:
            hh, mm = int(segs[0]), int(segs[1]) if len(segs) > 1 else 0
        except (ValueError, IndexError):
            return None
        from datetime import time as _time
        return _time(hh, mm) if 0 <= hh < 24 and 0 <= mm < 60 else None

    if ' - ' in s:
        left, right = s.split(' - ', 1)
        start_t, end_t = _t(left), _t(right)
        if start_t is None or end_t is None:
            return None
        # Same start/end = point-in-time deadline; no "not-before" constraint.
        ws = CUSTOMER_DAY_START if start_t == end_t else start_t
        return datetime.combine(date_anchor, ws), datetime.combine(date_anchor, end_t)
    else:
        # Single time = deadline only.
        end_t = _t(s)
        if end_t is None:
            return None
        return (datetime.combine(date_anchor, CUSTOMER_DAY_START),
                datetime.combine(date_anchor, end_t))



def _operating_day_window(day) -> Tuple[datetime, datetime]:
    return datetime.combine(day, CUSTOMER_DAY_START), datetime.combine(day, OPERATING_DAY_END)


def _is_midnight_marker(ts: datetime) -> bool:
    return ts.hour == 0 and ts.minute == 0


def _is_pickup_placeholder_time(ts: datetime) -> bool:
    """True when a pickup-side requested time is acting as a day marker.

    In the historical Qargo feed, pickup requests often appear as either:
      - 00:00 (pure date-only marker), or
      - exactly OPERATING_DAY_START (typically 06:00), which operators have
        treated as "available at some point that day", not a hard must-start.
    """
    return _is_midnight_marker(ts) or ts.time() == OPERATING_DAY_START


def _pickup_anchor_timestamp(row: pd.Series) -> datetime:
    """Choose the pickup-side planning anchor without overfitting to hindsight.

    Forward planning should respect a specific requested pickup time when one is
    present, but should *not* harden day-level placeholder values (00:00 / day
    start) into artificial route constraints. We still keep the actual origin
    timestamp's DATE when the freight was clearly rescheduled onto a different
    day than originally requested, because otherwise the order would land in
    the wrong planning day entirely — but never its time-of-day: collections
    never comply with historical actual execution times (stakeholder rule,
    2026-07-04). A midnight marker on the actual's date lets
    _is_pickup_placeholder_time expand it to the operating day downstream.
    """
    actual = pd.to_datetime(row.get('origin_timestamp_local'), errors='coerce')
    requested = pd.to_datetime(row.get('origin_requested_start_timestamp_local'), errors='coerce')

    if not pd.isna(requested):
        if not pd.isna(actual) and actual.date() != requested.date():
            return datetime.combine(actual.date(), time(0, 0))
        return requested.to_pydatetime()

    if not pd.isna(actual):
        return datetime.combine(actual.date(), time(0, 0))

    day = datetime.now().date()
    return datetime.combine(day, OPERATING_DAY_START)


def _parse_twv_with_hardness(twv, date_anchor) -> WindowPolicy | None:
    s = str(twv or '').strip()
    if ' - ' in s:
        left, right = (part.strip() for part in s.split(' - ', 1))
        midnight_markers = {'00:00', '00:00:00'}
        if left in midnight_markers and right in midnight_markers:
            window = _operating_day_window(date_anchor)
            return WindowPolicy(window, window, 'date_only', 'date_only_time_window')
    parsed = _parse_twv(twv, date_anchor)
    if parsed is None:
        return None
    hardness = 'soft_deadline'
    reason = 'explicit_deadline'
    if ' - ' in s:
        left, right = s.split(' - ', 1)
        if left.strip() != right.strip():
            hardness = 'hard_slot'
            reason = 'explicit_time_window'
    # 2026-07-18 (soft delivery windows): the EFFECTIVE window is the operating-day
    # bound for EVERY class — earliest_start/latest_finish become the hard duty bound.
    # The tight customer window survives in raw_window and is enforced SOFTLY
    # downstream (convex earliness/tardiness penalty), not as a hard cutoff, so the
    # solver delivers slightly late rather than slipping a whole day.
    effective = _operating_day_window(date_anchor)
    return WindowPolicy(raw_window=parsed, effective_window=effective,
                        hardness=hardness, reason=reason)


def _delivery_window_policy(row: pd.Series) -> WindowPolicy:
    req_ts = pd.to_datetime(row.get('destination_requested_start_timestamp_local'), errors='coerce')
    raw_date = pd.to_datetime(row.get('destination_date'), errors='coerce')
    if pd.isna(req_ts) and pd.isna(raw_date):
        day = datetime.now().date()
    else:
        day = raw_date.date() if not pd.isna(raw_date) else req_ts.date()

    twv_policy = _parse_twv_with_hardness(row.get('destination_time_window_value'), day)
    if twv_policy is not None:
        return twv_policy

    if pd.isna(req_ts):
        window = _operating_day_window(day)
        return WindowPolicy(window, window, 'unknown', 'missing_requested_start')

    if req_ts.hour == 0 and req_ts.minute == 0:
        window = _operating_day_window(day)
        return WindowPolicy(window, window, 'date_only', 'date_only_requested_start')

    raw = (datetime.combine(day, CUSTOMER_DAY_START), datetime.combine(day, req_ts.time()))
    effective = _operating_day_window(day)
    return WindowPolicy(raw, effective, 'soft_deadline', 'requested_timestamp_soft_deadline')


def _estimate_feasibility(
    stop_lat: float,
    stop_lon: float,
    pallets: float,
    window: Tuple[datetime, datetime],
    window_hardness: str,
) -> OrderFeasibility:
    roundtrip_road_km = _nearest_depot_roundtrip_road_km(stop_lat, stop_lon)
    speed = max(float(AVG_SPEED_KMH or 1.0), 1.0)
    drive_min = (roundtrip_road_km / speed) * 60.0
    service_min = customer_service_minutes("tractor")
    duty_min = drive_min + service_min + LOCAL_FEASIBILITY_BUFFER_MIN

    day = window[0].date()
    shift_start = datetime.combine(day, TRACTOR_TYPE_SHIFT_START)
    shift_end = datetime.combine(day, TRACTOR_TYPE_SHIFT_END)
    usable_start = max(shift_start, window[0])
    usable_end = min(shift_end, window[1])
    usable_min = max(0.0, (usable_end - usable_start).total_seconds() / 60.0)
    tractor_feasible = roundtrip_road_km <= MAX_LOCAL_ROUNDTRIP_ROAD_KM and duty_min <= usable_min
    reason = 'tractor_same_day_feasible' if tractor_feasible else 'same_day_infeasible'
    return OrderFeasibility(
        rigid_same_day_feasible=False,
        tractor_same_day_feasible=tractor_feasible,
        same_day_feasible_depots=['NEAREST'] if tractor_feasible else [],
        requires_multiday=not tractor_feasible,
        reason=reason,
        nearest_depot_roundtrip_km=roundtrip_road_km,
        estimated_drive_min=drive_min,
        estimated_service_min=service_min,
        usable_window_min=usable_min,
        window_hardness=window_hardness,
    )
def _delivery_window(row: pd.Series) -> Tuple[datetime, datetime]:
    """Compute raw (window_start, window_end) for a delivery stop."""
    return _delivery_window_policy(row).raw_window


def _collection_window(row: pd.Series) -> Tuple[datetime, datetime]:
    """For FULL_FLEET, derive collection window from pickup-side planning data.

    Requested pickup timestamps that are date markers (00:00) or operating-day
    placeholders (06:00) should not create fake narrow starts in forward mode;
    they expand to the operating day. A genuine reschedule onto a different day
    still follows the actual origin timestamp so the collection lands in the
    correct planning day.
    """
    start = _pickup_anchor_timestamp(row)
    if _is_pickup_placeholder_time(start):
        start = datetime.combine(start.date(), CUSTOMER_DAY_START)
        end = datetime.combine(start.date(), OPERATING_DAY_END)
    else:
        end = start + timedelta(hours=DEFAULT_WINDOW_HOURS)
    return start, end


def _pl_export_window(row: pd.Series) -> Tuple[datetime, datetime]:
    """Derive the PL_EXPORT collection window.

    Start = pickup planning anchor. Specific requested times remain specific;
            date-only / day-start placeholders expand to the operating day.
            Actual origin_timestamp is only allowed to override when the freight
            was genuinely moved onto a different calendar day than requested.
    End   = trunk departure time on the same calendar day (18:00).
    """
    coll_start = _pickup_anchor_timestamp(row)
    if _is_pickup_placeholder_time(coll_start):
        coll_start = datetime.combine(coll_start.date(), CUSTOMER_DAY_START)
    # Freight must be staged at depot TRUNK_PREP_MARGIN_H before trunk departs.
    trunk_deadline = datetime.combine(
        coll_start.date(),
        time(TRUNK_DEPART_HOUR - int(TRUNK_PREP_MARGIN_H), 0),
    )
    return coll_start, trunk_deadline


def _resource_rigid(row: pd.Series) -> Optional[str]:
    """Extract and normalise Qargo resource_rigid.

    For single-registration rows: normalise and return.
    For comma-joined rows: returns the raw (unnormalised) string ->callers
    that need per-vehicle attribution should use _split_regs() directly.
    """
    v = row.get('resource_rigid')
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if not s or s.lower() == 'nan':
        return None
    if ',' in s:
        return s  # raw; caller splits with _split_regs if needed
    return _normalise_reg(s)


def _resource_tractor(row: pd.Series) -> Optional[str]:
    """Extract and normalise Qargo resource_tractor.

    For single-registration rows: normalise and return.
    For comma-joined rows: returns the raw (unnormalised) string.
    """
    v = row.get('resource_tractor')
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if not s or s.lower() == 'nan':
        return None
    if ',' in s:
        return s
    return _normalise_reg(s)


def build_scoped_orders(qargo_df: pd.DataFrame,
                        postcode_cache: dict,
                        day=None,
                        split_movement_units: bool = False) -> list[ScopedOrder]:
    """Classify and scope-filter every Qargo row; return in-scope orders only.

    REDUNDANT (2026-07-19): DEAD in the live pipeline — no live `freight_planner`
    caller. Order scope is now settled UPSTREAM by data cleaning (enriched
    `candidate_df` carrying `corrected_flow`); the only callers of this function
    are the archived `cambridge.scope` pipeline and `investigations/*`. The
    fleet-confirmed clause is also redundant on an already-in-universe dataset
    (fleet-confirmed is universally true, so the geography fallback never fires).
    Retained for the universe-accounting table; see experiments/REDUNDANT_FILES.md
    and METHODOLOGY_FORMULAS.md §M3-A. (`classify_order`, `assign_depot`, the
    window parsers etc. in THIS file remain live — only this function is dead.)

    Fleet-confirmed orders (explicit vehicle assignment in Qargo) are always
    included regardless of destination geography ->an operator assignment is
    proof the job was ours.  Orders with no assigned vehicle fall through to the
    geography filter so only our franchise territory is planned.
    """
    out: list[ScopedOrder] = []
    trip_to_vehicle, trip_to_dest_areas = _build_trip_maps(qargo_df)

    def _delivery_rigid_for_row(raw: Optional[str], _row: pd.Series) -> Optional[str]:
        """Resolve the delivery-vehicle registration for a row.

        For single-reg rows: return normalised reg.
        For comma-joined rows: use trip maps to find which vehicle delivered
        to the destination area; fall back to first CB22 match or first part.
        """
        if not raw or (isinstance(raw, str) and raw.lower() == 'nan'):
            return None
        if ',' not in raw:
            return _normalise_reg(raw) if raw else None
        parts = _split_regs(raw)
        if len(parts) == 1:
            return parts[0]
        # Use trip maps to find which vehicle delivered to destination
        dest_prefix = _postcode_prefix(
            str(_row.get('destination_postal_code', '') or ''))
        trips = [t.strip() for t in
                 str(_row.get('shipment_names', '') or '').split(',') if t.strip()]
        for trip in trips:
            vehicle = trip_to_vehicle.get(trip)
            if vehicle in parts:
                dest_areas = trip_to_dest_areas.get(trip, set())
                if dest_prefix in dest_areas:
                    return vehicle  # this trip delivers to our destination area
        # Fallback: first match in any fleet, first part otherwise
        from freight_planner.shared.config import ALL_RIGIDS as _ALL_RIGIDS
        fleet_matches = [p for p in parts if p in _ALL_RIGIDS]
        return fleet_matches[0] if fleet_matches else (parts[0] if parts else None)

    _res_cols = ['resource_tractor', 'resource_rigid',
                 'resource_trailer', 'resource_drawbartrailer', 'resource_van']

    def _col_empty(v) -> bool:
        return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() in ('', 'nan', 'None')

    for _, row in qargo_df.iterrows():
        # Drop cancelled orders ->no delivery was ever made.
        _status = str(row.get('status', '') or '').strip().upper()
        if _status == 'CANCELLED':
            continue

        # Drop orders where no fleet vehicle touched the job.  All-empty resource
        # columns means a subcontractor or external carrier handled it; our
        # dispatcher should not plan or count these.
        if all(_col_empty(row.get(c)) for c in _res_cols if c in row.index):
            continue

        flow = corrected_flow(row, classify_order(row))
        if flow is None:
            continue

        # Un-routable guard: an order whose served endpoint will not geocode
        # cannot be planned (the DUBLIN/CARLOW/garbled-postcode unassignment
        # class). Runs BEFORE the fleet-confirmed override below, because a
        # recorded vehicle does not make a non-geocoding destination routable.
        if served_endpoint_unroutable(row, flow, postcode_cache):
            continue

        rig = _resource_rigid(row)
        tractor = _resource_tractor(row)

        # Expand comma-joined registrations into normalised sets
        rig_set     = set(_split_regs(rig))     if rig     and ',' in rig     else ({_normalise_reg(rig)}     if rig     else set())
        tractor_set = set(_split_regs(tractor)) if tractor and ',' in tractor else ({_normalise_reg(tractor)} if tractor else set())

        # resource_van: service vans are part of the rigid asset class
        _v_van = row.get('resource_van')
        _van_raw = (None if _v_van is None or (isinstance(_v_van, float) and pd.isna(_v_van))
                    else str(_v_van).strip() or None)
        if _van_raw and _van_raw.lower() == 'nan':
            _van_raw = None
        van_set = (set(_split_regs(_van_raw)) if _van_raw and ',' in _van_raw
                   else ({_normalise_reg(_van_raw)} if _van_raw else set()))

        from freight_planner.shared.config import ALL_RIGIDS, ALL_TRACTORS
        # Any non-empty van assignment is fleet-confirmed without a master-list
        # check ->vans are always our vehicles if recorded in Qargo.
        # Rigids and tractors must appear in the master fleet to confirm.
        _fleet_confirmed = bool(van_set) or bool((rig_set | tractor_set) & (ALL_RIGIDS | ALL_TRACTORS))

        # Fleet-confirmed orders are always included ->an explicit vehicle
        # assignment overrides the geographic scope check.  Orders with no
        # assigned vehicle fall through to the geography filter.
        if not _fleet_confirmed and not in_cambridge_scope(row, flow, postcode_cache):
            continue

        # FULL_FLEET orders where we collect today but deliver a future day
        # are broken into two trips: collect at origin_pc today (cross-dock),
        # then deliver from depot on the delivery day. Only the collection stop
        # is modelled here; the delivery appears in tomorrow's plan.
        _origin_dt = pd.to_datetime(row.get('origin_timestamp_local'), errors='coerce')
        _dest_dt   = pd.to_datetime(row.get('destination_timestamp_local'), errors='coerce')
        is_collect_only = (
            day is not None
            and flow == 'FULL_FLEET'
            and not pd.isna(_origin_dt) and not pd.isna(_dest_dt)
            and _origin_dt.date() == day
            and _dest_dt.date() > day
        )

        if is_collect_only:
            _stop_type      = 'pickup'
            _delivery_win   = _collection_window(row)   # constrain arrival at origin_pc
            _collection_win = None                       # pickup IS the stop; no separate leg
            _effective_dispatch_win = _delivery_win
            _window_hardness = 'pickup_window'
        elif flow == 'PL_EXPORT':
            _stop_type      = 'pickup'
            _delivery_win   = _pl_export_window(row)
            _collection_win = None
            _effective_dispatch_win = _delivery_win
            _window_hardness = 'pickup_window'
        elif flow == 'LOCAL_COLLECT':
            # Non-network single-leg pickup at origin; no onward trunk.
            _stop_type      = 'pickup'
            _delivery_win   = _collection_window(row)
            _collection_win = None
            _effective_dispatch_win = _delivery_win
            _window_hardness = 'pickup_window'
        else:
            _stop_type      = 'delivery'
            _window_policy  = _delivery_window_policy(row)
            _delivery_win   = _window_policy.raw_window
            _effective_dispatch_win = _window_policy.effective_window
            _window_hardness = _window_policy.hardness
            _collection_win = _collection_window(row) if flow == 'FULL_FLEET' else None

        _sub = row.get('resource_subcontractor')
        _sub_str = ('' if _sub is None or (isinstance(_sub, float) and pd.isna(_sub))
                    else str(_sub))

        # Determine depot assignment from territory map (resolve FC codes first)
        _assign_pc = _resolve_fc_postcode(
            str(row.get('origin_postal_code', '') or '').strip().upper()
            if _stop_type == 'pickup'
            else str(row.get('destination_postal_code', '') or '').strip().upper()
        )
        _depot_id = assign_depot(_assign_pc)

        _feasibility = None

        # Determine order_class from flow and territory
        if flow == 'PL_IMPORT':
            _order_class = OrderClass.LOCAL
        elif _stop_type == 'pickup':
            # PL_EXPORT pickups and FULL_FLEET same-day collections: trunk or local
            _order_class = OrderClass.TRUNK if flow == 'PL_EXPORT' else OrderClass.LOCAL
        elif _depot_id == 'OVERFLOW':
            _oc_pc = (str(row.get('destination_postal_code', '') or '').strip().upper()
                      if _stop_type == 'delivery'
                      else str(row.get('origin_postal_code', '') or '').strip().upper())
            _oc_coords = _lookup_coords(_oc_pc, postcode_cache)
            if _oc_coords:
                _oc_lat, _oc_lon = _oc_coords
                _pallets = float(row.get('goods_pallet_spaces') or 0.0)
                _feasibility = _estimate_feasibility(
                    _oc_lat, _oc_lon, _pallets, _effective_dispatch_win, _window_hardness,
                )
                _order_class = (
                    OrderClass.LOCAL
                    if _feasibility.tractor_same_day_feasible
                    else OrderClass.TOUR
                )
            else:
                _order_class = OrderClass.TOUR  # unknown postcode, assume remote
        else:
            _order_class = OrderClass.LOCAL  # in-territory delivery
        # timestamp_created ->when the order was first locked in Qargo
        _ts_created = None
        if 'timestamp_created' in row.index:
            _ts_raw = pd.to_datetime(row.get('timestamp_created'), errors='coerce')
            if pd.notna(_ts_raw):
                _ts_created = _ts_raw.to_pydatetime()

        _units = (
            _movement_unit_count(row, flow)
            if split_movement_units and _order_class != OrderClass.TOUR
            else 1
        )
        _unit_regs = _powered_vehicle_reg_list(row)
        _base_rigid = _delivery_rigid_for_row(rig, row)
        _base_tractor = _delivery_rigid_for_row(tractor, row) if tractor else None
        for _unit_idx in range(1, _units + 1):
            _unit_id = _movement_unit_id(str(row['order_id']), _unit_idx, _units)
            _unit_reg = _unit_regs[_unit_idx - 1] if _unit_idx - 1 < len(_unit_regs) else None
            _unit_rigid = _base_rigid
            _unit_tractor = _base_tractor
            if _units > 1 and _unit_reg:
                _unit_rigid = _unit_reg if _unit_reg in ALL_RIGIDS else None
                _unit_tractor = _unit_reg if _unit_reg in ALL_TRACTORS else None
            out.append(ScopedOrder(
                order_id=_unit_id,
                name=str(row.get('name', '')),
                flow=flow,
                origin_pc=(_resolve_fc_postcode(str(row['origin_postal_code']).strip().upper())
                           if flow in ('FULL_FLEET', 'PL_EXPORT', 'LOCAL_COLLECT') else None),
                destination_pc=_resolve_fc_postcode(str(row['destination_postal_code']).strip().upper()),
                weight_kg=_unit_share(row.get('goods_weight', 0), _units),
                pallets=_unit_share(row.get('goods_pallet_spaces', 0), _units),
                delivery_window=_delivery_win,
                collection_window=_collection_win,
                effective_dispatch_window=_effective_dispatch_win,
                window_hardness=_window_hardness,
                feasibility=_feasibility,
                time_window_value=(str(row['destination_time_window_value'])
                                   if 'destination_time_window_value' in row.index
                                   and pd.notna(row.get('destination_time_window_value'))
                                   else None),
                requested_start_raw=(pd.to_datetime(row['destination_requested_start_timestamp_local'], errors='coerce').to_pydatetime()
                                     if 'destination_requested_start_timestamp_local' in row.index
                                     and pd.notna(row.get('destination_requested_start_timestamp_local'))
                                     else None),
                resource_rigid=_unit_rigid,
                resource_tractor=_unit_tractor,
                shipment_names=(str(row['shipment_names'])
                                if 'shipment_names' in row.index
                                and pd.notna(row.get('shipment_names'))
                                else None),
                subcontractor=_sub_str if _sub_str else None,
                stop_type=_stop_type,
                depot_id=_depot_id,
                order_class=_order_class,
                timestamp_created=_ts_created,
            ))
    return out





