from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from functools import lru_cache
from typing import Any

import pandas as pd

from freight_planner.enrich import apply_order_load_corrections

from freight_planner.shared.scope import classify_order, _served_postcodes, _cached_coords
from freight_planner.shared.verified_legs import corrected_flow, verified_leg

from freight_planner.enrich import verified_leg_lookup
from freight_planner.paths import DEFAULT_POSTCODE_CACHE

_RESOURCE_COLS = ("resource_rigid", "resource_tractor", "resource_van")


@lru_cache(maxsize=1)
def _postcode_cache() -> dict:
    """The postcode cache, read once. Used only to detect endpoints that will not
    geocode (a recorded failure / no coordinates) — never to mutate/geocode."""
    try:
        return json.loads(DEFAULT_POSTCODE_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _served_endpoint_ungeocodable(row: pd.Series, flow: str | None) -> bool:
    """True when an endpoint the fleet must serve has no coordinates in the
    postcode cache (DUBLIN/CARLOW/garbled-postcode class), so the leg is
    un-routable. Cache-only (no geocoding) — mirrors the dissertation universe."""
    if flow is None:
        return False
    cache = _postcode_cache()
    return any(_cached_coords(str(pc), cache) is None for pc in _served_postcodes(row, flow))
BACKTEST_VERIFIED = "backtest_verified"
FORWARD_STRUCTURAL = "forward_structural"
RESPONSIBILITY_MODES = {BACKTEST_VERIFIED, FORWARD_STRUCTURAL}


def is_empty(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text == "" or text.lower() == "nan"


def split_csv_values(value: Any) -> list[str]:
    if is_empty(value):
        return []
    return [p.strip().replace(" ", "").upper() for p in str(value).split(",") if p.strip()]


def powered_vehicle_regs(row: pd.Series) -> list[str]:
    regs: list[str] = []
    for col in _RESOURCE_COLS:
        regs.extend(split_csv_values(row.get(col)))
    # Preserve order while deduplicating.
    return list(dict.fromkeys(regs))


def first_date(row: pd.Series, *columns: str) -> date | None:
    for col in columns:
        ts = pd.to_datetime(row.get(col), errors="coerce")
        if pd.notna(ts):
            return ts.date()
    return None


def first_timestamp(row: pd.Series, *columns: str) -> str:
    for col in columns:
        ts = pd.to_datetime(row.get(col), errors="coerce")
        if pd.notna(ts):
            return ts.isoformat(sep=" ")
    return ""


def _date_in_window(value: date | None, start: date, end: date) -> bool:
    return value is not None and start <= value <= end


def in_window(row: pd.Series, start: date, end: date, flow: str | None = None) -> bool:
    collect = first_date(row, "origin_timestamp_local", "origin_requested_start_timestamp_local", "origin_date")
    deliver = first_date(row, "destination_timestamp_local", "destination_requested_start_timestamp_local", "destination_date")
    if flow in {"PL_EXPORT", "LOCAL_COLLECT"}:
        return _date_in_window(collect, start, end)
    if flow in {"PL_IMPORT", "LOCAL_DELIVER"}:
        return _date_in_window(deliver, start, end)
    if flow == "FULL_FLEET":
        return _date_in_window(collect, start, end) or _date_in_window(deliver, start, end)
    return _date_in_window(collect, start, end) or _date_in_window(deliver, start, end)


def network_from_flow(flow: str | None) -> str:
    if flow in ("PL_IMPORT", "PL_EXPORT"):
        return "HAZCHEM_OR_PALLETLINE"
    if flow == "FULL_FLEET":
        return "FULL_FLEET"
    if flow in ("LOCAL_COLLECT", "LOCAL_DELIVER"):
        return "LOCAL"
    return "UNKNOWN"


def exclusion_reason(row: pd.Series, flow: str | None, responsibility_mode: str = BACKTEST_VERIFIED) -> str:
    status = str(row.get("status") or "").upper()
    if status == "CANCELLED":
        return "CANCELLED"
    vehicle_category = str(row.get("vehicle_category_name") or "").lower()
    if "crane" in vehicle_category:
        return "CRANE_HIRE"
    if "specialist" in vehicle_category:
        return "SPECIALIST_MOVEMENT"
    if flow is None:
        return "AMBIGUOUS_MANUAL" if responsibility_mode == FORWARD_STRUCTURAL or powered_vehicle_regs(row) else "NO_RESOURCES"
    # Stakeholder 2026-07-02: an order no fleet vehicle ever touched is not our
    # work in ANY mode, and a subcontractor does NOT count as a resource — those
    # are third-party/network movements (the Palletline-API class) that inflated
    # the universe and sent phantom tours to Scotland.
    if not powered_vehicle_regs(row):
        return "NO_RESOURCES"
    # An order whose served endpoint will not geocode cannot be routed (the
    # DUBLIN/CARLOW/garbled-postcode class). Excluded here so it never enters the
    # in-universe denominator (previously it slipped in and was rejected late as
    # a BAD_GEOCODE unassignment).
    if _served_endpoint_ungeocodable(row, flow):
        return "BAD_GEOCODE"
    return ""


@dataclass(frozen=True)
class DemandRecord:
    order_id: str
    order_name: str
    status: str
    raw_flow: str
    corrected_flow: str
    responsibility_shape: str
    responsibility_source: str
    exclusion_reason: str
    origin_pc: str
    destination_pc: str
    collect_date: str
    deliver_date: str
    collect_timestamp: str
    deliver_timestamp: str
    pallets: float
    weight_kg: float
    historical_resources: str
    resource_subcontractor: str
    network: str

    def to_dict(self) -> dict:
        return asdict(self)


def responsibility_shape(flow: str | None, leg: str | None, exclusion: str) -> tuple[str, str]:
    """Which leg the FLEET is responsible for, from (flow, telematics-verified leg).

    IMPORTANT — the NETWORK_* labels name the NETWORK's leg; OUR (fleet) leg is
    the OTHER one. Do NOT read "NETWORK_IMPORT" as "the network does it all":

      * NETWORK_EXPORT  (PL_EXPORT + verified COLLECTION) -> WE COLLECT, network delivers onward.
      * NETWORK_IMPORT  (PL_IMPORT + verified DELIVERY)   -> network trunks it in, WE DELIVER.
      * FULL_END_TO_END (FULL_FLEET)                      -> we collect AND deliver.
      * PICKUP_ONLY / DELIVERY_ONLY (LOCAL_*)             -> we do that single leg.
      * OUT_OF_SCOPE / AMBIGUOUS_PARTIAL                  -> not ours / needs manual.

    ``source == "telematics_verified"`` means the GPS trace confirms the fleet
    DID our leg, so the order is IN scope. Coverage rule of thumb: fleet
    deliveries ~= PL_IMPORT + FULL_FLEET + LOCAL_DELIVER; a plan with collections
    but ~no PL_IMPORT deliveries is UNDER-serving, not correct groupage.
    """
    if exclusion == "NO_RESOURCES":
        return "OUT_OF_SCOPE", "qargo_no_fleet_resource"
    if exclusion in {"CANCELLED", "CRANE_HIRE", "SPECIALIST_MOVEMENT"}:
        return "OUT_OF_SCOPE", "qargo_status_or_specialist"
    if exclusion == "BAD_GEOCODE":
        return "OUT_OF_SCOPE", "endpoint_will_not_geocode"
    if exclusion == "AMBIGUOUS_MANUAL":
        return "AMBIGUOUS_PARTIAL", "manual_required"
    if leg:
        source = "telematics_verified"
        if leg == "FULL_FLEET":
            return "FULL_END_TO_END", source
        if leg == "COLLECTION":
            if flow == "PL_EXPORT":
                return "NETWORK_EXPORT", source
            return "PICKUP_ONLY", source
        if leg == "DELIVERY":
            if flow == "PL_IMPORT":
                return "NETWORK_IMPORT", source
            return "DELIVERY_ONLY", source
    if flow == "PL_IMPORT":
        return "NETWORK_IMPORT", "structural_rule"
    if flow == "PL_EXPORT":
        return "NETWORK_EXPORT", "structural_rule"
    if flow == "FULL_FLEET":
        return "FULL_END_TO_END", "structural_rule"
    if flow == "LOCAL_COLLECT":
        return "PICKUP_ONLY", "structural_rule"
    if flow == "LOCAL_DELIVER":
        return "DELIVERY_ONLY", "structural_rule"
    return "AMBIGUOUS_PARTIAL", "manual_required"


def _flow_and_leg(
    row: pd.Series,
    raw_flow: str | None,
    responsibility_mode: str,
    lookup: dict | None = None,
) -> tuple[str | None, str | None]:
    if responsibility_mode not in RESPONSIBILITY_MODES:
        raise ValueError(f"Unknown responsibility mode: {responsibility_mode}")
    leg = verified_leg(row.get("order_id"), lookup)
    return corrected_flow(row, raw_flow, lookup), leg


def build_demand_records(
    qargo_df: pd.DataFrame,
    start: date,
    end: date,
    responsibility_mode: str = BACKTEST_VERIFIED,
) -> list[DemandRecord]:
    # Idempotent safeguard for callers that pass a raw/in-memory order frame
    # instead of the canonical enriched parquet.
    qargo_df = apply_order_load_corrections(qargo_df)
    records: list[DemandRecord] = []
    # Prefer the persisted enriched verified_leg column when present; otherwise
    # verified_leg falls back to freight_planner/data/verified_legs.csv.
    leg_lookup = verified_leg_lookup(qargo_df)
    for _, row in qargo_df.iterrows():
        raw = classify_order(row)
        corr, leg = _flow_and_leg(row, raw, responsibility_mode, leg_lookup)
        if not in_window(row, start, end, corr):
            continue
        excl = exclusion_reason(row, corr, responsibility_mode)
        shape, source = responsibility_shape(corr, leg, excl)
        resources = powered_vehicle_regs(row)
        records.append(DemandRecord(
            order_id=str(row.get("order_id") or ""),
            order_name=str(row.get("name") or ""),
            status=str(row.get("status") or ""),
            raw_flow=raw or "",
            corrected_flow=corr or "",
            responsibility_shape=shape,
            responsibility_source=source,
            exclusion_reason=excl,
            origin_pc=str(row.get("origin_postal_code") or "").strip().upper(),
            destination_pc=str(row.get("destination_postal_code") or "").strip().upper(),
            collect_date=str(first_date(row, "origin_timestamp_local", "origin_requested_start_timestamp_local", "origin_date") or ""),
            deliver_date=str(first_date(row, "destination_timestamp_local", "destination_requested_start_timestamp_local", "destination_date") or ""),
            collect_timestamp=first_timestamp(row, "origin_timestamp_local", "origin_requested_start_timestamp_local"),
            deliver_timestamp=first_timestamp(row, "destination_timestamp_local", "destination_requested_start_timestamp_local"),
            pallets=float(row.get("goods_pallet_spaces") or 0.0),
            weight_kg=float(row.get("goods_weight") or 0.0),
            historical_resources=",".join(resources),
            resource_subcontractor="" if is_empty(row.get("resource_subcontractor")) else str(row.get("resource_subcontractor")),
            network=network_from_flow(corr),
        ))
    return records
