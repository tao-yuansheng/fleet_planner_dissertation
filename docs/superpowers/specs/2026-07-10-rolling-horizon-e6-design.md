# E6 — Non-Anticipative Rolling-Horizon Dispatch (design)

**Date:** 2026-07-10
**Status:** design agreed, not yet implemented
**Supersedes:** nothing. **Depends on:** E1 baseline (OSRM epoch), `handover.py`, `tour_plan.py`, `routing_adapter.py`

---

## 1. Purpose

E1 gives the planner the whole week's order book at solve time. A referee will say the
saving is bought with clairvoyance: the plan consolidates orders it could not have
known about when it dispatched the vehicle that serves them.

**Deliverable: a working non-anticipative dispatcher.** The solver only ever sees
orders booked by the decision epoch, never alters a trip that has left the depot, and
runs on the current pipeline with every shipped feature intact (§1.1). The E1
contrast is how the mechanism is evaluated, not the product itself.

**Research question.** How much of E1's km and vehicle-day saving survives when
foresight is removed?

**Success criteria.** (a) The rolling mode runs end-to-end on real windows and its
outputs flow through the existing KPI/validation/viz stack unchanged. (b) A controlled
contrast against E1 in which *only* visibility and commitment differ, reported on
three axes: Δ km, Δ vehicle-days, and a service ledger.

## 1.1 Compatibility contract — what must not break

The rolling mode is strictly additive. Feature by feature:

| shipped feature | behaviour under rolling |
|---|---|
| default paths (`run_alns`, `run_month`) | untouched; flag-off **bit-identical** (gate in §9) |
| **multi-day tours** | still form — every epoch's seed runs tour formation over the orders visible at that epoch; a departed tour freezes whole (`reserved` for its span) |
| **K1 shuttle** | still runs — standing scheduled capacity, exempt from visibility gating (§4.1); the pre-loop carve happens per epoch exactly as in the seed today |
| **night trunk** | still runs nightly — structural standing service, never order-triggered |
| **weekly handover** | preserved — rolling runs inside a window, consumes `--handover-in`, emits `handover.json` at window end, so month chains keep working |
| **emission contract** | identical plan-dir artifacts (`route_stops.csv`, `selected_plan_alns.csv`, KPI, manifest), so `month_summary`, `viz_app --validate`, `plan_full` consume rolling outputs unmodified |

---

## 2. Evidence base

Everything below is measured, on Jan–Feb 2026, over the eight E1 windows. These are
inputs to the design, not outputs of it.

| Fact | Value | Source |
|---|---|---|
| `timestamp_created` is a genuine booking clock | 98.4% non-zero seconds; flat minute distribution | raw Qargo parquet |
| `origin_date` is an **outcome**, not a booked date | `== date(origin_timestamp)` for 100% of rows once BST is applied (the 13 apparent mismatches are all 23:00 UTC on/after 2026-03-29) | raw parquet |
| `destination_date` is likewise an outcome | 99.93% match | raw parquet |
| SLA transit is **collection-anchored** | Next day holds at 1.0 d transit at collection lead 0/1/2/3; 72 Hours at 5.0. `promise` (booking→delivery) grows 1:1 with the lag | raw parquet |
| `service_level_name` cannot license deferral | late-booked freight is *more* urgent than average (63.3% Next day vs 58.3% overall) | raw parquet |
| The depot runs a de-facto **noon booking cut-off** | P(same-day collection): 63.7% at 11:00 → **34.9% at 12:00** → 22.9% at 13:00 → 4.7% at 15:00 | raw parquet |
| The depot does **not** buy out late bookings with overtime | of 64 owned collections booked after 19:00, **1** was collected same day | raw parquet |
| Day-shift duty spans, rigids | 18t: 7.0% over 13 h; 26t: 10.0% | telematics, ignition-on |
| Collections are day-pinned | day-flex found 2/2,317 eligible | K2 |
| Departure structure | 81.8% before 10:00; 8.2% of vehicle-days run a 2nd trip | E1 plan |
| Foresight exposure (commitment audit) | 70.0% clean, 19.5% soft (divertible), **10.5% hard** (median overshoot 1.7 h) | E1 plan × booking clock |
| Week-open information | at Mon 04:00 only **14.0%** of the week's collections exist | raw parquet |
| A 10k-iteration solve | ~2,000 s wall (ALNS 1,844–2,049 s; setup ~115 s) | E1 progress logs |
| Late-booked orders cluster at standing-service / near-depot shippers | 16.1% of the slip set is the shuttle shipper; top origins MK42 (Bedford doorstep), CB97/CB25 (CB22 doorstep) | raw parquet |
| Multi-slip is real | 19.1% of afternoon-booked collections are collected **2+ days** after booking | raw parquet |
| A 03:00 first epoch costs nothing in visibility | 2 bookings in 03:00–04:00 across all 8 weeks | raw parquet |

