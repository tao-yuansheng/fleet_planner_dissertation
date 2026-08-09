# Verified-Leg Correctness Fix — Design

**Date:** 2026-07-03
**Status:** Approved (design), pending implementation plan
**Author:** freight_planner session
**Standing rules:** NO git commits ever. Tests run from
`e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics` with `python -m pytest`.

## Problem

`planning_agent/verify_legs.py` classifies, per order, which physical leg our fleet
did (COLLECTION / DELIVERY / FULL_FLEET) from telematics. That verified leg is
**consumed as truth in forward mode** — `cambridge/verified_legs.py::corrected_flow`
overrides the raw Qargo api tag with it, so a wrong verified leg silently mis-plans
the order.

Two defects, both surfaced by order **WT254741** (`d220744d-…`, Ardex CB9 8QP →
Rayleigh SS6 7NG, 34 pal, booked N8GNW+TA70WTL, api_flow FULL_FLEET):

1. **Match precision.** `_pc_matches` requires the order postcode to *start with* the
   telematics postcode (`order_pc.startswith(telem_pc)`). Telematics `Location_Postcode`
   is reverse-geocoded from GPS and snaps to the nearest/containing unit. Our vehicle
   N8GNW was stopped **147 m** from the order's SS6 7NG at 06:49 — but the ping was
   labelled `SS6 7UA` (adjacent unit, same sector), so the match was **rejected**. The
   real delivery evidence was discarded.

2. **Evidence asymmetry.** With the delivery discarded, the verifier fell to the
   substitute path and found a fleet vehicle (`Y88RNW`) merely *present* at the shared
   Ardex origin, and stamped the whole order **COLLECTION**. But **57 orders left CB9 8QP
   that day for 49 destinations** — presence at a shared shipper proves nothing about
   *which* order a vehicle carried. The verifier fabricated a leg from near-worthless
   evidence while discarding the strong, order-specific delivery evidence.

Aggregate scale: **960 FULL_FLEET → single-leg demotions**; 68% are inter-customer
directs (both ends >15 km from a depot), 71% overnight, concentrated in mega-shippers
(CB98 250 + ST48 240 = half). **1,875 `telematics_substitute` rows** (1,019 COLLECTION)
are the weakest-evidence class.

## Principle

Telematics gives a **lower bound** on what we did: it can *confirm* a leg when the
endpoint is order-specific, but **absence at a shared endpoint is not evidence the leg
wasn't ours.** The booking's api label is the contractual scope; telematics may only
**demote** it on *positive contrary evidence*, never on non-observation at a shared site.

An endpoint match is **order-specific** in exactly two cases:
- **Booking linkage** — the *assigned* (booked) vehicle is seen there (booking ties the
  vehicle to this order), regardless of site busyness; or
- **Endpoint uniqueness** — *any* fleet vehicle is seen there AND few orders use that
  endpoint that day (a substitute at a unique customer almost certainly carried it).

## Components

### C1 — Matching primitive: `is_at(endpoint, ping)`
Replaces the asymmetric prefix logic in `_pc_matches` / `_stopped_at` / `_any_fleet_at`.

- **Primary:** haversine(telematics lat/lon, endpoint geocoded coords) ≤ **250 m**.
  Endpoint coords come from the existing `postcode_cache.json` (95.3% of orders have both
  endpoints geocoded); telematics rows already carry Latitude/Longitude.
- **Fallback:** when a ping OR the endpoint has no usable coords, symmetric **sector**
  postcode compare (truncate both to the UK sector — 4 chars for a 3-char outward, 5 for
  a 4-char outward, per the existing `_sector_len`) and compare for equality. No more
  `startswith`.
- Unchanged: stop gate (`GPSSpeed < 5`), the time windows (collection ±180 m, delivery
  ±120 m, `PLACEHOLDER_EXTRA` widening), and the depot-area handling (depot stops still
  need sector precision so a depot stop can't masquerade as a neighbour delivery).

### C2 — Endpoint shared-ness
- Precompute `endpoint_order_count[(postcode_norm, service_date)]` = number of DISTINCT
  orders using that endpoint (as origin or destination) on that date, from the eligible
  order frame.
- **shared** ⟺ count **≥ 5**; **unique** otherwise. Default threshold **5**; a single
  named constant, revisited during the measurement run.
- Service date for an endpoint = the leg's own anchor date (origin date for a collection
  point, destination date for a delivery point).

