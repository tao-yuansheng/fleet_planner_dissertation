"""E6 rolling-horizon epoch state: grid, commit bands, freeze selection, duty
carry, slip pool and the service ledger.

Semantics (spec 2026-07-10-rolling-horizon-e6-design.md):

* Epochs at 00:00 (midnight seed) / 12:00 (noon re-opt) each operating day; a
  plan computed at epoch ``t`` may only commit departures at or after
  ``t + DELTA_MIN`` (decision lag: solve wall + dispatch overhead; the 00:00
  midnight seed gives the warehouse its full loading runway before the day's
  06:00 collection micros begin).
* At the NEXT epoch, every trip that departed inside the previous commit band
  is frozen: its records leave the solver's problem for good, its orders are
  served, and its vehicle re-enters the pool carrying the duty it consumed
  (``duty_after_freeze`` re-evaluates the frozen prefix with the SAME evaluator
  the solver used, so the statutory-break accumulator and the remaining daily
  driving budget cannot drift).
* An order targeted today but not committed today slips to tomorrow's pool with
  a reason; slipped orders age (seed priority) and may slip again. Whatever is
  still open when the window closes is UNSERVED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import pandas as pd

from freight_planner.routing_adapter import DutyOverride, RouteVehicle, evaluate_day

# Two decisions per operating day, matching the depot's own rhythm: 00:00 is the
# midnight SEED — the first optimization of the operating day, planning off the
# overnight book plus the network-import orders already spawned at the depot (the
# invisible hub delivers them); 12:00 is the operation's demonstrated booking
# cut-off and re-optimizes the afternoon. Moved from 03:00 to 00:00 (2026-07-24):
# the real operation plans the next day's network imports at 20:00 the evening
# before, so seeding at the start of the day (midnight) is the closest simple fit
# to that rhythm. The 08:00 epoch was removed by design review 2026-07-10: the
# reveal decomposition showed it learns 3-40 orders/day (booking phones are quiet
# at dawn) — informationally hollow, pure commitment granularity.
EPOCH_TIMES = (time(0, 0), time(12, 0))
DELTA_MIN = 60          # VESTIGIAL (2026-07-13): kept only as the --delta-min CLI default.
                        # The live dispatcher gates everything on the single 90-min commit
                        # freeze (Watermarks.floor = now + delta_r1), NOT this 60-min lag.
NO_EPOCH = "NO_EPOCH"   # booked after the day's last epoch -> discovered already late


def epoch_grid(window_start: date, window_end: date,
               times: tuple[time, ...] = EPOCH_TIMES) -> list[datetime]:
    """All decision epochs of the window, ordered (3 per operating day)."""
    out: list[datetime] = []
    d = window_start
    while d <= window_end:
        out.extend(datetime.combine(d, t) for t in times)
        d = d + timedelta(days=1)
    return out


# commit_band was the 60-min "decision-lag" tier of a two-tier commit model. It is UNUSED
# in the live pipeline (only ever exercised by test_commit_band_delta_and_final) — the
# dispatcher gates on the single 90-min commit freeze. Commented out 2026-07-13 (user:
# simplify, remove the vestigial 60). Restore this + its test to revive the two-tier model.
# def commit_band(grid: list[datetime], i: int,
#                 delta_min: int = DELTA_MIN) -> tuple[datetime, datetime | None]:
#     """Departure interval epoch ``i`` commits: [epoch+delta, next_epoch+delta).
#     The window's last epoch commits everything from epoch+delta onward."""
#     lo = grid[i] + timedelta(minutes=delta_min)
#     hi = grid[i + 1] + timedelta(minutes=delta_min) if i + 1 < len(grid) else None
#     return lo, hi


