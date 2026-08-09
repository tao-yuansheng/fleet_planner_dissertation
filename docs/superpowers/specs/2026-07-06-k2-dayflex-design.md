# K2 v1 — Earlier-Only Day-Flexibility For Depot-Controlled Deliveries (Design)

**Date:** 2026-07-06
**Status:** approved design (stakeholder-scoped 2026-07-06)
**Backlog origin:** QUEST_LOG K2 "Day-flexibility / load-building across days" (filed 2026-07-03)

## Problem

The plan pins every job to its `service_date`. The seed bins jobs by day, and in ALNS each
job's `JobMeta` carries a single fixed `day` — every insertion targets `(vehicle, job.day)`
only (`alns.py` ~line 401 and the `rv(vid, day)` insertion sites ~555-653). Reality's
dispatchers exploit a day axis we don't have: freight that is already sitting in our depot
can go out on whichever day's round fills a truck best. That is one of the catalogued
"unmodeled real-operator levers" behind the plan-vs-reality structure gap.

## Stakeholder scoping decisions (2026-07-06)

1. **Eligible universe: FULL_FLEET orders whose freight is under our depot control** —
   i.e. orders resolved to the XDOCK shape, delivery legs only. For these we know exactly
   when the freight is in depot (`freight_ready_time`) and we control the outbound day.
   PL_IMPORT/PL_EXPORT stay pinned (network-scheduled); collections stay pinned
   (hindsight-hardened per the collection-window-anchoring work); DIRECTs never touch the
   depot; tour/shuttle/trunk jobs are seed-fixed and out of scope.
2. **Earlier-only flexibility.** A delivery may move to any day in
   `[ready_date → historical due date]`, never later. Fairness is bulletproof: reality met
   that date, so the plan is never granted freedom reality lacked. No service-promise model
   is needed. (Holding freight *later* than history = a future model version requiring a
   promise model; explicitly out of scope.)
3. **Earliness cap: 2 days.** Where no explicit delivery `window_start` exists, the plan
   may deliver at most 2 days before the due date:
   `day_min = max(ready_date, due_date − 2d, window_start_date if present)`.
   Calendar days — non-operating days simply offer no insertion slots, so the effective
   flexibility is "this round or the previous one or two".
   Matches groupage practice; keeps the days-early KPI defensible.
4. **Mechanism: ALNS cross-day moves (approach A).** The seed keeps every job on its
   nominal day; only the ALNS repair step may exploit the flexibility. Rationale: the day
   choice is optimized jointly with vehicle + sequence by the component the budget sweep
   proved effective; a seed-level day assignment (approach B) would freeze days before any
   route exists — the same premature-structure mistake as the reverted tour-aware resolver.
   Seed-assign+search-refine (approach C) is YAGNI for v1.

### Key population insight

Same-day XDOCK orders (collect and deliver the same day — 254 of 348 same-day FULL_FLEET
option sets in the Jan 12-17 reference week) gain **nothing** from earlier-only flexibility
(`ready_date == due_date` ⇒ pinned). The beneficiaries are the **multi-day dwellers**:
FF-XDOCK orders collected days before their historical delivery, and freight already
`AT_DEPOT_OR_HUB_PENDING` at window open (including handover-staged freight, which is
flexible from day 1 of the window). The eligibility KPI must report the actual qualifying
count per run.

## Design

### 1. Eligibility + allowed-days resolver (new module `freight_planner/dayflex.py`)

Pure function over the candidate-jobs frame (+ leg fields already present:
`freight_ready_time`, `ready_state`, `effective_window_start/end`, `raw_window_start`):

```
eligible(job) iff
    flow == FULL_FLEET
    and leg_kind == CUSTOMER_DELIVERY
    and xdock-shaped (freight passes our depot; sibling pickup leg or AT_DEPOT ready_state)
    and ready_date < due_date
day_flex_min(job) = max(ready_date, due_date − 2 days, window_start_date if present)
```

