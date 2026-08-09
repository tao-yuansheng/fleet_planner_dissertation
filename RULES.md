# The Rules — what this system must obey, and must never do

Compiled 2026-07-14 from the CURRENT code, one rule per enforcement point, so gaps are
findable. Each rule names its enforcement site and how it is verified. `Δ` = `delta_r1`
(90 min): the commit lag between deciding and a driver executing. "Decision" = any epoch:
00:00 midnight seed, micro passes every `config.MICRO_EVERY_MIN` minutes (default 30) within
06:00–18:00, 12:00 warm re-opt, 18:00 close. Narrative walkthrough of the mechanisms behind these rules:
`README_DYNAMIC.md`; this file is the authoritative contract.

## A. Knowledge — the plan may only use what exists

- **A1. An order is invisible until booked.** No epoch may see, place, or reason about an
  order whose `timestamp_created` is after the epoch. — `visibility.visible_order_ids`;
  every anchor/micro filters through it.
- **A2. A collection can never be SERVED before its order was booked.** Each collection
  leg's `earliest_start` is floored to the order's creation time — a per-order fact that
  survives every re-plan. — `_floor_collection_earliest_to_creation` (run_alns);
  emission-audited by `audit_non_anticipation` (temporal violations must be 0).
- **A3. A plan computed at t may not dispatch anything before t + Δ — and "dispatch"
  includes the DRIVING.** Idle vehicles' depot departures are floored (`dispatch_floor`
  → `dispatch_floor_ov`, applied to micro solves, warm anchors, AND snapshot emission);
  micro arrivals get `earliest_start = floor`; and on launched trips the deviation
  point itself is floored (B2 departure-based flooring, 2026-07-14 evening).
- **A4. Nothing is ever planned in the PAST of its deciding epoch.** Trunks: an epoch may
  not re-draw past nights (`trunk_from` filter). Tours: day-1 is clamped to the anchor's
  day (`trunk_from` clamp in `tour_plan._assign_one`, Fix 8) — a member due yesterday is
  served late and accounted SLIPPED, never backdated. Daily: a rolling epoch's candidate
  frame is day-clamped (`_clamp_past_candidates`, 2026-07-14 — stale-dated unserved legs
  are not plannable; committed legs keep their rows for injected-trip metadata), and the
  ALNS floor guard arms for any today-or-past key even without a watermark entry.
  Emission-audited at BOTH levels:
  order-level (`audit_non_anticipation`) AND route-level (`audit_route_backdating`,
  2026-07-14: every tour row's date >= its creating seed's day, every daily stop's arrive
  >= the epoch that first placed its job — the gate that would have caught Fix 8's class).
  **Gap closed 2026-07-28:** `route_seed.rebuild_daily_routes_after_drop` (the post-drop
  re-time after `drop_orphan_deliveries`/`drop_superseded_option_legs` remove a leg) used a
  bare vehicle profile with no per-job floor awareness, so a SURVIVING stop could re-time to
  arrive before the epoch that placed it (WT262812/Y90RNW/2026-02-02: corrupted 18:03→10:06,
  a real audit violation the gate above correctly caught). Fixed by threading each surviving
  job's own dispatch floor (`job_floors`, from the rolling loop's `placement` trace) into the
  rebuild via a `_FloorOverride` proxy reusing the existing `no_early_arrival` mechanism —
  confirmed 0 violations in a full re-run.

## B. Commitment — a promise made is a promise kept

- **B1. A trip LAUNCHES (becomes in-flight) when its departure enters the horizon**
  (`next decision + Δ`), priced off the LIVE plan — micro-inserted trips launch exactly like
  seed trips. — `expire_commit` + `live_departures` overlay at every heartbeat.
