# Daily depot-collocated DIRECT consolidation — design

**Date:** 2026-07-17
**Status:** Spec for review (not yet implemented)
**Owner request:** "Y888AUK on Jan 12 did direct deliveries back and forth, while it could have
just been 3 deliveries without coming back to depot… we have a customer at this exact location
(ST4 8JB, same estate as the STOKE base). Those direct carries really should be a consolidated
delivery run. Go through 1-4 and write the spec for the fixes."

---

## 1. Problem

A customer sits on the same industrial estate as the STOKE base (customer **ST4 8JB**,
depot anchor **ST4 8HP**, a few hundred metres apart). Its outbound orders are FULL_FLEET
same-day moves that the planner serves as `DIRECT_CUSTOMER_MOVE` legs — and a DIRECT is an
**atomic arc**: `evaluate_route` walks `previous stop → origin → destination` inside one job
(`routing_adapter.py` `_TWO_POINT_KINDS` branch). Two DIRECTs can never have freight on board
at the same time, so consecutive same-origin orders force deliver-then-return ping-pong.

Receipts from `run_depotgate/2026-01/2026-01-12_to_2026-01-13`:

- **Y888AUK Jan 12** (`ROUTE:Y888AUK:2026-01-12#T1`): ST4 8JB→OL7 0QN, *return*, →M23 9LL,
  *return*, PL_EXPORT dock pickup, →WA8 0SW, home 19:11. ~469 km for 6 pallets.
- **C29BAL Jan 12**: drove to Wigan **twice** in one day (WN5 8DB seq 1, WN2 3DW seq 10, ~5 km
  apart) because each DIRECT is its own arc.
- **M888 WSM (CB22-homed) and X888GNW (BEDFORD-homed)** each carried a single small ST4 8JB
  DIRECT (97 / 101 km legs) — foreign vehicles moonlighting because nothing binds a DIRECT to a
  depot.
- Seven outbound ST4 8JB DIRECTs on Jan 12: 1–5 pallets each (~18 pal total — under one tractor
  load), six of seven into one NW corridor. ~682 plan-km where one loaded sweep is ~250–300 km.
- **Ground truth:** the real operator collected all of them in ONE dock visit 06:11–07:41
  (identical `origin_timestamp` in the source parquet). The freight was staged by 07:41.

Under the atomic model the ping-pong is *optimal for the solver*: inserting two same-origin
DIRECTs adjacently saves nothing, so ALNS has no consolidation gradient to climb. This is a
representation failure, not a search failure.

## 2. Why the existing machinery doesn't catch it

1. **Tour side already has the cure, daily side doesn't.** `TOUR_DEPOT_DIRECT_AS_DELIVERY`
   (shipped 2026-07-15, `tour_plan._as_depot_delivery`) reclassifies a DIRECT collected at its
   anchor depot into a depot-loaded delivery — but only inside the multi-day tour batcher.
   These orders are daily-range (60–90 km), so they never reach it.
2. **The DIRECT-vs-XDOCK resolver can't rescue them.** `resolve_options` runs with
   `allow_same_day_xdock=True` and the geometric choice *would* pick XDOCK for a collocated
   origin (via-depot km ≈ direct km ≤ 1.6×). But the same-day XDOCK delivery window is floored
   at `collection deadline + SAME_DAY_XDOCK_HANDOFF_MIN (90)` (`legs.staged_delivery_start`).
   These orders carry **date-only collection windows** that expand to the whole operating day,
   so the staged start (~19:30) lands past every delivery window end → `xdock_window_infeasible`
   → forced DIRECT. Deadline-anchoring is correct pessimism for a far origin; for a collocated
   origin it is pure loss, because the delivering vehicle can collect at its own doorstep on the
   way out.

## 3. Design

One sentence: **at leg emission, a same-day FULL_FLEET DIRECT whose origin lies within a small
radius of its source depot is emitted as a depot-loaded `CUSTOMER_DELIVERY` (single leg, no
XDOCK alternative), carrying a trip-wide departure floor for freight readiness and a hard
depot-bound gate so only vehicles homed at that depot may serve it.** Consolidation then comes
free: deliveries board together at departure and multi-drop through the corridor — machinery
that already exists and is well tested.

The four fixes (items 1–4 as discussed):

### 3.1 Reclassify at leg emission (`legs.py`)

Site: the **same-day FULL_FLEET branch** of leg building (where the DIR + XC/XD option group is
emitted today). The site moved from my earlier "candidate-build" sketch for a hard reason:
`build_initial_freight_states` derives freight states from **`legs_df`** (pre-resolve), not from
post-resolve candidates (`run_alns.py` input assembly). Emitting the reclassified shape at the
source makes candidates, compatibility, the resolver, freight states, seed ledger gates, the
manifest and the viz all inherit it from one site, per epoch, idempotently.

When `origin coords` are within `DAILY_ORIGIN_AT_DEPOT_RADIUS_KM` of
`DEPOT_ANCHORS[origin_depot]` (guard: anchor exists, coords geocoded — else emit legacy legs):