**Flow taxonomy** (from `freight_planner.demand`, *not* `cambridge.scope`):
collections are `PL_EXPORT`, `LOCAL_COLLECT`, `FULL_FLEET`; deliveries are `PL_IMPORT`,
`LOCAL_DELIVER`.

---

## 3. Non-goals

* **No en-route diversion.** A departed trip is frozen whole. Diverting a moving
  vehicle to a newly-booked collection requires live vehicle position, which most
  carriers do not have. This is stated as future work, and it is exactly the 19.5%
  soft category.
* **No overtime valve.** Reality does not use one (§2), the failure mode is
  commitment-bound not duty-bound, and loosening `shift_end` would break the
  controlled contrast by handing E6 a constraint E1 lacks.
* **No constructed due dates.** Deriving a due date from the booking cut-off would
  change the day assignment as well as the visibility, and the two effects would no
  longer be separable.
* **No production hardening.** E6 is an offline simulation.

---

## 4. Architecture

Four new modules plus one enabling refactor. Nothing in the E1 solve path changes
semantically; the flag-off path must stay bit-identical.

```
freight_planner/
  visibility.py     NEW  order-visibility mask at an epoch
  epoch_state.py    NEW  commitment + vehicle-pool state vector
  run_rolling.py    NEW  epoch loop, freeze, accumulate, emit
  run_alns.py       MOD  extract solve_window() out of main()
  routing_adapter.py MOD evaluate_day() gains drive_since_break carry-in
```

### 4.1 `visibility.py`

```
visible_order_ids(demand_df, as_of: datetime) -> set[str]
```

* **Collection** (`corrected_flow ∈ {PL_EXPORT, LOCAL_COLLECT, FULL_FLEET}`):
  visible iff `timestamp_created <= as_of`.
* **Delivery** (`PL_IMPORT`, `LOCAL_DELIVER`): visible from `18:00` on the day before
  its service day. The freight lands at our depot or a network hub overnight, so
  booking time is not our knowledge point.
* `origin_timestamp` is **never read** (51.7% placeholder). Only `origin_date`, and
  only as a target day, never as a visibility signal.
* **Standing-service exemption (shuttle).** K1 shuttle-carved jobs bypass the
  visibility gate. The shuttle is committed capacity on a schedule: the vehicle goes
  to the address regardless of what has been booked and takes what is on the dock, so
  serving a late-booked order there requires no order knowledge — the same logic that
  exempts the nightly trunk. The data agrees: the single owned collection booked after
  19:00 yet collected same-day was at the shuttle address, and 131 of the 813
  late-booked orders (16.1%) originate there. Exempt orders are tagged
  `SHUTTLE_STANDING` in the ledger so the exemption is auditable.

Applied by filtering `legs_df` and `demand_df` on `order_id` before the seed runs.

### 4.2 Epoch grid and decision lag δ

A plan computed at epoch `t` cannot dispatch a vehicle at `t`. **δ** is the lag from
epoch to earliest dispatchable departure: solve wall time plus dispatch overhead
(briefing, paperwork). Loading is handled by the grid, not by δ (below).

* **Default δ = 60 min.** A 10k solve is ~33 min, leaving ~25 min of overhead.
  Sensitivities at δ = 30 and δ = 120.