- **B2. A launched trip's committed PREFIX is inviolable; its suffix opens only beyond
  the Δ window — DEPARTURE-BASED flooring** (tightened 2026-07-14 evening, user rule,
  WT255677/FJ72XFF: flooring arrivals alone is structurally leaky — an order 100 min of
  driving away always ARRIVES outside the 90-min freeze, yet the truck must start
  driving toward it inside it). A suffix change is legal only if the trip's **deviation
  point** — the last committed stop's departure, the first moment the driver's remaining
  plan changes — is itself ≥ the Δ floor, AND every suffix arrival is ≥ the floor (each
  later suffix drive then chains compliant), AND nothing is inserted after a DEPARTED
  prefix (the now-guard, the WT/bu20vhy rule — subsumed by the departure floor whenever
  now ≤ floor, kept as defense in depth). Committed stops themselves never move (B3).
  Practical meaning: within `now + Δ`, neither the driver's stops NOR his planned
  drives may change; a launched trip is top-uppable only while ≥ Δ of ground remains
  before the insertion point. — `floor_ok` (the single guard every insertion door
  shares); the earlier open-suffix form (arrival-floor only, reconfirmed after the
  full-immutability revert) is superseded by this rule.
  **Depot-hold corollary (2026-07-16, trip-wide since 2026-07-17):** the floor must not
  FORCE fresh activations. A micro arrival carries its floor ON the job
  (`RouteJob.depart_floor`, stamped by `new_arrival_meta`; collocated depot-deliveries
  carry theirs from leg emission); `evaluate_route` holds the vehicle AT THE DEPOT until
  the LATEST floor of ALL jobs in the trip (route_start = max floor, no curb idle) —
  depot-loaded freight boards at departure wherever its job rides in the sequence, so
  lead-job-only reading silently lost the hold for mid-trip jobs. Job-carried means
  snapshots and emission re-derive the same held departure with no per-call wiring.
  **Suffix-insertion guard:** because the floor now re-times the WHOLE trip, a floored
  job may not join a LAUNCHED trip's open suffix if that would move the committed
  departure (`_retimes_committed_departure` beside `floor_ok`; `floor_ok` alone only
  checks the re-timed deviation point against the floor, not that it stayed put) — the
  job falls through to a fresh trip, exactly the Jan-13 reuse shape.
- **B3. Committed stops only ever delay; they never move vehicles or vanish.** Watermarked
  prefixes are pinned against destroy; departed trips' jobs are pinned wholesale (freight is
  on the truck). — `committed_job_ids` pinning, Fix 7; board-verified: 0 frozen-window
  vanishes/null-times.
- **B4. A committed vehicle-day is forever evaluated under the context it was built with.**
  Launch captures the build override (`micro_ctx` → `inflight_ctx`); later views may not
  re-floor or re-time it (phantom overrides are deleted, not merely ignored). —
  `_built_ctx`/`_commit_ctx`, `apply_commit_ctx`.
- **B5. Tours freeze the moment their first departure enters the horizon — at ANY
  decision** (not just anchors), from the last seed's records; a frozen tour's orders leave
  the candidate universe and its vehicle-days are reserved everywhere (daily seed, ALNS,
  trunk draw). — `_freeze_due_tours` (Fix 9). A frozen tour is never re-planned.
- **B7. Tours are BORN only at 00:00 midnight seeds — by design** (user-confirmed 2026-07-14).
  Micros and warm anchors never create a new multi-day tour; the only intraday tour
  lever is attaching a late-booked far order to an existing tour's un-departed tail
  WITHOUT adding tour days (`TOUR_ATTACH_ENABLED`, built + TDD'd, default ON since
  2026-07-16 — coverage-safe: only ever serves an otherwise-unserved far order). A far
  order booked mid-morning can free-ride an in-flight tour when one fits; otherwise it
  waits for the next seed and its lateness is accounted SLIPPED — the intended cost, not a defect.
- **B6. The snapshot tells the truth at its own epoch.** Persisted per-epoch plans reflect
  post-heartbeat commitment (a trip inserted-and-launched at 10:00 is committed=1 in the
  10:00 snapshot) and are timed under each key's build context — a timing failure warns
  loudly, never silently blanks. — `_snapshot` after the heartbeat with a refreshed view.

## C. Physical honesty — freight and trucks exist in one place

- **C1. Freight is conserved.** Every order resolves to exactly one manifest outcome
  (ROUTED / accounted / unassigned-with-reason); a destroyed-infeasible day is re-validated,
  and silent record drops raise (B16 guard, `FP_ALNS_CONSERVE`). Ledger violations must be 0.
- **C2. One vehicle, one place.** No vehicle-day may carry both tour and daily work
  (tour spans are reserved before daily planning; verified: 0 overlaps). Duplicate job ids
  inside a vehicle-day raise at creation (`_assert_no_dups`).
