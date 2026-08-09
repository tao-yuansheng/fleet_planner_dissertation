# Phase 1 — Strategic Planner Design Options

**Status:** Design — decision pending  
**Date:** 2026-06  
**See also:** `architecture-two-phase.md`, `data-contract.md`

---

## What Phase 1 must produce

Given a 4–7 day order list and a fleet roster, Phase 1 must:

1. Classify every order as LOCAL, TOUR, or TRUNK (see `data-contract.md`)
2. Cluster TOUR orders into multi-day vehicle tours by region and delivery sequence
3. Assign a vehicle to each tour (respecting capacity and availability)
4. Allocate remaining local vehicles to each depot for each day
5. Output a `WeeklyPlan` consumed by Phase 2 each morning

The hard sub-problem is step 2–3: deciding which vehicle takes which set of remote orders and in what sequence over multiple days. This is the part that varies by model choice.

---

## Fleet and order context (from backtest analysis)

From the Jan 6–8 backtest:
- **~300 orders/day**, ~1,800–2,100 orders over 7 days
- **~60 vehicles** (CB22 rigids ×17, CB22 articulateds ×20+, Bedford rigids ×12, Bedford articulateds ×8, St Ives ×3)
- **TOUR orders**: roughly 10–20% of total (EX, TR, PL, BA, CF, LL, DG, LA, IV orders)
  - In Jan 6–8 data: ~8 vehicles were on remote tours on any given day
- **LOCAL orders**: ~75–80% — handled by local rigids within ~60–120 km of depot
- **TRUNK orders**: ~10–15% — PL_EXPORT to B37, handled by existing trunk_planner

The TOUR sub-problem is: ~150–300 remote orders over 7 days, assigned to ~15–20 artic slots, packed into tours of 1–3 days each. This is manageable in size.

---

## Option A — Greedy region-clustering + capacity bin-packing

### How it works

```
1. Filter: extract all TOUR-class orders for the week
2. Cluster: group by geographic region
   (e.g. SW_ENGLAND = {EX, TR, PL, BA, TA}, NW_ENGLAND = {LA, WN, OL, BB, ...})
3. Pack: for each region-cluster, greedily fill tours
   - Sort orders by delivery_date ASC
   - Fill a tour vehicle up to capacity (pallets/weight)
   - If next order is >1 day away or in wrong direction, close tour and open new one
4. Assign: match each tour to an available artic
   - Prefer vehicles whose home depot is closest to the tour's start region
5. Allocate: remaining vehicles + LOCAL orders → DepotDayBudget per depot per day
```

### Pros
- Simple to implement and reason about (~200 lines of new code)
- Transparent — every decision is traceable to a rule
- Fast — runs in seconds
- Matches what a human dispatcher does intuitively

### Cons
- Suboptimal — greedy packing misses cross-region tours (e.g. a vehicle going SW could pick up Dorset orders on the way)
- Region boundaries are fixed — needs manual tuning per operational area
- No global capacity balancing — a busy day might starve one depot while another has slack

### Verdict
Good enough for a first working implementation. Gets all TOUR orders assigned. Fix quality issues in a second pass with ALNS.

---

## Option B — Extended VRPTW over full week horizon

### How it works

Treat the 7-day planning period as a single extended time window. Each vehicle has 7 × 24h of available time. The existing ALNS VRPTW solver assigns orders to vehicle-days naturally, with overnight stops modelled as long stop durations.

```
Time horizon: 0h = Monday 00:00, 168h = Sunday 24:00
Each order has: release_time (earliest pickup), delivery deadline (end of delivery day)
Each vehicle has: capacity, home depot (start and end position)
Overnight constraint: vehicle must be stopped 00:00–06:00 at some location each night
ALNS optimises total distance / cost across the full week
```

### Pros
- Globally optimal within ALNS quality
- Naturally handles cross-region tours and shared legs
- No manual region definitions

### Cons
- Problem size: 7× the daily problem. ~2,100 orders × 60 vehicles × 7 days = very large state space
- Overnight constraints require significant solver extension
- ALNS already takes 300s on a single-day ~300-order problem — week-level may be intractable in reasonable time
- Multi-day vehicle trajectories complicate the feasibility check logic substantially

### Verdict
Academically ideal but likely too slow for practical use without significant decomposition. Worth exploring if Phase 1 quality becomes the binding constraint, but not the starting point.

---

## Option C — Column generation / set partitioning

### How it works

A classic VRP decomposition technique:

```
Master problem (set partitioning LP):
  - Decision variable: select subset of candidate tours that covers all orders
  - Constraint: each order covered by exactly one tour
  - Objective: minimise total tour cost

Pricing subproblem (per vehicle):
  - Generate new candidate tours (columns) that improve the master LP objective
  - Solved as a resource-constrained shortest path problem

Solve iteratively: generate columns, solve LP, generate more columns, ...
```

### Pros
- Near-optimal solution quality
- Well-studied in operations research literature — strong academic framing
- Handles heterogeneous fleet naturally

### Cons
- Significant implementation complexity (~1,500–2,000 lines)
- Requires LP solver (PuLP, Gurobi, or similar)
- Pricing subproblem is itself NP-hard without further approximation
- Overkill for a fleet of 60 vehicles with ~200 TOUR orders per week

### Verdict
Not appropriate for the project timescale. The academic contribution is in applying this well to a real dataset, not in building the solver from scratch. Mention as a "future direction" in write-up.

---

