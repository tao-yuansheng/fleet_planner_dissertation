"""Milestone 5: ALNS improvement layer (self-contained, under freight_planner/).

A destroy-and-repair large-neighbourhood search that improves the greedy
route-seed solution. Unlike the first cut, the search state now carries both
assigned jobs and a repairable unassigned pool, so ALNS can insert rejected
jobs and optimise the existing routes inside the same loop.

The solution model is multi-trip aware: each (vehicle, day) maps to a list of
trips, and each trip is a depot->stops->depot loop. Capacity resets per trip;
shift time and driving cap are evaluated across the full vehicle-day.

Guarantees (current M5 behaviour):
  * same selected-job schema as the seed (emits SelectedPlanRecord);
  * hard blockers and pickup-before-delivery dependencies remain respected;
  * acceptance is lexicographic: increase served jobs first, then reduce km;
  * route-cost improvement (km) is still reported separately from coverage.
"""
from __future__ import annotations

import math
import os
import random
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from time import monotonic
from types import SimpleNamespace

import pandas as pd

from freight_planner.option_mutex import OptionMutex
from freight_planner.plan_records import build_plan_records
from freight_planner.plan_schema import SelectedPlanRecord
from freight_planner.planner_state import RejectedJob
from freight_planner.route_seed import (
    _DEP_RANK,
    _job_coords,
    _ok_options,
    _reorder,
    _slip_rank as _seed_slip_rank,
    make_route_job,
    same_order_handoff_conflict,
)
from freight_planner.catchment import job_distance_km
from freight_planner import config as _fp_cfg
from freight_planner.config import (ALNS_CONVERGE_MIN_ITERS, ALNS_CONVERGE_PCT,
                                    ALNS_CONVERGE_WINDOW, MERGE_SWEEP_ENABLED,
                                    SCARCE_DEPOT_HEADROOM_GBP)
from freight_planner.dayflex import shifted_route_job
from freight_planner.merge_sweep import apply_zero_cost_merges
from freight_planner.route_costs import road_km
from freight_planner.shared.scope import SPOKE_DELIVERY_RADIUS_KM as _SPOKE_RADII
from freight_planner.routing_adapter import (
    RouteJob, RouteVehicle, apply_avail_override, evaluate_day, try_insert_job,
)
from freight_planner.vehicle_cost import (
    driver_day_cost,
    driver_day_cost_ev,
    fuel_cost_per_km,
    road_cost_per_km,
    out_of_area_penalty_km,
    vehicle_day_cost_enabled,
)

_EPS = 1e-6
# OPTION_SUPERSEDED (2026-07-28): the seed's DIRECT/XDOCK pick is pure insertion
# ORDER (_DEP_RANK always processes XDOCK's pickup before DIRECT's leg), never a
# cost comparison -- the loser is rejected before its cost is even computed. This
# reason must stay repairable so the loser re-enters `unassigned` and ALNS's
# option_swap operator gets a genuine, cost-based chance to swap it back in.
# Without it, DIRECT can only ever be chosen when it happens to rank first --
# which for an ordinary same-day FULL_FLEET pair, essentially never happens
# (192/192 option sets resolved to XDOCK before this fix, in a real run).
_REPAIRABLE_REASONS = {"SHIFT", "DRIVING_CAP", "TIME_WINDOW", "NO_FEASIBLE_ROUTE", "EXCESS_WAIT",
                       "OPTION_SUPERSEDED"}

# B16 diagnostics: FP_ALNS_CONSERVE=1 asserts job conservation after every
# accepted move and at the search/emission boundaries, so a silent job loss
# halts the run at the exact move instead of surfacing as a coverage drop.
_CONSERVE = os.environ.get("FP_ALNS_CONSERVE", "").strip() == "1"


def _pinned_check(routes: dict, pinned: frozenset, where: str) -> None:
    assigned = {j.job_id for trips in routes.values() for t in _as_trips(trips) for j in t}
    missing = pinned - assigned
    assert not missing, f"pinned jobs missing from solution at {where}: {sorted(missing)[:10]}"


def _conserve_check(routes: dict, job_loc: dict, unassigned: dict, label: str) -> None:
    counts: dict[str, int] = {}
    for trips in routes.values():
        for j in _flatten(_as_trips(trips)):
            counts[j.job_id] = counts.get(j.job_id, 0) + 1
    in_routes = set(counts)
    in_loc = set(job_loc)
    dups = sorted(jid for jid, n in counts.items() if n > 1)
    lost = sorted(in_loc - in_routes)
    ghost = sorted(in_routes - in_loc)
    overlap = sorted(in_routes & set(unassigned))
    if dups or lost or ghost or overlap:
        raise AssertionError(
            f"ALNS job conservation broken ({label}): "
            f"lost(in job_loc, not in routes)={lost[:20]} "
            f"ghost(in routes, not in job_loc)={ghost[:20]} "
            f"dups={dups[:20]} in_both_routes_and_pool={overlap[:20]}"
        )

# ---- destroy operators (B8 phase 2: real ALNS neighbourhood) ----------------
# The first cut only ever removed jobs at random, then greedily re-inserted them,
# which mostly snapped each job back where it started (the search was inert). These
# operators give the search a gradient: pull out the *costly* jobs (worst removal)
# or a *related cluster* (Shaw removal) so the greedy repair has somewhere better to
# put them. Operator choice is governed by adaptive weights (_AdaptiveOps).
_DESTROY_OPS = ("random", "worst", "shaw", "option_swap")


# ---- experiment-only env toggles (Ch.5 ablations; see experiments/README.md) --
# ALL default-off: with no FP_ALNS_* env set, every code path below reproduces the
# original behavior exactly (bit-identical checkpoint fingerprint). Restore notes:
# freight_planner/experiments/code_snapshots/RESTORE.md.

def _removal_band() -> tuple[int, int]:
    """Destroy removal-size band (k drawn uniformly in [lo, hi]); default 2..5."""
    lo = int(os.environ.get("FP_ALNS_REMOVAL_MIN", "2"))
    hi = int(os.environ.get("FP_ALNS_REMOVAL_MAX", "5"))
    if lo < 1 or hi < lo:
        raise ValueError(f"invalid removal band FP_ALNS_REMOVAL_MIN..MAX = {lo}..{hi}")
    return lo, hi


def _active_destroy_ops() -> tuple[str, ...]:
    """Destroy-operator set; default all three. FP_ALNS_DESTROY_OPS='worst' etc."""
    raw = os.environ.get("FP_ALNS_DESTROY_OPS", "").strip()
    if not raw:
        return _DESTROY_OPS
    names = tuple(n.strip() for n in raw.split(",") if n.strip())
    unknown = [n for n in names if n not in _DESTROY_OPS]
    if unknown or not names:
        raise ValueError(f"FP_ALNS_DESTROY_OPS unknown ops {unknown}; allowed {_DESTROY_OPS}")
    return names


def _read_accept_env() -> tuple[str, float]:
    """Worse-move acceptance criterion: 'sa' (default, original behavior) or 'rrt'
    (record-to-record travel, Dueck: accept iff candidate < best*(1+deviation))."""
    mode = os.environ.get("FP_ALNS_ACCEPT", "sa").strip().lower() or "sa"
    if mode not in {"sa", "rrt"}:
        raise ValueError(f"FP_ALNS_ACCEPT must be 'sa' or 'rrt', got {mode!r}")
    deviation = float(os.environ.get("FP_ALNS_RRT_DEVIATION", "0.02"))
    return mode, deviation


def _accept_worse(mode: str, rrt_deviation: float, *, candidate_total: float,
                  best_total: float, temperature: float, rng: random.Random,
                  delta: float = 0.0) -> bool:
    """Accept a worsening move? 'sa' draws rng ONLY when temperature > 0 (exactly
    the original consumption pattern — bit-identical when defaults); 'rrt' is a
    deterministic threshold on the best-so-far and never touches the rng stream."""
    if mode == "rrt":
        return candidate_total < best_total * (1.0 + rrt_deviation)
    if temperature > 0.0:
        return rng.random() < math.exp(-delta / temperature)
    return False


def _roulette_pick(ranked_ids: list[str], k: int, rng: random.Random, randomness: float) -> list[str]:
    """Pick k ids from a list ranked best-first, biased toward the front by y**p
    (Ropke & Pisinger): a larger ``randomness`` exponent ~ deterministic top pick."""
    chosen: list[str] = []
    pool = list(ranked_ids)
    while pool and len(chosen) < k:
        idx = int((rng.random() ** randomness) * len(pool))
        if idx >= len(pool):
            idx = len(pool) - 1
        chosen.append(pool.pop(idx))
    return chosen


def committed_job_ids(routes: dict, watermarks: dict | None) -> set[str]:
    """Job ids at or before their trip's commitment watermark (spec 4.7a):
    unioned into the pinned set so no destroy operator can ever remove them.
    ``watermarks``: {(vid, day) -> tuple of committed stop counts per trip
    position}. Trip-index alignment is safe because commitment is chronological
    — committed trips precede free ones, and only fully-free trips can empty
    and drop out of the list."""
    if not watermarks:
        return set()
    out: set[str] = set()
    for key, counts in watermarks.items():
        trips = _as_trips(routes.get(key, []))
        for t_idx, c in enumerate(counts):
            if t_idx >= len(trips):
                continue
            for j in trips[t_idx][: int(c)]:
                out.add(j.job_id)
    return out


def _retimes_committed_departure(day_ev, base_starts, idx: int, minpos: int) -> bool:
    """B2 suffix guard: a launched trip's committed stops are fact — inserting into
    its open suffix may not MOVE the trip's departure. The trip-wide depart_floor
    (2026-07-17) is the one mover a suffix insertion can introduce: a floored job
    joining a committed trip would silently re-time the whole committed prefix
    (floor_ok only checks the re-timed deviation point AGAINST the floor, not that
    it stayed put). Refuse the position; the job falls through to a fresh trip."""
    if minpos <= 0 or not base_starts or idx >= len(base_starts):
        return False
    tevs = day_ev.trip_evaluations
    return idx < len(tevs) and str(tevs[idx].route_start) != str(base_starts[idx])


def floor_ok(day_ev, committed_counts: tuple, floor, now=None) -> bool:
    """No plannable work — INCLUDING THE DRIVING TOWARD IT — may happen before
    ``floor`` = now + delta_R1 (spec 4.7a): a plan computed now cannot reach a
    driver faster than that. Fully committed trips are fact (skip); untouched/new
    trips must not even start before the floor. A partially committed trip's
    suffix is open ONLY while its deviation point — the LAST COMMITTED stop's
    departure, the moment the driver's remaining plan first changes — is itself
    at/after the floor (**departure-based flooring**, user rule 2026-07-14 /
    WT255677: flooring arrivals alone is structurally leaky — an order 100 min of
    driving away always ARRIVES outside a 90-min freeze, yet the truck must start
    driving toward it inside it). Suffix arrivals must also clear the floor; each
    later suffix drive then chains compliant from the prior stop's departure.
    When ``now`` is given, a suffix whose last committed stop has already
    departed (< now) is additionally rejected as a return-leg violation
    (2026-07-13; subsumed by the departure floor whenever now <= floor, kept as
    defense in depth). Requires a detail=True evaluation (per-stop strings)."""
    for t_idx, tev in enumerate(day_ev.trip_evaluations):
        stops = tev.stops or ()
        c = int(committed_counts[t_idx]) if t_idx < len(committed_counts) else 0
        if c >= len(stops) and c > 0:
            continue                      # fully committed: already history
        if c == 0:
            try:
                if datetime.fromisoformat(str(tev.route_start)) < floor:
                    return False
            except (TypeError, ValueError):
                return False
            continue
        # 0 < c < len: partially committed. The truck deviates from its committed
        # plan the moment it departs its last committed stop — that departure is
        # the first thing a suffix change alters, so it must sit at/after the
        # floor (and, with `now`, must not already be behind the truck).
        try:
            last_committed_depart = datetime.fromisoformat(str(stops[c - 1].depart))
        except (TypeError, ValueError):
            return False
        if last_committed_depart < floor:
            return False
        if now is not None and last_committed_depart < now:
            return False
        for s in stops[c:]:
            try:
                if datetime.fromisoformat(str(s.arrive)) < floor:
                    return False
            except (TypeError, ValueError):
                return False
    return True


def _floor_guard_active(commit_floor, wm, day) -> bool:
    """Whether the E6 floor/watermark guard arms for this (key, day) insertion.

    Historically it armed only for keys WITH a watermark entry — so a key that
    never launched (no watermark) was never floor-checked, and a seed could
    plant an unserved candidate on YESTERDAY's vehicle-day (route-backdating
    mechanism a, 2026-07-14). Now: any key whose day is on/before the floor's
    own day is checked too — floor_ok's c==0 branch requires every fresh trip
    to START at/after the floor, which a past-day trip never can. Future-day
    keys skip the (detail=True) check: their trips trivially clear the floor."""
    if commit_floor is None:
        return False
    return bool(wm) or str(day)[:10] <= commit_floor.date().isoformat()