- **C3. A delivery needs its pickup, and a freight uses ONE mode.** XDOCK deliveries depend
  on their collection (`REQUIRES_PRIOR_PICKUP`); an order whose delivery is placed but whose
  collection never runs must not read ON_TIME. For same-day FULL_FLEET both option groups
  (DIRECT, and the XDOCK pickup+delivery pair) flow to the optimizer, but **at most ONE group
  may serve a freight** — no freight is both moved-direct and collected-to-depot. Enforced by
  `option_mutex.OptionMutex` in the seed and ALNS, and the `ledger.drop_superseded_option_legs`
  commit-boundary backstop at emission (2026-07-23, endogenous DIRECT/XDOCK; the static
  `ρ = 1.6` resolver was deleted).
  Fixed-tour assignments and the daily seed now share one option claim: a tour permits
  same-group feeder legs in the daily seed but blocks the rival before commitment.
  Final cleanup remains a defensive integrity check, not the normal cross-planner resolver.
  **Exception:** the backstop never drops a leg that was ever watermark-committed (locked to
  a driver) — silently reassigning an already-promised job breaks the freeze guarantee, so a
  conflict here is logged (`OPTION CONFLICT`) and left unresolved rather than fixed at a
  driver's expense (2026-07-28, R888GNW/2026-02-02).
  **Root cause closed 2026-07-28 (the exception above should now be rare-to-never):**
  `insertion_pass` (the E6 micro-pass) had ONLY `_supersede_pending`'s within-batch dedup — no
  mutex check against jobs already resident in the current solution from an EARLIER epoch. A
  freight's XDOCK alternative (a different job_id than its DIRECT leg) could ride in on a later
  micro pass with no memory that the option_set was already resolved, producing exactly the
  double-commit the exception above exists to catch. Fixed by threading an `option_index`
  (job_id -> (option_set, option_group)) into `insertion_pass`, seeding an `OptionMutex` from
  the current solution before considering any new candidate — the same invariant the seed/ALNS
  already had, extended to the one path that lacked it.
  **Separately, DIRECT never got a genuine cost comparison at all:** `route_seed.py`'s
  `_DEP_RANK` always processes XDOCK's pickup (rank 0) before the same freight's DIRECT leg
  (rank 1), so the seed's mutex claim was pure insertion order — DIRECT was rejected
  `OPTION_SUPERSEDED` before its cost was ever priced. `OPTION_SUPERSEDED` was missing from
  `alns._REPAIRABLE_REASONS`, so the rejected side never reached ALNS's `option_swap` operator
  either — DIRECT was permanently dead the instant XDOCK claimed the set, real cost or not.
  Fixed by adding it to `_REPAIRABLE_REASONS`. Real-data effect (192 option sets, Feb 2-3
  backtest): 0/192 chose DIRECT with only the race fixed; 12/192 once DIRECT got a fair shot.
  **Audit visibility closed 2026-07-28:** the `OPTION CONFLICT` count above previously reached
  only the runlog text line — not `feasibility_audit.csv`, not `validation_metrics.json`, no
  structured artifact — so a real occurrence in a campaign run would never surface in a §6.1
  0-violation table built from that CSV. `run_alns.emit_outputs` now returns
  `(rc, option_conflicts)`; `run_rolling.py` threads the count into
  `feasibility_audit.augment_with_dynamic_audits`, which adds an `option_conflicts` column to
  `feasibility_audit.csv` / `09_feasibility_audit.md` alongside `non_anticipativity` and
  `route_backdating` — all FOUR correctness-audit families now read 0 in one place.
- **C4. Freight readiness gates service.** Depot legs can't run before the freight is at
  the depot (ledger states; staging depot rules); the trunk cannot depart before its last
  feeding collection returns + consolidation buffer.
- **C4b. The night hub trunk is EXPORT-ONLY.** The nightly depot↔hub trunk is sized on
  PL_EXPORT pallets alone (`ceil(export / TRUNK_DECK_PALLETS)`); its trip count is never
  driven by imports. Network IMPORT freight arrives at the depot via the unmodelled
  "invisible hub" resource we do not own (treated as spawning at the depot) and NEVER
  charges a trunk trip. The tractor still round-trips and returns empty (km unchanged; only
  trip count is export-driven). The removed `TrunkNight.import_pallets` field and the
  `import_pallets` column of `trunk_schedule.csv` reflect this — imports are not trunked.
