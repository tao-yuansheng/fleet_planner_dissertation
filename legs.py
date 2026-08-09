from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil
from datetime import date, datetime, timedelta

import pandas as pd

from freight_planner.shared.scope import (
    _collection_window,
    _delivery_window_policy,
    _pl_export_window,
    combined_staging_depot,
    resolve_staging_depot,
)

from freight_planner import geocode
from freight_planner import config as _fp_config
from freight_planner.demand import DemandRecord, first_date, is_empty
from freight_planner.route_costs import haversine_km
from freight_planner.shared.config import DEPOT_ANCHORS
from freight_planner.vehicles import fleet_capacity_ceiling

# Largest single-vehicle capacity in the fleet, from the validated vehicle master.
# An order above this fits no one vehicle (MASSIVE_UNSUPPORTED) and is split into
# chunks of at most this size. Sourced from the master so leg generation tracks the
# real fleet (44 t artics = 28 t / 26 pallets) rather than a stale hardcoded value.
MAX_VEHICLE_PALLETS, MAX_VEHICLE_KG = fleet_capacity_ceiling()

B37_HUB = "B37_HUB"
LE10_HUB = "LE10_HUB"
CUSTOMER = "CUSTOMER"
DEPOT = "DEPOT"
HUB = "HUB"

CUSTOMER_PICKUP = "CUSTOMER_PICKUP"
CUSTOMER_DELIVERY = "CUSTOMER_DELIVERY"
DIRECT_CUSTOMER_MOVE = "DIRECT_CUSTOMER_MOVE"
HUB_DROP = "HUB_DROP"
OUTBOUND_TRUNK = "OUTBOUND_TRUNK"
INBOUND_TRUNK = "INBOUND_TRUNK"
ACCOUNTING_ONLY = "ACCOUNTING_ONLY"

# Geocodable postcodes for the two network hubs, so a hub-drop has a real stop.
HUB_POSTCODE = {B37_HUB: "B37 7HB", LE10_HUB: "LE10 3BS"}

# Minutes from a same-day collection deadline to the freight being staged at the
# depot and ready to re-deliver (collection -> depot drive + dock handling). A
# same-day crossdock delivery cannot start before this (Milestone 8b).
SAME_DAY_XDOCK_HANDOFF_MIN = 90


def _collocated_with_depot(o_lat, o_lon, depot: str) -> bool:
    """True when a DIRECT's collection origin sits on its source depot's own estate
    (ST4 8JB vs the STOKE anchor): collecting it is a dock move the delivering
    vehicle makes at departure, so the leg is functionally a depot-loaded delivery.
    Daily analogue of TOUR_DEPOT_DIRECT_AS_DELIVERY, with a deliberately tighter
    radius — on a daily trip an unpriced approach is real km, not tour noise."""
    if not _fp_config.DAILY_DEPOT_DIRECT_AS_DELIVERY or o_lat is None or o_lon is None:
        return False
    anchor = DEPOT_ANCHORS.get(str(depot or ""))
    if anchor is None:
        return False
    return (haversine_km(float(o_lat), float(o_lon), anchor[0], anchor[1])
            <= float(_fp_config.DAILY_ORIGIN_AT_DEPOT_RADIUS_KM))


def _collocated_ready(pes: str) -> str:
    """Departure floor for a collocated depot-delivery: collection window OPEN plus
    the dock staging allowance. Window-open anchored (non-anticipative) — NOT the
    deadline+90 staging pessimism, which is exactly what made same-day XDOCK
    window-infeasible for these orders and forced them into atomic DIRECT arcs."""
    try:
        t = datetime.fromisoformat(str(pes))
    except (TypeError, ValueError):
        return ""
    return (t + timedelta(minutes=float(_fp_config.COLLOCATED_STAGING_MIN))).isoformat(sep=" ")


def _pinned(depot: str) -> str:
    """depot_bound under DEPOT_PINNING (2026-07-17): a pickup may only be carried by
    vehicles homed at the depot its freight must LAND at, a delivery by vehicles
    homed where its freight RESTS — inter-depot movement rides priced trunks only.
    Without this 130/618 delivery legs rode vehicles that never visit the freight's
    depot (the cross-depot teleport, worst-case 12.4% unpriced km)."""
    return str(depot or "") if _fp_config.DEPOT_PINNING else ""


