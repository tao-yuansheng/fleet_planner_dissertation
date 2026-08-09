# Dynamic dispatcher structural review — why the smokes kept failing

**Date:** 2026-07-10 (night)
**Trigger:** smoke attempt 8 crashed (the eighth consecutive full-week failure); user directive: "review the codes structurally and find out why this keeps on failing."
**Method:** full static read of every availability-override assembly site, then a deterministic probe rerun (`PYTHONHASHSEED=0`, `FP_DEBUG_KEY=N888WSM:2026-01-14`, pure logging) that reproduced the crash bit-identically and traced every view the crashing vehicle-day passed through.
**Status:** root cause CONFIRMED by direct observation. Remedy designed, NOT implemented — awaiting stakeholder sign-off.

---

## 1. The crash (attempt 8, identical in the probe rerun)

`ValueError: cannot emit plan records for N888WSM 2026-01-14: day evaluates infeasible (TIME_WINDOW)` at the **Wednesday 12:00 anchor's** record minting. 27 same-address CUSTOMER_PICKUP jobs in two trips. The minting evaluation started the vehicle at **10:00** (a trunk next-day rest); trip 1 (20 stops) ran 10:00→16:41 feasible, trip 2 (7 stops) chained to 17:11 and broke its 18:00 windows. Both trips only fit a **06:00** start.

## 2. Probe trace — the observed causal chain

```
Mon 03:00 POST-SOLVE: sol=[26] 06:00→13:41  last_ov=None      (internal draw: no N888WSM Tue night)
Mon 12:00 POST-SOLVE: sol=[26] 10:00→17:41  last_ov='10:00'   (internal draw: N888WSM ON Tue night)
Tue 03:00 POST-SOLVE: sol=[26] 10:00→17:41  last_ov='10:00'
Tue 12:00 POST-SOLVE: sol=[26] 06:00→13:41  last_ov=None      (flip-flop; Tue DAY CLOSE = authority:
                                                               did NOT draw N888WSM → state_av stays None)
Wed 03:00 PRE-SOLVE : extra=None state_av=None ctx=None        (truth: day starts 06:00)
Wed 03:00 POST-SOLVE: sol=[20,7] T1 06:00→12:41, T2 13:11→17:38  feasible, minted fine
Wed 04:00..10:00    : T1 committed (dep 06:00 < horizon), ctx_pin=None
Wed 11:00           : T2 committed too (dep 13:11 < 13:30 horizon), chosen=[20,7], ctx_pin=None
Wed 12:00 PRE-SOLVE : extra=None state_av=None ctx=None inflight=[20,7] wm=(20,1)
                      seed_over=DutyOverride(start 17:38, drive_left 513m)   ← seed view CORRECT
Wed 12:00 inject    : ov='10:00'  merged_feasible=False reason='TIME_WINDOW' validated=False   ← KILL SHOT
→ ALNS runs 2,000 iterations around the poisoned day → minting raises (B16 refuses to emit fiction)
```

The Wednesday day was legitimately built and committed under its true 06:00 profile. The **noon solve's own internal trunk draw re-imagined Tuesday night with N888WSM in it**, minted a phantom "rest until 10:00" for a day that was already half-driven, and the emission guard correctly refused the resulting fiction.

## 3. Root cause — three structural defects, one class

**(A) Anchors re-plan the past.** [tour_plan.py:418] schedules the per-solve internal trunk from the **window start**, not the epoch day. Every anchor re-draws nights that already happened, over that epoch's visible candidate frame. Visibility grows between epochs (1,971 → 2,155 legs Wed 03→12), so pallet totals per night change, so trip counts change, so the rotation draws **different tractors for the same past night at different anchors** (the observed 06/10 flip-flop across four consecutive anchors). The fabricated morning rests enter `combined_avail_overrides` UNDER the epoch extras ([run_alns.py:344-348]) and therefore govern exactly the keys the epoch state never claimed.