def _coord_index(routes: dict) -> tuple[dict[str, tuple[float, float]], dict[str, str]]:
    coord: dict[str, tuple[float, float]] = {}
    day_of: dict[str, str] = {}
    for (_vid, day), trips in routes.items():
        for j in _flatten(_as_trips(trips)):
            coord[j.job_id] = (j.lat, j.lon)
            day_of[j.job_id] = day
    return coord, day_of


def _worst_removal(
    routes: dict,
    vehicle_meta: dict[str, VehicleMeta],
    k: int,
    rng: random.Random,
    *,
    randomness: float = 3.0,
    pinned: frozenset = frozenset(),
) -> list[str]:
    """Remove the jobs that add the most in-trip detour, so repair can rehome them.

    Detour(j) = d(prev, j) + d(j, next) - d(prev, next), with the depot as the
    anchor at trip ends. Cheap (no full day re-evaluation) and uses the same
    road_km the optimizer costs with. Pinned jobs (shuttle carve-out) are excluded
    from the ranking itself — filtering after the roulette pick would silently
    shrink k and distort the adaptive-weight rewards."""
    scored: list[tuple[float, str]] = []
    for (vid, day), trips in routes.items():
        vm = vehicle_meta.get(vid)
        anchor = (vm.lat, vm.lon) if vm is not None else None
        for trip in _as_trips(trips):
            n = len(trip)
            for i, j in enumerate(trip):
                if j.job_id in pinned:
                    continue
                prev = anchor if i == 0 else (trip[i - 1].lat, trip[i - 1].lon)
                nxt = anchor if i == n - 1 else (trip[i + 1].lat, trip[i + 1].lon)
                if prev is None or nxt is None:
                    detour = 0.0
                else:
                    detour = (
                        road_km(prev[0], prev[1], j.lat, j.lon)
                        + road_km(j.lat, j.lon, nxt[0], nxt[1])
                        - road_km(prev[0], prev[1], nxt[0], nxt[1])
                    )
                scored.append((detour, j.job_id))
    scored.sort(key=lambda t: t[0], reverse=True)
    return _roulette_pick([jid for _, jid in scored], k, rng, randomness)


def _shaw_removal(
    routes: dict,
    k: int,
    rng: random.Random,
    *,
    randomness: float = 5.0,
    pinned: frozenset = frozenset(),
) -> list[str]:
    """Remove a spatially related cluster of *same-day* jobs (Shaw relatedness) so the
    repair can re-consolidate them onto one passing vehicle. Stays within a single day
    because each job's service day is fixed — cross-day removal can never consolidate.

    Pinned jobs (shuttle carve-out) are excluded from BOTH the anchor choice and the
    candidate pool: on a shuttle-heavy day the tightest cluster is often the pinned
    one, and anchoring there would yield an all-pinned pick that post-filters to an
    empty removal (a wasted iteration)."""
    coord, day_of = _coord_index(routes)
    all_ids = [jid for jid in coord if jid not in pinned]
    if not all_ids:
        return []
    seed = rng.choice(all_ids)
    seed_day = day_of[seed]
    chosen = [seed]
    pool = [jid for jid in all_ids if day_of[jid] == seed_day and jid != seed]
    while pool and len(chosen) < k:
        ref = rng.choice(chosen)
        rx, ry = coord[ref]
        pool.sort(key=lambda jid: road_km(rx, ry, coord[jid][0], coord[jid][1]))
        idx = int((rng.random() ** randomness) * len(pool))
        if idx >= len(pool):
            idx = len(pool) - 1
        chosen.append(pool.pop(idx))
    return chosen


class _AdaptiveOps:
    """Adaptive operator selection (the 'A' in ALNS). Each operator earns score when
    its move is accepted; weights are periodically blended toward recent performance so
    the search leans on whatever is currently paying off."""

    # Ropke & Pisinger reward tiers.
    REWARD_BEST = 33.0      # produced a new global best
    REWARD_BETTER = 9.0     # improved the current solution (not a new best)
    REWARD_ACCEPTED = 13.0  # accepted though worse (SA escape)

    def __init__(self, names, rng: random.Random, reaction: float = 0.1):
        self.names = list(names)
        self.rng = rng
        self.reaction = float(reaction)
        self.weights = {n: 1.0 for n in self.names}
        self._score = {n: 0.0 for n in self.names}
        self._uses = {n: 0 for n in self.names}
        # Experiment toggle (E3 ablation): freeze weights at uniform — selection
        # becomes a uniform roulette; scores still tallied, never blended in.
        self.adaptive = os.environ.get("FP_ALNS_UNIFORM_WEIGHTS", "").strip() not in {"1", "true", "yes"}

    def select(self) -> str:
        total = sum(self.weights.values())
        if total <= 0.0:
            return self.rng.choice(self.names)
        r = self.rng.random() * total
        upto = 0.0
        for n in self.names:
            upto += self.weights[n]
            if r <= upto:
                return n
        return self.names[-1]

    def reward(self, name: str, amount: float) -> None:
        if name in self._score:
            self._score[name] += amount
            self._uses[name] += 1

    def update_weights(self) -> None:
        for n in self.names:
            if self.adaptive and self._uses[n] > 0:
                avg = self._score[n] / self._uses[n]
                self.weights[n] = max(0.01, (1.0 - self.reaction) * self.weights[n] + self.reaction * avg)
            self._score[n] = 0.0
            self._uses[n] = 0


@dataclass
class VehicleMeta:
    vehicle_id: str
    home_depot: str
    lat: float
    lon: float
    available_from: str
    shift_end: str
    capacity_pallets: float
    capacity_kg: float
    vehicle_type: str
    # RETIRED 2026-07-16 (user rule: no trip-count cap) — kept only so existing
    # frames/tests constructing VehicleMeta with these kwargs stay valid; nothing
    # reads them.
    median_trips_per_day: int = 1
    multi_trip_share: float = 0.0
    # B15 service-area radius (km). 0.0 = unknown/uncalibrated -> penalty disabled,
    # so existing callers that never set it see zero behaviour change.
    catchment_km: float = 0.0


@dataclass
class JobMeta:
    rjob: RouteJob
    day: str
    eligible_vehicles: list[str]
    candidate: dict
    # K2: earlier allowed (day, window-shifted RouteJob) variants, ascending by
    # day; empty = pinned (default, and always when --day-flex is off).
    flex_variants: tuple[tuple[str, RouteJob], ...] = ()


def _insertion_days(meta: JobMeta) -> tuple[tuple[str, RouteJob], ...]:
    """(day, rjob) pairs the search may place this job on. Nominal day LAST so
    earlier days are tried first; the best-delta comparison makes order
    irrelevant to the result, ties break toward earlier days deterministically."""
    return meta.flex_variants + ((meta.day, meta.rjob),)


def _flex_variants_for(cand: dict, rjob: RouteJob, day_flex: bool) -> tuple:
    """Pre-built per-day RouteJob variants for an eligible candidate (K2).
    Empty when the flag is off or the job is pinned (day_flex_min unset)."""
    if not day_flex:
        return ()
    dmin = str(cand.get("day_flex_min", "") or "")
    due = str(cand.get("service_date", "") or "")[:10]
    if not dmin or not due or dmin >= due:
        return ()
    out = []
    d = date.fromisoformat(dmin)
    stop = date.fromisoformat(due)
    while d < stop:
        var = shifted_route_job(rjob, cand, d.isoformat())
        if var is not None:
            out.append((d.isoformat(), var))
        d += timedelta(days=1)
    return tuple(out)


@dataclass
class ConvergenceGate:
    """Stop when the best objective improved by less than ``pct`` PERCENT over
    the last ``window`` iterations, checked in whole windows, once past
    ``min_iters`` (the noisy warm-up must never trip it). ``pct <= 0``
    disables. A served-count increase always counts as improvement — coverage
    is never traded away for wall-clock (user rule 2026-07-13; the absolute
    no-improve patience never fired because 0.01-km gains kept resetting it).

    ``frozen_cost`` (2026-07-24) is the portion of the objective the search
    CANNOT move — committed/pinned trips carried in a warm-started rolling
    solution. The percentage is measured against the IMPROVABLE base
    (``total - frozen_cost``), not the whole total: without this, a later epoch
    whose warm start already carries a big frozen cost sees the same absolute
    optimization register as a smaller percentage, so a fixed ``pct`` trips the
    gate prematurely and the epoch's fresh work is starved of iterations. The
    numerator is already pure improvable-cost improvement (frozen can't change),
    so dividing by the improvable base makes ``pct`` invariant to how much has
    frozen. Defaults to 0.0 → identical to the pre-2026-07-24 whole-total gate
    (the static, non-rolling path has no frozen cost)."""
    pct: float
    window: int
    min_iters: int = 0
    frozen_cost: float = 0.0
    _mark_it: int = 0
    _mark_total: float = 0.0
    _mark_served: int = -1

    def should_stop(self, it: int, best_total: float, best_served: int) -> bool:
        if self.pct <= 0 or self.window <= 0:
            return False
        if self._mark_served < 0:              # first call arms the mark
            self._mark_it, self._mark_total, self._mark_served = it, best_total, best_served
            return False
        if it - self._mark_it < self.window:
            return False
        base = self._mark_total - self.frozen_cost   # improvable headroom, not whole total
        improved = (best_served > self._mark_served
                    or (base > 0
                        and (self._mark_total - best_total) / base * 100.0 >= self.pct))
        self._mark_it, self._mark_total, self._mark_served = it, best_total, best_served
        if it < self.min_iters:
            return False
        return not improved


@dataclass
class SolutionImprovement:
    solution: dict
    km_before: float
    km_after: float
    served_before: int
    served_after: int
    accepted_moves: int
    iterations: int
    inserted_jobs: int = 0
    attempted_jobs: int = 0
    remaining_unassigned: list[str] = field(default_factory=list)
    # The search optimises generalized cost (per-type GBP); km_* stay physical.
    cost_before: float = 0.0
    cost_after: float = 0.0
    stop_reason: str = ""            # iterations | converged | time_budget | no_improve | drained
    iterations_run: int = 0
    seed_by_type: dict = field(default_factory=dict)   # §6.3b seed veh-days + km per type


@dataclass
class RouteSeedImprovement:
    selected: list[SelectedPlanRecord]
    km_before: float
    km_after: float
    served_before: int
    served_after: int
    accepted_moves: int
    route_totals: dict = field(default_factory=dict)
    route_times: dict = field(default_factory=dict)
    solution: dict = field(default_factory=dict)
    inserted_jobs: int = 0
    attempted_jobs: int = 0
    remaining_rejected: list[RejectedJob] = field(default_factory=list)
    cost_before: float = 0.0
    cost_after: float = 0.0
    merge_sweep: object = None
    stop_reason: str = ""
    iterations_run: int = 0
    cost_decomposition: list = field(default_factory=list)  # per (v,d) objective-cost terms (§6.4)
    seed_by_type: dict = field(default_factory=dict)        # §6.3b seed veh-days + km per type


@dataclass
class _CandidatePlan:
    work: dict
    placements: list[tuple[str, tuple[str, str]]]
    delta_km: float
    served_gain: int
    inserted_jids: list[str]
    attempted_jobs: int


def _time_of(ts: str, fallback: time) -> time:
    if not ts:
        return fallback
    try:
        return datetime.fromisoformat(str(ts)).time()
    except ValueError:
        return fallback


def _route_vehicle(vm: VehicleMeta, day: str) -> RouteVehicle:
    start_t = _time_of(vm.available_from, time(int(_fp_cfg.FLEET_DAY_START_HOUR), 0))
    d = date.fromisoformat(day) if day else date(2026, 1, 1)
    # A blank shift_end means NO wall (user rule 2026-07-16: duty/driving caps
    # bind, 19:00 is soft) — never invent one. A set value keeps its wall.
    se = str(vm.shift_end or "")
    return RouteVehicle(
        vehicle_id=vm.vehicle_id,
        start_node=vm.home_depot,
        start_lat=vm.lat,
        start_lon=vm.lon,
        start_time=datetime.combine(d, start_t).isoformat(sep=" "),
        capacity_pallets=vm.capacity_pallets,
        capacity_kg=vm.capacity_kg,
        vehicle_type=vm.vehicle_type,
        home_depot=vm.home_depot,
        home_lat=vm.lat,
        home_lon=vm.lon,
        return_to_depot=True,
        shift_end=datetime.combine(d, _time_of(se, time(18, 0))).isoformat(sep=" ") if se else "",
    )


def _rv_ov(vm: VehicleMeta, day: str, overrides: dict[tuple[str, str], object] | None) -> RouteVehicle:
    """``_route_vehicle`` plus a per-(vehicle_id, day) availability override.
    Values: "HH:MM" (T1 trunk next-day delay) or a routing_adapter.DutyOverride
    (E6 rolling: post-freeze start plus duty carry). Application semantics live
    in routing_adapter.apply_avail_override, shared with route_seed's closure."""
    veh = _route_vehicle(vm, day)
    return apply_avail_override(veh, (overrides or {}).get((vm.vehicle_id, day)), day)


