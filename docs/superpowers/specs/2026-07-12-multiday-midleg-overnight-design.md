# Multi-day Mid-leg Overnight (Approach A) — Design

**Date:** 2026-07-12
**Status:** approved (conversation), ready for plan
**Owner:** freight_planner / tours

## Goal

Make a multi-day tour end each day *where the driving cap actually runs out* — a
point part-way along the current leg — instead of parking back at the last
customer stop. Carry that overnight location, the freight aboard, and a fresh
daily duty budget into the next day, so each tour-day becomes a bona-fide
single-day route a solver can reorder or insert into. This is the physics fix
that also lays the foundation for the later multi-day-insertion lever.

## Background — what happens today

`evaluate_tour` ([freight_planner/tours.py](../../../freight_planner/tours.py))
walks `depot → stop → … → depot`, splitting into days by the 10 h driving cap
(`MAX_DRIVING_H_PER_DAY`) and the 13 h duty cap (`TOUR_DAY_ELAPSED_CAP_MIN`),
with a 4-day hard ceiling (`MAX_TOUR_DAYS_HARD`). The split is **at a stop
boundary**: when the *next* leg won't fit the remaining day, the whole leg is
deferred and `prev_lat/prev_lon` carries over unchanged — i.e. the vehicle
"sleeps" at the last stop it completed.

Two consequences:

1. **Residual daily hours are wasted.** Finish a stop at 15:00 with 3 h of drive
   cap left → those 3 h evaporate; the next leg starts fresh tomorrow.
2. **The return leg can burn a whole extra vehicle-day**
   ([tours.py:269-275](../../../freight_planner/tours.py)): a 5 h drive home
   when only 3 h of cap remain costs a full extra day, tying the vehicle up.

Real drivers (sleeper-cab tractors) push toward the next destination and take
the statutory 11 h daily rest at a services / lay-by en route. GB/EU hours do
not require resting at a customer; "rest only at a stop" is a modelling
artifact, not a constraint. So the honest model is *end the day part-way along
the leg* — no truck-stop dataset needed, because for a km/duty/day model the
overnight **position** is what matters, not the amenities.

## The change

In `evaluate_tour`, when a leg's driving would overflow the day (the *same*
`day_elapsed > 0` fit test the stop-boundary split already uses), the vehicle
banks the drive-cap residual, takes **one** overnight rest part-way along the
leg, and rides the remainder on the next day. The **return-home leg** gets the
same treatment.

Two deliberate bounds keep the change monotone (verified below):

- **At most one overnight per leg** — exactly the +1 day the stop-boundary split
  would add. The remainder rides the new day whole (as OFF already does), so a
  single over-cap leg — e.g. a ~685 min depot→far-NW-Scotland hop — never gets
  split into two nights ON where OFF counted one. Banking the residual still
  frees room on the next day, which can let a *later* stop fit a day earlier — so
  days come out **≤ OFF**, and often strictly less, without ever exceeding OFF.
- **Gated on `day_elapsed > 0`** — the first leg out of the depot is left whole,
  identical to OFF (whose split guard is also `day_elapsed > 0`). Every leg after
  the first stop, and the return, run through the mid-leg path.

At each overnight boundary we record a `DayStart`: the interpolated overnight
coordinate + the freight aboard at that moment. Day *k*'s `DayStart` is exactly
what a solver needs to treat that day as a single-day route: start location,
carried load, and a full fresh daily budget (the 11 h rest resets the 10 h drive
/ 13 h duty caps and the break accumulator).

### Invariants (these define "correct")

- **Total km identical** to today for the *unmodified* tour. Each leg's original
  `road_km` is summed once; the overnight coordinate is metadata, never a
  re-`road_km`'d half-leg (interpolating a point off the OSRM network and
  re-routing through it would perturb distance). Only an actual solver
  modification (later: insertion) changes km — by design.
- **Total drive minutes identical.**
- **`days` monotone: same or fewer, never more, and no coverage loss.**
  Empirically checked on 6,000 random far tours (1–4 stops, 50–58.5°N): km
  byte-identical every time, `days_ON ≤ days_OFF` every time, **zero** tours
  feasible under OFF but not ON, and ~20% (1,096) finished a day earlier
  (typically 4→3). The residual-banking win is real; the downside is provably nil.
- **Every hard constraint still enforced, per day:** capacity peak, 10 h drive
  cap, 13 h duty (drive + service + breaks), due-date deadline offsets,
  readiness floor offsets, 4-day ceiling. Mid-leg splitting only relocates the
  boundary; it never relaxes a limit. A tour that was capacity-infeasible stays
  infeasible; a stop past its due day stays LATE.
- The only *intended* downstream change (flag ON): a tour previously rejected
  `TOUR_TOO_LONG` / `LATE` purely because of wasted residual can become feasible
  — serving far work the model currently drops. This is the coverage benefit.

### Flag & regression gate

New config knob **`MULTIDAY_MIDLEG_OVERNIGHT: bool = False`** in
`freight_planner/config.py`. Default **OFF** ⇒ `evaluate_tour` takes the exact
current code path and is byte-identical, so the β=0 / static bit-identity
regression gate passes by construction. ON ⇒ the new physics. We validate the
deltas (km identical, days ≤, coverage ≥) on a corpus before proposing to flip
the default — never flipped silently. Matches the OSRM-durations / day-flex
precedent.

