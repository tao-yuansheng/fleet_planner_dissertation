"""Week-to-week state handover artifact.

Week N's run emits ``handover.json``; week N+1 consumes it so its opening state
is week N's plan end-state: in-flight vehicles stay out, already-delivered spill
orders are excluded, and staged freight is seeded at the depot the prior plan
left it. Empty/absent handover == cold start (unchanged single-week behavior).
"""
from __future__ import annotations

import datetime
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from freight_planner.freight_ledger import FREIGHT_AT_DEPOT, FREIGHT_DELIVERED

# Responsibility shapes where WE do not deliver — freight is handed to the Palletline
# network at a hub. Their AT_DEPOT state in the plan is a handoff, not staging.
COLLECTION_ONLY_SHAPES = {"NETWORK_EXPORT", "PICKUP_ONLY"}


@dataclass(frozen=True)
class VehicleAvailability:
    vehicle_id: str
    available_from: str  # ISO datetime the vehicle is free again
    at_node: str         # where it ends (home depot under whole-tour ownership)


@dataclass(frozen=True)
class StagedFreight:
    order_id: str
    freight_id: str
    depot: str
    ready_time: str
    pallets: float
    weight_kg: float


@dataclass
class Handover:
    produced_by_start: str = ""
    produced_by_end: str = ""
    vehicle_availability: list[VehicleAvailability] = field(default_factory=list)
    delivered_order_ids: set[str] = field(default_factory=set)
    staged_freight: list[StagedFreight] = field(default_factory=list)

    @classmethod
    def empty(cls) -> "Handover":
        return cls()

    def is_empty(self) -> bool:
        return (
            not self.vehicle_availability
            and not self.delivered_order_ids
            and not self.staged_freight
        )

    def to_json_dict(self) -> dict:
        return {
            "produced_by": {"start": self.produced_by_start, "end": self.produced_by_end},
            "vehicle_availability": [asdict(v) for v in self.vehicle_availability],
            "delivered_order_ids": sorted(self.delivered_order_ids),
            "staged_freight": [asdict(s) for s in self.staged_freight],
        }

    @classmethod
    def from_json_dict(cls, d: dict) -> "Handover":
        pb = d.get("produced_by", {}) or {}
        return cls(
            produced_by_start=str(pb.get("start", "") or ""),
            produced_by_end=str(pb.get("end", "") or ""),
            vehicle_availability=[
                VehicleAvailability(str(v.get("vehicle_id", "")), str(v.get("available_from", "")), str(v.get("at_node", "")))
                for v in d.get("vehicle_availability", []) or []
            ],
            delivered_order_ids={str(x) for x in d.get("delivered_order_ids", []) or []},
            staged_freight=[
                StagedFreight(
                    str(s.get("order_id", "")), str(s.get("freight_id", "")),
                    str(s.get("depot", "")), str(s.get("ready_time", "")),
                    float(s.get("pallets", 0.0) or 0.0), float(s.get("weight_kg", 0.0) or 0.0),
                )
                for s in d.get("staged_freight", []) or []
            ],
        )


def save_handover(handover: Handover, path: str | Path) -> None:
    Path(path).write_text(json.dumps(handover.to_json_dict(), indent=2), encoding="utf-8")


def load_handover(path: str | Path | None) -> Handover:
    if not path:
        return Handover.empty()
    p = Path(path)
    if not p.exists():
        return Handover.empty()
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return Handover.empty()
    return Handover.from_json_dict(json.loads(text))


def _end_stamp(row: pd.Series) -> str:
    """Best available end timestamp for a plan row: depart, else arrive, else EOD."""
    for col in ("planned_depart", "planned_arrive"):
        v = str(row.get(col) or "")
        if v:
            return v
    sd = str(row.get("service_date") or "")
    return f"{sd[:10]}T23:59:00" if sd else ""


