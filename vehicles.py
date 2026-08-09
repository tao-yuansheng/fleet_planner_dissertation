from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

from freight_planner.shared.config import DEPOT_ANCHORS, VEHICLE_DEPOT_MAP, VEHICLE_PROFILES

# User rule 2026-07-16: one fleet-wide operating day. Vehicles become available
# at config.FLEET_DAY_START_HOUR (08:00, calibrated 2026-07-20 against the
# telematics first-delivery wave); there is NO per-vehicle end wall (19:00 is a
# soft target — service coverage first) — the 10h driving / 13h duty caps are
# the binding physics. Telematics shift medians are NOT constraints: the median
# hid real capacity (a vehicle was refused evening work its own telematics show
# it regularly did).
from freight_planner import config as _fp_config
FLEET_AVAILABLE_FROM = time(int(_fp_config.FLEET_DAY_START_HOUR), 0)

# Finalized enriched vehicle master (physical payload + pallet capacity, validated).
# Built by freight_planner/tools/build_vehicle_master.py. Its capacities are the source of
# truth; the telematics observed-p95 profile is only a fallback for any vehicle absent.
_MASTER_CSV = Path(__file__).resolve().parent / "data" / "vehicle_master.csv"


def _norm_reg(reg) -> str:
    return str(reg or "").replace(" ", "").upper()


def _load_master_caps(path=_MASTER_CSV) -> dict[str, tuple[float, float]]:
    """reg -> (payload_kg, pallet_capacity) from the vehicle master, if present."""
    out: dict[str, tuple[float, float]] = {}
    p = Path(path)
    if p.exists():
        with open(p, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    out[_norm_reg(r.get("reg"))] = (float(r["payload_kg"]), float(r["pallet_capacity"]))
                except (KeyError, ValueError, TypeError):
                    continue
    return out


_MASTER_CAPS = _load_master_caps()

# Conservative fallback ceiling (pallets, kg) used only when the master is absent.
_DEFAULT_CEILING = (26.0, 26000.0)


def fleet_capacity_ceiling(master_caps: dict | None = None) -> tuple[float, float]:
    """(max_pallets, max_kg) of the largest single vehicle in the fleet master.

    This is the physical ceiling above which no one vehicle can carry an order
    (MASSIVE_UNSUPPORTED) and the chunk size used to split one that exceeds it.
    Sourcing it from the validated master keeps leg generation in step with the
    real fleet (our 44 t artics carry 28 t / 26 pallets) instead of a stale
    hardcoded constant. Falls back conservatively if the master is unavailable.
    """
    caps = _MASTER_CAPS if master_caps is None else master_caps
    if not caps:
        return _DEFAULT_CEILING
    max_kg = max(kg for kg, _pal in caps.values())
    max_pal = max(pal for _kg, pal in caps.values())
    return (max_pal, max_kg)


def _resolve_capacity(vehicle_id, profile: dict, master_caps: dict) -> tuple[float, float, str, str]:
    """Capacity for a vehicle: the validated vehicle-master physical payload/pallets
    win over the observed-p95 profile; fall back to the profile when not in the master."""
    cap_kg = float(profile.get("capacity_kg_per_trip") or 0.0)
    cap_pal = float(profile.get("capacity_pallets_per_trip") or 0.0)
    kg_src = str(profile.get("capacity_kg_source") or "")
    pal_src = str(profile.get("capacity_pallets_source") or "")
    mc = master_caps.get(_norm_reg(vehicle_id))
    if mc:
        cap_kg, cap_pal = mc
        kg_src = pal_src = "vehicle_master"
    return cap_kg, cap_pal, kg_src, pal_src


@dataclass(frozen=True)
class VehicleStateRecord:
    vehicle_id: str
    home_depot: str
    current_node: str
    current_lat: float
    current_lon: float
    available_from: str
    shift_end: str
    vehicle_type: str
    asset_type: str
    capacity_kg: float
    capacity_pallets: float
    capacity_kg_source: str
    capacity_pallets_source: str
    master_max_tonnes: float | None
    master_typical_tonnes: float | None
    can_sleep_out: bool
    can_trunk: bool
    can_cross_depot: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _vehicle_type(asset_type: str) -> str:
    text = (asset_type or "").strip().lower()
    if text == "tractor unit":
        return "tractor"
    if "van" in text:
        return "van"
    return "rigid"


def build_vehicle_states(start: date) -> list[VehicleStateRecord]:
    records: list[VehicleStateRecord] = []
    for vehicle_id in sorted(VEHICLE_DEPOT_MAP):
        profile = VEHICLE_PROFILES.get(vehicle_id)
        if not profile:
            continue
        home_depot = VEHICLE_DEPOT_MAP[vehicle_id]
        lat, lon = DEPOT_ANCHORS.get(home_depot, DEPOT_ANCHORS["CB22"])
        asset_type = str(profile.get("asset_type") or "")
        vtype = _vehicle_type(asset_type)
        cap_kg, cap_pal, kg_src, pal_src = _resolve_capacity(vehicle_id, profile, _MASTER_CAPS)
        records.append(VehicleStateRecord(
            vehicle_id=vehicle_id,
            home_depot=home_depot,
            current_node=home_depot,
            current_lat=float(lat),
            current_lon=float(lon),
            available_from=datetime.combine(start, FLEET_AVAILABLE_FROM).isoformat(sep=" "),
            shift_end="",
            vehicle_type=vtype,
            asset_type=asset_type,
            capacity_kg=cap_kg,
            capacity_pallets=cap_pal,
            capacity_kg_source=kg_src,
            capacity_pallets_source=pal_src,
            master_max_tonnes=profile.get("master_max_tonnes"),
            master_typical_tonnes=profile.get("master_typical_tonnes"),
            can_sleep_out=vtype == "tractor",
            can_trunk=vtype == "tractor",
            can_cross_depot=True,
        ))
    return records


def vehicle_states_frame(start: date) -> pd.DataFrame:
    return pd.DataFrame([record.to_dict() for record in build_vehicle_states(start)])