def _as_trips(value) -> list[list[RouteJob]]:
    if not value:
        return []
    first = value[0]
    if isinstance(first, RouteJob):
        return [list(value)]
    return [list(trip) for trip in value if trip]


def _flatten(trips: list[list[RouteJob]]) -> list[RouteJob]:
    return [job for trip in trips for job in trip]


def _build_vehicle_meta(vehicles: pd.DataFrame) -> dict[str, VehicleMeta]:
    out: dict[str, VehicleMeta] = {}
    for row in vehicles.itertuples(index=False):
        vid = str(getattr(row, "vehicle_id", "") or "")
        if not vid:
            continue
        out[vid] = VehicleMeta(
            vehicle_id=vid,
            home_depot=str(getattr(row, "home_depot", "") or ""),
            lat=float(getattr(row, "current_lat", 0.0) or 0.0),
            lon=float(getattr(row, "current_lon", 0.0) or 0.0),
            available_from=str(getattr(row, "available_from", "") or ""),
            shift_end=str(getattr(row, "shift_end", "") or ""),
            capacity_pallets=float(getattr(row, "capacity_pallets", 0.0) or 0.0),
            capacity_kg=float(getattr(row, "capacity_kg", 0.0) or 0.0),
            vehicle_type=str(getattr(row, "vehicle_type", "") or ""),
            median_trips_per_day=int(float(getattr(row, "median_trips_per_day", 1) or 1)),
            multi_trip_share=float(getattr(row, "multi_trip_share", 0.0) or 0.0),
            catchment_km=float(getattr(row, "catchment_km", 0.0) or 0.0),
        )
    return out


def _synth_candidate(rjob, day) -> dict:
    """A minimal candidate carrying the RouteJob's OWN identity, for a job that is
    in the solution but absent from the candidate frame (e.g. an afternoon
    micro-inserted collection emitted at finalize against the last anchor's
    frame). Without this the frame lookup returned ``{}`` and emission read an
    empty job_id, colliding as DuplicateAssignmentError '' when two such jobs
    met on one plan."""
    jid = str(rjob.job_id)
    leg = jid[4:] if jid.startswith("JOB:") else jid
    return {
        "job_id": jid, "leg_id": leg,
        "order_id": str(getattr(rjob, "order_id", "") or ""),
        "leg_kind": str(rjob.leg_kind), "service_date": str(day),
        "service_pc": str(rjob.node),
        "pallets": float(rjob.pallets), "weight_kg": float(rjob.kg),
        "earliest_start": str(getattr(rjob, "earliest_start", "") or ""),
        "latest_finish": str(getattr(rjob, "latest_finish", "") or ""),
        "preferred_start_node": "", "preferred_end_node": "",
    }


def _build_job_meta(solution: dict, candidates: pd.DataFrame, compatibility: pd.DataFrame,
                    day_flex: bool = False) -> dict[str, JobMeta]:
    options = _ok_options(compatibility)
    cand_by_job = {str(r.get("job_id")): r for r in candidates.to_dict("records")}
    out: dict[str, JobMeta] = {}
    for (_vid, day), trips in solution.items():
        for rjob in _flatten(_as_trips(trips)):
            cand = cand_by_job.get(rjob.job_id)
            if not cand:
                cand = _synth_candidate(rjob, day)
            leg_id = str(cand.get("leg_id", ""))
            opts = options.get(leg_id, [])
            eligible = [v for v, same in opts if same] + [v for v, same in opts if not same]
            out[rjob.job_id] = JobMeta(rjob=rjob, day=day, eligible_vehicles=eligible, candidate=cand,
                                       flex_variants=_flex_variants_for(cand, rjob, day_flex))
    return out


def _build_all_candidate_meta(candidates: pd.DataFrame, compatibility: pd.DataFrame,
                              day_flex: bool = False) -> dict[str, JobMeta]:
    coords = _job_coords(compatibility)
    options = _ok_options(compatibility)
    out: dict[str, JobMeta] = {}
    for cand in candidates.to_dict("records"):
        jid = str(cand.get("job_id", "") or "")
        leg_id = str(cand.get("leg_id", "") or "")
        if not jid or not leg_id:
            continue
        rjob = make_route_job(SimpleNamespace(**cand), coords)
        if rjob is None:
            continue
        opts = options.get(leg_id, [])
        eligible = [v for v, same in opts if same] + [v for v, same in opts if not same]
        out[jid] = JobMeta(
            rjob=rjob,
            day=str(cand.get("service_date", "") or ""),
            eligible_vehicles=eligible,
            candidate=cand,
            flex_variants=_flex_variants_for(cand, rjob, day_flex),
        )
    return out


def _priority_key(meta: JobMeta) -> tuple:
    cand = meta.candidate
    return (
        # E6 aging: slipped orders outrank fresh work (0.0 when absent -> unchanged;
        # route_seed._slip_rank guards the NaN a mixed frame produces)
        -_seed_slip_rank(cand.get("slip_priority", 0.0)),
        str(cand.get("service_date", "") or ""),
        _DEP_RANK.get(str(cand.get("dependency_type", "") or ""), 5),
        str(cand.get("latest_finish", "") or "~"),
        -float(cand.get("pallets", 0.0) or 0.0),
        str(cand.get("job_id", "") or ""),
    )


def _duty_hours(day_ev) -> float:
    """On-duty span (hours) of an evaluated vehicle-day, from its start/end
    stamps. 0.0 for an empty or degenerate day. This is the paid shift length
    fed to ``driver_day_cost``."""
    start = getattr(day_ev, "day_start", "") or ""
    end = getattr(day_ev, "day_end", "") or ""
    if not start or not end:
        return 0.0
    try:
        a = datetime.fromisoformat(str(start))
        b = datetime.fromisoformat(str(end))
    except ValueError:
        return 0.0
    return max(0.0, (b - a).total_seconds() / 3600.0)


def route_km(trips, vm: VehicleMeta, day: str) -> float:
    # T1: not threaded with avail_overrides on purpose -- this feeds route_cost /
    # solution_cost, a standalone reporting utility over an already-fixed solution
    # (used by tests and external callers), not a placement decision inside
    # improve_solution's search loop. The search's own rv() closures (which decide
    # what CAN be placed) all route through _rv_ov.
    normalised = _as_trips(trips)
    if not normalised:
        return 0.0
    return evaluate_day(_route_vehicle(vm, day), normalised).total_km


def _oa_penalty_km(vm: VehicleMeta, rjob: RouteJob) -> float:
    """B15: phantom ranking-km for one job beyond ``vm``'s catchment radius.

    Zero when the vehicle has no calibrated radius (catchment_km <= 0), so the
    penalty is opt-in per vehicle and existing callers see no change."""
    if vm.catchment_km <= 0.0:
        return 0.0
    return out_of_area_penalty_km(job_distance_km(vm.lat, vm.lon, rjob), vm.catchment_km)


# Scarce/dedicated depot pools (tiny fleets, e.g. Stoke's 5 tractors) that must not
# be silently consumed by the ALNS insertion delta when a large-depot alternative
# exists for the same job (decision-audit #9, 2026-07-26). Mirrors tours.py's
# _SCARCE_TOUR_DEPOTS -- same set, same concern, different decision point.
_SCARCE_DEPOTS: frozenset = frozenset(_SPOKE_RADII)


def _scarce_depot_headroom(vm: VehicleMeta, eligible_vehicles, vehicle_meta: dict[str, VehicleMeta]) -> float:
    """Opportunity-cost surcharge (GBP) for using a scarce-depot vehicle when a
    non-scarce alternative is eligible for this same job (decision-audit #9,
    2026-07-26): the marginal-cost-only delta let an already-active, floor-sunk
    scarce-depot vehicle beat an idle large-depot vehicle purely because
    activating a fresh vehicle-day costs more than adding to a busy one --
    silently consuming scarce capacity a later scarce-exclusive job then found
    gone. Zero when the candidate isn't scarce, or when it's the only fit
    (coverage must never drop because of this)."""
    if vm.home_depot not in _SCARCE_DEPOTS:
        return 0.0
    has_alternative = any(vehicle_meta[v].home_depot not in _SCARCE_DEPOTS
                          for v in eligible_vehicles if v in vehicle_meta)
    return SCARCE_DEPOT_HEADROOM_GBP if has_alternative else 0.0


def _trips_penalty_km(trips, vm: VehicleMeta) -> float:
    """Sum of per-job out-of-area phantom km across a vehicle-day's trips."""
    if vm.catchment_km <= 0.0:
        return 0.0
    return sum(_oa_penalty_km(vm, j) for j in _flatten(_as_trips(trips)))


def _range_overage_cost(vt: str, day_km: float) -> float:
    """Daily-range soft cost (2026-07-21; rate merged into road_cost_per_km
    2026-07-27): the fuel-only per-km cost made rigids cheaper everywhere,
    inverting reality's role split (plan rigids 44% of days >300 km vs the
    telematics 13%; tractors are the long-range workhorses at a median
    351 km/day). A rigid/van day beyond its DAILY_RANGE_SOFT_KM prior pays its
    own live road_cost_per_km(vt) AGAIN per excess km — soft, so a genuine
    capacity crunch can still stretch one; tractors are unbounded. Using the
    type's own rate (rather than an unrelated flat GBP/km) ties the overage to
    the same recalibratable fuel+R&M rate the distance term already prices,
    and it self-scales sensibly per type: rigid's doubled rate (0.71/km)
    already exceeds tractor's flat rate (0.49/km), giving ALNS a genuine
    reason to prefer a tractor for long days rather than an arbitrary knob."""
    from freight_planner import config as _cfg
    cap = _cfg.DAILY_RANGE_SOFT_KM.get(str(vt).lower())
    if cap is None:
        return 0.0
    return road_cost_per_km(vt) * max(0.0, float(day_km) - float(cap))


def _day_nonkm_cost(vt: str, day_ev) -> float:
    """The non-km portion of a vehicle-day's generalized cost: driver day/overtime
    cost plus the soft delivery-window earliness/tardiness penalty (2026-07-18)
    plus the per-type daily-range overage (2026-07-21). Bundling them here keeps
    every objective site (route_cost + the incremental insert deltas) consistent."""
    return (driver_day_cost_ev(vt, day_ev) + float(getattr(day_ev, "lateness_cost", 0.0))
            + _range_overage_cost(vt, float(getattr(day_ev, "total_km", 0.0) or 0.0)))


def route_cost(trips, vm: VehicleMeta, day: str) -> float:
    """Per-type generalized cost (GBP) of a route = fuel+R&M rate x (road-km +
    out-of-area phantom km), plus the driver-day activation cost when enabled
    (a guaranteed-shift floor + overtime; 0.0 when disabled).

    Real and phantom km share one live rate, ``road_cost_per_km``. (A separate
    frozen reference rate existed briefly, 2026-07-27, to insulate the penalty
    from the FP_MAINT_MULT R&M ablation; removed once that ablation showed
    negligible effect and the driver-cost recalibration corrected the
    objective's cost composition.)

    This is the optimizer's objective unit. Reported plan distance stays physical
    km (see ``_route_totals_from_solution``); only the search ranks on cost."""
    base = road_cost_per_km(vm.vehicle_type) * (route_km(trips, vm, day) + _trips_penalty_km(trips, vm))
    normalised = _as_trips(trips)
    if not normalised:
        return base
    day_ev = evaluate_day(_route_vehicle(vm, day), normalised)
    return base + _day_nonkm_cost(vm.vehicle_type, day_ev)


def solution_cost(solution: dict, vehicle_meta: dict[str, VehicleMeta]) -> float:
    total = 0.0
    for (vid, day), trips in solution.items():
        vm = vehicle_meta.get(vid)
        if vm is not None:
            total += route_cost(trips, vm, day)
    return total


def _served(solution: dict) -> set[str]:
    out: set[str] = set()
    for trips in solution.values():
        out.update(j.job_id for j in _flatten(_as_trips(trips)))
    return out


def _has_option_sets(job_loc, unassigned, cand_of) -> bool:
    """True if any placed or unassigned job carries an option_set — i.e. an
    endogenous DIRECT/XDOCK choice exists. When false, the OptionSwap operator is
    filtered out so option-free runs are bit-identical to the pre-endogenous code.
    """
    for jid in job_loc:
        if str((cand_of(jid) or {}).get("option_set", "") or ""):
            return True
    for meta in unassigned.values():
        if str((meta.candidate or {}).get("option_set", "") or ""):
            return True
    return False