### C3 — Evidence gating in `classify_leg`
- **Step 1 (assigned vehicle):** unchanged in spirit — an assigned vehicle matched (via
  C1) at either end is positive evidence. Two-end match ⇒ FULL_FLEET (HIGH). One-end ⇒
  that leg (HIGH), subject to C4 for FULL_FLEET bookings.
- **Step 2 (substitute vehicle):** a substitute match counts as positive evidence **only
  at a UNIQUE endpoint** (C2). A substitute at a SHARED endpoint is **ignored** (not a
  match). This removes the fabricated mega-shipper COLLECTIONs. When both ends are shared
  and only substitutes are present, Step 2 yields nothing and the row falls through to the
  structural / inferred tiers exactly as an untelematic order would.

### C4 — FULL_FLEET demotion trigger
A `FULL_FLEET` booking with exactly **one** leg confirmed (C3) stays **FULL_FLEET**
unless **all** of the following hold — then demote to the confirmed single leg:
- `powered == 1` (one powered vehicle booked — a genuine solo direct, not consolidation);
- that vehicle was **tracked** in the window (had ≥1 ping) — so its absence is meaningful;
- the confirmed endpoint is **unique** (C2);
- the **other** endpoint is **unique** (C2) AND the tracked vehicle was **not** within
  250 m of it at that leg's time.

WT254741 (`powered == 2`) never satisfies this ⇒ stays FULL_FLEET. The structural rule
(`ships > powered` ⇒ single leg, 99.6% validated) and the `flow_full_fleet` LOW-confidence
keep are **untouched** — they remain the guard against over-classifying FULL_FLEET.

## Data flow & isolation

- All changes are inside `planning_agent/verify_legs.py` (matching helpers, shared-ness
  precompute, `classify_leg` gating). `cambridge/verified_legs.py::corrected_flow` and the
  enrich/demand consumers are **unchanged** — they keep reading the same CSV schema.
- The CSV schema is unchanged (order_id, order_name, api_flow, service_date, leg,
  confidence, method, matched_vehicle). New method labels may appear (e.g. a substitute
  ignored at a shared origin no longer emits `telematics_substitute`).

## Rebuild, preservation & order-by-order comparison

- **Preserve the current dataset as the baseline.** Do NOT overwrite in place. Before the
  rebuild, snapshot the existing `planning_agent/verified_legs.csv` (e.g. copy to
  `verified_legs.before_gpsmatch.csv`) and **leave the existing
  `freight_planner/data/enriched_orders_2026-01_2026-02.parquet` untouched** until the
  comparison is reviewed. The enriched parquet is only rebuilt after the diff is accepted.
- **Regenerate** `verified_legs.csv` (Jan+Feb) with the new logic.
- **Order-by-order comparison** (the required audit): emit a diff CSV keyed on order_id
  joining old vs new — columns: `order_id, order_name, api_flow, old_leg, new_leg,
  old_confidence, new_confidence, old_method, new_method, changed` plus context
  (`origin_pc, destination_pc, powered, ships, origin_shared, dest_shared`). Summaries:
  transition matrix (old_leg × new_leg), how many of the 960 FULL_FLEET demotions revert,
  how many substitute-COLLECTIONs dissolve, count of each old→new method transition.
- **Spot-check** a labelled sample of the largest transition buckets by hand (WT254741 must
  land FULL_FLEET).
- **Forward measurement** (only after the enriched rebuild): one run each wk1/wk2 →
  coverage + combined-km delta. Hypothesis: genuine full-fleet directs replacing
  collect-to-depot half-orders **reduce** modelled km.

## Calibration risk

Relaxing demotion can re-admit some of the ~30% "ships==powered but not truly full" cases.
The `powered == 1` trigger in C4 plus the untouched `ships > powered` structural rule hold
that line; the order-by-order diff and the forward run are where we confirm the net effect
before committing to the enriched rebuild.

## Testing (TDD)

- C1: unit tests — same-sector-different-unit stop within 250 m matches; >250 m does not;
  coords-missing falls back to sector compare; depot-area still requires sector precision.
- C2: shared-ness count and threshold boundary (4 vs 5 distinct orders).
- C3: substitute at shared endpoint ignored; substitute at unique endpoint accepted.
- C4: the WT254741 shape (powered==2, one leg confirmed) stays FULL_FLEET; a
  powered==1 solo-direct with the vehicle provably elsewhere demotes; an untracked
  single-vehicle booking stays FULL_FLEET.
- Regression: the existing `tests/planning_agent/test_verify_legs.py` suite stays green
  (adjust only assertions that encoded the old prefix-match behaviour, with justification).