def _readiness_floor(service_date: str) -> str:
    """A2 readiness lag (2026-07-18): the earliest an import delivery can DEPART the
    depot when its freight lands by day-trunk = day-start(06:00) + READINESS_LAG_MIN.
    '' when the knob is 0 (nominal night-trunk world) or no date. Stamped ONLY on
    PL_IMPORT delivery legs, so the fleet does other work in the morning and imports
    ride later trips — a freight-availability gate, not a shorter operating day."""
    m = float(_fp_config.READINESS_LAG_MIN)
    if m <= 0.0 or not service_date:
        return ""
    try:
        d = date.fromisoformat(str(service_date)[:10])
    except (TypeError, ValueError):
        return ""
    start = datetime(d.year, d.month, d.day, int(_fp_config.FLEET_DAY_START_HOUR))
    return (start + timedelta(minutes=m)).isoformat(sep=" ")


def staged_delivery_start(delivery_start: str, pickup_end: str, buffer_min: int) -> str:
    """Earliest a same-day crossdock delivery can begin: the later of the delivery
    window opening and (collection deadline + handoff buffer). If the collection
    time is unknown, the delivery start is unchanged."""
    try:
        pe = datetime.fromisoformat(str(pickup_end))
    except ValueError:
        return delivery_start
    staged = pe + timedelta(minutes=buffer_min)
    try:
        ds = datetime.fromisoformat(str(delivery_start))
    except ValueError:
        return staged.isoformat(sep=" ")
    return delivery_start if ds >= staged else staged.isoformat(sep=" ")


def hub_for_row(row: pd.Series, flow: str) -> str:
    sub = "" if is_empty(row.get("resource_subcontractor")) else str(row.get("resource_subcontractor"))
    import_type = "" if is_empty(row.get("order_import_integration_type")) else str(row.get("order_import_integration_type"))
    value = f"{sub} {import_type}".lower()
    return LE10_HUB if "hazchem" in value else B37_HUB


def clean_pc(value) -> str:
    return geocode.resolve_fc_alias(str(value or "").strip().upper())


def geocode_ok(pc: str, cache: dict) -> bool:
    return geocode.geocode_ok(pc, cache)


def latlon(pc: str, cache: dict) -> tuple[float | None, float | None]:
    return geocode.latlon(pc, cache)


def iso_pair(window) -> tuple[str, str]:
    if not window:
        return "", ""
    return window[0].isoformat(sep=" "), window[1].isoformat(sep=" ")


def delivery_windows(row: pd.Series) -> tuple[str, str, str, str, str]:
    policy = _delivery_window_policy(row)
    raw_start, raw_end = iso_pair(policy.raw_window)
    eff_start, eff_end = iso_pair(policy.effective_window)
    return raw_start, raw_end, eff_start, eff_end, policy.hardness


def pickup_windows(row: pd.Series, flow: str) -> tuple[str, str, str, str, str]:
    window = _pl_export_window(row) if flow == "PL_EXPORT" else _collection_window(row)
    start, end = iso_pair(window)
    return start, end, start, end, "pickup_window"


def is_massive(record: DemandRecord) -> bool:
    return record.pallets > MAX_VEHICLE_PALLETS or record.weight_kg > MAX_VEHICLE_KG


@dataclass(frozen=True)
class MovementLegRecord:
    leg_id: str
    freight_id: str
    order_id: str
    order_name: str
    flow: str
    responsibility_shape: str
    leg_kind: str
    dispatchable: bool
    planner_status: str
    service_date: str
    origin_node: str
    destination_node: str
    service_pc: str
    source_depot: str
    target_depot: str
    hub: str
    ready_state: str
    result_state: str
    raw_window_start: str
    raw_window_end: str
    effective_window_start: str
    effective_window_end: str
    window_hardness: str
    freight_ready_time: str
    pallets: float
    weight_kg: float
    geocode_ok: bool
    option_set: str = ""    # mutual-exclusion key (order_id) when alternatives exist
    option_group: str = ""  # DIRECT | XDOCK | "" (non-optional)
    origin_lat: float | None = None  # DIRECT moves: the collection point
    origin_lon: float | None = None
    origin_pc: str = ""              # DIRECT/HUB-DROP moves: the collection postcode
    depart_floor: str = ""  # collocated depot-delivery: trip may not DEPART before this (freight readiness)
    depot_bound: str = ""   # collocated depot-delivery: serving vehicle must be homed at this depot

    def to_dict(self) -> dict:
        return asdict(self)


