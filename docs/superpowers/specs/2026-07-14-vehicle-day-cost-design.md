# Vehicle-Day Activation Cost — Design Spec

**Date:** 2026-07-14
**Status:** Approved (pending spec review)
**Author:** dispatch optimizer work stream

---

## 1. Problem

During intraday micro-insertion, the dynamic dispatcher opens a **fresh vehicle for
a small newly-revealed job even when a previously-used vehicle has already returned
to depot with duty budget to spare**. Operationally this is wasteful: the returning
vehicle's driver is already on a paid shift, whereas the fresh vehicle commits a whole
additional driver-day.

### Root cause (traced in code)

The optimizer's objective is **fuel cost per km only**. There is no cost for putting a
vehicle on the road for a day. Every insertion is ranked purely on marginal generalized
cost:

- `route_cost` = `fuel_cost_per_km(type) × (road_km + out_of_area_phantom_km)`
  ([alns.py:701](../../../freight_planner/alns.py)).
- `solution_cost` = Σ `route_cost` over occupied `(vehicle, day)` keys
  ([alns.py:711](../../../freight_planner/alns.py)).
- Insertion ranking = `fuel_rate × (Δkm + phantom)` ([alns.py:809](../../../freight_planner/alns.py)).

A fresh vehicle's first trip and a returning vehicle's second trip both cost the same
`depot→job→depot` round-trip km when they share a depot, so the objective is *indifferent*
between them — and a fresh vehicle homed even slightly closer wins outright. Driver and
standing costs live only in the downstream `profitability_report`, invisible to the search
(confirmed by the `vehicle_cost.py` docstring: *"Driver-hour and standing-day costs are
later layers"*).

## 2. Why adding a vehicle cost is safe now (historical failure reconciled)

An earlier version of this system was *not* given a vehicle cost because it caused the
solver to stop opening vehicles and leave orders unassigned (high unassignment rate). That
failure was real **for a scalar-penalty objective**, where an unassigned order carried a
soft penalty `P` folded into one cost number: if `vehicle_cost > P`, dropping an order was
"cheaper" than serving it.

That architecture is gone. The current ALNS objective is **lexicographic**:

- acceptance score is the tuple `(served_after, -candidate_total)`
  ([alns.py:1231](../../../freight_planner/alns.py), [alns.py:1295](../../../freight_planner/alns.py));
- `improving = served_gain > 0 or delta < -_EPS` ([alns.py:1304](../../../freight_planner/alns.py)) —
  a move that serves one more order is accepted regardless of cost.

**Served-count is the primary key; cost is only the tie-break.** Therefore any cost term
added to the secondary key can only re-rank solutions that serve the *same* number of
orders. It can never make the solver drop a job to save money. The seed is coverage-safe
too: it rejects jobs only on infeasibility / no-eligible-vehicle
([route_seed.py:387-416](../../../freight_planner/route_seed.py)), never because an insertion
is "too expensive."

**Consequence:** the vehicle cost is coverage-safe at *any* magnitude. We choose the value
for realism, not for safety.

## 3. The cost model: guaranteed shift = floor + overtime

Drivers are paid a **guaranteed daily shift** (a paid minimum day regardless of load).
Depreciation (the £70/day standing cost in `vehicle_cost_rates.json`) is **excluded** — it
is sunk: incurred whether the vehicle is driven or parked, so it must not penalize *use* of
an owned vehicle. The cost that actually changes when a fresh vehicle is opened instead of
reusing a returning one is the **driver**.

The driver-day cost is a floor-plus-overtime hockey stick:

```
driver_day_cost(type, duty_hours) =
    0.0                                    if the vehicle-day has no trips
    hourly[type] × max(GUARANTEED_HOURS, duty_hours)   if occupied
```

where `duty_hours = day_end − day_start` from `DayEvaluation`
([routing_adapter.py:162-171](../../../freight_planner/routing_adapter.py)), and the 13h duty
ceiling is already enforced as a hard feasibility constraint (`SHIFT`), so `duty_hours ≤ 13`
for any feasible day — the ceiling is a wall, not a price.

| Duty worked | Driver cost (rigid @ £40.97/h) | Marginal cost of +1h |
|---|---|---|
| 0–8h | £328 (flat floor) | £0 — free within the guarantee |
| 8–13h | hourly × actual (10h → £410) | £41/h — real overtime |
| >13h | infeasible (`SHIFT` cap) | — |

### Rates (from `profitability_report/vehicle_cost_rates.json`, `driving_hourly_gbp`)

| type | hourly | 8h floor |
|---|---|---|
| tractor | £47.59 | £381 |
| rigid | £40.97 | £328 |
| van | £40.97 | £328 |

Overtime (8→13h) is charged at the plain hourly rate — the declared rates carry no overtime
premium. `GUARANTEED_HOURS` defaults to **8.0**.

### Why floor + overtime (not a flat per-day cost)

A flat per-day cost would make hours 8→13 free, so the solver would cram a vehicle to the
13h duty wall at zero labour cost (the opposite over-consolidation error). The overtime slope
prices those hours honestly, so the solver balances load across already-active vehicles
instead of stuffing one to the ceiling. The floor term *is* the activation cost, so this model
subsumes the flat idea.

### Incentives this produces at an insertion

- **Fresh vehicle for a small job** → charged the full 8h floor (~£328) even for a 2h duty.
  Strong deterrent.
- **Reuse, staying ≤ 8h duty** → driver cost unchanged (extra work inside the already-paid
  guarantee). Free — reuse wins.
- **Reuse into overtime** (e.g. 7h→10h) → pays only the +2h over the floor (~£82), still far
  below +£328 for a new shift, but honestly priced.
- **Reuse infeasible** (returning vehicle already near 13h, or duty/window infeasible) → a
  fresh vehicle opens, and coverage is guaranteed by the lexicographic serve-first rule.

## 4. Scope decisions

- **In scope:** add the driver-day cost to the ALNS objective *and* the micro-insertion
  ranking (see §5 — five sites). One shared helper is the single source of truth.
- **Excluded:** depreciation / standing cost (sunk). Overtime premium (no data). Fuel stays
  the existing per-km term.
- **Seed unchanged** (see §7).
- **Default OFF, byte-identical** when disabled (hard gate).

## 5. Implementation surface — five sites, one helper

The micro-insertion path does **not** flow through `route_cost`. Traced:
`run_rolling` micro-pass ([run_rolling.py:1102](../../../freight_planner/run_rolling.py)) →
`insertion_pass` ([alns.py:1629](../../../freight_planner/alns.py)) →
`_ranked_inserts_for_job` / `_best_insert_for_job`, which compute their **own km-based delta**
independent of `route_cost` / `changed_costs`. Adding the cost only to `route_cost` would miss
the exact path the complaint originates from. All five sites must incorporate it:

| # | Site | Form | Used by |
|---|---|---|---|
| 1 | `route_cost` / `solution_cost` ([alns.py:701](../../../freight_planner/alns.py)) | absolute | reporting + checks |
| 2 | `changed_costs` inner fn ([alns.py:989](../../../freight_planner/alns.py)) | absolute per changed key | ALNS improve deltas |
| 3 | `route_cost_by_key` init ([alns.py:1012](../../../freight_planner/alns.py)) | absolute per key | ALNS improve baseline |
| 4 | `_ranked_inserts_for_job` ([alns.py:809](../../../freight_planner/alns.py)) | delta | micro-pass (regret) + repair |
| 5 | `_best_insert_for_job` ([alns.py:876](../../../freight_planner/alns.py), [884](../../../freight_planner/alns.py), + eject-fallback branch below 890) | delta | micro-pass (single) + repair |

### Shared helper (in `vehicle_cost.py`, mirroring `fuel_cost_per_km`)

```python
DRIVER_GBP_PER_HOUR: dict[str, float] = {"tractor": 47.59, "rigid": 40.97, "van": 40.97}
DEFAULT_DRIVER_GBP_PER_HOUR = DRIVER_GBP_PER_HOUR["rigid"]

def driver_hourly_gbp(vehicle_type: str) -> float: ...        # case-insensitive, rigid fallback

def driver_day_cost(vehicle_type: str, duty_hours: float) -> float:
    """0.0 when disabled (byte-identical) or duty_hours <= 0 (empty day);
    else driver_hourly_gbp(type) * max(GUARANTEED_HOURS, duty_hours)."""
```

- **Absolute sites (1–3):** add `driver_day_cost(type, duty_hours(day_ev))` to each occupied
  vehicle-day's cost. Sites 2–3 already evaluate the day; site 1 (`route_cost`) currently uses
  `route_km` only and must obtain the day evaluation (or duty) to add the term.
- **Delta sites (4–5):** add `driver_day_cost(type, after_duty) − driver_day_cost(type,
  before_duty)` to the ranked delta, where `before_duty` is the current trips' duty (0 for an
  empty vehicle-day → driver cost 0) and `after_duty` is the candidate's duty. This is what makes
  "empty→occupied pays the floor, occupied-within-guarantee pays 0, into-overtime pays overtime"
  fall out automatically. Both the new-trip branch and the insert-into-existing-trip branch
  must apply it.

