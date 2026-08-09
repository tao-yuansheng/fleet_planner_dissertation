# Endogenous DIRECT-vs-XDOCK Mode Choice — Design

**Date:** 2026-07-23
**Status:** Approved design, pending implementation plan
**Author:** design session (freight_planner)

## Problem

For same-day FULL_FLEET orders, `legs.py` emits two mutually exclusive ways to
serve the freight: **DIRECT** (one vehicle carries origin → destination) or
**XDOCK** (collect to depot, then deliver from depot on a later same-day trip).
Today the choice is made *before* routing by `resolve_options`, using a static
geometric rule: keep XDOCK unless the via-depot route exceeds `ρ = 1.6 ×` the
direct customer-to-customer distance, scored with a standalone proto-vehicle.
The rejected group's legs are then deleted, so the seed and ALNS only ever see
one mode.

This is structurally wrong. The true cost of XDOCK depends on how much its
collection and delivery legs can **piggyback** on collection/delivery routes
that are running anyway — a quantity that varies by day, by demand geometry,
and by the solution itself. A single static `ρ` bakes in the average of a
distribution that actually varies order by order. A decision-split sweep
(Feb 2–8, combined parquet) confirmed the sensitivity: `ρ` governs the mode for
**305 of 396** same-day option sets, the knee spans the entire range 1.0–1.6,
and `ρ = 1.6` sits exactly at the saturation point where every cost-decided
order flips to XDOCK. The static resolver cannot see co-load at all, so `ρ` is a
blind fudge for it.

## Goal

Delete the static `ρ` decision and let the optimizer choose the mode by **real,
co-loaded insertion cost**, where the piggyback is actually priced. The choice
becomes endogenous in both the seed and ALNS. `ρ` is removed entirely.

## Scope

- **In scope:** same-day FULL_FLEET DIRECT-vs-XDOCK option sets — where both
  legs live in the same planning window and the co-loaded comparison is exact.
- **Out of scope (unchanged):** multi-day DIRECT-vs-XDOCK option sets (almost
  always XDOCK in practice) keep their existing mechanism; the TRUNK/HUBDROP
  resolver (`resolve_hub_drop`) is untouched.
- **Replaces two forced-decision reasons** that live inside `resolve_options`
  today and are deleted with it:
  - `xdock_window_infeasible` (staged delivery past its window → force DIRECT) is
    no longer a pre-decision. The optimizer enforces it naturally: an XDOCK
    bundle whose XD cannot be placed in-window fails to insert, so DIRECT is
    chosen. An optional emission-time pre-filter may prune such doomed XDOCK legs
    purely for efficiency; it is not required for correctness.
  - `pre_window_collected` (freight already collected on a prior day → force
    XDOCK) is subsumed by the **mode-lock** in Component 4: a committed XC seeds
    `chosen_group = XDOCK`, so DIRECT is never offered.

## Key decisions (from brainstorming)

1. **Choice locus:** fully endogenous in **seed and ALNS**; `ρ` deleted.
2. **Mechanism:** interleaved greedy seed choice + a new ALNS **OptionSwap**
   destroy operator that rides the existing repair pass (Approach A).
3. **Static resolver fate:** delete the DIRECT/XDOCK path and `DEFAULT_XDOCK_RATIO`
   entirely. The `ρ = 1.6` baseline is **not** captured (user decision,
   2026-07-23). The decision-split sweep already documents the old resolver's
   behavior, and the endogenous run's empirical cost-ratio distribution stands on
   its own as evidence — no baseline A/B is needed.
4. **Same-day XDOCK is confirmed supported** (Milestone 8b): the delivery leg is
   emitted for the same day with a staged ready-time (`pickup-window-close + 90
   min`, floored to delivery-window-open), pinned to the origin depot, and a
   multi-trip vehicle-day lets a later trip carry the staged freight. Infeasible
   staging (staged start past the delivery window) is filtered to DIRECT — today
   inside `resolve_options`; after this change, by the optimizer failing to place
   the XD leg.

## Prerequisite fact base

- `legs.py` **already emits both groups** for same-day sets (DIR + XC/XD),
  sharing `option_set` (== `freight_id`) and `option_group ∈ {DIRECT, XDOCK}`
  ([legs.py:498-568](../../../freight_planner/legs.py)).
- ALNS **already links** XD to XC via `dependency_type == "REQUIRES_PRIOR_PICKUP"`,
  so pickup-before-delivery ordering is handled.
- `JobMeta.candidate` is a dict that carries `order_id`/`leg_kind` today and will
  carry `option_set`/`option_group` for free once the collapse is removed.
- Committed/departed legs are unioned into the ALNS `pinned` set; **no destroy
  operator can remove them** ([alns.py:167](../../../freight_planner/alns.py)).
- `build_initial_freight_states` builds **one state per `freight_id`**
  (dedup via `unique()`), so both groups present do not double-count state
  ([state.py:47-62](../../../freight_planner/state.py)).

## Architecture & data flow

**Today:** `legs.py` (both groups) → `build_window_inputs` → `resolve_options`
**collapses** to one group → seed/ALNS see one mode → emission.

**New:** `legs.py` (both groups, unchanged) → `build_window_inputs`
**stops collapsing** (remove the `resolve_options` DIRECT/XDOCK call at
[run_alns.py:321](../../../freight_planner/run_alns.py); keep `resolve_hub_drop`)
→ both groups flow into `candidate_df` → `JobMeta.candidate` carries
`option_set`/`option_group` → seed picks a starting group per set → ALNS
OptionSwap refines against the full solution → emission reflects the finally
selected group.