def _served_units(placed_job_ids, cand_of) -> int:
    """Option-set-aware coverage: each non-option job counts 1; each DISTINCT
    option_set counts 1 (DIRECT's single leg and XDOCK's two legs score equal),
    so the (served, -cost) ranking decides an option set's mode by COST, not by
    how many legs the mode happens to carry. ``cand_of(jid)`` -> candidate dict.
    """
    n = 0
    seen: set[str] = set()
    for jid in placed_job_ids:
        oset = str((cand_of(jid) or {}).get("option_set", "") or "")
        if not oset:
            n += 1
        elif oset not in seen:
            seen.add(oset)
            n += 1
    return n


def _repairable_unassigned_meta(
    rejected: list[RejectedJob] | None,
    all_job_meta: dict[str, JobMeta],
    selected_leg_ids: set[str],
) -> tuple[dict[str, JobMeta], list[RejectedJob]]:
    repairable: dict[str, JobMeta] = {}
    remaining: list[RejectedJob] = []
    for rj in rejected or []:
        jid = str(rj.job_id)
        meta = all_job_meta.get(jid)
        if meta is None or str(rj.reason) not in _REPAIRABLE_REASONS:
            remaining.append(rj)
            continue
        cand = meta.candidate
        if str(cand.get("hard_blocker", "") or ""):
            remaining.append(rj)
            continue
        if str(cand.get("dependency_type", "") or "") == "REQUIRES_PRIOR_PICKUP":
            predecessor = str(cand.get("predecessor_leg_id", "") or "")
            if predecessor and predecessor not in selected_leg_ids:
                remaining.append(rj)
                continue
        repairable[jid] = meta
    return repairable, remaining


def _ranked_inserts_for_job(
    meta: JobMeta,
    work: dict,
    routes: dict,
    vehicle_meta: dict[str, VehicleMeta],
    rv_cache: dict[tuple[str, str], RouteVehicle],
    excluded: set[tuple[str, str]],
    top: int = 2,
    avail_overrides: dict[tuple[str, str], str] | None = None,
    watermarks: dict | None = None,
    commit_floor=None,
    now=None,
    locked_keys: dict | None = None,
) -> list[tuple[float, tuple[str, str], list[list[RouteJob]]]]:
    """Up to ``top`` cheapest feasible insertions (no ejection), sorted by km delta.
    Used by regret-k repair, which needs the best *and* second-best to rank urgency."""
    def rv(vid: str, day: str) -> RouteVehicle:
        cached = rv_cache.get((vid, day))
        if cached is None:
            cached = _rv_ov(vehicle_meta[vid], day, avail_overrides)
            rv_cache[(vid, day)] = cached
        return cached

    def day_km(vid: str, day: str, trips: list[list[RouteJob]]) -> float:
        return 0.0 if not trips else evaluate_day(rv(vid, day), trips, detail=False).total_km

    lock = (locked_keys or {}).get(meta.rjob.job_id)
    cands: list[tuple[float, tuple[str, str], list[list[RouteJob]]]] = []
    for day, rjob in _insertion_days(meta):  # K2: nominal day + earlier variants
        for vid in meta.eligible_vehicles:
            if vid not in vehicle_meta or (vid, day) in excluded:
                continue
            key = (vid, day)
            if lock is not None and key != lock:
                continue                    # in-flight suffix job: pinned to its vehicle
            wm = (watermarks or {}).get(key, ())
            det = _floor_guard_active(commit_floor, wm, day)
            current_trips = work[key] if key in work else routes.get(key, [])
            base_km = day_km(vid, day, current_trips)
            vt = vehicle_meta[vid].vehicle_type
            base_ev = (evaluate_day(rv(vid, day), current_trips, detail=False)
                       if (current_trips and vehicle_day_cost_enabled()) else None)
            rate = road_cost_per_km(vt)                             # rank on GBP (fuel+R&M), not km
            pen = _oa_penalty_km(vehicle_meta[vid], rjob)            # B15 out-of-area phantom km
            headroom = _scarce_depot_headroom(vehicle_meta[vid], meta.eligible_vehicles, vehicle_meta)
            base_starts = None
            if det and current_trips:
                bev = base_ev if base_ev is not None else evaluate_day(rv(vid, day), current_trips, detail=False)
                base_starts = [t.route_start for t in bev.trip_evaluations]
            for idx, trip in enumerate(current_trips):
                if same_order_handoff_conflict(trip, rjob):
                    continue
                minpos = int(wm[idx]) if idx < len(wm) else 0
                trip_ev = try_insert_job(rv(vid, day), trip, rjob, "best", detail=False,
                                         min_position=minpos)
                if not trip_ev.feasible:
                    continue
                candidate_trips = [list(t) for t in current_trips]
                candidate_trips[idx] = _reorder(trip + [rjob], trip_ev)
                day_ev = evaluate_day(rv(vid, day), candidate_trips, detail=det)
                if not day_ev.feasible:
                    continue
                if det and not floor_ok(day_ev, wm, commit_floor, now=now):
                    continue
                if det and _retimes_committed_departure(day_ev, base_starts, idx, minpos):
                    continue
                cands.append((rate * (day_ev.total_km - base_km + pen) + headroom
                              + _day_nonkm_cost(vt, day_ev) - _day_nonkm_cost(vt, base_ev),
                              key, candidate_trips))
            # no trip-count cap (user rule 2026-07-16): a fresh trip is bounded by
            # duty/driving/window feasibility below, not a telematics habit count
            candidate_trips = [list(t) for t in current_trips] + [[rjob]]
            day_ev = evaluate_day(rv(vid, day), candidate_trips, detail=det)
            if day_ev.feasible and (not det or floor_ok(day_ev, wm, commit_floor, now=now)):
                cands.append((rate * (day_ev.total_km - base_km + pen) + headroom
                              + _day_nonkm_cost(vt, day_ev) - _day_nonkm_cost(vt, base_ev),
                              key, candidate_trips))
    cands.sort(key=lambda c: c[0])
    return cands[:top]


def _best_insert_for_job(
    meta: JobMeta,
    work: dict,
    routes: dict,
    vehicle_meta: dict[str, VehicleMeta],
    rv_cache: dict[tuple[str, str], RouteVehicle],
    excluded: set[tuple[str, str]],
    *,
    allow_eject: bool = False,
    pinned: frozenset = frozenset(),
    avail_overrides: dict[tuple[str, str], str] | None = None,
    watermarks: dict | None = None,
    commit_floor=None,
    now=None,
    locked_keys: dict | None = None,
) -> tuple[float, tuple[str, str], list[list[RouteJob]], str | None] | None:
    def rv(vid: str, day: str) -> RouteVehicle:
        cached = rv_cache.get((vid, day))
        if cached is None:
            cached = _rv_ov(vehicle_meta[vid], day, avail_overrides)
            rv_cache[(vid, day)] = cached
        return cached

    def day_km(vid: str, day: str, trips: list[list[RouteJob]]) -> float:
        return 0.0 if not trips else evaluate_day(rv(vid, day), trips, detail=False).total_km

    lock = (locked_keys or {}).get(meta.rjob.job_id)
    best = None
    for day, rjob in _insertion_days(meta):  # K2: nominal day + earlier variants
        for vid in meta.eligible_vehicles:
            if vid not in vehicle_meta or (vid, day) in excluded:
                continue
            key = (vid, day)
            if lock is not None and key != lock:
                continue                    # in-flight suffix job: pinned to its vehicle
            wm = (watermarks or {}).get(key, ())
            det = _floor_guard_active(commit_floor, wm, day)
            current_trips = work[key] if key in work else routes.get(key, [])
            base_km = day_km(vid, day, current_trips)
            vt = vehicle_meta[vid].vehicle_type
            base_ev = (evaluate_day(rv(vid, day), current_trips, detail=False)
                       if (current_trips and vehicle_day_cost_enabled()) else None)
            rate = road_cost_per_km(vt)                             # rank on GBP (fuel+R&M), not km
            pen = _oa_penalty_km(vehicle_meta[vid], rjob)            # B15 out-of-area phantom km
            headroom = _scarce_depot_headroom(vehicle_meta[vid], meta.eligible_vehicles, vehicle_meta)
            base_starts = None
            if det and current_trips:
                bev = base_ev if base_ev is not None else evaluate_day(rv(vid, day), current_trips, detail=False)
                base_starts = [t.route_start for t in bev.trip_evaluations]

            for idx, trip in enumerate(current_trips):
                if same_order_handoff_conflict(trip, rjob):
                    continue
                minpos = int(wm[idx]) if idx < len(wm) else 0
                trip_ev = try_insert_job(rv(vid, day), trip, rjob, "best", detail=False,
                                         min_position=minpos)
                if not trip_ev.feasible:
                    continue
                candidate_trips = [list(t) for t in current_trips]
                candidate_trips[idx] = _reorder(trip + [rjob], trip_ev)
                day_ev = evaluate_day(rv(vid, day), candidate_trips, detail=det)
                if not day_ev.feasible:
                    continue
                if det and not floor_ok(day_ev, wm, commit_floor, now=now):
                    continue
                if det and _retimes_committed_departure(day_ev, base_starts, idx, minpos):
                    continue
                delta = (rate * (day_ev.total_km - base_km + pen) + headroom
                         + _day_nonkm_cost(vt, day_ev) - _day_nonkm_cost(vt, base_ev))
                if best is None or delta < best[0]:
                    best = (delta, key, candidate_trips, None)

            # no trip-count cap (user rule 2026-07-16): feasibility bounds the day
            candidate_trips = [list(t) for t in current_trips] + [[rjob]]
            day_ev = evaluate_day(rv(vid, day), candidate_trips, detail=det)
            if day_ev.feasible and (not det or floor_ok(day_ev, wm, commit_floor, now=now)):
                delta = (rate * (day_ev.total_km - base_km + pen) + headroom
                         + _day_nonkm_cost(vt, day_ev) - _day_nonkm_cost(vt, base_ev))
                if best is None or delta < best[0]:
                    best = (delta, key, candidate_trips, None)
    if best is not None or not allow_eject:
        return best

    for day, rjob in _insertion_days(meta):  # K2: eject fallback searches the same days
        for vid in meta.eligible_vehicles:
            if vid not in vehicle_meta or (vid, day) in excluded:
                continue
            key = (vid, day)
            if lock is not None and key != lock:
                continue                    # in-flight suffix job: pinned to its vehicle
            wm = (watermarks or {}).get(key, ())
            det = _floor_guard_active(commit_floor, wm, day)
            current_trips = work[key] if key in work else routes.get(key, [])
            base_km = day_km(vid, day, current_trips)
            vt = vehicle_meta[vid].vehicle_type
            base_ev = (evaluate_day(rv(vid, day), current_trips, detail=False)
                       if (current_trips and vehicle_day_cost_enabled()) else None)
            rate = road_cost_per_km(vt)                             # rank on GBP (fuel+R&M), not km
            pen = _oa_penalty_km(vehicle_meta[vid], rjob)            # B15 out-of-area phantom km
            headroom = _scarce_depot_headroom(vehicle_meta[vid], meta.eligible_vehicles, vehicle_meta)
            base_starts = None
            if det and current_trips:
                bev = base_ev if base_ev is not None else evaluate_day(rv(vid, day), current_trips, detail=False)
                base_starts = [t.route_start for t in bev.trip_evaluations]

            for idx, trip in enumerate(current_trips):
                minpos = int(wm[idx]) if idx < len(wm) else 0
                for stop_idx, evicted in enumerate(trip):
                    if evicted.job_id in pinned:
                        continue
                    reduced_trip = [j for j in trip[:stop_idx] + trip[stop_idx + 1:]]
                    if same_order_handoff_conflict(reduced_trip, rjob):
                        continue
                    trip_ev = try_insert_job(rv(vid, day), reduced_trip, rjob, "best", detail=False,
                                             min_position=minpos)
                    if not trip_ev.feasible:
                        continue
                    candidate_trips = [list(t) for t in current_trips]
                    candidate_trips[idx] = _reorder(reduced_trip + [rjob], trip_ev)
                    candidate_trips = [t for t in candidate_trips if t]
                    day_ev = evaluate_day(rv(vid, day), candidate_trips, detail=det)
                    if not day_ev.feasible:
                        continue
                    if det and not floor_ok(day_ev, wm, commit_floor, now=now):
                        continue
                    if det and _retimes_committed_departure(day_ev, base_starts, idx, minpos):
                        continue
                    # eviction swaps penalties: the incoming job's arrives, the
                    # evicted job's leaves — net them so the delta tracks the true
                    # generalized-cost change of the day.
                    delta = (rate * (day_ev.total_km - base_km + pen
                                     - _oa_penalty_km(vehicle_meta[vid], evicted)) + headroom
                             + _day_nonkm_cost(vt, day_ev) - _day_nonkm_cost(vt, base_ev))
                    if best is None or delta < best[0]:
                        best = (delta, key, candidate_trips, evicted.job_id)
    return best