`duty_hours` is derived from `DayEvaluation.day_start`/`day_end` (ISO strings) via a small parse
helper. An infeasible day is rejected before costing, so its degenerate `day_end == day_start`
never reaches the term.

**Per-key semantics (rolling carry-in):** the driver-day cost is charged **once per occupied
`(vehicle, day)` key**, computed on that key's full-day duty span. A vehicle that returns and is
re-dispatched later the same day accumulates its trips under the *same* `(vehicle, day)` key, so
the 8h floor is charged once and `duty_hours` reflects the whole span — no double-count. The
implementer must confirm `day_start` semantics under a `DutyOverride` (E6 rolling duty carried in
from earlier frozen trips) so the paid span is measured from the nominal shift start, not the
override start.

## 6. Configuration & flag-gating

Mirror the existing `FREIGHT_FUEL_UNIFORM` env-ablation convention.

- `config.py`: `VEHICLE_DAY_COST_ENABLED: bool = False` (default OFF) and
  `GUARANTEED_SHIFT_HOURS: float = 8.0`, each overridable by env
  (`FREIGHT_VEHICLE_DAY_COST`, `FREIGHT_GUARANTEED_SHIFT_HOURS`) following the module's existing
  knob pattern.
- When `VEHICLE_DAY_COST_ENABLED` is false, `driver_day_cost` returns `0.0`, so every site's
  arithmetic is unchanged → **byte-identical** output. This is the regression gate.