def committed_stop_count(depot_depart_iso: str, stops: list, now: datetime) -> int:
    """How many leading stops of one trip are committed at ``now`` (already
    incorporating delta_R1 in the caller's ``now``).

    A stop is committed when the driver has BEGUN it (arrived by ``now``) or is
    already rolling toward it — the previous stop's departure (the trip's depot
    departure for the first stop) has passed. ``stops`` = [(arrive_iso,
    depart_iso), ...] in sequence order.
    """
    def _dt(s):
        try:
            return datetime.fromisoformat(str(s))
        except (TypeError, ValueError):
            return None

    prev_depart = _dt(depot_depart_iso)
    n = 0
    for arrive_iso, depart_iso in stops:
        arrive = _dt(arrive_iso)
        begun = (arrive is not None and arrive <= now)
        rolling = (prev_depart is not None and prev_depart <= now)
        if begun or rolling:
            n += 1
            prev_depart = _dt(depart_iso)
        else:
            break
    return n


@dataclass
class Watermarks:
    """Stop-level freeze periods (spec 4.7a). ``marks[(vid, day, trip_index)]``
    = number of leading stops that are fact; everything after is the OPEN
    SUFFIX (insertable / re-sequenceable, pinned to its vehicle in v1).
    ``strict=True`` pins the mark to the trip end at first advance — the old
    whole-trip freeze as a degenerate config."""
    delta_r1_min: int = 90     # solve wall + driver notification + contingency
    strict: bool = False
    marks: dict = field(default_factory=dict)

    def floor(self, now: datetime) -> datetime:
        return now + timedelta(minutes=self.delta_r1_min)

    def advance(self, key: tuple, depot_depart_iso: str, stops: list,
                now: datetime) -> int:
        new = len(stops) if self.strict else committed_stop_count(
            depot_depart_iso, stops, self.floor(now))
        cur = int(self.marks.get(key, 0))
        self.marks[key] = max(cur, new)
        return self.marks[key]


@dataclass
class PartialFreeze:
    """A vehicle-day with some trips frozen and some still free."""
    frozen_trips: list          # list[list[RouteJob]], in trip order
    frozen_trip_indices: list[int]


@dataclass
class FrozenSlice:
    """What one epoch's commit band takes out of the solver's problem."""
    records: list = field(default_factory=list)          # SelectedPlanRecord
    served_order_ids: set = field(default_factory=set)
    full_day_frozen: set = field(default_factory=set)    # (vehicle_id, day)
    partial: dict = field(default_factory=dict)          # (vehicle_id, day) -> PartialFreeze
    route_totals: dict = field(default_factory=dict)     # frozen keys only
    route_times: dict = field(default_factory=dict)


def _trip_depart(route_times: dict, vid: str, day: str, trip_index: int,
                 n_trips: int) -> datetime | None:
    key = f"ROUTE:{vid}:{day}#T{trip_index}"
    val = route_times.get(key)
    if val is None and n_trips == 1:
        val = route_times.get(f"ROUTE:{vid}:{day}")
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val[0]))
    except ValueError:
        return None


def select_frozen(solution: dict, records: list, route_times: dict,
                  band: tuple[datetime, datetime | None]) -> FrozenSlice:
    """Freeze every daily trip whose depot departure falls inside ``band``.

    ``solution``: {(vehicle_id, day) -> list[trip]} (the improvement's routes);
    ``records``: the improvement's SelectedPlanRecords (trip_index 1-based,
    matching the ``#T{k}`` route_times keys).
    """
    lo, hi = band
    fz = FrozenSlice()
    frozen_keys: dict[tuple[str, str], list[int]] = {}
    for (vid, day), trips in solution.items():
        trips = [list(t) for t in trips if t] if trips and not hasattr(trips[0], "job_id") else (
            [list(trips)] if trips else [])
        if not trips:
            continue
        chosen: list[int] = []
        for k in range(1, len(trips) + 1):
            dep = _trip_depart(route_times, str(vid), str(day), k, len(trips))
            if dep is None:
                continue
            if dep >= lo and (hi is None or dep < hi):
                chosen.append(k)
        if not chosen:
            continue
        frozen_keys[(str(vid), str(day))] = chosen
        if len(chosen) == len(trips):
            fz.full_day_frozen.add((str(vid), str(day)))
        # EVERY frozen vehicle-day exposes its frozen trips so the caller can
        # compute a duty carry and return the vehicle to the pool at its depot
        # return (spec 4.4). Retiring a single-trip day outright starves every
        # afternoon epoch: 91.8% of vehicle-days are single-trip.
        fz.partial[(str(vid), str(day))] = PartialFreeze(
            frozen_trips=[trips[k - 1] for k in chosen],
            frozen_trip_indices=chosen)
        rid = f"ROUTE:{vid}:{day}"
        for key in (rid, *(f"{rid}#T{k}" for k in chosen)):
            if key in route_times:
                fz.route_times[key] = route_times[key]

    for r in records:
        key = (str(r.vehicle_id), str(r.service_date))
        if key in frozen_keys and int(r.trip_index) in frozen_keys[key]:
            fz.records.append(r)
            if str(r.order_id):
                fz.served_order_ids.add(str(r.order_id))
    return fz