def improve_solution(
    solution: dict,
    job_meta: dict[str, JobMeta],
    vehicle_meta: dict[str, VehicleMeta],
    iterations: int = 2000,
    rng_seed: int = 0,
    max_candidates: int = 8,
    log_every: int = 0,
    on_progress=None,
    excluded_vehicle_days: set[tuple[str, str]] | None = None,
    time_budget_s: float | None = None,
    no_improve_patience: int | None = None,
    unassigned_meta: dict[str, JobMeta] | None = None,
    sa_temp_fraction: float = 0.0,
    sa_cooling: float = 0.999,
    repair_every: int = 1,
    regret_repair: bool = False,
    pinned_job_ids=None,
    avail_overrides: dict[tuple[str, str], str] | None = None,
    watermarks: dict | None = None,
    commit_floor=None,
    now=None,
    locked_keys: dict | None = None,
    beta: float = 0.0,
    reference_routes: dict | None = None,
    disturbance_weight: dict | None = None,
    disturbance_gamma: float = 0.5,
    converge_pct: float | None = None,
    converge_window: int | None = None,
    converge_min_iters: int | None = None,
) -> SolutionImprovement:
    pinned = frozenset(str(j) for j in (pinned_job_ids or ()))
    rng = random.Random(rng_seed)
    excluded = excluded_vehicle_days or set()
    routes = {key: _as_trips(value) for key, value in solution.items()}
    if watermarks:
        # spec 4.7a: committed stops are fact — no destroy operator may remove
        # them (their positions are additionally floored at insertion time)
        pinned = pinned | committed_job_ids(routes, watermarks)
    unassigned: dict[str, JobMeta] = dict(unassigned_meta or {})
    t0 = monotonic()
    last_improve_it = 0

    rv_cache: dict[tuple[str, str], RouteVehicle] = {}

    def rv(vid: str, day: str) -> RouteVehicle:
        cached = rv_cache.get((vid, day))
        if cached is None:
            cached = _rv_ov(vehicle_meta[vid], day, avail_overrides)
            rv_cache[(vid, day)] = cached
        return cached

    def km(trips, vid, day) -> float:
        trips = _as_trips(trips)
        return 0.0 if not trips else evaluate_day(rv(vid, day), trips, detail=False).total_km

    def changed_costs(work: dict) -> dict | None:
        # B16 root cause: road distance violates the triangle inequality (OSRM),
        # so REMOVING a stop can push a day over its shift/window limits — and an
        # infeasible day evaluates to total_km 0.0, which made breaking a route
        # look like a huge saving. Cost every changed day in one pass and refuse
        # the whole spec if any of them no longer evaluates feasible.
        out: dict = {}
        for key, trips_w in work.items():
            tt = _as_trips(trips_w)
            if not tt:
                out[key] = 0.0
                continue
            ev = evaluate_day(rv(key[0], key[1]), tt, detail=False)
            if not ev.feasible:
                return None
            vm = vehicle_meta[key[0]]
            out[key] = (road_cost_per_km(vm.vehicle_type) * (ev.total_km + _trips_penalty_km(tt, vm))
                        + driver_day_cost_ev(vm.vehicle_type, ev))
        return out

    route_cost_by_key: dict[tuple[str, str], float] = {}
    phys_km_before = 0.0
    # §6.3b seed snapshot: the SEED plan's vehicle-days + km per vehicle type, BEFORE any
    # ALNS move — the baseline for the consolidation / type-shift evidence (seed vs final).
    seed_by_type: dict[str, dict] = {}
    job_loc: dict[str, tuple[str, str]] = {}
    for (vid, day), trips in routes.items():
        if vid in vehicle_meta:
            tt = _as_trips(trips)
            # Perf: evaluate_day is the expensive call here and this loop runs
            # once per vehicle-day in the FULL cumulative solution every epoch
            # — compute it once and reuse for both the km baseline and the
            # driver-day cost, instead of calling it twice with identical args.
            ev0 = evaluate_day(rv(vid, day), tt, detail=False) if tt else None
            k_phys = ev0.total_km if ev0 is not None else 0.0
            phys_km_before += k_phys
            if tt:
                _st = seed_by_type.setdefault(str(vehicle_meta[vid].vehicle_type),
                                              {"vehicle_days": 0, "km_road": 0.0})
                _st["vehicle_days"] += 1
                _st["km_road"] += float(k_phys)
            cost = road_cost_per_km(vehicle_meta[vid].vehicle_type) * (
                k_phys + _trips_penalty_km(trips, vehicle_meta[vid]))
            if tt and vehicle_day_cost_enabled():
                cost += driver_day_cost_ev(vehicle_meta[vid].vehicle_type, ev0)
            route_cost_by_key[(vid, day)] = cost
        else:
            route_cost_by_key[(vid, day)] = 0.0
        for j in _flatten(trips):
            job_loc[j.job_id] = (vid, day)

    def cand_of(jid: str) -> dict:
        m = job_meta.get(jid) or unassigned.get(jid)
        return (m.candidate if m is not None else {}) or {}

    total = sum(route_cost_by_key.values())   # GBP (per-type generalized cost) — the objective
    cost_before = total
    km_before = phys_km_before                # physical km, retained for reporting only
    served_before = _served_units(job_loc, cand_of)
    accepted = 0
    inserted_jobs = 0
    attempted_jobs = 0
    job_ids = sorted(j for j in job_loc if j not in pinned)

    # Dynamic v2 (spec 2026-07-11 §5): the objective is cost + beta*disturbance,
    # where disturbance is deviation from the warm-started reference plan. _adj
    # returns the disturbance-adjusted delta of a move; when beta == 0 (or no
    # reference) it returns the raw cost delta unchanged, so the search is
    # bit-identical to today's cost-only behaviour (the regression gate).
    from freight_planner.disturbance import job_positions as _job_positions
    from freight_planner.disturbance import key_disturbance as _key_disturbance
    _ref_pos = (_job_positions(reference_routes)
                if (beta > 0.0 and reference_routes) else None)

    def _adj(delta: float, changed, work) -> float:
        if _ref_pos is None:
            return delta
        dd = (sum(_key_disturbance(k, work.get(k, []), _ref_pos,
                                   gamma=disturbance_gamma, weight=disturbance_weight)
                  for k in changed)
              - sum(_key_disturbance(k, routes.get(k, []), _ref_pos,
                                     gamma=disturbance_gamma, weight=disturbance_weight)
                    for k in changed))
        return delta + beta * dd

    # Simulated-annealing acceptance (B8): accept a km-worsening rearrangement with
    # probability exp(-delta/T) to escape local optima; T cools geometrically. The
    # best (coverage-first, then km) solution is tracked and returned, so wandering
    # never degrades the result. sa_temp_fraction == 0 -> pure hill-climb (default).
    temperature = max(0.0, cost_before * float(sa_temp_fraction))
    best_routes = {k: [list(t) for t in v] for k, v in routes.items()}
    best_unassigned = sorted(unassigned)
    best_total = total
    best_served = served_before

    # Perf: best_routes is re-snapshotted every time an accepted move improves
    # on the incumbent — which can happen thousands of times per epoch. `routes`
    # is only ever reassigned by full key (never mutated in place, see the two
    # `routes[key] = trips` sites below), so instead of deep-copying the WHOLE
    # dict each time, track which keys changed since the last best snapshot and
    # only re-copy those. best_routes is mutated in place, not rebuilt.
    dirty_since_best: set[tuple[str, str]] = set()

    def _try_option_swap():
        """Endogenous DIRECT<->XDOCK re-pricing: pick a served, un-pinned option
        set, remove its active-group legs and insert its rival-group bundle against
        the FULL current solution. Returns a swap dict (work/placements/new_cost/
        delta) or None when no set is swappable or the rival can't be placed. Served
        units are unchanged by construction, so the swap is decided on cost."""
        by_set: dict[str, dict] = {}
        for jid, key in job_loc.items():
            cand = cand_of(jid)
            oset = str(cand.get("option_set", "") or "")
            if not oset:
                continue
            d = by_set.setdefault(oset, {"grp": str(cand.get("option_group", "") or ""),
                                         "active": [], "pinned": False})
            d["active"].append(jid)
            if jid in pinned:
                d["pinned"] = True
        if not by_set:
            return None
        sib_by_set: dict[str, list[str]] = {}
        for jid, meta in unassigned.items():
            oset = str((meta.candidate or {}).get("option_set", "") or "")
            if oset:
                sib_by_set.setdefault(oset, []).append(jid)
        swappable = []
        for oset, d in by_set.items():
            if d["pinned"]:
                continue  # mode-locked: a committed leg is present
            sibs = [j for j in sib_by_set.get(oset, [])
                    if str(cand_of(j).get("option_group", "") or "") != d["grp"]]
            if sibs:
                swappable.append((oset, d["active"], sibs))
        if not swappable:
            return None
        oset, active_jobs, sibling_jobs = swappable[rng.randrange(len(swappable))]
        active_set = set(active_jobs)
        affected = {job_loc[j] for j in active_jobs}
        work = {}
        for key in affected:
            trimmed = [[j for j in trip if j.job_id not in active_set]
                       for trip in _as_trips(routes.get(key, []))]
            work[key] = [t for t in trimmed if t]
        # insert the rival bundle, pickups before deliveries (XC before XD)
        ordered = sorted(sibling_jobs,
                         key=lambda j: 0 if "PICKUP" in str(cand_of(j).get("leg_kind", "")) else 1)
        placements = []
        for jid in ordered:
            meta = unassigned[jid]
            insert = _best_insert_for_job(
                meta, work, routes, vehicle_meta, rv_cache, excluded,
                allow_eject=False, pinned=pinned, avail_overrides=avail_overrides,
                watermarks=watermarks, commit_floor=commit_floor, now=now, locked_keys=locked_keys)
            if insert is None:
                return None  # rival can't be placed feasibly -> keep current mode
            _delta, key, new_trips, _ev = insert
            work[key] = new_trips
            placements.append((jid, key))
        new_cost = changed_costs(work)
        if new_cost is None:
            return None
        changed = list(new_cost)
        delta = sum(new_cost.values()) - sum(route_cost_by_key.get(k, 0.0) for k in changed)
        delta = _adj(delta, changed, work)
        return {"oset": oset, "active": active_jobs, "work": work,
                "placements": placements, "new_cost": new_cost, "delta": delta}

    # Adaptive destroy-operator selection (B8 phase 2). Weights are reblended every
    # _WEIGHT_INTERVAL iterations toward whichever operator has been earning reward.
    # Experiment toggles (default = original behavior, see _active_destroy_ops/_removal_band).
    _ops_pool = list(_active_destroy_ops())
    if "option_swap" in _ops_pool and not _has_option_sets(job_loc, unassigned, cand_of):
        _ops_pool = [o for o in _ops_pool if o != "option_swap"] or ["random"]
    ops = _AdaptiveOps(tuple(_ops_pool), rng)
    _WEIGHT_INTERVAL = 50
    _k_lo, _k_hi = _removal_band()
    _accept_mode, _rrt_dev = _read_accept_env()
    # Anytime-trace (E5): checkpoint rows collected in memory (zero I/O in the hot
    # loop), written once after the loop. Off unless FP_ALNS_TRACE is set.
    _trace_path = os.environ.get("FP_ALNS_TRACE", "").strip()
    _trace_rows: list[tuple] | None = [] if _trace_path else None
    _trace_every = log_every if log_every and log_every > 0 else 200
    _last_it = -1  # last executed iteration (for the trace's final endpoint row)

    if _CONSERVE:
        _conserve_check(routes, job_loc, unassigned, "init (seed input)")
        if pinned:
            _pinned_check(routes, pinned, "init (seed input)")

    # Frozen cost = the objective of fully-pinned (committed) vehicle-days the search
    # cannot move; the gate normalizes its % against total-frozen_cost so a warm-started
    # rolling epoch is not judged against cost it has no power to improve (2026-07-24).
    frozen_cost = sum(
        route_cost_by_key.get((vid, day), 0.0)
        for (vid, day), trips in routes.items()
        for _jobs in [_flatten(_as_trips(trips))]
        if _jobs and all(j.job_id in pinned for j in _jobs))
    gate = ConvergenceGate(
        pct=ALNS_CONVERGE_PCT if converge_pct is None else converge_pct,
        window=ALNS_CONVERGE_WINDOW if converge_window is None else converge_window,
        min_iters=ALNS_CONVERGE_MIN_ITERS if converge_min_iters is None else converge_min_iters,
        frozen_cost=frozen_cost)
    stop_reason = "iterations"
    iterations_run = 0
    for it in range(iterations):
        iterations_run = it
        if not job_ids and not unassigned:
            stop_reason = "drained"
            break
        if time_budget_s is not None and (monotonic() - t0) > time_budget_s:
            stop_reason = "time_budget"
            break
        if no_improve_patience is not None and (it - last_improve_it) >= no_improve_patience:
            stop_reason = "no_improve"
            break
        if gate.should_stop(it, best_total, best_served):
            stop_reason = "converged"
            break

        # --- destroy: pick an operator, then the jobs to rip out ---
        op = ops.select() if job_ids else "random"
        if op == "option_swap":
            swap = _try_option_swap()
            if swap is None:
                ops.reward(op, 0.0)
            else:
                candidate_total = total + swap["delta"]
                improving = swap["delta"] < -_EPS   # served units unchanged by a swap
                accept = improving
                if not accept and swap["delta"] > 0.0:
                    accept = _accept_worse(_accept_mode, _rrt_dev, candidate_total=candidate_total,
                                           best_total=best_total, temperature=temperature,
                                           rng=rng, delta=swap["delta"])
                if not accept:
                    ops.reward(op, 0.0)
                else:
                    for key, trips in swap["work"].items():
                        routes[key] = trips
                        route_cost_by_key[key] = swap["new_cost"][key]
                    dirty_since_best.update(swap["work"])
                    for jid in swap["active"]:      # unserved active legs -> swap-back pool
                        job_loc.pop(jid, None)
                        if jid in job_meta:
                            unassigned[jid] = job_meta.pop(jid)
                    for jid, key in swap["placements"]:   # rival legs now served
                        job_loc[jid] = key
                        if jid in unassigned:
                            job_meta[jid] = unassigned.pop(jid)
                    total = candidate_total
                    accepted += 1
                    job_ids = sorted(j for j in job_loc if j not in pinned)
                    cur_served = _served_units(job_loc, cand_of)
                    if _CONSERVE:
                        _conserve_check(routes, job_loc, unassigned,
                                        f"it={it} op=option_swap set={swap['oset']}")
                        if pinned:
                            _pinned_check(routes, pinned, f"it={it} op=option_swap (post-accept)")
                    if (cur_served, -total) > (best_served, -best_total):
                        for key in dirty_since_best:
                            best_routes[key] = [list(t) for t in routes[key]]
                        dirty_since_best.clear()
                        best_unassigned = sorted(unassigned)
                        best_total = total
                        best_served = cur_served
                        last_improve_it = it
                        ops.reward(op, _AdaptiveOps.REWARD_BEST)
                    elif improving:
                        ops.reward(op, _AdaptiveOps.REWARD_BETTER)
                    else:
                        ops.reward(op, _AdaptiveOps.REWARD_ACCEPTED)
            if (it + 1) % _WEIGHT_INTERVAL == 0:
                ops.update_weights()
            if temperature > 0.0:
                temperature *= sa_cooling
            continue
        if job_ids:
            k = rng.randint(_k_lo, min(_k_hi, len(job_ids))) if len(job_ids) >= _k_lo else 1
        else:
            k = 0
        if not job_ids:
            removed = []
        elif op == "worst":
            removed = _worst_removal(routes, vehicle_meta, k, rng, pinned=pinned)
        elif op == "shaw":
            removed = _shaw_removal(routes, k, rng, pinned=pinned)
        else:
            removed = rng.sample(job_ids, k)

        if pinned:  # belt-and-braces: the ops filter internally, this can't regress
            removed = [j for j in removed if j not in pinned]

        plan_specs: list[tuple[list[str], list[str], int]] = [(list(removed), [], 0)]
        # Coverage-repair (re-attempting the unassigned pool) is the expensive part —
        # up to max_candidates x 5 full destroy/repair specs per iteration. The rejected
        # jobs rarely become insertable, so attempting them *every* iteration starves the
        # cheap km-improving move of cycles. Run it on a cadence; every iteration still
        # does the base destroy/repair above. repair_every == 1 -> original behaviour.
        do_repair = bool(unassigned) and (repair_every <= 1 or it % repair_every == 0)
        if do_repair:
            extra_ids = list(unassigned)
            rng.shuffle(extra_ids)
            extra_ids = extra_ids[: max(1, min(max_candidates, len(extra_ids)))]
            for extra_jid in extra_ids:
                extra_meta = unassigned[extra_jid]
                same_day_assigned = [jid for jid in job_ids if job_meta[jid].day == extra_meta.day]
                if same_day_assigned:
                    kk = min(max(1, k), len(same_day_assigned))
                    extra_removed = rng.sample(same_day_assigned, kk)
                else:
                    extra_removed = list(removed)
                plan_specs.append((extra_removed, [extra_jid], 1))

                targeted_specs: list[list[str]] = []
                for vid in extra_meta.eligible_vehicles[:max_candidates]:
                    key = (vid, extra_meta.day)
                    if key in excluded:
                        continue
                    current_trips = routes.get(key, [])
                    if not current_trips:
                        continue
                    for trip in current_trips[:2]:
                        ruined = [j.job_id for j in trip if j.job_id not in pinned]
                        if ruined:
                            targeted_specs.append(ruined)
                    if len(targeted_specs) >= 4:
                        break
                for ruined in targeted_specs[:4]:
                    plan_specs.append((ruined, [extra_jid], 1))

        best_plan = None
        current_served = _served_units(job_loc, cand_of)

        for removed_for_plan, inserted_jids, attempted in plan_specs:
            attempted_jobs += attempted
            removed_set = set(removed_for_plan)
            work: dict[tuple[str, str], list[list[RouteJob]]] = {}
            for jid in removed_for_plan:
                key = job_loc[jid]
                if key not in work:
                    work[key] = [[j for j in trip if j.job_id not in removed_set] for trip in routes.get(key, [])]
                    work[key] = [trip for trip in work[key] if trip]

            available_leg_ids = {
                str(job_meta[jid].candidate.get("leg_id", "") or "")
                for jid in job_loc
                if jid not in removed_set and jid in job_meta
            }
            # DIRECT/XDOCK mutual exclusion: seed from the jobs still assigned in
            # this trial (assigned minus removed). A rival-group leg is then never
            # inserted while its option_set is already served; when OptionSwap has
            # removed the active group, the set is free and the sibling can enter.
            mutex = OptionMutex()
            mutex.seed_from_assigned(
                job_meta[jid].candidate for jid in job_loc
                if jid not in removed_set and jid in job_meta)
            placements: list[tuple[str, tuple[str, str]]] = []
            ok = True

            # Regret-2 repair for the base km spec (no coverage insert / no ejection):
            # reinsert the highest-regret job first — the one that loses the most if its
            # cheapest slot is taken by another. A job with a single feasible option has
            # infinite regret, so forced placements go first. Greedy-by-priority remains
            # for coverage specs (eviction chains make regret ordering unsafe there).
            if regret_repair and not inserted_jids and len(removed_for_plan) >= 2:
                pending = list(removed_for_plan)
                while pending and ok:
                    scored: list[tuple[float, float, str, tuple[str, str], list[list[RouteJob]]]] = []
                    for jid in pending:
                        meta = job_meta.get(jid)
                        if meta is None:
                            ok = False
                            break
                        predecessor = str(meta.candidate.get("predecessor_leg_id", "") or "")
                        if (str(meta.candidate.get("dependency_type", "") or "") == "REQUIRES_PRIOR_PICKUP"
                                and predecessor and predecessor not in available_leg_ids):
                            continue  # blocked until its predecessor is placed this round
                        if not mutex.insertable(meta.candidate):
                            continue  # rival option-group already active in this solution
                        ranked = _ranked_inserts_for_job(
                            meta, work, routes, vehicle_meta, rv_cache, excluded, top=2,
                            avail_overrides=avail_overrides, watermarks=watermarks,
                            commit_floor=commit_floor, now=now, locked_keys=locked_keys)
                        if not ranked:
                            ok = False
                            break
                        best_delta = ranked[0][0]
                        regret = (ranked[1][0] - best_delta) if len(ranked) > 1 else float("inf")
                        scored.append((regret, best_delta, jid, ranked[0][1], ranked[0][2]))
                    if not ok:
                        break
                    if not scored:
                        ok = False  # remaining jobs all dependency-blocked (deadlock)
                        break
                    scored.sort(key=lambda t: (-t[0], t[1]))
                    _r, _bd, jid, key, new_trips = scored[0]
                    work[key] = new_trips
                    placements.append((jid, key))
                    leg_id = str(job_meta[jid].candidate.get("leg_id", "") or "")
                    if leg_id:
                        available_leg_ids.add(leg_id)
                    mutex.assign(job_meta[jid].candidate)
                    pending.remove(jid)
                if not ok:
                    continue
                new_cost = changed_costs(work)
                if new_cost is None:
                    continue  # a changed day no longer evaluates feasible
                changed = list(new_cost)
                delta = sum(new_cost.values()) - sum(route_cost_by_key.get(key, 0.0) for key in changed)
                served_after = current_served
                delta = _adj(delta, changed, work)
                candidate_total = total + delta
                score = (served_after, -candidate_total)
                if best_plan is None or score > best_plan[0]:
                    best_plan = (score, work, placements, new_cost, delta, 0, [], attempted)
                continue

            insert_queue = sorted(removed_for_plan, key=lambda jid: _priority_key(job_meta[jid]))
            if inserted_jids:
                insert_queue = list(inserted_jids) + insert_queue
            queued = set(insert_queue)

            while insert_queue:
                jid = insert_queue.pop(0)
                meta = unassigned.get(jid) or job_meta.get(jid)
                if meta is None:
                    ok = False
                    break
                predecessor = str(meta.candidate.get("predecessor_leg_id", "") or "")
                if str(meta.candidate.get("dependency_type", "") or "") == "REQUIRES_PRIOR_PICKUP" and predecessor:
                    if predecessor not in available_leg_ids:
                        ok = False
                        break
                if not mutex.insertable(meta.candidate):
                    ok = False  # rival option-group already active: plan infeasible
                    break
                allow_eject = jid in inserted_jids
                insert = _best_insert_for_job(
                    meta,
                    work,
                    routes,
                    vehicle_meta,
                    rv_cache,
                    excluded,
                    allow_eject=allow_eject,
                    pinned=pinned,
                    avail_overrides=avail_overrides,
                    watermarks=watermarks,
                    commit_floor=commit_floor, now=now,
                    locked_keys=locked_keys,
                )
                if insert is None:
                    ok = False
                    break
                _delta, key, new_trips, evicted_jid = insert
                work[key] = new_trips
                placements.append((jid, key))
                mutex.assign(meta.candidate)
                leg_id = str(meta.candidate.get("leg_id", "") or "")
                if leg_id:
                    available_leg_ids.add(leg_id)
                if evicted_jid:
                    evicted_leg = str(job_meta[evicted_jid].candidate.get("leg_id", "") or "")
                    if evicted_leg and evicted_leg in available_leg_ids:
                        available_leg_ids.remove(evicted_leg)
                    if evicted_jid not in queued:
                        insert_queue.insert(0, evicted_jid)
                        queued.add(evicted_jid)

            if not ok:
                continue

            new_cost = changed_costs(work)
            if new_cost is None:
                continue  # a changed day no longer evaluates feasible
            changed = list(new_cost)
            delta = sum(new_cost.values()) - sum(route_cost_by_key.get(key, 0.0) for key in changed)
            # Option-set-aware coverage: recount over the resulting placement so a
            # second XDOCK leg for an already-served set adds 0, not +1. Served
            # gain is the units delta (not raw job count), so mode swaps register
            # as coverage-neutral and are decided on cost.
            resulting_placed = (set(job_loc) - removed_set) | {jid for jid, _ in placements}
            served_after = _served_units(resulting_placed, cand_of)
            served_gain = served_after - current_served
            delta = _adj(delta, changed, work)
            candidate_total = total + delta
            score = (served_after, -candidate_total)
            if best_plan is None or score > best_plan[0]:
                best_plan = (score, work, placements, new_cost, delta, served_gain, list(inserted_jids), attempted)

        if best_plan is None:
            continue

        _score, work, placements, new_cost, delta, served_gain, inserted_jids, _attempted = best_plan
        candidate_total = total + delta
        improving = served_gain > 0 or delta < -_EPS
        accept = improving
        if not accept and delta > 0.0:
            accept = _accept_worse(_accept_mode, _rrt_dev, candidate_total=candidate_total,
                                   best_total=best_total, temperature=temperature,
                                   rng=rng, delta=delta)
        if accept:
            for key, trips in work.items():
                routes[key] = trips
                route_cost_by_key[key] = new_cost[key]
            dirty_since_best.update(work)
            for jid, key in placements:
                job_loc[jid] = key
            total = candidate_total
            accepted += 1
            for jid in inserted_jids:
                if jid in unassigned:
                    job_meta[jid] = unassigned.pop(jid)
                    inserted_jobs += 1
            job_ids = sorted(j for j in job_loc if j not in pinned)
            cur_served = _served_units(job_loc, cand_of)
            if _CONSERVE:
                _conserve_check(
                    routes, job_loc, unassigned,
                    f"it={it} op={op} inserted={inserted_jids} placements={placements}",
                )
                if pinned:
                    _pinned_check(routes, pinned, f"it={it} op={op} (post-accept)")
            if (cur_served, -total) > (best_served, -best_total):
                for key in dirty_since_best:
                    best_routes[key] = [list(t) for t in routes[key]]
                dirty_since_best.clear()
                best_unassigned = sorted(unassigned)
                best_total = total
                best_served = cur_served
                last_improve_it = it
                ops.reward(op, _AdaptiveOps.REWARD_BEST)
            elif improving:
                ops.reward(op, _AdaptiveOps.REWARD_BETTER)
            else:
                ops.reward(op, _AdaptiveOps.REWARD_ACCEPTED)
        else:
            ops.reward(op, 0.0)

        if (it + 1) % _WEIGHT_INTERVAL == 0:
            ops.update_weights()

        if temperature > 0.0:
            temperature *= sa_cooling

        if on_progress and log_every and (it + 1) % log_every == 0:
            on_progress(it + 1, iterations, accepted, total, len(job_loc))
        if _trace_rows is not None:
            _last_it = it
            if (it + 1) % _trace_every == 0:
                _trace_rows.append((monotonic() - t0, it + 1, accepted, total, best_total, len(job_loc)))

    if _trace_rows is not None:
        # final row (guarantees an endpoint even when iterations < cadence)
        _trace_rows.append((monotonic() - t0, _last_it + 1, accepted, total, best_total, len(job_loc)))
        from pathlib import Path as _Path
        _tp = _Path(_trace_path)
        header = not _tp.exists() or _tp.stat().st_size == 0
        with _tp.open("a", encoding="utf-8", newline="") as fh:
            if header:
                fh.write("elapsed_s,iteration,accepted,cost,best_cost,served\n")
            for el, i2, acc2, c2, bc2, srv in _trace_rows:
                fh.write(f"{el:.2f},{i2},{acc2},{c2:.2f},{bc2:.2f},{srv}\n")

    if _CONSERVE:
        snapshot_served = _served(best_routes)
        snapshot_units = _served_units(snapshot_served, cand_of)
        if snapshot_units != best_served:
            raise AssertionError(
                f"ALNS best-snapshot served mismatch: tracked best_served={best_served} "
                f"but best_routes holds {snapshot_units} served units ({len(snapshot_served)} jobs)"
            )

    # km_after is physical (best_total is the cost objective): the search can accept a
    # km-increasing move if it lowers cost (e.g. shifting a leg onto a cheaper rigid).
    km_after = sum(
        km(trips, vid, day) for (vid, day), trips in best_routes.items() if vid in vehicle_meta
    )
    if stop_reason == "iterations":
        iterations_run = iterations
    return SolutionImprovement(
        solution=best_routes,
        km_before=km_before,
        km_after=km_after,
        served_before=served_before,
        served_after=_served_units(_served(best_routes), cand_of),
        accepted_moves=accepted,
        iterations=iterations,
        inserted_jobs=inserted_jobs,
        attempted_jobs=attempted_jobs,
        remaining_unassigned=best_unassigned,
        cost_before=cost_before,
        cost_after=best_total,
        seed_by_type=seed_by_type,
        stop_reason=stop_reason,
        iterations_run=iterations_run,
    )


