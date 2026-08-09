# Cambridge Dispatcher v1.8 Validation

> **Historical validation snapshot — superseded.** This document records the
> v1.8 model and its then-current `10 + 6 × pallets` service-time assumption.
> The live freight planner now uses fixed dwell per distinct customer visit:
> 15 minutes for vans/rigids and 30 minutes for tractors, independent of pallet
> count. See `freight_planner/PIPELINE.md` and
> `freight_planner/experiments/METHODOLOGY_FORMULAS.md`.

## What changed

| Area | v1.7 | v1.8 |
|---|---|---|
| Solver objective | activation + fuel + UNASSIGNED_PENALTY | **+ LATENESS_PENALTY × minutes past per-stop window** (£1/min default) |
| `DeliveryStop` | order_id, lat, lon, weight, pallets, service_h | **+ `window_end`** (datetime) |
| `ScopedOrder` | order_id, …, delivery_window, collection_window | **+ `time_window_value`** (Qargo string) **+ `requested_start_raw`** (preserves 00:00 for date-only entries) |
| Service time per stop | flat 20 min | **`10 + 6 × pallets` min** (parcel = 10 min, 5-pallet drop = 40 min) |
| On-time metric (planned) | `arrival_iso ≤ delivery_window[1]` — fell into Case-B with 06:00 cutoff for date-only orders | **3-case rule** via shared `cambridge/on_time.py` helper, using raw requested_start: Case A (window value), Case B (timed request), Case C (date-only same-day) |
| On-time metric (actual) | datetime comparison against `destination_end_timestamp_local` (departure) — 4.7% on-time | **3-case rule + correct field**: uses `destination_timestamp_local` (arrival = delivery time) at appropriate granularity per case; filters to delivered statuses |
| Cost reporting | combined "Fuel cost GBP" (mislabel — included £150 activation) | **split**: Planned activation / Planned fuel (vs Jigsaw) / Planned total |
| Jigsaw actual fuel | whole-fleet diesel spend | **filtered to CB22 rigid registrations** |
| Tractor pool | hardcoded 6 vehicles (unverified) | **derived from telematics** (44 fleet tractors → 6 qualify by overnight-at-CB22 ≥ 90% across Jan–Feb) |
| Postcode Jaccard | always 0.000 (placeholder `set()`) | **actual-side stop matcher**: telematics dwell ≥5 min within 100 m → nearest postcode within 500 m → outward district set; depot dwells excluded |

## 5-day validation (Jan 7–11 OSRM)

| Day | Orders | Planned km | Actual km | km Δ | Planned fuel | Jigsaw fuel | fuel Δ | Planned on-time | Actual on-time | Lateness min | Jaccard | Not yet delivered |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Jan 07 | 147/150 | 2,010 | 1,966 | **+2.2%** | £516 | £624 | **+17.3%** | 93% | 98% | 59 | 0.028 | 22 |
| Jan 08 | 169/175 | 3,882 | 2,255 | +72.1% | £987 | £640 | +54.4% | 92% | 99% | 206 | 0.065 | 19 |
| Jan 09 | 141/141 | 2,910 | 2,199 | +32.3% | £748 | £820 | **+8.8%** | 89% | 92% | 155 | 0.056 | 23 |
| Jan 10 | 2/2 | 126 | 119 | +6.1% | £20 | £31 | +36.9% | 100% | 100% | 0 | 0.000 | 0 |
| Jan 11 | 0/0 | 0 | 0 | n/a | £0 | £0 | n/a | n/a | n/a | 0 | n/a | 0 |

**Aggregate medians:** km +6.1% (was +6.1% v1.7), fuel pp delta around +17%, Jaccard 0.056 (was 0.000 v1.7).

## What v1.8 fixes

