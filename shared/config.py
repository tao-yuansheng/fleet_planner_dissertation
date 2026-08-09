"""Cambridge dispatcher configuration constants."""
from datetime import time
import csv as _csv
import json as _json
from freight_planner.shared.paths import LOGISTICS_ROOT as _LOGISTICS_ROOT

# Cambridge depot (Duxford CB22 4PS) GPS anchor
CB22_DEPOT_ANCHOR = (52.0859, 0.1717)

# Known depot postcodes -orders with origin_pc in this set were loaded at the depot
# and cannot be carried by a vehicle that is already at a REMOTE overnight location.
DEPOT_POSTCODES: frozenset[str] = frozenset({'CB22 4PS', 'MK42 0LF', 'PE27 3WR', 'ST4 8HP'})

# Bedford depot anchor -MK42 0LF, derived from Jan 2026 telematics
# (dominant overnight parking of the Bedford fleet: ~4.9k pings at MK42 0LF).
# Corrected from a stale NN9 5TA coordinate that did not match the MK42 0LF comment.
BEDFORD_DEPOT_ANCHOR: tuple[float, float] = (52.1225, -0.43149)

# St Ives depot anchor -PE27 3WR
ST_IVES_DEPOT_ANCHOR: tuple[float, float] = (52.33367, -0.06487)

# Stoke-on-Trent satellite base -ST4 8HP, from Jan 2026 telematics (5 tractors
# overnight at ST4 ~52.967,-2.167, e.g. BX67ZFV 33 nights). Feeds the Palletline
# B37 hub directly (~65 km), NOT the SE depots. The same tractors also rotate to
# CB22 on the Midlands corridor and are not forced to return nightly (long-haul
# uses requires_depot_return=False).
STOKE_DEPOT_ANCHOR: tuple[float, float] = (52.9674, -2.1666)

# Depot ID 鈫?depot anchor coords
DEPOT_ANCHORS: dict[str, tuple[float, float]] = {
    'CB22':     (52.0859, 0.1717),   # CB22 4PS Duxford
    'BEDFORD':  (52.1225, -0.43149),
    'ST_IVES':  (52.33367, -0.06487),
    'STOKE':    (52.9674, -2.1666),  # ST4 8HP Stoke-on-Trent satellite
}

# Authoritative fleet source: enriched vehicle list from Supatrak.
# CircuitName is the master assignment -no telematics-threshold heuristics.
_VEHICLE_LIST_CSV = (_LOGISTICS_ROOT
                     / 'data' / 'Input' / 'supatrak'
                     / 'supatrak_vehicle_list_enriched.csv')

# Asset types that count as delivery trucks (excludes Service Van from dispatch pool).
_RIGID_ASSET_TYPES = frozenset({'Lorry', 'Mini Truck', 'Rigid Truck', 'Service Van'})
_TRACTOR_ASSET_TYPES = frozenset({'Tractor Unit'})

# Hardcoded fallbacks used only when the CSV is absent (e.g. fresh checkout).
_FALLBACK_RIGIDS = {
    'HX66DUH', 'T88GNW', 'T88RNW', 'W88RNW', 'LN67SWJ', 'M88GNW',
    'AY18JWA', 'L88GNW', 'BF65WBY', 'AR05DEX',
}
_FALLBACK_TRACTORS = {
    'AR02DEX', 'AR03DEX', 'HX17CVV', 'N88GNW', 'N8GNW', 'R88GNW',
    'S88GNW', 'S88RNW', 'TA70WTL', 'V88GNW', 'V88RNW', 'W88GNW',
    'X88GNW', 'X88RNW', 'X8GNW', 'X8RNW', 'Y888GNW', 'Y88RNW', 'Y90RNW',
}

# Vehicles explicitly excluded from dispatch regardless of their CircuitName.
# Now EMPTY. X888WSM and BU20VHY were previously excluded for having zero telematics,
# but they are valid, active vehicles -telematics absence is a monitoring gap, not
# inactivity (BU20VHY ran 33 Stoke-circuit orders in Jan 2026). They now load into the
# fleet like any other plate: X888WSM via 'St Ives - Artic' -> CB22 tractor; BU20VHY
# via the Recently-Released branch (Tractor Unit) -> CB22 tractor.
_EXCLUDED_VEHICLES: frozenset[str] = frozenset()

# Recently-Released tractors that telematics places at the Stoke base (not CB22):
# BX67ZFV (33 overnights at ST4) and BU69XGK (24). Routed to STOKE below.
_STOKE_RECENTLY_RELEASED: frozenset[str] = frozenset({'BX67ZFV', 'BU69XGK'})


# ---- consolidated vehicle master (2026-07-13): ONE runtime fleet file -------
# tools/build_vehicle_master.py bakes depot + fleet_kind (from CircuitName) and
# the full dispatcher profile into freight_planner/data/vehicle_master.csv.
# When those columns are present, the fleet is read from the master ONLY; the
# supatrak list + profiles JSON stay as REGEN inputs for the master, and the
# legacy builders below serve a checkout without a built master.
_MASTER_FLEET_CSV = (_LOGISTICS_ROOT / 'freight_planner' / 'data' / 'vehicle_master.csv')


