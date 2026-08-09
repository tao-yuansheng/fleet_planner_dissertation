# DIRECT Consolidation in Tour Building — Design

**Date:** 2026-07-04
**Status:** Approved (design); ready for implementation planning
**Scope:** `freight_planner` tour builder (`tours.py`, `tour_plan.py`) — resolver untouched

## Problem

Far full-fleet orders that reality serves as **one consolidated multi-drop sweep** are
split by our planner into a **dedicated DIRECT run plus a separate delivery tour** — two
vehicles up the same corridor where the real operation used one.

Concrete case (wk1): the actual Scotland run was **X8RNW**, one vehicle, Jan 14–15,
carrying five orders on a single multi-drop line-haul (KY11, then KA1/KA6/ML6). Our plan
served the same freight with **two** vehicles:
- **X888RNW** — `WT255892` (Stoke ST4 8JB → Dunfermline KY11) as a standalone **DIRECT**;
- **W88RNW** — `WT255893` + `WT255267` + `WT255768` (ML6/KA1/KA6) as one **XDOCK** tour.

This dedicated-DIRECT pattern is a major contributor to the wk1 plan-km overshoot vs the
odometer (+15.4% after the verified-leg fix) — the planner drives fuller/dedicated
journeys than the real consolidated groupage.

## Root cause (a deliberate exclusion, not a physics gap)

The tour engine already has everything needed:
- `evaluate_tour` ([tours.py:196–248]) carries freight **on board** with a running
  capacity peak — a `CUSTOMER_PICKUP`/`DIRECT_CUSTOMER_MOVE` loads mid-route, a
  `CUSTOMER_DELIVERY` unloads — and splits the sweep into days by the drive/duty caps.
- `_leg_km` ([tours.py:167]) already drives a two-point DIRECT as `prev → origin → dest`,
  so the **en-route pickup is baked into the DIRECT job itself** (no load-stop needed).
- `resolve_cluster` already builds **multi-depot sweeps with en-route load-stops** for
  depot-staged deliveries.

The single thing blocking the X8RNW pattern is a policy gate in `resolve_cluster`
([tours.py:514]):

```python
# A DIRECT move collected at a customer in another depot's territory would need
# cross-territory customer collection -> not allowed (fall back per depot).
if any(j.leg_kind == DIRECT_CUSTOMER_MOVE and not _origin_at_depot(j, anchors) for j in jobs):
    return _per_depot()
```

