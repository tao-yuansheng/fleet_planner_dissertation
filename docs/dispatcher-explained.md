# How the ZEEFLEET Dispatcher Works — A Plain-Language Guide

This document explains the logistics dispatcher end-to-end: what problem it solves, how it thinks, and what it produces. Written for a non-technical audience, with just enough detail on the key variables and decisions to understand *why* it works the way it does.

---

## The Problem

Every day, you have two things:

- **Orders** — each order has an origin (pickup location), a destination (delivery location), a weight, a pallet count, and a time window the customer expects delivery within.
- **Vehicles** — each vehicle has a current GPS location, a weight capacity, a pallet capacity, and a cost profile (fuel rate, driver mileage rate).

The dispatcher's job is to answer: **which vehicle should handle which orders, and in what sequence, to minimise total fleet cost while meeting every deadline?**

This class of problem is formally called a **Pickup-and-Delivery Problem with Time Windows (PDPTW)**. It's notoriously hard to solve perfectly — the number of possible combinations explodes as orders and vehicles grow — so the dispatcher uses smart search algorithms to find a very good solution within a fixed time budget (300 seconds by default). It offers three: a fast **greedy** baseline, **Monte Carlo Tree Search (MCTS)**, and **Large Neighbourhood Search (LNS)**. All three are explained below, along with how they perform against each other.

---

## What a Route Actually Looks Like

### Consolidation

A single vehicle can carry **multiple orders at the same time**. This is called consolidation. A vehicle's route is therefore an interleaved list of pickups and deliveries, not a simple "pick up one thing, drop it off, repeat" sequence.

Example route for Vehicle A:

```
[Start position]
  → Pickup: Order 1   (load rises: +500 kg, +2 pallets)
  → Pickup: Order 2   (load rises: +300 kg, +1 pallet)
  → Delivery: Order 1 (load falls: -500 kg, -2 pallets)
  → Pickup: Order 3   (load rises: +400 kg, +2 pallets)
  → Delivery: Order 3 (load falls: -400 kg, -2 pallets)
  → Delivery: Order 2 (load falls: -300 kg, -1 pallet)
```

Key rules enforced at every stop:
- **Pickup before delivery**: you cannot deliver something you haven't picked up yet.
- **Capacity never exceeded**: weight on board and pallets on board must stay within the vehicle's limits at every single stop, not just on average.
- **Deadline met**: each delivery must arrive before the customer's time window closes.

### Key variables per stop

| Variable | What it means |
|---|---|
| `order_id` | Which order this stop belongs to |
| `lat`, `lon` | GPS coordinates of this stop |
| `stop_type` | `"pickup"` or `"delivery"` |
| `load_after` | Weight (kg) and pallets on board *after* this stop |
| `arrival_time` | Estimated arrival datetime, calculated from distance ÷ average speed |

---

## How Cost Is Calculated

Every possible route has a single cost formula:

```
Cost = Total miles driven × (fuel rate per mile + driver mileage rate per mile)
```

Distance between any two points is calculated using the **Haversine formula** — the straight-line distance over the Earth's surface, in kilometres, then converted to miles. This is a simplification (no road network), but consistent and fast.

The rates come from a config file (`vehicle_cost_rates.json`) keyed by vehicle asset type (e.g., rigid truck vs. artic). Every part of the system — the search, the comparison, the final output — uses this same formula. There is no separate cost model for planning vs. reporting.

---

## The Core Building Block: Trying to Insert One Order

Before we get to the full search, the key question the system asks repeatedly is:

> "I have a vehicle with a route already planned. What is the cheapest way to add one more order to it?"

This is called **cheapest insertion**, and it works like this:

1. Take the vehicle's current list of stops (say, 6 stops for 3 existing orders).
2. Try inserting the new order's **pickup** at every possible position in the list (positions 1 through 7).
3. For each pickup position, try inserting the new order's **delivery** at every valid position *after* the pickup (the delivery must come after its own pickup).
4. For each (pickup position, delivery position) combination:
   - Recompute the route cost with these two new stops included.
   - Check: does the load on board exceed vehicle capacity at any point? If yes, skip.
   - Check: does any delivery now miss its deadline? If yes, skip.
   - If both checks pass, record the **extra cost** this insertion adds: `new cost − old cost`.
5. Return the combination with the lowest extra cost, or `None` if no valid placement exists.

This is repeated for every vehicle in the fleet. The vehicle where the order can be inserted most cheaply (the `cheapest_insertion`) is the winning assignment.

---

## The Search: How MCTS Thinks