def _load_master_fleet_rows() -> list | None:
    if not _MASTER_FLEET_CSV.exists():
        return None
    with open(_MASTER_FLEET_CSV, encoding='utf-8') as f:
        rows = list(_csv.DictReader(f))
    # 2026-07-16 (user rule): telematics behavior columns (shift_*, median_trips,
    # multi_trip, capacity_*_per_trip/source) are no longer part of the master —
    # payload_kg/pallet_capacity are the capacity truth. Tolerate their presence
    # on not-yet-stripped files; never REQUIRE them.
    need = {'reg', 'active', 'depot', 'fleet_kind', 'fleet_reg', 'profile_asset_type',
            'master_max_tonnes', 'master_typical_tonnes',
            'payload_kg', 'pallet_capacity'}
    if not rows or not need <= set(rows[0].keys()):
        return None   # pre-consolidation master: use the legacy builders
    return [r for r in rows
            if r.get('depot', '').strip()
            and str(r.get('active', '')).strip().lower() == 'true']


_MASTER_FLEET_ROWS = _load_master_fleet_rows()


def _canonical_reg(row: dict) -> str:
    """The join-safe registration for a master-fleet row.

    vehicle_master.csv carries two registration-like columns: ``reg`` (normalized by
    build_vehicle_master.py's ``_norm_reg`` -- no spaces, uppercase) and ``fleet_reg``
    (the raw telematics AssetName, only ``.strip()``-ed -- can carry an internal space,
    e.g. "M888 WSM"). Callers used to prefer ``fleet_reg`` unconditionally, so a raw
    spaced AssetName became the vehicle_id every downstream join (capacity resolution,
    VEHICLE_DEPOT_MAP, route_stops) keys on -- no match, silent default-capacity
    fallback for that vehicle (audit #9, 2026-07-26). Prefer ``reg``; fully normalize
    whichever column wins so neither path can leak internal whitespace."""
    reg = str(row.get('reg') or '').strip()
    src = reg if reg else str(row.get('fleet_reg') or '')
    return src.strip().replace(' ', '').upper()


def _load_all_depot_fleets() -> dict[str, tuple[set, set]]:
    """Load rigids and tractors per depot from the enriched vehicle list CSV.

    Returns dict mapping depot_id 鈫?(rigids: set, tractors: set).
    Depots: 'CB22' (Duxford) and 'BEDFORD'.
    ST_IVES is kept as a structural key (empty) so downstream imports don't break.

    Circuit 鈫?depot mapping (derived from Jan鈥-eb 2026 telematics analysis):
      Duxford - Rigid/Artic  鈫?CB22  (home depot)
      Bedford - Rigid/Artic  鈫?BEDFORD (home depot)
      St Ives - Rigid/Artic  鈫?CB22  (physically based at Duxford CB22 4PS;
                                       St Ives PE27 yard is a satellite parking
                                       lot, not a separate hub -no trunk leg)
      Stoke                  鈫?CB22  (Duxford-based Midlands corridor tractors;
                                       overnight at CB22 4PS, run NN/LE/DE/ST
                                       postcode deliveries on weekly rotations)
      *Recently Released      鈫?CB22  (confirmed Duxford-based; excludes BU20VHY
                                       which has zero telematics)
      *Subscription Expired   鈫?excluded (no telematics in 2026 data window)
      Bedford - Service       鈫?BEDFORD rigid (service vans; Service Van asset type)
    """
    depots: dict[str, tuple[set, set]] = {
        'CB22':     (set(), set()),
        'BEDFORD':  (set(), set()),
        'ST_IVES':  (set(), set()),   # kept empty; vehicles now pooled into CB22
        'STOKE':    (set(), set()),   # Stoke-on-Trent satellite (Midlands corridor)
    }
    if _MASTER_FLEET_ROWS is not None:
        for row in _MASTER_FLEET_ROWS:
            d, k = row['depot'].strip(), row['fleet_kind'].strip()
            reg = _canonical_reg(row)
            if d in depots and k == 'rigid':
                depots[d][0].add(reg)
            elif d in depots and k == 'tractor':
                depots[d][1].add(reg)
        return depots
    if not _VEHICLE_LIST_CSV.exists():
        return {'CB22': (set(_FALLBACK_RIGIDS), set(_FALLBACK_TRACTORS)),
                'BEDFORD': (set(), set()), 'ST_IVES': (set(), set()),
                'STOKE': (set(), set())}

    circuit_to_depot = {
        'Duxford - Rigid':   ('CB22',    'rigid'),
        'Duxford - Artic':   ('CB22',    'tractor'),
        'Bedford - Rigid':   ('BEDFORD', 'rigid'),
        'Bedford - Artic':   ('BEDFORD', 'tractor'),
        # St Ives vehicles overnight at Duxford (CB22 4PS); no independent trunk.
        'St Ives - Rigid':   ('CB22',    'rigid'),
        'St Ives - Artic':   ('CB22',    'tractor'),
        # Stoke tractors are based at the Stoke-on-Trent ST4 yard (telematics), feed
        # the B37 Palletline hub directly, and also run the CB22 Midlands corridor.
        'Stoke':             ('STOKE',   'tractor'),
        # Bedford service vans -small rigids used for local service calls.
        'Bedford - Service': ('BEDFORD', 'rigid'),
    }

    with open(_VEHICLE_LIST_CSV, encoding='utf-8') as _f:
        for row in _csv.DictReader(_f):
            name    = row.get('AssetName', '').strip()
            circuit = row.get('CircuitName', '').strip()
            atype   = row.get('AssetType', '').strip()
            if not name or name in _EXCLUDED_VEHICLES:
                continue
            if 'Subscription Expired' in circuit:
                continue
            if 'Recently Released' in circuit:
                _rr_depot = 'STOKE' if name in _STOKE_RECENTLY_RELEASED else 'CB22'
                if atype in _RIGID_ASSET_TYPES:
                    depots[_rr_depot][0].add(name)
                elif atype in _TRACTOR_ASSET_TYPES:
                    depots[_rr_depot][1].add(name)
                continue
            entry = circuit_to_depot.get(circuit)
            if entry is None:
                continue
            depot_id, pool = entry
            if pool == 'rigid' and atype in _RIGID_ASSET_TYPES:
                depots[depot_id][0].add(name)
            elif pool == 'tractor' and atype in _TRACTOR_ASSET_TYPES:
                depots[depot_id][1].add(name)

    return depots