`WT255892`'s origin (Stoke) is not at a depot → the whole cluster drops to per-depot
tours → the DIRECT strands as a dedicated sweep. A companion exclusion in the salvage
re-pool ([tour_plan.py:334], *"Two-point moves stay put — they are what poisons
consolidation"*) keeps it stranded.

## Design (tour-builder only)

**Principle:** a DIRECT move may be **collected en route** during a consolidated sweep
even when its origin is a customer (not a depot) — kept only when it actually saves
system km. The resolver, the XDOCK path, and the daily seed are untouched.

### Change 1 — `resolve_cluster` (`tours.py`)

Replace the blanket *"any non-depot-origin DIRECT → `_per_depot()`"* gate with a
**try-consolidate-then-guard** step that reuses the existing per-depot fallback as its
own km baseline:

1. Build the consolidated tour **including** the DIRECT(s):
   `depot, ordered, ev = _build_at(primary, jobs)` — the two-point legs self-drive
   `depot → … → origin-pickup → dest → …`.
2. Compute the fallback once: `split = _per_depot()`.
3. **Return the consolidated tour iff `ev.feasible` and
   `ev.total_km ≤ sum(t.evaluation.total_km for t in split)`; otherwise return `split`.**

`_per_depot()` *is* the `km_split` baseline: an off-corridor DIRECT makes the consolidated
km exceed the split and it falls back automatically. No new tunable; coverage can never
drop (worst case equals today's behaviour).

**Scope guard — this triggers ONLY when the cluster contains a non-depot-origin DIRECT.**
Pure-delivery clusters are completely unchanged: the existing single-depot and multi-depot
delivery-load-stop branches keep their current `return [...] if ev.feasible else
_per_depot()` behaviour verbatim (no km-comparison is added to them). When a non-depot
DIRECT is present, its jobs flow into `_build_at(primary, jobs)` and ride along via their
own two-point legs (they are not delivery depots, so they add no load-stop), and the
km-guard above decides consolidated-vs-split.

### Change 2 — salvage re-pool (`tour_plan.py`)

Drop the two-point exclusion in the salvage pass ([tour_plan.py:330–335]) so a stranded
**on-corridor** DIRECT (single-job sweep) re-pools with readiness-compatible neighbours
and gets a second chance to merge. The km-guard in Change 1 still gates the merge, so a
genuinely off-corridor DIRECT re-strands harmlessly.

## Why the resolver (and its `1.6` ratio) stay as-is

The `DEFAULT_XDOCK_RATIO = 1.6` and `_window_infeasible` gates were only harmful because
DIRECT meant *dedicated*. Once DIRECT can consolidate:
- `_window_infeasible` (same-day → DIRECT, which is what caught `WT255892`) now produces a
  **consolidatable** DIRECT that rides a sweep — correct, not harmful.
- The `1.6` ratio sends *far* orders to DIRECT (→ consolidated line-haul) and *near*
  orders to XDOCK (→ depot pooling) — the right split.

**Deferred (out of scope):** whether *far, multi-day* orders that the ratio currently
sends to XDOCK (depot backtrack) would be better as consolidated directs. Measure the
tour-builder fix first; only revisit the ratio / `_window_infeasible` if far multi-day
orders still fragment.

## Edge cases (handled by `evaluate_tour`; we only stop blocking them)

- **Capacity** — transient load peak between a DIRECT's origin and dest is tracked;
  over-capacity → infeasible → fall back.
- **Readiness** — `floor_offsets` reject reaching a DIRECT origin before its freight is
  ready (e.g. a pre-window or next-day collection).
- **Cross-territory collection** — legitimate for a FULL_FLEET order (the whole journey is
  ours); the km-guard, not a territory rule, decides if it is worth it.
- **Poor sequencing** — if `_order_nearest_neighbour` orders the pickup badly, km rises
  and the guard falls back. A two-point-aware ordering tweak is a possible follow-up, not
  part of this change.

## Testing (TDD)

- **Consolidate on-corridor DIRECT:** cluster with a far-but-on-corridor DIRECT + nearby
  deliveries → one consolidated tour, DIRECT origin visited en route (not `_per_depot`).
- **Reject off-corridor DIRECT:** origin far off the corridor → `km_fold > km_split` →
  falls back to standalone; all jobs still served.
- **Infeasible fold:** capacity/day-cap exceeded → falls back, coverage preserved.
- **Salvage re-pool:** two stranded on-corridor directs merge into one sweep.
- **Regression:** existing single-depot, multi-depot load-stop, and `resolve_cluster`
  tests unchanged.

## Validation (controller runs inline — never a subagent)

Re-run wk1 → wk2. Confirm: `WT255892` rides a shared Scotland sweep instead of a dedicated
X888RNW run; fewer far-corridor vehicle-days; wk1 plan km moves **down** toward the
odometer (89,571); coverage held 99.9%/100%; zero ledger/temporal violations. Report the
km delta as a stakeholder line — a km-down-with-better-structure change, not a silent
revert. Snapshot before/after.

## Non-goals (YAGNI)

- No changes to `options_resolver.py` (DIRECT-vs-XDOCK selection, the `1.6` ratio,
  `_window_infeasible`).
- No new "consolidated direct / line-haul" mode — the existing two-point job + tour engine
  already carry freight on board; we only remove the exclusion.
- No `_order_nearest_neighbour` two-point sequencing rework (follow-up if validation shows
  the km-guard rejecting genuinely on-corridor folds).

---

## Implementation outcome (2026-07-04)

**Shipped Task/Change 1 only. Change 2 (salvage exclusion) was DROPPED** — dropping the salvage
two-point exclusion regressed delivery re-merges: a same-day far-origin DIRECT poisons the salvage
re-pool (its backtrack pickup makes the 3-way consolidation LATE-infeasible, and `_per_depot` then
fragments the deliveries that would merge without it). The main-pass km-guard (Change 1) is the whole
fix; the salvage exclusion stays as-is. Only `tours.py::resolve_cluster` changed.

**Result: structural goal met, km-NEUTRAL (not the km lever).** WT255892 rides a shared sweep; wk1 has
0 dedicated far-direct tours. But plan km did not drop (wk1 +2%, wk2 flat, combined +1.1% — within ALNS
±2-4% variance); coverage held 99.9/100%, 0 violations. The +15% odometer overshoot is unchanged — its
driver is elsewhere (tracked as a follow-up in QUEST_LOG). User chose to KEEP + hunt the km lever next.