- CLI wiring in `run_rolling` (and `run_alns`) exposes the flag so runs can opt in without
  editing config or env.

## 7. The seed is intentionally NOT changed

`route_seed.py:369` adds a `+10000` penalty to opening a **second separate loop** on an
already-used vehicle (`score = delta + (10000.0 if old_trips else 0.0)`), which makes the greedy
seed *spread* first-trips across idle vehicles rather than stack loops. This is a
**coverage-safety heuristic**, not an efficiency choice: greedy construction is myopic (no
lookahead, no global un-place/retry), so stacking early loops onto one vehicle can exhaust its
13h duty and later strand a job that *only* that vehicle is eligible for → an unserved order the
seed cannot recover from. Spreading preserves per-vehicle "resource headroom" for
eligibility-constrained jobs. (Note the penalty applies only to a new second *loop*; chaining a
job into an *existing* trip, [route_seed.py:339-354](../../../freight_planner/route_seed.py), is
unpenalized.)

The driver-day cost therefore belongs in the **ALNS/micro path, not the seed**:

- **Seed** = coverage-safe construction (myopic, can't recover) → keep spreading.
- **ALNS** = global search with the lexicographic serve-first safety net → *re-consolidates* the
  seed's over-spread onto fewer vehicle-days once the cost is present, filling each activated
  vehicle toward its duty limit before opening the next, and opening a fresh one the moment reuse
  becomes infeasible.

Changing the seed would reintroduce greedy myopia and risk coverage. The ALNS is the correct and
sufficient place, and it is exactly where the intraday micro-insertion complaint lives.

**Follow-up (out of scope here):** the ALNS is iteration-bounded, so if it does not fully
consolidate the seed's spread within budget, revisit the seed's `+10000` heuristic — but only
*after* measuring, and only if the safe fix proves insufficient.

## 8. Coverage-safety argument (invariant)

For any two candidate solutions A and B where A serves strictly more orders than B, A wins
regardless of cost, because the score compares `served_after` first. Adding a non-negative cost
term to `candidate_total` changes only the second tuple element, which is consulted only when
`served_after` ties. Therefore introducing `driver_day_cost` **cannot reduce served-order count**
in the ALNS. In the micro-pass, `insertion_pass` inserts the cheapest *feasible* candidate and
only fails a job when no feasible vehicle exists at all; the cost term re-ranks feasible
candidates but never removes feasibility, so it cannot turn an insertable job into a failed one.
This invariant is asserted by tests (§9).

## 9. Testing strategy (TDD, no git commits)

Write failing tests first. Group:

**A. Flag OFF = byte-identical (regression gate)**
1. `driver_day_cost` returns `0.0` when disabled, for occupied and empty days.
2. `route_cost` / `solution_cost` numerically identical to pre-change for a fixed solution.
3. `_ranked_inserts_for_job` / `_best_insert_for_job` return identical rankings with the flag off.

**B. Flag ON = correct cost shape**
4. Occupied vehicle-day charges exactly one floor: `rate × max(8, duty)` — a 2h-duty day costs
   the 8h floor; a 10h-duty day costs `10 × rate`.
5. A second trip added to a vehicle-day still under 8h duty adds **zero** driver cost (delta = 0).
6. A second trip pushing duty 7h→10h adds exactly `rate × (10 − max(8,7))` = `2 × rate`.
7. An empty vehicle-day costs `0.0` (floor applies only when occupied).
8. Per-type rates resolve correctly (tractor £47.59, rigid/van £40.97, unknown → rigid fallback).

**C. End-to-end behavioral (the actual fix)**
9. A toy universe with a returning vehicle idle at depot and a fresh vehicle at the same depot,
   plus one small late job: with the flag ON, the micro-pass inserts onto the **returning**
   vehicle (reuse), not the fresh one; with the flag OFF, current behavior is preserved.
10. **Coverage invariant:** in a scenario where the only feasible placement is a fresh vehicle,
    the flag ON still opens it and serves the job (served-count unchanged vs flag OFF).

**D. Full suite** — run the existing pytest suite to confirm no regressions with the flag at its
default (OFF).

## 10. Validation plan (after implementation)

Run a real window with the flag ON vs OFF and compare:
- vehicle-days used (expect a drop — the intended effect);
- unassignment rate (expect **no rise** — guaranteed by §8, but verify empirically);
- total km and total generalized cost (expect fuller/longer days per vehicle, fewer vehicles;
  km may rise slightly as detours replace fresh activations — the intended trade).

Only after this validation is the flag considered for default-ON.

## 11. Open parameter

- `GUARANTEED_SHIFT_HOURS` default = **8.0**. Adjust if the operation guarantees a different
  minimum (e.g. 9h). It is a config knob regardless.

## 12. Standing constraints honored

- **No git commits** (`e:\BEAT` is not a git repo).
- **TDD** for all Python changes.
- **Flag-off / disabled path byte-identical** (hard gate, tested in §9A).
- Seed left untouched (§7).