- **C5. Capacity and duty are hard; per-vehicle shift walls are GONE.** Per-trip pallet/kg
  caps use vehicle-master physical capacities (PEAK load on multi-stop trips, not sum). A
  driver's day is bounded by BOTH legal caps at every evaluation — seed, ALNS insertion,
  AND suffix insertion into a launched trip: **13h duty PER CHAIN** (`DUTY_CAP` in
  `evaluate_day` since 2026-07-16 — a depot gap ≥ `SPLIT_SHIFT_GAP_H` (3h) ends a chain,
  the driver rests or swaps, so a morning vehicle can legally run a held evening trip
  while no single working stretch exceeds 13h; before this the duty bound rode on the
  now-deleted telematics shift wall) and **10h driving WHOLE-DAY**
  (`MAX_DRIVING_H_PER_DAY` → `DRIVING_CAP` across all trips, break accumulator carried,
  frozen-trip drive deducted via `duty_after_freeze` — EU daily driving does not reset on
  a short rest). EU drive-break arithmetic (45 min per 4.5 h) in both daily and tour
  evaluators; tour days carry the same 13h/10h caps (`TOUR_DAY_ELAPSED_CAP_MIN`,
  `_DAY_DRIVE_CAP_MIN`).
  **2026-07-16 (user rule): telematics behavior numbers are NOT operating constraints.**
  The fleet works one operating day — every vehicle available from **06:00**, no
  per-vehicle `shift_end` wall, and **no trip-count cap** (κ_v = max(2, median) REMOVED
  from the seed, both ALNS insertion enumerators and the repair pass — duty/driving/
  window feasibility is the only per-day limit). `shift_start`/`shift_end`,
  `median_trips_per_day`/`multi_trip_day_pct` and the four `capacity_*_per_trip`/source
  profile columns are REMOVED from the vehicle master — `payload_kg`/`pallet_capacity`
  are the capacity truth (`_resolve_capacity` always made them win; the profile numbers
  sat beside them contradicting physics). A blank shift_end builds NO wall —
  `evaluate_day`'s SHIFT gate stays only for explicitly-set synthetic frames. Why: the
  medians were *descriptive* (the middle of observed days) misused as *prescriptive* —
  half of a vehicle's real days ended later than the wall we enforced, so paid-for
  afternoon capacity was refused and fresh activations bought instead (Jan-13 trace:
  HX17CVV median-walled at 17:02 with an observed P75 end of 18:25 / P90 span 14h).
  **19:00 is a soft target, not a wall — service coverage comes first**; late running is
  bounded by the duty/driving caps and PRICED by the overtime + fairness cost (SHIPPED
  same day, spec 2026-07-16): the driver-day cost = paid 9h floor + payroll OT (×1.5
  beyond the floor, working hours only — split-shift depot idle is unpaid) + an
  unsocial-hours premium that RAMPS past 19:00 (×1.5 rising +0.25/h → ×2.0 at 21:00).
  The ramp makes late cost QUADRATIC per vehicle-day, so spreading evening work across
  active vehicles is strictly cheaper than piling it on one driver — fairness emerges
  from the objective (user probe: 1h+1h beats 2h-on-one whenever geometry is equal; a
  genuine km saving still consolidates, `LATE_RAMP_PER_HOUR` is the exchange rate).
  Serve-first stays lexicographic: the premium shapes WHERE late work lands, never
  WHETHER an order is served. `--no-overtime-cost` = the straight-time ablation.
  DEFERRED (phase 2): cross-day evenness via a late-hours ledger in handover.json —
  build only if week runs still show one vehicle hogging evenings.

## D. The solver — bounded, honest optimization

- **D1. The search may only improve what is UNCOMMITTED.** Destroy operators skip pinned
  jobs; insertion respects B2; excluded vehicle-days (tours/reserved) are untouchable.
  EVERY insertion path carries the watermark + floor guards — including the post-ALNS
  merge sweep (2026-07-14, WT255131: the sweep was the one unguarded door and top-upped
  a departed trip) and keys without a watermark entry whose day is on/before the floor's
  day (`_floor_guard_active` — a past-day key never launches, so absence of a watermark
  must arm the guard, not disarm it).