With one order and one vehicle, cheapest insertion is easy. With 50 orders and 10 vehicles, the problem is that assigning Order 1 cheaply now might make Orders 2–50 harder or more expensive later. A purely greedy approach misses this.

**Monte Carlo Tree Search (MCTS)** addresses this by exploring multiple possible assignment sequences and learning from them. Here is how it thinks:

### The Tree

Each **node** in the tree represents a partial state of the world:

- `assigned` — which orders have been committed to which vehicles so far
- `unassigned` — which orders still need to be placed
- `routes` — the current stop sequences for every vehicle

Each **branch** from a node represents one decision: "assign this order to this vehicle."

### The Loop (runs for 300 seconds (5 minutes))

**Step 1 — Select**: Walk down the tree from the root, at each level choosing the branch that looks most promising. "Most promising" balances two things:
- **Exploitation**: branches that have produced good results before
- **Exploration**: branches that haven't been tried much yet (they might be hiding something better)

This balance is controlled by a formula called **SP-UCB**, which takes into account how often a branch has been visited and how variable its results have been. Highly variable branches get extra exploration, because the variance might be hiding upside.

**Step 2 — Expand**: When we reach a node that hasn't been fully explored, try one new action (assign the next order to a vehicle we haven't tried at this position yet). The candidate actions are the **top-K cheapest insertions** for that order across all vehicles (K = 8 by default), plus a "skip" option if no vehicle can take it.

**Step 3 — Rollout (simulate)**: From the new node, quickly finish assigning all remaining orders using pure greedy cheapest insertion. This gives a complete assignment — every order either placed or marked unassignable. No more tree branching; just speed.

**Step 4 — Score**: Evaluate the complete assignment:

```
Score = −(total fleet route cost + penalty × number of unassigned orders)
```

The penalty per dropped order is £10,000 — deliberately much higher than any realistic single route cost — so the system strongly prefers assigning every order over saving a few pounds of fuel.

**Step 5 — Backpropagate**: Walk back up the tree, updating every ancestor node with the score we just got. This is how the tree "learns" — good rollouts improve the reputation of all the decisions that led to them.

**Step 6 — Remember the best**: If this complete rollout produced the best score seen so far, save it. After 300 seconds (5 minutes), return that best complete solution.

The tree search degrades gracefully: on a small day (20 orders), MCTS explores many branches and significantly improves on greedy. On a very large day (400 orders), each rollout takes longer, so fewer iterations happen — but the best rollout is still returned, which is at least as good as greedy quality.

---

## The Greedy Baseline

Alongside MCTS, a **greedy dispatcher** runs the same problem with a much simpler strategy: go through the orders one at a time (tightest deadline first) and assign each to whichever vehicle adds the least cost right now, with no look-ahead.

It's fast (completes in under a second) and often produces reasonable results. It exists so you can compare: how much better did MCTS do? On most days the answer is "meaningfully better." On very large days the gap narrows because MCTS doesn't have time to explore many branches.

Both dispatchers use identical cost calculations and feasibility checks, so the comparison is fair.

---

## A Third Strategy: Large Neighbourhood Search (LNS)

MCTS builds a plan one assignment decision at a time. **LNS works the opposite way**: it starts from a *complete* plan and improves it by repeatedly tearing out a piece and rebuilding it. This turns out to be the dispatcher's most effective strategy — and the answer to the large-day scaling problem.

### The destroy-and-repair loop

LNS begins from the greedy result (a complete, feasible plan) and then repeats three moves until the time budget runs out:

1. **Destroy** — remove a chunk of orders (20–30) from their current routes, leaving gaps.
2. **Repair** — reinsert those orders using the same cheapest-insertion logic, free to find better positions anywhere across the fleet.
3. **Accept or discard** — if the rebuilt plan is cheaper, keep it; otherwise throw it away and try again from the previous best.

Which orders to remove matters, so LNS alternates two strategies:
- **Worst-cost removal** — pull out the orders adding the most distance to their routes (the biggest detours — the best improvement opportunities).
- **Random removal** — pull out a random handful, to diversify and avoid getting stuck in a local rut.

### Why it scales where MCTS doesn't

Each MCTS iteration simulates the *entire* remaining problem to completion — that gets slower as the day grows. Each LNS iteration only re-routes the 20–30 orders it removed; the rest of the plan is left untouched and its cost reused. So LNS keeps making real progress on large days, exactly where MCTS stalls after one or two iterations.

