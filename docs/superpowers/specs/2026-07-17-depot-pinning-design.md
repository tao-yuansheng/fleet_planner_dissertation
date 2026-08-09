# Depot pinning: freight is served from where it physically sits — design

**Date:** 2026-07-17 · **Trigger:** A1 gate of the final results campaign FAILED
(experiments/FINAL_CAMPAIGN.md): 130/618 routed delivery legs in run_collocated2 are
physically inconsistent — freight at depot X, delivered by a Y-homed vehicle that
never visits X. Class B (60 legs, 274 pal: xdock-landed + prestaged) is genuinely
unpriced inter-depot repositioning, worst-case 4,277 km = 12.4% of the window's
combined km, one-directional (understates plan km → inflates the headline vs the
incumbent). Feeder: 31/415 pickups' ledger `target_depot` ≠ the carrying trip's
actual return depot.

## Decision

**Static end-to-end depot pinning, default ON, flag `--no-depot-pinning`.**

Every daily leg is emitted with `depot_bound` = the depot label it already carries:

| leg kind | bound to | why |
|---|---|---|
| `CUSTOMER_PICKUP` (all flows: XC, xdock :C, PL_EXPORT :C, LOCAL_COLLECT :C) | `target_depot` | the freight must LAND there (the delivery stages there; the outbound trunk departs there) |
| `CUSTOMER_DELIVERY` (all flows: XD, xdock :D, PL_IMPORT :D, LOCAL_DELIVER :D) | `source_depot` | the freight IS there (trunk landed it / pickup landed it / opening stock) |
| `DIRECT_CUSTOMER_MOVE`, `HUB_DROP`, trunk legs | — (no bound) | never touch a depot / are themselves the priced inter-depot move |

`depot_bound` is enforced by the existing hard `DEPOT_BOUND` infeasibility in
`evaluate_route` (kind-agnostic loop over member jobs, shipped with the collocated
rule) and by `tour_attach._depot_bound_mismatch` on the intraday paths. No evaluator
change is needed. Emission-site placement is load-bearing exactly as in the
collocated rule: candidates, resolver options, route_seed, ALNS, micro-pass, tour
candidates, and new-arrival legs all inherit from `legs_df` / `build_candidate_jobs`,
which already plumb `depot_bound` end-to-end.

Under pinning, a pickup can only be carried by a vehicle homed at its target depot,
so it physically lands where the ledger says; the delivery is bound to that same
depot; labels become physical. Inter-depot freight movement happens ONLY on priced
trunk legs. The model's invariant, stated for the paper: *"freight flows are
depot-pinned end-to-end; a leg may only be served by a vehicle homed at the depot
where the freight rests; all inter-depot movement rides explicitly priced trunks."*

## Why static pinning, not commit-time propagation

The initially sketched alternative — stamp the delivery's bound with the picking
trip's ACTUAL return depot at pickup commit — is circular: pickup (day D) and
delivery (day D+1) are co-planned at the same anchors (the window is multi-day), so
the bound would derive from an uncommitted assignment the same ALNS run is free to
move. Static pinning has no ordering problem, and it is the precedented shape: the
tour side already enforces exactly this (`WT254009` target-depot gate — "a tour with
a CUSTOMER_PICKUP never goes cross-depot"). This extends the same law to the daily
path.

## Class A (PL_IMPORT) rides along

The 70 PL_IMPORT teleports are the trunk lander and the daily assigner disagreeing.
Re-landing analysis showed moving those pallets across the nightly hub lanes costs
+0 trips / +0 km, so pinning the delivery to the lander's choice (its
`source_depot`) costs ~nothing on the trunk side and restores coherence with the
daily side re-optimizing under the bind.

## Expected behavioral impact (probe must verify)

- ~161 currently-violating leg assignments (130 deliveries + 31 pickup homes)
  re-home to bound-depot vehicles or become honestly UNASSIGNED with reason
  `DEPOT_BOUND` (no same-depot vehicle) — fake feasibility is not preserved.