_ALL_DEPOT_FLEETS = _load_all_depot_fleets()

CB22_RIGIDS    = frozenset(_ALL_DEPOT_FLEETS['CB22'][0])
CB22_TRACTORS  = frozenset(_ALL_DEPOT_FLEETS['CB22'][1])
BEDFORD_RIGIDS    = frozenset(_ALL_DEPOT_FLEETS['BEDFORD'][0])
BEDFORD_TRACTORS  = frozenset(_ALL_DEPOT_FLEETS['BEDFORD'][1])
ST_IVES_RIGIDS    = frozenset(_ALL_DEPOT_FLEETS['ST_IVES'][0])
ST_IVES_TRACTORS  = frozenset(_ALL_DEPOT_FLEETS['ST_IVES'][1])
STOKE_RIGIDS      = frozenset(_ALL_DEPOT_FLEETS['STOKE'][0])
STOKE_TRACTORS    = frozenset(_ALL_DEPOT_FLEETS['STOKE'][1])

ALL_RIGIDS   = CB22_RIGIDS   | BEDFORD_RIGIDS   | ST_IVES_RIGIDS   | STOKE_RIGIDS
ALL_TRACTORS = CB22_TRACTORS | BEDFORD_TRACTORS | ST_IVES_TRACTORS | STOKE_TRACTORS

# vehicle reg 鈫?depot_id lookup (used by scope and state)
VEHICLE_DEPOT_MAP: dict[str, str] = {}
for _depot_id, (_r, _t) in _ALL_DEPOT_FLEETS.items():
    for _reg in _r | _t:
        VEHICLE_DEPOT_MAP[_reg] = _depot_id
del _depot_id, _r, _t, _reg

# Geographic scope for PL_IMPORT delivery destinations -our Palletline franchise area.
# Derived from Qargo data (Jan鈥-eb 2026): postcode districts where 鈮?0 PL_IMPORT orders
# exist in the data and 鈮?0% are delivered by our CB22 fleet.
#   SG: 956/2841 (34%)  AL: 797/922 (86%)  CB: 850/881 (96%)  CM: 554/560 (99%)
# PE/LU/SS/NW/IP/RH removed -effectively zero or negligible CB22 share in the data.
CAMBRIDGE_SERVICE_PREFIXES = {
    'CB',   # 96% ours -core Cambridge/South Cambs territory
    'CM',   # 99% ours -Stansted/Bishop's Stortford/Chelmsford corridor
    'AL',   # 86% ours -Welwyn Garden City / Hatfield / St Albans
    'SG',   # 34% ours -shared with Bedford depot; still our largest volume area
}

# 鈹€鈹€ Territory map -derived from Jan鈥-eb 2026 PL_IMPORT analysis 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# Bedford territory: MK (100%), LU/Luton (100%), NN/Northampton (~40 km),
#   northern SG (SG1-SG7, SG15-SG19), PE1-PE9 (Peterborough, ~60 km), PE19 (St Neots)
# CB22 territory:   CB (100%), CM (100%), AL (100%),
#   southern SG (SG8-SG14), Huntingdon PE (PE26-PE29)
# Everything else 鈫?OVERFLOW (proximity-assigned by _pre_assign_overflow)
# NOTE: LTN was removed -that's an airport code, not a postcode prefix; LU is correct.
BEDFORD_TERRITORY_PREFIXES: frozenset[str] = frozenset({'MK', 'LU', 'NN'})
CB22_TERRITORY_PREFIXES:    frozenset[str] = frozenset({'CB', 'CM', 'AL'})
# Stoke satellite territory -core Staffordshire/Cheshire around the ST4 base.
# Conservative on purpose: ST/TF/CW are unambiguously Stoke-local (~240 km from the
# SE depots). The wider Midlands corridor (DE/LE/WS) the Stoke tractors also run is
# left to proximity overflow so it doesn't steal genuine CB22-corridor work.
STOKE_TERRITORY_PREFIXES:   frozenset[str] = frozenset({'ST', 'TF', 'CW'})

