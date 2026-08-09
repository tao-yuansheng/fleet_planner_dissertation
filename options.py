from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd


@dataclass(frozen=True)
class JobOptionSummaryRecord:
    job_id: str
    leg_id: str
    order_id: str
    order_name: str
    leg_kind: str
    service_date: str
    source_depot: str
    target_depot: str
    hard_blocker: str
    ok_vehicle_count: int
    same_depot_ok_count: int
    cross_depot_ok_count: int
    best_vehicle_id: str
    best_vehicle_home_depot: str
    best_vehicle_type: str
    best_distance_km: float | None
    best_drive_minutes: float | None
    best_same_depot_vehicle_id: str
    best_same_depot_distance_km: float | None
    best_cross_depot_vehicle_id: str
    best_cross_depot_home_depot: str
    best_cross_depot_distance_km: float | None
    option_status: str

    def to_dict(self) -> dict:
        return asdict(self)


def _empty_best() -> dict[str, object]:
    return {
        "vehicle_id": "",
        "vehicle_home_depot": "",
        "vehicle_type": "",
        "current_to_service_km": None,
        "estimated_drive_minutes": None,
    }


def _best(grp: pd.DataFrame) -> dict[str, object]:
    if grp.empty:
        return _empty_best()
    row = grp.sort_values(["current_to_service_km", "estimated_drive_minutes", "vehicle_id"], na_position="last").iloc[0]
    return {
        "vehicle_id": str(row.get("vehicle_id") or ""),
        "vehicle_home_depot": str(row.get("vehicle_home_depot") or ""),
        "vehicle_type": str(row.get("vehicle_type") or ""),
        "current_to_service_km": None if pd.isna(row.get("current_to_service_km")) else float(row.get("current_to_service_km")),
        "estimated_drive_minutes": None if pd.isna(row.get("estimated_drive_minutes")) else float(row.get("estimated_drive_minutes")),
    }


def _option_status(job, ok_count: int, same_count: int, cross_count: int) -> str:
    blocker = str(getattr(job, "hard_blocker", "") or "")
    if blocker:
        return blocker
    if ok_count <= 0:
        return "NO_OK_VEHICLE_PAIR"
    if same_count > 0:
        return "HAS_SAME_DEPOT_OPTION"
    if cross_count > 0:
        return "CROSS_DEPOT_ONLY"
    return "NO_OK_VEHICLE_PAIR"


def build_job_option_summaries(
    candidates: pd.DataFrame,
    compatibility: pd.DataFrame,
) -> list[JobOptionSummaryRecord]:
    if candidates.empty:
        return []
    ok = compatibility[compatibility["compatibility_status"].eq("OK")].copy() if not compatibility.empty else compatibility.copy()
    ok_by_leg = {str(leg_id): grp.copy() for leg_id, grp in ok.groupby("leg_id")} if not ok.empty else {}
    rows: list[JobOptionSummaryRecord] = []
    for job in candidates.itertuples(index=False):
        leg_id = str(getattr(job, "leg_id", "") or "")
        grp = ok_by_leg.get(leg_id, pd.DataFrame())
        same = grp[grp["same_depot"].astype(bool)] if not grp.empty else grp
        cross = grp[grp["cross_depot"].astype(bool)] if not grp.empty else grp
        best = _best(grp)
        best_same = _best(same)
        best_cross = _best(cross)
        ok_count = int(len(grp))
        same_count = int(len(same))
        cross_count = int(len(cross))
        rows.append(JobOptionSummaryRecord(
            job_id=str(getattr(job, "job_id", "") or ""),
            leg_id=leg_id,
            order_id=str(getattr(job, "order_id", "") or ""),
            order_name=str(getattr(job, "order_name", "") or ""),
            leg_kind=str(getattr(job, "leg_kind", "") or ""),
            service_date=str(getattr(job, "service_date", "") or ""),
            source_depot=str(getattr(job, "source_depot", "") or ""),
            target_depot=str(getattr(job, "target_depot", "") or ""),
            hard_blocker=str(getattr(job, "hard_blocker", "") or ""),
            ok_vehicle_count=ok_count,
            same_depot_ok_count=same_count,
            cross_depot_ok_count=cross_count,
            best_vehicle_id=str(best["vehicle_id"]),
            best_vehicle_home_depot=str(best["vehicle_home_depot"]),
            best_vehicle_type=str(best["vehicle_type"]),
            best_distance_km=best["current_to_service_km"],
            best_drive_minutes=best["estimated_drive_minutes"],
            best_same_depot_vehicle_id=str(best_same["vehicle_id"]),
            best_same_depot_distance_km=best_same["current_to_service_km"],
            best_cross_depot_vehicle_id=str(best_cross["vehicle_id"]),
            best_cross_depot_home_depot=str(best_cross["vehicle_home_depot"]),
            best_cross_depot_distance_km=best_cross["current_to_service_km"],
            option_status=_option_status(job, ok_count, same_count, cross_count),
        ))
    return rows


def job_options_frame(candidates: pd.DataFrame, compatibility: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([row.to_dict() for row in build_job_option_summaries(candidates, compatibility)])