def _status_for_leg(record: DemandRecord, service_pc: str, cache: dict,
                    pallets: float | None = None, weight_kg: float | None = None) -> tuple[bool, str, bool]:
    if record.exclusion_reason:
        return False, record.exclusion_reason, geocode_ok(service_pc, cache)
    pc_ok = geocode_ok(service_pc, cache)
    if not pc_ok:
        return False, "BAD_GEOCODE", False
    p = record.pallets if pallets is None else float(pallets)
    kg = record.weight_kg if weight_kg is None else float(weight_kg)
    if p > MAX_VEHICLE_PALLETS or kg > MAX_VEHICLE_KG:
        return False, "MASSIVE_UNSUPPORTED", True
    return True, "DISPATCHABLE", True


def _split_parts(record: DemandRecord, *, hazardous: bool = False) -> list[tuple[str, str, float, float]]:
    """Return (suffix_tag, freight_id, pallets, kg) parts for a demand record.

    An over-ceiling order (no single vehicle can carry it) splits into equal
    parts of at most the fleet ceiling — the multi-vehicle dispatch a real
    operator would send. User decision 2026-07-16: splitting applies to EVERY
    flow (imports/exports/local too, via their branches), and hazardous loads
    never split (one sealed consignment)."""
    if hazardous or not is_massive(record):
        return [("", record.order_id, float(record.pallets), float(record.weight_kg))]
    pallet_parts = ceil(record.pallets / MAX_VEHICLE_PALLETS) if record.pallets > 0 else 1
    weight_parts = ceil(record.weight_kg / MAX_VEHICLE_KG) if record.weight_kg > 0 else 1
    count = max(1, pallet_parts, weight_parts)
    base_p = float(record.pallets) / count
    base_kg = float(record.weight_kg) / count
    return [
        (f"S{i}of{count}", f"{record.order_id}#S{i}", base_p, base_kg)
        for i in range(1, count + 1)
    ]


def _suffix(base: str, tag: str) -> str:
    return f"{base}_{tag}" if tag else base


def _leg(
    *,
    record: DemandRecord,
    suffix: str,
    flow: str,
    leg_kind: str,
    service_date: str,
    origin_node: str,
    destination_node: str,
    service_pc: str,
    source_depot: str,
    target_depot: str,
    hub: str = "",
    ready_state: str,
    result_state: str,
    raw_window_start: str = "",
    raw_window_end: str = "",
    effective_window_start: str = "",
    effective_window_end: str = "",
    window_hardness: str = "",
    freight_ready_time: str = "",
    cache: dict,
    customer_dispatchable: bool = True,
    option_set: str = "",
    option_group: str = "",
    origin_lat: float | None = None,
    origin_lon: float | None = None,
    origin_pc: str = "",
    freight_id: str | None = None,
    pallets: float | None = None,
    weight_kg: float | None = None,
    depart_floor: str = "",
    depot_bound: str = "",
) -> MovementLegRecord:
    load_p = record.pallets if pallets is None else float(pallets)
    load_kg = record.weight_kg if weight_kg is None else float(weight_kg)
    dispatchable, status, pc_ok = _status_for_leg(record, service_pc, cache, load_p, load_kg)
    if not customer_dispatchable:
        dispatchable = False
        if status == "DISPATCHABLE":
            status = "TRUNK_LEG"
    return MovementLegRecord(
        leg_id=f"{record.order_id}:{suffix}",
        freight_id=str(freight_id or record.order_id),
        order_id=record.order_id,
        order_name=record.order_name,
        flow=flow,
        responsibility_shape=record.responsibility_shape,
        leg_kind=leg_kind,
        dispatchable=dispatchable,
        planner_status=status,
        service_date=service_date,
        origin_node=origin_node,
        destination_node=destination_node,
        service_pc=service_pc,
        source_depot=source_depot,
        target_depot=target_depot,
        hub=hub,
        ready_state=ready_state,
        result_state=result_state,
        raw_window_start=raw_window_start,
        raw_window_end=raw_window_end,
        effective_window_start=effective_window_start,
        effective_window_end=effective_window_end,
        window_hardness=window_hardness,
        freight_ready_time=freight_ready_time,
        pallets=load_p,
        weight_kg=load_kg,
        geocode_ok=pc_ok,
        option_set=option_set,
        option_group=option_group,
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        origin_pc=origin_pc,
        depart_floor=depart_floor,
        depot_bound=depot_bound,
    )


