# Trunk Deck Size + Day Availability — Design

**Date:** 2026-07-12
**Status:** approved (implement both, no A/B per stakeholder)

## Motivation

Two trunk-model assumptions are contradicted by the January telematics, and both
push planned work *below* what the fleet actually does.

**Broken assumption A — trunks are night-only, double-deck (52-pallet).** In
reality the hub trunk is a **continuous day+night flow** (near-B37 pings for the 9
Bedford artics cluster at 20:00–05:00 *and* 12:00–15:00). We cannot model the
daytime arrivals (no hub-arrival-time data), so we keep the simplification that
trunks run at night — but that means the *night* trunk must carry the whole day's
freight, which single-deck 26-pallet trailers size correctly. The 52-pallet
double-deck was chosen only to reconcile "~7 tractors observed at B37/night" with
demand *under the night-only assumption*; that assumption is now overturned, and
52-pallet **under-deploys** night trunks (Bedford: 3–4 trips/night modelled vs
"8/9 artics trunk nightly" observed).

**Broken assumption B — a trunk tractor rests until 10:00 the next day.** The
telematics says otherwise (3 heaviest Bedford trunk artics, whole January):

| Evidence | Finding |
|---|---|
| Busiest hour of day | **08:00** (their peak) |
| Morning (06–11) movement | **78%** out on customer routes (avg 32 km from depot, 105 from hub); ~0.1% at the hub |
| Active clock-hours/day | **14.6 of 24** (weekday mean) |
| Drivers per vehicle-day | mean **1.41**; **54%** of days ≥2 drivers |
| Daytime (10–17) hub pings | ~**1%** |

The vehicle ≠ the driver: a fresh day driver runs the tractor from ~06:00 while the
night-trunk driver rests. So the tractor genuinely does a **full customer day *and*
a night trunk** (~14.6 active h). The 10:00 hold models it at ~11 h — it *under*-utilizes
these vehicles by ~4 h/day, suppressing real km. Removing the hold cannot
over-utilize them, because `[full day] + [night trunk]` is exactly what they do; the
daytime hub flow that could warrant a debit is ~1% for the heavy trunkers.

Both changes move modelled trunk-artic utilization/km toward the observed pattern
(more night trips + full days), narrowing the planned-vs-actual gap.

## Change 1 — `TRUNK_DECK_PALLETS` 52 → 26

`freight_planner/config.py`: `TRUNK_DECK_PALLETS: float = 26.0` (single-deck
standard trailer). Rewrite the justifying comment: the 52-pallet double-deck
reconciliation assumed night-only trunks; with day+night reality folded into a
night-only model, 26-pallet is correct and the resulting extra night tractors
stand in for the un-modelled daytime trunks (keeps total trunk deployment/km
realistic — matches "8/9 Bedford artics trunk nightly").

Effect: `_trips = ceil(max(import, export) / 26)` ≈ 2× today's trips → ~2× tractors
drawn per night (Bedford ~6–7 vs 3–4; CB22 ~5–6 B37 + 2–3 LE10 vs 3–4 + 1).

## Change 2 — remove the trunk next-day 10:00 hold

New flag `freight_planner/config.py`: `TRUNK_NEXT_DAY_HOLD: bool = False`.
- **False (new default):** trunk-drawn tractors get **no** next-day availability
  override — they are available for a full normal day (evidence-backed).
- **True:** the legacy 10:00 hold (`TRUNK_NEXT_DAY_START`), preserved for
  reference/reversibility and a possible future partial debit.

Single control point — `freight_planner/trunk.py::draw_tractors`: emit
`plan.avail_overrides[(vid, next_day_iso)] = TRUNK_NEXT_DAY_START` **only when
`TRUNK_NEXT_DAY_HOLD` is True**. Everything downstream keys off
`trunk.avail_overrides`:
- daily seed / ALNS start-time via `combined_avail_overrides` (run_alns.py) and
  the seed's `apply_avail_override`;
- the repair "busy" set `daily_busy` (tour_plan.py:478, `set(trunk.avail_overrides)`).

So gating the emission empties both consumers → tractors free for full days, no
other file changes needed. The `plan.draws` trip/rotation bookkeeping and shortfall
logging are unchanged (trip counting and one-trip-per-tractor-night still hold).

## Non-goals

- Modelling the daytime hub trunk explicitly (no hub-arrival data — folded into
  the night trunk via Change 1).
- A partial `DutyOverride` debit (available early but N fewer hours) — reserved if a
  future daytime-hub model needs it; evidence says ~0 for the heavy trunkers now.
- An A/B run (skipped per stakeholder — the direction is evidence-established).

## Impact / risks

These intentionally change outputs (NOT byte-identical): more night trunk trips,
more trunk km, and trunk tractors now available for daytime routes. Experiment
baselines that pinned 52-pallet / the 10:00 hold shift. Expected direction:
higher trunk-artic utilization and trunk km, closer to the odometer. Daily
coverage should not fall (freeing trunk tractors *adds* morning capacity); a
larger night draw may raise `trunk: SHORTFALL` logs where a depot pool can't cover
~2× trips — those still count km (freight moves in reality), as today.

## Tests (TDD)

1. `draw_tractors`, `TRUNK_NEXT_DAY_HOLD=False` (default): `plan.avail_overrides`
   is empty; `plan.draws` and `total_trips` unchanged.
2. `draw_tractors`, `TRUNK_NEXT_DAY_HOLD=True`: `(vid, next_day)` → "10:00" emitted
   (legacy behaviour preserved).
3. `TRUNK_DECK_PALLETS == 26.0`; `_trips(52)==2`, `_trips(53)==3`, `_trips(26)==1`,
   `_trips(27)==2`.
4. `trunk_schedule`: a (depot, night) with 100 import pallets → `ceil(100/26)=4`
   trips.
5. Update existing `test_trunk.py` expectations from 52- to 26-pallet trip counts;
   full suite green.
