# T1 — Nightly B37 Hub Trunk as a Fixed Scheduled Service — Design

**Date:** 2026-07-04
**Status:** stakeholder-approved direction; spec for review
**Problem (QUEST_LOG T1):** the model contains zero trunk km. PL_IMPORT freight
materializes free at the depot; PL_EXPORT hands off free after collection. In
reality ~7 fleet tractors run depot↔B37 round trips every weeknight (~9-11k
km/week in the odometer actuals that the plan never sees), and trunk duty
consumes artic capacity. T1 models the trunk as a FIXED nightly service —
schedule-driven, not optimizer-driven.

## Verified foundations (2026-07-02 sizing + 2026-07-04 depot check)

### Double-deck tractor units — the load-bearing assumption, and why

In-universe hub flow (Jan 05-30 weekdays): **PL_IMPORT 307 pallets/night mean
(max 388); PL_EXPORT 205 (max 280)**. A trunk round trip carries exports up AND
imports down, so required trips/night = ceil(max(import, export) / capacity).

| fleet assumption | required trips/night (mean / peak) | telematics observed |
|---|---|---|
| single-deck artic, 26 pal | 12.3 / 15 | — |
| **double-deck artic, 52 pal** | **6.5 / 8** | **7.0 mean / 11 peak tractors/weeknight at B37** |

Single-deck demands nearly twice the vehicles the telematics shows; double-deck
matches the observation almost exactly. **The real trunk runs double-deck; the
model adopts `TRUNK_DECK_PALLETS = 52.0` as a verified operational fact, not a
tuning knob.** (Kg note: a 44t artic's ~28t payload over 52 pallets averages
538 kg/pallet headroom — the fleet's pallet freight averages well under that,
so pallets, not kg, is the binding trunk dimension. The sizing ignores kg.)
Evidence: scratchpad trunk_sizing.py (session 2026-07-02h); B37 stop pings peak
21:00-23:00 and 00:00-03:00; 25 distinct fleet tractor regs rotate; ~zero rigids.

### Which depots run the night trunk (2026-07-04 verification)

Night-window (18:00-06:00) B37 stop pings, January, fleet regs only:
- **BEDFORD: 9 regs, 49 reg-nights** (HX17CUA 19 nights, N888GNW 12, W888RNW 7…)
- **CB22: 12 regs, 44 reg-nights** (X88GNW 9, W88GNW 9, AR03DEX 6…)
- **STOKE: zero.** Stoke's two B37 visitors (BU69XGK, BX67ZFV) appear ONLY
  10:00-17:00 — daytime hub drops inside normal routes. The Stoke fleet is
  parked overnight (21 moving pings 20:00-05:00 in the whole month = noise).

**Therefore: two nightly services — BEDFORD↔B37 and CB22↔B37. No Stoke trunk**
(stakeholder-confirmed; Stoke's hub link stays inside its existing daytime
routes and is NOT part of T1).

## Design

### Sizing (per depot, per night)

For each weeknight N (Mon-Fri departure nights) and depot D ∈ {BEDFORD, CB22}:
- `import_pal(D, N)` = pallets of in-universe PL_IMPORT orders whose delivery
  legs run FROM depot D on day N+1 (they must come down the trunk overnight);
- `export_pal(D, N)` = pallets of in-universe PL_EXPORT (TRUNK-resolved) orders
  collected INTO depot D on day N (they go up that night);
- `trips(D, N) = ceil(max(import_pal, export_pal) / TRUNK_DECK_PALLETS)`.

Imports deliverable on the window's FIRST day rode a pre-window trunk — they are
prestaged (no trip charged), consistent with the existing BEFORE_PLANNING_START
/ PRESTAGED_DELIVERY semantics. The window's LAST collection day still charges
its departure night.

### Vehicle draw (stakeholder: nightly draw from the artic pool)

Each night, `trips(D, N)` tractors are drawn from depot D's tractor pool —
one round trip per tractor-night (observed pattern: 25 regs rotate at ~1
trip/night; round trip Bedford↔B37 ≈ 2.5-3 h). Draw rule: rotation by
least-recently-drawn (stable, deterministic, mimics the observed rotation),
skipping vehicles reserved for multiday tours that night or the following day.

**Day-capacity coupling (the honest cost of trunk duty):** a tractor drawn for
night N gets its **next-day availability delayed to `TRUNK_NEXT_DAY_START`
(default 10:00)** on day N+1 (unit turnaround + driver rest after a ~01:00-03:00
return). Departure-side needs no change: trunks leave ~20:00-22:00, after every
modeled day shift ends (18:00-19:38). If a depot's pool can't cover the trips
(after tour reservations), the shortfall is logged loudly (`trunk: SHORTFALL…`)
and the remaining trips still count in the km line — the freight physically
moves in reality; the model must not silently drop the cost.