def _records_from_solution(
    solution: dict,
    job_meta: dict[str, JobMeta],
    vehicle_meta: dict[str, VehicleMeta],
    plan_id: str,
    avail_overrides: dict[tuple[str, str], str] | None = None,
) -> list[SelectedPlanRecord]:
    candidate_by_job = {jid: meta.candidate for jid, meta in job_meta.items()}
    rv_cache: dict[tuple[str, str], RouteVehicle] = {}

    def rv(vid: str, day: str) -> RouteVehicle:
        cached = rv_cache.get((vid, day))
        if cached is None:
            cached = _rv_ov(vehicle_meta[vid], day, avail_overrides)
            rv_cache[(vid, day)] = cached
        return cached

    return build_plan_records(
        solution,
        candidate_by_job,
        rv,
        lambda vid: vehicle_meta[vid].home_depot if vid in vehicle_meta else "",
        lambda candidate, home: "ALNS",
        plan_id,
    )


def cost_decomposition_from_solution(
    solution: dict,
    vehicle_meta: dict[str, VehicleMeta],
    avail_overrides: dict[tuple[str, str], str] | None = None,
) -> list[dict]:
    """Per (vehicle, day) breakdown of the objective's generalized cost (Eq 3):
    fuel · driver guaranteed-floor · overtime premium · evening late premium ·
    delivery lateness · daily-range overage · total — plus ``km_road`` (physical,
    the reported distance) and ``km_phantom`` (out-of-area RANKING penalty km, which
    is EXCLUDED from reported km and fuel, §5.2). This is the objective's own view
    (matches ``cost_after``); reported plan distance stays the committed geometry."""
    from freight_planner import config as _cfg
    from freight_planner.vehicle_cost import (
        driver_day_cost, driver_hourly_gbp, fuel_cost_per_km, guaranteed_shift_hours,
        late_premium_gbp, overtime_cost_enabled, span_hours, working_hours,
    )
    ot_mult = float(_cfg.OT_DUTY_MULTIPLIER)
    rows: list[dict] = []
    for (vid, day), trips in solution.items():
        trips = _as_trips(trips)
        if not trips or vid not in vehicle_meta:
            continue
        vm = vehicle_meta[vid]
        vt = str(vm.vehicle_type)
        day_ev = evaluate_day(_rv_ov(vm, day, avail_overrides), trips)
        km_road = float(getattr(day_ev, "total_km", 0.0) or 0.0)
        km_phantom = float(_trips_penalty_km(trips, vm))
        fuel = fuel_cost_per_km(vt) * km_road            # reported fuel excludes phantom (§5.2)
        if overtime_cost_enabled():
            wh = working_hours(day_ev)
            hourly = driver_hourly_gbp(vt)
            floor = guaranteed_shift_hours()
            floor_cost = hourly * max(floor, wh)
            overtime = hourly * (ot_mult - 1.0) * max(0.0, wh - floor)
            evening = float(late_premium_gbp(vt, day_ev))
        else:
            floor_cost = float(driver_day_cost(vt, span_hours(day_ev)))
            overtime = evening = 0.0
        lateness = float(getattr(day_ev, "lateness_cost", 0.0))
        rng = float(_range_overage_cost(vt, km_road))
        total = fuel + floor_cost + overtime + evening + lateness + rng
        rows.append({
            "vehicle_id": str(vid), "service_date": str(day), "vehicle_type": vt,
            "km_road": round(km_road, 2), "km_phantom": round(km_phantom, 2),
            "fuel_cost": round(fuel, 2), "driver_floor_cost": round(floor_cost, 2),
            "overtime_premium": round(overtime, 2), "evening_late_premium": round(evening, 2),
            "lateness_cost": round(lateness, 2), "range_overage_cost": round(rng, 2),
            "total_cost": round(total, 2),
        })
    return rows