* **Default epoch grid: 03:00, 12:00** (AMENDED 2026-07-10, stakeholder
  decision): the run-1 reveal decomposition measured the 08:00 epoch
  informationally hollow — 3-40 new bookings/day against 350-510 at 03:00 and
  ~100-136 at 12:00 — so the day carries two solver decisions (morning wave,
  noon cut-off) plus the non-solver 18:00 day close (§4.6a). Removing 08:00
  locks mid-morning departures at 03:00: strictly MORE pessimistic, safe for
  the bound. 12 solves per 6-day window; `--epochs 03:00,08:00,12:00` restores
  the 3-epoch grid as a sensitivity.

**Why 03:00 and not 04:00:** the morning wave is the one dispatch where δ alone is not
enough — a 26-pallet trailer takes 45–60 min to load, doors and forklifts queue, and
the warehouse needs the load plan *before* loading starts, not while drivers are
briefed. A 03:00 epoch lands the plan ~03:35 and gives the warehouse a two-hour runway
to the 06:00 wave. The visibility cost is **2 bookings across all eight weeks**
(03:00–04:00 slot). 12:00 is the operation's own booking cut-off, recovered from data;
08:00 is the mid-morning top-up. The grid was not fitted; it maps onto the observed
departure structure:

| epoch | commits departures in | E1 trips landing there |
|---|---|---|
| 03:00 | 04:00 – 09:00 | 207 (the 06:00–08:00 wave) |
| 08:00 | 09:00 – 13:00 | 46 |
| 12:00 | 13:00 → | 10 |

Both grid and δ are parameters. The grid's coarseness is a known bias (§10).

### 4.3 Commitment rule (strict)

At epoch `t` with next epoch `t_next`:

* **Freeze** every trip whose depot departure `< t + δ`. It is gone from the solver's
  problem: its orders are excluded, its vehicle returns via the pool (§4.4).
* **Commit** (newly fix) trips departing in `[t + δ, t_next + δ)`.
* Everything departing `>= t_next + δ` is released and re-planned.
* A **multi-day tour** freezes whole on departure; its `(vehicle_id, day)` pairs enter
  `reserved` for the tour's full span.

The three primitives already exist: `reserved` (`tour_plan.py:108`), `avail_overrides`
(`trunk.py:48`), `delivered_order_ids` (`handover.py`).

### 4.4 `epoch_state.py` — the vehicle pool

For each `(vehicle_id, day)` the epoch state carries **five** fields derived from the
frozen prefix. Omitting the last two is a silent capacity leak that biases E6
optimistic.

| field | value | why |
|---|---|---|
| `available_from` | frozen trip's `depot_return.planned_arrive` | vehicle is back |
| `at_node` | home depot | whole-tour ownership guarantees it |
| `shift_end` | unchanged | absolute clock; envelope shrinks automatically |
| `drive_since_break` | frozen prefix's `end_drive_since_break` | **else the driver gets a fresh 4.5 h** |
| `max_drive_minutes` | `MAX_DRIVING_H_PER_DAY*60 − consumed` | **else a fresh daily driving cap** |

`evaluate_day` currently hardcodes `carry = 0.0` and `total_drive = 0.0`
([routing_adapter.py:355](../../freight_planner/routing_adapter.py)). It gains a
`drive_since_break_carry: float = 0.0` parameter. Default 0.0 ⇒ existing behaviour
bit-identical.

A vehicle whose frozen trip returns after `shift_end` falls out of the pool for that
day with no special-casing. A vehicle inside a frozen tour never enters the pool.

### 4.5 Horizon

Receding to the **end of the current weekly window**. Not a constant 7-day roll:
orders are day-pinned, so a constant roll would pull next week's demand in and break
comparability with E1 and with the weekly odometer baseline. The week survives because
tours, the nightly trunk, and cross-day vehicle allocation need it.

Its far end is informationally thin (14.0% of the week's collections exist at Monday
04:00). That is a reported result, not a defect.

### 4.6 Slip rule and service ledger

An order's **target day** is its `origin_date`. Every collection resolves to exactly one
of:

* **`ON_TIME`** — collected on its target day.
* **`SLIPPED(n)`** — carried into the next operating day's pool, with `n` counting the
  days late. **An order may slip more than once**: if day D+1 cannot fit it either, it
  re-enters D+2's pool, and so on until the window ends. Reality does the same — 19.1%
  of afternoon-booked collections are collected 2+ days after booking. For
  `PL_EXPORT` / `LOCAL_COLLECT` (622 of the 813 slip-set orders, 76%) nothing
  downstream of ours moves; the freight catches the next night's trunk.
  **For `FULL_FLEET` the DELIVERY PROMISE IS FIXED (amended 2026-07-10,
  stakeholder rule):** a slip consumes collection float first —
  `new_origin_date = origin_date + n`, `destination_date` unchanged while the
  new collection day is at or before it. When collection lands ON the delivery
  day, the legs builder's same-day option set (DIRECT collect-and-deliver, or
  same-day xdock) is the natural recovery mode — consolidation degrades to
  direct as float runs out, exactly as an operator would run it. Only when the
  float is exhausted does the delivery move with the collection
  (`dest = max(dest, new_origin)`) — an explicit promise break, not a silent
  re-date. (This supersedes the earlier collection-anchored re-dating, which
  described observed slip behaviour, not the planner's obligation.)
* **Aging priority.** A slipped order enters the next day's seed **ahead of that day's
  fresh work** — yesterday's failure is today's first job. This is how every real
  depot triages, and it prevents the solver from perpetually deferring the same
  awkward order to protect its km.
* **`UNSERVED`** — could not be fitted by the end of the window. A collection still
  unserved after **Saturday** has nowhere to go and is counted `UNSERVED` rather than
  carried cross-window; the volume is small and this is the conservative treatment.

Every `SLIPPED(n)` and `UNSERVED` order is tagged with a **failure reason**:

* `NO_EPOCH` — booked after the last epoch that could reach it. Commitment-bound.
  Overtime is irrelevant; only diversion recovers these.
* `DUTY` — visible in time, but the shift envelope or driving cap had no room.
* `CAPACITY` — no vehicle with spare pallets/weight.
* `NO_VEHICLE` — no compatible vehicle in catchment.

This instrumentation is the whole point of not pre-building an overtime valve: if
failures are dominated by `NO_EPOCH`, the overtime question is closed by evidence. If a
material share return `DUTY`, we have discovered that the afternoon wave overflows the
shift envelope, and *that* motivates an overtime sensitivity as follow-up.

### 4.6a Day close — the non-solver decision (AMENDED 2026-07-10)

Run-1 falsified the original plan of committing tonight's trunk from the noon
solve: at that epoch the solver can see neither the morning's already-frozen
exports nor tomorrow's imports (which reveal at 18:00, and trunk sizing charges
a day-N+1 delivery to night N) — the committed trunk halved vs E1 (15-16 trips
vs 35), an optimistic bias. The corrected model adds a fourth daily moment that
is **accounting, not optimization**: at the 18:00 day close the dock is fact,
and tonight's trunk is sized from (a) export legs actually frozen today plus
(b) tomorrow's revealed import manifest, with tractors drawn against tour
reservations (`day_close_trunk` in run_rolling). The per-epoch solver's
internal trunk remains a planning estimate only. The pool roll (§4.6) happens
at the same moment. Also fixed from run-1 evidence, stakeholder-diagnosed: a
vehicle whose single committed trip froze was being retired for the whole day;
spec §4.4 semantics now apply to EVERY frozen vehicle-day (return to pool at
depot-return with duty carry; reserve only when exhausted).

### 4.7 Accumulation and emission

Frozen trips accumulate into a merged `routes: dict[(vehicle_id, day), list[trip]]`.
After the last epoch, `build_plan_records(...)` ([plan_records.py:28](../../freight_planner/plan_records.py))
emits once over the union. The KPI, validation, viz and `month_summary` stacks then
work unchanged.

`run_rolling.py` additionally emits `rolling_manifest.json`: per epoch, the visible
order count, trips frozen, trips committed, and wall time.

