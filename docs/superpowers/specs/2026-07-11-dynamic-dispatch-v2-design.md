# Dynamic dispatch v2 — warm-start, insertion, and a single noon re-optimization

**Date:** 2026-07-11
**Status:** design of record for the dynamic phase. Supersedes the R1 dynamic design in
`2026-07-10-rolling-horizon-e6-design.md` §4.7a. The static seed, the visibility model,
the promise-fixed slip rule, the dock-based trunk, tours, the shuttle carve-out, weekly
handover and the emission contract are unchanged and carried forward.
**Motivation:** the smoke-9→11 validation campaign proved the *re-seed-every-anchor*
architecture is the root of a whole class of non-anticipation bugs (a noon solve rebuilds
a morning trip from scratch and stamps new orders with morning times). This spec replaces
that architecture with warm-start + incremental insertion + one bounded re-optimization,
scored by a single objective. See `2026-07-10-dynamic-dispatcher-structural-review.md` for
the forensics that led here.

---

## 1. The core principle: one engine, three swapped inputs

There is exactly one ALNS engine — destroy operators, regret/greedy repair, SA/RRT
acceptance, adaptive operator weights. It is built once and never forks. Everything that
differs between the static seed and the dynamic phase is one of three *inputs* to that
same engine:

1. **Starting solution** — where the loop begins.
2. **Destroy scope** — which stops the loop may remove.
3. **Objective** — how a candidate is scored.

| | Starting solution | Destroy scope | Objective (see §5) |
|---|---|---|---|
| **03:00 seed** | empty / from scratch | everything (nothing committed) | `(served, −cost)` — disturbance is 0 because there is no reference plan |
| **Dynamic phase** | the current live plan (warm start) | only uncommitted stops | `(served, −(cost + β·disturbance))` |

The objective does **not** fork into two functions. It is one function,
`(served, −(cost + β·disturbance(candidate, reference)))`, and the seed is its degenerate
case: with no reference plan the disturbance term is identically zero, so the seed scores
on pure cost. `β = 0` in the dynamic phase recovers the same behaviour, which is the
regression gate (§11).

## 2. The daily rhythm

Grounded in the Jan 12–17 reveal data (serviceable collections only — the physical pickup
work, accounting-only entries excluded):

- ~200–280 new collections arrive per operating day, plus ~160 advance-booked before the
  window; the same-day-target subset floods in during the **morning** (8.8% known by 07:00,
  51.8% by 09:00, 83.2% by 11:00, **93.6% by 12:00**, only 6.4% more all afternoon).

The rhythm has four moments. Two are solver runs, two are not.

1. **03:00 — static seed (solver, full run).** Builds the day's skeleton from the overnight
   book and the 18:00 import manifest: multi-day tours, the shuttle carve, the trunk demand,
   and the morning sweeps. Heavy, irreversible decisions live here where runway and
   information are maximal.
2. **Every 30 min, all operating day — insertion (solver, repair-only).** A micro-batch of
   newly-visible orders is slotted into the live plan: onto an in-flight trip's open suffix
   when a vehicle is already heading that way, or onto a new floored trip otherwise. Near-free
   (no destroy loop). This absorbs the morning flood.
3. **12:00 — noon re-optimization (solver, full destroy+repair, bounded).** The one expensive
   consolidation pass, timed at the information peak. Warm-starts from the live plan; may only
   destroy the *uncommitted* horizon; plans **same-day single-day work only** (§8). This is
   what insertion alone cannot do: merge the morning's scattered tentative work into clean
   sweeps.
