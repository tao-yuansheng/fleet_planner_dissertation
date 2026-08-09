# Week-to-Week State Handover — Design

**Date:** 2026-07-04
**Status:** Approved (design); ready for implementation planning
**Scope:** `freight_planner` weekly runs

## Problem

Each weekly planning run bootstraps fresh: every vehicle starts at its home depot,
and freight state is re-derived from scratch. There is no state handoff from one
week's run to the next. This produces three concrete boundary defects, measured on
the shipped week-1 (2026-01-12..17) → week-2 (2026-01-19..24) plans:

| Piece | Volume (wk1→wk2) | Handled today? |
|---|---|---|
| **In-flight vehicles** — busy past Saturday on a multi-day tour tail | 18 vehicles | ❌ No — week 2 resets them home Monday 00:00 |
| **Spill-delivered orders** — wk1 tour tail delivers Jan 19–21 | 45 orders | ❌ No — week 2 re-plans them → double-counted km/coverage |
| **Staged freight** — collected by wk1, not delivered, not handed to Palletline | 94 orders | ⚠️ Partly — inferred from historical legs, not from wk1's actual plan |

The 735 collected-and-handed-to-Palletline orders (outbound trunk) correctly do **not**
carry — Palletline owns their delivery leg.

## Truth model (decided)

**Rolling simulation, plan → plan.** Week N+1's opening state is derived from what
**our week-N plan actually did**, not from independent historical reality. This is the
faithful "live rolling operation" model: the plan's end-state feeds the next plan's
opening-state.

**Whole-tour ownership (decided).** A multi-day tour that starts in week N but finishes
inside week N+1's window is owned **entirely by week N**, end-to-end (including its
Monday deliveries). Week N+1 is told only that (a) those vehicles are unavailable until
their tour ends and (b) those orders are already served. Tours stay atomic — never split
or re-planned across the boundary.

## Architecture

