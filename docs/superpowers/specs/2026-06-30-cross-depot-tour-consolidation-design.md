# Cross-depot tour consolidation — design

- Date: 2026-06-30
- Status: design approved, awaiting spec review → implementation plan
- Component: `freight_planner` multi-day tour layer (`tour_plan.py`, `tours.py`)

## Problem

Multi-day "far" work (e.g. Scotland deliveries) is batched into tours **per source
depot**: [`tour_plan.py`](../../../freight_planner/tour_plan.py) does
`for depot, rjobs in buckets.items(): build_tours(rjobs, _proto_vehicle(depot, ...))`.
So freight bound for the same region but owned by different depots is never
considered together.

Concrete case on window 2026-01-12→17:
- `TOUR:X888RNW:2026-01-13` (Bedford) sweeps central Scotland + NE England, **01-13→01-16**.
- `TOUR:W88RNW:2026-01-15` (CB22) delivers Ayr + Kilmarnock, **01-15**, a ~1,000 km round trip.

On 01-15 X888RNW is in Airdrie→Dumfries — **~45–70 km from Ayr/Kilmarnock**, with
~18 pallets of spare capacity. The two runs overlap in region, date, and have the
headroom to merge, yet they are two separate ~1,000 km trips. The blocker is the
per-depot bucketing, **not** date and **not** capacity. The waste here is ~800–900 km
plus a vehicle-day — on one example.

## Goal & constraints

- **Objective: both km and coverage.** Consolidating overlapping far tours cuts km and
  vehicle-days, and frees tour vehicles for the far work that currently fails
  `NO_FEASIBLE_TOUR`.
- **Constraint — "already-at-depot" only.** A consolidated tour may **load at depots**
  but must **not** make one vehicle collect across multiple depots' *customer*
  territories. So the **consolidation scope** is **depot-staged** freight (deliveries
  whose freight already sits at a depot — trunked-in imports, or collected by a daily op
  and staged).
- **Emergent regions, not a hardcoded postcode table.** Clustering must come from the
  actual geography of the jobs, reusing the cohesion mechanism `build_tours` already has.
- **Behind a flag** (default off) for clean A/B and risk-free revert.

## Approach (chosen)

**Region-pool + multi-depot load.** Replace per-depot bucketing with a single
cross-depot pool, cluster once by cohesion (emergent regions), and for clusters whose
freight spans depots, load at those depots in passing.

Rejected alternatives: *pre-consolidation transfer* (extra depot→depot move + double
handling), *post-build merge pass* (greedy, order-dependent, fiddly merge bookkeeping).

## Design

### 1. Core mechanism — pool, cluster once, multi-depot load

Drop the per-depot split. Pool all far tour jobs into one list and cluster them once
(consolidation is then gated per-cluster in section 3 — the pool is everything; only
depot-loadable multi-depot clusters actually consolidate):

```
pooled = [all far tour jobs, any depot]
for tour in build_tours(pooled, _proto_vehicle(<clustering anchor>, ...)):
    load_depots = { job.source_depot for job in tour.jobs }
    ... resolve per cluster (section 3) -> multi-depot load + vehicle (section 2)
```

- **Emergent regions.** `build_tours` already clusters by *inter-job distance* (seed the
  farthest job, grow by `_min_gap_km ≤ cohesion_km`, bounded by `max_span_days` and
  feasibility). Running it once over the pooled list yields a Scotland cluster, a Wales
  cluster, etc., from the data alone — no postcode→region table.
- **Clustering anchor (concrete).** The proto anchor only selects the *seed* (the farthest
  job) and is the depot→region→depot reference for the cohesion feasibility test. Use the
  **centroid of `DEPOT_ANCHORS`** (the SE depots). Because every depot is in the SE ~40 km
  apart while the work is hundreds of km out, the choice is immaterial to which clusters
  form; the centroid is a deterministic, hardcoding-free default. (The *real* per-cluster
  route anchor is resolved separately in section 2.)
