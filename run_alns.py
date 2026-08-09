"""Milestone 5 CLI: improve the greedy route seed with coverage-aware ALNS.

Builds the greedy seed, then runs ALNS over the daily plan with a unified state:
assigned routes plus repairable rejected jobs. Coverage is allowed to increase;
hard blockers and freight dependencies remain fixed.

E6 rolling: the window solve is exposed as ``build_window_inputs`` /
``solve_window`` / ``emit_outputs`` so ``run_rolling`` can invoke one restricted
solve per decision epoch and emit a merged plan once. ``main()`` recomposes the
three exactly as before; with no rolling hooks set the behaviour is bit-identical.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from types import SimpleNamespace

from freight_planner import config as _fp_cfg
from freight_planner import geocode, route_costs
from freight_planner.alns import _as_trips, improve_existing_solution, improve_route_seed
from freight_planner.build_phase0 import _load_cache, _load_qargo, _parse_date
from freight_planner.catchment import build_vehicle_catchment, job_distance_km
from freight_planner.compatibility import vehicle_job_compatibility_frame
from freight_planner.cross_depot import cross_depot_report, cross_depot_report_md
from freight_planner.date_basis import VALID_BASIS, align_demand_to_legs, filter_demand_by_basis, filter_legs_by_basis
from freight_planner.dayflex import dayflex_stats, render_stats_md
from freight_planner.demand import FORWARD_STRUCTURAL, RESPONSIBILITY_MODES, build_demand_records
from freight_planner.jobs import candidate_jobs_frame
from freight_planner.legs import build_movement_leg_records
from freight_planner.osrm_setup import warm_osrm_for_run
from freight_planner.options_resolver import (
    hub_drop_choices_md,
    resolve_hub_drop,
)
from freight_planner.option_report import (
    endogenous_option_choices,
    endogenous_option_choices_md,
)
from freight_planner.output_layout import flat_window_label, run_dirs, window_label, write_run_manifest
from freight_planner.paths import DEFAULT_ENRICHED, DEFAULT_OUT_DIR, DEFAULT_POSTCODE_CACHE
from freight_planner.plan_full import emit_plan_full
from freight_planner.ledger import (
    drop_freightless_tours,
    drop_orphan_deliveries,
    drop_superseded_option_legs,
    selected_option_conflicts,
)
from freight_planner.plan_schema import plan_ledger_violations, selected_plan_export_frame, selected_plan_frame
from freight_planner.plan_validation import temporal_violations
from freight_planner.progress import RunLog
from freight_planner.manifest import (
    build_route_stops,
    clip_route_stops_to_window,
    geom_route_drive_totals,
    geom_route_totals,
)
from freight_planner.reports import write_reports
from freight_planner.runsheets import build_runsheets_html
from freight_planner.state import build_initial_freight_states
from freight_planner.handover import (
    build_handover, load_handover, save_handover,
    apply_exclusion, apply_availability, staged_depot_map,
)
from freight_planner.tour_plan import run_multiday_seed_plan
from freight_planner.tour_attach import rebuild_pruned_tour_records
from freight_planner.route_seed import _job_coords, _route_vehicle, rebuild_daily_routes_after_drop
from freight_planner.trunk import trunk_log_summary
from freight_planner.vehicle_cost import out_of_area_penalty_km
from freight_planner.vehicles import vehicle_states_frame


def _env_toggles() -> dict[str, str]:
    """Experiment env toggles present at run time — recorded in the manifest for
    provenance (Ch.5 campaign: every results row must be reconstructable)."""
    return {k: v for k, v in sorted(os.environ.items())
            if k.startswith("FP_ALNS") or k == "FREIGHT_FUEL_UNIFORM"}


def _imp_score(imp) -> tuple[int, float, int, int]:
    return (
        int(imp.served_after),
        -float(imp.km_after),
        int(imp.inserted_jobs),
        -int(imp.accepted_moves),
    )


def _rebuild_tours_after_final_drops(tours, records, vehicle_df):
    """Drop empty tour models and rebuild geometry for partially-pruned tours."""
    by_route: dict[str, list] = {}
    non_tour: list = []
    for record in records:
        rid = str(getattr(record, "route_id", ""))
        if rid.startswith("TOUR:"):
            by_route.setdefault(rid, []).append(record)
        else:
            non_tour.append(record)
    vrows = {str(getattr(row, "vehicle_id", "")): row
             for row in vehicle_df.itertuples(index=False)}
    rebuilt_tours: list = []
    rebuilt_records: list = []
    rebuilt_ids: set[str] = set()
    known_ids: set[str] = set()
    for ta in tours or []:
        rid = f"TOUR:{ta.vehicle_id}:{ta.start_date}"
        known_ids.add(rid)
        route_records = by_route.get(rid, [])
        if not route_records:
            continue
        vrow = vrows.get(str(ta.vehicle_id))
        if vrow is None:
            raise ValueError(f"tour vehicle {ta.vehicle_id} missing from vehicle master")
        original_count = len(ta.jobs)
        rebuilt_ta, route_records = rebuild_pruned_tour_records(
            ta, route_records, _route_vehicle(vrow, str(ta.start_date)[:10]))
        if rebuilt_ta is None:
            continue
        if len(rebuilt_ta.jobs) != original_count:
            rebuilt_ids.add(rid)
        rebuilt_tours.append(rebuilt_ta)
        rebuilt_records.extend(route_records)
    for rid, route_records in by_route.items():
        if rid not in known_ids:
            rebuilt_records.extend(sorted(route_records, key=lambda r: int(r.sequence)))
    return non_tour + rebuilt_records, rebuilt_tours, rebuilt_ids


def _tour_route_totals(tours) -> dict[str, float]:
    return {
        f"TOUR:{ta.vehicle_id}:{ta.start_date}": float(ta.evaluation.total_km)
        for ta in tours
    }


def _final_km_objective_by_type(route_totals: dict, fleet_types: dict[str, str]) -> dict[str, float]:
    """Per-type final objective km (RESULTS_OUTLINE.md §6.3b refinement,
    2026-07-24), on the SAME evaluator basis as ``seed_by_type``'s
    ``km_road`` (both are ``evaluate_day().total_km`` on the daily, non-tour
    solution, pre-commit-drop, no phantom) -- so the two are genuinely
    subtractable for a same-basis ALNS km delta, unlike seed_km_objective vs
    final_km_physical (a different, post-drop, geometry-reconciled basis).

    ``route_totals`` (``imp.route_totals``) carries both day-level
    ``"ROUTE:vid:day"`` entries and their per-trip ``"...#T{n}"``
    sub-entries; only the day-level entries are summed here, else a route's
    own trips would double-count against its day total."""
    out: dict[str, float] = {}
    for key, v in (route_totals or {}).items():
        if "#T" in key or not key.startswith("ROUTE:"):
            continue
        vid = key.split(":", 2)[1] if key.count(":") >= 2 else ""
        vt = fleet_types.get(vid, "")
        if vt:
            out[vt] = out.get(vt, 0.0) + float(v)
    return out


def build_validation_metrics(imp, selected_df: pd.DataFrame, trunk=None,
                             committed_km: float | None = None) -> dict:
    """Seed->ALNS deltas + planned vehicle-days for the single-day validation viz.

    ``seed_km``/``alns_km`` and ``*_cost`` are OBJECTIVE (search-space) values off
    the improvement object: they show what the optimizer did, so with ``moves == 0``
    (immediate convergence) ``alns_km`` equals ``seed_km`` by construction, and they
    over-count option-set alternatives + the evaluator's return residual. They are
    NOT the physical plan distance. ``committed_km`` is that physical distance -- the
    committed route_stops geometry, identical to the KPI ``planned km`` and every
    other output -- and is the number to validate against telematics.

    ``served_before``/``served_after`` are the served-unit counts at the same two
    points, so a large seed->ALNS km jump can be read correctly: on a rolling/dynamic
    run the seed is priced at the START of the day (a single anchor epoch, well short
    of the day's full universe), while the final ALNS/repair pass has inserted
    everything the day's epochs + repair could place -- most of a km jump is usually
    "serving substantially more of the universe," not route bloat. (For ``run_rolling``,
    both ``cost_before`` and ``served_before`` are the FIRST anchor epoch's true
    values, not the finalize stage's trivial 0-iteration re-price -- see
    ``run_dynamic_loop``'s ``first_cost_before``/``first_served_before``, audit #8's
    same bug class fixed 2026-07-28.)

    ``planned_vehicle_days`` is distinct (vehicle, day) in the plan. ``trunk`` is the
    T1 fixed nightly trunk service's TrunkPlan (km/trips/shortfalls as a separate
    fixed-service line, never folded into the optimizer's own planned_km).
    """
    cols = set(selected_df.columns) if selected_df is not None else set()
    if selected_df is not None and not selected_df.empty and {"vehicle_id", "service_date"} <= cols:
        veh_days = int(selected_df.drop_duplicates(["vehicle_id", "service_date"]).shape[0])
    else:
        veh_days = 0
    out = {
        # Physical committed plan distance (route_stops geometry) == KPI planned km ==
        # every other output. THE number to validate against telematics.
        "committed_km": round(float(committed_km), 1) if committed_km is not None else None,
        # Optimizer objective (search space): what the ALNS ranks on. Equal when
        # moves == 0; over-counts option-set alternatives + return residual, so NOT
        # the plan distance. Kept for the seed->ALNS improvement view.
        "seed_km": round(float(imp.km_before), 1),
        "alns_km": round(float(imp.km_after), 1),
        "seed_cost": round(float(imp.cost_before), 2),
        "alns_cost": round(float(imp.cost_after), 2),
        # Served-unit counts alongside the km/cost deltas above (2026-07-28): a large
        # seed_km -> alns_km jump is often the seed leaving far more of the universe
        # unassigned, not route bloat -- these give that context so the km delta isn't
        # read as pure inflation.
        "served_before": int(imp.served_before),
        "served_after": int(imp.served_after),
        "moves": int(imp.accepted_moves),
        "planned_vehicle_days": veh_days,
        "trunk_km": round(float(trunk.total_km), 1) if trunk is not None else 0.0,
        "trunk_trips": int(trunk.total_trips) if trunk is not None else 0,
        "trunk_shortfall_nights": len(trunk.shortfalls) if trunk is not None else 0,
    }
    if committed_km is None:
        out.pop("committed_km")
    return out


def _summary(
    start: date,
    end: date,
    seed,
    served_before: int,
    served_after: int,
    km_before: float,
    km_after: float,
    accepted_moves: int,
    repaired_jobs: int,
    remaining_rejected: int,
    tour_km: float,
    restarts: int,
    committed_km: float | None = None,
) -> str:
    saved = km_before - km_after
    pct = (saved / km_before * 100.0) if km_before else 0.0
    tour_jobs = sum(len(ta.jobs) for ta in seed.tours)
    selected_total = served_after + tour_jobs
    # The plan DISTANCE reported here is the committed route_stops geometry (matches
    # the KPI / vehicle_routes / plan_full). The ALNS objective (km_after) is
    # search-space cost that over-counts seed-epoch return legs, so it is reported
    # only as an improvement %, never as the plan's km headline.
    if committed_km is not None:
        c_total = float(committed_km)
        c_daily = max(0.0, c_total - float(tour_km))
        km_lines = [
            f"- committed plan km (route_stops geometry): {c_total:,.0f}"
            f"  (daily {c_daily:,.0f} + tour {tour_km:,.0f}) — matches KPI",
            f"- ALNS objective improvement: {pct:.1f}% over {restarts} restart(s)",
        ]
    else:
        km_lines = [
            f"- daily km before: {km_before:,.0f}",
            f"- daily km after:  {km_after:,.0f}",
            f"- daily km saved:  {saved:,.0f} ({pct:.1f}%)",
            f"- tour km: {tour_km:,.0f}",
            f"- total km after: {km_after + tour_km:,.0f}",
        ]
    return "\n".join([
        "# ALNS Improvement Summary (daily) + Multiday Tours",
        "",
        f"- window: {start} to {end}",
        f"- restarts: {restarts}",
        f"- selected jobs: {selected_total} (daily + {tour_jobs} tour)",
        f"- multiday tours: {len(seed.tours)}",
        f"- daily served before / after: {served_before} / {served_after}",
        f"- repaired rejected jobs: {repaired_jobs}",
        f"- remaining rejected jobs: {remaining_rejected}",
        *km_lines,
        f"- accepted moves: {accepted_moves}",
        "",
    ])


@dataclass
class WindowInputs:
    """Everything the window solve consumes, built once. Rolling epochs re-filter
    the demand-side frames (via the hooks on ``args``) without re-warming
    geocode/OSRM/catchment."""
    qargo_df: pd.DataFrame
    postcode_cache: dict
    cache_path: Path
    handover: object
    handover_overrides: dict
    use_osrm: bool
    osrm_router: object
    osrm_pairs_before: int
    demand_df: pd.DataFrame
    legs_all_df: pd.DataFrame
    legs_df: pd.DataFrame
    vehicle_df: pd.DataFrame
    candidate_all: pd.DataFrame
    candidate_df: pd.DataFrame
    option_choices: list
    hub_drop_choices: list
    compatibility_df: pd.DataFrame
    freight_states_df: pd.DataFrame


@dataclass
class SolveResult:
    """One window solve's outcome, in the exact shape ``emit_outputs`` consumes.
    ``run_rolling`` fabricates one of these from merged frozen trips."""
    seed: object
    imp: object
    trunk_plan: object
    tour_records: list
    tour_km: float
    combined_avail_overrides: dict


_COLLECT_LEG_KINDS = {"CUSTOMER_PICKUP", "DIRECT_CUSTOMER_MOVE"}


def _floor_collection_earliest_to_creation(candidate_df: pd.DataFrame,
                                           qargo_df: pd.DataFrame) -> pd.DataFrame:
    """Non-anticipation guarantee (user rule 2026-07-11), dynamic-only: floor
    every collection leg's ``earliest_start`` to its order's creation time so the
    router never schedules a pickup before the freight was booked — in planning
    AND at emission. Creation is a FIXED per-order value, so unlike a
    decision-time dispatch floor it survives every anchor re-plan / warm-start
    rebuild that reconstructs jobs from the candidate frame. An existing, tighter
    (later) pickup window is kept. Deliveries are untouched (only collections are
    audited). The static planner never sets this flag, so its full-knowledge
    baselines are unchanged."""
    if (candidate_df is None or candidate_df.empty
            or "leg_kind" not in candidate_df.columns or "order_id" not in candidate_df.columns):
        return candidate_df
    created = (pd.to_datetime(qargo_df.set_index(qargo_df["order_id"].astype(str))["timestamp_created"],
                              errors="coerce", utc=True).dt.tz_localize(None)
               .dt.strftime("%Y-%m-%d %H:%M:%S")).to_dict()
    df = candidate_df.copy()
    is_collect = df["leg_kind"].astype(str).isin(_COLLECT_LEG_KINDS)
    cr = df["order_id"].astype(str).map(created).fillna("")
    es = (df["earliest_start"].astype(str).replace("nan", "")
          if "earliest_start" in df.columns else pd.Series("", index=df.index))
    # element-wise max of two "YYYY-MM-DD HH:MM:SS" strings (lexicographic == chronological)
    floored = es.combine(cr, lambda a, b: a if a >= b else b)
    if "earliest_start" not in df.columns:
        df["earliest_start"] = es
    df.loc[is_collect, "earliest_start"] = floored[is_collect]
    # creation_floor: the raw booking time carried on collection legs, kept SEPARATE
    # from earliest_start (which may hold a later real pickup window). The tour date
    # floor reads it so a multi-day sweep never departs before its freight was booked
    # — evaluate_tour is day-granular and does not honour earliest_start. Dynamic
    # only: this function runs solely under the ``collect_creation_floor`` flag.
    if "creation_floor" not in df.columns:
        df["creation_floor"] = ""
    df.loc[is_collect, "creation_floor"] = cr[is_collect]
    return df


def _clamp_past_candidates(candidate_df: pd.DataFrame, min_day, keep_leg_ids: set,
                           runlog) -> pd.DataFrame:
    """A4 day clamp (route-backdating fix 2026-07-14): a rolling epoch may not
    OFFER candidates dated before its own day. A stale-dated unserved leg is
    plantable on a PAST vehicle-day — a key that can never launch, so no floor
    or watermark ever guards it (the daily sibling of the Fix-8 tour clamp; three
    runs_rules stops were planted this way by next-day seeds). Committed /
    in-flight legs (``keep_leg_ids`` = the epoch's seed-exclude set) keep their
    rows: injected trips still need the candidate metadata. ``min_day`` None
    (static path) = untouched."""
    if not min_day or candidate_df is None or candidate_df.empty \
            or "service_date" not in candidate_df.columns:
        return candidate_df
    stale = candidate_df["service_date"].astype(str).str[:10] < str(min_day)
    if keep_leg_ids:
        stale &= ~candidate_df["leg_id"].astype(str).isin(keep_leg_ids)
    if not stale.any():
        return candidate_df
    runlog.log(f"day-clamp: dropped {int(stale.sum())} candidate leg(s) dated before "
               f"{min_day} (A4: nothing plans into the deciding epoch's past)")
    return candidate_df[~stale].copy()


def _clamp_future_candidates(candidate_df: pd.DataFrame, max_day,
                             runlog) -> pd.DataFrame:
    """Withhold ordinary candidate legs beyond the planning-window end.

    Demand and leg metadata stay in the window universe, allowing an in-window
    collection to finish AT_DEPOT and transfer through handover.  Multi-day
    tour tails are generated later by tour emission and do not pass through
    this ordinary-candidate clamp.
    """
    if not max_day or candidate_df is None or candidate_df.empty \
            or "service_date" not in candidate_df.columns:
        return candidate_df
    future = candidate_df["service_date"].astype(str).str[:10] > str(max_day)
    if not future.any():
        return candidate_df
    runlog.log(
        f"window-clamp: dropped {int(future.sum())} ordinary candidate leg(s) "
        f"dated after {max_day}; collected freight transfers via handover")
    return candidate_df[~future].copy()


def _exclude_rival_tour_options(candidate_df: pd.DataFrame,
                                tour_records: list | None) -> pd.DataFrame:
    """Remove option groups contradicted by a fixed multi-day tour.

    Tours live outside ALNS's daily-route solution. Without carrying their mode
    claim across that boundary, ALNS can repair an unassigned rival into a daily
    route even though the shared freight ledger has committed the tour option.
    Same-group legs remain available because an XDOCK tour may still need its
    daily feeder pickup.
    """
    if (candidate_df is None or candidate_df.empty or not tour_records
            or not {"leg_id", "option_set", "option_group"} <= set(candidate_df.columns)):
        return candidate_df

    by_leg = {
        str(r.leg_id): (str(r.option_set or ""), str(r.option_group or ""))
        for r in candidate_df[["leg_id", "option_set", "option_group"]].itertuples(index=False)
    }
    claimed: dict[str, str] = {}
    for record in tour_records:
        option_set, option_group = by_leg.get(
            str(getattr(record, "leg_id", "") or ""), ("", ""))
        if option_set and option_group:
            previous = claimed.setdefault(option_set, option_group)
            if previous != option_group:
                raise ValueError(
                    f"fixed tour records claim conflicting option groups for {option_set}: "
                    f"{previous} and {option_group}")
    if not claimed:
        return candidate_df

    option_set = candidate_df["option_set"].fillna("").astype(str)
    option_group = candidate_df["option_group"].fillna("").astype(str)
    rival = pd.Series(False, index=candidate_df.index)
    for claimed_set, claimed_group in claimed.items():
        rival |= option_set.eq(claimed_set) & option_group.ne(claimed_group)
    return candidate_df.loc[~rival].copy()


def build_window_inputs(args, start: date, end: date, runlog: RunLog) -> WindowInputs:
    """The former "build inputs" stage of ``main``. Rolling hooks (all read via
    ``getattr`` so plain argparse Namespaces work unchanged):

    * ``visible_order_ids``: set[str] | None — E6 visibility gate; filters the
      demand-side frames to orders knowable at the epoch. None = full knowledge.
    * ``slip_priority``: dict[order_id -> int] | None — E6 aging; lands as a
      ``slip_priority`` column on the candidate frame (absent -> 0.0 everywhere).
    * ``qargo_frame``: pd.DataFrame | None — pre-loaded (possibly slip-re-dated)
      Qargo frame; None loads ``args.qargo`` from disk as always.
    * ``exclude_leg_ids``: set[str] | None — legs already frozen by committed
      trips; they leave the problem for good (leg-level, so an order whose
      collection froze still gets its delivery planned).
    * ``extra_staged``: dict[order_id -> (depot, ready_iso)] | None — freight
      collected by frozen trips, staged at the depot from the freeze time on
      (merged over the weekly handover's staged map).
    """
    handover = load_handover(args.handover_in)

    use_osrm = args.router == "osrm"
    osrm_router = None
    osrm_pairs_before = 0

    cache_path = Path(args.postcode_cache)
    with runlog.stage("build inputs"):
        qargo_df = getattr(args, "qargo_frame", None)
        if qargo_df is None:
            qargo_df = _load_qargo(Path(getattr(args, "qargo", None) or DEFAULT_ENRICHED))
        postcode_cache = _load_cache(cache_path)
        demand_records = build_demand_records(qargo_df, start, end, responsibility_mode=args.responsibility_mode)
        leg_records = build_movement_leg_records(qargo_df, demand_records, postcode_cache)
        legs_all_df = pd.DataFrame([r.to_dict() for r in leg_records])
        legs_df = filter_legs_by_basis(legs_all_df, start, end, args.date_basis)
        if handover.delivered_order_ids and not legs_df.empty:
            legs_df = legs_df[~legs_df["order_id"].astype(str).isin(handover.delivered_order_ids)].copy()
            runlog.log(f"handover: excluded {len(handover.delivered_order_ids)} orders already delivered by prior week")
        visible = getattr(args, "visible_order_ids", None)
        if visible is not None and not legs_df.empty:
            n_before = len(legs_df)
            legs_df = legs_df[legs_df["order_id"].astype(str).isin(visible)].copy()
            runlog.log(f"visibility: {len(legs_df)}/{n_before} leg rows knowable at this epoch")
        frozen_legs = getattr(args, "exclude_leg_ids", None)
        if frozen_legs and not legs_df.empty:
            legs_df = legs_df[~legs_df["leg_id"].astype(str).isin(frozen_legs)].copy()
        staged_orders = getattr(args, "extra_staged", None)
        if staged_orders and not legs_df.empty and "ready_state" in legs_df.columns:
            # A staged order's freight is AT_DEPOT: its remaining problem is the
            # XDOCK delivery only. Drop the now-impossible option variants (the
            # DIRECT move and any other AT_CUSTOMER-origin leg) or the resolver,
            # seeing an incomplete option universe, may pick one of them.
            drop = (legs_df["order_id"].astype(str).isin(set(staged_orders))
                    & legs_df["ready_state"].astype(str).ne("AT_DEPOT"))
            legs_df = legs_df[~drop].copy()
        vehicle_df = vehicle_states_frame(start)
        vehicle_df, handover_overrides = apply_availability(vehicle_df, handover, start)
        if handover_overrides:
            runlog.log(f"handover: {len(handover_overrides)} vehicles held in-flight at window open")
        fleet_types = dict(zip(vehicle_df["vehicle_id"].astype(str), vehicle_df["vehicle_type"].astype(str)))
        catchment = build_vehicle_catchment(qargo_df, postcode_cache, fleet_types=fleet_types)
        vehicle_df["catchment_km"] = vehicle_df["vehicle_id"].astype(str).map(catchment).fillna(0.0)
        n_own = int((vehicle_df["catchment_km"] > 0.0).sum())
        runlog.log(f"catchment: radii for {len(catchment)} regs; {n_own}/{len(vehicle_df)} fleet vehicles mapped")
        candidate_all = candidate_jobs_frame(legs_df, vehicle_df, start)
        if use_osrm:
            osrm_router, n_warm = warm_osrm_for_run(
                candidate_all,
                vehicle_df,
                postcode_cache,
                cache_path=getattr(args, "osrm_cache", None),
            )
            osrm_pairs_before = len(osrm_router.matrix)
            runlog.log(f"osrm warmed over {n_warm} coords -> {osrm_pairs_before} cache pairs")
        # Endogenous DIRECT/XDOCK (2026-07-23): both option groups flow into the
        # optimizer; the seed + ALNS choose the mode on real routed cost. The choice
        # is read back from the selected plan at emit time (endogenous_option_choices),
        # so there is no up-front collapse and no static ratio.
        candidate_df = candidate_all
        option_choices: list = []
        candidate_df, hub_drop_choices = resolve_hub_drop(candidate_df, postcode_cache)
        if getattr(args, "collect_creation_floor", False):
            # E6 dynamic: a collection cannot be served before its order existed.
            candidate_df = _floor_collection_earliest_to_creation(candidate_df, qargo_df)
        slip_map = getattr(args, "slip_priority", None)
        if slip_map and not candidate_df.empty:
            candidate_df = candidate_df.copy()
            candidate_df["slip_priority"] = (
                candidate_df["order_id"].astype(str).map(slip_map).fillna(0.0).astype(float))
            runlog.log(f"aging: {int((candidate_df['slip_priority'] > 0).sum())} candidate rows carry slip priority")
        candidate_df = _clamp_past_candidates(
            candidate_df, getattr(args, "min_service_day", None),
            set(getattr(args, "seed_exclude_leg_ids", None) or set()), runlog)
        candidate_df = _clamp_future_candidates(
            candidate_df, getattr(args, "max_service_day", None), runlog)
        compatibility_df = vehicle_job_compatibility_frame(candidate_df, vehicle_df, postcode_cache)
        demand_df_all = pd.DataFrame([r.to_dict() for r in demand_records])
        if args.date_basis == "service_date":
            demand_df = align_demand_to_legs(demand_df_all, legs_df)
        else:
            demand_df = filter_demand_by_basis(demand_df_all, start, end, args.date_basis)
            demand_df = align_demand_to_legs(demand_df, legs_df) if not legs_df.empty else demand_df
        _, demand_df = apply_exclusion(legs_df, demand_df, handover)
        if visible is not None and not demand_df.empty and "order_id" in demand_df.columns:
            demand_df = demand_df[demand_df["order_id"].astype(str).isin(visible)].copy()
        staged = staged_depot_map(handover)
        extra_staged = getattr(args, "extra_staged", None)
        if extra_staged:
            staged = {**staged, **extra_staged}
        freight_states_df = pd.DataFrame([r.to_dict() for r in build_initial_freight_states(
            demand_df, legs_df, planning_start=start, staged_overrides=staged)])

    return WindowInputs(
        qargo_df=qargo_df, postcode_cache=postcode_cache, cache_path=cache_path,
        handover=handover, handover_overrides=handover_overrides,
        use_osrm=use_osrm, osrm_router=osrm_router, osrm_pairs_before=osrm_pairs_before,
        demand_df=demand_df, legs_all_df=legs_all_df, legs_df=legs_df,
        vehicle_df=vehicle_df, candidate_all=candidate_all, candidate_df=candidate_df,
        option_choices=option_choices, hub_drop_choices=hub_drop_choices,
        compatibility_df=compatibility_df, freight_states_df=freight_states_df,
    )


def solve_window(args, start: date, inputs: WindowInputs, runlog: RunLog) -> SolveResult:
    """The former seed + trunk + ALNS stages of ``main``. Rolling hooks:

    * ``external_reserved``: set[(vehicle_id, day)] | None — vehicle-days frozen
      by committed trips/tours; excluded from seed, tours and ALNS.
    * ``extra_avail_overrides``: dict[(vehicle_id, day) -> "HH:MM" | DutyOverride]
      | None — post-freeze vehicle state (start + duty carry); wins over trunk
      and handover overrides.
    """
    candidate_df = inputs.candidate_df
    vehicle_df = inputs.vehicle_df
    compatibility_df = inputs.compatibility_df
    freight_states_df = inputs.freight_states_df
    handover_overrides = inputs.handover_overrides

    external_reserved = getattr(args, "external_reserved", None)
    extra_avail_overrides = getattr(args, "extra_avail_overrides", None)

    # E6 dynamic (spec 4.7a): the seed plans only the not-yet-committed
    # remainder — legs already riding in-flight trips are hidden from IT ONLY
    # (ALNS still needs their candidate rows for job metadata).
    seed_exclude = getattr(args, "seed_exclude_leg_ids", None)
    seed_candidates = candidate_df
    if seed_exclude and not candidate_df.empty:
        seed_candidates = candidate_df[
            ~candidate_df["leg_id"].astype(str).isin(set(seed_exclude))]

    # E6 dynamic: the seed must also see in-flight VEHICLE TIME — a vehicle
    # whose committed trips run 07:00-16:50 is only seed-plannable after 16:50
    # (with duty carry), else the seed double-books the day and injection chains
    # an impossible trip 2 behind trip 1. These constraints are SEED-ONLY:
    # ALNS keeps the full day open for suffix insertion into the same trips.
    seed_avail = dict(extra_avail_overrides or {})
    seed_avail.update(getattr(args, "seed_avail_overrides", None) or {})
    seed_reserved = set(external_reserved or set())
    seed_reserved |= set(getattr(args, "seed_external_reserved", None) or set())

    with runlog.stage("multiday seed"):
        seed = run_multiday_seed_plan(seed_candidates, vehicle_df, compatibility_df, freight_states_df, start,
                                      consolidate_tours=args.consolidate_tours,
                                      external_reserved=(seed_reserved or None),
                                      extra_avail_overrides=(seed_avail or None),
                                      trunk_from=getattr(args, "trunk_from", None))
    tour_records = seed.tour_records or []
    tour_km = sum(ta.evaluation.total_km for ta in seed.tours)
    runlog.log(
        f"seed selected={len(seed.selected)} rejected={len(seed.rejected)} daily-vehicle-days={len(seed.routes)} tours={len(seed.tours)}"
    )
    sstats = getattr(seed.daily, "shuttle_stats", {}) or {}
    if sstats.get("trips"):
        top = ", ".join(f"{pc} {d}: {n} trips" for (d, pc), n in sstats.get("top", []))
        runlog.log(
            f"shuttle: {sstats['address_days']} address-days -> {sstats['trips']} trips / "
            f"{sstats['jobs']} jobs / {sstats['pallets']:,.0f} pallets ({top})")

    trunk_plan = getattr(seed, "trunk", None)
    trunk_avail_overrides = trunk_plan.avail_overrides if trunk_plan is not None else None
    combined_avail_overrides = dict(trunk_avail_overrides or {})
    combined_avail_overrides.update(handover_overrides)
    if extra_avail_overrides:
        # E6: epoch state is the most informed view of a vehicle's day — it wins.
        combined_avail_overrides.update(extra_avail_overrides)

    excluded_vehicle_days = set(seed.reserved or set())
    if external_reserved:
        excluded_vehicle_days |= set(external_reserved)

    # Tours are fixed outside ALNS's daily solution, but their ledger/mode claim
    # remains binding on the repair universe (WT267756, 2026-02-16).
    alns_candidates = _exclude_rival_tour_options(candidate_df, tour_records)

    with runlog.stage("coverage-aware alns"):
        best_imp = None
        restart_count = max(1, args.restarts)
        for restart_idx in range(restart_count):
            seed_i = args.seed + restart_idx
            imp_i = improve_route_seed(
                seed.daily,
                alns_candidates,
                vehicle_df,
                compatibility_df,
                iterations=args.iterations,
                rng_seed=seed_i,
                log_every=args.log_every,
                converge_pct=getattr(args, "converge_pct", None),
                converge_window=getattr(args, "converge_window", None),
                converge_min_iters=getattr(args, "converge_min_iters", None),
                excluded_vehicle_days=excluded_vehicle_days,
                time_budget_s=args.time_budget,
                no_improve_patience=args.no_improve,
                rejected=seed.rejected,
                sa_temp_fraction=args.sa_temp,
                sa_cooling=args.sa_cooling,
                repair_every=args.repair_every,
                regret_repair=args.regret_repair,
                pinned_job_ids=(set(getattr(seed.daily, "shuttle_job_ids", set()) or set())
                                | set(getattr(args, "extra_pinned_job_ids", None) or set())),
                avail_overrides=(combined_avail_overrides or None),
                day_flex=args.day_flex,
                extra_routes=getattr(args, "inject_routes", None),
                watermarks=getattr(args, "watermarks", None),
                commit_floor=getattr(args, "commit_floor", None),
                now=getattr(args, "now", None),
                locked_keys=getattr(args, "locked_keys", None),
                beta=float(getattr(args, "beta", 0.0) or 0.0),
                reference_routes=getattr(args, "reference_routes", None),
                disturbance_weight=getattr(args, "disturbance_weight", None),
                # NB: the 4th arg is the COST objective (per-km fuel-weighted), not
                # physical km — the search accepts a km-up move if it lowers cost. Label
                # it 'cost' so the convergence curve isn't misread as kilometres.
                # served=coverage per logged iteration -> convergence AND coverage-vs-iteration
                # both live in alns_progress.log (§6.7).
                on_progress=lambda it, its, acc, cost, served=None, ridx=restart_idx, rc=restart_count: runlog.log(
                    f"alns[{ridx + 1}/{rc}] iter {it}/{its} accepted={acc} cost={cost:,.0f}"
                    + (f" served={served}" if served is not None else "")
                ),
            )
            runlog.log(
                f"alns[{restart_idx + 1}/{restart_count}] km {imp_i.km_before:,.0f} -> {imp_i.km_after:,.0f} "
                f"saved={imp_i.km_before - imp_i.km_after:,.0f} accepted={imp_i.accepted_moves} "
                f"inserted={imp_i.inserted_jobs} remaining={len(imp_i.remaining_rejected)} seed={seed_i}"
            )
            if best_imp is None or _imp_score(imp_i) > _imp_score(best_imp):
                best_imp = imp_i
        imp = best_imp
    runlog.log(
        f"best alns km {imp.km_before:,.0f} -> {imp.km_after:,.0f} saved={imp.km_before - imp.km_after:,.0f} "
        f"accepted={imp.accepted_moves} inserted={imp.inserted_jobs} remaining={len(imp.remaining_rejected)}"
    )
    ms = getattr(imp, "merge_sweep", None)
    if ms is not None:
        runlog.log(
            f"merge-sweep: applied {ms.applied} of {ms.candidates} candidates "
            f"(km delta {ms.km_delta:+,.1f}, rollbacks {ms.rollbacks}) "
            f"census={dict(ms.census)}")

    plan_km_total = imp.km_after + tour_km
    if trunk_plan is not None and trunk_plan.nights:
        combined_km = plan_km_total + trunk_plan.total_km
        runlog.log(f"trunk: {trunk_log_summary(trunk_plan.nights)}")
        runlog.log(
            f"       total {trunk_plan.total_km:,.0f} km, {trunk_plan.total_trips} trips "
            f"| shortfall nights: {len(trunk_plan.shortfalls)}"
        )
        runlog.log(
            f"       combined: plan {plan_km_total:,.0f} + trunk {trunk_plan.total_km:,.0f} "
            f"= {combined_km:,.0f} km"
        )

    _vrow = {str(r.vehicle_id): r for r in vehicle_df.itertuples(index=False)}
    n_oa = n_jobs = 0
    for (vid, _day), trips in imp.solution.items():
        row = _vrow.get(str(vid))
        if row is None or not trips:
            continue
        cat = float(getattr(row, "catchment_km", 0.0) or 0.0)
        tt = [trips] if hasattr(trips[0], "job_id") else trips
        for trip in tt:
            for j in trip:
                n_jobs += 1
                if out_of_area_penalty_km(
                        job_distance_km(float(row.current_lat), float(row.current_lon), j), cat) > 0.0:
                    n_oa += 1
    if n_jobs:
        runlog.log(f"catchment: {n_oa}/{n_jobs} daily jobs beyond their vehicle's radius ({100.0 * n_oa / n_jobs:.1f}%)")

    return SolveResult(
        seed=seed, imp=imp, trunk_plan=trunk_plan, tour_records=tour_records,
        tour_km=tour_km, combined_avail_overrides=combined_avail_overrides,
    )


def reoptimize_window(args, start: date, inputs: WindowInputs, runlog: RunLog) -> SolveResult:
    """Dynamic v2 warm-start solve (spec 2026-07-11 §4): improve the injected LIVE
    plan IN PLACE — insert newly-visible orders and re-optimize the uncommitted
    horizon — with NO re-seed (no tour formation, no trunk, no daily seed). Tours,
    the shuttle carve and the trunk are established once at the 00:00 midnight seed and
    carried inside the injected plan; watermarks/floor/locks protect committed
    stops so nothing rebuilds a departed trip (this is what closes the
    non-anticipation class the re-seed architecture kept re-opening)."""
    from freight_planner.planner_state import RejectedJob

    candidate_df = inputs.candidate_df
    # The warm-start base is the COMPLETE live incumbent. Watermarks pin its
    # committed prefix; uncommitted work remains movable by the destroy/repair
    # operators. Keeping it assigned is essential to the lexicographic coverage
    # guarantee: deleting it first would make restoration depend on random
    # rejected-pool sampling rather than on ALNS accepting a coverage-preserving
    # move. Newly visible jobs alone enter the repair pool below.
    live = {k: [list(t) for t in _as_trips(v)]
            for k, v in (getattr(args, "inject_routes", None) or {}).items()}
    reference = getattr(args, "reference_routes", None) or live

    combined = dict(inputs.handover_overrides or {})
    extra = getattr(args, "extra_avail_overrides", None)
    if extra:
        combined.update(extra)
    reserved_vehicle_days = set(
        getattr(args, "external_reserved", None) or set()
    )

    # Newly-visible orders = candidate jobs not already on the live plan -> the
    # repair pool. improve inserts them under floor/watermark/lock, so a noon
    # arrival can only land after the dispatch floor or on a fresh floored trip,
    # never inside a departed morning trip.
    # reason must be in alns._REPAIRABLE_REASONS for the repair pool to attempt
    # insertion; NO_FEASIBLE_ROUTE is the generic "not yet placed" tag.
    on_plan = {j.job_id for trips in live.values() for t in trips for j in t}
    new_rejected = [RejectedJob(job_id=str(jid), reason="NO_FEASIBLE_ROUTE")
                    for jid in candidate_df["job_id"].astype(str).unique()
                    if str(jid) not in on_plan] if not candidate_df.empty else []

    imp = improve_existing_solution(
        live, candidate_df, inputs.vehicle_df, inputs.compatibility_df,
        iterations=args.iterations, rng_seed=args.seed, log_every=args.log_every,
        time_budget_s=args.time_budget, no_improve_patience=args.no_improve,
        converge_pct=getattr(args, "converge_pct", None),
        converge_window=getattr(args, "converge_window", None),
        converge_min_iters=getattr(args, "converge_min_iters", None),
        rejected=new_rejected, sa_temp_fraction=args.sa_temp, sa_cooling=args.sa_cooling,
        repair_every=args.repair_every, regret_repair=args.regret_repair,
        excluded_vehicle_days=reserved_vehicle_days,
        pinned_job_ids=set(getattr(args, "extra_pinned_job_ids", None) or set()),
        avail_overrides=(combined or None),
        watermarks=getattr(args, "watermarks", None),
        commit_floor=getattr(args, "commit_floor", None),
        now=getattr(args, "now", None),
        locked_keys=getattr(args, "locked_keys", None),
        beta=float(getattr(args, "beta", 0.0) or 0.0),
        reference_routes=reference,
        disturbance_weight=getattr(args, "disturbance_weight", None),
    )
    runlog.log(f"warm-start reopt: served {imp.served_before}->{imp.served_after} "
               f"km {imp.km_before:,.0f}->{imp.km_after:,.0f} inserted={imp.inserted_jobs} "
               f"repair-attempts={imp.attempted_jobs} remaining={len(imp.remaining_rejected)}")
    seed_shell = SimpleNamespace(tours=[], rejected=[],
                                 daily=SimpleNamespace(shuttle_job_ids=set()))
    return SolveResult(seed=seed_shell, imp=imp, trunk_plan=None, tour_records=[],
                       tour_km=0.0, combined_avail_overrides=combined)


def emit_outputs(args, start: date, end: date, inputs: WindowInputs, result: SolveResult,
                 plan_dir: Path, reports_dir: Path, runlog: RunLog,
                 final_avail_overrides: dict | None = None,
                 final_job_floors: dict[str, str] | None = None,
                 ever_committed_legs: set[str] | None = None) -> int:
    """The former "write outputs" stage of ``main``, unchanged: the full plan-dir
    artifact contract (selected plan, route_stops via reports, KPI, handover,
    runsheets, summary). Rolling calls this once with a merged SolveResult.

    ``final_avail_overrides`` (rolling only; ``None`` on the static path) is the
    SAME dispatch-floor/build-context override map ``imp_final`` was itself
    computed under, and ``final_job_floors`` (leg_id -> ISO floor) is each
    surviving job's OWN per-job dispatch floor (from the rolling loop's
    ``placement`` trace) -- both threaded into ``rebuild_daily_routes_after_drop``
    so a post-drop retime re-times from the vehicle's TRUE build context and
    never arrives before whichever epoch actually placed a given stop, instead
    of a bare profile with no per-job floor awareness (2026-07-28
    route-backdating fix, see route_seed.py).

    ``ever_committed_legs`` (rolling only) is every leg_id ever watermark-committed
    (locked to a driver) at any epoch during the day -- passed to
    ``drop_superseded_option_legs`` so the commit-boundary DIRECT/XDOCK backstop
    never silently drops an already-promised job onto a different vehicle
    (2026-07-28, see ledger.py).

    Returns ``(rc, option_conflicts)``: ``rc`` is the process return code (0), and
    ``option_conflicts`` is the count of option sets left with BOTH a DIRECT and an
    XDOCK leg selected because the losing side was already committed (should be 0) --
    threaded out so callers can fold it into feasibility_audit.csv alongside the other
    dynamic audits instead of it living only in this function's runlog line."""
    seed = result.seed
    imp = result.imp
    trunk_plan = result.trunk_plan
    tour_records = result.tour_records
    tour_km = result.tour_km
    candidate_df = inputs.candidate_df
    demand_df = inputs.demand_df
    legs_all_df = inputs.legs_all_df
    vehicle_df = inputs.vehicle_df
    compatibility_df = inputs.compatibility_df
    hub_drop_choices = inputs.hub_drop_choices
    postcode_cache = inputs.postcode_cache
    cache_path = inputs.cache_path
    osrm_router = inputs.osrm_router
    osrm_pairs_before = inputs.osrm_pairs_before
    use_osrm = inputs.use_osrm

    combined_selected = list(imp.selected) + tour_records

    with runlog.stage("write outputs"):
        report_candidate_df = candidate_df
        route_totals = dict(imp.route_totals)
        route_totals.update(_tour_route_totals(seed.tours))
        route_times = dict(imp.route_times)   # depot depart/return clock per daily route (v1.1)
        # Commit-boundary drops (below) filter combined_selected by leg_id but never
        # re-time the survivors of a shortened daily route — track which ROUTE: ids
        # lose a leg so they can be re-evaluated afterward (audit follow-up 2026-07-27).
        affected_route_ids: set[str] = set()
        violations = plan_ledger_violations(combined_selected, report_candidate_df)
        if violations:
            # ENFORCE, don't just count: a delivery whose pickup was never selected is
            # freight delivered that was never collected. Drop it so the plan is honest
            # (the order stays unserved). No-op for the full-knowledge static plan.
            _before = list(combined_selected)
            combined_selected, dropped = drop_orphan_deliveries(combined_selected, report_candidate_df)
            affected_route_ids |= {str(r.route_id) for r in _before if str(r.leg_id) in dropped}
            runlog.log(f"dropped {len(dropped)} orphan deliveries (delivery without its pickup -> "
                       f"freight never collected): {sorted(dropped)[:6]}")
            violations = plan_ledger_violations(combined_selected, report_candidate_df)
        # Commit-boundary DIRECT/XDOCK invariant (2026-07-23): the rolling loop can
        # commit a freight's DIRECT leg AND an XDOCK pickup across separate passes/
        # epochs (the XDOCK delivery having stranded). Keep the delivering group,
        # drop the superseded one, so no freight is both moved-direct and collected.
        _before = list(combined_selected)
        combined_selected, superseded, superseded_conflicts = drop_superseded_option_legs(
            combined_selected, report_candidate_df, committed_leg_ids=ever_committed_legs)
        affected_route_ids |= {str(r.route_id) for r in _before if str(r.leg_id) in superseded}
        if superseded:
            runlog.log(f"dropped {len(superseded)} superseded option legs (freight served by the "
                       f"other mode): {sorted(superseded)[:6]}")
        if superseded_conflicts:
            # A losing group held an already watermark-committed leg -- dropping it would
            # silently reassign a promised job to a different vehicle, so it was kept
            # instead. The DIRECT/XDOCK invariant stays violated for these option sets;
            # this is a real conflict, not a resolved one (2026-07-28, see ledger.py).
            runlog.log(f"!! OPTION CONFLICT: {len(superseded_conflicts)} option set(s) left with "
                       f"BOTH DIRECT and XDOCK legs selected because the losing side was already "
                       f"committed to a driver (should be 0): {sorted(superseded_conflicts)[:6]}")
        # A tour whose only freight leg was just superseded/orphan-dropped keeps its
        # leg_id-less OVERNIGHT/RETURN scaffolding — prune the whole empty tour so it books
        # no vehicle-days/km/subsistence for zero freight (audit #1).
        combined_selected, empty_tours = drop_freightless_tours(combined_selected)
        if empty_tours:
            runlog.log(f"pruned {len(empty_tours)} freight-less tour(s) (all freight legs dropped "
                       f"-> scaffolding only): {sorted(empty_tours)[:4]}")
        combined_selected, rebuilt_tours, rebuilt_ids = _rebuild_tours_after_final_drops(
            seed.tours, combined_selected, vehicle_df)
        seed.tours = rebuilt_tours
        tour_records = [r for r in combined_selected
                        if str(getattr(r, "route_id", "")).startswith("TOUR:")]
        tour_km = sum(float(ta.evaluation.total_km) for ta in rebuilt_tours)
        if rebuilt_ids:
            runlog.log(f"rebuilt {len(rebuilt_ids)} partially-pruned tour(s) from surviving "
                       f"freight stops: {sorted(rebuilt_ids)[:4]}")
        # Daily (non-tour) counterpart to the tour rebuild above: a route that lost a
        # leg to one of the drops above kept every OTHER stop's stale arrive/depart/
        # break/drive-minutes, computed against a route that no longer exists (found
        # verifying the tour audit's closing run — R888GNW/2026-02-02 drove 297 min in
        # one day with zero statutory break recorded anywhere, and downstream stops
        # carried an hours-long phantom gap). Re-evaluate just those routes.
        if affected_route_ids:
            _cand_by_leg = {str(getattr(r, "leg_id", "")): r
                            for r in report_candidate_df.itertuples(index=False)}
            _vrow_by_id = {str(getattr(r, "vehicle_id", "")): r
                          for r in vehicle_df.itertuples(index=False)}
            combined_selected, _retime_failed = rebuild_daily_routes_after_drop(
                combined_selected, affected_route_ids, _cand_by_leg,
                _job_coords(compatibility_df), _vrow_by_id,
                avail_overrides=final_avail_overrides, job_floors=final_job_floors)
            _retimed = affected_route_ids - _retime_failed
            if _retimed:
                runlog.log(f"re-timed {len(_retimed)} daily route(s) whose sequence lost a "
                           f"leg to the drops above: {sorted(_retimed)[:4]}")
            if _retime_failed:
                # Shortening the route made a still-no_early_arrival-gated stop arrive
                # before it was ever real (the leg that used to absorb that travel time
                # is gone) — evaluator correctly refuses it. Keep this route's pre-drop
                # values (already-shipped behaviour) rather than crash the whole run.
                runlog.log(f"kept {len(_retime_failed)} daily route(s) at their pre-drop "
                           f"timing — re-timing them was infeasible: "
                           f"{sorted(_retime_failed)[:4]}")
        selected_df = selected_plan_frame(combined_selected)
        # Build the committed geometry ONCE (daily returns = real road km, tours carry
        # their multi-day residual) and derive per-route km from it. EVERY km output
        # below — selected_plan_alns, cross-depot, and (via write_reports) the manifest,
        # vehicle_routes, utilization and KPI — reads THIS committed distance, so the
        # phantom evaluator route_totals never reaches an output number and all files
        # reconcile. route_totals stays in only to source tours' multi-day return.
        _tour_return_dates = {}
        for _ta in (seed.tours or []):
            _rid = f"TOUR:{_ta.vehicle_id}:{_ta.start_date}"
            _end = date.fromisoformat(str(_ta.start_date)) + timedelta(days=max(0, int(_ta.days) - 1))
            _tour_return_dates[_rid] = _end.isoformat()
        route_stops_df = build_route_stops(
            selected_df, report_candidate_df, compatibility_df, vehicle_df, route_totals,
            tour_return_dates=_tour_return_dates, route_times=route_times)
        geom_totals = geom_route_totals(route_stops_df)
        geom_drive_totals = geom_route_drive_totals(route_stops_df)
        # Later pruning/rebuild steps can remove an intermediate committed-mode
        # conflict. Publish the state of the final surviving records, not the
        # earlier commit-boundary snapshot.
        superseded_conflicts = selected_option_conflicts(
            combined_selected, report_candidate_df)
        tviol = temporal_violations(selected_df)
        xreport = cross_depot_report(selected_df, report_candidate_df, route_totals=geom_totals)
        plan_path = plan_dir / "selected_plan_alns.csv"
        summary_path = reports_dir / "07_alns_summary.md"
        selected_plan_export_frame(
            combined_selected,
            geom_totals,
            route_drive_totals=geom_drive_totals,
        ).to_csv(plan_path, index=False)
        out_handover = build_handover(
            selected_df, demand_df, start, end, candidate_df=report_candidate_df)
        save_handover(out_handover, plan_dir / "handover.json")
        # Orders collected THIS window with delivery staged for the next window: their
        # deferred delivery must not count against this window's completeness (KPI).
        staged_handover_ids = {str(s.order_id).split("#", 1)[0]
                               for s in out_handover.staged_freight}
        runlog.log(
            f"handover: emitted {len(out_handover.vehicle_availability)} in-flight vehicles, "
            f"{len(out_handover.delivered_order_ids)} delivered, {len(out_handover.staged_freight)} staged "
            f"-> {plan_dir / 'handover.json'}")
        endo_choices = endogenous_option_choices(selected_df, report_candidate_df)
        (reports_dir / "06_plan_choices.md").write_text(
            "# Plan choices" + chr(10) + chr(10)
            + endogenous_option_choices_md(endo_choices).strip() + chr(10) + chr(10)
            + hub_drop_choices_md(hub_drop_choices).strip() + chr(10) + chr(10)
            + cross_depot_report_md(xreport).strip() + chr(10), encoding="utf-8")
        if not tviol.empty:
            tviol.to_csv(reports_dir / "temporal_violations.csv", index=False)
        kpi_report, fully_accounted = write_reports(
            plan_dir,
            start=start,
            end=end,
            demand_df=demand_df,
            legs_all_df=legs_all_df,
            candidate_df=report_candidate_df,
            compatibility_df=compatibility_df,
            vehicle_df=vehicle_df,
            selected_df=selected_df,
            rejected=imp.remaining_rejected,
            tours=seed.tours,
            planned_km=imp.km_after + tour_km,
            cross_depot_km=xreport.repositioning_km,
            phantom_deliveries=len(violations),
            route_totals=route_totals,
            route_times=route_times,
            trunk=trunk_plan,
            shuttle_job_ids=getattr(seed.daily, "shuttle_job_ids", None),
            handover_order_ids=staged_handover_ids,
            route_stops_df=route_stops_df,
        )
        # Written AFTER write_reports so committed_km == the committed route_stops
        # geometry (== KPI planned km), not the phantom-carrying objective km_after.
        (plan_dir / "validation_metrics.json").write_text(
            json.dumps(build_validation_metrics(
                imp, selected_df, trunk=trunk_plan,
                committed_km=float(getattr(kpi_report, "planned_km", imp.km_after + tour_km))),
                indent=2), encoding="utf-8")
        # §6.4 cost decomposition — anchored on the COMMITTED route_stops geometry so km_road
        # reconciles with the headline plan km / km_by_type (physical, 1 authoritative
        # (v,d)->type grouping). km_phantom (objective ranking penalty) carried from the
        # objective decomposition for reference, never folded into reported km/fuel (§5.2).
        from freight_planner.cost_report import build_committed_cost_decomposition, write_cost_decomposition
        _cost_route_stops = clip_route_stops_to_window(
            route_stops_df, start.isoformat(), end.isoformat())
        _cost_rows = build_committed_cost_decomposition(
            _cost_route_stops,
            objective_rows=list(getattr(imp, "cost_decomposition", []) or []))
        write_cost_decomposition(_cost_rows, plan_dir, reports_dir)
        # §6.3b/6.4 committed km + vehicle-days by vehicle type (final side)
        if (_cost_route_stops is not None and not _cost_route_stops.empty
                and "vehicle_type" in _cost_route_stops.columns):
            _km_by = _cost_route_stops.groupby("vehicle_type")["leg_km"].sum().rename("km_road")
            _vd_by = (_cost_route_stops.drop_duplicates(["vehicle_id", "service_date"])
                      .groupby("vehicle_type").size().rename("vehicle_days"))
            pd.concat([_vd_by, _km_by], axis=1).reset_index().to_csv(plan_dir / "km_by_type.csv", index=False)
            # §6.3b seed vs final by type. Vehicle-days are the clean within-basis metric
            # (both are counts) but the DIRECTION is data-dependent: ALNS may spread work
            # onto more veh-days to buy down OT/lateness/range, so a +delta is not a failure
            # to consolidate. The km columns are DIFFERENT bases and must NOT be subtracted:
            # seed_km is the objective/evaluator basis (carries option + return-residual km),
            # final_km is the committed physical basis (post drop_superseded_option_legs). The
            # within-basis ALNS story is the objective-cost curve (cost_before->cost_after).
            _seed = getattr(imp, "seed_by_type", {}) or {}
            if _seed:
                _fvd, _fkm = _vd_by.to_dict(), _km_by.to_dict()
                _fleet_types = dict(zip(vehicle_df["vehicle_id"].astype(str), vehicle_df["vehicle_type"].astype(str)))
                _final_obj_km = _final_km_objective_by_type(
                    getattr(imp, "route_totals", {}) or {}, _fleet_types)
                _rows = [{"vehicle_type": t,
                          "seed_vehicle_days": int(_seed.get(t, {}).get("vehicle_days", 0)),
                          "final_vehicle_days": int(_fvd.get(t, 0)),
                          "seed_km_objective": round(float(_seed.get(t, {}).get("km_road", 0.0)), 1),
                          "final_km_objective": round(float(_final_obj_km.get(t, 0.0)), 1),
                          "final_km_physical": round(float(_fkm.get(t, 0.0)), 1)}
                         for t in sorted(set(_seed) | set(_fvd))]
                _tot = {"vehicle_type": "ALL",
                        "seed_vehicle_days": sum(r["seed_vehicle_days"] for r in _rows),
                        "final_vehicle_days": sum(r["final_vehicle_days"] for r in _rows),
                        "seed_km_objective": round(sum(r["seed_km_objective"] for r in _rows), 1),
                        "final_km_objective": round(sum(r["final_km_objective"] for r in _rows), 1),
                        "final_km_physical": round(sum(r["final_km_physical"] for r in _rows), 1)}
                pd.DataFrame(_rows + [_tot]).to_csv(plan_dir / "seed_vs_final_by_type.csv", index=False)
        # §6.3a incumbent actuals (telematics) + plan-vs-incumbent deltas at FLEET-TOTAL level
        # (all plan vehicles vs all telematics vehicles) + plan driver-hours (committed depot-to-
        # depot span). The per-registration (vehicle,day) match was removed 2026-07-24 — the solver
        # never reproduces reality's vehicle assignment, so a per-reg pairing is meaningless and
        # drops in-universe km on the non-matched side (see incumbent_actuals.build docstring).
        try:
            from freight_planner.incumbent_actuals import build_incumbent_actuals, incumbent_actuals_md
            from freight_planner.kpi import delivery_timeliness_stats
            _plan_hours, _plan_vd = 0.0, None
            # WINDOW-CLIP plan veh-days & driver-hours to [start,end] so they match the
            # incumbent side (built over the same window); a tour's beyond-window return
            # day must not add a plan veh-day/hours reality can't have. (decision-audit #1)
            _rs_win = clip_route_stops_to_window(route_stops_df, start.isoformat(), end.isoformat())
            if _rs_win is not None and not _rs_win.empty:
                _plan_vd = int(_rs_win.drop_duplicates(["vehicle_id", "service_date"]).shape[0])
                for (_vid, _sd), _g in _rs_win.groupby(["vehicle_id", "service_date"]):
                    _dep = _g.loc[_g["stop_type"] == "depot_start", "planned_depart"]
                    _ret = _g.loc[_g["stop_type"] == "depot_return", "planned_arrive"]
                    _s = pd.to_datetime(_dep.iloc[0], errors="coerce") if len(_dep) else pd.NaT
                    _e = pd.to_datetime(_ret.iloc[-1], errors="coerce") if len(_ret) else pd.NaT
                    if pd.notna(_s) and pd.notna(_e) and _e > _s:
                        _plan_hours += (_e - _s).total_seconds() / 3600.0
            _eligible_delivery_ids = set(
                demand_df.loc[
                    demand_df["exclusion_reason"].fillna("").astype(str).eq("")
                    & demand_df["corrected_flow"].astype(str).isin(
                        {"PL_IMPORT", "LOCAL_DELIVER", "FULL_FLEET"}),
                    "order_id",
                ].astype(str)
            )
            _inc = build_incumbent_actuals(
                start,
                end,
                orders_df=inputs.qargo_df,
                delivery_order_ids=_eligible_delivery_ids,
            )
            _plan_delivery_timeliness = delivery_timeliness_stats(_rs_win)
            (reports_dir / "10_incumbent_actuals.md").write_text(
                incumbent_actuals_md(_inc, plan_km=float(getattr(kpi_report, "planned_km", 0.0)),
                                     plan_trunk_km=float(getattr(kpi_report, "trunk_km", 0.0)),
                                     plan_vehicle_days=_plan_vd,
                                     plan_driver_hours=round(_plan_hours, 1) or None,
                                     plan_delivery_timeliness=_plan_delivery_timeliness),
                encoding="utf-8")
            _csv = {k: _inc[k] for k in ("fleet_size", "total_odometer_km", "total_vehicle_days", "total_duty_hours")}
            if "delivery_timeliness" in _inc:
                _csv.update({
                    f"delivery_{k}": v
                    for k, v in _inc["delivery_timeliness"].items()
                })
            if _plan_delivery_timeliness:
                _csv.update({
                    f"plan_delivery_{k}": v
                    for k, v in _plan_delivery_timeliness.items()
                })
            pd.DataFrame([_csv]).to_csv(plan_dir / "incumbent_actuals.csv", index=False)
        except Exception as _e:   # telematics files absent (e.g. headless/CI) -> skip, don't fail the run
            runlog.log(f"incumbent actuals skipped: {_e}")
        if args.day_flex:
            # K2 service-impact ledger: eligible / shifted / days-early histogram
            kpi_path = plan_dir / "02_kpi_summary.md"
            k2_md = render_stats_md(dayflex_stats(selected_df, report_candidate_df))
            kpi_path.write_text(
                kpi_path.read_text(encoding="utf-8").rstrip() + "\n" + k2_md,
                encoding="utf-8")
        rs_df = pd.read_csv(plan_dir / "route_stops.csv")
        (reports_dir / "runsheets.html").write_text(
            build_runsheets_html(rs_df, title=f"Runsheets {start.isoformat()}..{end.isoformat()}"),
            encoding="utf-8")
        runlog.log(f"runsheets: {rs_df['vehicle_id'].nunique()} vehicles")
        util_path = plan_dir / "03_fleet_utilization.md"
        util_md = util_path.read_text(encoding="utf-8").strip() if util_path.exists() else ""
        summary_md = _summary(
            start,
            end,
            seed,
            imp.served_before,
            imp.served_after,
            imp.km_before,
            imp.km_after,
            imp.accepted_moves,
            imp.inserted_jobs,
            len(imp.remaining_rejected),
            tour_km,
            max(1, args.restarts),
            committed_km=float(getattr(kpi_report, "planned_km", imp.km_after + tour_km)),
        ).rstrip()
        if util_md:
            summary_md = f"{summary_md}\n\n{util_md}\n"
        else:
            summary_md = summary_md + "\n"
        summary_path.write_text(summary_md, encoding="utf-8")
        geocode.save_cache(postcode_cache, cache_path)
        if osrm_router is not None and len(osrm_router.matrix) != osrm_pairs_before:
            from freight_planner.shared.routing import CACHE_PATH as OSRM_CACHE_PATH, save_cache as save_osrm_cache
            _osrm_cache_path = getattr(args, "osrm_cache", None) or OSRM_CACHE_PATH
            save_osrm_cache(_osrm_cache_path, osrm_router.matrix)
        # plan_full.csv at the run root — the whole-plan overview, auto on every run
        # (dynamic runs included: it reads the same manifest/route_stops contract)
        window_dir = Path(os.fspath(plan_dir))
        if window_dir.name == "plan":   # legacy layout, e.g. direct callers
            window_dir = window_dir.parent
        emit_plan_full(window_dir, runlog,
                       qargo_cache={str(getattr(args, "qargo", "")): inputs.qargo_df})
    runlog.log(
        f"cross-depot={xreport.cross_depot_assignments} repositioning_km={xreport.repositioning_km:,.0f} "
        f"ledger_violations={len(violations)} temporal_violations={len(tviol)} -- DONE"
    )

    hubdrop = sum(1 for c in hub_drop_choices if c.chosen == "HUBDROP")
    trunk = sum(1 for c in hub_drop_choices if c.chosen == "TRUNK")
    tour_jobs = sum(len(ta.jobs) for ta in seed.tours)
    osrm_fallbacks = getattr(osrm_router, "fallback_count", 0) if osrm_router is not None else 0
    print(f"ALNS improvement for {start} to {end} ({args.iterations} iterations, {max(1, args.restarts)} restart(s))")
    print(f"  road router: {args.router}" + (f" (OSRM fallbacks to haversine: {osrm_fallbacks})" if use_osrm else ""))
    print(f"  PL_EXPORT options:       HUBDROP {hubdrop} / TRUNK {trunk}")
    print(f"  multiday tours:          {len(seed.tours)} ({tour_jobs} jobs, {tour_km:,.0f} km)")
    print(f"  selected (daily+tour):   {len(combined_selected)}")
    print(f"  cross-depot assignments: {xreport.cross_depot_assignments} (repositioning {xreport.repositioning_km:,.0f} km)")
    print(f"  temporal violations (must be 0): {len(tviol)}")
    print(f"  progress log: {reports_dir / 'alns_progress.log'}")
    print(f"  daily served before/after: {imp.served_before} / {imp.served_after}")
    print(f"  repaired rejected jobs: {imp.inserted_jobs} (remaining {len(imp.remaining_rejected)})")
    print(f"  daily km before: {imp.km_before:,.0f}")
    print(f"  daily km after:  {imp.km_after:,.0f}")
    print(f"  daily km saved:  {imp.km_before - imp.km_after:,.0f}")
    # audit #7 (2026-07-26): this used to print imp.km_after + tour_km — the ALNS working
    # solution's km FROZEN AT LOOP EXIT, before drop_orphan_deliveries / drop_superseded_
    # option_legs / drop_freightless_tours run (~16k km of duplicated option legs and pruned
    # empty tours on a typical run). Report the COMMITTED basis instead — the same
    # route_stops.csv sum that km_by_type.csv / cost_by_type.csv / the KPI headline (pre-
    # window-clip) all use — so this line is never a third, unreconciled km number.
    _committed_km = (float(route_stops_df["leg_km"].sum())
                     if route_stops_df is not None and not route_stops_df.empty
                     else imp.km_after + tour_km)
    print(f"  committed km (daily+tour, post-cleanup): {_committed_km:,.0f}")
    print(f"  accepted moves: {imp.accepted_moves}")
    print(f"  stopped: {getattr(imp, 'stop_reason', '') or 'iterations'} "
          f"after {getattr(imp, 'iterations_run', 0)} iterations "
          f"(cap {getattr(imp, 'iterations', getattr(args, 'iterations', '?'))})")
    print(f"  ledger violations (must be 0): {len(violations)}")
    print(f"  option conflicts (must be 0): {len(superseded_conflicts)}")
    print(f"  in-universe orders: {kpi_report.in_universe_orders} | assigned {kpi_report.assigned_orders} ({kpi_report.order_assignment_rate:.1f}%)")
    print(f"  every order accounted in manifest: {fully_accounted}")
    print(f"  run dir: {plan_dir}  (plan_full.csv + html + log at root; tables in csv/, reports in reports/)")
    return 0, len(superseded_conflicts)


def _apply_vehicle_day_cost_flags(args) -> None:
    """Map the driver-day-cost CLI flags onto the config knobs the objective reads
    at call-time (vehicle_cost.driver_day_cost). ``--vehicle-day-cost`` /
    ``--no-vehicle-day-cost`` force it on/off; absent = keep the config default."""
    if getattr(args, "vehicle_day_cost", None) is not None:
        _fp_cfg.VEHICLE_DAY_COST_ENABLED = bool(args.vehicle_day_cost)
    if getattr(args, "guaranteed_shift_hours", None) is not None:
        _fp_cfg.GUARANTEED_SHIFT_HOURS = float(args.guaranteed_shift_hours)
    if getattr(args, "overtime_cost", None) is not None:
        _fp_cfg.OVERTIME_COST_ENABLED = bool(args.overtime_cost)
    if getattr(args, "tour_depot_direct_as_delivery", None) is not None:
        _fp_cfg.TOUR_DEPOT_DIRECT_AS_DELIVERY = bool(args.tour_depot_direct_as_delivery)
    if getattr(args, "daily_depot_direct_as_delivery", None) is not None:
        _fp_cfg.DAILY_DEPOT_DIRECT_AS_DELIVERY = bool(args.daily_depot_direct_as_delivery)
    if getattr(args, "daily_depot_direct_radius_km", None) is not None:
        _fp_cfg.DAILY_ORIGIN_AT_DEPOT_RADIUS_KM = float(args.daily_depot_direct_radius_km)
    if getattr(args, "collocated_staging_min", None) is not None:
        _fp_cfg.COLLOCATED_STAGING_MIN = float(args.collocated_staging_min)
    if getattr(args, "depot_pinning", None) is not None:
        _fp_cfg.DEPOT_PINNING = bool(args.depot_pinning)
    if getattr(args, "readiness_lag_min", None) is not None:
        _fp_cfg.READINESS_LAG_MIN = float(args.readiness_lag_min)
    if getattr(args, "hard_time_windows", False):
        _fp_cfg.SOFT_DELIVERY_WINDOWS = False
    if getattr(args, "tardiness_coef", None) is not None:
        _fp_cfg.TARDINESS_COEF = float(args.tardiness_coef)
    if getattr(args, "earliness_coef", None) is not None:
        _fp_cfg.EARLINESS_COEF = float(args.earliness_coef)
    if getattr(args, "tour_osrm_durations", None) is not None:
        _fp_cfg.TOUR_OSRM_DURATIONS = bool(args.tour_osrm_durations)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Improve the greedy route seed with ALNS.")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    # Orders input FIXED to the combined enriched parquet (user rule 2026-07-22, no
    # CLI override — see run_rolling for rationale). Programmatic callers (run_rolling
    # builds Namespaces directly) may still set the `qargo` attribute.
    parser.add_argument("--postcode-cache", default=str(DEFAULT_POSTCODE_CACHE))
    parser.add_argument("--osrm-cache", default=None,
                        help="optional per-run OSRM matrix cache path")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--handover-in", default=None,
                        help="path to a prior week's handover.json (opening state); omit for cold start")
    parser.add_argument("--date-basis", choices=sorted(VALID_BASIS), default="planning_window")
    parser.add_argument("--responsibility-mode", choices=sorted(RESPONSIBILITY_MODES), default=FORWARD_STRUCTURAL)
    parser.add_argument("--iterations", type=int, default=100000, help="hard iteration cap")
    parser.add_argument("--time-budget", type=float, default=120.0, help="seconds per restart")
    parser.add_argument("--no-improve", type=int, default=4000, help="stop after this many iterations with no improvement")
    parser.add_argument("--converge-pct", type=float, default=None,
                        help="stop when best km improves < this %% over --converge-window "
                             "iterations (default: config.ALNS_CONVERGE_PCT; 0 disables)")
    parser.add_argument("--converge-window", type=int, default=None,
                        help="iterations per convergence check (default: config)")
    parser.add_argument("--converge-min-iters", type=int, default=None,
                        help="never converge-stop before this many iterations (default: config)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--restarts", type=int, default=1, help="independent ALNS restarts; best served/km plan wins")
    parser.add_argument("--log-every", type=int, default=200,
                        help="log km-vs-time every N iterations; keep well below the iters "
                             "achievable in --time-budget so the convergence curve is captured")
    parser.add_argument("--router", choices=["osrm", "haversine"], default="osrm",
                        help="road-distance provider; osrm uses the shared OSRM cache+server, haversine is the offline fallback")
    parser.add_argument("--sa-temp", type=float, default=0.005,
                        help="ALNS simulated-annealing initial temperature as a fraction of starting km "
                             "(0 = greedy hill-climb). Marginally positive once operators + throughput land "
                             "(B8): ~+0.3%% of the km saved; best-tracking means it never degrades the result")
    parser.add_argument("--sa-cooling", type=float, default=0.999,
                        help="ALNS simulated-annealing geometric cooling factor per iteration")
    parser.add_argument("--no-consolidate-tours", dest="consolidate_tours", action="store_false",
                        help="disable cross-depot tour consolidation (ON by default)")
    parser.set_defaults(consolidate_tours=True)
    parser.add_argument("--repair-every", type=int, default=20,
                        help="run the expensive coverage-repair (re-attempt the unassigned pool) "
                             "every Nth iteration; 1 = every iteration (slow). Higher = more "
                             "km-improving iterations per second within the time budget (B8 phase 2 throughput)")
    parser.add_argument("--regret-repair", action="store_true",
                        help="use regret-2 ordering when reinserting the destroyed jobs. Measured "
                             "net-negative at small removal sizes (costs ~kx the repair, fewer "
                             "iterations/budget), so OFF by default; kept for larger-destroy experiments")
    parser.add_argument("--day-flex", action="store_true",
                        help="K2 v1: allow depot-controlled FULL_FLEET deliveries to serve up to "
                             "2 days EARLIER than their historical date (never later). Default off; "
                             "off is bit-identical to pre-K2 behavior")
    parser.add_argument("--vehicle-day-cost", action=argparse.BooleanOptionalAction, default=None,
                        help="per-vehicle-day driver activation cost in the objective "
                             "(guaranteed-shift floor + overtime; default: config, ON since 2026-07-15). "
                             "--no-vehicle-day-cost = the fuel-only ablation")
    parser.add_argument("--guaranteed-shift-hours", type=float, default=None,
                        help=f"paid minimum shift hours = floor of the driver-day cost "
                             f"(default: config.GUARANTEED_SHIFT_HOURS = {_fp_cfg.GUARANTEED_SHIFT_HOURS})")
    parser.add_argument("--overtime-cost", action=argparse.BooleanOptionalAction, default=None,
                        help="overtime + late-ramp surcharges in the driver-day cost (payroll OT "
                             "beyond the paid floor + unsocial premium ramping past 19:00; default: "
                             "config, ON since 2026-07-16). --no-overtime-cost = the pre-fairness "
                             "straight-time ablation")
    parser.add_argument("--tour-depot-direct-as-delivery",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="plan a DIRECT collected at its anchor depot as a depot-loaded delivery so "
                             "same-destination far orders consolidate onto one tour (default: config, ON "
                             "since 2026-07-15). --no-tour-depot-direct-as-delivery = the pre-fix split")
    parser.add_argument("--daily-depot-direct-as-delivery",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="emit a same-day DIRECT collected AT its source depot as a depot-loaded "
                             "delivery so same-origin orders co-load (default: config, ON since "
                             "2026-07-17). --no-daily-depot-direct-as-delivery = legacy atomic arcs")
    parser.add_argument("--daily-depot-direct-radius-km", type=float, default=None,
                        help="collocation radius (km) for the daily depot-direct rule (default: config, 2.0)")
    parser.add_argument("--collocated-staging-min", type=float, default=None,
                        help="minutes from collection-window open to the freight being loadable at the "
                             "dock — the reclassified leg's departure floor (default: config, 30)")
    parser.add_argument("--depot-pinning",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="serve every pickup/delivery only with vehicles homed at its freight's "
                             "depot (default: config, ON since 2026-07-17). --no-depot-pinning = "
                             "legacy free assignment (the cross-depot teleport ablation)")
    parser.add_argument("--readiness-lag-min", type=float, default=None,
                        help="EXPERIMENT (A2): floor PL_IMPORT delivery departures to 06:00 + M minutes "
                             "(default: config, 0 = off). Floors ONLY import deliveries")
    parser.add_argument("--hard-time-windows", dest="hard_time_windows",
                        action="store_true", default=False,
                        help="ablation (2026-07-18): hard cutoff on every stated delivery deadline "
                             "instead of the default soft earliness/tardiness penalty")
    parser.add_argument("--tardiness-coef", type=float, default=None,
                        help="GBP per (minute late)^2 for the soft delivery-window penalty (default: config)")
    parser.add_argument("--earliness-coef", type=float, default=None,
                        help="GBP per minute early for the soft delivery-window penalty (default: config)")
    parser.add_argument("--tour-osrm-durations",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="time tours with OSRM per-road-type durations (default: config, ON); "
                             "--no-tour-osrm-durations reverts to the flat 50/80 km/h model")
    args = parser.parse_args(argv)
    _apply_vehicle_day_cost_flags(args)

    start, end = _parse_date(args.start), _parse_date(args.end)
    # Flattened, month-grouped layout: <out-dir>/<YYYY-MM>/<window>/ with root
    # deliverables + csv/ + md/ (see output_layout). mode/basis live in
    # run_manifest.json, not the path (suffixed onto the window only when
    # non-default so they can never collide with the default run).
    out_dir = Path(args.out_dir) / f"{start:%Y-%m}"
    window = flat_window_label(start, end, args.responsibility_mode, args.date_basis)
    _base_dir, plan_dir, reports_dir = run_dirs(out_dir, window)
    write_run_manifest(out_dir, window, {
        "runner": "run_alns",
        "window": window,
        "start": str(start),
        "end": str(end),
        "responsibility_mode": args.responsibility_mode,
        "date_basis": args.date_basis,
        "handover_in": str(args.handover_in) if args.handover_in else None,
        "qargo": str(getattr(args, "qargo", None) or DEFAULT_ENRICHED),
        "iterations": args.iterations,
        "time_budget_s": args.time_budget,
        "no_improve": args.no_improve,
        "restarts": max(1, args.restarts),
        "seed": args.seed,
        "day_flex": bool(args.day_flex),
        "env_toggles": _env_toggles(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })

    runlog = RunLog(reports_dir / "alns_progress.log")
    runlog.log(
        f"window {start}..{end} iterations={args.iterations} seed={args.seed} restarts={max(1, args.restarts)}"
    )

    inputs = build_window_inputs(args, start, end, runlog)
    result = solve_window(args, start, inputs, runlog)
    rc, _option_conflicts = emit_outputs(args, start, end, inputs, result, plan_dir, reports_dir, runlog)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