BEDFORD_SG_DISTRICTS: frozenset[str] = frozenset({
    'SG1','SG2','SG3','SG4','SG5','SG6','SG7',
    'SG15','SG16','SG17','SG18','SG19',
})
CB22_SG_DISTRICTS: frozenset[str] = frozenset({
    'SG8','SG9','SG10','SG11','SG12','SG13','SG14',
})
# PE19 = St Neots (Bedford); PE1-PE9 = Peterborough city/suburbs (~60 km from Bedford);
# PE26-PE29 = Huntingdon/Ely corridor (CB22).
BEDFORD_PE_DISTRICTS: frozenset[str] = frozenset({
    'PE19',
    'PE1','PE2','PE3','PE4','PE5','PE6','PE7','PE8','PE9',
})
CB22_PE_DISTRICTS:    frozenset[str] = frozenset({'PE26','PE27','PE28','PE29'})

# Maximum origin distance from CB22 for a FULL_FLEET order to be in scope.
# Cambridge audit: ARDEX@CB9 (~30 km), Stoke@ST4 (~240 km, excluded), Royston SG8 ~20 km,
# Welwyn AL7 ~30 km. 100 km threshold includes all genuine Cambridge origins while excluding
# cross-region planning artifacts. See investigations/verify_direction_and_pickups.py.
CATCHMENT_RADIUS_KM = 100.0

# Haversine distance beyond which a tractor delivery qualifies for an overnight
# stay rather than a same-day return. At 200 km one-way the round trip is ~400 km
# of driving (~8 h at 50 km/h average), leaving little margin for service time
# within a 13-h duty window.
OVERNIGHT_STAY_MIN_KM: float = 200.0

# Palletline national hub (B37 7HB, Chelmsley Wood, Birmingham).
# CB22 tractors trunk here nightly; inbound PL_IMPORT freight is sorted here.
# Coordinates geocoded via postcodes.io (prior value 52.467,-1.787 was ~5 km off).
PALLETLINE_HUB_COORDS: tuple[float, float] = (52.4602, -1.7329)

# Hazchem Network hub (LE10 3BS, Jacknell Road, Dodwells Bridge Ind Est, Hinckley).
# Operates identically to Palletline but for hazardous shipments.
# X8GNW / X88GNW trunk here; Hazchem delivers onward to final consignees.
# Coordinates geocoded via postcodes.io (prior value 52.537,-1.376 was ~3 km off).
HAZCHEM_HUB_COORDS: tuple[float, float] = (52.5406, -1.4134)

# Nightly trunk schedule parameters.
# Trunk runs are fully overnight: tractors depart after daily ops finish (~21:00)
# and return before daily ops start (~05:00 from hub).  Tractors are therefore
# fully available for the entire daytime delivery/collection shift.
TRUNK_DEPART_HOUR: int = 21         # trunk departs at ~21:00 after daily ops finish
TRUNK_HUB_DWELL_MIN: int = 90       # sort/load time at Palletline hub (minutes)
TRUNK_PREP_MARGIN_H: float = 1.0    # tractors must be back at depot this many hours
                                     # before trunk departs (i.e. by 20:00)

# Maximum origin distance from CB22 for a tractor-assigned PL_EXPORT order.
# Rigid catchment is 100 km; tractors can reach Stoke ST4 (~190 km direct),
# Coventry, Leicester, and similar long-haul shipper locations.
TRACTOR_CATCHMENT_RADIUS_KM: float = 300.0

# Operating day boundaries.
OPERATING_DAY_START = time(6, 0)
OPERATING_DAY_END = time(18, 0)
# Two-layer operating window (2026-07-21): the telematics movement curve shows
# the fleet ROLLING from 06:00 (OPERATING_DAY_START = vehicle/depot layer) while
# the first-delivery wave starts 08:00-10:00 — customers RECEIVE from ~08:00.
# Default/expanded CUSTOMER service windows open here; stated earlier slots
# are preserved verbatim.
CUSTOMER_DAY_START = time(8, 0)

# Type-level forward-planning shift windows -derived from January 2026 telematics
# across all Duxford Rigid and Artic vehicles (317 and 381 vehicle-days respectively).
#   Rigid:   first-move median 06:59  鈫?plan start 07:00
#            last-move  median 16:45, p75 17:50 鈫?plan end 18:00
#   Tractor: first-move median 06:14 (p10=01:01 from overnight hub returns, excluded)
#            鈫?plan start 07:00 for daytime delivery/collection
#            Trunk departs 21:00; tractors must finish daytime work by 20:00 (1h prep)
#            鈫?shift ends 20:00 (TRUNK_DEPART_HOUR - TRUNK_PREP_MARGIN_H)
# Used by vehicle_shift_for_event() in forward mode instead of per-vehicle licence-plate profiles.
RIGID_TYPE_SHIFT_START   = time(7,  0)
RIGID_TYPE_SHIFT_END     = time(18, 0)
TRACTOR_TYPE_SHIFT_START = time(7,  0)
TRACTOR_TYPE_SHIFT_END   = time(20, 0)  # full day available; trunk departs at 21:00

# Vehicle re-availability gate.
MIN_VIABLE_TRIP_HOURS = 1.5

# Cross-dock buffer between trunk arrival and freight ready for delivery.
CROSS_DOCK_BUFFER_MIN = 30