One small JSON artifact, `handover.json`, written into each run's `plan/` directory.
Week N's run **emits** it; week N+1's run **consumes** it via a new `--handover-in <path>`
flag. No predecessor → no flag → cold start (today's behavior, unchanged).

A single new module, `freight_planner/handover.py`, owns the artifact schema, the
producer (`build_handover`), and the consumer loader + `apply_*` helpers. The run driver
(`run_alns.py`) calls it at a handful of points; nothing else in the pipeline knows
handover exists.

### Artifact schema (`handover.json`)

```json
{
  "produced_by": {"start": "2026-01-12", "end": "2026-01-17"},
  "vehicle_availability": [
    {"vehicle_id": "N88GNW", "available_from": "2026-01-19T14:30:00", "at_node": "BEDFORD"}
  ],
  "delivered_order_ids": ["<uuid>", "..."],
  "staged_freight": [
    {"order_id": "<uuid>", "freight_id": "<uuid>", "depot": "CB22",
     "ready_time": "2026-01-17T16:00:00", "pallets": 12.0, "weight_kg": 8400.0}
  ]
}
```

- **`vehicle_availability`** — *fully new.* Every vehicle whose last selected job ends
  after `produced_by.end` (Saturday). `available_from` = that last job's end time;
  `at_node` = its last destination node (under whole-tour ownership this is the home
  depot, since tours include `DEPOT_RETURN`).
- **`delivered_order_ids`** — *fully new; the key correctness win.* Every order the plan
  delivered (final ledger state `DELIVERED`). Only the plan knows this; inference cannot.
- **`staged_freight`** — *refines an existing mechanism.* Every freight unit whose final
  ledger state is `AT_DEPOT`. `depot` = where the plan left it; `ready_time` = the pickup
  completion time (truthfully "ready since collection"); `pallets`/`weight_kg` from demand.

### Producer

One call at the end of `run_alns.main()`, after the plan is finalized:
`build_handover(final_ledger, selected_records, vehicle_df, window)` →
write `<out-dir>/…/plan/handover.json`. **Always emitted**, whether or not one was
consumed, so the chain continues to week 3+.

Deriving `delivered_order_ids` and `staged_freight` from the **final freight ledger**
(not leg patterns) is deliberate: it captures both fresh-collected-not-delivered freight
and carried-in freight that is *still* not delivered (seeded, never picked up this week,
so a leg-pattern derivation would miss it). This is what makes the chain correct across
3+ weeks.

### Consumer

`--handover-in <path>` → `load_handover()` (missing/empty path → empty handover), then
four touch points in `run_alns.main()`, each a thin call into a `handover.apply_*` helper:

1. **Exclude served orders** — after `legs_df` (line ~201) and `demand_df` (line ~220):
   drop every row whose `order_id ∈ delivered_order_ids` from both frames. Removes the 45
   double-deliveries.
2. **Vehicle availability** — at `vehicle_df = vehicle_states_frame(start)` (line ~202):
   patch `available_from` for in-flight vehicles, but **only where** the handover
   `available_from` > `start` (window open). Skip unknown vehicle_ids defensively. Also
   merge these into the `avail_overrides` dict passed to ALNS (line ~264), so seed,
   compatibility, and ALNS-time gating all agree the vehicle is out.
3. **Staged depot correction** — at `build_initial_freight_states(...)` (line ~222): pass
   a `{order_id: (depot, ready_time)}` map built from `staged_freight`; when an order is
   present, seed `AT_DEPOT` at the handover depot instead of the inferred depot. The
   pickup leg stays hard-blocked (pre-window date, [jobs.py:142]) — no double-collect.
4. **Producer emit** — end of `main()` (see Producer).

Everything else in the pipeline is untouched.

## Cold start

Week 1 has no predecessor. `load_handover()` returns an empty handover; all four touch
points are no-ops; the existing `_pre_window_collected` inference ([state.py:27]) stays
active. Week 1's output is identical to current behavior — a hard regression guarantee.

## Edge cases

- **Unknown vehicle in handover** (fleet regenerated, reg retired): skip the availability
  override — don't crash.
- **Staged ∩ delivered:** impossible by construction (staged = `AT_DEPOT`, delivered =
  `DELIVERED`); if it ever occurs, delivered-exclusion wins and we log it.
- **Vehicle home by Sunday:** `available_from` < Monday window start → no override → free
  Monday. Correct.
- **Vehicle out past mid-week:** override keeps it unavailable for days — correct for a
  long tour.
- **Staged freight still not deliverable in week N+1** (delivery date beyond that window
  too): stays `AT_DEPOT`, auto-re-emitted to week N+2. Correct by the ledger-based
  producer definition.

## Testing (TDD)

- **Unit `build_handover`:** in-flight detection (last job end > Saturday), delivered set
  from ledger `DELIVERED`, staged set from ledger `AT_DEPOT` with correct depot/ready_time.
- **Unit `load_handover`:** JSON round-trip; missing/empty file → empty handover.
- **Unit `apply_*`:** exclusion drops matching orders from legs + demand; availability
  patches only late vehicles and skips unknown regs; staged map overrides the seed depot.
- **Integration (two-run chain, synthetic fixture):** wk1 stages an order and leaves a
  vehicle out; wk2 consumes; assert (a) no double-delivery, (b) vehicle unavailable
  Monday, (c) staged order delivered from the handover depot.
- **Regression:** cold-start wk1 (no `--handover-in`) matches a baseline KPI snapshot.

## Validation (controller runs inline — never a subagent)

Re-run wk1 → wk2 *with* the handover chain. Confirm: the 45 double-deliveries are gone
(order/coverage counts sane), the 18 in-flight vehicles show reduced Monday availability,
and report the km delta as a **stakeholder line** — a km change with better boundary
correctness is a conversation, not a silent revert. Snapshot before/after.

## Non-goals (YAGNI)

- No shared persistent state store / DB — the JSON artifact suffices for the backtest and
  chains to N weeks.
- No mid-route re-planning of in-flight vehicles — availability is a simple
  `available_from` override; the vehicle returns home and is free from then.
- No boundary-cut / tour truncation — whole-tour ownership keeps tours atomic.
