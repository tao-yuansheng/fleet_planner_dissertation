"""Mega-shipper shuttle carve-out (K1): detection + packing.

Pure functions over the candidate frame — no ledger, no routing. The seed
applies the bins (route_seed.run_route_seed_plan) with the real evaluators.
Spec: docs/superpowers/specs/2026-07-03-shuttle-carveout-design.md
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from freight_planner.config import SHUTTLE_MIN_FILL, SHUTTLE_MIN_PALLETS

_CARVABLE_KINDS = ("CUSTOMER_PICKUP", "CUSTOMER_DELIVERY")
_EPS = 1e-6  # float tolerance on capacity-fit and min-fill gates (3.9*6 vs 0.9*26)


@dataclass(frozen=True)
class ShuttleBin:
    service_date: str
    service_pc: str
    leg_kind: str
    anchor_depot: str
    job_ids: tuple[str, ...]
    pallets: float
    bin_capacity: float
    eligible_vehicles: tuple[str, ...]  # anchor-depot vehicles, tractors first


def _pallets(row: dict) -> float:
    """Pallet count with NaN treated as 0.0 (NaN would poison the qualify gate)."""
    p = row.get("pallets")
    if p is None or pd.isna(p):
        return 0.0
    return float(p)


def _anchor(row: dict) -> str:
    if str(row.get("leg_kind", "")) == "CUSTOMER_PICKUP":
        return str(row.get("target_depot", "") or "")
    return str(row.get("source_depot", "") or "")


def eligible_shuttle_vehicles(job_rows, options, vehicles: pd.DataFrame,
                              anchor_depot: str) -> list[str]:
    """Vehicles OK for EVERY job, homed at the anchor depot, tractors first."""
    ok: set[str] | None = None
    for row in job_rows:
        vids = {v for v, _same in options.get(str(row.get("leg_id", "")), [])}
        ok = vids if ok is None else (ok & vids)
    if not ok:
        return []
    vrows = vehicles[vehicles["vehicle_id"].astype(str).isin(ok)
                     & vehicles["home_depot"].astype(str).eq(anchor_depot)]
    vrows = vrows.assign(
        _rank=(vrows["vehicle_type"].astype(str) != "tractor").astype(int))
    vrows = vrows.sort_values(["_rank", "capacity_pallets"], ascending=[True, False])
    return [str(v) for v in vrows["vehicle_id"]]


def detect_shuttle_bins(runnable: pd.DataFrame, options, vehicles: pd.DataFrame,
                        min_pallets: float = SHUTTLE_MIN_PALLETS,
                        min_fill: float = SHUTTLE_MIN_FILL) -> list[ShuttleBin]:
    """Qualify address-days and pack them into nearly-full shuttle bins (FFD)."""
    if runnable is None or runnable.empty:
        return []
    df = runnable[runnable["leg_kind"].astype(str).isin(_CARVABLE_KINDS)].copy()
    if df.empty:
        return []
    df = df[df["hard_blocker"].fillna("").astype(str) == ""]
    if df.empty:
        return []
    df["_anchor"] = [_anchor(r) for r in df.to_dict("records")]
    df = df[df["_anchor"] != ""]
    bins: list[ShuttleBin] = []
    cap_by_vid = dict(zip(vehicles["vehicle_id"].astype(str),
                          vehicles["capacity_pallets"].astype(float)))
    for (day, pc, kind, anchor), grp in df.groupby(
            ["service_date", "service_pc", "leg_kind", "_anchor"], sort=True):
        rows = grp.to_dict("records")
        total = sum(_pallets(r) for r in rows)
        if total + _EPS < float(min_pallets) or str(pc).strip() == "":
            continue
        vids = eligible_shuttle_vehicles(rows, options, vehicles, str(anchor))
        if not vids:
            continue
        bin_cap = max(cap_by_vid.get(v, 0.0) for v in vids)
        if bin_cap <= 0.0:
            continue
        # first-fit-decreasing
        packed: list[list[dict]] = []
        loads: list[float] = []
        for r in sorted(rows, key=lambda r: -_pallets(r)):
            p = _pallets(r)
            for i, load in enumerate(loads):
                if load + p <= bin_cap + _EPS:
                    packed[i].append(r)
                    loads[i] += p
                    break
            else:
                packed.append([r])
                loads.append(p)
        for jobs, load in zip(packed, loads):
            if load >= float(min_fill) * bin_cap - _EPS:
                bins.append(ShuttleBin(
                    service_date=str(day), service_pc=str(pc), leg_kind=str(kind),
                    anchor_depot=str(anchor),
                    job_ids=tuple(str(r.get("job_id", "")) for r in jobs),
                    pallets=float(load), bin_capacity=float(bin_cap),
                    eligible_vehicles=tuple(vids)))
    return bins