**(B) Commitment context is sparse, not total.** `inflight_ctx[key] = None` means both "unclaimed" and "committed with NO override (profile 06:00)". Every merge site filters `if v is not None`, so a committed day whose true context is *absence of an override* cannot defend itself against a later solve fabricating a value for its key. The crash-6 fix (`inflight_ctx`) was right but incomplete: it pinned values, not views.

**(C) Injected committed days are never validated.** The injection merge-repair skips any key where `len(trips) <= n_inj` ([alns.py]), i.e. every purely-in-flight day. An injected day that is infeasible under the current solve's view sails through 2,000 ALNS iterations (watermark/lock guards keep ALNS from touching it) and dies at record minting — far from the seam that broke. The probe printed `merged_feasible=False ... validated=False` at injection, 35 solver-seconds before the crash.

**The class:** availability views are assembled independently at six sites (anchor extras, seed view, micro merged view, commitment pin, solve-internal combined, finalize). Correctness requires all pairwise agreements; nothing enforces any of them; no failure surfaces at the seam that caused it. Crashes 1, 3, 6, 8 are four strains of this one disease. This is also why the strict-era E6 never hit it: strict froze whole days, excluded them from re-solve, and carried their records append-only — committed days never re-entered a later solve's evaluation, so fabricated views had nothing to poison.

## 4. Why Monday/Tuesday survived

The kill requires a **committed** day meeting a **later** anchor whose draw fabricates a rest for it, on a day tight enough that +4h breaks it. Monday/Tuesday's committed days either met no fabrication (the flip-flop happened to agree at the moment of injection) or had slack. N888WSM's Wednesday was a 27-job, 11.6-hour mega-shipper day from 06:00 — zero slack. Determinism note: with `PYTHONHASHSEED` unpinned, the draws' tie-breaks and set orders shuffle per process, which is why attempts 1–7 died in five different places. Pinned, the trajectory reproduces exactly.

## 5. Remedy (designed, not yet implemented)

**Fix 1 — the internal trunk never re-draws the past.** Thread the epoch day into the solve; `run_multiday_seed_plan` filters internal trunk nights to `night >= epoch_day`. Past nights are facts owned by the day-close authority (`state.avail_overrides`), delivered via extras. Default `None` keeps the static path byte-identical (E1 gate re-run to prove it). After this fix no in-solve source can mint a value for a today-or-past key: internal draw covers tonight-and-future only, handover overrides are window-start facts, extras are the epoch state itself.

**Fix 2 — commitment context becomes total.** The pin records the governing view **including absence**; merges apply it two-way (set the value, or delete the key when the pinned view is "no override"). One helper (`apply_commit_ctx(base, ctx)`) replaces the three `if v is not None` merge sites (anchor, micro, finalize). With Fix 1 the pinned view is trustworthy; with Fix 2 it is immutable. Together: the evaluation context a day was committed under is part of the commitment — now with no representational hole.

**Fix 3 — injection contract, fail fast.** Remove the `len <= n_inj` skip: every injected key is evaluated under the solve's overrides at injection. An infeasible purely-in-flight day raises immediately, naming the key, the override in force, and the reason — a contract violation caught at the seam, not 2,000 iterations later at minting. (Seed-chained keys keep the existing strip-to-pool repair.)

**Fix 4 — micro option-set exclusivity (found during this review, user-prompted).** `new_arrival_meta` hands the micro inserter ALL of an arrival's same-day candidate legs raw — for a FULL_FLEET order both :DIR and :C pass the filter and `insertion_pass` has no option-set exclusivity → possible double-serve, invisible until emission. Fix: resolve micro arrivals to one branch before insertion (v1: collect leg only; DIRECT election remains an anchor decision).

**Fix 5 (deferred, hygiene).** Micro expiry compares against the last anchor's `route_times`, which go stale for trips behind micro-grown suffixes. Not implicated in any crash (the probe shows this day was born [20,7] at the anchor; no micro insertions touched it). Re-time changed keys after micro insertions when we next touch that code.

