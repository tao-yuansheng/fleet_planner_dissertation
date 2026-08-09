# Intraday Tour Attachment (free-riding late orders onto in-flight tours) — Design

**Date:** 2026-07-12
**Status:** draft for review
**Owner:** freight_planner / run_rolling + tours
**Builds on:** `2026-07-12-multiday-midleg-overnight-design.md` (`day_starts` +
tighter packing — the modifiable-tail foundation)

## Goal

Serve far orders **booked after the 03:00 seed** that would otherwise go
unassigned, by free-riding them onto a multi-day tour **already in flight** whose
route still passes near the pickup. The tour's remaining multi-day span is the
buffer: an order booked today can be

- **collected today** — slotted into the tour's remaining route for the current
  day, or
- **collected a few days later** — slotted into a *future* day of the same tour,

whichever is feasible and cheapest, **without ever adding a day to the tour** and
without disturbing anything the driver has already committed to. This is the
coverage lever for the "booked late, still needs a pickup" case the daily rolling
loop currently drops (`NO_FEASIBLE_ROUTE`).

## Background — the blocker, verified

Tours are formed at the 03:00 seed. In [run_rolling.py:966-983](../../../run_rolling.py),
a tour is frozen **whole** the instant its *first* departure crosses the commit
horizon: every one of its multi-day stops goes into `merged_tour_records`, its rid
enters `frozen_tour_rids`, and it is excluded from every later re-solve. So an
in-flight tour is 100% immutable today — a late order can't ride it even when the
truck is heading straight past the pickup.

Two assets already exist to build on:
- **The insertion primitive:** `tours.try_insert_tour_job(vehicle, jobs, candidate,
  due_offsets, floor_offsets)` — every-position insert with full tour physics
  (capacity peak, day caps, LATE, EARLY), lowest-km winner or `None`.
- **The modifiable-tail foundation:** `TourEvaluation.day_starts` (mid-leg
  overnight) already treats each tour-day as a single-day route with a start
  location + carried freight.

## Design

### 1. Frontier-split the in-flight tour (committed head / mutable tail)

Relax tour immutability from **whole-tour** to **stop-level**, reusing the daily
side's exact safety primitive `epoch_state.committed_stop_count`. For an in-flight
tour at epoch `now`, split its stop sequence at the committed count computed with
the frontier `now + delta_r1`:

- **Head** — the leading stops the driver has **begun** (arrived) or is **already
  rolling toward** (the previous stop's departure has passed by the frontier).
  **Bit-frozen. Never touched.** This is the non-negotiable safety rule: a vehicle
  driving toward its next stop is *never* redirected mid-leg — inserts can only
  land in the suffix after the stop it is currently heading to.
- **Tail** — the open suffix after the committed head: the modifiable remainder,
  spanning the rest of today through the tour's last day.

This is a *controlled additive mutation*, not a return to the general re-solve: a
frozen tour is never re-optimized (its shape stays stable across epochs); the
attachment step may only **insert** into its tail and re-emit that slice.

**Overnight nuance (v1 accepts conservative):** `committed_stop_count` measures
"rolling" as `prev_depart ≤ frontier`, which doesn't model the overnight rest — so
on the *morning of* a future tour day it locks that day's first stop (its prev
depart was the night before) even though the truck hasn't pulled away yet. That is
strictly *over*-locking (safe, never a mid-leg risk); it only mildly limits
prepending to a day already dawning. Future days not yet reached stay fully open,
so the "collect a few days later" case is unaffected. An overnight-aware count
(free the first stop of a not-yet-started day) is a noted follow-up, not v1.

### 2. Resume state at the frontier

Reconstruct where the vehicle actually is when the tail begins:

- **Position** — the last committed (head) stop's node/coords (the truck is there
  or en route from it). If mid-leg between head and tail under
  `MULTIDAY_MIDLEG_OVERNIGHT`, the interpolated frontier point.
- **Carried freight** — pallets/kg aboard after the head (deliveries dropped,
  pickups collected so far).
- **Consumed duty on the current day** — `day_drive`, `day_elapsed`,
  `drive_since_break` used by the head today. This is a `_DayCursor` seed.

### 3. Evaluate the tail from the resume state (new `evaluate_tour` arg)

`evaluate_tour` today always starts fresh (day 0, empty duty). To keep **"no new
day" truthful** mid-afternoon, it gains an optional `resume: _DayCursor | None`
that seeds the first day's `day_drive/day_elapsed/drive_since_break` and start
position. Default `None` ⇒ byte-identical to today. With a resume state, the tail
is evaluated from the vehicle's real mid-day point, so an insert that would spill
today's remaining hours into a new day is correctly counted as +1 day and
therefore rejected by the guard. `try_insert_tour_job` threads `resume` through.

### 4. The guarded attachment primitive (shared with the seed-time idea)

```python
def best_tour_attachment(candidate, tail_jobs, resume, tour_days, *, vehicle,
                         due_offsets, floor_offsets, standalone_km,
                         max_extra_days=0):
    """Best position to free-ride `candidate` into an in-flight tour's tail, or None.
    Minimizes added km subject to:
      * try_insert_tour_job(vehicle, tail_jobs, candidate, due_offsets, floor_offsets,
        resume=resume) feasible (capacity peak, day caps, LATE, EARLY);
      * new_eval.days - len(committed_head_days) <= tour_days  (i.e. NO new tour day
        beyond the committed span) with max_extra_days=0;
      * added_km = new_eval.total_km - base_tail_km <= standalone_km."""
```

