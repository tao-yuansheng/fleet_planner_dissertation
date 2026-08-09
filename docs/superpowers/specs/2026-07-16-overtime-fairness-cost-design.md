# Overtime + fairness cost — design spec (2026-07-16)

**Approved by user 2026-07-16** (conversation: item 4 of the telematics de-wire directive,
refined through the "2h-on-one vs 1h-each" probe).

## Problem

Since the telematics de-wire (2026-07-16), the fleet works one operating day: available
06:00, **no shift-end wall**, bounded only by the 10h driving / 13h duty caps. 19:00 is a
soft target — service coverage comes first. But the objective's driver cost is linear
straight-time (`hourly × max(9h, duty)`), so the solver:

1. has no reason to end days near 19:00 (a 22:00 hour prices like a 10:00 hour);
2. has no pressure to SPREAD late work — piling 4 late hours on one vehicle prices the
   same as 2h+2h on two (driver burnout);
3. measures duty as first-departure→last-return INCLUDING depot idle, so a 06:00 morning
   vehicle held (depart_floor) for an evening trip busts a 13h "duty" it never worked —
   late work would keep defaulting to fresh vehicles despite the depot-hold fix.

## Decisions (user)

- **OT trigger: BOTH stacked** — payroll overtime beyond the 9h paid floor AND an
  unsocial-hours premium past 19:00.
- **Split-shift rule** — a depot gap ≥ 3h between trips does not count toward the 13h
  duty cap (driver rests or swaps); the premium still prices the late clock hours.
- **Ramp, not bands** — the late premium rises linearly (+0.25×hourly per hour past
  19:00) so the late cost is QUADRATIC in per-vehicle late hours: one driver's second
  late hour always costs more than another active driver's first. This is the fairness
  mechanism; no bookkeeping.
- **Serve-first invariant** — lexicographic (served, −cost) acceptance unchanged: the
  premium shapes WHERE late work lands, never WHETHER an order is served.
- **Fairness is a preference, not a rule** — ~£4/h edge at default slope; a genuine
  30 km saving still consolidates. `LATE_RAMP_PER_HOUR` is the exchange-rate knob.
- **Phase 2 (deferred)**: cross-day evenness via a late-hours ledger in handover.json
  with escalating rates. Build only if week runs show one vehicle hogging evenings.

## Cost function

For one vehicle-day evaluation `day_ev` (all trips), with `hourly = hourly rate[type]`:

```
chains        = trips grouped so a depot gap ≥ SPLIT_SHIFT_GAP_H starts a new chain
working_h     = Σ chain spans (route_start→route_end per chain)   # depot idle unpaid
paid_base     = hourly × max(GUARANTEED_SHIFT_HOURS, working_h)   # 9h floor, unchanged
duty_ot       = hourly × (OT_DUTY_MULTIPLIER − 1) × max(0, working_h − 9)
late_cost     = hourly × Σ_trips ∫ (LATE_PREMIUM_BASE + LATE_RAMP_PER_HOUR·t) dt
                over t = hours past 19:00 within each trip's [route_start, route_end]

driver_day_cost = paid_base + duty_ot + late_cost      (when OVERTIME_COST_ENABLED)
```

- The integral over absolute clock-t (t = 0 at 19:00) makes late cost quadratic in the
  day's TOTAL late span even across multiple trips: closed form per trip
  `hourly × (0.5·(t1−t0) + 0.125·(t1²−t0²))` with `t0 = max(0, start−19:00)`,
  `t1 = max(0, end−19:00)` in hours. Multipliers at defaults: ×1.5 at 19:00 → ×2.0 at
  21:00 → ×2.5 at 23:00 (continuous).
- Premium attaches to WORKING time only (trip spans: drive + service + curb wait) —
  never to depot idle.
- Stacking example: a 20:00 hour on an 11h-working day costs 1.0 (straight, beyond
  floor) + 0.5 (duty OT) + ~0.75 (ramp at t=1..2) ≈ ×2.25.
- `OVERTIME_COST_ENABLED = False` (or `--no-overtime-cost` / env) reproduces today's
  cost exactly (ablation baseline).

## Feasibility change (split-shift duty)

`evaluate_day` gains chain detection: consecutive non-empty trips belong to one chain
while the gap between the previous trip's `route_end` and the next trip's `route_start`
is `< SPLIT_SHIFT_GAP_H` (the 30-min reload never splits). **The 13h duty cap applies
per chain**; the 10h DRIVING cap stays whole-day (EU daily driving does not reset on a
short rest). This is what legally lets a 06:00 morning vehicle take a floored evening
trip (depart_floor holds it at the depot ≥3h → new chain).

Verification note: confirm where the 13h duty currently binds in the daily evaluator —
if the old bound was only the (now-removed) shift wall, the per-chain 13h check is a NEW
hard constraint this spec introduces (C5 requires it).

## Config (freight_planner/config.py)

| knob | default | meaning |
|---|---|---|
| `OVERTIME_COST_ENABLED` | `True` | master flag; False = today's cost, byte-identical |
| `OT_DUTY_MULTIPLIER` | `1.5` | pay rate for working hours beyond the 9h floor |
| `LATE_PREMIUM_START_HOUR` | `19.0` | clock hour the unsocial ramp starts |
| `LATE_PREMIUM_BASE` | `0.5` | premium at ramp start (+50% → ×1.5) |
| `LATE_RAMP_PER_HOUR` | `0.25` | multiplier slope per hour past start |
| `SPLIT_SHIFT_GAP_H` | `3.0` | depot gap that ends a duty chain |

Defaults are UK-haulage convention (×1.5 OT, ×2.0 by 21:00), each ablatable; replace
with payroll figures when available. Rates come from the existing
`vehicle_cost_rates.json` hourly table.

## Where it binds (call sites)

`vehicle_cost.driver_day_cost_ev(vtype, day_ev)` (new, chain-aware) replaces the
`driver_day_cost(vt, _duty_hours(day_ev))` pattern at every objective site: `route_cost`
/ `solution_cost`, both ALNS insertion delta enumerators, and the remaining
vehicle-day-cost delta sites (5 sites, 1 helper — the 2026-07-15 activation-cost set).
The old `driver_day_cost(vtype, duty_h)` stays as the base-formula helper. Seed +10000
fresh-vehicle spread untouched. Tours are out of scope (tour days already carry their
own caps; tour cost model unchanged).

## Honesty notes

- We plan VEHICLES; trunk tractors demonstrably swap drivers (1.41 drivers/veh-day). A
  second chain's "overtime" may really be a second driver's straight time — cost-close,
  refine when driver-level data exists.
- With the split-shift rule, one vehicle-day can mean two paid drivers; the 9h floor is
  charged once per vehicle-day (not per chain) — conservative toward reuse, revisit in
  phase 2 if it distorts.

## Acceptance tests (behavioral)

1. **User's probe**: two late orders, two vehicles both active past the 9h floor, equal
   km → the 1h+1h split beats 2h-on-one (ramp convexity).
2. **Reuse still wins**: floored evening job, one returned vehicle vs one idle vehicle →
   returned vehicle preferred (premium equal, fresh pays the floor).
3. **Km still trumps small fairness**: consolidation saving ≫ ramp edge → consolidates.
4. **Split-shift legality**: 06:00 morning chain + ≥3h gap + evening chain ending 21:00
   is feasible; a single 14h chain is not.
5. **Ablation**: flag off → cost identical to pre-change at full precision.
6. **Week run**: service outcomes (ON_TIME/SLIPPED/UNSERVED) unchanged or better;
   past-21:00 working minutes lower/spread vs the no-overtime baseline.
