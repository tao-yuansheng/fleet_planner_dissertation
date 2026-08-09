"""Milestone 9: KPI accounting for the selected plan.

The denominator is explicit and reproducible: every raw order is partitioned into
excluded / ambiguous-manual / in-universe, and assignment is measured against the
in-universe set (and runnable candidate jobs). Phantom deliveries are reported and
expected to be zero. The KPI is defined by the new plan's own accounting, not by
matching old plan labels.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

DISPATCHABLE_FLOWS = {"PL_IMPORT", "PL_EXPORT", "FULL_FLEET", "LOCAL_COLLECT", "LOCAL_DELIVER"}

# Unserved-by-reason bucketing (thesis results). Every real this-window-miss reason
# code — routing rejections (NO_FEASIBLE_ROUTE/TOUR, TIME_WINDOW, EXCESS_WAIT,
# EARLY_ARRIVAL, SHIFT, SLIPPED(n)…) and pass-through hard blockers
# (MASSIVE_UNSUPPORTED, NO_CAPABLE_VEHICLE, BAD_GEOCODE…), i.e. exactly the data
# behind the "Unassigned By Reason (real this-window misses)" block — collapses to
# one of four operator-facing buckets. Kept module-level (dict + function) so it is
# unit-testable in isolation, and any unknown/future code falls to `other` (never
# raises). Precedence: explicit no-vehicle set, then window (explicit set OR any
# code containing "WINDOW"), then slip (any code containing "SLIP"), else `other`.
UNSERVED_BUCKETS = ("no_feasible_vehicle", "window_unreachable", "slipped", "other")

_NO_FEASIBLE_VEHICLE_REASONS = frozenset({
    "NO_FEASIBLE_ROUTE", "NO_FEASIBLE_TOUR", "NO_CAPABLE_VEHICLE", "NO_RESOURCES",
    "CAPACITY", "CAPACITY_OVERFLOW", "MASSIVE_UNSUPPORTED",
})
_WINDOW_UNREACHABLE_REASONS = frozenset({
    "TIME_WINDOW", "EARLY_ARRIVAL", "EXCESS_WAIT", "SHIFT",
})


def bucket_for_reason(reason) -> str:
    """Map one raw unassigned/rejected reason code to a bucket in UNSERVED_BUCKETS.
    Unknown or empty codes (incl. BAD_GEOCODE) fall to `other`; never raises."""
    r = str(reason or "").strip().upper()
    if r in _NO_FEASIBLE_VEHICLE_REASONS:
        return "no_feasible_vehicle"
    if r in _WINDOW_UNREACHABLE_REASONS or "WINDOW" in r:
        return "window_unreachable"
    if "SLIP" in r:
        return "slipped"
    return "other"


def bucket_unserved(reason_counts) -> dict:
    """Collapse a reason->count mapping (e.g. KpiReport.unassigned_by_reason) into
    bucket->count over the four fixed UNSERVED_BUCKETS. Every bucket is present (0
    when empty) so the table is stable, and the buckets partition the input — their
    counts always sum to the input total."""
    out = {b: 0 for b in UNSERVED_BUCKETS}
    for reason, count in (reason_counts or {}).items():
        out[bucket_for_reason(reason)] += int(count)
    return out


@dataclass
class KpiReport:
    window_start: str
    window_end: str
    raw_orders: int
    excluded_total: int
    excluded_by_reason: dict
    ambiguous_manual: int
    in_universe_orders: int
    runnable_candidates: int
    assigned_jobs: int
    assigned_orders: int
    unassigned_by_reason: dict
    planned_km: float
    cross_depot_km: float
    vehicle_days_daily: int
    vehicle_days_tour: int
    same_day_jobs: int
    multiday_jobs: int
    phantom_deliveries: int
    # T1: fixed nightly B37 trunk service (double-deck 52 pal), reported separately
    # from the optimizer's own planned_km -- see the "Reporting" section of
    # docs/superpowers/specs/2026-07-04-night-trunk-service-design.md. Zero when
    # the trunk is disabled/absent (backward compatible).
    trunk_km: float = 0.0
    trunk_trips: int = 0
    trunk_shortfall_nights: int = 0
    # Order-level accounting closure (2026-07-17): the Assignment section must SUM
    # to the in-universe denominator — fully + partial + wholly-unassigned = universe.
    # Option-group losers (the XC/XD pair when DIRECT won, the DIR when XDOCK won)
    # are ALTERNATIVES, not misses: pre-fix they silently demoted their order out of
    # `assigned_orders` while appearing in no unassigned table (run_pinned: 996
    # universe vs 943 "assigned" vs 7 unassigned = a 46-order hole).
    partial_orders: int = 0            # >=1 job placed, an IN-WINDOW required job unplaced
    zero_assigned_orders: int = 0      # in-universe, nothing placed at all
    option_alternative_jobs: int = 0   # runnable jobs in LOSING option groups
    handover_orders: int = 0           # served THIS window but with a beyond-window leg deferred
    next_window_deferred_jobs: int = 0        # rejected DUE_BEYOND_WINDOW (next window's obligation)
    option_alternative_unassigned_jobs: int = 0  # rejected legs of the LOSING option group (order served the other way)
    in_universe_collection: int = 0      # orders we COLLECT (== the service-summary denominator)
    in_universe_delivery_only: int = 0   # orders we only DELIVER (network import / local deliver)

    @property
    def order_assignment_rate(self) -> float:
        return (self.assigned_orders / self.in_universe_orders * 100.0) if self.in_universe_orders else 0.0

    @property
    def within_window_pct(self) -> float:
        """Share of in-universe orders whose IN-WINDOW obligation is fully met.
        Beyond-window tails are handovers, not misses — so this is the honest
        completion rate for the window the run actually planned."""
        denom = self.assigned_orders + self.partial_orders + self.zero_assigned_orders
        return (self.assigned_orders / denom * 100.0) if denom else 0.0

    @property
    def job_assignment_rate(self) -> float:
        return (self.assigned_jobs / self.runnable_candidates * 100.0) if self.runnable_candidates else 0.0


def _counter(series) -> dict:
    return dict(Counter(str(v) for v in series))


def relabel_beyond_window(rejected, candidate_df, window_end_iso: str) -> list:
    """A rejected job whose earliest allowed service opens AFTER the window's last
    day was never THIS window's obligation: its freight is collected/staged and
    hands over to the next window (handover.json). Its non-placement is an
    accounting-scope fact, not a physical failure — relabel to DUE_BEYOND_WINDOW
    so it never masquerades as NO_FEASIBLE_ROUTE (run_pinned: 7 Jan-14/15 delivery
    tails in a Jan-12..13 window). REPAIRED_DIRECT is superseded-leg bookkeeping
    (build_kpi keys on it) and is never touched. Called once in write_reports, so
    every emitter (manifest, unassigned_jobs.csv, KPI, universe report) agrees."""
    if not rejected or candidate_df is None or candidate_df.empty:
        return list(rejected or [])
    opens: dict = {}
    es = (candidate_df["earliest_start"] if "earliest_start" in candidate_df.columns
          else pd.Series("", index=candidate_df.index))
    sd = (candidate_df["service_date"] if "service_date" in candidate_df.columns
          else pd.Series("", index=candidate_df.index))
    for jid, e, s in zip(candidate_df["job_id"].astype(str), es, sd):
        opens[jid] = (str(e or "") or str(s or ""))[:10]
    end_day = str(window_end_iso)[:10]
    out = []
    for r in rejected:
        reason = str(getattr(r, "reason", ""))
        day = opens.get(str(getattr(r, "job_id", "")), "")
        if reason != "REPAIRED_DIRECT" and day and day > end_day:
            try:
                from dataclasses import replace as _dc_replace
                r = _dc_replace(r, reason="DUE_BEYOND_WINDOW")
            except TypeError:
                from types import SimpleNamespace
                r = SimpleNamespace(job_id=getattr(r, "job_id", ""), reason="DUE_BEYOND_WINDOW")
        out.append(r)
    return out


def build_kpi(
    window_start: str,
    window_end: str,
    demand_df: pd.DataFrame,
    candidate_df: pd.DataFrame,
    selected_df: pd.DataFrame,
    rejected,
    tours,
    planned_km: float,
    cross_depot_km: float,
    phantom_deliveries: int,
    trunk_km: float = 0.0,
    trunk_trips: int = 0,
    trunk_shortfall_nights: int = 0,
    handover_order_ids: set | None = None,
) -> KpiReport:
    raw_orders = 0 if demand_df is None else len(demand_df)
    excluded_by_reason: dict = {}
    ambiguous = 0
    in_universe = 0
    in_universe_collection = 0
    in_universe_delivery_only = 0
    universe_ids: set = set()
    if demand_df is not None and not demand_df.empty:
        excl_reason = demand_df["exclusion_reason"].fillna("").astype(str)
        excl_mask = excl_reason.ne("")
        excluded_by_reason = _counter(demand_df.loc[excl_mask, "exclusion_reason"])
        rest = demand_df.loc[~excl_mask]
        dispatchable = rest["corrected_flow"].astype(str).isin(DISPATCHABLE_FLOWS)
        in_universe = int(dispatchable.sum())
        # collection-side vs delivery-only split (reconciles the KPI's in-universe total
        # with the service summary's collection-order count): we COLLECT FULL_FLEET /
        # PL_EXPORT / LOCAL_COLLECT; we only DELIVER network-import / LOCAL_DELIVER.
        _disp_flows = rest.loc[dispatchable, "corrected_flow"].astype(str)
        in_universe_collection = int(_disp_flows.isin({"FULL_FLEET", "PL_EXPORT", "LOCAL_COLLECT"}).sum())
        in_universe_delivery_only = in_universe - in_universe_collection
        if "order_id" in rest.columns:
            universe_ids = set(rest.loc[dispatchable, "order_id"].astype(str))
        # ambiguous/manual = unflagged-but-no-flow, plus those explicitly flagged
        # AMBIGUOUS/MANUAL (a cross-cut of the excluded set, shown for visibility)
        ambiguous = int((~dispatchable).sum()) + int(
            excl_reason.str.contains("AMBIG|MANUAL", case=False, regex=True).sum())

    runnable = 0
    blocked_by_reason: dict = {}
    if candidate_df is not None and not candidate_df.empty:
        blockers = candidate_df["hard_blocker"].fillna("").astype(str)
        runnable = int(blockers.eq("").sum())
        # BEFORE_PLANNING_START is satisfied HISTORY, not a miss (2026-07-16, third
        # emitter swept: manifest.build_unassigned and the universe report already
        # exclude/annotate it — the KPI's unassigned table must agree): the
        # collection happened before the window and the order was reframed as a
        # prestaged delivery. It stays visible in the universe report's receipt.
        blocked_by_reason = dict(Counter(
            v for v in blockers if v and v != "BEFORE_PLANNING_START"))

    # TOUR_OVERNIGHT rows are synthetic sleep points (MULTIDAY_MIDLEG_OVERNIGHT),
    # not jobs — they must not inflate the assigned count.
    if selected_df is not None and not selected_df.empty and "leg_kind" in selected_df.columns:
        selected_df = selected_df[selected_df["leg_kind"].astype(str) != "TOUR_OVERNIGHT"]
    assigned_jobs = 0 if selected_df is None else len(selected_df)
    assigned_orders = 0
    daily_vehicle_days = 0
    selected_ids = set(selected_df["job_id"].astype(str)) if (selected_df is not None and not selected_df.empty) else set()
    if selected_df is not None and not selected_df.empty:
        route_ids = selected_df["route_id"].astype(str)
        daily_vehicle_days = int(route_ids[route_ids.str.startswith("ROUTE:")].nunique())
    # legs superseded by the stranded-backhaul repair are replaced by a selected
    # DIRECT carry — they must not gate their order's completion
    superseded = {str(getattr(rj, "job_id", "")) for rj in (rejected or [])
                  if str(getattr(rj, "reason", "")) == "REPAIRED_DIRECT"}

    def _parent(v) -> str:
        return str(v).split("#", 1)[0]

    # Order-level closure (2026-07-17): fully + partial + wholly-unassigned must
    # sum to in-universe. Two pre-fix holes: (a) option-group LOSERS (candidate
    # alternatives the search rightly did not pick) demoted fully-served orders;
    # (b) split parts ('uuid#S1') were counted per PART against a per-ORDER
    # denominator. Completeness is per PARENT: every no-option job selected AND,
    # per option_set, the CHOSEN group fully selected (a set with nothing chosen
    # is a genuine miss).
    partial_orders = 0
    zero_assigned = 0
    handover_orders = 0
    option_alt_jobs = 0
    option_alt_ids: set = set()   # job_ids of LOSING option groups (served the other way)
    win_end = str(window_end)[:10] if window_end else ""
    # Orders COLLECTED this window whose freight is staged for next-window delivery
    # (handover.staged_freight). Their delivery is deferred, so it must not gate this
    # window's completeness (2026-07-19 fix).
    staged_ids: set = {str(x).split("#", 1)[0] for x in (handover_order_ids or ())}
    sel_parents: set = set()
    if selected_df is not None and not selected_df.empty and "order_id" in selected_df.columns:
        sel_parents = {_parent(v) for v in selected_df["order_id"]}
    if candidate_df is not None and not candidate_df.empty:
        runnable_df = candidate_df[candidate_df["hard_blocker"].fillna("").astype(str).eq("")]
        have_opts = {"option_set", "option_group"} <= set(runnable_df.columns)
        has_sd = "service_date" in runnable_df.columns
        complete_parents: set = set()
        incomplete_parents: set = set()
        handover_parents: set = set()
        for parent, grp in runnable_df.groupby(runnable_df["order_id"].map(_parent)):
            # A leg dated AFTER the window end is NEXT window's obligation (a staged
            # handover), NOT a this-window miss — exclude it from completeness so an
            # order whose only shortfall is a beyond-window tail counts as served for
            # THIS window (2026-07-18: was over-counting these as partial — 50 partial
            # vs 4 actual unplaced in-window legs). Track them as handovers instead.
            sd = (grp["service_date"].astype(str).str[:10] if has_sd
                  else pd.Series("", index=grp.index))
            beyond = (sd > win_end) if win_end and has_sd else pd.Series(False, index=grp.index)
            # A staged-freight order was COLLECTED this window; its delivery is deferred
            # to the next window (handover). Exclude its DELIVERY leg from this window's
            # completeness — same as a beyond-window tail — so collect-now/deliver-next
            # FULL_FLEET orders (nominal delivery date in-window) are not miscounted as
            # partial (2026-07-19: 183/184 heaviest-week "partials" were staged handovers).
            if str(parent) in staged_ids and "leg_kind" in grp.columns:
                beyond = beyond | grp["leg_kind"].astype(str).eq("CUSTOMER_DELIVERY")
            inw = grp[~beyond]
            if bool(beyond.any()):
                handover_parents.add(str(parent))
            jids = inw["job_id"].astype(str)
            osets = (inw["option_set"].fillna("").astype(str) if have_opts
                     else pd.Series("", index=inw.index))
            ogrps = (inw["option_group"].fillna("").astype(str) if have_opts
                     else osets)
            complete = True
            required = set(jids[osets.eq("")]) - superseded
            if not required <= selected_ids:
                complete = False
            for s in (x for x in osets.unique() if x):
                groups = {g: set(jids[osets.eq(s) & ogrps.eq(g)])
                          for g in ogrps[osets.eq(s)].unique()}
                chosen = {g for g, js in groups.items() if js & selected_ids}
                if chosen:
                    for g, js in groups.items():
                        if g in chosen and not (js - superseded) <= selected_ids:
                            complete = False
                        elif g not in chosen:
                            option_alt_jobs += len(js)
                            option_alt_ids |= js
                elif set().union(*groups.values()) - superseded:
                    complete = False   # nothing picked from the set = a real miss
            (complete_parents if complete else incomplete_parents).add(str(parent))
        if universe_ids:
            complete_parents &= universe_ids
            incomplete_parents &= universe_ids
            handover_parents &= universe_ids
        assigned_orders = len(complete_parents)
        partial_orders = len(incomplete_parents & sel_parents)
        zero_assigned = len(incomplete_parents - sel_parents)
        handover_orders = len(handover_parents & complete_parents)
        if universe_ids:
            zero_assigned += len(universe_ids - complete_parents - incomplete_parents)
    elif selected_df is not None and not selected_df.empty:
        assigned_orders = int(selected_df["order_id"].nunique())

    tour_job_ids = {j.job_id for t in (tours or []) for j in t.jobs}
    tour_vehicle_days = sum(int(getattr(t, "days", 0)) for t in (tours or []))
    multiday_jobs = len(selected_ids & tour_job_ids)
    same_day_jobs = assigned_jobs - multiday_jobs

    # Honest unassigned accounting (2026-07-23): with endogenous DIRECT/XDOCK both
    # option groups flow in, so a rejected leg is usually NOT a miss — it is either
    # the LOSING option alternative (its order is served the other way) or a
    # DUE_BEYOND_WINDOW leg (next window's obligation). Only the residual are real
    # this-window misses, which must reconcile with within-window completeness.
    def _cat(rj) -> str:
        jid = str(getattr(rj, "job_id", ""))
        if jid in selected_ids:
            return "stale"
        if str(getattr(rj, "reason", "")) == "DUE_BEYOND_WINDOW":
            return "defer"
        return "alt" if (jid in option_alt_ids or jid in superseded) else "miss"

    rej = list(rejected or [])
    real_misses = [rj for rj in rej if _cat(rj) == "miss"]
    next_window_deferred = sum(1 for rj in rej if _cat(rj) == "defer")
    option_alt_unassigned = sum(1 for rj in rej if _cat(rj) == "alt")
    unassigned_by_reason = dict(Counter(str(getattr(rj, "reason", "")) for rj in real_misses)
                                + Counter(blocked_by_reason))

    return KpiReport(
        window_start=str(window_start), window_end=str(window_end),
        raw_orders=raw_orders,
        excluded_total=int(sum(excluded_by_reason.values())),
        excluded_by_reason=excluded_by_reason,
        ambiguous_manual=ambiguous,
        in_universe_orders=in_universe,
        in_universe_collection=int(in_universe_collection),
        in_universe_delivery_only=int(in_universe_delivery_only),
        runnable_candidates=runnable,
        assigned_jobs=assigned_jobs,
        assigned_orders=assigned_orders,
        unassigned_by_reason=unassigned_by_reason,
        next_window_deferred_jobs=int(next_window_deferred),
        option_alternative_unassigned_jobs=int(option_alt_unassigned),
        planned_km=float(planned_km),
        cross_depot_km=float(cross_depot_km),
        vehicle_days_daily=daily_vehicle_days,
        vehicle_days_tour=tour_vehicle_days,
        same_day_jobs=same_day_jobs,
        multiday_jobs=multiday_jobs,
        phantom_deliveries=int(phantom_deliveries),
        trunk_km=float(trunk_km),
        trunk_trips=int(trunk_trips),
        trunk_shortfall_nights=int(trunk_shortfall_nights),
        partial_orders=int(partial_orders),
        zero_assigned_orders=int(zero_assigned),
        option_alternative_jobs=int(option_alt_jobs),
        handover_orders=int(handover_orders),
    )


def delivery_timeliness_stats(route_stops) -> dict | None:
    """Order-level lateness against explicit slots and deadlines.

    Split consignments count once at their final delivery arrival (the maximum
    lateness across their parts), matching the historical per-order denominator.
    """
    if route_stops is None or len(route_stops) == 0 or "minutes_late" not in route_stops.columns:
        return None
    d = route_stops[
        route_stops["stop_type"].astype(str) == "customer_delivery"
    ].copy()
    if d.empty:
        return None
    if "order_id" in d.columns:
        d["_delivery_unit"] = d["order_id"].astype(str).str.split("#S", n=1).str[0]
    else:
        d["_delivery_unit"] = d.index.astype(str)

    date_only_excluded = 0
    if "window_hardness" in d.columns:
        is_explicit = d["window_hardness"].astype(str).isin({"hard_slot", "soft_deadline"})
        date_only_excluded = int(
            d.loc[d["window_hardness"].astype(str).eq("date_only"), "_delivery_unit"].nunique())
        d = d.loc[is_explicit].copy()
    d["_minutes_late"] = pd.to_numeric(d["minutes_late"], errors="coerce")
    ml = d.groupby("_delivery_unit")["_minutes_late"].max().dropna()
    if ml.empty:
        return None
    n = len(ml)
    late = int((ml > 0).sum())
    ontime = n - late
    lt = ml[ml > 0]
    return {
        "delivery_obligations": int(route_stops.loc[
            route_stops["stop_type"].astype(str) == "customer_delivery"
        ].assign(
            _unit=lambda x: (
                x["order_id"].astype(str).str.split("#S", n=1).str[0]
                if "order_id" in x.columns else x.index.astype(str))
        )["_unit"].nunique()),
        "explicit_windows": int(n),
        "date_only_excluded": date_only_excluded,
        "missing_actual_timestamps": int(
            d.groupby("_delivery_unit")["_minutes_late"].max().isna().sum()),
        "on_time": int(ontime),
        "late": int(late),
        "average_late_minutes": round(float(lt.mean()), 1) if late else 0.0,
        "median_late_minutes": round(float(lt.median()), 1) if late else 0.0,
        "p90_late_minutes": round(float(lt.quantile(0.9)), 1) if late else 0.0,
        "maximum_late_minutes": round(float(lt.max()), 1) if late else 0.0,
    }


def delivery_timeliness_md(route_stops) -> str:
    """Intra-day delivery lateness against explicit slots and deadlines only."""
    stats = delivery_timeliness_stats(route_stops)
    if stats is None:
        return ""
    n = stats["explicit_windows"]
    late = stats["late"]
    ontime = stats["on_time"]
    lines = [
        "## Delivery timeliness (intra-day)",
        "",
        f"- deliveries with an explicit window/deadline: {n}",
        f"- date-only placeholders excluded: {stats['date_only_excluded']}",
        f"- on-time (by the deadline): {ontime} ({100.0 * ontime / n:.1f}%)",
        f"- late: {late} ({100.0 * late / n:.1f}%)",
    ]
    if late:
        lines.append(
            f"- late minutes — avg {stats['average_late_minutes']:.0f}, "
            f"median {stats['median_late_minutes']:.0f}, "
            f"p90 {stats['p90_late_minutes']:.0f}, "
            f"max {stats['maximum_late_minutes']:.0f}")
    lines.append("")
    return "\n".join(lines)


def _block(title: str, counts: dict) -> list[str]:
    lines = [f"## {title}", "", "```text"]
    lines += [f"{reason:<28} {count}" for reason, count in sorted(counts.items(), key=lambda kv: -kv[1])] or ["(none)"]
    lines += ["```", ""]
    return lines


def kpi_summary_md(r: KpiReport) -> str:
    lines = [
        "# KPI Summary",
        "",
        f"- window: {r.window_start} to {r.window_end}",
        "",
        "## Demand Accounting (denominator)",
        "",
        f"- raw orders: {r.raw_orders}",
        f"- excluded: {r.excluded_total}",
        f"- ambiguous / manual (cross-cut, incl. flagged excludes): {r.ambiguous_manual}",
        f"- in-universe orders (planning denominator): {r.in_universe_orders}"
        + (f" = {r.in_universe_collection} collection-side (we collect; the 01_service_summary "
           f"denominator) + {r.in_universe_delivery_only} delivery-only (we only deliver)"
           if r.in_universe_collection or r.in_universe_delivery_only else ""),
        f"- runnable candidate jobs: {r.runnable_candidates}",
        "",
        "## Assignment",
        "",
        # HEADLINE: the honest completion rate is against THIS window's obligation —
        # beyond-window tails are handovers to the next window, not misses.
        f"- **within-window completeness: {r.within_window_pct:.1f}%** "
        f"({r.assigned_orders} of {r.assigned_orders + r.partial_orders + r.zero_assigned_orders} "
        f"in-window obligations met)",
        f"- fully served this window: {r.assigned_orders}"
        + (f" (incl. {r.handover_orders} collected with delivery staged for the next window)"
           if r.handover_orders else ""),
        f"- partially served (an IN-WINDOW leg unplaced): {r.partial_orders}",
        f"- wholly unassigned orders: {r.zero_assigned_orders}",
        (lambda tot: f"- identity: {r.assigned_orders} fully + {r.partial_orders} partial + "
                     f"{r.zero_assigned_orders} unassigned = {tot} vs in-universe "
                     f"{r.in_universe_orders} [{'OK' if tot == r.in_universe_orders else 'MISMATCH'}]")(
            r.assigned_orders + r.partial_orders + r.zero_assigned_orders),
        f"- assigned candidate jobs: {r.assigned_jobs} ({r.job_assignment_rate:.1f}% of runnable"
        + (f"; {r.option_alternative_jobs} runnable jobs were losing option alternatives, not misses"
           if r.option_alternative_jobs else "") + ")",
        f"- same-day jobs: {r.same_day_jobs}",
        f"- multiday (tour) jobs: {r.multiday_jobs}",
    ]
    lines += [
        "",
        "## Resources & Distance",
        "",
        f"- vehicle-days: {r.vehicle_days_daily} daily + {r.vehicle_days_tour} tour",
        f"- committed route km (excludes separately scheduled trunk): {r.planned_km:,.0f}",
        f"- cross-depot repositioning km: {r.cross_depot_km:,.0f}",
        "",
        f"## Phantom deliveries (expected 0): {r.phantom_deliveries}",
        "",
    ]
    lines += _block("Excluded By Reason", r.excluded_by_reason)
    # Not misses (endogenous DIRECT/XDOCK, 2026-07-23): the losing option group's
    # legs (order served the other way) and next-window (DUE_BEYOND_WINDOW) legs are
    # NOT this-window failures. Reported separately so "Unassigned By Reason" carries
    # only real this-window misses and reconciles with within-window completeness.
    if r.option_alternative_unassigned_jobs or r.next_window_deferred_jobs:
        lines += [
            "## Not misses (accounting scope)",
            "",
            f"- losing option alternatives (order served the other mode): {r.option_alternative_unassigned_jobs}",
            f"- deferred to next window (DUE_BEYOND_WINDOW): {r.next_window_deferred_jobs}",
            "",
        ]
    lines += _block("Unassigned By Reason (real this-window misses)", r.unassigned_by_reason)
    # Unserved-by-bucket (thesis results): the SAME real-this-window-miss reasons
    # collapsed to four operator buckets, with an identity check that they partition
    # the total. The rejection records the KPI receives carry no per-day date (only
    # job_id + reason), so only totals are emitted here — a per-day table would need
    # a date field on the reason data, which we do NOT invent (see note below).
    _buckets = bucket_unserved(r.unassigned_by_reason)
    _btot = sum(_buckets.values())
    _real = sum(int(v) for v in r.unassigned_by_reason.values())
    lines += [
        "## Unserved by bucket",
        "",
        f"- no feasible vehicle: {_buckets['no_feasible_vehicle']}",
        f"- window unreachable: {_buckets['window_unreachable']}",
        f"- slipped: {_buckets['slipped']}",
        f"- other: {_buckets['other']}",
        f"- identity: {_buckets['no_feasible_vehicle']} + {_buckets['window_unreachable']} + "
        f"{_buckets['slipped']} + {_buckets['other']} = {_btot} vs real this-window misses "
        f"{_real} [{'OK' if _btot == _real else 'MISMATCH'}]",
        # per-day breakdown deliberately omitted: the rejected reason records carry
        # no date field (only job_id + reason), so a day -> 4-bucket table would
        # require adding one, which is out of scope here.
        "- per-day breakdown unavailable: rejected reason records carry no date field.",
        "",
    ]
    if r.trunk_trips:
        combined_km = r.planned_km + r.trunk_km
        # "scheduled": a standing nightly timetable, not demand-driven routes
        # (previously mislabeled "Fixed ... double-deck 52 pal" — the deck was
        # double-deck restored 2026-07-21; read the live config, never bake it)
        from freight_planner.config import TRUNK_DECK_PALLETS as _deck
        lines += [
            f"## Scheduled nightly hub trunk (double-deck {_deck:.0f} pal/trip)",
            "",
            f"- trunk trips: {r.trunk_trips}",
            f"- trunk km: {r.trunk_km:,.0f}",
            f"- shortfall nights: {r.trunk_shortfall_nights}",
            f"- combined: plan {r.planned_km:,.0f} + trunk {r.trunk_km:,.0f} = {combined_km:,.0f} km",
            "",
        ]
    return "\n".join(lines)