`due_offsets`/`floor_offsets` are set relative to the tail's resume day, so the
candidate's **readiness floor** (can't collect before it's ready) and **delivery
deadline** (LATE) decide which tail days are legal — that's exactly what lets the
same call land a pickup **today or a few days on**, whichever fits. The km guard
keeps it honest: attach only when it beats a dedicated run.

### 5. The intraday attachment step in `run_rolling`

A new step at each re-opt epoch (the 12:00 warm re-opt and the hourly micros),
gated on **`TOUR_ATTACH_ENABLED` (default False → byte-identical)**:

1. **Candidates:** orders visible at this epoch, still unassigned after the
   re-solve (rejected reason ∈ {`NO_FEASIBLE_ROUTE`, `NO_FEASIBLE_TOUR`,
   `NO_OK_VEHICLE_PAIR`}), booked today. Build the one-leg `RouteJob` (DIRECT
   carry for FULL_FLEET; single pickup/delivery leg otherwise). Pre-filter by
   proximity to any in-flight tour's tail stops (bounds cost).
2. **Hosts:** in-flight tours (`merged_tours` / their tail records), split at `H`.
3. **Attach:** for each candidate (ordered by due date, then pallets desc) call
   `best_tour_attachment(..., max_extra_days=0)`. On a hit, splice the new stop
   into that tour's tail records in `merged_tour_records`, mark the order served,
   and **notify the driver** via the same emission/runsheet path the daily
   micro-inserts already use. Greedy + fixed order ⇒ deterministic; a committed
   attachment is visible to later candidates in the same epoch.
4. **Ledger:** the attached order's collection leg is now in the plan, so it flows
   through the existing `collection_orders_in_plan` reconciliation as served.

### 6. Stability across epochs

Attachment is **additive-only**: an inserted stop is never removed. Once its
`planned_depart` crosses `H` it becomes head (committed) like any other stop; while
still in the tail it re-emits each epoch so the driver's forward plan is
consistent. A driver is never un-notified of a stop already given to them.

## Pairing with mid-leg overnight

`MULTIDAY_MIDLEG_OVERNIGHT` packs each tour-day tighter (banked residual), so an
in-flight tour carries more slack within its committed days — exactly the room a
free-riding attachment consumes under the no-new-day guard. Attachment works with
the flag off, but lands more rides with it on. (Recommend running attachment with
mid-leg on.)

## Invariants

- **Flag OFF ⇒ byte-identical** (step never runs; `resume=None` keeps
  `evaluate_tour` unchanged). Regression gate holds.
- **Committed head stops are never modified** — only tail insertion; the head
  slice of `merged_tour_records` is untouched.
- **No new tour day** — the tour's committed span never grows (`max_extra_days=0`,
  evaluated from the *real* resume duty so the guard can't be fooled mid-day).
- **No existing tail stop goes LATE**, order preserved (primitive's due check).
- **Capacity peak respected** across the carry (primitive re-evaluates the tail).
- **km rises only by committed detours**, each `≤` its standalone dedicated-run km;
  coverage only rises (rejected → served), never falls.
- **Additive + deterministic** ⇒ stable, no un-notification.

## Open decisions (for review)

1. **Epoch cadence** — run attachment at every hourly micro (max responsiveness,
   more churn) or only the 12:00 warm re-opt (cheaper, less notification traffic)?
   *Recommendation:* both, since the micros are where late bookings surface — but
   micros already only touch collection-side single-order inserts, so this fits.
2. **Backtrack policy** — rely on the km guard alone to prevent riding an
   already-passed pickup, or add a hard "pickup must be ahead of the frontier
   position" gate? *Recommendation:* km guard first (simpler); add the hard gate
   only if evidence shows uneconomic backtracks slipping through.
3. **Delivery of a collected order** — for a collect-only flow the pickup rides the
   tour back to the depot at tour end (no extra stop); for a FULL_FLEET DIRECT the
   delivery is a stop on the tail too. v1 inserts the single candidate leg the
   repair already models; multi-leg same-tour delivery is confirmed in-scope via
   the DIRECT two-point job.

## Testing strategy (TDD)

1. `evaluate_tour(resume=None)` byte-identical (regression); with a resume cursor,
   the first tail day starts from the seeded duty/position (a stop that fits a
   fresh morning but not the real remaining hours is correctly pushed +1 day).
2. `best_tour_attachment` accepts a same-day tail insert (order in `new_jobs`,
   `days` unchanged); accepts a *future-day* tail insert (pickup lands on a later
   tour day within span); rejects an insert that adds a day; rejects a LATE insert;
   rejects when `added_km > standalone_km`.
3. Frontier split: head stops (depart `< H`) are identical before/after; only the
   tail changes.
4. `run_rolling` step: a late far order booked at 10:30, unassigned after the
   re-solve, attaches to an in-flight Scotland tour heading its way — served, tour
   `days` unchanged, driver notified; km up by the detour only.
5. Flag OFF ⇒ a full rolling window is byte-identical (served/rejected/km/records).
6. Mid-leg interaction: an order that doesn't fit under mid-leg OFF attaches under
   mid-leg ON (tighter days created the slack).
7. Full suite green (regression).

## Out of scope / follow-ups

- **Seed-time attachment** (orders visible at 03:00 that failed assignment) — the
  same `best_tour_attachment` primitive, run once post-seed; a cheap add-on once
  the intraday path exists, but not required for the late-booking case.
- Reordering committed head stops (never) or tail stops beyond the single insert
  (v1 inserts, doesn't permute the tail).
- Multi-order batch attachment (v1 is one order at a time, greedy).
- Re-opening a tour to *drop* a marginal stop in favour of a better one (v1 is
  additive-only).