LNS does need a complete starting plan, which is why the greedy result is always computed first — it's the floor LNS improves on, and LNS can never do worse than it.

---

## How the Three Algorithms Compare

All three optimise the identical objective (total fleet cost, plus the £10,000 penalty per unassigned order) using the same cost engine and feasibility checks, so the comparison is fair. Both test days below used the default 300-second budget.

**Smaller day — 79 orders:**

| Metric | Greedy | MCTS | LNS |
|---|---|---|---|
| Estimated cost | £5,972 | £5,525 | **£3,613** |
| vs greedy | — | −7.5% | **−39.5%** |
| Vehicles used | 8 | 11 | **5** |
| Distance | 13,611 km | 11,999 km | **7,405 km** |
| Iterations | — | 277 | 86 |

**Larger day — 482 orders:**

| Metric | Greedy | MCTS | LNS |
|---|---|---|---|
| Estimated cost | £23,661 | £23,661 | **£16,915** |
| vs greedy | — | 0.0% | **−28.5%** |
| Distance | 45,250 km | 45,250 km | **35,665 km** |
| Iterations | — | 2 | 13 |

Two clear conclusions:

- **LNS wins decisively at every scale tested.** On the small day it cut cost by nearly 40% and used 5 trucks instead of 8 — its destroy-and-repair moves consolidate orders far more aggressively than MCTS's assignment search. On the large day it saved 28.5% where MCTS saved nothing at all.
- **MCTS collapses on large days.** With 482 orders it managed only 2 iterations — it ran the greedy seed, tried one alternative, and ran out of time. Its result is identical to greedy, to the penny. (This is the scaling limitation explained in the next section.)

So **LNS is the recommended algorithm** for medium and large days. MCTS now mainly serves as a point of comparison.

> **A note on LNS speed.** Most of LNS's per-iteration cost was removed by two optimisations: caching each order's parsed deadline (so the deadline check inside cheapest-insertion stops re-parsing the same dates millions of times), and only re-checking the routes that actually changed in a given iteration instead of all of them. On the 482-order day this roughly tripled the iterations LNS completes in the same budget (from 5 to 13), and improved its result from −18% to −28.5% vs greedy. The remaining fixed cost is the greedy seed itself — about 70 seconds on 482 orders — which must finish before the improvement loop can begin.

---

## Rolling Windows: Staying Up to Date

The dispatcher doesn't run once and lock in the whole day. It runs **multiple times per day** — typically 3–4 windows (e.g., 6am, 10am, 2pm, 6pm).

Each re-run:

1. **Locks prior assignments** — orders already committed to drivers are not reshuffled. Their vehicle, route, and sequence are frozen.
2. **Projects vehicle positions** — each vehicle's start position is moved forward to the end of its committed work. If Vehicle A's last committed stop is a delivery in Manchester, the next planning window treats Manchester as Vehicle A's starting location. The truck is assumed to be empty after completing its committed work.
3. **Plans the new window** — the fresh batch of orders (new arrivals, orders in the next time window) is optimised against the current, updated fleet state.

This means the plan stays realistic throughout the day rather than becoming stale by lunchtime.

---

## Route Polish: Tidying Up Before Output

After MCTS returns its best assignment, each vehicle's stop sequence is passed through a **2-opt polish** step. This is a classic route-shortening technique: it looks for pairs of stops where swapping the order of a segment would reduce total distance.

The constraint: any swap that puts a delivery before its own pickup is rejected immediately (precedence rule).

Additionally, after polishing, the system re-checks capacity and deadlines on the polished route. If the polish accidentally creates a violation (rare, but possible on tight capacity days), the original sequence is kept instead.

---

## What the Output Looks Like

For each vehicle that has at least one stop:

```
Vehicle V001:
  Stop 1: Pickup  Order-A  [lat, lon]  load after: 500 kg / 2 pallets  arrive: 08:14
  Stop 2: Pickup  Order-B  [lat, lon]  load after: 800 kg / 3 pallets  arrive: 09:02
  Stop 3: Delivery Order-A [lat, lon]  load after: 300 kg / 1 pallet   arrive: 10:45
  Stop 4: Delivery Order-B [lat, lon]  load after: 0 kg   / 0 pallets  arrive: 11:30
  Total distance: 187.4 km
  Estimated cost: £94.20
```

Plus a summary across the whole fleet:
- Total orders assigned / dropped
- Total fleet cost (£)
- MCTS iterations completed
- Time taken (seconds)

---

## Known Limitation: MCTS Scaling on Large Days