# Route-cost constants (mirror vrptw_engine defaults; overridable here).
# v1.9: activation cost calibrated via sweep at 拢0/拢150/拢500/拢2000/拢10000 on
# Jan 7 OSRM with 120s solver budget. 拢500 hits the sweet spot -solver opens
# 11 vehicles (matching actual) with 0 lateness. Aligns with real UK HGV
# driver-day cost (拢200-400 driver basic + ~拢100 fixed amortised). See
# investigations/sweep_activation_cost.py and v18 update doc.
VEHICLE_ACTIVATION_COST = 0.0   # retired v2.2 -idle standing cost replaces activation
ROAD_DISTANCE_FACTOR = 1.3  # Empirical UK urban/suburban road-vs-haversine multiplier, mirroring simulation/vrptw_engine.py:39. Calibrated for East Anglia operating area.
AVG_SPEED_KMH = 50.0

# Multi-day VRPTW (deep) -- CL-003. Per-day driving cap (proxy for EC 561/2006;
# Goel 2009 for the full tacho model). The objective is a TRUE lexicographic tuple
# compare (unserved, leg_days_late, tractors, km) -- no magnitude weights to tune.
MAX_DRIVING_H_PER_DAY: float = 10.0
MAX_TOUR_DAYS_HARD: int = 4    # generous physical cap so multi-day routes can't run forever
MAX_LATENESS_DAYS: int = 3     # served beyond this -> counted UNSERVED (no phantom delivery)
# Effective straight-line (haversine) speed for multi-day LONG-HAUL legs when OSRM
# is unavailable. The local AVG_SPEED_KMH (50) x ROAD_DISTANCE_FACTOR overestimates
# motorway trunking badly (Scotland read ~13h one-way vs the real ~6h), forcing
# spurious overnights / lateness / unrouted far legs. ~80 km/h on the straight line
# folds in motorway speed + mild detour and matches observed long-run times. OSRM,
# when enabled, supersedes this. (Lit: Erdogan 2017 on real road distances.)
MULTIDAY_AVG_SPEED_KMH: float = 80.0
SERVICE_MINUTES_PER_STOP = 20.0
UNASSIGNED_PENALTY = 50_000.0

# Planner tuning knobs live in freight_planner/config.py

# Per-origin collection profiles learned from telematics
# (investigations/verify_collection_patterns.py).
COLLECTION_PROFILES = {
    'CB9': {'depart_hour': 10, 'dwell_min': 62,  'trip_hours': 3.2},
    'SG8': {'depart_hour':  8, 'dwell_min': 282, 'trip_hours': 5.7},
    'AL7': {'depart_hour':  7, 'dwell_min': 289, 'trip_hours': 9.5},
    'SG6': {'depart_hour':  9, 'dwell_min': 66,  'trip_hours': 6.8},
}
# Fallback for origins without a learned profile (new shippers not in telematics yet).
# Conservative defaults: 08:00 morning depart, 60 min dwell, 4h round trip.
DEFAULT_COLLECTION_PROFILE = {'depart_hour': 8, 'dwell_min': 60, 'trip_hours': 4.0}

# Delivery time-window width from service level.
# Used only when destination_time_window_value is absent AND the requested-start
# timestamp is non-midnight (Case B in the 3-case window logic in scope.py).
# For most orders, the window is fully determined by the Qargo timestamp data
# and these values are not consulted.
SERVICE_LEVEL_WINDOW_HOURS = {
    'Next day':               12,   # deliver on destination_date; 06:00-8:00
    'Economy':                12,   # same-day window; date-only orders
    '72 Hours':               72,   # deliver within 3 days of collection
    '5 Day Service':         120,   # deliver within 5 days of collection
    'Saturday Delivery':      12,   # any time on the Saturday
    'Export To Europe':       12,   # out-of-scope; treated as same-day for planning
}
DEFAULT_WINDOW_HOURS = 12  # same as Next day; used for unmapped service levels

# Default freight availability times for backtest fallback.
DEFAULT_PRE_STAGED_HOUR = 6
DEFAULT_VIA_DEPOT_HOUR = 12

# Level 0 + Level 1 pass thresholds.
PASS_THRESHOLDS = {
    'km_pct':           0.20,     # 卤20% on total delivery km
    'vehicles_count':   2,        # 卤2 vehicles
    'fuel_pct':         0.25,     # 卤25% on fuel cost
    'assignment_pp':    0.10,     # 卤10 percentage points
    'on_time_pp':       0.10,
    'ks_max':           0.30,     # KS distance ceiling for distributional metrics
    'jaccard_min':      0.80,     # Postcode-district set overlap floor
    'time_minutes':     60,       # 卤60 min for median depart/return time
}

# v1.5: per-vehicle profile loaded from the telematics-derived JSON.
# Source: investigations/derive_v15_parameters.py.
_PROFILES_JSON = (_LOGISTICS_ROOT
                  / 'data' / 'Output' / 'cambridge'
                  / 'vehicle_profiles_derived.json')