4. **18:00 — day close (non-solver accounting).** Sizes tonight's trunk from the dock
   (committed collections + tomorrow's revealed manifest) and rolls the slip pool. No
   optimization.

**One re-opt is the default.** After noon there is almost nothing new to consolidate, and
before noon most morning capacity is already committed. A second mid-morning re-opt (~10:00)
is the single open lever, to be settled by ablation (§11), not assumed.

## 3. The commitment frontier (two levels, clock-driven not GPS)

Commitment is a rolling "point of no return" that advances with the **clock and the plan**,
never with live vehicle positions (we have no telematics; the plan's own clock is the
position feed). It operates at two zoom levels:

- **Trip level — the expiry rule.** A trip whose departure falls within `now + δ_R1` is
  *launched*: sent to the driver, moved into the in-flight set, no longer changeable. `δ_R1`
  = solve wall + driver notification + contingency (default 90 min at anchors; a smaller
  `micro-δ` of 30–45 min for insertion passes, since a repair pass computes in seconds — a
  config dial, see §11). Delivery trips are frozen *whole* at launch: the freight is on
  board, so no delivery stop can be removed once dispatched.
- **Stop level — the watermark.** Inside an in-flight trip, each stop the driver has begun or
  is rolling toward (per the plan clock) is locked; the suffix ahead of that mark stays open.
  "Open" means open to *inserting a new collection* into the remaining path only — existing
  stops are pinned to their vehicle (delivery stops absolutely; collection stops v1: no
  cross-vehicle moves). This is the "returning vehicle grabs a nearby pickup" case.

Drivers are pushed to **only what crosses the frontier**, at most at the 30-min cadence, and
β (§5) suppresses churn near the front so mid-route pushes stay rare. The insertion cadence
updates the *plan*; the frontier decides what becomes a *driver instruction*.

## 4. The architectural shift that fixes non-anticipation

**Anchors stop re-seeding.** In v1 every anchor ran the seed from scratch and then ALNS,
protected by freeze/floor overrides that proved leaky. The noon re-seed rebuilt the morning
CB9/MK42 sweeps as if the day were blank and backfilled them with noon-booked orders at
morning times.

In v2 the noon anchor **warm-starts from the live plan and only inserts/re-optimizes the
uncommitted horizon**. The morning trips are already departed and frozen, so a new order can
only land on the open suffix (subject to `floor_ok`, arrival ≥ now + δ) or on a fresh floored
trip. There is no path by which a noon order acquires a morning service time, because nothing
rebuilds a morning trip after it has departed. The non-anticipation class dies structurally,
not by another override patch.

The `audit_non_anticipation` guard (a collection stop served before its order's
`timestamp_created` raises under `FP_STRICT_CAUSALITY`, logs otherwise) stays as the tripwire
that proves it.

## 5. The unified objective

```
score(candidate) = ( served(candidate),  −( cost(candidate) + β · disturbance(candidate, reference) ) )
```

- **Lexicographic, served first.** Coverage is never traded for stability; disturbance lives
  in the second key only. β must not be able to refuse an order to stay stable.
- **cost** — the existing per-type fuel-weighted generalized cost (GBP), unchanged.
- **disturbance(candidate, reference)** — deviation from the plan being warm-started:
  ```
  disturbance = Σ_j w(j)·reassigned(j) + γ · Σ_j w(j)·resequenced(j)
  ```
  `reassigned` (job moved to a different vehicle) weighted heavier than `resequenced`
  (reordered on the same vehicle) via `γ < 1`. `w(j)` is the **imminence weight**: high for
  jobs about to dispatch, decaying toward the far end of the day — this is what makes
  "changes to soon-to-happen stops hurt more" fall out of the arithmetic. One scalar dial β;
  the imminence shape lives in `w(j)`.
- **reference** is the live plan as it stood before this solve. Seed: `reference = ∅` ⇒
  `disturbance = 0`.

## 6. Insertion mechanics

Insertion is two formulas, both already in `alns.py` and carried forward:

- **Placement — cheapest feasible insertion.** For job `j` at position `p` on a vehicle-day,
  the marginal generalized cost, computed by a *full day re-evaluation* (breaks/duty/windows
  make it non-decomposable, so no local-detour shortcut):
  ```
  c(j, key, p) = rate(vehicle) · ( Day_km(route+j@p) − Day_km(route) + oa_penalty(j) )  +  β · D(j, key, p)
  ```
  `D` is the imminence-weighted downstream displacement the insertion causes (≈0 for an
  append onto a tentative far trip; large for shoving a soon-to-dispatch trip — so β steers
  inserts toward stable placements). Opening a new floored trip is one more candidate position.
- **Selection — regret-2.** `regret(j) = c*_2(j) − c*_1(j)`; place `argmax_j regret(j)` at its
  best position. A job with one feasible slot gets `regret = ∞` (place now).
- **Gates (causal safety):** `min_position = watermark` (no insertion before committed
  stops), `floor_ok` (arrival ≥ now + δ_R1), `lock` (onboard/committed jobs pinned to their
  vehicle).

**Open empirical item:** E3 found regret worthless in the *batch* solve, but the one-shot
insertion regime is where it may invert. Regret-2 is the default; its value here is an
ablation (§11), not an assumption.

## 7. Visibility & who is ours to serve (unchanged, restated)

- **Creation gate.** An order exists for any solver only once `timestamp_created ≤ now`.
  Deliveries reveal at 18:00 the day before their service day. **Shuttle-exempt orders are
  creation-gated too** — the exemption lifts the delivery-reveal delay and dedicates capacity,
  it never reveals an order before it was booked (smoke-9 fix).
- **Serviceable collections only.** `collect_ids` = collection-flow orders with a real pickup
  leg (`CUSTOMER_PICKUP` / `DIRECT_CUSTOMER_MOVE`). `ACCOUNTING_ONLY` entries (network- or
  subcontractor-handled) are excluded from the served-universe, the slip pool and the ledger
  (smoke-10 fix — they were 100 of 103 phantom "unserved").

## 8. Where heavy decisions live

- **Multi-day tours** are formed **only at the 03:00 seed**, with a full day of runway. The
  noon re-opt *preserves* in-flight tours (frozen) but forms **no new ones**; a far order that
  arrives mid-day and cannot be same-day-collected **slips** to the next 03:00 seed via the
  promise-fixed rule. The re-opt therefore plans same-day single-day work only.
- **The shuttle** is standing capacity carved at the seed on visible (creation-gated) orders.
- **The trunk** is sized only at the 18:00 close from the dock; epoch-internal trunk estimates
  never re-draw past nights (Fix 1).

## 9. Compatibility contract

Everything shipped stays working, flag-off bit-identical where a flag exists:
tours, K1 shuttle, T1 trunk, weekly handover, the single `emit_outputs` emission, the B16
conserve guard, and the whole static E1 path (a plain `run_alns` run must be unchanged).
`β = 0` reproduces cost-only dynamic behaviour. The `run_dynamic_loop(cfg, ctx)` extraction
with injectable `build_fn/solve_fn/trunk_fn` and the scripted-solver harness
(`test_dynamic_e2e.py`) are retained and extended.

## 10. Component boundaries

- **`objective`** (new, in `alns.py`): `disturbance(candidate, reference, w, γ)` and the β
  fold into `candidate_total`. Pure, unit-tested; `β=0` ⇒ identity.
- **`reference plan`** threading: the live plan handed to each dynamic solve as the disturbance
  baseline (the injected in-flight solution already exists; extend it to cover the uncommitted
  warm-start body).
- **`run_dynamic_loop`**: anchors become warm-start-and-reoptimize (drop the re-seed);
  insertion passes unchanged in shape; the noon re-opt is the only full destroy+repair.
- **commitment frontier**: `expire_commit` (trip) + `advance_watermarks`/`suffix_locks` (stop)
  — carried forward unchanged.

## 11. Dials and the one open question

- **β** — imminence-scaled stability weight. `β=0` = pure efficiency (and the regression gate).
- **Batch window** — 30 min through the morning surge; 60 min after 13:00 acceptable.
- **micro-δ** — a separate, smaller commitment lag for insertion passes (30–45 min) since they
  compute in seconds; widens the late-afternoon same-day pickup slot. CLI dial.
- **Open ablation:** one noon re-opt vs. two (10:00 + noon), compared on planned km and churn.
  Prior: one is enough (insertion absorbs the morning onto suffixes). Measure, don't assume.
  Likewise a regret-on vs regret-off ablation for the insertion pass.

## 12. Testing strategy

- **β=0 regression gate** — dynamic with β=0 must match cost-only behaviour (flag-off
  discipline).
- **Scripted-solver harness** (`test_dynamic_e2e.py`) — every historical smoke crash is a
  sub-second scripted regression; add a warm-start-no-reseed reproduction (a noon arrival can
  never acquire a morning service time) and a disturbance-ordering test (high β keeps a
  soon-trip stable; low β re-consolidates the far horizon).
- **`audit_non_anticipation`** — 0 violations is a hard gate on any citable run
  (`FP_STRICT_CAUSALITY`).
- **`validate_dynamic` A–G** — non-anticipation (stop-arrival vs created), ledger
  reconciliation, conservation, promise-fixed, temporal, same-day curve vs reality, trunk vs E1.

## 13. Cleanup carried in this change

Stale scaffolding removed with the suite staying green: the `FP_DEBUG_KEY` per-vehicle-day
probes in `run_rolling.py` and the inject-merge probe in `alns.py` (one-off diagnostics from
the smoke campaign, env-gated, superseded by the harness + audit). `merge_frozen_routing` is
retained (it has a unit test and is harmless) but flagged as loop-unused.

## 14. Non-goals / deferred

- Live telematics ingestion (event-triggered re-opt on real positions) — parked; the frontier
  is the clock-driven stand-in.
- Cross-vehicle suffix moves for in-flight collections (v1: pinned to vehicle).
- Fix 2b idle-vehicle mid-day dispatch (needs trip-level floors as solution state).
- The `--strict` whole-trip-freeze config is retained for the E6 floor measurement but is not
  the operating mode.