### 4.8 `solve_window()` extraction

The load-bearing refactor. `run_alns.main()` (~200 lines) becomes:

```
main()            # argparse -> SolveConfig -> solve_window -> emit
solve_window(cfg: SolveConfig, *, epoch_state: EpochState | None = None) -> SolveResult
```

Additive only. Gate: the existing 548 tests, plus a **bit-identical check** of one E1
window (`2026-01-12`, seed 0) against its stored `selected_plan_alns.csv`.

---

## 4.7a THE DYNAMIC DISPATCHER (approved 2026-07-10; supersedes the strict-only design)

Stakeholder decision: the strict-freeze campaign is CANCELLED as a standalone
arm — the target is the modern dynamic model directly (batch seed + rolling
horizon + insertion into in-flight routes + freeze periods). Strict behaviour
survives as a degenerate CONFIG (watermark pinned to trip end), not a run
commitment, so the floor measurement stays available for free.

### Commitment watermarks (replaces whole-trip freezing for daily work)

Per in-flight `(vehicle, day, trip)`, the **watermark** is the last stop index
the driver has begun by `t + δ_R1`, where **δ_R1 = solve wall + driver
notification + contingency margin** (deployability against drift; forbids
razor-thin diversions). Everything at or before the watermark is fact.
Everything after is the OPEN SUFFIX:

* the solver may INSERT newly-revealed orders into it and RE-SEQUENCE it;
* suffix stops stay PINNED to their vehicle in v1 (onboard freight physically
  cannot change trucks; cross-vehicle reassignment of not-yet-loaded suffix
  stops is deferred until data demands it);
* every candidate change re-evaluates the WHOLE trip from its original depot
  departure with the standard evaluator — the untouched prefix reproduces
  identically, so duty/breaks/capacity/shift-end stay exact with NO new
  evaluator features (no mid-route vehicle spawning, no initial-load support);
* destroy operators must never remove at-or-before-watermark stops; repair may
  only place stops whose planned arrival is ≥ now + δ_R1.

In-flight trips are INJECTED into each epoch's solve as part of the initial
solution (improve_existing_solution already accepts source routes). A vehicle's
later trips remain fully free — multi-trip pool return is therefore automatic
via evaluate_day, retiring most of the strict design's DutyOverride pool
machinery for daily work (it remains for trunk next-day and handover holds).

Departure commitment keeps the §4.3 expiry rule (a trip departing before the
next decision point commits now); the watermark then advances WITHIN committed
trips as the simulated clock passes stops. Multiday tours and the shuttle keep
their standing treatment; tours freeze whole in v1.

**Stability guard:** a per-modification penalty on suffix changes (default
small; 0 = pure cost-greedy), reported alongside the churn metric so plan
nervousness is priced, not free.

### Micro-passes (R2, built together with R1 — required for post-noon arrivals)

Between anchor epochs, an INSERTION-ONLY pass runs on a cadence (default every
30-60 min of simulated time, configurable): no destroy, no re-sequencing beyond
the insertion, just repair of accumulated newly-revealed orders into open
suffixes and unstarted trips, respecting watermarks + δ_R1. This is what
serves an order booked at 14:00 the same afternoon — R1 alone has no decision
point after noon.

### Later stages
* **R3 — delayed-dispatch scoring** under the FIXED delivery promise (§4.6):
  holding consumes collection float; recovery degrades toward DIRECT; never
  promise-eating.
* **R4 — live ingestion / event triggers**: deployment tier, parked
  (stakeholder call) — in simulation the plan's own clock IS the position feed.

## 5. Experimental protocol

Identical to E1 in every respect except visibility and commitment.

* **Windows:** `2026-01-12` (mid-volume, the telematics-validated window) and
  `2026-01-26` (peak).
* **Seeds:** 0, 1, 2.
* **Iterations:** 10,000, fixed. `--time-budget 100000 --no-improve 100000` so neither
  early-stop gate can fire.
* **Router:** OSRM durations, default on.
* **Handover:** each window chains from its E1 predecessor's `handover.json`, as E1 did.
* **Compute:** 18 epochs × 2 windows × 3 seeds = 108 solves. Later epochs solve a
  strictly smaller problem. Estimate ~6–7 h at six-way parallelism.