# Fallback profiles for fleet vehicles without a telematics-derived entry.
# Used for any Duxford - Rigid or Duxford - Artic vehicle absent from
# vehicle_profiles_derived.json until derivation runs for that vehicle.
_LORRY_FALLBACK_PROFILE: dict = {
    'asset_type':                'Lorry',
    'capacity_kg_per_trip':      10_000,
    'capacity_pallets_per_trip': 15,
    'shift_start':               time(7, 0),
    'shift_end':                 time(17, 0),
    'median_trips_per_day':      1,
    'multi_trip_share':          0.40,
}
_MINI_TRUCK_FALLBACK_PROFILE: dict = {
    'asset_type':                'Mini Truck',
    'capacity_kg_per_trip':      2_500,
    'capacity_pallets_per_trip': 8,
    'shift_start':               time(7, 30),
    'shift_end':                 time(16, 30),
    'median_trips_per_day':      2,
    'multi_trip_share':          0.60,
}
_TRACTOR_FALLBACK_PROFILE: dict = {
    'asset_type':                'Tractor Unit',
    'capacity_kg_per_trip':      24_000,
    'capacity_pallets_per_trip': 30,
    'shift_start':               time(6, 0),
    'shift_end':                 time(18, 0),
    'median_trips_per_day':      1,
    'multi_trip_share':          0.30,
}

# Map asset type string 鈫?fallback profile for vehicles missing from derived JSON.
_ASSET_TYPE_FALLBACKS: dict = {
    'Lorry':        _LORRY_FALLBACK_PROFILE,
    'Rigid Truck':  _LORRY_FALLBACK_PROFILE,
    'Mini Truck':   _MINI_TRUCK_FALLBACK_PROFILE,
    'Tractor Unit': _TRACTOR_FALLBACK_PROFILE,
}


def _safe_float(value) -> float | None:
    try:
        if value in (None, ''):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_vehicle_master_rows() -> dict[str, dict]:
    """Load Supatrak master vehicle rows keyed by registration."""
    if not _VEHICLE_LIST_CSV.exists():
        return {}
    rows: dict[str, dict] = {}
    with open(_VEHICLE_LIST_CSV, encoding='utf-8') as _f:
        for row in _csv.DictReader(_f):
            reg = row.get('AssetName', '').strip()
            if not reg:
                continue
            rows[reg] = row
    return rows


_VEHICLE_MASTER_ROWS = _load_vehicle_master_rows()


def _asset_type_from_csv(reg: str) -> str:
    """Look up AssetType for a registration from the vehicle list CSV."""
    row = _VEHICLE_MASTER_ROWS.get(reg)
    if row is None:
        return 'Lorry'
    return row.get('AssetType', 'Lorry').strip() or 'Lorry'


def _master_weight_metadata(reg: str) -> tuple[float | None, float | None]:
    """Return Supatrak gross max/typical tonnes as metadata only."""
    row = _VEHICLE_MASTER_ROWS.get(reg, {})
    return _safe_float(row.get('max_tonnes')), _safe_float(row.get('typical_tonnes'))


def _capacity_profile_fields(reg: str, observed: dict | None, fallback: dict) -> dict:
    """Build payload capacity fields with explicit provenance metadata."""
    master_max_tonnes, master_typical_tonnes = _master_weight_metadata(reg)
    observed_kg_p95 = None
    observed_kg_max = None
    observed_pallets_p95 = None
    observed_pallets_max = None
    if observed:
        observed_kg_p95 = observed.get('derived_capacity_kg_p95')
        observed_kg_max = observed.get('derived_capacity_kg_max')
        observed_pallets_p95 = observed.get('derived_capacity_pallets_p95')
        observed_pallets_max = observed.get('derived_capacity_pallets_max')

    if observed_kg_p95 is not None:
        capacity_kg = int(observed_kg_p95)
        kg_source = 'observed_p95'
    else:
        capacity_kg = fallback['capacity_kg_per_trip']
        kg_source = 'asset_type_default'

    if observed_pallets_p95 is not None:
        capacity_pallets = int(observed_pallets_p95)
        pallets_source = 'observed_p95'
    else:
        capacity_pallets = fallback['capacity_pallets_per_trip']
        pallets_source = 'asset_type_default'

    return {
        'capacity_kg_per_trip': capacity_kg,
        'capacity_kg_source': kg_source,
        'capacity_pallets_per_trip': capacity_pallets,
        'capacity_pallets_source': pallets_source,
        'master_max_tonnes': master_max_tonnes,
        'master_typical_tonnes': master_typical_tonnes,
        'observed_capacity_kg_p95': observed_kg_p95,
        'observed_capacity_kg_max': observed_kg_max,
        'observed_capacity_pallets_p95': observed_pallets_p95,
        'observed_capacity_pallets_max': observed_pallets_max,
    }