- **D2. Coverage outranks distance.** Acceptance key is (served, −cost); the convergence
  gate counts a served increase as improvement regardless of km.
- **D3. Runs stop when improvement stops.** Default: < `ALNS_CONVERGE_PCT` (0.05%) gain
  over `ALNS_CONVERGE_WINDOW` (500) iterations after `ALNS_CONVERGE_MIN_ITERS` (1,500) —
  `--iterations` is a cap; `--converge-pct 0` restores fixed budgets (provenance replays).
- **D4. km is physical, cost is the objective.** Reported km comes from real road
  distances (OSRM, cached); the optimizer may accept km-increasing moves only when
  generalized cost (per-type GBP) improves.

## E. Data — single sources of truth

- **E1. Orders = the monthly ENRICHED parquet** (raw universe + embedded verified_leg);
  `verified_legs.csv` is the regen artifact and runtime fallback. The verified leg is the
  OWNERSHIP truth and overrides the raw API flow tag.
- **E2. Fleet = vehicle_master.csv, alone.** Depot & fleet-kind from CircuitName (baked by
  the build tool), dispatcher profiles included; supatrak list and profiles JSON are regen
  inputs only. Config keys vehicles by RAW AssetName (one reg contains a space).
- **E3. Every data path derives from ONE anchor** (`shared.paths.LOGISTICS_ROOT`). A
  relocated module must fail loudly, never silently read nothing (the partb6 lesson —
  a `__file__` grep of shared/ and tools/ must return only paths.py).
- **E4. verify_legs never runs concurrently with a planner run** (it overwrites a planner
  input). Regen chains are acyclic: tools read raw sources, never `shared/config`.

## F. Reproducibility & verification