- Plan km expected to RISE somewhat; the headline improvement SHRINKS. That is the
  honest direction and the point of the fix.
- Resolver unchanged: it may still pick an XDOCK option whose XD later finds no
  bound-depot vehicle → visible unassigned, not a teleport. Watch the count.
- Service ledger must remain complete (every order accounted; slips/unserved may
  grow honestly).

## The accounting re-key (critical regression guard)

Two sites currently infer "collocated reclassified delivery" from `depot_bound`
presence. Under universal pinning that test is no longer discriminating — every
PL_IMPORT order would be counted collection-satisfying and the collection ledger
would be flooded. Both re-key on the `:DIR`-tail identity (a CUSTOMER_DELIVERY whose
leg/job id tail starts with `DIR` is the collocated reclassification; normal
deliveries keep `:D`/`:XD` tails):

- `run_rolling._collection_satisfying_job` (~line 512): `kind == CUSTOMER_DELIVERY
  and depot_bound` → `kind == CUSTOMER_DELIVERY and job-id tail startswith "DIR"`.
- `run_rolling.serviceable_collect_ids` (~line 629): `bound = oid[kind eq
  CUSTOMER_DELIVERY & depot_bound ne ""]` → same set further masked by leg_id tail
  startswith "DIR".
- `collection_orders_in_plan` already keys on the DIR tail — unchanged.

## Config & flags

- `config.DEPOT_PINNING: bool = True` (+ comment), next to
  `DAILY_DEPOT_DIRECT_AS_DELIVERY`.
- CLI `--depot-pinning/--no-depot-pinning` (BooleanOptionalAction) in run_rolling.py
  AND run_alns.py, mapped in the existing `_apply_vehicle_day_cost_flags` blocks.
- Flag OFF = exact legacy emission (`depot_bound=""` everywhere EXCEPT the
  collocated single-delivery leg, which keeps its own unconditional bind under its
  own flag — shipped behavior preserved for pre-fix replays and the ablation run).
- Emission helper in legs.py: `_pinned(depot: str) -> str` returning `depot` when
  `DEPOT_PINNING` (via `_fp_config()`, same pattern as `_collocated_with_depot`)
  else `""`; guards falsy depots.

## Out of scope (recorded, not built)

- Launched-trip suffix insertion of a depot-loaded delivery (freight could not have
  been aboard at departure) — pre-existing, orthogonal to pinning, candidate for the
  audit register. The A1 probe script's visited-before test will surface it if it
  occurs.
- Dynamic re-landing of PL_IMPORT trunks to follow the daily assignment (the +0 km
  analysis makes static binding equivalent in cost).
- Depot↔depot shuttle legs as a priced repair mechanism (no such lane exists; not
  needed once pinning holds).
- The tour batcher's internally-reclassified deliveries (materialize DEPOT_LOAD and
  physically visit the depot — consistent by construction).
- The static tour batcher (`tour_plan.py`) does not read `depot_bound` on
  legs.py-emitted jobs. Empirically moot at probe scale (the reference window's
  tours carried exactly one leg — a pickup, covered by the WT254009 gate); the
  probe's 0-violation acceptance sweeps tour routes too, so a regression here is
  caught, and a batcher-side gate is a follow-up if month runs ever surface one.

## Acceptance (probe)

Fresh probe run (`run_pinned`, same window/CLI as run_collocated2), then:
1. Re-run the A1 analysis script against it: **spatial violations = 0** (all routed
   delivery legs served from the freight's depot or an explicitly visited one).
2. Service ledger complete: every in-universe order accounted; report the
   ON_TIME/SLIPPED/UNSERVED delta vs run_collocated2 (453/0/0).
3. Report km delta (combined), vehicle-days delta, unassigned count + reasons.
4. Suite green (929 + new tests).