def _build_vehicle_profiles() -> dict:
    """Load the derived JSON and reshape into the v1.5 profile dict.

    Each profile carries per-trip capacity, median shift times, and the
    multi-trip share for the dispatcher to decide single- vs two-event flow.
    Any Duxford fleet vehicle missing from the derived JSON receives the
    appropriate fallback profile based on its AssetType from the vehicle list.
    """
    out: dict = {}
    if _MASTER_FLEET_ROWS is not None:
        for row in _MASTER_FLEET_ROWS:
            reg = _canonical_reg(row)
            # Shift columns were REMOVED from the master 2026-07-16 (user rule:
            # telematics medians are not operating constraints). Keep the profile
            # keys for dict-shape compatibility with static operating-day values;
            # the freight planner ignores them (vehicles.py sets its own window).
            def _hhmm_time(col: str, default: time) -> time:
                v = str(row.get(col) or '')
                if not v:
                    return default
                hh, mm = v.split(':')[:2]
                return time(int(hh), int(mm))
            start = _hhmm_time('shift_start', time(6, 0))
            end = _hhmm_time('shift_end', time(19, 0))
            # Capacity truth = the physical payload/pallet columns (2026-07-16);
            # the retired per-trip profile columns are only a fallback while
            # not-yet-stripped files still carry them.
            cap_kg = row.get('payload_kg') or row.get('capacity_kg_per_trip') or 0
            cap_pal = row.get('pallet_capacity') or row.get('capacity_pallets_per_trip') or 0
            out[reg] = {
                'asset_type':                row['profile_asset_type'],
                'capacity_kg_per_trip':      int(float(cap_kg)),
                'capacity_kg_source':        'vehicle_master',
                'capacity_pallets_per_trip': int(float(cap_pal)),
                'capacity_pallets_source':   'vehicle_master',
                'master_max_tonnes':         _safe_float(row['master_max_tonnes']),
                'master_typical_tonnes':     _safe_float(row['master_typical_tonnes']),
                'shift_start':               start,
                'shift_end':                 end,
                'median_trips_per_day':      int(float(row.get('median_trips_per_day') or 1)),
                'multi_trip_share':          float(row.get('multi_trip_day_pct') or 0.0) / 100.0,
            }
        return out
    if _PROFILES_JSON.exists():
        raw = _json.loads(_PROFILES_JSON.read_text())
        for veh, p in raw.items():
            hh, mm = p['shift_start_median'].split(':')
            start = time(int(hh), int(mm))
            hh, mm = p['shift_end_median'].split(':')
            end = time(int(hh), int(mm))
            fallback = _ASSET_TYPE_FALLBACKS.get(p['asset_type'], _LORRY_FALLBACK_PROFILE)
            capacity_fields = _capacity_profile_fields(veh, p, fallback)
            out[veh] = {
                'asset_type':                p['asset_type'],
                **capacity_fields,
                'shift_start':               start,
                'shift_end':                 end,
                'median_trips_per_day':      int(p['median_trips_per_day']),
                'multi_trip_share':          float(p['multi_trip_day_pct']) / 100.0,
            }
    # Fill in any fleet vehicle that has no derived profile yet.
    for reg in ALL_RIGIDS | ALL_TRACTORS:
        if reg not in out:
            asset_type = _asset_type_from_csv(reg)
            fallback = _ASSET_TYPE_FALLBACKS.get(asset_type, _LORRY_FALLBACK_PROFILE)
            profile = fallback.copy()
            profile['asset_type'] = asset_type
            profile.update(_capacity_profile_fields(reg, None, fallback))
            out[reg] = profile
    return out


VEHICLE_PROFILES = _build_vehicle_profiles()

# v1.5 multi-trip constants (from trip_profile_derived.json + data study).
MULTI_TRIP_THRESHOLD     = 0.40   # share of days with 鈮? depot returns
DEPOT_DWELL_MIN          = 42     # median inter-trip dwell across 242 obs
EVENT_B_DEFAULT_HOUR     = 12     # forward-mode default mid-day reload hour
DEFAULT_TRIP_DURATION_H  = 4.1    # median trip duration across 601 obs

# v1.6: allow shifts to overrun past median end by this many hours.
# Telematics shows shifts up to 20.8h (p90 = 13.1h); 4h overrun lets the
# solver fit more deliveries without dropping orders.
SHIFT_OVERRUN_HOURS = 4

# v1.6: minimum remaining shift budget needed for another event.
# Below this, the vehicle is considered "done for the day."
MIN_VIABLE_TRIP_HOURS_V16 = 3.0

# v2.3: legal-max on-duty span used as a multi-trip rigid's daily hour BUDGET in
# forward mode. Previously the budget was each plate's telematics-median span
# (~9h, several at 7-8h), so a vehicle that historically ran a short day quit
# after one trip and staged PL_IMPORT freight overflowed on peak days -even
# though the same driver could legally work much longer. 13.0h matches the p90
# of observed shift spans (13.1h) and a UK HGV on-duty spread (鈮?h driving +
# breaks/loading). The routing window (07:00 + SHIFT_OVERRUN_HOURS 鈫?22:00)
# already accommodates it; only the per-trip-gating budget needed raising.
RIGID_LEGAL_MAX_SHIFT_HOURS = 13.0

# v2.4: legal daily DRIVING limit (not on-duty span). Used to cap how far a
# vehicle may reposition/return in one day when consuming idle hours. UK HGV
# rule: 9h driving/day (extendable to 10h twice weekly); 9.0 is the safe base.
LEGAL_DAILY_DRIVE_HOURS = 9.0

# --- OSRM toggle (v1.7) ---
# When CAMBRIDGE_OSRM=1 (or 'true' / 'yes', case-insensitive), run_day_multi_trip
# installs an OSRMRouter against OSRM_URL before the solver runs. Default is the
# Haversine router (pre-v1.7 behaviour).
import os as _os