- Emit **one leg** instead of three (no XC/XD pair, `option_set=""` so the resolver skips it):
  - `suffix` stays `DIR` (leg/job identity, split-part machinery, and provenance unchanged)
  - `leg_kind=CUSTOMER_DELIVERY`, `origin_node=DEPOT`, `destination_node=CUSTOMER`
  - `service_pc=dest_pc`; `origin_pc` kept for provenance; **no** `origin_lat/lon`
  - `ready_state="AT_DEPOT"`, `result_state="DELIVERED"`
  - windows = the **delivery** windows (same `drs/dre/des/dee` the DIR leg carries today)
  - `freight_ready_time = effective collection start + COLLOCATED_STAGING_MIN`
  - `depart_floor = freight_ready_time` (new carried column, §3.2)
  - `depot_bound = origin_depot` (new carried column, §3.3)
- The multi-day branch is **untouched**: multi-day collocated orders already resolve to XDOCK
  on cost and their D-leg consolidates like any delivery.
- Freight states need no new code: an order whose only leg is a delivery falls into the existing
  `AT_DEPOT_OR_HUB_PENDING` shape branch with `initial_depot = source_depot` and
  `ready_time = freight_ready_time` (`state.py` FULL_END_TO_END → delivery-only branch) — the
  same state the 58 pre-window prestaged orders use, so the seed's
  `exists_at_depot`/`DELIVERY_BEFORE_PICKUP` gate passes by the established precedent.
- Log one line in the build: `collocated depot-deliveries: N (radius X km)`.

*Why not enable same-day XDOCK for collocated origins instead?* It spends two legs and two
vehicle touches per order (a 0-km serviced pickup + a delivery), keeps the pickup/delivery
handoff-conflict machinery in play, and prices dock work that the warehouse does — while ending
in the same place: a delivery from the depot. Reclassification is one leg on one vehicle and
matches the shipped tour-side semantics. The yard transfer is warehouse work, consistent with
how every other depot-loaded delivery treats loading (unpriced vehicle time), and the tight
radius (§3.4) bounds the approximation.

### 3.2 Departure floor binds trip-wide (`routing_adapter.py`)

Today `evaluate_route` honours `RouteJob.depart_floor` only when the floored job **leads** the
trip. For depot-loaded freight the floor is a property of the whole trip — the freight boards at
departure wherever the job rides in the sequence.

Change: route departure floor = `max(depart_floor over ALL jobs in the trip)`, applied once at
trip start (same `route_start_shift` mechanics, still composes with first-stop wait absorption;
B2 departure-based flooring unchanged). This also closes the latent gap where a prestaged
overnight-DIRECT arrival riding mid-trip would lose its hold today. Existing lead-job behaviour
is a special case of the new rule, so the two shipped depart_floor tests stay green.

Plumbing: `depart_floor` and `depot_bound` become carried columns
legs → candidate rows → `make_route_job` (which currently leaves `depart_floor` to the dynamic
`new_arrival_meta` stamping only). For intraday arrivals of collocated orders, the arrival
stamping becomes `depart_floor = max(readiness floor, creation floor)` instead of overwrite.

### 3.3 Hard depot-affinity gate (`RouteJob.depot_bound`)

Investigation finding: the daily path has **no depot-affinity enforcement at all** —
`same_depot/cross_depot` in the compatibility frame are labels, not gates, and in the depot-gate
run **135 of 603 routed deliveries** ride vehicles homed away from the freight's source depot
with no depot visit in the route (e.g. BEDFORD-homed X888GNW departing Bedford straight to a
CB22-sourced delivery at B32 3BZ — the freight teleports). Fixing that hole fleet-wide is out of
scope (§6); the requirement here is that the NEW legs don't inherit it.

Mechanism — one unbypassable site, mirroring how SHIFT/CAPACITY work:

- `RouteJob.depot_bound: str = ""` (required home depot; empty = unconstrained, all existing
  jobs unchanged).
- `evaluate_route`: if any job has `depot_bound` and `vehicle.home_depot != depot_bound` →
  `_infeasible("DEPOT_BOUND")`. Covers seed, ALNS operators, micro insertions, warm re-opt and
  emission in one place; honest fall-through to slip machinery when no home vehicle fits.
- Tour paths don't go through `evaluate_route`, so the existing WT254009 helper
  `_depot_bound_mismatch` (tour attach + commissioning) is extended to also flag
  `cand.depot_bound and home != cand.depot_bound`, with `prefer_depot=depot_bound` on
  commissioning picks — same shape as the shipped pickup gate.

### 3.4 Knobs and CLI (config + both entrypoints)

```python
# config.py
DAILY_DEPOT_DIRECT_AS_DELIVERY: bool = True   # master switch for §3.1
DAILY_ORIGIN_AT_DEPOT_RADIUS_KM: float = 2.0  # collocation radius for the DAILY reclassification
COLLOCATED_STAGING_MIN: float = 30.0          # collection open -> freight loadable at the dock
```

