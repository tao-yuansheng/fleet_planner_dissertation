# Soft delivery time windows (earliness/tardiness penalty) — design

**Date:** 2026-07-18 · **Status:** design approved, spec for implementation.

## Problem

Delivery time windows are enforced as a HARD `TIME_WINDOW` infeasibility in
`evaluate_route` (arrive past `latest_finish` -> route rejected), while coverage
degrades gracefully (an unservable order slips to the next day). The two disagree:
a delivery that cannot make its intra-day window today is forced to slip to
**tomorrow** rather than be delivered **slightly late today** — the model prefers a
day-late delivery over a minute-late one, which is backwards for the customer. And
because the service ledger is DAY-granular (`ON_TIME` = served on the target date),
intra-day lateness is invisible — the summary reads "all on time" regardless of
within-day timing.

Telematics (2026-07-18, [[delivery-window-model-validated]]) shows the real operation
treats windows as soft targets: range windows delivered within 69% / not-late 97%;
point/deadline windows delivered a median ~50 min EARLY. A soft time window with a
tardiness penalty is both the more standard VRPTW formulation and a faithful model of
the operation.

## Decision

Replace the hard delivery-window cutoff with a **soft earliness/tardiness penalty**,
layered on the existing lexicographic objective. The service hierarchy (best to
worst), confirmed with the stakeholder:

**on-time (0) < early (small penalty) < late (large convex penalty) < slipped/unserved (worst)**

- **slipped / unserved = worst** is handled by the EXISTING top lexicographic tier
  (`increase served jobs first, then reduce km`, alns.py). An unserved/slipped order
  fails the coverage tier, so it is worse than any same-day outcome by construction —
  no hand-tuned "biggest penalty" needed. Consequence (confirmed): a very-late
  same-day delivery is still preferred over an on-time next-day delivery, because
  coverage dominates; a delivery only slips when serving it today is INFEASIBLE
  (duty/end-of-day), making slip a genuine last resort.
- **on-time / early / late** are three levels WITHIN the cost tier: a penalty added
  to km, `earliness_coef · earliness  +  tardiness_coef · tardiness^2` (convex,
  p=2 — mirrors the overtime late-ramp: tolerate tiny slips, punish big ones).

Scope: **CUSTOMER_DELIVERY legs only.** Pickups/collections keep their current hard
windows (collection lateness has different downstream semantics — future work).

## Window references — the RAW (tight) window, per class

Earliness and tardiness are measured against the customer's TIGHT window
(`raw_window`), NOT the widened effective window:

| class | raw window | earliness bites? | tardiness bites? |
|---|---|---|---|
| range `09:00–12:00` | (09:00, 12:00) | before 09:00 (open>06:00) | after 12:00 |
| point `10:00–10:00` | (06:00, 10:00) | no (open=day start) | after 10:00 |
| single `11:00` | (06:00, 11:00) | no | after 11:00 |
| missing (no TWV) | — | no | **no deadline — no penalty** |

`earliness = max(0, window_open − service_start)`, `tardiness = max(0, service_start
− deadline)`, where `window_open = raw_window_start`, `deadline = raw_window_end`.
Because points/deadlines open at 06:00, earliness only ever bites RANGE windows —
exactly the classes with a customer-specified opening. Missing-window deliveries have
no `deadline` and incur no penalty (all-day, unchanged).

## Hard constraints retained (the penalty does NOT relax these)

- **Freight availability** (`depart_floor` / freight_ready) — cannot deliver before
  the freight is physically at the depot.
- **Non-anticipation** (`no_early_arrival`) — cannot act before the order was booked.
- **Duty / shift / operating day** — the vehicle's working hours; this is the
  effective bound on lateness (a delivery that cannot be done by end-of-day is
  duty-infeasible -> slips). No separate hard time-window bound is added.
- Capacity, depot_bound, etc. — unchanged.

The earliness penalty bites only in the physically-possible gap
`[freight_ready, window_open)`.

## Mechanism & plumbing

1. **Window policy** (`shared/scope._delivery_window_policy` /
   `_parse_twv_with_hardness`): for ALL delivery classes, `effective_window` =
   operating-day (all-day) so `earliest_start`/`latest_finish` become the HARD
   operating bounds; the tight `raw_window` is preserved and flows to the job's new
   soft-deadline fields. (This removes the hard_slot tight cutoff — ranges become
   soft, matching the "all classes" decision.)