OSRM_URL: str = _os.environ.get('OSRM_URL', 'http://localhost:5000')


def osrm_enabled() -> bool:
    """True iff CAMBRIDGE_OSRM env var is set to '1' / 'true' / 'yes' (case-insensitive)."""
    val = _os.environ.get('CAMBRIDGE_OSRM', '').strip().lower()
    return val in ('1', 'true', 'yes')


def multiday_vrptw_enabled() -> bool:
    """True unless MULTIDAY_VRPTW env var explicitly disables it. CL-003/CL-004.

    Default is now ON (the multi-day VRPTW over delivery + collection legs). Set
    MULTIDAY_VRPTW=0/false/off/no to force the legacy build_tours+tour_router path
    (and CL-002 off) for a baseline/revert -- so the flip stays controlled."""
    val = _os.environ.get('MULTIDAY_VRPTW', '').strip().lower()
    if val in ('0', 'false', 'off', 'no'):
        return False
    return True


# Multi-day VRPTW objective weights (CL-004 A-ii). The solver objective is
# (unserved, weighted_cost): serving stays the HARD top priority and
# max_lateness_days stays a hard feasibility bound, but tractors / lateness / km
# are traded off by a weighted sum instead of a strict lexicographic order -- so a
# small lateness can no longer justify deploying a whole extra truck. Rates:
#   - a tractor (200) is worth avoiding > MULTIDAY_COST_PER_TRACTOR/PER_LATE_DAY
#     = ~0.67 day of lateness, or ~200 road-km of extra driving.
MULTIDAY_COST_PER_TRACTOR = 200.0
MULTIDAY_COST_PER_LATE_DAY = 300.0
MULTIDAY_COST_PER_KM = 1.0


# Fixed working time per distinct customer visit. Rounded Jan-Feb 2026 observed
# means are approximately 15 minutes for rigids and 30 minutes for tractors.
# The verified van sample is insufficient, so vans use the rigid allowance.
# Pallet count explained only about 4-6% of visit-duration variation.
CUSTOMER_SERVICE_MIN_BY_TYPE: dict[str, float] = {
    "van": 15.0,
    "rigid": 15.0,
    "tractor": 30.0,
}


def customer_service_minutes(vehicle_type: str = "tractor") -> float:
    """Return fixed working minutes for one distinct customer visit."""
    vt = str(vehicle_type or "tractor").strip().lower()
    return CUSTOMER_SERVICE_MIN_BY_TYPE.get(
        vt, CUSTOMER_SERVICE_MIN_BY_TYPE["tractor"]
    )


def service_minutes_for_load(pallets: float) -> float:
    """Compatibility alias for legacy tractor reach screens; load is ignored."""
    return customer_service_minutes("tractor")


def rigid_service_minutes_for_load(pallets: float) -> float:
    """Compatibility alias for legacy rigid callers; load is ignored."""
    return customer_service_minutes("rigid")


# v1.8: solver objective penalty for arriving past a stop's window_end (GBP/min).
LATENESS_PENALTY_GBP_PER_MIN: float = 1.0


# v1.8: Qargo statuses that indicate the order was actually delivered (not
# planned / in-transit / cancelled). Used to filter actual on-time computations.
DELIVERED_STATUSES = frozenset({
    'INVOICE_POSTED', 'DONT_INVOICE', 'INVOICED', 'INVOICE_READY', 'COMPLETED',
})

# Nightly trunk dwell times at hub destinations (telematics-derived, Jan 2026).
TRUNK_B37_DWELL_MIN: int = 380   # B37 7HB Palletline -kept for reference; superseded by HUB_DEPART
TRUNK_LE10_DWELL_MIN: int = 330   # LE10 3BS Hazchem   -kept for reference; superseded by HUB_DEPART

# Fixed hub-departure times -derived from Jan 2026 telematics (112 B37, 63 LE10 overnight departures).
# The Palletline/Hazchem hubs run a fixed overnight sort schedule. Tractors always leave around
# the same clock time regardless of when they arrived (sort is not triggered by our arrival).
#   B37:  median 02:40, peak 02:xx 鈫?plan 03:00 (conservative, allows slight variability)
#   LE10: median 02:01, peak 01:xx-02:xx 鈫?plan 02:00
# expected_return = datetime.combine(next_morning, HUB_DEPART) + drive_back_h
TRUNK_B37_HUB_DEPART  = time(3, 0)   # 03:00 next morning
TRUNK_LE10_HUB_DEPART = time(2, 0)   # 02:00 next morning
TRUNK_LOADING_BUFFER_MIN: int = 30    # depot staging buffer before trunk departs

# Bedford trunk schedule -overnight, same pattern as CB22.
# Telematics showed 19:xx departures (old model); corrected to align with
# overnight-only trunk policy (daily ops finish, then trunk departs ~21:00).
# Hub return: same B37 sort schedule as CB22 (03:00 fixed departure).
BEDFORD_TRUNK_DEPART_HOUR:      int  = 21
BEDFORD_TRUNK_B37_HUB_DEPART        = time(3, 0)

STATE_DIR = (_LOGISTICS_ROOT
             / 'data' / 'Output' / 'cambridge' / 'state')