def build_movement_leg_records(
    qargo_df: pd.DataFrame,
    demand_records: list[DemandRecord],
    postcode_cache: dict,
) -> list[MovementLegRecord]:
    rows = {str(r.get("order_id") or ""): r for _, r in qargo_df.iterrows()}
    out: list[MovementLegRecord] = []

    for record in demand_records:
        row = rows.get(record.order_id)
        if row is None:
            continue
        flow = record.corrected_flow
        origin_pc = clean_pc(record.origin_pc)
        dest_pc = clean_pc(record.destination_pc)
        o_lat, o_lon = latlon(origin_pc, postcode_cache)
        origin_depot = resolve_staging_depot(origin_pc, is_delivery_anchor=False,
                                             lat=o_lat, lon=o_lon)
        dest_depot = resolve_staging_depot(dest_pc, is_delivery_anchor=True)
        hub = hub_for_row(row, flow)

        if record.exclusion_reason:
            out.append(_leg(
                record=record, suffix="A", flow=flow, leg_kind=ACCOUNTING_ONLY,
                service_date=record.deliver_date or record.collect_date,
                origin_node="", destination_node="", service_pc=dest_pc or origin_pc,
                source_depot="", target_depot="", ready_state="", result_state=record.exclusion_reason,
                cache=postcode_cache, customer_dispatchable=False,
            ))
            continue

        if flow == "PL_IMPORT":
            rs, re, es, ee, hardness = delivery_windows(row)
            service_date = record.deliver_date
            inbound_date = ""
            d = first_date(row, "destination_requested_start_timestamp_local", "destination_timestamp_local", "destination_date")
            if d is not None:
                inbound_date = str(d - timedelta(days=1))
            # over-ceiling imports split like FULL_FLEET (user decision 2026-07-16):
            # a 34-pal delivery is real two-truck work, not MASSIVE_UNSUPPORTED
            for tag, fid, part_p, part_kg in _split_parts(record, hazardous=(hub == LE10_HUB)):
                out.append(_leg(
                    record=record, suffix=_suffix("T_IN", tag), flow=flow, leg_kind=INBOUND_TRUNK,
                    service_date=inbound_date or service_date,
                    origin_node=hub, destination_node=DEPOT, service_pc=dest_pc,
                    source_depot=dest_depot, target_depot=dest_depot, hub=hub,
                    ready_state="AT_HUB", result_state="AT_DEPOT",
                    cache=postcode_cache, customer_dispatchable=False,
                    freight_id=fid, pallets=part_p, weight_kg=part_kg,
                ))
                out.append(_leg(
                    record=record, suffix=_suffix("D", tag), flow=flow, leg_kind=CUSTOMER_DELIVERY,
                    service_date=service_date, origin_node=DEPOT, destination_node=CUSTOMER,
                    service_pc=dest_pc, source_depot=dest_depot, target_depot=dest_depot,
                    hub=hub, ready_state="AT_DEPOT", result_state="DELIVERED",
                    raw_window_start=rs, raw_window_end=re,
                    effective_window_start=es, effective_window_end=ee,
                    window_hardness=hardness, freight_ready_time=es,
                    cache=postcode_cache,
                    freight_id=fid, pallets=part_p, weight_kg=part_kg,
                    depot_bound=_pinned(dest_depot),
                    depart_floor=_readiness_floor(service_date),
                ))
            continue

        if flow == "PL_EXPORT":
            rs, re, es, ee, hardness = pickup_windows(row, flow)
            service_date = record.collect_date
            hub_pc = HUB_POSTCODE.get(hub, "")
            o_lat, o_lon = latlon(origin_pc, postcode_cache)
            # over-ceiling exports split like FULL_FLEET (user decision 2026-07-16);
            # per part the TRUNK vs HUBDROP choice stays mutually exclusive (option_set=fid)
            for tag, fid, part_p, part_kg in _split_parts(record, hazardous=(hub == LE10_HUB)):
                # TRUNK option (default): collect to depot, then scheduled depot->hub trunk.
                out.append(_leg(
                    record=record, suffix=_suffix("C", tag), flow=flow, leg_kind=CUSTOMER_PICKUP,
                    service_date=service_date, origin_node=CUSTOMER, destination_node=DEPOT,
                    service_pc=origin_pc, source_depot=origin_depot, target_depot=origin_depot,
                    hub=hub, ready_state="AT_CUSTOMER_ORIGIN", result_state="AT_DEPOT",
                    raw_window_start=rs, raw_window_end=re,
                    effective_window_start=es, effective_window_end=ee,
                    window_hardness=hardness, freight_ready_time=es,
                    cache=postcode_cache,
                    option_set=fid, option_group="TRUNK",
                    freight_id=fid, pallets=part_p, weight_kg=part_kg,
                    depot_bound=_pinned(origin_depot),
                ))
                out.append(_leg(
                    record=record, suffix=_suffix("T_OUT", tag), flow=flow, leg_kind=OUTBOUND_TRUNK,
                    service_date=service_date, origin_node=DEPOT, destination_node=hub,
                    service_pc=origin_pc, source_depot=origin_depot, target_depot=origin_depot,
                    hub=hub, ready_state="AT_DEPOT", result_state="WITH_NETWORK",
                    cache=postcode_cache, customer_dispatchable=False,
                    option_set=fid, option_group="TRUNK",
                    freight_id=fid, pallets=part_p, weight_kg=part_kg,
                ))
                # HUBDROP option (opportunistic): the collecting vehicle drops straight
                # at the hub (customer->hub), freeing trunk capacity. Two-point move.
                out.append(_leg(
                    record=record, suffix=_suffix("H", tag), flow=flow, leg_kind=HUB_DROP,
                    service_date=service_date, origin_node=CUSTOMER, destination_node=hub,
                    service_pc=hub_pc or origin_pc, source_depot=origin_depot, target_depot=origin_depot,
                    hub=hub, ready_state="AT_CUSTOMER_ORIGIN", result_state="WITH_NETWORK",
                    raw_window_start=rs, raw_window_end=re,
                    effective_window_start=es, effective_window_end=ee,
                    window_hardness=hardness, freight_ready_time=es,
                    cache=postcode_cache,
                    option_set=fid, option_group="HUBDROP",
                    origin_lat=o_lat, origin_lon=o_lon, origin_pc=origin_pc,
                    freight_id=fid, pallets=part_p, weight_kg=part_kg,
                ))
            continue

        if flow == "FULL_FLEET":
            collect_date = record.collect_date
            deliver_date = record.deliver_date
            multi_day = bool(collect_date) and bool(deliver_date) and collect_date != deliver_date
            parts = _split_parts(record, hazardous=(hub == LE10_HUB))
            if multi_day:
                # The freight moves across dates, so it must stage at a depot. An
                # ordinary vehicle cannot retain the load onboard until the later
                # delivery date. The existing tour path may still carry the XDOCK
                # delivery leg from that depot; it does not require a DIRECT option.
                prs, pre, pes, pee, ph = pickup_windows(row, flow)
                drs, dre, des, dee, dh = delivery_windows(row)
                o_lat, o_lon = latlon(origin_pc, postcode_cache)
                # 2026-07-20: stage the whole overnight trio at the depot minimising
                # COMBINED collect+deliver deadhead, not the collection-nearest depot.
                # We own both legs and there is no inter-depot trunk for FULL_FLEET, so a
                # single-depot stage is teleport-safe (collect to S, rest at S, deliver
                # from S). Falls back to origin_depot when coords or the flag are absent.
                stage_depot = origin_depot
                if _fp_config.FULLFLEET_COMBINED_STAGING and o_lat is not None and o_lon is not None:
                    d_lat, d_lon = latlon(dest_pc, postcode_cache)
                    if d_lat is not None and d_lon is not None:
                        stage_depot = combined_staging_depot(o_lat, o_lon, d_lat, d_lon)
                for tag, fid, part_p, part_kg in parts:
                    out.append(_leg(
                        record=record, suffix=_suffix("C", tag), flow=flow, leg_kind=CUSTOMER_PICKUP,
                        service_date=collect_date, origin_node=CUSTOMER, destination_node=DEPOT,
                        service_pc=origin_pc, source_depot=stage_depot, target_depot=stage_depot,
                        ready_state="AT_CUSTOMER_ORIGIN", result_state="AT_DEPOT",
                        raw_window_start=prs, raw_window_end=pre,
                        effective_window_start=pes, effective_window_end=pee,
                        window_hardness=ph, freight_ready_time=pes,
                        cache=postcode_cache, option_set=fid, option_group="XDOCK",
                        freight_id=fid, pallets=part_p, weight_kg=part_kg,
                        depot_bound=_pinned(stage_depot),
                    ))
                    out.append(_leg(
                        record=record, suffix=_suffix("D", tag), flow=flow, leg_kind=CUSTOMER_DELIVERY,
                        service_date=deliver_date, origin_node=DEPOT, destination_node=CUSTOMER,
                        service_pc=dest_pc, source_depot=stage_depot, target_depot=dest_depot,
                        ready_state="AT_DEPOT", result_state="DELIVERED",
                        raw_window_start=drs, raw_window_end=dre,
                        effective_window_start=des, effective_window_end=dee,
                        window_hardness=dh, freight_ready_time=collect_date,
                        cache=postcode_cache, option_set=fid, option_group="XDOCK",
                        freight_id=fid, pallets=part_p, weight_kg=part_kg,
                        depot_bound=_pinned(stage_depot),
                    ))
            else:
                # Same-day: emit BOTH direct and crossdock options per freight unit.
                service_date = deliver_date or collect_date
                prs, pre, pes, pee, ph = pickup_windows(row, flow)
                drs, dre, des, dee, dh = delivery_windows(row)
                o_lat, o_lon = latlon(origin_pc, postcode_cache)
                # decision-audit #11 (2026-07-26): mirrors the multi-day branch above --
                # stage at the depot minimising COMBINED collect+deliver deadhead (with
                # the collection-side scarcity/spoke-cap guard), not the
                # collection-nearest depot alone. Pre-fix, a CB22-collect/Bedford-deliver
                # order pinned its XDOCK legs to CB22 even when Bedford minimises the
                # round trip -- an avoidable cross-depot km every time it fired.
                stage_depot = origin_depot
                if _fp_config.FULLFLEET_COMBINED_STAGING and o_lat is not None and o_lon is not None:
                    d_lat, d_lon = latlon(dest_pc, postcode_cache)
                    if d_lat is not None and d_lon is not None:
                        stage_depot = combined_staging_depot(o_lat, o_lon, d_lat, d_lon)
                for tag, fid, part_p, part_kg in parts:
                    option_key = fid
                    if _collocated_with_depot(o_lat, o_lon, origin_depot):
                        # The origin IS the source depot's estate: emit ONE depot-loaded
                        # delivery (identity kept as :DIR) instead of the DIR + XC/XD
                        # trio, so same-origin orders co-load into one multi-drop run.
                        # depot_bound pins it to home vehicles (the freight is THERE);
                        # depart_floor holds the trip until the freight is loadable.
                        ready = _collocated_ready(pes)
                        out.append(_leg(
                            record=record, suffix=_suffix("DIR", tag), flow=flow, leg_kind=CUSTOMER_DELIVERY,
                            service_date=service_date,
                            origin_node=DEPOT, destination_node=CUSTOMER,
                            service_pc=dest_pc, source_depot=origin_depot, target_depot=dest_depot,
                            ready_state="AT_DEPOT", result_state="DELIVERED",
                            raw_window_start=drs, raw_window_end=dre,
                            effective_window_start=des, effective_window_end=dee,
                            window_hardness=dh, freight_ready_time=ready,
                            cache=postcode_cache, origin_pc=origin_pc,
                            freight_id=fid, pallets=part_p, weight_kg=part_kg,
                            depart_floor=ready, depot_bound=origin_depot,
                        ))
                        continue
                    out.append(_leg(
                        record=record, suffix=_suffix("DIR", tag), flow=flow, leg_kind=DIRECT_CUSTOMER_MOVE,
                        service_date=service_date,
                        origin_node=CUSTOMER, destination_node=CUSTOMER,
                        service_pc=dest_pc, source_depot=stage_depot, target_depot=dest_depot,
                        ready_state="AT_CUSTOMER_ORIGIN", result_state="DELIVERED",
                        raw_window_start=drs, raw_window_end=dre,
                        effective_window_start=des, effective_window_end=dee,
                        window_hardness=dh, freight_ready_time=record.collect_timestamp,
                        cache=postcode_cache,
                        option_set=option_key, option_group="DIRECT",
                        origin_lat=o_lat, origin_lon=o_lon, origin_pc=origin_pc,
                        freight_id=fid, pallets=part_p, weight_kg=part_kg,
                    ))
                    out.append(_leg(
                        record=record, suffix=_suffix("XC", tag), flow=flow, leg_kind=CUSTOMER_PICKUP,
                        service_date=collect_date or service_date,
                        origin_node=CUSTOMER, destination_node=DEPOT,
                        service_pc=origin_pc, source_depot=stage_depot, target_depot=stage_depot,
                        ready_state="AT_CUSTOMER_ORIGIN", result_state="AT_DEPOT",
                        raw_window_start=prs, raw_window_end=pre,
                        effective_window_start=pes, effective_window_end=pee,
                        window_hardness=ph, freight_ready_time=pes,
                        cache=postcode_cache,
                        option_set=option_key, option_group="XDOCK",
                        freight_id=fid, pallets=part_p, weight_kg=part_kg,
                        depot_bound=_pinned(stage_depot),
                    ))
                    staged_des = staged_delivery_start(des, pee, SAME_DAY_XDOCK_HANDOFF_MIN)
                    out.append(_leg(
                        record=record, suffix=_suffix("XD", tag), flow=flow, leg_kind=CUSTOMER_DELIVERY,
                        service_date=service_date,
                        origin_node=DEPOT, destination_node=CUSTOMER,
                        service_pc=dest_pc, source_depot=stage_depot, target_depot=dest_depot,
                        ready_state="AT_DEPOT", result_state="DELIVERED",
                        raw_window_start=drs, raw_window_end=dre,
                        effective_window_start=staged_des, effective_window_end=dee,
                        window_hardness=dh, freight_ready_time=staged_des,
                        cache=postcode_cache,
                        option_set=option_key, option_group="XDOCK",
                        freight_id=fid, pallets=part_p, weight_kg=part_kg,
                        depot_bound=_pinned(stage_depot),
                    ))
            continue

        if flow == "LOCAL_COLLECT":
            rs, re, es, ee, hardness = pickup_windows(row, flow)
            for tag, fid, part_p, part_kg in _split_parts(record, hazardous=(hub == LE10_HUB)):
                out.append(_leg(
                    record=record, suffix=_suffix("C", tag), flow=flow, leg_kind=CUSTOMER_PICKUP,
                    service_date=record.collect_date, origin_node=CUSTOMER, destination_node=DEPOT,
                    service_pc=origin_pc, source_depot=origin_depot, target_depot=origin_depot,
                    ready_state="AT_CUSTOMER_ORIGIN", result_state="AT_DEPOT",
                    raw_window_start=rs, raw_window_end=re,
                    effective_window_start=es, effective_window_end=ee,
                    window_hardness=hardness, freight_ready_time=es,
                    cache=postcode_cache,
                    freight_id=fid, pallets=part_p, weight_kg=part_kg,
                    depot_bound=_pinned(origin_depot),
                ))
            continue

        if flow == "LOCAL_DELIVER":
            rs, re, es, ee, hardness = delivery_windows(row)
            for tag, fid, part_p, part_kg in _split_parts(record, hazardous=(hub == LE10_HUB)):
                out.append(_leg(
                    record=record, suffix=_suffix("D", tag), flow=flow, leg_kind=CUSTOMER_DELIVERY,
                    service_date=record.deliver_date, origin_node=DEPOT, destination_node=CUSTOMER,
                    service_pc=dest_pc, source_depot=dest_depot, target_depot=dest_depot,
                    ready_state="AT_DEPOT", result_state="DELIVERED",
                    raw_window_start=rs, raw_window_end=re,
                    effective_window_start=es, effective_window_end=ee,
                    window_hardness=hardness, freight_ready_time=es,
                    cache=postcode_cache,
                    freight_id=fid, pallets=part_p, weight_kg=part_kg,
                    depot_bound=_pinned(dest_depot),
                ))
            continue

        out.append(_leg(
            record=record, suffix="A", flow=flow, leg_kind=ACCOUNTING_ONLY,
            service_date=record.deliver_date or record.collect_date,
            origin_node="", destination_node="", service_pc=dest_pc or origin_pc,
            source_depot="", target_depot="", ready_state="", result_state="AMBIGUOUS_MANUAL",
            cache=postcode_cache, customer_dispatchable=False,
        ))

    return out