### Freight semantics

- **PL_EXPORT cutoff:** already modeled — `_pl_export_window` ends collection
  windows at the trunk-prep deadline (TRUNK_DEPART_HOUR − TRUNK_PREP_MARGIN_H).
  T1 changes nothing here; the spec documents the dependency.
- **PL_IMPORT availability:** trunk arrives back ~03:00-05:00, before the
  06:00 operating-day start, so morning availability is effectively unchanged.
  T1 sets import `freight_ready_time` = trunk arrival for documentation honesty
  but this is a NO-OP constraint today (documented as such; it becomes real if
  operating hours ever widen).

### Reporting (stakeholder: separate fixed-service line)

Trunk km is NOT added to the optimizer's plan km. New KPI block + run-log line:

```
trunk (fixed nightly service, double-deck 52 pal):
  BEDFORD: 28 trips / 5 nights, 7,022 km   CB22: 11 trips / 5 nights, 3,857 km
  total 10,879 km | shortfall nights: 0
  plan km 87,134 + trunk 10,879 = 98,013 combined
```

Round-trip distances via OSRM (depot anchor ↔ B37 7HB), haversine fallback.
`plan_manifest` / validation metrics gain `trunk_km`, `trunk_trips`,
`trunk_shortfall_nights`. Baselines stay comparable: plan km vs plan km, and
the combined line is what plan-vs-reality comparisons use from now on (the
actuals side already contains real trunk driving).

### Config (freight_planner/config.py)

| knob | default | meaning |
|---|---|---|
| `TRUNK_ENABLED` | `True` | master switch |
| `TRUNK_DECK_PALLETS` | `52.0` | double-deck artic capacity — VERIFIED assumption, see above |
| `TRUNK_DEPOTS` | `("BEDFORD", "CB22")` | night-trunk depots — verified; STOKE deliberately absent |
| `TRUNK_NEXT_DAY_START` | `"10:00"` | drawn tractor's next-day availability |

(`TRUNK_DEPART_HOUR` / `TRUNK_PREP_MARGIN_H` already exist in cambridge config
and stay there — shared with scope window derivation.)

### New module + integration points

- `freight_planner/trunk.py` (pure): sizing from the demand/candidate frames
  (`trunk_schedule(candidates, window) -> list[TrunkNight]` with depot, night,
  import_pal, export_pal, trips, km), draw bookkeeping
  (`draw_tractors(schedule, vehicle_df, reserved) -> dict[(vid, day), avail_from]`).
- **Ordering constraint (important):** the draw must skip tour-reserved
  vehicle-days, and `reserved` only exists inside the multiday orchestrator
  (`tour_plan.run_multiday_seed_plan`) after tour selection. Integration
  therefore happens THERE: schedule + draw run after tours are reserved and
  before the daily seed; the resulting per-(vehicle, day) availability
  overrides are threaded into BOTH vehicle constructions — the daily seed's
  `_rv`/`_route_vehicle` (via an `avail_overrides: dict[(vid, day), time]`
  parameter on `run_route_seed_plan`) AND ALNS's own `_route_vehicle`/
  `_build_vehicle_meta` path (same parameter on `improve_route_seed`, threaded
  like `pinned_job_ids` was) — otherwise ALNS would happily move jobs back into
  the blocked morning. `run_alns.py` passes the overrides to ALNS and emits the
  KPI/run-log lines from the schedule the orchestrator returns.
- `kpi.py` / validation metrics: the trunk block.
- Viz/runsheets: out of scope for T1 v1 (trunk trips are a schedule, not routes;
  a later pass can render them).

### Out of scope

Stoke (verified no night trunk); rendering trunk trips in viz/runsheets;
hub-injection economics (K3); weekend trunks (telematics shows weeknights);
LE10 hazchem hub (negligible volume, stays as-is).

## Acceptance / measurement

One run per week, no tuning. Coverage must hold (99.7 / 99.9). Plan km expected
roughly flat to slightly UP (morning artic capacity shrinks); trunk line ≈
9-11k km/week matching the odometer analysis; combined km compared against
reality's odometer (which contains real trunk driving) should land near parity
— report the number as it falls. Trunk trips/night should average ≈6.5-8
(sanity: matches the 7.0 observed). Shortfall nights expected 0; nonzero =
stakeholder conversation.

## Testing

TDD. Unit: sizing arithmetic (ceil boundaries at exactly 52/104 pallets;
max(import, export) not sum; first-day imports excluded; last-day exports
included), rotation draw (deterministic, skips tour-reserved, shortfall path),
next-day availability adjustment, km line arithmetic. Integration: run_alns
wiring on a synthetic two-night fixture; full suite green.