def duty_after_freeze(vehicle: RouteVehicle, frozen_trips: list) -> DutyOverride | None:
    """Re-evaluate the frozen prefix with the solver's own evaluator and return
    the state the vehicle re-enters the pool with — or None when the prefix
    already exhausts the day (returns after shift end / infeasible)."""
    from freight_planner.shared.config import MAX_DRIVING_H_PER_DAY

    ev = evaluate_day(vehicle, frozen_trips)
    if not ev.feasible:
        return None
    end = ev.day_end
    try:
        if vehicle.shift_end and datetime.fromisoformat(str(end)) >= datetime.fromisoformat(
                str(vehicle.shift_end)):
            return None
    except ValueError:
        pass
    last = ev.trip_evaluations[-1] if ev.trip_evaluations else None
    return DutyOverride(
        start_iso=str(end),
        drive_since_break0=float(last.end_drive_since_break) if last is not None else 0.0,
        drive_minutes_left=max(0.0, MAX_DRIVING_H_PER_DAY * 60.0 - float(ev.total_drive_minutes)),
    )


@dataclass
class RollingState:
    """Accumulates commitments and the slip pool across the window's epochs."""
    window_start: date
    window_end: date
    served: set = field(default_factory=set)             # order_id
    served_day: dict = field(default_factory=dict)       # order_id -> day served
    collected_day: dict = field(default_factory=dict)    # order_id -> day its pickup froze
    staged: dict = field(default_factory=dict)           # order_id -> (depot, ready_iso)
    slip_pool: dict = field(default_factory=dict)        # order_id -> days late so far
    slip_reason: dict = field(default_factory=dict)      # order_id -> first failure reason
    unserved: dict = field(default_factory=dict)         # order_id -> reason (window closed)
    final_slip: dict = field(default_factory=dict)       # order_id -> days it tried before failing
    records: list = field(default_factory=list)
    route_totals: dict = field(default_factory=dict)
    route_times: dict = field(default_factory=dict)
    reserved: set = field(default_factory=set)           # (vehicle_id, day) frozen whole
    avail_overrides: dict = field(default_factory=dict)  # (vehicle_id, day) -> DutyOverride|"HH:MM"
    manifest: list = field(default_factory=list)

    def note_served(self, order_ids: set, day: str) -> None:
        for oid in order_ids:
            oid = str(oid)
            if oid in self.served:
                continue
            self.served.add(oid)
            self.served_day[oid] = day
            self.slip_pool.pop(oid, None)

    def roll_day_end(self, day: date, targeted_today: set, visible_today: set,
                     reasons: dict) -> None:
        """Orders whose target day (origin_date or slipped-to day) was ``day``
        and were not committed roll into tomorrow's pool one day later — or are
        UNSERVED when the window has closed. Never-visible-today orders were
        booked after the day's last epoch: reason NO_EPOCH."""
        window_closed = day >= self.window_end
        for oid in sorted(targeted_today):
            oid = str(oid)
            if oid in self.served:
                continue
            reason = self.slip_reason.get(oid) or (
                reasons.get(oid) if oid in visible_today else NO_EPOCH) or "UNKNOWN"
            if window_closed:
                # preserve the accumulated slip count for the ledger BEFORE dropping
                # from the live pool — else an order that fought for N days reports
                # days_late=0 and its carryover is invisible in the outputs
                self.final_slip[oid] = self.slip_pool.pop(oid, 0)
                self.unserved[oid] = reason
                continue
            self.slip_pool[oid] = self.slip_pool.get(oid, 0) + 1
            self.slip_reason.setdefault(oid, reason)

    def reconcile_to_plan(self, plan_collected: set | dict[str, str], collect_ids: set) -> None:
        """Finalize honesty: an order is served only if its collection leg actually
        survived into the EMITTED plan (``plan_collected``). Promotes plan
        collections to served; demotes anything counted served during the loop but
        NOT in the plan — a delivery-marked order, or a pickup placed at a day-close
        then pruned at the finalize — to UNSERVED. Makes the ledger match the plan
        by construction, so no over-report can hide an orphan or a pruned pickup."""
        plan_days = (dict(plan_collected) if isinstance(plan_collected, dict)
                     else {str(oid): "" for oid in plan_collected})
        plan = set(plan_days) & set(collect_ids)
        # The emitted plan is the final source of truth.  A pickup can survive
        # into the accumulated committed plan even when its transient rolling
        # state entry was lost after an earlier failed micro insertion.  Recover
        # that collection (and its real committed day) before reconciling.
        for oid in plan:
            day = str(plan_days.get(oid, "") or "")[:10]
            if day:
                prior = self.collected_day.get(oid, "")
                if not prior or day < prior:
                    self.collected_day[oid] = day
        for oid, d in list(self.collected_day.items()):
            if oid in plan:
                self.served.add(oid)
                self.served_day.setdefault(oid, d)
                self.slip_pool.pop(oid, None)
                self.unserved.pop(oid, None)
                self.final_slip.pop(oid, None)
        # anything we thought handled (marked served OR collected) but whose pickup
        # is not in the plan -> honestly UNSERVED (keeps its failure reason)
        for oid in ((set(self.served) | set(self.collected_day)) & set(collect_ids)) - plan:
            self.served.discard(oid)
            self.served_day.pop(oid, None)
            self.unserved.setdefault(oid, self.slip_reason.get(oid) or "NOT_IN_PLAN")

    def slip_priority_map(self) -> dict:
        return dict(self.slip_pool)

    def ledger_frame(self, target_day: dict) -> pd.DataFrame:
        """One row per order seen by the rolling loop. ``target_day``:
        order_id -> original target day (origin_date)."""
        rows = []
        for oid, tday in target_day.items():
            oid = str(oid)
            if oid in self.served:
                sd = str(self.served_day.get(oid, ""))[:10]
                late = (pd.Timestamp(sd) - pd.Timestamp(tday)).days if sd and tday else 0
                outcome = "ON_TIME" if late <= 0 else f"SLIPPED({late})"
                reason = self.slip_reason.get(oid, "")
                days_late = max(0, late)                       # actual days served late
            elif oid in self.unserved:
                outcome, reason = "UNSERVED", self.unserved[oid]
                days_late = self.final_slip.get(oid, 0)        # days it tried before failing
            elif oid in self.slip_pool:
                outcome, reason = "OPEN", self.slip_reason.get(oid, "")
                days_late = self.slip_pool.get(oid, 0)         # days tried so far
            else:
                outcome, reason, days_late = "NOT_PLANNED", "", 0
            rows.append({"order_id": oid, "target_day": str(tday), "outcome": outcome,
                         "days_late": days_late, "reason": reason})
        return pd.DataFrame(rows)