MCTS works by running many simulations (rollouts) and learning from them. Each rollout completes all remaining unassigned orders using greedy insertion — and that step gets slower as the day gets bigger, because there are more orders to try against more vehicles.

The cost of one rollout grows roughly as **O(n²)**: doubling the number of orders roughly quadruples the time per rollout. This means the 300-second budget buys very different amounts of exploration depending on the day size:

| Day size | Approx. time per rollout | Iterations in 300s | Result |
|---|---|---|---|
| ~80 orders | ~1 second | ~280 | MCTS meaningfully beats greedy (~7%) |
| ~150 orders | ~10 seconds | ~30 | MCTS somewhat better |
| ~300 orders | ~60 seconds | ~5 | Small improvement |
| ~480 orders | ~150 seconds | ~2 | MCTS = greedy (no exploration) |

On a large day, MCTS runs the greedy seed, attempts one tree expansion, and time runs out. The result is identical to the greedy baseline — not because the algorithm is wrong, but because it never had enough time to explore alternatives.

### The real fix: use LNS instead

This limitation is specific to MCTS. **LNS does not have it** — its iterations stay cheap regardless of day size, so it keeps improving the plan where MCTS stalls. On the same 482-order day, LNS beat the greedy/MCTS result by 28.5% (see *How the Three Algorithms Compare* above). For any medium or large day, run LNS rather than relying on MCTS.

### Optional: increase the budget

Either algorithm benefits from a bigger `--budget`, which is practical when the dispatcher runs as a scheduled overnight batch job where time is not the constraint. Run all three side by side with `--compare`:

```powershell
# 30-minute budget
python run_daily_batch.py --date 2026-01-06 --window-start 00:00 --fresh --compare --budget 1800

# 1-hour budget
python run_daily_batch.py --date 2026-01-06 --window-start 00:00 --fresh --compare --budget 3600
```

For MCTS this still buys only limited extra exploration on a large day. For LNS it directly translates into more destroy-and-repair iterations and a lower-cost plan.

---

## Future Improvements

> **Note:** Large Neighbourhood Search (LNS), previously listed here as the top future improvement, is now **implemented** — see *A Third Strategy: Large Neighbourhood Search (LNS)* and the comparison results above. The items below remain open.

### 1. Real Road Distances

The current cost model uses straight-line (Haversine) distances between stops. This is consistent and fast, but underestimates actual driving distances — especially in urban areas, around geographic features, or across routes that require motorway detours. Integrating a routing API (e.g. OSRM, Google Maps Distance Matrix, or HERE) would make cost estimates and arrival times much more accurate. The cost formula itself would not change — just the distance input.

### 2. Actual Road Speed Profiles

Arrival times are currently estimated using a flat average speed (50 km/h). In practice, speeds vary by road type, time of day, and congestion. A speed profile — or better, live traffic data — would improve arrival time accuracy and make deadline feasibility checks more realistic.

### 3. Multi-Depot Support

Currently, all vehicles are assumed to start from their last GPS position and have no home depot to return to. A multi-depot model would allow vehicles to be dispatched from specific depots, and optionally required to return there at the end of their shift. This matters for planning driver hours and overnight vehicle positioning.

### 4. Driver Hours and Shift Constraints

The dispatcher currently only enforces delivery deadlines — it does not track how long a driver has been on shift or whether a route would exceed legal driving hours. Adding a maximum shift duration per vehicle (e.g. 9 hours) as a hard constraint would make the output legally compliant and operationally realistic.

### 5. Partial Day Re-Optimisation with Order Priorities

The rolling-window system locks prior assignments and plans the next window. A future enhancement would allow re-evaluating locked assignments if a high-priority order arrives that cannot feasibly fit into any free vehicle — triggering a re-plan of a limited scope rather than a full reshuffle.

---

## What This Dispatcher Covers — and What It Doesn't

This section is a plain-language map of the dispatcher's scope relative to the full Rolling Dispatch planning requirement. It is intended to help anyone reading this understand where the engine is genuinely useful today and where the gaps are.

### What is built and working