**Orphan safety.** Launch the full parallel shape from the start. Never stop-and-hand-off
mid-run. To kill: parent shell first, then verify the PID set is empty. `TaskStop` does
not kill the process tree.

---

## 6. Metrics

Reported against E1 on the same windows and seeds.

1. **Δ planned km** (total, per served order, and **split tour / trunk / daily** — the
   pessimism should concentrate in under-filled tours committed at low visibility, and
   the split shows where the cost actually lands instead of letting a reader assume it
   is spread evenly).
2. **Δ vehicle-days** (distinct `(vehicle_id, service_date)` with activity).
3. **Service ledger**: `ON_TIME` / `SLIPPED(n)` (slip-days distribution, not a binary)
   / `UNSERVED`, each split by failure reason, by flow, and by **origin cluster** (the
   late-booking mass sits at near-depot and standing-service shippers, and the ledger
   should show it).
4. **Commitment profile**: trips frozen and committed per epoch.
5. **Behavioural validation**: E6's realised P(same-day collection | booking hour)
   against reality's curve (§2). If E6 slips roughly the freight reality slips, at
   roughly the hours reality slips it, the simulation is behaviourally calibrated
   rather than merely plausible.
6. **Plan stability**: share of uncommitted stops reassigned (vehicle or day) between
   consecutive epoch plans. Operators judge dispatchability by whether the plan stops
   moving; a plan that reshuffles Thursday three times a day is undispatchable
   regardless of its km. Computed from per-epoch plan snapshots.

---

## 7. Pre-registered predictions

Recorded before the run, so confirmation is informative.

* **First-attempt slip set ≈ 682 owned-collection orders (6.9%, ~85/wk).** The raw
  post-noon set is 813 orders (8.2% of 9,892; 613 of the 9,067 pickup stops = 6.8% at
  stop level), minus the 131 shuttle-address orders that ride the standing service
  under the §4.1 exemption. A slip occurs iff an order is booked after the last epoch
  that could reach it, i.e. after 12:00 on its target day.
* **Slip origins cluster at near-depot shippers** (MK42, CB97, CB25 lead the set), so
  the residual pessimism vs a diverting dispatcher is mostly short-hop freight — cheap
  in km even when late.
* **Failures dominated by `NO_EPOCH`.** A material `DUTY` share would be a finding.
* **Δ km > 0 and Δ vehicle-days ≥ 0.** E6 cannot beat E1: it solves the same instance
  with strictly less information and strictly more constraints.

If E6 slips ≈7%, the mechanism is confirmed and the fleet has afternoon headroom. If it
slips ≫7%, the fleet cannot absorb the afternoon wave without foresight — itself a
result.

---

## 8. Deployability corollary

δ upper-bounds the solve budget. The budget sweep already showed 120 s yields 85,939
planned km against 75,457 at 1800 s (a 12.2% quality gap), so the 120 s production
budget is not deployable-quality. **The 1800 s plateau is 30 minutes and fits inside a
60-minute lag; 3600 s does not.** The quality we report is therefore achievable inside
the dispatch window — demonstrated, not assumed.

---

## 9. Testing

TDD, per standing rules. No git commits; checkpoints instead.

* `visibility.py`: collection before/after epoch; delivery day-before boundary at 18:00;
  placeholder `origin_timestamp` never consulted; unknown flow excluded.
* `epoch_state.py`: `available_from` from depot return; `drive_since_break` carried;
  `max_drive_minutes` reduced; vehicle returning after `shift_end` drops out; tour span
  reserved.
* `routing_adapter.evaluate_day`: carry-in default 0.0 is bit-identical; a non-zero
  carry triggers a statutory break earlier; a reduced `max_drive_minutes` triggers
  `DRIVING_CAP`.
* `run_rolling.py`: two-epoch toy window where a post-cut-off order slips exactly one
  day and is tagged `NO_EPOCH`; a frozen trip's km appear exactly once in the merged
  plan; an order that misses twice reports `SLIPPED(2)`; a slipped order outranks
  fresh same-day work in the next seed (aging priority); Saturday slip is `UNSERVED`.