**Test plan (TDD):** scripted harness reproduction of the fabricated-history crash (fake solve returns a combined override for a committed key that extras don't carry → must now raise at injection under Fix 3, and never arise under Fixes 1–2); unit tests for the night filter, the total-pin merge helper, and the micro option resolution; full suite; N=300 bit-identical E1 gate; then smoke attempt 9 only on user clearance.

## 6. Diagnostics kept

`FP_DEBUG_KEY=VID:YYYY-MM-DD` probes (run_rolling + alns injection) stay in the code — env-gated, zero cost when unset, and they turn any future view divergence into a 15-minute traced rerun instead of an evening of inference.

---

## 7. Implementation record (2026-07-10 night — user approved, with two amendments)

User sign-off added two design commitments that became Fixes 6 and 7:
(a) **departed trips are immutable** — depot-loaded delivery stops especially: they stay on that vehicle no matter what; suffixes stay open only for inserting collection-side work (to-depot for crossdock, direct for full-fleet);
(b) **one consolidated trunk list per night**, absorbing inserted collections until the loading cutoff.

Shipped, TDD (RED observed for every test; the T2 e2e verified red<->green in both directions), in `tests/freight_planner/test_structural_fixes.py` (11 tests):

- **Fix 1** `trunk_from` threaded ns → solve_window → run_multiday_seed_plan; internal trunk nights filtered to `>= epoch day`. Default `None` = static path untouched.
- **Fix 2** `apply_commit_ctx(base, ctx)` — total pins (set the value, or DELETE the key when the pinned view is "no override") — replaces the three sparse merges (anchor extras, micro view, finalize). A monkeypatch spy proves the phantom is deleted from the micro-pass view.
- **Fix 2b (descoped to backlog)** mid-day dispatch of an *idle* vehicle under the floor needs trip-level floors as first-class solution state; a transient floor at insert time would re-time early at the next evaluation and emit a causality-violating artifact (audit A guards this). The evaluator seam (`evaluate_day(trip_earliest=...)`) is in and unit-tested but has no production caller yet. Returning-vehicle suffix insertion — the user's actual scenario — works today.
- **Fix 3** injection validates EVERY injected key under the solve's view; a purely-in-flight day arriving infeasible raises at the seam naming key/override/reason; chained seed trips keep the strip-to-pool repair, now with core re-validation after the strip.
- **Fix 4** `new_arrival_meta` restricts micro arrivals to CUSTOMER_PICKUP / DIRECT_CUSTOMER_MOVE (delivery legs are anchor business); `insertion_pass` takes ONE branch per order — the sibling option leg is superseded (dropped), not failed.
- **Fix 6** the day close is a real 18:00 decision after the day's last micro-pass (`DAY_CLOSE_HOUR`); `next_decision_after` looks through closes, so the 17:00 micro's expiry horizon reaches tomorrow's anchor and launches everything still departing today. Afternoon-inserted collections join tonight's trunk list (test: 12:30 booking inserted 13:00 appears in the dock sizing).
- **Fix 7** every job of a departed trip is pinned via `extra_pinned_job_ids` (loop) unioned into ALNS `pinned_job_ids` (solve_window) — no destroy/eject/reassign of committed-route stops, ever; insertion around them stays legal.

**Verification:** `tests/freight_planner` 622 passed (611 + 11). Full-repo failures (3-4) are pre-existing legacy/network tests (`routing`, `simulation.postcode_resolver`, `window_start`) with zero import overlap with the fix pack. **Static-path gate: N=300 E1 window 2026-01-12..17 seed 0, PYTHONHASHSEED=0 — all 14 plan artifacts SHA256-identical pre/post fix pack.**

Smoke attempt 9 staged, NOT launched — awaiting user clearance per standing rule.