Emits one new nullable column on candidate jobs: `day_flex_min` (ISO date). Empty ⇒ pinned
(today's behavior, and the default for every non-eligible job). No existing column changes.

### 2. Window semantics on a shifted day

When the search tries day `d'` with `day_flex_min ≤ d' < due_date`, the job's intra-day
time window on `d'` is `[max(freight_ready_time, d' operating open) → d' operating close]`,
reusing the operating-day expansion machinery introduced by collection-window anchoring.
On the due day itself the existing effective window applies unchanged. The freight-readiness
gate is the floor of the range — never bypassed, so DELIVERY_BEFORE_PICKUP remains impossible.

### 3. ALNS change (the only search change)

- `JobMeta` gains `allowed_days: tuple[str, ...]`, default `(day,)` — zero behavior change
  when the flag is off or the job is pinned.
- The repair insertion loop iterates `allowed_days × eligible_vehicles` (instead of the
  single nominal day) and keeps the cheapest feasible slot. `excluded_vehicle_days`
  (seed-reserved) and pinned job sets (shuttle) are respected per day.
- Destroy operators, SA acceptance, vehicle-day consolidation, and coverage repair are
  untouched. Multi-day cost deltas flow through the existing B16-hardened `changed_costs`
  day-revalidation.
- Expected win path: pulling a job off a thin day empties it → existing consolidation drops
  the whole vehicle-day.

### 4. Flag + determinism

- CLI `--day-flex` on `run_alns` (default **off**), recorded in `run_manifest.json` and
  passed through by `run_month` (add `--day-flex` passthrough there too).
- **Flag-off must be bit-identical to current behavior** — proven by test (same standard as
  the road_km memo determinism gate), not asserted.
- `FP_ALNS_CONSERVE=1` invariants must hold with the flag on (no silent job loss across
  day moves).

### 5. Honesty KPIs (service-impact ledger)

- `kpi_summary.md` gains a K2 block: eligible jobs, shifted jobs, days-early histogram
  (0 / 1 / 2 days).
- Plan records / `route_stops.csv` carry `due_date` and `days_early` per customer stop so
  the viz and any reviewer can audit each early delivery individually.
- Unassigned accounting unchanged.

### 6. Tests (TDD)

1. Resolver predicate table: same-day order ⇒ pinned; multi-day FF-XDOCK ⇒ flexible;
   `window_start` respected; 2-day cap enforced; pre-window `AT_DEPOT_OR_HUB_PENDING` ⇒
   flexible from window day 1; PL_IMPORT / DIRECT / collection legs ⇒ never eligible.
2. Window-on-shifted-day derivation fixture (ready-time floor honored on the early day).
3. Synthetic two-day repair fixture: flexible job moves to the cheaper earlier day; pinned
   twin does not.
4. Determinism: flag-off run bit-identical to pre-change baseline (cost checkpoints).
5. `FP_ALNS_CONSERVE` invariants with flag on.
6. KPI lines render (eligible/shifted/histogram) and `days_early` lands in route_stops.

### 7. Measurement protocol (decision gate)

Controlled A/B on Jan 12-17, seed 0, same handover-in (`runs/2026-01/2026-01-05_to_2026-01-10/plan/handover.json`):
OFF vs ON at 120s (quick signal), then at 1800s (the real read — day moves may need budget
to be exploited; 1800s is the established past-the-knee operating point).

Keep iff: **km ↓ at equal coverage**, vehicle-days ↓ or =, and the days-early histogram
looks like groupage practice (mass at 1 day, not everything piled at the 2-day cap).
A km-neutral result is a finding, not a failure (cf. K1 merge replay).

Run outputs go to a separate experiment out-root (`runs_exp_k2/`) so the 120s month
baseline and the budget-sweep runs stay intact.

## Risks

- **Repair throughput:** flexible jobs try up to 2 extra days per insertion. Bounded — only
  the eligible minority pays it; we hold +24% memo headroom and the 1800s budget sits on a
  plateau. Measure iters/s in the A/B.
- **Window correctness on d':** the fixture tests pin the ready-time floor and operating-day
  bounds; this is where a silent bug would hide.
- **Handover:** none — earlier-only never holds freight past its historical date, so no new
  cross-week state is possible by construction.

## Out of scope (recorded for the next model version)

- Later-than-history holds (requires a service-promise model).
- PL_IMPORT final-leg flexibility (trunk-arrival-controlled; a natural v2 extension since
  T1 gives a known 10:00 ready time).
- Seed-level day assignment (approach C's first half) — revisit only if the A/B convergence
  curve shows the search starving.
- K3 hub-injection fee-vs-km tradeoff (separate backlog item).
