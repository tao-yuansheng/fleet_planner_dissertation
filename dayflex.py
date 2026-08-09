"""K2 v1: earlier-only day flexibility for depot-controlled deliveries.

A FULL_FLEET delivery leg whose freight is already in our depot (XDOCK shape:
dependency_type PRESTAGED_DELIVERY or REQUIRES_PRIOR_PICKUP) may be served up
to MAX_DAYS_EARLY days before its historical due date, never later. The floor
is the freight-ready date (readiness gate), the raw delivery window start when
one exists, and the planning-window open. Earlier-only means no service-promise
model is needed: reality met the due date, so the plan is never granted freedom
reality lacked (spec docs/superpowers/specs/2026-07-06-k2-dayflex-design.md).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pandas as pd

MAX_DAYS_EARLY = 2
_FLEX_DEP_TYPES = {"PRESTAGED_DELIVERY", "REQUIRES_PRIOR_PICKUP"}


def _to_date(value) -> date | None:
    s = str(value or "")[:10]
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        parsed = pd.to_datetime(value, errors="coerce")
        return None if pd.isna(parsed) else parsed.date()


def day_flex_min(
    flow: str,
    leg_kind: str,
    dependency_type: str,
    service_date: str,
    freight_ready_time: str,
    raw_window_start: str,
    planning_start: date | None,
) -> str:
    """Earliest allowed service day (ISO) for an eligible delivery; "" = pinned."""
    if str(flow) != "FULL_FLEET" or str(leg_kind) != "CUSTOMER_DELIVERY":
        return ""
    if str(dependency_type) not in _FLEX_DEP_TYPES:
        return ""
    due = _to_date(service_date)
    if due is None:
        return ""
    floors = [due - timedelta(days=MAX_DAYS_EARLY)]
    if planning_start is not None:
        floors.append(planning_start)
    ready = _to_date(freight_ready_time)
    if ready is not None:
        floors.append(ready)
    win = _to_date(raw_window_start)
    if win is not None:
        floors.append(win)
    lo = max(floors)
    return lo.isoformat() if lo < due else ""


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        parsed = pd.to_datetime(value, errors="coerce")
        return None if pd.isna(parsed) else parsed.to_pydatetime()


def _iso(dt: datetime) -> str:
    return dt.isoformat(sep=" ")


def shifted_route_job(rjob, cand: dict, day: str):
    """RouteJob variant for serving on ``day`` (< nominal): transplant the window's
    time-of-day onto ``day`` (same receiving hours, earlier day), floored by the
    freight-ready instant on the ready day itself. None = not usable that day."""
    d = date.fromisoformat(day)
    ready = _parse_dt(cand.get("freight_ready_time"))
    if ready is not None and ready.date() > d:
        return None
    es = _parse_dt(rjob.earliest_start)
    lf = _parse_dt(rjob.latest_finish)
    es2 = datetime.combine(d, es.time()) if es is not None else None
    if ready is not None and ready.date() == d:
        es2 = ready if es2 is None else max(es2, ready)
    lf2 = datetime.combine(d, lf.time()) if lf is not None else None
    if es2 is not None and lf2 is not None and es2 >= lf2:
        return None
    return replace(
        rjob,
        earliest_start=_iso(es2) if es2 is not None else "",
        latest_finish=_iso(lf2) if lf2 is not None else "",
    )


def dayflex_stats(selected: pd.DataFrame, candidates: pd.DataFrame) -> dict:
    """Service-impact ledger: how many jobs COULD move, how many DID, how far."""
    if candidates is None or candidates.empty or "day_flex_min" not in candidates.columns:
        return {"eligible": 0, "shifted": 0, "histogram": {0: 0, 1: 0, 2: 0}}
    flex = candidates[candidates["day_flex_min"].astype(str).ne("")]
    due_by_job = dict(zip(flex["job_id"].astype(str),
                          flex["service_date"].astype(str).str[:10]))
    hist = {0: 0, 1: 0, 2: 0}
    shifted = 0
    if selected is not None and not selected.empty:
        for row in selected.itertuples(index=False):
            jid = str(getattr(row, "job_id", ""))
            due = due_by_job.get(jid)
            if not due:
                continue
            served = str(getattr(row, "service_date", ""))[:10]
            try:
                early = (date.fromisoformat(due) - date.fromisoformat(served)).days
            except ValueError:
                continue
            early = max(0, early)
            bucket = min(early, MAX_DAYS_EARLY)
            hist[bucket] = hist.get(bucket, 0) + 1
            if early > 0:
                shifted += 1
    return {"eligible": int(len(flex)), "shifted": int(shifted), "histogram": hist}


def render_stats_md(stats: dict) -> str:
    h = stats.get("histogram", {})
    return "\n".join([
        "",
        "## K2 Day-Flex (earlier-only)",
        f"- day-flex eligible jobs: {stats.get('eligible', 0)}",
        f"- shifted earlier: {stats.get('shifted', 0)}",
        f"- days-early histogram: 0d={h.get(0, 0)}, 1d={h.get(1, 0)}, 2d={h.get(2, 0)}",
        "",
    ])