def _route_totals_from_solution(
    solution: dict,
    vehicle_meta: dict[str, VehicleMeta],
    avail_overrides: dict[tuple[str, str], str] | None = None,
) -> dict[str, float]:
    rt_cache: dict[tuple[str, str], RouteVehicle] = {}
    route_totals: dict[str, float] = {}
    for (vid, day), trips in solution.items():
        trips = _as_trips(trips)
        if not trips or vid not in vehicle_meta:
            continue
        rv = rt_cache.get((vid, day))
        if rv is None:
            rv = _rv_ov(vehicle_meta[vid], day, avail_overrides)
            rt_cache[(vid, day)] = rv
        day_ev = evaluate_day(rv, trips)
        route_id = f"ROUTE:{vid}:{day}"
        route_totals[route_id] = day_ev.total_km
        for trip_index, trip_ev in enumerate(day_ev.trip_evaluations, start=1):
            route_totals[f"{route_id}#T{trip_index}"] = trip_ev.total_km
    return route_totals


def _route_times_from_solution(
    solution: dict,
    vehicle_meta: dict[str, VehicleMeta],
    avail_overrides: dict[tuple[str, str], str] | None = None,
) -> dict[str, tuple[str, str]]:
    """(depart, return) ISO times per route/trip — mirrors _route_totals_from_solution,
    capturing the depot-to-depot clock the evaluator already computes so depot_start /
    depot_return rows can be timed (v1.1 depot-timing emission)."""
    rt_cache: dict[tuple[str, str], RouteVehicle] = {}
    route_times: dict[str, tuple[str, str]] = {}
    for (vid, day), trips in solution.items():
        trips = _as_trips(trips)
        if not trips or vid not in vehicle_meta:
            continue
        rv = rt_cache.get((vid, day))
        if rv is None:
            rv = _rv_ov(vehicle_meta[vid], day, avail_overrides)
            rt_cache[(vid, day)] = rv
        day_ev = evaluate_day(rv, trips)
        route_id = f"ROUTE:{vid}:{day}"
        route_times[route_id] = (day_ev.day_start, day_ev.day_end)
        for trip_index, trip_ev in enumerate(day_ev.trip_evaluations, start=1):
            route_times[f"{route_id}#T{trip_index}"] = (trip_ev.route_start, trip_ev.route_end)
    return route_times


def improve_existing_solution(
    solution: dict,
    candidates: pd.DataFrame,
    vehicles: pd.DataFrame,
    compatibility: pd.DataFrame,
    iterations: int = 2000,
    rng_seed: int = 0,
    plan_id: str = "ALNS",
    max_candidates: int = 8,
    log_every: int = 0,
    on_progress=None,
    excluded_vehicle_days: set[tuple[str, str]] | None = None,
    time_budget_s: float | None = None,
    no_improve_patience: int | None = None,
    converge_pct: float | None = None,
    converge_window: int | None = None,
    converge_min_iters: int | None = None,
    rejected: list[RejectedJob] | None = None,
    sa_temp_fraction: float = 0.0,
    sa_cooling: float = 0.999,
    repair_every: int = 1,
    regret_repair: bool = False,
    pinned_job_ids=None,
    avail_overrides: dict[tuple[str, str], str] | None = None,
    day_flex: bool = False,
    watermarks: dict | None = None,
    commit_floor=None,
    now=None,
    locked_keys: dict | None = None,
    beta: float = 0.0,
    reference_routes: dict | None = None,
    disturbance_weight: dict | None = None,
    disturbance_gamma: float = 0.5,
    run_merge_sweep: bool = True,
) -> RouteSeedImprovement:
    """E6 dynamic-dispatch hooks (spec 4.7a), all default-off / bit-identical:
    ``watermarks`` {(vid, day) -> tuple of committed stop counts per trip} —
    committed stops become pinned (never destroyed) and insertions into those
    trips start after the watermark; ``commit_floor`` (datetime) — no plannable
    work before now + delta_R1; ``locked_keys`` {job_id -> (vid, day)} — an
    in-flight suffix job may re-sequence on its own vehicle-day but never move
    to another (onboard freight cannot change trucks)."""
    normalised = {key: _as_trips(value) for key, value in solution.items()}
    vehicle_meta = _build_vehicle_meta(vehicles)
    selected_job_meta = _build_job_meta(normalised, candidates, compatibility, day_flex=day_flex)
    all_job_meta = _build_all_candidate_meta(candidates, compatibility, day_flex=day_flex)
    selected_leg_ids = {
        str(meta.candidate.get("leg_id", "") or "")
        for meta in selected_job_meta.values()
    }
    repairable_meta, static_remaining = _repairable_unassigned_meta(rejected, all_job_meta, selected_leg_ids)

    improvement = improve_solution(
        normalised,
        selected_job_meta,
        vehicle_meta,
        iterations,
        rng_seed,
        max_candidates=max_candidates,
        log_every=log_every,
        on_progress=on_progress,
        excluded_vehicle_days=excluded_vehicle_days,
        time_budget_s=time_budget_s,
        no_improve_patience=no_improve_patience,
        converge_pct=converge_pct,
        converge_window=converge_window,
        converge_min_iters=converge_min_iters,
        unassigned_meta=repairable_meta,
        sa_temp_fraction=sa_temp_fraction,
        sa_cooling=sa_cooling,
        repair_every=repair_every,
        regret_repair=regret_repair,
        pinned_job_ids=pinned_job_ids,
        avail_overrides=avail_overrides,
        watermarks=watermarks,
        commit_floor=commit_floor, now=now,
        locked_keys=locked_keys,
        beta=beta,
        reference_routes=reference_routes,
        disturbance_weight=disturbance_weight,
        disturbance_gamma=disturbance_gamma,
    )

    final_job_meta = dict(selected_job_meta)
    for jid, meta in all_job_meta.items():
        if jid in _served(improvement.solution):
            final_job_meta[jid] = meta

    remaining_rejected = list(static_remaining)
    remaining_rejected.extend(RejectedJob(job_id=jid, reason="NO_FEASIBLE_ROUTE") for jid in improvement.remaining_unassigned)

    sweep = None
    if MERGE_SWEEP_ENABLED and run_merge_sweep:
        sweep = apply_zero_cost_merges(
            improvement.solution, final_job_meta, vehicle_meta,
            lambda vm, day: _rv_ov(vm, day, avail_overrides), excluded_vehicle_days or set(),
            frozenset(str(j) for j in (pinned_job_ids or ())),
            # E6 guards (WT255131): the sweep is an insertion path like any other —
            # it must respect the watermark and the commit floor on launched trips.
            watermarks=watermarks, commit_floor=commit_floor, now=now)
        if sweep.applied:
            improvement.km_after += sweep.km_delta

    selected = _records_from_solution(improvement.solution, final_job_meta, vehicle_meta, plan_id, avail_overrides)
    if _CONSERVE:
        emitted = {str(getattr(r, "job_id", "") or "") for r in selected}
        solved = _served(improvement.solution)
        if emitted != solved:
            missing = solved - emitted
            lines = [
                f"ALNS emission diverged from solution: "
                f"in_solution_not_emitted={sorted(missing)[:20]} "
                f"emitted_not_in_solution={sorted(emitted - solved)[:20]}"
            ]
            for (vid, day), trips in improvement.solution.items():
                tt = _as_trips(trips)
                jids = {j.job_id for j in _flatten(tt)}
                hit = sorted(jids & missing)
                if not hit:
                    continue
                ev = evaluate_day(_rv_ov(vehicle_meta[vid], day, avail_overrides), tt)
                no_cand = [j for j in hit if final_job_meta.get(j) is None]
                lines.append(
                    f"  key=({vid},{day}) trips={len(tt)} evals={len(ev.trip_evaluations)} "
                    f"day_feasible={ev.feasible} reason={ev.failure_reason!r} "
                    f"missing_here={hit} missing_candidate={no_cand}"
                )
            raise AssertionError("\n".join(lines))
    route_totals = _route_totals_from_solution(improvement.solution, vehicle_meta, avail_overrides)
    route_times = _route_times_from_solution(improvement.solution, vehicle_meta, avail_overrides)
    cost_decomp = cost_decomposition_from_solution(improvement.solution, vehicle_meta, avail_overrides)
    return RouteSeedImprovement(
        selected=selected,
        km_before=improvement.km_before,
        km_after=improvement.km_after,
        served_before=improvement.served_before,
        served_after=improvement.served_after,
        accepted_moves=improvement.accepted_moves,
        route_totals=route_totals,
        route_times=route_times,
        cost_decomposition=cost_decomp,
        seed_by_type=dict(getattr(improvement, "seed_by_type", {}) or {}),
        solution=improvement.solution,
        inserted_jobs=improvement.inserted_jobs,
        attempted_jobs=improvement.attempted_jobs,
        remaining_rejected=remaining_rejected,
        cost_before=improvement.cost_before,
        cost_after=improvement.cost_after,
        merge_sweep=sweep,
        stop_reason=getattr(improvement, "stop_reason", ""),
        iterations_run=getattr(improvement, "iterations_run", 0),
    )