def build_handover(
    selected_df: pd.DataFrame,
    demand_df: pd.DataFrame,
    window_start: datetime.date,
    window_end: datetime.date,
    candidate_df: pd.DataFrame | None = None,
) -> Handover:
    h = Handover(produced_by_start=window_start.isoformat(), produced_by_end=window_end.isoformat())
    if selected_df is None or selected_df.empty:
        return h

    df = selected_df.copy()
    df["order_id"] = df["order_id"].astype(str)
    df["freight_state_after"] = df.get("freight_state_after", "").astype(str)

    # --- delivered: any leg reached DELIVERED ---
    delivered = set(df.loc[df["freight_state_after"] == FREIGHT_DELIVERED, "order_id"])
    h.delivered_order_ids = {o for o in delivered if o}

    # --- staged: reached AT_DEPOT, never DELIVERED ---
    at_depot = set(df.loc[df["freight_state_after"] == FREIGHT_AT_DEPOT, "order_id"])
    staged_ids = {o for o in (at_depot - delivered) if o}
    dmd = demand_df.copy() if demand_df is not None else pd.DataFrame()
    if not dmd.empty:
        dmd["order_id"] = dmd["order_id"].astype(str)
        dmd = dmd.drop_duplicates("order_id").set_index("order_id")
    cand = candidate_df.copy() if candidate_df is not None else pd.DataFrame()
    if not cand.empty and "leg_id" in cand.columns:
        cand["leg_id"] = cand["leg_id"].astype(str)
        cand = cand.drop_duplicates("leg_id").set_index("leg_id")
    # A collection-only order (PL_EXPORT) that reaches AT_DEPOT was handed to the
    # Palletline network at a hub; its handoff leg lives OUTSIDE selected_df (in the
    # trunk schedule), so it only looks staged. It is not in our network — exclude it.
    if not dmd.empty and "responsibility_shape" in dmd.columns:
        collection_only = {
            oid for oid in staged_ids
            if oid in dmd.index and str(dmd.loc[oid, "responsibility_shape"]) in COLLECTION_ONLY_SHAPES
        }
        staged_ids -= collection_only
    for oid in sorted(staged_ids):
        pk = df[(df["order_id"] == oid) & (df["freight_state_after"] == FREIGHT_AT_DEPOT)]
        pk = pk.sort_values("sequence")
        last_pk = pk.iloc[-1] if not pk.empty else None
        crow = None
        if last_pk is not None and not cand.empty:
            leg_id = str(last_pk.get("leg_id") or "")
            if leg_id and leg_id in cand.index:
                crow = cand.loc[leg_id]
        depot = ""
        if crow is not None:
            depot = str(crow.get("target_depot") or crow.get("source_depot") or "")
        if not depot and last_pk is not None:
            node = str(last_pk.get("destination_node") or "")
            if node and node not in {"DEPOT", "CUSTOMER"}:
                depot = node
        if not depot and last_pk is not None:
            depot = str(last_pk.get("vehicle_home_depot") or "")
        ready = _end_stamp(pk.iloc[-1]) if not pk.empty else ""
        drow = dmd.loc[oid] if (not dmd.empty and oid in dmd.index) else None
        freight_id = crow.get("freight_id") if crow is not None else None
        if pd.isna(freight_id) or not str(freight_id or "").strip():
            freight_id = drow.get("freight_id") if drow is not None else None
        if pd.isna(freight_id) or not str(freight_id or "").strip():
            freight_id = oid
        h.staged_freight.append(StagedFreight(
            order_id=oid,
            freight_id=str(freight_id),
            depot=depot,
            ready_time=ready,
            pallets=float(drow.get("pallets") if drow is not None else 0.0),
            weight_kg=float(drow.get("weight_kg") if drow is not None else 0.0),
        ))

    # --- in-flight vehicles: any job dated after Saturday (window_end) ---
    sd = pd.to_datetime(df["service_date"], errors="coerce").dt.date
    late_mask = sd.notna() & (sd > window_end)
    for vid, grp in df[late_mask].groupby("vehicle_id"):
        grp2 = df[df["vehicle_id"] == vid].copy()
        grp2["_sd"] = pd.to_datetime(grp2["service_date"], errors="coerce")
        grp2 = grp2.sort_values(["_sd", "sequence"])
        last = grp2.iloc[-1]
        home = str(last.get("vehicle_home_depot") or "") or str(last.get("destination_node") or "")
        h.vehicle_availability.append(VehicleAvailability(
            vehicle_id=str(vid),
            available_from=_end_stamp(last),
            at_node=home,
        ))
    return h


def apply_exclusion(
    legs_df: pd.DataFrame, demand_df: pd.DataFrame, handover: Handover
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Drop already-delivered orders from both frames so the next week does not
    re-plan spill deliveries that the prior plan's tour tail already served."""
    served = handover.delivered_order_ids
    if not served:
        return legs_df, demand_df
    legs2 = legs_df
    if legs_df is not None and not legs_df.empty and "order_id" in legs_df.columns:
        legs2 = legs_df[~legs_df["order_id"].astype(str).isin(served)].copy()
    demand2 = demand_df
    if demand_df is not None and not demand_df.empty and "order_id" in demand_df.columns:
        demand2 = demand_df[~demand_df["order_id"].astype(str).isin(served)].copy()
    return legs2, demand2


def apply_availability(
    vehicle_df: pd.DataFrame, handover: Handover, window_start: datetime.date
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Patch ``available_from`` for vehicles still in flight at window open.

    Only vehicles present in the fleet AND whose handover availability is later
    than the window's opening midnight are held back. Returns the patched frame
    and an ``{vehicle_id: available_from}`` override dict for ALNS-time gating.
    """
    overrides: dict[str, str] = {}
    if not handover.vehicle_availability or vehicle_df is None or vehicle_df.empty:
        return vehicle_df, overrides
    known = set(vehicle_df["vehicle_id"].astype(str))
    open_iso = f"{window_start.isoformat()}T00:00:00"
    for va in handover.vehicle_availability:
        if va.vehicle_id not in known:
            continue  # fleet regenerated / reg retired -> skip defensively
        if not va.available_from or va.available_from <= open_iso:
            continue  # home before the window opens -> free, no override
        overrides[va.vehicle_id] = va.available_from
    if not overrides:
        return vehicle_df, overrides
    veh = vehicle_df.copy()
    veh["available_from"] = veh.apply(
        lambda r: overrides.get(str(r["vehicle_id"]), r["available_from"]), axis=1
    )
    return veh, overrides


def staged_depot_map(handover: Handover) -> dict[str, tuple[str, str]]:
    """``{order_id: (depot, ready_time)}`` for seeding staged freight at the depot
    the prior plan actually left it, overriding the historical-leg inference."""
    return {s.order_id: (s.depot, s.ready_time) for s in handover.staged_freight}