* `visibility.py` exemption: a shuttle-carved job booked after the epoch is still
  servable and tagged `SHUTTLE_STANDING`; a non-shuttle job at the same address is
  gated normally.
* Stability metric: reassignments counted correctly across two synthetic epoch
  snapshots (vehicle change, day change, unchanged).
* **Bit-identical gate**: E1 window `2026-01-12` seed 0 reproduces `selected_plan_alns.csv`.

---

## 10. Limitations, to be written down separately

Three distinct threats, deliberately not conflated.

1. **Foresight (what E6 addresses).** Strict commitment excludes en-route diversion, so
   E6 is an **upper bound** on the cost of non-anticipation and E1 a lower bound. The
   commitment audit locates the truth: 70.0% of collection stops are clean under either
   rule; only 19.5% turn on diversion. **The pessimism concentrates in multi-day tour
   formation**: a Monday-morning tour freezes whole having seen ~14% of the week's
   collections, where E1 consolidated the whole week's far work — no real planner
   commits a tour blind; they hold far work because they know their recurring lanes.
   E6's zero-forecast dispatcher is dumber than any real one, which is what makes the
   bound honest. The tour/trunk/daily km split (§6.1) shows where this lands; tour
   departure deferral is one line of future work.

2. **Day assignment (pre-existing, not introduced by E6).** `origin_date` is an
   execution outcome, and both E1 and E6 consume it identically as the target day.
   The contrast is therefore clean, but neither run forecasts what a planner would do
   with a customer-requested date, which does not exist anywhere in the data.

3. **Epoch-grid coarseness.** Commitment really happens per-trip at `departure − δ`;
   three epochs approximate that. Orders booked between epochs can only ride the next
   committable wave, where a real dispatcher might have loaded them sooner. This biases
   E6 **pessimistic** and is bounded by the grid spacing.

Supporting disclosures: `timestamp_created` is a back-office record time that lags the
real booking, so every foresight figure quoted is conservative. E6 slips 100% of
non-exempt post-noon bookings where reality slips 77% at 13:00 and 90% at 14:00, an
excess pessimism of roughly **1.4% of collection stops**. The vehicle pool is
re-decidable at every epoch whereas real driver rosters are fixed the day before —
controlled, since both arms share the same fleet envelope. A slip is priced in days
only, not in the customer-relationship cost of a missed collection. PL_IMPORT
visibility at 18:00 day-before is conservative — network EDI manifests can arrive
earlier.

---

## 11. Provenance

E6 introduces new code, so it opens a **third provenance epoch** after constant-speed
(`70d5253a`, E3) and OSRM (`21564100` + `code_snapshot_osrm.patch`, E1).

Before the first E6 run: snapshot `git diff` of the E6 working tree to
`freight_planner/experiments/code_snapshot_rolling.patch`, record the parent SHA in
`PROVENANCE.md`, and record epoch grid, δ, iterations, seeds and windows in
`E6_rolling/README.md`. **Never compare results across epochs.**

E1's numbers remain valid: `solve_window()` extraction is behaviour-preserving and
gated bit-identical, and `evaluate_day`'s new parameter defaults to the current
behaviour.

---

## 12. Effort

| item | est. |
|---|---|
| `visibility.py` + tests | 0.5 d |
| `epoch_state.py` + tests | 1.0 d |
| `solve_window()` extraction + bit-identical gate | 1.0–1.5 d |
| `evaluate_day` carry-in + tests | 0.25 d |
| `run_rolling.py` + accumulation + slip pool w/ aging + tests | 1.75–2.25 d |
| E6 harness, analysis (incl. stability + km split), figures | 0.75 d |
| **total** | **~5.25–6.25 d** |

Compute ~6–7 h. Deadline 3 Aug 2026.

**Fallback.** The commitment audit (§2) and the noon cut-off are already computed and
make a strong threats-to-validity section on their own. E6 upgrades that section from a
defence into a contribution.