## Components

### 1. Mutual-exclusion invariant

- **Key:** `option_set` (== `freight_id`).
- **Invariant:** at most one `option_group` per `option_set` is assigned at any
  time — in the seed, after every ALNS move, and at emission.
- **Enforcement:** a tracker `chosen_group: dict[option_set → "DIRECT"|"XDOCK"]`
  plus a guard in the repair/insertion step. A leg of group G is insertable only
  if `chosen_group[set]` is unset or equal to G. XDOCK counts as chosen once
  *either* XC or XD is placed (both must ultimately be placed; the existing
  `REQUIRES_PRIOR_PICKUP` dependency orders XC before XD).
- **Backstop:** `plan_validation`'s double-delivery check remains as a safety net.

### 2. Seed interleaved choice

When the seed reaches an option-set order, it trial-inserts the DIRECT leg and
the XDOCK bundle (XC then XD, respecting the ready-time floor) against the
**partial** solution, commits the cheaper by marginal cost, sets `chosen_group`,
and masks the sibling for the rest of the seed pass. Deliberately a warm start,
not sophisticated — the seed can only ever price against a partial solution, so
ALNS carries the real decision quality. Hook point: the candidate loop inside
`run_multiday_seed_plan`.

### 3. ALNS OptionSwap operator

A fourth destroy operator joining `random`/`worst`/`shaw` in `_DESTROY_OPS`,
adaptive-weighted like the others. It selects an option-set order, removes its
currently-assigned group's legs into the unassigned pool, and clears
`chosen_group[set]` so **both** groups become repair-eligible. The existing
repair pass then re-inserts the best group against the **full** solution under
the mutex guard — the endogenous, co-load-aware choice. Acceptance uses the
existing lexicographic (served, −cost) rule, so a swap sticks only if it holds
coverage and improves cost (or adds coverage). Rejection is the normal ALNS
undo; no bespoke revert logic.

### 4. Rolling-horizon mode-lock

Committed/departed legs are already pinned, so OptionSwap physically cannot rip
them out — the mode is locked once XC or DIR departs. Addition: at each window's
start, **initialize `chosen_group` from already-committed legs** so a set with a
departed XC is XDOCK-locked and the sibling DIR is never offered. Not-yet-committed
sets stay free, so the 12:00 re-opt can still flip a mode on new information.

### 5. Freight-ledger consistency

One state per freight unit (confirmed). Verification/fix point during
implementation: the initial location still resolves to "at origin customer" now
that the freight's leg set also contains XD (`ready_state=AT_DEPOT`, a mid-state).
Emission and the ledger derive from the *finally selected* legs, so with exactly
one group selected per set the downstream ledger is identical in shape to today's.

### 6. Reporting

Replace the `resolve_options`-produced `option_choices` record with an
**endogenous-choice record built from the final selected plan**: per `option_set`,
which group survived, plus the realized marginal cost of each mode where
available. Preserves the DIRECT/XDOCK split report and adds the **empirical
per-order cost-ratio distribution** — direct evidence on whether a single `ρ`
could ever have been right.

### 7. Deletion scope

Remove the DIRECT/XDOCK path in `resolve_options`, the `DEFAULT_XDOCK_RATIO`
constant, and their unit tests (`test_options_resolver.py` DIRECT/XDOCK cases,
the resolver half of `test_same_day_xdock.py`). **Keep** `resolve_hub_drop`, and
keep the `staged_delivery_start` staging math and its tests (still needed to set
the XD leg's staged window in `legs.py`; feasibility is then enforced by the
optimizer, which cannot place an XD whose staged start is past its window).

## Testing (TDD)

- Mutex guard rejects inserting a second group into an already-chosen set.
- Seed commits exactly one group per option set.
- OptionSwap flips DIRECT↔XDOCK and holds coverage.
- A swap that worsens cost without coverage gain is rejected.
- `FP_ALNS_CONSERVE` job-conservation holds across swap moves.
- Mode-lock: a committed XC forbids the sibling DIR.
- Initial freight state resolves to "at origin customer" with both groups present.

## Validation (the point)

- **Same-day XDOCK end-to-end** — instrument a run, count option sets where both
  XC and XD committed and the delivery landed on time; require zero silent strands.
- **Endogenous run metrics** — routed km, served-count, and the DIRECT/XDOCK split
  on the validation window (Feb 2–8, combined parquet), plus the empirical
  per-order cost-ratio distribution as the evidence that a single `ρ` could not
  have been right. No `ρ = 1.6` A/B — baseline intentionally not captured.

## Risks & mitigations

- **Seed choice priced against a partial solution** → accepted by design; ALNS
  OptionSwap re-prices against the full solution.
- **Initial freight location under both-groups-present** → explicit verification
  test before trusting emission.
- **Same-day XDOCK stranding** (the multi-day analog once needed an anti-stranding
  fix) → end-to-end both-legs-served instrumentation; design is *safe* even if a
  strand occurs (shows as an honest unserved order, not a silently wrong mode).
- **Coverage regression from removing the up-front collapse** → lexicographic
  acceptance preserves served-count; conservation checks and comparison against
  recent-run served-counts catch it.

## Out-of-scope / future work

- Multi-day DIRECT-vs-XDOCK endogenous choice (needs future-window delivery
  cost estimation + tour-path coordination + cross-window ledger).