2. **RouteJob** gains `window_open: str` and `deadline: str` (the tight raw window;
   empty for missing-window deliveries and non-deliveries). Plumbed through
   `CandidateJobRecord` (already carries `raw_window_start`; add `raw_window_end`) and
   `make_route_job`.
3. **Evaluator** (`routing_adapter.evaluate_route`): for `CUSTOMER_DELIVERY` with a
   non-empty `deadline`, do NOT return `TIME_WINDOW` infeasible on the customer
   deadline; instead accumulate `earliness`/`tardiness` and return their penalty as
   part of the route cost. The hard `latest_finish` (operating/duty bound) check
   stays for the day bound. Pickups and other kinds: `latest_finish` stays a hard
   `TIME_WINDOW` (unchanged). `DayEvaluation` carries a `lateness_cost` /
   `late_minutes` field; the ALNS cost tier adds it.
4. **Objective**: coverage stays the top lexicographic tier; the cost tier becomes
   `km_cost + driver_cost + … + lateness_penalty`. On-time is strictly preferred
   over early over late within a fixed served-count.

## Config & flags

- `config.SOFT_DELIVERY_WINDOWS: bool = True` (default ON).
- `config.TARDINESS_COEF: float` (λ), `TARDINESS_POWER: float = 2.0`,
  `EARLINESS_COEF: float` (small). Values set by calibration (see below); seeded with
  a principled first guess.
- CLI: `--hard-time-windows` (ablation — restores a hard cutoff on every stated
  deadline: the strict hard-VRPTW comparison arm), `--tardiness-coef`,
  `--earliness-coef`.

## Calibration (validation HELD per stakeholder — in-universe set may change)

λ and the earliness coef are calibrated by a short sweep so that: (a) ~1h tardiness
outweighs any realistic single-order km saving -> late is a genuine last resort;
(b) a few minutes' tardiness stays cheaper than the coverage cost of a day-slip
(automatic — coverage is a higher tier); (c) earliness is a gentle nudge (on-time
mildly preferred over early), small enough that the ~28%-early real behavior is still
reachable. **Do NOT run the calibration/validation probe yet** — the in-universe
order set may change, which would invalidate any numbers. Ship the mechanism +
unit tests; calibrate after the universe is settled.

## Reporting — make intra-day lateness visible

- Plan output (`plan_full` / route_stops): per-delivery `minutes_early`,
  `minutes_late` vs its raw window.
- `01_service_summary` / KPI: a lateness section — on-time %, count early/late,
  avg/max/percentile late-minutes, distribution. The day-granular ledger
  (`target_day`/`days_late`) is UNCHANGED (it tracks the right date); intra-day
  lateness becomes a new first-class reported dimension, replacing the misleading
  "always all on time" reading.

## Interactions

- **Readiness-lag experiment** gains a cost signal: a floored import delivered after
  its deadline now shows as TARDINESS (penalty), not just a slip — so the readiness
  ladder becomes sensitive even where it previously showed zero service loss. This
  strengthens that experiment.
- **Headline**: this changes feasibility (fewer hard rejections) and the objective —
  all campaign runs are post-change. Correctly lands BEFORE the freeze.

## Testing (TDD)

- Evaluator: a delivery past its deadline is FEASIBLE with a tardiness cost (not
  `TIME_WINDOW`); a pickup past its window is still `TIME_WINDOW` infeasible.
- Convexity: tardiness cost grows faster than linear (3h vs 1h > 3×).
- Earliness: penalized for a range window delivered before open; zero for a point
  delivered before its deadline (open=06:00); zero for missing windows.
- Ordering: among equal served-count solutions, on-time < early < late in cost;
  served-late always beats a slip (coverage tier).
- Plumbing: RouteJob carries window_open/deadline; missing-window delivery -> empty.
- Reporting: minutes_late aggregates; service_summary lateness section renders.
- Ablation: `--hard-time-windows` restores hard `TIME_WINDOW` on the deadline.

## Out of scope

- Pickup/collection soft windows.
- Softening the non-anticipation or freight-availability hard floors.
- A distinct never-served-vs-slipped gradation (both are coverage misses; refine
  later if needed).
