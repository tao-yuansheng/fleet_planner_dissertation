"""E6 order-visibility: what the dispatcher may know at a decision epoch.

Collections (PL_EXPORT / LOCAL_COLLECT / FULL_FLEET — and unknown flows,
conservatively) are visible once their commercial order exists:
``timestamp_created <= as_of``. Deliveries (PL_IMPORT / LOCAL_DELIVER) are
visible from 18:00 the evening before their service day: the freight lands at
our depot or a network hub overnight, so booking time is not our knowledge
point. ``origin_timestamp`` is never consulted (51.7% placeholder stamps).

Shuttle-exempt orders are standing scheduled capacity (the vehicle goes to the
address daily regardless of what has been booked and takes what is on the dock)
— but the dock only holds freight that EXISTS. An order booked at 14:00 is not
on the dock for the 09:00 sweep, so the exemption is still creation-gated: it
lifts the delivery-reveal delay and dedicates capacity, it never reveals an
order before ``timestamp_created`` (smoke-9 finding: the old blanket bypass
collected 27 orders hours before they were booked, a clairvoyance violation).

An order with no usable ``timestamp_created`` is visible (we cannot gate on
absent evidence; this errs toward E1 behaviour and callers may count the rows).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

COLLECT_FLOWS = {"PL_EXPORT", "LOCAL_COLLECT", "FULL_FLEET"}
DELIVER_FLOWS = {"PL_IMPORT", "LOCAL_DELIVER"}
DELIVERY_REVEAL_HOUR = 18  # D-1 evening


def visible_order_ids(
    order_meta: pd.DataFrame,
    as_of: datetime,
    exempt_order_ids: set[str] | None = None,
) -> set[str]:
    """Order ids knowable at ``as_of``.

    ``order_meta`` columns: ``order_id``, ``flow``, ``created`` (datetime64,
    NaT allowed), ``service_day`` (datetime64 midnight; the delivery day for
    deliver-flows).
    """
    if order_meta is None or order_meta.empty:
        return set(exempt_order_ids or set())
    m = order_meta
    ts = pd.Timestamp(as_of)
    exempt = set(exempt_order_ids or set())
    is_exempt = m["order_id"].astype(str).isin(exempt)
    deliver = m["flow"].astype(str).isin(DELIVER_FLOWS) & ~is_exempt
    reveal = m["service_day"] - pd.Timedelta(days=1) + pd.Timedelta(hours=DELIVERY_REVEAL_HOUR)
    vis_deliver = deliver & (ts >= reveal)
    # Collection rule for every non-deliver flow AND every exempt order (incl.
    # blank/unknown flows: conservative). The exemption lifts the delivery-
    # reveal delay but NOT the creation gate: standing capacity still cannot
    # collect freight that does not yet exist.
    vis_collect = (~deliver) & (m["created"].isna() | (m["created"] <= ts))
    return set(m.loc[vis_deliver | vis_collect, "order_id"].astype(str))


def build_order_meta(qargo_df: pd.DataFrame, demand_df: pd.DataFrame) -> pd.DataFrame:
    """One row per order: flow (from demand's corrected_flow), tz-naive creation
    stamp and delivery service day (destination_date) from the raw Qargo frame."""
    fcol = "corrected_flow" if "corrected_flow" in demand_df.columns else "flow"
    dm = (demand_df[["order_id", fcol]].astype({"order_id": str})
          .drop_duplicates("order_id").rename(columns={fcol: "flow"}))
    q = qargo_df[["order_id", "timestamp_created", "destination_date"]].copy()
    q["order_id"] = q["order_id"].astype(str)
    q = q.drop_duplicates("order_id")
    q["created"] = (pd.to_datetime(q["timestamp_created"], errors="coerce", utc=True)
                    .dt.tz_localize(None))
    q["service_day"] = pd.to_datetime(q["destination_date"], errors="coerce").dt.normalize()
    meta = dm.merge(q[["order_id", "created", "service_day"]], on="order_id", how="left")
    meta["flow"] = meta["flow"].fillna("").astype(str)
    return meta[["order_id", "flow", "created", "service_day"]]


def shuttle_exempt_order_ids(bins, candidate_df: pd.DataFrame) -> set[str]:
    """Orders riding standing shuttle bins (K1): bin job_ids -> candidate order_ids."""
    if not bins or candidate_df is None or candidate_df.empty:
        return set()
    job_ids: set[str] = set()
    for b in bins:
        job_ids.update(str(j) for j in getattr(b, "job_ids", ()) or ())
    c = candidate_df[["job_id", "order_id"]].astype(str)
    return set(c.loc[c["job_id"].isin(job_ids), "order_id"])