- **2.0 km, deliberately not the tour side's 8 km.** On a 200+ km tour an unpriced 8 km approach
  is noise; on a daily trip it would hide up to 16 km of real driving per order. 2 km covers the
  shared-estate case (ST4 8JB ≈ 0.5–1 km) and nothing else. The tour knob is unchanged.
- **30 min staging.** Real op collected 06:11–07:41 after a 06:00 window open; the floor says
  "the trip may not depart before collection-open + 30 min", which is cheap insurance against a
  time-paradox plan (departing with freight not yet collectable) without inventing a fake
  90-min pessimism.
- CLI: `--no-daily-depot-direct-as-delivery` / `--daily-depot-direct-as-delivery`,
  `--daily-depot-direct-radius-km`, `--collocated-staging-min` on `run_alns` and `run_rolling`,
  wired through the existing `_apply_*_flags` pattern (same as
  `--no-tour-depot-direct-as-delivery`). Flag off = byte-identical legacy leg emission
  (ablation path for the dissertation).

## 4. What emitted artifacts look like after

- `plan_full.csv` / `route_stops.csv`: the seven orders appear as `customer_delivery` stops on
  STOKE-homed routes, loads stepping DOWN across consecutive corridor drops (co-loaded at
  departure); no ST4 8JB revisits between drops; `leg_id` still `…:DIR`.
- Map: no `collect_lat/lon` → drawn as ordinary delivery legs from the depot — faithful, and the
  ping-pong polylines disappear.
- KPI/service ledger/universe report: `CUSTOMER_DELIVERY` rides existing paths; `DEPOT_BOUND`
  may appear as a new honest failure reason in unassigned/blocked tallies.
- `06_plan_choices.md`: unchanged for other orders; collocated orders no longer appear as
  resolver decisions (they have no option group) — the legs-build log line is their receipt.

## 5. Tests (TDD, RED before GREEN)

1. **legs**: collocated same-day order emits exactly one `CUSTOMER_DELIVERY` leg (`:DIR`,
   `AT_DEPOT`, floor = collection open + 30 min, `depot_bound=STOKE`, no XC/XD legs); an origin
   just OUTSIDE the radius emits the legacy DIR+XC+XD trio; flag off = legacy trio; missing
   anchor/geocode = legacy trio.
2. **routing_adapter**: a floored job in mid-trip position holds the trip's departure (new); the
   two shipped lead-job floor tests stay green; `depot_bound` job on a foreign-homed vehicle →
   infeasible `DEPOT_BOUND`; on the home vehicle → feasible.
3. **state**: reclassified order's initial freight state = `AT_DEPOT_OR_HUB_PENDING` at the
   source depot with the staged ready time.
4. **end-to-end seed/ALNS**: two collocated orders + one STOKE vehicle → one trip, both aboard
   (peak load = sum), destinations served consecutively; a cheaper-looking foreign vehicle is
   never chosen.
5. **tour paths**: a `depot_bound` candidate never attaches/commissions onto a foreign-homed
   tour (extension of the WT254009 tests).
6. **CLI**: flag mapping round-trips into config for both entrypoints.

## 6. Explicitly out of scope (flagged findings, owner's call later)

- **Cross-depot delivery teleport (pre-existing):** 135/603 deliveries in the depot-gate run ride
  foreign-homed vehicles with no source-depot visit. Interacts with deliberate OVERFLOW staging
  semantics; needs its own design (materialize a daily DEPOT_LOAD stop, or gate + reposition).
- **Daily pickup landing-depot hole (pre-existing):** a daily `CUSTOMER_PICKUP` lands freight at
  the vehicle's return depot while the ledger stamps `target_depot` (e.g. fb87a861: ST4 8JB
  pickup targeting STOKE riding Bedford-homed X888GNW). Same class as WT254009 but on the daily
  path.
- **Inbound mirror case:** DIRECTs *terminating* at the collocated customer (e.g. WT254943
  M12 5DD → ST4 8JB) could co-load with other collections, but need a "delivered at depot yard"
  ledger semantic — round two.
- **Per-order resolver reasons** in `06_plan_choices.md` (observability nice-to-have).

## 7. Validation plan

Rerun the 2-day probe (Jan 12–13). Acceptance:

1. All seven ST4 8JB same-day DIRECTs emit as depot deliveries on STOKE-homed vehicles and
   co-load into ≤2 corridor sweeps (route_stops shows descending loads, no ST4 8JB returns
   between drops).
2. Stoke-area vehicle-days and total km drop; no foreign-homed vehicle carries these orders.
3. Service ledger unchanged or better (453 ON_TIME / 0 SLIPPED / 0 UNSERVED baseline).
4. `--no-daily-depot-direct-as-delivery` reproduces the current plan (ablation sanity).
5. Full suite green (914 + new tests).