- **Multi-depot load = the cluster's own freight locations.** A tour's `load_depots` is
  the distinct `source_depot`s of its jobs. Single-depot clusters are identical to today
  (no regression). Multi-depot clusters load at those depots, never at customers.

### 2. Multi-depot route, vehicle, feasibility

- **Route shape.** Primary depot = the holding depot with the most of the cluster's
  freight **by pallet volume** (ties → most kg, then depot id, for determinism). Route =
  *primary (start, load) → load-stop(s) at the other holding depot(s) →
  regional sweep → return to primary*. Load-stops sit at the **front** (an SE depot
  milk-run, ~40 km) before any delivery — valid because every delivery is far/north and
  every load-depot is SE ("load everything, then head north"). Return-to-primary matches
  today; nearest-depot return is a later optimisation.
- **Feasibility mostly reused.** The vehicle is fully loaded once it leaves the last
  load-stop, so **peak load = sum of the cluster's deliveries** — exactly what
  `evaluate_tour` already checks against capacity. The only change to `evaluate_tour` is
  *including the front load-stop hops in the route walk* (their km); the capacity peak,
  day-cap split, due-date dwelling, `cohesion_km`, and `max_span_days` are untouched.
- **Vehicle selection.** `select_tour_vehicle` runs on the combined
  `total_pallets`/`total_kg` (already summed over the cluster), preferring a vehicle at
  the primary depot, reserving it across the span. If a combined cluster exceeds one
  vehicle, `build_tours` never grew it that big (it tests feasibility on the 26-pal proto
  as it grows) — clusters are self-limited to one-vehicle size.

### 3. Eligibility / per-cluster resolution (tight first build)

Pool **all** far tour jobs (the same set today's per-depot `build_tours` receives —
deliveries, regional pickups, and DIRECT moves) so clustering is identical to today
for any single region. Then resolve each emergent cluster by its `load_depots`:

- **Single-depot cluster** (all jobs share one `source_depot`): build exactly as today,
  anchored at that depot. **Byte-identical** to current behaviour — this is what keeps it
  regression-safe.
- **Multi-depot cluster, all cross-depot freight depot-loadable** (deliveries + regional
  pickups; *no* DIRECT moves): **consolidate** into one multi-depot tour (section 2).
- **Multi-depot cluster containing a far DIRECT move** (would need cross-territory
  *customer* collection, Stoke-style): **do not consolidate** — fall back to building its
  jobs per `source_depot`, as today. Broadening to DIRECT is an explicit later step.

So the only *new* routing is the middle case; the first and third reproduce today's plan.

## Testing (TDD, tour layer, no OSRM)

1. Two depots' deliveries in one region within the date span **merge into one
   multi-depot tour**, loading at both depots.
2. A single-depot cluster is **byte-identical** to today (no regression).
3. A cluster whose combined load exceeds one vehicle does **not** over-merge
   (`build_tours` self-limits).
4. A multi-depot cluster carrying cross-territory DIRECT collection is **not**
   consolidated (falls back to per-depot).
5. `evaluate_tour` counts the load-stop km and the capacity peak correctly.

## Measurement

A/B at equal ALNS budget on 12-17, flag off vs on, judged on the **final plan**:
- total km (expect ↓ — the redundant Scotland round-trip),
- tour vehicle-days (↓),
- coverage (= or ↑ as far vehicles free up),
- sanity: the Scotland pair becomes one tour,
- 0 temporal / 0 ledger violations.

## Out of scope / future

- DIRECT (customer-collect) far moves across territories.
- Nearest-depot (vs primary) return optimisation.
- Dynamic anchor / true spatial clustering beyond the cohesion heuristic.

## Files expected to change

- `freight_planner/tour_plan.py` — pooling + multi-depot load resolution + flag.
- `freight_planner/tours.py` — `build_tours`/`evaluate_tour` load-stop walk; possibly
  `select_tour_vehicle` prefer-depot for multi-depot clusters.
- `freight_planner/run_alns.py` — `--consolidate-tours` flag wiring.
- tests under `tests/freight_planner/` (tour layer).