## Option D — Two-stage heuristic with ALNS refinement (recommended)

### How it works

Stage 1 (fast, rule-based): identical to Option A — classify, cluster by region, greedily pack tours. Output is a `WeeklyPlan` that assigns all TOUR orders to tours.

Stage 2 (optimisation): run a lightweight ALNS over the TOUR assignments only.

```
ALNS destroy operators:
  - Remove a random tour, return its orders to an unassigned pool
  - Split a long tour into two if a vehicle is over-utilised
  - Merge two short tours to a single vehicle if capacity allows

ALNS repair operators:
  - Cheapest insertion: find the best tour+position for each unassigned order
  - Regret insertion: insert the order whose second-best option is most expensive
  - Region sweep: re-cluster orders by geographic proximity and rebuild tours

Objective: minimise total TOUR km (sum of all multi-day tour distances)
Budget: 60–120 seconds (only ~200 TOUR orders, much smaller than daily VRPTW)
```

Stage 3 (local orders): once TOUR vehicles are committed, remaining vehicles and LOCAL orders flow directly to Phase 2. Phase 2's existing ALNS handles the local routing optimally per depot per day — no change needed there.

### Why this is the right choice

- **Reuses existing ALNS infrastructure** — the destroy/repair pattern is already implemented in `simulation/vrptw_alns.py`; the tour builder just needs different feasibility checks (multi-day vs. single-day)
- **Separates the two problems cleanly** — TOUR optimisation and LOCAL optimisation have different cost structures and shouldn't compete in the same solver
- **Fast enough to run nightly** — Stage 1 is instant, Stage 2 runs on ~200 orders in under 2 minutes
- **Incrementally improvable** — start with Stage 1 only, add Stage 2 as a quality improvement

### Implementation plan

```
cambridge/week_planner/
├── order_classifier.py     # assign order_class + release_time to each ScopedOrder
├── region_map.py           # postcode prefix → region label (SW_ENGLAND etc.)
├── tour_builder.py         # Stage 1: greedy region-cluster → Tour objects
├── tour_alns.py            # Stage 2: ALNS over Tour assignments
├── capacity_allocator.py   # Stage 3: remaining vehicles → DepotDayBudget per day
└── weekly_plan.py          # WeeklyPlan dataclass + JSON serialisation
```

---

## Region map (initial)

Derived from the Jan 6–8 actual fleet analysis. Refinement expected from production data.

| Region label | Postcode prefixes | Typical tour length | Typical vehicle |
|---|---|---|---|
| `SW_ENGLAND` | EX, TR, PL, TQ, TA, BA, DT, BH | 2–3 days | CB22 or BED artic |
| `WALES` | CF, SA, LL, SY, NP, HR, LD | 1–2 days | CB22 or BED artic |
| `NW_ENGLAND` | LA, WN, OL, BB, PR, BL, FY, WA, CW | 1–2 days | CB22 or BED artic |
| `SCOTLAND` | DG, KA, G, EH, ML, KY, FK, PH, IV | 2–3 days | CB22 or BED artic |
| `NE_ENGLAND` | DH, NE, SR, TS, DL | 1–2 days | BED artic preferred |
| `YORKSHIRE` | HU, DN, YO, LS, WF, BD, HX | 1 day | BED artic preferred |
| `MIDLANDS_W` | ST, WV, WS, B, DY, CV | 1 day | either |
| `LONDON_SW` | SW, TW, KT, SM, CR, RH, GU | 1 day | CB22 artic preferred |
| `LONDON_SE` | SE, DA, BR, ME, TN, CT | 1 day | CB22 artic preferred |

Orders not matching any region → OVERFLOW, handled by existing `_pre_assign_overflow()`.

---

## TOUR_THRESHOLD calibration

The boundary between LOCAL and TOUR (currently proposed at ~4h one-way drive):

```python
TOUR_THRESHOLD_SECONDS = 4 * 3600   # 4h one-way via OSRM

# This means:
# CB22 → EX4 (Exeter)    ≈ 3h 20min → borderline; needs overnight if delivering multiple
# CB22 → TR18 (Truro)    ≈ 4h 30min → TOUR
# CB22 → SO14 (Soton)    ≈ 2h 10min → LOCAL
# CB22 → WN8 (Wigan)     ≈ 3h 00min → borderline
# CB22 → G33 (Glasgow)   ≈ 7h 00min → TOUR (2-3 day)
```

The threshold is per-vehicle-type: articulateds have lower thresholds than rigids (capacity forces multi-drop, increasing time on-site). A sensible starting point:

| Vehicle type | TOUR threshold |
|---|---|
| Rigid | 3h one-way (rigids rarely do 4h one-way and return same day) |
| Artic | 4h one-way |

---

## Decision summary

| | Option A | Option B | Option C | Option D |
|---|---|---|---|---|
| Implementation effort | Low | Very high | Very high | Medium |
| Solution quality | Moderate | High | Very high | High |
| Runtime (7-day plan) | <5s | Hours | 10–30min | 2–3min |
| Reuses existing ALNS | No | Yes (extended) | No | Yes |
| Academic rigour | Low | High | Very high | Medium-High |
| Practical for project | ✓ | ✗ | ✗ | ✓✓ |

**Recommendation: Option D.**  
Build Option A first (greedy tour builder, no ALNS) to get the data contract working end-to-end. Add the ALNS refinement pass once Phase 2 integration is confirmed working. This gives a working system fast and a clear improvement path.