def _supersede_pending(cand_ins: dict, pending: list[str], new_meta: dict) -> list[str]:
    """After a leg inserts, remove its now-SUPERSEDED alternatives from ``pending``.

    Option-set leg: drop pending legs of the SAME ``option_set`` but a DIFFERENT
    ``option_group`` (the rival mode). The same-group partner (XDOCK's XC<->XD) is
    kept — it still needs inserting, ordered by REQUIRES_PRIOR_PICKUP. Non-optional
    leg (``option_set`` == ""): preserve the legacy one-branch-per-order_id drop.
    """
    oset = str((cand_ins or {}).get("option_set", "") or "")
    ogrp = str((cand_ins or {}).get("option_group", "") or "")
    if oset and ogrp:
        return [p for p in pending
                if not (str((new_meta[p].candidate or {}).get("option_set", "") or "") == oset
                        and str((new_meta[p].candidate or {}).get("option_group", "") or "")
                        not in ("", ogrp))]
    oid = str((cand_ins or {}).get("order_id", "") or "")
    if oid:
        return [p for p in pending
                if str((new_meta[p].candidate or {}).get("order_id", "") or "") != oid]
    return list(pending)


def insertion_pass(
    solution: dict,
    new_meta: dict[str, "JobMeta"],
    vehicle_meta: dict[str, VehicleMeta],
    *,
    excluded: set | None = None,
    pinned: frozenset = frozenset(),
    avail_overrides: dict | None = None,
    watermarks: dict | None = None,
    commit_floor=None,
    now=None,
    locked_keys: dict | None = None,
    regret: bool = True,
    option_index: dict[str, tuple[str, str]] | None = None,
) -> tuple[dict, list[str], list[str]]:
    """E6 micro-pass (spec 4.7a R2): one-shot insertion of newly-revealed orders
    into the CURRENT solution — open suffixes included — with no destroy phase.
    Regret-2 ordering by default: in a one-shot pass there is no later destroy
    to repair a greedy mistake, so the job that loses most by waiting goes
    first (this is the regime where E3's regret-is-worthless verdict may
    invert). Returns (new_solution, inserted_job_ids, failed_job_ids).

    ``option_index`` (job_id -> (option_set, option_group)), optional: DIRECT/XDOCK
    mutual exclusion against jobs already resident in ``solution`` from an EARLIER
    epoch (2026-07-28, R888GNW/2026-02-02). The seed/ALNS-repair trial seeds an
    OptionMutex from its own working set and the same-batch case is already handled
    by ``_supersede_pending`` below, but neither sees a rival leg committed in a
    PRIOR epoch when THIS pass reveals the other option group as a brand-new
    arrival — a different job_id, so the exact-job_id ``served_jobs`` filter the
    caller applies upstream does not catch it either. Without this, the freight
    ends up carrying both option groups, discovered only as a commit-boundary
    conflict at final emission (``ledger.drop_superseded_option_legs``)."""
    work = {key: [list(t) for t in _as_trips(trips)] for key, trips in solution.items()}
    rv_cache: dict[tuple[str, str], RouteVehicle] = {}
    excl = excluded or set()
    inserted: list[str] = []
    failed: list[str] = []
    pending = sorted(new_meta, key=lambda jid: _priority_key(new_meta[jid]))
    if option_index:
        mutex = OptionMutex()
        for trips in work.values():
            for trip in trips:
                for j in trip:
                    pair = option_index.get(j.job_id)
                    if pair:
                        mutex.assign({"option_set": pair[0], "option_group": pair[1]})
        blocked = [jid for jid in pending if not mutex.insertable(new_meta[jid].candidate or {})]
        if blocked:
            failed.extend(blocked)
            blocked_set = set(blocked)
            pending = [jid for jid in pending if jid not in blocked_set]
    while pending:
        pick = None
        if regret and len(pending) >= 2:
            scored = []
            for jid in pending:
                ranked = _ranked_inserts_for_job(
                    new_meta[jid], work, work, vehicle_meta, rv_cache, excl, top=2,
                    avail_overrides=avail_overrides, watermarks=watermarks,
                    commit_floor=commit_floor, now=now, locked_keys=locked_keys)
                if not ranked:
                    continue
                best_delta = ranked[0][0]
                reg = (ranked[1][0] - best_delta) if len(ranked) > 1 else float("inf")
                scored.append((reg, best_delta, jid, ranked[0][1], ranked[0][2]))
            if scored:
                scored.sort(key=lambda t: (-t[0], t[1]))
                _r, _d, jid, key, new_trips = scored[0]
                pick = (jid, key, new_trips)
            else:
                failed.extend(pending)
                break
        else:
            jid = pending[0]
            best = _best_insert_for_job(
                new_meta[jid], work, work, vehicle_meta, rv_cache, excl,
                allow_eject=False, pinned=pinned, avail_overrides=avail_overrides,
                watermarks=watermarks, commit_floor=commit_floor, now=now, locked_keys=locked_keys)
            if best is None:
                failed.append(jid)
                pending.remove(jid)
                continue
            _d, key, new_trips, _ev = best
            pick = (jid, key, new_trips)
        jid, key, new_trips = pick
        work[key] = new_trips
        rv_cache.pop(key, None)
        inserted.append(jid)
        pending.remove(jid)
        # Once a leg inserts, its now-SUPERSEDED alternatives are dropped from
        # pending (the order is served on this branch; failing the rivals would
        # be a lie). Option-group-aware: for an option-set leg drop only the
        # RIVAL group (DIRECT<->XDOCK), keeping the same-group XDOCK partner
        # (XC<->XD), which still needs inserting.
        pending = _supersede_pending(new_meta[jid].candidate or {}, pending, new_meta)
    return work, inserted, failed


def improve_route_seed(
    seed_result,
    candidates: pd.DataFrame,
    vehicles: pd.DataFrame,
    compatibility: pd.DataFrame,
    iterations: int = 2000,
    rng_seed: int = 0,
    plan_id: str = "ALNS",
    max_candidates: int = 8,
    log_every: int = 0,
    on_progress=None,
    excluded_vehicle_days: set[tuple[str, str]] | None = None,
    time_budget_s: float | None = None,
    no_improve_patience: int | None = None,
    converge_pct: float | None = None,
    converge_window: int | None = None,
    converge_min_iters: int | None = None,
    rejected: list[RejectedJob] | None = None,
    sa_temp_fraction: float = 0.0,
    sa_cooling: float = 0.999,
    repair_every: int = 1,
    regret_repair: bool = False,
    pinned_job_ids=None,
    avail_overrides: dict[tuple[str, str], str] | None = None,
    day_flex: bool = False,
    extra_routes: dict | None = None,
    watermarks: dict | None = None,
    commit_floor=None,
    now=None,
    locked_keys: dict | None = None,
    beta: float = 0.0,
    reference_routes: dict | None = None,
    disturbance_weight: dict | None = None,
    disturbance_gamma: float = 0.5,
) -> RouteSeedImprovement:
    source_routes = getattr(seed_result, "route_trips", None)
    if not source_routes:
        source_routes = {k: _as_trips(v) for k, v in seed_result.route_jobs.items()}
    if rejected is None:
        rejected = getattr(seed_result, "rejected", None)
    if extra_routes:
        # E6 dynamic: in-flight trips (committed departures, open suffixes) are
        # INJECTED into the initial solution — the seed planned around them
        # (their legs excluded from its view), ALNS optimizes across both under
        # the watermark/floor/lock guards.
        source_routes = dict(source_routes)
        injected_counts: dict = {}
        for key, trips in extra_routes.items():
            base = _as_trips(source_routes.get(key, []))
            injected = _as_trips(trips)
            source_routes[key] = injected + base
            injected_counts[key] = len(injected)
        # The merge is where marginal feasibility flips: the seed evaluated its
        # remainder trips from the override start in ISOLATION, but chaining
        # them behind the in-flight trips adds reloads and break carry. Validate
        # each merged day once; where it breaks, strip the seed-side trips and
        # hand their jobs to the repair pool — ALNS reinserts them under the
        # true chained evaluation (or they surface as honest rejections).
        vm = _build_vehicle_meta(vehicles)
        rejected = list(rejected or [])
        for key, n_inj in injected_counts.items():
            trips = source_routes.get(key, [])
            vid, day = key
            if vid not in vm:
                continue
            ov_rv = _rv_ov(vm[vid], day, avail_overrides)
            if len(trips) > n_inj:
                ev = evaluate_day(ov_rv, trips, detail=False)
                if ev.feasible:
                    continue
                for trip in trips[n_inj:]:
                    for j in trip:
                        rejected.append(RejectedJob(job_id=j.job_id, reason="INJECTION_CHAIN"))
                trips = trips[:n_inj]
                source_routes[key] = trips
            # Fix 3 (structural review): the in-flight core itself must be
            # feasible under THIS solve's view — it was feasible under the view
            # it was committed with, so infeasibility here means the override
            # views diverged. Fail at the seam, loudly, not 2,000 iterations
            # later at record minting.
            core = evaluate_day(ov_rv, trips, detail=False)
            if not core.feasible:
                raise ValueError(
                    f"committed day arrives infeasible at injection: {key} "
                    f"reason={core.failure_reason} "
                    f"override={(avail_overrides or {}).get(key)!r} "
                    f"trips={[len(t) for t in trips]} — override views diverged "
                    f"(structural review Fix 3)")
    return improve_existing_solution(
        source_routes,
        candidates,
        vehicles,
        compatibility,
        iterations=iterations,
        rng_seed=rng_seed,
        plan_id=plan_id,
        max_candidates=max_candidates,
        log_every=log_every,
        on_progress=on_progress,
        excluded_vehicle_days=excluded_vehicle_days,
        time_budget_s=time_budget_s,
        no_improve_patience=no_improve_patience,
        converge_pct=converge_pct,
        converge_window=converge_window,
        converge_min_iters=converge_min_iters,
        rejected=rejected,
        sa_temp_fraction=sa_temp_fraction,
        sa_cooling=sa_cooling,
        repair_every=repair_every,
        regret_repair=regret_repair,
        pinned_job_ids=pinned_job_ids,
        avail_overrides=avail_overrides,
        day_flex=day_flex,
        watermarks=watermarks,
        commit_floor=commit_floor, now=now,
        locked_keys=locked_keys,
        beta=beta,
        reference_routes=reference_routes,
        disturbance_weight=disturbance_weight,
        disturbance_gamma=disturbance_gamma,
    )




