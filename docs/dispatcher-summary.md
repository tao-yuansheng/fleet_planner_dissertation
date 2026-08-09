# ZEEFLEET Dispatcher — Condensed Summary

A short companion to `dispatcher-explained.md`. It covers what was built, why LNS beats MCTS, where the project stands, and what comes next.

---

## What was built

A dispatcher that assigns a day's **orders** (pickup → delivery, with weight, pallets, and a delivery deadline) to a **fleet** of vehicles (each with a position, capacity, and cost rate), minimising total fleet cost while meeting every deadline. This is a Pickup-and-Delivery Problem with Time Windows (PDPTW).

One shared cost engine underpins everything: route cost = miles driven × (fuel rate + driver rate), distances via the Haversine formula. Capacity and deadlines are checked at every stop. Three algorithms plug into this engine:

- **Greedy** — assign each order (tightest deadline first) to whichever vehicle adds least cost right now. Instant, no look-ahead. Used as the baseline and as the starting point for the other two.
- **MCTS** (Monte Carlo Tree Search) — explores a tree of assignment decisions, using fast greedy "rollouts" to score each branch.
- **LNS** (Large Neighbourhood Search) — starts from the greedy plan and repeatedly **destroys** a slice (removes 20–30 orders) and **repairs** it (reinserts them better), keeping any improvement.

All three run on the identical objective, so results are directly comparable. `run_daily_batch.py --compare` runs all three, saves the cheapest plan to `dispatch_<date>.json`, and writes a `comparison_<date>.json` summary.

---

## Why MCTS works — but LNS works better

**MCTS works** because it looks ahead: assigning an order cheaply now can make later orders expensive, and the tree search learns which assignment sequences pay off overall. On small days it beats greedy.

**But it doesn't scale.** Every MCTS iteration runs a full greedy rollout over all remaining orders — an O(n²) operation. As the day grows, each iteration gets slower, so fewer fit in the time budget. On a 482-order day it completes only ~2 iterations and collapses to greedy quality.

**LNS wins** because each iteration only re-routes the handful of orders it removed — cheap, and the cost doesn't grow with day size. It also consolidates far more aggressively (its destroy/repair moves find shared trips that the assignment tree misses). Measured at a 300-second budget:

| Day | Greedy | MCTS | LNS |
|---|---|---|---|
| 79 orders | £5,972 | £5,525 (−7.5%) | **£3,613 (−39.5%)** |
| 482 orders | £23,661 | £23,661 (0.0%) | **£16,915 (−28.5%)** |

LNS won at every scale tested, using fewer trucks and less distance.

---

## Current state

- **Working and verified:** PDPTW routing, hard time windows, weight/pallet capacity, multi-stop consolidation, intra-day rolling windows (lock committed work, project vehicle positions, replan new orders), and all three algorithms with side-by-side comparison.
- **Output is persisted:** the best plan is saved per day, with a comparison summary; 41 tests passing.
- **Recommended use:** LNS for medium and large days; MCTS now mainly a comparison point.

---

## Limitations

- **Scale ceiling.** The greedy seed alone takes ~70s on ~480 orders before LNS's improvement loop begins, and there is no problem decomposition — the very largest days still need a bigger overnight budget.
- **Cost-only objective.** No revenue or margin awareness; can't price, accept/decline, or avoid unprofitable lanes.
- **Single-day horizon.** No multi-day planning; today's plan ignores commitments later in the week.
- **No vehicle/driver constraints beyond capacity.** No fridge/ADR/tail-lift matching, no EU driver-hours or shift limits.
- **No live integration.** Reads flat JSON; no TMS (Qargo), telematics, or fuel/driver-hours feeds.
- **Straight-line distances.** Haversine underestimates real road distance; arrival times use a flat 50 km/h.
- **No disruption replanning, no UI/visualisation, no explainability layer.**

---

## Future steps

Now that the core engine is solid, the next layers are:

1. **Speed up the inner loop further** — incremental distance deltas in the insertion routine would cut both the greedy seed and every LNS repair, unlocking more iterations and larger days.
2. **Real road distances and speeds** — integrate a routing API (OSRM / Google / HERE) for accurate cost and arrival times.
3. **Hard vehicle/driver constraints** — capability matching (fridge, ADR, tail-lift) and EU driver-hours / shift limits.
4. **Revenue and pricing layer** — margin-aware objective, rate floors, accept/decline recommendations.
5. **Multi-day horizon and disruption replanning** — plan across the week; re-plan automatically on breakdowns or cancellations.
6. **Decision support** — explainability, counterfactuals ("what if vehicle 14 is out?"), planner overrides, and visualisation (Gantt, margin map).
7. **Integration** — connect to Qargo (TMS) and telematics for live orders, positions, and plan write-back.