| Capability | Detail |
|---|---|
| Pickup-and-delivery routing | Each order has an origin (pickup) and a destination (delivery); routes are interleaved sequences of both |
| Hard time windows | Every delivery must arrive before the customer's deadline — enforced as a hard constraint, not a preference |
| Weight and pallet capacity | The engine never builds a route that exceeds a vehicle's weight or pallet limit at any point along the journey |
| Multi-stop consolidation | A single vehicle can carry multiple orders simultaneously; load rises at pickups, falls at deliveries |
| Cost minimisation | Routes are scored by miles driven × (fuel rate + driver mileage rate); rates are keyed per vehicle asset type |
| Intra-day rolling windows | The dispatcher can be re-run 3–4 times per day; committed assignments are locked, vehicle positions are projected forward, and new orders are planned against the updated fleet state |
| Three optimisation strategies | Greedy (instant baseline), MCTS (assignment-tree search), and LNS (destroy-and-repair) all run on the same cost model; `--compare` runs all three side by side. LNS is strongest, winning at every scale tested |

### What is not built

**Vehicle and order compatibility**

The dispatcher treats all vehicles as interchangeable beyond weight and pallets. There is no concept of vehicle capability types (refrigerated, tail-lift, ADR hazardous goods, etc.), so the engine cannot enforce that a chilled order only goes on a fridge vehicle, or that a hazardous load requires an ADR-trained driver. Any such constraint must currently be handled upstream before orders reach the dispatcher.

**Driver hours and shift compliance**

The engine enforces delivery deadlines but has no awareness of how long a driver has been on shift. It will produce routes that technically meet all customer time windows but could breach EU drivers' hours regulations (4.5-hour driving limit, 9-hour daily maximum, mandatory break requirements). Routes must be audited for compliance externally.

**Multi-day planning horizon**

Each planning run is self-contained to the current day. The dispatcher has no visibility of orders accepted for Wednesday when planning Tuesday, and cannot make Tuesday's plan account for Wednesday's commitments. A multi-day rolling horizon — where accepting an order today is assessed against future capacity — does not exist.

**Revenue, margin, and pricing**

The objective function is cost only. The dispatcher has no awareness of what a job pays, so it cannot optimise margin, avoid unprofitable lanes, or surface a rate floor below which a job should be declined. The "accept or decline" and counter-pricing layer is entirely absent.

**Disruption replanning**

If a vehicle breaks down, a driver calls in sick, or a customer cancels mid-day, the dispatcher has no event-triggered replanning capability. A user can manually re-run the tool and the rolling-window locking will respect committed work — but the system does not detect the disruption or initiate a replan automatically.

**Backloading and depot return**

Vehicles are assumed to start from their last GPS position and finish wherever their last delivery lands. There is no concept of a home depot to return to, no backload optimisation, and no overnight vehicle positioning logic.

**Decision-support and planner interaction**

There is no natural-language interface, no explanation of why a specific order was assigned to a specific vehicle, no counterfactual ("what if vehicle 14 is unavailable?"), and no planner-in-the-loop override beyond manually editing the committed assignments list before re-running.

**External integrations**

The dispatcher reads from flat JSON input files. There is no live connection to a TMS (e.g. Qargo), telematics systems (Optifleet, Supatrak), fuel cost feeds (Jigsaw), or driver-hours data (Clockwatcher).

**Visualisation**

All output is structured JSON and a printed summary. There is no Gantt view, no map, no margin overlay, and no constraint-violation surfacing in a UI.

---

### Scale ceiling

At around 400+ orders the **MCTS** search degrades to greedy quality because each rollout takes too long to fit multiple iterations in the time budget. **LNS** does not have this problem and is now the recommended algorithm at scale — on a 482-order day it cut cost 28.5% below the greedy/MCTS result. A 500-order × 80-vehicle operation — the scale described in the Rolling Dispatch requirement — is therefore within reach using LNS, though two caveats remain: the greedy seed alone takes ~70 seconds on ~480 orders before LNS's improvement loop begins, and there is no problem decomposition, so the very largest days still benefit from a larger overnight budget.

---

## Summary: The Flow in One Paragraph

The dispatcher takes a list of orders and a fleet of vehicles. It anchors its planning clock to the earliest order time window. It tries to insert each order — tightest deadline first — into every vehicle's route, checking weight, pallets, and deadlines at every candidate position. It offers three search strategies on top of this: a fast greedy baseline; MCTS, which explores the best-looking combinations of insertions over the time budget; and LNS, which starts from the greedy plan and repeatedly destroys and repairs slices of it. Whichever runs, each vehicle's route is then polished for distance efficiency with a feasibility check, and the final plan — with exact stop sequences, load profiles, arrival times, and costs — is emitted as the dispatch output. Across both a small day (79 orders) and a large day (482 orders), **LNS produced the cheapest plans** — 40% and 28% below greedy respectively — while MCTS collapses to greedy quality once the day grows past ~400 orders. LNS is the recommended choice for medium and large days; run `--compare` to see all three head to head.
