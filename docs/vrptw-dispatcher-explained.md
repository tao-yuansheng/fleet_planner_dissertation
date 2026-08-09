# The VRPTW Dispatcher — A Plain-Language Guide

This document explains the **VRPTW dispatcher agent** that lives in `simulation/` (`vrptw_engine.py`, `vrptw_alns.py`, `rolling_dispatcher.py`). It is written for a non-technical reader and uses simple ASCII diagrams.

> ⚠️ **Scope and honesty note.** This dispatcher is a work in progress. It models a real chunk of ZEEFLEET's daily delivery operation, but it does **not** yet capture the whole picture (cross-depot trunking, driver-hours law, time-window enforcement, live integrations). Treat the numbers it produces as **planning estimates inside a model**, not as a production schedule. The known gaps are listed at the bottom.

---

## What problem is this thing trying to solve?

ZEEFLEET runs a **groupage network** — small consignments from many customers are funnelled through three depots (Duxford, Bedford, St Ives), bundled together, and delivered out to end customers on shared local-area trucks.

Picture the day like this:

```
            morning collections                 afternoon deliveries
                  ───────►                            ───────►

       customer                  ┌─────────┐                     customer
       customer ───collect──►    │         │   ──pre-load──►     customer
       customer                  │  DEPOT  │   ─load on truck─►  customer
       customer ───collect──►    │         │   ──truck leaves─►  customer
                                 └─────────┘
                                     ▲                              │
                                     │                              │
                                     └──────── truck returns ───────┘
```

The dispatcher's job is the **afternoon-out half**: given a list of orders that need delivering today and a fleet of trucks parked at each depot, decide

- which truck delivers which orders, and
- in what order along the route,

so total fleet cost is as low as possible — and every order gets delivered if possible.

That class of problem is called **VRPTW** — Vehicle Routing Problem with Time Windows. "VRP" because we are routing vehicles. "TW" because (in principle) each order has a delivery time window, and (in practice) each vehicle has a shift end it must return to the depot before.

---

## How VRPTW differs from the older PDPTW model

There is a sibling document, `dispatcher-explained.md`, that covers a **PDPTW** (Pickup-and-Delivery) dispatcher. Same family of problem, but a different shape:

| | **PDPTW** (`mcts_dispatcher.py`) | **VRPTW** (this doc) |
|---|---|---|
| Where does the truck start? | Wherever its GPS last pinged | At its **home depot** |
| Where does it finish? | Wherever the last delivery is | Back at its **home depot** (closed loop) |
| Order = | A *pickup* + a *delivery* the truck must do in order | A single *delivery* — goods are pre-loaded at the depot |
| Best for | One-off point-to-point jobs | Groupage / hub-and-spoke local delivery |

In simple terms: PDPTW thinks of trucks as taxis that pick up and drop off as they go. VRPTW thinks of trucks as **delivery vans loaded at a warehouse each morning, doing a loop, coming home**.

VRPTW matches ZEEFLEET's real operation much more closely. That is why this newer dispatcher exists.

---

## The three order types

Not every order looks the same. The dispatcher first classifies each order by *how the goods get to the depot in the first place*:

```
PRE_STAGED   the goods are already at the depot at 06:00
             ───► ready to go on the first wave out

VIA_DEPOT    the goods need collecting from a customer first
             ───► collect → return to depot → THEN onto delivery
             ───► ready time = when collection truck got back + 30 min buffer

DIRECT       the order is one-shot point-to-point, no groupage
             ───► one dedicated truck does depot → pickup → drop → depot
             ───► computed separately, not fed to the batch solver
             ───► ~1.7% of orders
```

Why this matters: the dispatcher can't plan a delivery until the goods exist at the depot. So **freight arrival time becomes the start signal** for each order entering the planning pool.

---

## How a single route looks

For a regular VIA_DEPOT or PRE_STAGED order, the truck's route is a closed loop:

```
                 ┌─ stop 1 (deliver order A)
                 │
                 │     stop 2 (deliver order B)
                 │       │
   DEPOT ────────┘       │           stop 3 (deliver order C)
     ▲                   │              │
     │                   └──────────────┘
     │                                  │
     └──────── back to DEPOT ───────────┘
```

Key rules the dispatcher enforces:

- **Capacity never exceeded** — total weight loaded at the depot ≤ truck weight limit; same for pallets. (Because everything is loaded upfront, the "peak load" is just the sum of every stop.)
- **Back to depot before shift ends** — driving time + a 20-minute service stop at each delivery + driving back must fit inside the vehicle's remaining shift hours.
- **Stay near home** — the engine has a `max_depot_km` guard (default **100 km**) so trucks don't get routed onto another depot's patch.

---

## How cost is calculated

The cost of one route is simple:

```
route_cost = £150 activation cost   (only if the truck actually moves)
           + fuel_rate_per_mile × road_distance_in_miles
```