1. **On-time metric is honest at last.** Both sides use the same 3-case rule. Pp delta is +0pp / +2.9pp / +7.8pp / +0pp / +0pp across the 5 days — all pass. Previously +93pp / +89pp / +95pp / +100pp / +0pp.
2. **Cost is reported apples-to-apples.** "Planned fuel" vs "Jigsaw fuel" — both in £ diesel. Activation overhead and lateness shown as separate lines, not conflated with fuel.
3. **CB22-only fuel actual.** Jigsaw filter drops actual fuel from ~£4,000/day (whole company) to £600–800/day (CB22 rigids only). Now the +17% Jan 7 delta is meaningful, not noise from cross-fleet contamination.
4. **Per-pallet service time.** Parcel drops complete in 10 min, palletised handovers take 40+ min. Solver feasibility now reflects this — Jan 7 lateness rose to 59 min (real signal: solver is honest about which stops it can't fit perfectly), Jan 8 to 206 min (the heavy-load day's tail simply can't fit in the modeled shifts).
5. **Tractor pool from data.** Replaced the hardcoded 6-vehicle set with telematics-derived set. 3 of the original 6 confirmed (AR02DEX, N88GNW, R88GNW), 3 dropped (N8GNW, Y88RNW, S88RNW — borderline 50–89% overnight share), 3 new (AR03DEX, TA70WTL, X88RNW).
6. **Postcode Jaccard non-zero.** Actual-side stop matcher produces 0.028–0.065 per day (was 0.000). Still low — most planned districts don't match real-vehicle dwells — but it's a real measurement now.

## What v1.8 does NOT fix

| Gap | Status |
|---|---|
| Jan 8 heavy-day km blow-up (+72%) | Solver opens 18 vehicles vs actual 11 — `VEHICLE_ACTIVATION_COST = £150` likely under-prices vehicle opening. Calibration is a v1.9 candidate. |
| Postcode Jaccard still low (0.05) | Two reasons: (a) we model direct depot→stop routing, real fleet does hub-and-spoke groupage through Bedford/St Ives; (b) telematics dwells often don't precisely match Qargo destination postcodes (500 m matching radius can be too tight near dense Cambridge centre). Architectural — v2.x territory. |
| `L1 stop-count KS dist = 1.000` every day | Actual-side `stops` count is still a placeholder (0). The Jaccard fix populated districts but not the per-vehicle stop count. Would need to count distinct dwell clusters per vehicle. Small follow-up. |
| Per-order trunk-arrival timing (rolling horizon) | Dropped from v1.8 spec because the Qargo `origin_end_timestamp_local` is a 06:00 placeholder for 93% of PL_IMPORT orders and the order→trunk linkage isn't in the data. Documented as permanent constraint. |
| Driver hours / breaks / refuel | Not modelled. |
| Time-of-day speed variation | Flat 1.24× truck factor on OSRM car free-flow. |

## Recommended v1.9 priorities

1. **Calibrate `VEHICLE_ACTIVATION_COST`** against real driver-day cost (basic shift + insurance + admin = ~£200–400/day in UK HGV ops). Will fix the heavy-day vehicle-count blow-up.
2. **Actual-side stop count from dwell detection** — small addition; closes the L1 stop-count KS metric.
3. **Time-of-day speed factor** — splits the flat 1.24× into morning-rush / off-peak / evening-rush profiles per the telematics signal.

## Operational notes

- Run `python -m investigations.derive_v18_parameters` once per data refresh to refresh `tractors_derived.json`.
- `LATENESS_PENALTY_GBP_PER_MIN = 1.0` is a starting value. Sweep candidates: £0.50, £2, £5. Higher pushes solver harder to fit windows; too high risks dropping orders (mitigated by `UNASSIGNED_PENALTY = £50,000` being much higher than any plausible lateness cost).
- Service-time defaults `SERVICE_BASE_MIN = 10`, `SERVICE_PER_PALLET_MIN = 6` are conservative guesses pending a telematics dwell-time study.

## Code-quality summary

- 184 unit tests pass (was 153 v1.7; +31 across the 9 v1.8 tasks).
- No commits — all changes local per project rule.
- New shared helper `cambridge/on_time.py` encodes the 3-case rule once; planned and actual paths call into it.
- New investigations script `investigations/derive_v18_parameters.py` regenerates the tractor pool JSON.
