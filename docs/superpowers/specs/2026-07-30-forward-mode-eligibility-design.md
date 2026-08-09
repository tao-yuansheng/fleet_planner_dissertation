# Forward Mode Eligibility Design

## Purpose

Mode must be decided from the order's physical dates before routing, not inferred
after a failed insertion. A cross-dock insertion failure does not demonstrate
that a direct movement is feasible or operationally valid.

## Mode rule

For a two-point order:

- When the collection service date equals the delivery service date, generate
  both the ordinary `DIRECT` and `XDOCK` alternatives. The seed and ALNS choose
  between them using their normal feasibility and cost evaluation.
- When the collection service date differs from the delivery service date,
  generate only the `XDOCK` alternative. An ordinary vehicle cannot retain the
  freight until a later delivery date.

There is no direct-mode exception for multi-day tours in this change. The
existing tour pipeline remains unchanged and may carry an eligible cross-dock
delivery leg from its depot to a distant customer.

## Stranded work

Remove the post-seed stranded-backhaul conversion. In particular:

- a rejected cross-dock pickup remains a rejected cross-dock pickup;
- its rejection remains eligible for the existing ALNS coverage-repair path;
- failure of both cross-dock legs never creates a synthetic direct job;
- `REPAIRED_DIRECT` is no longer produced by planning.

For an in-window collection whose delivery lies beyond the planning horizon,
the pickup remains plannable. Once collected, its freight is staged at the
cross-dock and carried through the handover for later delivery.

## Scope

The change is limited to:

1. forward `DIRECT`/`XDOCK` candidate eligibility;
2. removal of the post-seed stranded-to-direct repair and its bookkeeping;
3. reports and tests that explicitly depend on `REPAIRED_DIRECT`.

The multi-day tour classifier, tour evaluator, vehicle assignment, day splitting,
and tour emission are not redesigned.

## Verification

Automated tests must prove:

1. same-date two-point orders expose both `DIRECT` and `XDOCK`;
2. different-date two-point orders expose `XDOCK` only;
3. a rejected cross-dock pickup remains repairable by ALNS;
4. an in-window pickup with an out-of-window delivery can be staged for handover;
5. planning produces no synthetic `REPAIRED_DIRECT` movement;
6. existing multi-day tour tests remain green without altered expectations,
   except tests whose sole purpose was the removed stranded-direct mechanism.

The regression fixture should reproduce the essential WT270258 boundary:
collection on 20 February, delivery on 23 February, with the planning window
ending on 22 February.