Distance is straight-line (Haversine) **× 1.3** to roughly approximate real UK road distance. Speed is a flat 50 km/h for arrival-time estimates.

The fleet cost is just the sum across trucks, plus a big penalty for any order that didn't get assigned:

```
fleet_cost = Σ route_cost + £50,000 × unassigned_orders
```

That £150 activation fee is deliberate — it stops the solver from opening 30 trucks for 30 orders. A second truck has to save more than £150 of fuel before the maths agrees it's worth firing up. This is what produces sensible **clustering** of orders onto fewer trucks.

The £50,000 unassigned penalty is much higher than any plausible single delivery, so the solver almost always prefers "make it fit somehow" over "drop the order."

---

## How the day unfolds: the rolling event loop

The dispatcher does **not** plan the whole day in one shot. It plays the day forward like a clock, and triggers a fresh planning call every time new goods arrive at a depot:

```
   time   06:00 ──────── 09:40 ──────── 11:15 ──────── 14:30 ──────► shift end
            │              │              │              │
            │              │              │              │
   event  PRE_STAGED   collection      collection     collection
          + DIRECT      truck back     truck back     truck back
          orders        — new freight  — new freight  — new freight
          ready         ready          ready          ready
            │              │              │              │
            ▼              ▼              ▼              ▼
          [SOLVE]       [SOLVE]        [SOLVE]        [SOLVE]
            │              │              │              │
       dispatch        dispatch       dispatch       dispatch
       routes          routes         routes         routes
       (frozen)        (frozen)       (frozen)       (frozen)
```

At every event:

1. **Mark vehicles that finished a route as "at depot" again** — they re-enter the pool.
2. **Add newly-ready orders** to the eligible pool.
3. **List available vehicles** — must be at depot, must have ≥ 1.5 hours of shift left.
4. **Call the VRPTW solver** with just (eligible orders, available trucks).
5. **Freeze the result** — once a truck is dispatched, you can't change its assignment.
6. **Project forward** — each dispatched truck's "available again" time = now + estimated route hours.

Vehicles confirmed as **line-haul artics** (those that visited two depots that day in real life — i.e. trunk runs between depots) are excluded from the available pool. They aren't doing local delivery, so they're not relevant.

This rolling design has a big benefit: **the plan stays realistic as the day evolves**. Trucks come back, new freight arrives, the solver re-plans against the current reality, not against a stale morning estimate.

---

## What's inside the solver: ALNS

When the rolling loop calls the solver, it runs an algorithm called **ALNS** — Adaptive Large Neighbourhood Search. Easiest way to picture it:

```
   START with a quick first-cut plan (greedy seed)
           │
           ▼
   ┌───────────────────────┐
   │  Pick a "destroy" op  │   tear out a chunk of orders from their routes
   │  Pick a "repair" op   │   put them back in the cheapest valid spot
   └─────────┬─────────────┘
             │
             ▼
       Is the new plan cheaper?
         │
         ├── yes → keep it as the new best
         │
         ├── nearly as good → maybe keep it (helps escape local rut)
         │
         └── worse → throw it away
             │
             ▼
       Repeat until time runs out (~30 seconds per event by default)
```

### Destroy operators (4 of them)

```
  worst   pull out the orders that add the most distance to their routes
  random  pull out a random handful (keeps things exploring)
  shaw    pull out orders close to a randomly-chosen "pivot" order
  route   pick the most expensive route, scramble its order list
```

### Repair operators (2 of them)

```
  cheapest   for each removed order, find the cheapest slot anywhere in the fleet
  regret     pick the order whose "best slot vs second-best slot" gap is biggest
             — places the hardest-to-place orders first
```

### Why "adaptive"?

After each iteration, whichever destroy + repair pair was used gets its **weight nudged** based on how well it did:

- Found a brand-new best plan? +9 weight
- Just better than the current one? +3
- Neither, but accepted anyway? +1
- Worse? No reward.

Over time, the operators that keep producing wins get picked more often. The ones that don't, get picked less. The mix is decided by **roulette-wheel selection** on the weights — so under-performing operators are not killed off entirely, they just become rare.

### Why bother with "worse" plans sometimes?

That sideways step is **simulated annealing**: the solver allows a worse plan with a small probability that shrinks over time (the "temperature" cools). Without this, ALNS gets stuck in the first locally-good plan it finds. With it, ALNS can climb out of local ruts and find something genuinely better.

---

## What you get back: the output

For each truck that has at least one stop:

```
Vehicle V001 (Lorry, depot: Duxford):
  Stop 1: deliver Order-A   [lat, lon]   weight 500 kg / 2 pallets
  Stop 2: deliver Order-B   [lat, lon]   weight 300 kg / 1 pallet
  Stop 3: deliver Order-C   [lat, lon]   weight 400 kg / 2 pallets
  Total route km:  187.4
  Estimated cost: £244.20   (£150 activation + £94.20 fuel)
```

Plus a fleet-level summary:

```
orders_total:       120
orders_assigned:    115
orders_direct:      2
orders_unassigned:  3
vehicles_dispatched: 14
total_planned_km:   2,941.6
```

And solver metadata: how many ALNS iterations ran, how much the plan improved over the greedy seed, how long it took, etc.

---

## How the backtest works (and what it can and can't tell you)

`backtest_vrptw.py` re-plays a historical day:

1. Run `simulate_day()` to get the **planned** km and cost.
2. Pull GPS telematics for the same day to compute the **actual** km driven.
3. Print a side-by-side table.

> ⚠️ **Backtest caveat — known and important.** The planned-vs-actual gap that shows up in this table is **not** a quality signal for the dispatcher. The two numbers are measuring different things:
>
> - **Planned km** = our dispatcher's optimised routes for the orders **in Qargo** (which is the *whole network's* workload — including partners and subcontractors).
> - **Actual km** = telematics for **our own fleet only** (which delivers a subset of those orders).
>
> So a gap can mean any of: our model is wrong, our fleet covered less of the network than expected that day, partners absorbed work, or Qargo includes orders we never physically touched.
>
> This is documented in `network_scope_problem.md` and in the auto-memory under "Network-fleet scope mismatch in backtest." Until that scope problem is solved at the data layer, the backtest is best read as a sanity check ("are the route lengths in the right ballpark?") rather than a precision benchmark.

---

## Known limitations and gaps

A plain list of what this dispatcher does **not** do today. None of these are bugs — they are scope decisions:

| Gap | What it means |
|---|---|
| **No time-window enforcement** | Each order has a `time_window_end` field, but the engine does not currently reject routes that miss customer deadlines. Only the shift-end-back-at-depot constraint is enforced. |
| **No driver-hours / shift law** | The 11- or 13-hour shift budget is a hard cap, but EU drivers' hours (4.5h driving block, 45-minute break, 9-hour daily max) are not modelled. A legally non-compliant route can be produced. |
| **No vehicle capability matching** | All trucks of the right weight/pallet size are treated as equivalent. No fridge, ADR (hazardous), tail-lift, or trained-driver constraints. |
| **Straight-line distance × 1.3** | No actual road-network routing. A motorway-only journey and a winding rural journey of the same haversine distance are treated identically. |
| **Flat 50 km/h speed** | No traffic, no time-of-day variation, no road-class effects. |
| **No cross-depot routing** | The `max_depot_km` guard hard-prevents a Duxford truck from delivering near Bedford. This is *correct* for current ZEEFLEET ops but means the model can't propose load-balancing improvements between depots. |
| **DIRECT orders consume no fleet state** | They are costed separately and don't tie up a real truck in the model. At 1.7% of volume this is harmless; at higher volume it would over-promise capacity. |
| **No revenue or margin awareness** | Pure cost minimisation. The dispatcher has no idea what a job pays — so it cannot recommend "decline this loss-making order" or price a marginal pickup. |
| **No live integration** | Reads flat JSON / Qargo CSVs offline. Not wired to Qargo (TMS), Supatrak (telematics), or Jigsaw (fuel) as live feeds — just historical extracts. |
| **No UI / map / Gantt** | Output is JSON and console summary only. No visual schedule, no map, no constraint-violation surfacing. |
| **No disruption replanning** | If a truck breaks down or a customer cancels mid-day, the dispatcher does not notice. Re-running it manually does respect committed work via the rolling window. |
| **Backtest scope mismatch** | See the caveat block above — Qargo orders are network-wide workload, telematics is our-fleet only. The two are not directly comparable. |

---

## Where this fits in the broader picture

The VRPTW dispatcher is **one slice** of what a complete operational logistics agent would do. It covers the "outbound delivery routing" part of the day — and it covers it as a *decision engine*, not a *decision support tool*. Around it, the real planner still has to:

- Approve or override the plan before it goes to drivers.
- Handle disruptions live.
- Decide whether to accept marginal orders (no margin model exists here).
- Reconcile against the partners-and-subcontractors picture that this model can't see.

Think of this dispatcher as **a fast, cheap "what would good look like?" estimator** for one phase of one day. Useful — but not a finished product.

---

## In one paragraph

The VRPTW dispatcher takes ZEEFLEET's daily orders and the fleet parked at each depot, and decides which truck delivers which orders along which closed-loop route, with the aim of minimising total fleet cost (£150 activation + fuel) while not exceeding weight, pallets, or shift hours. It runs through the day as a series of events triggered by freight arriving at the depot, so the plan stays in sync with reality. Inside each event, an ALNS solver starts from a quick greedy plan and improves it by repeatedly tearing out chunks and reinserting them, with adaptive weights and a cooling acceptance rule to escape local ruts. The output is a per-truck stop list with km and cost, plus a fleet summary. It does **not** model time windows, driver-hours law, vehicle capabilities, real road distances, revenue, or live data feeds — so its plans are best read as planning estimates inside a simplified model of the real operation, not as ready-to-execute schedules.
