# Stoke PL_EXPORT Day-Trunk — Design

**Date:** 2026-07-12
**Status:** approved (implement + smoke 12-17)

## Problem

Stoke has **no night trunk** (`TRUNK_DEPOTS = ("BEDFORD", "CB22")`). A Stoke
PL_EXPORT order is collected to the Stoke depot (the `CUSTOMER_PICKUP` TRUNK
option), but its `OUTBOUND_TRUNK` leg (depot→hub) is `customer_dispatchable=False`
and `trunk_schedule` never sizes it for Stoke — so the freight **strands at the
depot** and never reaches the Palletline hub (B37 7HB). `resolve_hub_drop` keeps
picking TRUNK for Stoke (customer≈depot) — which is only wrong because the TRUNK it
picks is a dead end.

Telematics: Stoke's B37 visitors run **10:00–17:00 daytime hub drops** (config
comment; zero night visits). So the real flow is a **same-day** run to the hub, not
an overnight trunk.

## Fix — a Stoke same-day day-trunk (reuse the trunk machinery)

Model Stoke as a **day-trunk depot**: PL_EXPORT collected day N is consolidated and
run to B37 the **same day** (`ceil(export pallets / 26)` trips, a drawn Stoke
tractor, real round-trip km). This is the day analogue of the CB22/Bedford night
trunk and reuses `trunk_schedule` + `draw_tractors` wholesale.

Key consequence: with a real Stoke depot→hub trunk, `resolve_hub_drop`'s existing
TRUNK choice becomes **valid** — collect to Stoke depot → day-trunk to hub. So **no
change to `resolve_hub_drop` or `legs.py`**, and the mega-shipper's freight is
consolidated (no per-order HUB_DROP bouncing).

### Changes

1. **config.py:** `TRUNK_DAY_DEPOTS: tuple = ("STOKE",)` — depots with a same-day
   trunk to the hub (no night trunk; telematics-verified daytime drops).

2. **trunk.py:**
   - `TrunkNight` gains `day_trunk: bool = False`.
   - `trunk_schedule`: the **export** direction may originate from
     `TRUNK_DEPOTS ∪ TRUNK_DAY_DEPOTS`; **import** stays `TRUNK_DEPOTS`-only
     (day-depots are export-only for now). Emitted nights set
     `day_trunk = depot in TRUNK_DAY_DEPOTS`. Export night = collection day
     (unchanged), so a day-trunk's `night` field IS its run-day. Weeknight filter
     unchanged (a Sat/Sun collection would not day-trunk — Palletline weekend
     closure).
   - `draw_tractors`: **never** apply the next-day hold to a `day_trunk` night — a
     day-trunk consumes THIS day, not the next morning (belt-and-suspenders even
     when `TRUNK_NEXT_DAY_HOLD` is on).

3. **roundtrip_km construction** (both sites — `tour_plan.py` seed and
   `run_rolling.py` day-close): iterate `TRUNK_DEPOTS + TRUNK_DAY_DEPOTS`, so
   `(STOKE, B37_HUB)` km exists (STOKE is in `DEPOT_ANCHORS`).

## Non-goals

- Stoke PL_IMPORT inbound day-trunk (hub→Stoke) — the stated problem is EXPORT;
  import is a follow-up.
- A daytime-hub utilization debit on the drawn tractor (deferred, same open item as
  the CB22/Bedford daytime hub flow) — the trunk counts km + a tractor; the tractor
  is still available for daily routes, consistent with the hold-off decision.
- Vehicle-to-vehicle attribution (stakeholder: "doesn't matter who does the work,
  but the work has to be done").

## Impact / risks

Additive: Stoke gains trunk trips + km + tractor draws that were 0 before (Stoke
PL_EXPORT freight now reaches the hub instead of stranding). CB22/Bedford
unaffected. Existing `test_stoke_never_scheduled` INVERTS — Stoke export now
day-trunks (test updated). Stoke's 5-tractor pool may shortfall on heavy export
days — logged loudly, km still counts (freight moves in reality), same rule as
today.

## Tests (TDD)

1. `trunk_schedule`: a Stoke PL_EXPORT pickup → exactly one night, `day_trunk=True`,
   `trips = ceil(pallets/26)`, `night = collection day`, `km = trips ×
   roundtrip_km[(STOKE, B37_HUB)]`.
2. `trunk_schedule`: a Stoke PL_IMPORT delivery → NOT scheduled (export-only).
   Replaces `test_stoke_never_scheduled`.
3. CB22/Bedford night nights keep `day_trunk=False`.
4. `draw_tractors`: a `day_trunk` night draws a Stoke tractor; with
   `TRUNK_NEXT_DAY_HOLD=True` it still emits **no** avail_override for that draw.
5. Full suite green (fix any roundtrip_km / integration fallout).

## Smoke

Run the rolling dispatcher on 2026-01-12..17 into a NEW
`freight_planner/runs_stoke_daytrunk/…` folder; confirm the trunk log now shows
STOKE trips/km (was 0) and the run completes clean.