- **F1. Byte-reproducibility requires**: iteration-bound runs (no wall-clock cutoffs)
  and OSRM/postcode cache snapshots. Hash-seed sensitivity RESOLVED 2026-07-14 by
  controlled A/B (PYTHONHASHSEED 0 vs 7; inputs, seed, and 2,000-iteration full-CLI
  static runs, cache-restored): every plan artifact byte-identical — only wall-clock
  timestamps differ. The historic instability was the wall-clock budget + cache
  mutation, misattributed to hash order. `PYTHONHASHSEED=0` stays in the protocol as
  free defense-in-depth (the rolling path was not separately A/B'd).
- **F2. Every run must end with**: temporal violations = 0, ledger violations = 0, every
  order accounted in the manifest. Non-zero is a stop-the-line defect, never a warning.
- **F3. Migrations are gated byte-identical** (static + rolling) before they count as
  refactors; behavior changes ship flag-gated or default-documented with the old behavior
  reachable (e.g. `--converge-pct 0`).

## Known gaps

NONE OPEN as of 2026-07-14 (late evening).

*Same-day tightening (WT255677, user-caught on the smoke board):* the original B2
floored suffix ARRIVALS only — structurally leaky (a far order's arrival always clears
the window while its drive starts inside it; FJ72XFF got new work 47 min before wheels
turned). Fixed the same day: `floor_ok` now also requires the deviation point (last
committed stop's departure) ≥ floor — B2 is departure-based. Verified by re-smoke. The route-backdating finding the new gate caught on
its first full run (4 daily stops planned in the past) was root-caused and FIXED same day:

- *(b) the merge sweep was the one unguarded insertion path* — NOT stale context as first
  suspected: the noon warm anchor's post-ALNS same-address sweep top-upped 81a2457f onto
  bd5beb0c's MK41 0LF stop inside a fully-departed trip (every ALNS path had refused).
  `apply_zero_cost_merges` now takes `watermarks`/`commit_floor`/`now`: merges insert only
  after the watermark and must clear `floor_ok` on the merged day (FLOOR census bucket).
- *(a) past-day placement* — seeds placed still-unserved due-yesterday candidates onto
  YESTERDAY's vehicle-days (never-launched keys carry no watermark, so no guard armed).
  Two layers: `_clamp_past_candidates` drops stale-dated candidate rows at every rolling
  epoch (committed legs exempt — injected trips keep their metadata), and
  `_floor_guard_active` arms the ALNS floor check for any today-or-past key even without
  a watermark entry.

Verified: the fixed smoke (strict mode) reports `route-backdating audit: 0 violations`;
the ledger and the plan now agree on every previously-contradictory order (81a2457f
served honestly same-day at 16:06; f814feb2 genuinely ON_TIME; fd3187a5/f9e4fe38
honestly UNSERVED and absent from the plan).

The original five, all closed 2026-07-14:

1. A4 route-level audit blind spot → `audit_route_backdating` gates every emitted stop
   against its deciding epoch (verified: flags i5000's pre-Fix-8 backdated tour).
2. Hash-seed sensitivity → disproved by controlled A/B (see F1); the pin stays as
   defense-in-depth.
3. "Tours only born at seeds" → intended design, now rule B7 (the intraday lever is
   tour-tail attachment, default ON since 2026-07-16).
4. Open suffix extending the driver's return → bounded, not open-ended: the 13h duty /
   10h driving caps in C5 hold on every suffix insertion (13h clamp added at
   vehicle-state build — three trunk tractors carried 14-15h vehicle-span windows).
5. Trunk draws as bookkeeping → real per-vehicle assignments: `trunk_schedule.csv`
   names the picked tractors + the feasible pool, and the board renders each named
   trip on its tractor's own lane (the separate section survives only for
   unassigned/legacy trips).

New gaps get listed here the day they are found, not after they are fixed.

## Fix pack 2026-07-16 (WT255892 trace)

Root: one order, three verdicts — the tour subsystem delivered WT255892 (Stoke→KY11,
frozen at the Jan-14 03:00 seed, launched 07:00) while the service ledger wrote
`UNSERVED/NO_FEASIBLE_ROUTE days_late=4` and the map drew a teleported Duxford→KY11 run.
All shipped same day, each TDD'd:

1. **Tour serves reach the ledger** — a frozen tour record whose freight ends
   DELIVERED marks the order SERVED outright (`credit_frozen_tour_record`); the finalize
   reconcile unions `tour_served_order_ids(merged_tour_records)` into the plan-served
   set. Trigger was `TOUR_DEPOT_DIRECT_AS_DELIVERY` re-labelling depot-directs as
   `CUSTOMER_DELIVERY`, invisible to every collection-leg scan.
2. **Cross-depot tours materialize the repositioning** — a pick homed off the anchor
   depot re-evaluates from its REAL home with a `DEPOT_LOAD` call at the anchor
   (`cross_depot_tour_eval`); infeasible → same-depot fallback → honest reject. No more
   plans priced from one depot and drawn from another.
   **Pickup corollary (WT254009 rule, 2026-07-16): a tour carrying any
   `CUSTOMER_PICKUP` may NEVER go cross-depot.** The tour returns to its vehicle's home
   WITH the freight, but a pickup's freight belongs at its `target_depot` — a foreign
   vehicle lands it at the wrong depot (the found case: a Bedford-bound 23-pal MK42
   pickup commissioned onto an idle STOKE tractor, hauled ~195 km the wrong way, freight
   ledgered at the wrong depot). Enforced at ALL THREE tour-vehicle pick sites: the seed
   (`_assign_one` skips `cross_depot_tour_eval` when any member is a pickup), intraday
   ATTACH (a depot-bound candidate skips tours homed elsewhere), and intraday
   COMMISSIONING (`_depot_bound_mismatch` filters the idle pool to the target depot;
   `prefer_depot` is now passed so an all-idle pool no longer sorts on capacity alone —
   geography enters the pick). No qualifying vehicle → honest fall-through/reject; the
   order stays for the daily/slip machinery, never lands wrong.
   **Collocated depot-delivery corollary (ST4 8JB rule, 2026-07-17): a same-day
   FULL_FLEET DIRECT whose origin sits within `DAILY_ORIGIN_AT_DEPOT_RADIUS_KM` (2 km) of
   its source depot is EMITTED as a depot-loaded `CUSTOMER_DELIVERY`** (single `:DIR`
   leg, no XC/XD alternative) so same-origin orders co-load into one multi-drop run
   instead of atomic out-and-back arcs (Y888AUK Jan-12: three ping-pong round trips,
   ~469 km, where one ~250 km sweep serves the corridor). The leg carries
   `depart_floor` = collection-open + `COLLOCATED_STAGING_MIN` (30 min; window-open
   anchored, NOT the deadline+90 staging pessimism that made same-day XDOCK
   window-infeasible for exactly these orders) and `depot_bound` = the source depot:
   `evaluate_route` hard-rejects any vehicle homed elsewhere (`DEPOT_BOUND`, one
   unbypassable site like CAPACITY/SHIFT — the daily path otherwise has NO depot
   affinity for deliveries), and the tour candidate paths honor the same field through
   `_depot_bound_mismatch`. `--no-daily-depot-direct-as-delivery` restores the legacy
   atomic emission.
   **Depot-pinning corollary (2026-07-17, A1): the depot affinity is now UNIVERSAL.**
   Under `DEPOT_PINNING` (default ON) every daily leg is emitted with `depot_bound` =
   its depot label — pickups bind to `target_depot` (the freight must LAND there: the
   delivery stages there, the outbound trunk departs there), deliveries to
   `source_depot` (the freight RESTS there: inbound trunk landed it / the pickup
   landed it / opening stock). DIRECT and HUB_DROP never touch a depot and stay
   unbound. A leg may only be served by a vehicle homed at the depot where its freight
   rests; ALL inter-depot movement rides explicitly priced trunk legs. Without this,
   130/618 delivery legs in the 2-day probe teleported cross-depot (freight served by
   vehicles that never visit its depot — unpriced repositioning worst-case 12.4% of
   combined km, one-directionally flattering the plan) and 31/415 pickups landed at
   their vehicle's home instead of the ledgered target. The collocated-reclassification
   ledger identity is the `:DIR` leg-id tail, NEVER `depot_bound` presence (under
   pinning every delivery is bound — the bound-key would flood the collection ledger
   with imports). `--no-depot-pinning` = the teleport ablation.
   **Soft delivery-window corollary (2026-07-18): a delivery time window is a
   PENALTY, not a hard cutoff.** A `CUSTOMER_DELIVERY` past its tight customer
   deadline is feasible with a convex tardiness cost (`TARDINESS_COEF·late²`) plus a
   small earliness cost before the window opens; the solver delivers slightly late
   rather than slipping a whole day. Hierarchy **on-time < early < late <
   slip/unserved**, where slip/unserved is the EXISTING top lexicographic coverage
   tier (served-first) — a very-late same-day delivery beats an on-time next-day one,
   and an order slips only when serving it today is duty-INFEASIBLE (last resort).
   The hard `latest_finish` stays the widened operating/duty bound; the earliness
   penalty never relaxes the hard `depart_floor` / non-anticipation / duty floors;
   PICKUP windows stay hard. The DAY-granular ledger is unchanged (ON_TIME = right
   date); intra-day lateness is reported separately (02_kpi "Delivery timeliness",
   route_stops `minutes_late`). `--hard-time-windows` = the hard-VRPTW ablation arm.
3. **vdu tours are per-day and true-minutes** — records carry the stop's evaluated
   `leg_minutes` (OSRM-aware); `vehicle_day_utilization` books the depot-return residual
   on its own next day when it cannot fit the last stop day (no more 106.9% single rows).
4. **Unassigned is one row per order** — a JOB-level physical reason (e.g.
   MASSIVE_UNSUPPORTED) supersedes the ORDER-level routing outcome; ORDER rows carry
   their order_id.
5. **Operating window 05:00–19:00** — `TOUR_DAY_START_HOUR` 7→5 (user rule: start
   earlier → end earlier); 10 h driving / 13 h duty caps unchanged.
6. **Micros commission idle vehicles** — verified pre-existing in `insertion_pass`
   (eligible-vehicle × day enumeration reaches empty keys; activation priced by the
   vehicle-day cost) and pinned by tests: fresh vehicle-day when reuse is infeasible,
   reload-second-trip when the working vehicle can. NOTE: same-day TOUR-scale orders
   (multi-day geometry) remain seed/tour-attach territory — intraday tour commissioning
   is still an open gap.

## Universe rules 2026-07-16 (user-directed)

1. **Over-ceiling orders SPLIT in every flow** (user decision, revised same day):
   `_split_parts` (multi-vehicle dispatch, parts ≤ 26 pal / 28 t) now applies to
   PL_IMPORT / PL_EXPORT / LOCAL_* as well as FULL_FLEET — a 34-pal import is real
   two-truck work, not MASSIVE. Only HAZCHEM over-ceiling loads never split (one
   sealed consignment): those are the only remaining `MASSIVE_UNSUPPORTED`, and an
   order whose every customer leg is massive leaves the universe (no ledger row,
   never UNSERVED; visible as blocked rows).
   **Scenario C root cause + fix:** the seven 30-34-pal NOT_IN_PLAN FTLs were
   planned, launched AND emitted all along — record minting writes the FREIGHT id
   ('uuid#S1') into record.order_id so the ledger gates per part, and the finalize
   reconcile compared those part ids to PARENT ids and demoted every split order.
   `collection_orders_in_plan` / tour crediting now normalize '#S…' to the parent.
2. **Pre-window collections are satisfied history; their deliveries are live work.**
   A FULL_FLEET order collected before the window with an in-window delivery is
   assumed successfully collected and STAGED AT ITS COLLECTION LEG'S SOURCE DEPOT
   (the catchment that collected it: CB9/SG8/AL9→CB22, SG6/MK4x/SG1x→BEDFORD,
   ST4→STOKE) — the same rule the weekly `handover.staged_freight` uses. The
   machinery already existed end-to-end (state.py `_pre_window_collected` →
   AT_DEPOT; jobs.py → `PRESTAGED_DELIVERY`, no predecessor; options resolver →
   forced XDOCK): the ONE killer was the expiry target — `target_service_day` gave
   collect-flow orders their ORIGIN date, so the first anchor expired all 58 before
   any seed saw their unblocked delivery legs (the exact import lesson, one flow
   over). Now: `window_start`-aware retarget to the delivery date; excluded from
   the collection ledger (delivery-tracked like imports); their
   `BEFORE_PLANNING_START` pickup rows no longer pollute `unassigned_jobs.csv`.

## Slip recovery + intraday commissioning (1a/1b, 2026-07-16)

**1a — slipped windows move with the slip.** The collection window derives from the
pickup TIMESTAMP columns (`scope._pickup_anchor_timestamp`), not `origin_date`;
`redate_qargo` shifted only the date, so every slipped order retried with a window
pinned to its ORIGINAL day — TIME_WINDOW-dead forever. Nothing could EVER serve late
(zero SLIPPED outcomes in the whole week; WT255059 — a 7.1h day-trippable BA3→PE15
XDOCK with 39 compatible vehicles — starved 6 straight days). `redate_qargo` now
shifts the requested AND actual origin anchors together (the anchor's reschedule
branch re-pins to whichever is stale if they move apart) and the destination anchors
by the promise push. Windows only move LATER: non-anticipation holds.

**1b — fresh-tour commissioning (`TOUR_COMMISSION_ENABLED`, default ON, config-only).**
A far order that neither the daily pass nor tour-attach can serve gets a FRESH
one-vehicle tour on an idle capable truck the same epoch (`commission_intraday`,
sibling of `attach_intraday`): Q4 vehicle pick over idle-today vehicles, evaluated
from the vehicle's REAL home with day-1 duty starting AT THE DISPATCH FLOOR
(`_DayCursor` resume — a 10:30 commissioning cannot pretend a 05:00 launch), the
delivery promise a hard due-offset. Committed IMMEDIATELY (a phoned driver is a
commitment): frozen rid, tour-credited, span reserved. Physics note: an atomic
two-point DIRECT that no longer fits the floored day honestly books its whole leg on
day 2 — true same-day far-DIRECT launches need mid-leg overnight for two-point legs
(open item). With 1a, the NE42 pair already serves ON TIME off the next seed;
commissioning locks the dispatch decision at booking time and adds same-day service
for single-point far orders.

**Overnight DIRECT (`TOUR_DIRECT_OVERNIGHT_SPLIT`, default ON, 2026-07-16):** a two-point
DIRECT leg that no longer fits the day COLLECTS today, sleeps at the collection point,
and delivers tomorrow — the day boundary lands at the origin (segment-A fit test; the
readiness floor is checked on the COLLECTION day; the single stop lands on the DELIVERY
day so record emission stays 1:1). OFF = the legacy atomic slide. This closes the physics
gap that made same-day commissioning of far DIRECTs degenerate to next-day: the commission
chain now reproduces the human move end-to-end (booked 09:16 → floored 10:30 dispatch →
collect Thu afternoon → sleep out → deliver Fri on time).
