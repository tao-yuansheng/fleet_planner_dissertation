# Same-Address Dwell Merge — Design

**Date:** 2026-07-12
**Status:** superseded in part on 2026-07-29; same-address merging remains live

> This design records the former base-plus-per-pallet dwell model. The live
> evaluator now charges a fixed visit dwell (van/rigid 15 minutes; tractor
> 30 minutes), and contiguous orders at identical coordinates share that one
> dwell. The historical problem statement and implementation discussion below
> are retained as design provenance.

## Problem

The model bills one full service block **per order**. A stop's service time is
`service_minutes = SERVICE_BASE_MIN (10) + per_pallet * pallets`
(`6/pallet` tractor, `3/pallet` rigid; cambridge/config.py). The 10-min base is
defined as *"paperwork / check-in"* — a **per-visit** cost.

Because `legs.py` emits one leg per order → one `RouteJob` per leg → the route
evaluators (`evaluate_route`, `evaluate_tour`) charge a **full base + per-pallet
block on every job**, a single-shipper collection fragments into many stops each
re-charging the base. Observed: B29BAL collects 17 orders at ST4 8JB (one dock)
as 17 stops totalling **350 min**; one honest visit is `10 + 6*30 ≈ 190 min`.
The fragmentation invents ~2.7 h of phantom check-in time. Same on the intraday
insert path (each inserted job carries its own base). The `merge_sweep` pass does
**not** fix this: it only fires across ≥2 distinct vehicles and, even then,
consolidates *trucks-per-dock*, not *dwell-per-dock* (the guest keeps its block).

## Fix (approach a — evaluator base-skip)

When the flag is ON, a **single-point** stop (`CUSTOMER_PICKUP` /
`CUSTOMER_DELIVERY`) whose coordinates equal the **immediately preceding** stop's
coordinates charges **per-pallet only** — the vehicle is already parked at that
dock, so no fresh check-in. The base is charged **once per contiguous
same-address run**; if the route leaves and returns (A, B, A) the second A is a
new visit and re-charges the base.

- **Address key:** exact `(lat, lon)` equality against the previous stop. In this
  pipeline a postcode geocodes to one cached lat/lon, so equality == same
  postcode, and it is exactly the condition that already makes the inter-stop
  drive 0 km. Floats are the same cached object, so `==` is safe (no epsilon —
  which also avoids merging distinct-but-near addresses).
- **Base amount:** `service_minutes(0.0, vehicle_type)` (== base), subtracted from
  the full block. Decouples the skip from the `SERVICE_BASE_MIN` constant and is
  correct for both tractor and rigid.
- **Two-point moves** (`DIRECT_CUSTOMER_MOVE`, `HUB_DROP`) are unchanged: they are
  atomic origin→dest legs whose `*2` handling covers both ends; the same-address
  skip does not apply.
- **First stop:** naturally excluded — its "previous" coord is the vehicle start
  (a depot), which never equals a customer. A mid-day *resume* start that already
  sits at the stop's coords is genuinely "already there", so skipping the base is
  correct there too; the rule keys purely on coord equality.

## Scope

Both evaluators, under one flag, so the behaviour is model-wide and identical
wherever routes are priced (seed, ALNS, merge_sweep, tours, intraday inserts):

- `freight_planner/routing_adapter.py` `evaluate_route` (daily routes).
- `freight_planner/tours.py` `evaluate_tour` (multi-day tours).

## Flag & regression gate

`freight_planner/config.py`: `SAME_ADDRESS_DWELL_MERGE: bool = False`.

Flag OFF must be **byte-identical** to today (the hard regression gate): the new
branch is guarded by the flag read once per evaluation, so OFF never enters it.
Flag ON is the intended behaviour change — freed duty hours can make previously
tight days feasible, so ON may change chosen routes (that is the point).

## Out of scope

- Per-pallet **rate** recalibration for mega-shipper docks (separate question).
- #2 Stoke / daytime inbound+outbound hub trunks (separate work).

## Tests (TDD)

1. Flag ON: two consecutive same-coord pickups charge base once (tractor):
   `10 + 6*p1 + 6*p2`, not `2*(10 + 6*p)`.
2. Flag ON: two pickups at **different** coords each charge base (unchanged).
3. Flag ON, non-contiguous `A, B, A`: the second A re-charges the base.
4. Flag ON: rigid uses the rigid base/rate; base still skipped once.
5. Flag OFF: identical to pre-change (covered by the existing suite staying green).
6. `evaluate_tour`: same base-once behaviour under the flag.