## Data model

```python
@dataclass(frozen=True)
class DayStart:
    day_index: int          # 0 = depot departure; k>0 = overnight into day k
    start_lat: float
    start_lon: float
    start_node: str         # "DEPOT" for day 0, else "OVERNIGHT:{tour}:{k}"
    carried_pallets: float  # freight aboard at wake-up
    carried_kg: float
```

`TourEvaluation` gains `day_starts: tuple[DayStart, ...] = ()`. Empty on the
flag-OFF path (preserves identity for every existing consumer, which read only
`feasible/total_km/days/stops/peak_*`). Populated flag-ON: day 0 = depot, each
later day = the overnight boundary.

## v1 scope boundaries

- **Split single-point legs and the return leg only.** Two-point legs
  (`DIRECT_CUSTOMER_MOVE`, `HUB_DROP`) keep the current whole-leg stop-boundary
  deferral. Rationale: freight state changes only *at* a stop, so a single-point
  overnight has an unambiguous carried load (running load before the stop),
  whereas splitting a `prev → origin → dest` leg would need to know which
  sub-segment the night fell on. Two-point mid-leg is a clean follow-up. This
  fallback is strictly the current (safe, conservative) behaviour, so it
  violates nothing.
- **Great-circle / linear lat-lon interpolation** for the overnight point.
  OSRM-polyline interpolation is a later refinement; the point is metadata a
  solver re-routes from, so small positional error is immaterial.
- **No emission** of the overnight point to runsheets/viz yet. `day_starts` is
  produced for the insertion layer to consume; wiring it into outputs is
  separate.

## Algorithm sketch

Two helpers, both TDD'd:

- `_drive_fits(drive_since_break, drive_room, duty_room) -> float` — max
  drive-minutes that fit today under **both** the drive-cap room and the
  duty-cap room (duty must absorb the statutory break owed while driving that
  much). Van (no breaks) ⇒ `min(drive_room, duty_room)`. HGV ⇒ bisection on the
  monotone `x + breaks(since, x)`.
- `_interp_latlon(a_lat, a_lon, b_lat, b_lon, f) -> (lat, lon)` — linear
  interpolation at fraction `f` (drive-time fraction = km fraction, since
  `longhaul_drive_minutes` is linear in km).

Main loop (flag ON, `day_elapsed > 0`, single-point leg): `_advance_single_point`
tests whether the whole leg + service fits today; if so it returns unchanged
(no overnight). If not, it banks `_drive_fits(...)` today, records one overnight
`DayStart` at the banked fraction along prev→dst, resets the daily accumulators
(the rest clears `drive_since_break`), and returns the remainder as `arr_dm`.
`evaluate_tour` then runs the existing arrive/depart/load/capacity/offset logic
with `arr_dm` (the final-day drive) while `total_drive` still books the whole
leg. The return leg runs the same helper before adding `back_km`.

`arr_dm` distinguishes the drive that lands the arrival (residual, or the whole
leg when nothing splits) from the full leg booked into `total_drive` — this is
what keeps km/drive totals identical while the day attribution shifts.

Flag OFF (or the first depot leg, or a two-point leg): the existing
stop-boundary block runs unchanged.

## Testing strategy (TDD, red first)

1. `_drive_fits`: van = `min`; HGV drive-bound; HGV duty-bound (service earlier
   in day); exact break-boundary (landing on 270 min owes the break before the
   next drive).
2. `_interp_latlon`: f=0→a, f=1→b, f=0.5→midpoint.
3. `DayStart` / `day_starts`: flag-OFF ⇒ `day_starts == ()`.
4. **Flag-OFF byte-identity** on a crafted 3-day tour: `days`, per-stop
   `day_index`, `total_km`, `total_drive_minutes`, `stops` all equal current
   output. (The regression gate, at unit granularity.)
5. Flag-ON return-leg overflow: old = +1 day, new = same day-count; `total_km`
   identical; `day_starts` has the overnight point with correct carried load.
6. Flag-ON leg longer than a full day: splits across two nights (loop);
   `total_km` identical; `days` correct.
7. Flag-ON duty-bound mid-leg: `drive_today` bound by duty, not drive cap.
8. Hard constraints unchanged: capacity-infeasible stays infeasible; a stop past
   its due day stays LATE; `MAX_TOUR_DAYS_HARD` still trips.
9. Invariant on a small corpus: flag-ON `total_km` == flag-OFF `total_km` and
   flag-ON `days` ≤ flag-OFF `days`, tour by tour.
10. Full suite green (regression: prior 656 + new).

## Out of scope / follow-ups

- **Multi-day insertion** (the next lever): lift `DayStart[k]` into a
  `RouteVehicle` and let the solver insert intraday orders into that day's route,
  under the "no new day, respect the original schedule" guard. This spec exists
  to make that clean.
- OSRM-polyline overnight interpolation.
- Two-point-leg mid-leg splitting.
- Emitting the overnight waypoint to runsheets / the timeline app.
