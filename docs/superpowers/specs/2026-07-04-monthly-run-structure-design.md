# Monthly Run Structure + January Backtest — Design

**Date:** 2026-07-04
**Status:** Approved (design)
**Scope:** `freight_planner` output layout, a month orchestration script, and a
month rollup report. Planner logic (seed, ALNS, tours, resolver, handover) untouched.

## Problem

Output is scattered and hard to navigate. Five top-level dirs exist
(`out`, `out_wk1`, `out_wk1_ho`, `out_wk2`, `out_wk2_ho`), and every run is buried
four levels deep under a constant `<mode>/<basis>/<window>/` nesting
(`out_wk1_ho/forward_structural/planning_window/2026-01-12_to_2026-01-17/`). The
mode/basis layers never vary for this workflow, so they are pure noise, and a
per-week top-level dir means the month is not one browsable place.

Separately, only two January weeks have been run. To track km across the month and
to confirm the week-to-week handover chains correctly, the whole month should be
run as a single handover-linked sequence into one clean structure.

## Decisions (from brainstorming)

- **Structure:** flatten + month-group → `runs/<YYYY-MM>/<window>/{inputs,plan,reports}/`.
- **Windows:** five, handover-chained, week 1 cold-start:
  `2026-01-01_to_2026-01-03` (Thu–Sat stub, cold) → `2026-01-05_to_2026-01-10`
  → `2026-01-12_to_2026-01-17` → `2026-01-19_to_2026-01-24` → `2026-01-26_to_2026-01-31`.
- **Default:** `DEFAULT_OUT_DIR` changes from `.../out` to `.../runs`. Existing
  `out*` dirs are left untouched as historical.
- **Rollup includes odometer + gap** columns (needs telematics), reusing the
  thread-(a) reconciliation so the month km view is honest, not plan-only.

## Component 1 — flattened path in `run_alns.py`

`output_layout.py` stays as-is (its `run_dirs`/`window_label`/`write_run_manifest`
are already generic and unit-tested). The only change is how `run_alns.main` builds
the base dir. Replace the current:

```python
out_dir = Path(args.out_dir) / args.responsibility_mode / args.date_basis
window = window_label(start, end)
_inputs_dir, plan_dir, reports_dir = run_dirs(out_dir, window)
```

with a month-grouped base and a window label that only carries mode/basis when they
are **non-default**, so the common workflow gets clean paths while any other
mode/basis can never collide into the same window folder:

```python
base = Path(args.out_dir) / f"{start:%Y-%m}"
window = flat_window_label(start, end, args.responsibility_mode, args.date_basis)
_inputs_dir, plan_dir, reports_dir = run_dirs(base, window)
write_run_manifest(base, window, {... existing payload, already records mode/basis ...})
```

`flat_window_label` is a small helper (in `output_layout.py`, so it is unit-testable
without importing the CLI):

```python
def flat_window_label(start, end, mode, basis,
                      default_mode="forward_structural", default_basis="planning_window"):
    label = window_label(start, end)
    suffix = ""
    if mode != default_mode:
        suffix += f"__{mode}"
    if basis != default_basis:
        suffix += f"__{basis}"
    return label + suffix
```

Result for the default workflow: `runs/2026-01/2026-01-12_to_2026-01-17/plan/...`.
`DEFAULT_OUT_DIR` in `paths.py` becomes `LOGISTICS_ROOT / "freight_planner" / "runs"`.

## Component 2 — orchestration `run_month.py`

A thin CLI that runs a list of windows in order and wires the handover chain. It
does **not** re-implement the pipeline; it shells `run_alns` (subprocess) per window
so a single week is still runnable standalone.

```
python -m freight_planner.run_month --month 2026-01 \
    --windows 2026-01-01:2026-01-03 2026-01-05:2026-01-10 \
              2026-01-12:2026-01-17 2026-01-19:2026-01-24 2026-01-26:2026-01-31 \
    [--time-budget 120] [--viz/--no-viz]
```

Per window, in order:
1. Build the run: `run_alns --start S --end E --out-dir <root>/runs`
   `--handover-in <prev_plan>/handover.json` (omitted for the first, cold-start).
2. Resolve this week's `plan/` dir (deterministic from the flattened layout) and
   remember its `handover.json` as the next week's `--handover-in`.
3. If `--viz` (default on): emit `reports/trip_app.html` via `viz_app`.
4. On a non-zero run, stop the chain (a later week must not consume a missing
   handover) and report which window failed.

After all windows: invoke `month_summary` (Component 3).

## Component 3 — month rollup `month_summary.py`

Pure functions + a thin CLI. Scans `runs/<month>/*/` (skipping non-window dirs),
reads each week's artifacts, writes `runs/<month>/month_summary.md`.

**KM table** (one row per window, chronological). The gap columns follow thread (a):
we do **not** report the naive combined-÷-window-odometer number as *the* gap, because
that mismatch (9-day plan tail ÷ 6-day odometer, plus out-of-scope fleet driving) is a
measurement artifact. Both are shown, labelled, so the artifact is visible and corrected.

| window | in-univ | assigned % | plan km | trunk km | combined | odo (6d) | gap naive | gap matched |

- `in-univ`, `assigned %`, `plan km`, `trunk km`, `combined` — parsed from
  `plan/kpi_summary.md` (already emitted; reuse the regexes in `viz_app._parse_kpi`,
  extended for trunk/combined). `plan km` is the plan-side month trend — the primary
  "track km over the month" signal, independent of any odometer comparison.
- `odo (6d)` — our-fleet odometer over the window's Mon–Sat days via
  `vehicle_actuals.actual_km_by_vehicle(prefer_odometer=True)` intersected with the
  static fleet (`vehicles.vehicle_states_frame`). Exactly the quantity that reconciled
  to 89,571 / 92,789 in thread (a).
- `gap naive` — `(combined − odo6d) / odo6d`, **labelled "fleet-wide, incl. out-of-scope
  reality + day-tail"** so it is never mistaken for real inefficiency.
- `gap matched` — the honest (vehicle, day)-matched gap: over the plan's own service
  dates, sum plan km on vehicle-days that have telematics odometer vs that same
  vehicle-day odometer (`plan_matched / odo_matched − 1`). This is the thread-(a) method
  and the number we trust. A **month total** row aggregates each column.
- Telematics is best-effort: if a day fails to load, odometer/gap cells show `n/a` and
  the KM columns still render.

**Handover-continuity table** (one row per hop):

| from → to | prev delivered | in-handover delivered | match? | in-flight carried | staged |

- For each consecutive pair, read the producer week's emitted `plan/handover.json`
  and the consumer week's `run_manifest.json` (records its `--handover-in` path) and
  the consumer's applied counts. `match?` asserts the consumer consumed exactly the
  producer's `delivered_order_ids` and `vehicle_availability`. Any mismatch is a
  loud `✗` line — this is the "did handover actually work" check.

## Data flow

```
qargo_20260101_to_20260131.parquet + verified_legs.csv (covers 2026-01-02→)
      │  run_month (per window, in order)
      ▼
runs/2026-01/<window>/{inputs,plan,reports}/   plan/handover.json ──┐
      │  (next window --handover-in) ◄─────────────────────────────┘
      ▼
month_summary.py ──► runs/2026-01/month_summary.md  (km table + handover chain)
```

## Wiring / correctness gate

- OSRM confirmed up (port 5000) — required or runs hang.
- verified_legs covers 2026-01-02 → 2026-03-04 — all five windows covered.
- **Smoke test first:** run a single week (`2026-01-05_to_2026-01-10`) end-to-end
  before the full chain, to confirm the flattened path, handover emit, and viz.
  Only then launch the full 5-run chain (background; ~3–8 min/week).

## Testing (TDD)

- `flat_window_label`: default mode/basis → clean label; non-default → `__suffix`;
  both non-default → both suffixes.
- `run_alns` path: with defaults, plan dir is `runs/2026-01/<window>/plan` (assert
  on the resolved path; no full pipeline run in the unit test).
- `month_summary` KPI parse: a fixture `kpi_summary.md` → correct row dict.
- `month_summary` handover-continuity: fixtures for a producer `handover.json` + a
  consumer `run_manifest.json` → `match` True on aligned, False on a dropped order.
- Existing `test_output_layout.py` stays green (unchanged behaviour of `run_dirs`).

## Validation (inline, never a subagent)

Run the full month; confirm: five run dirs under `runs/2026-01/`, each with
`plan/handover.json`; `month_summary.md` renders the km table (km tracked across the
month) and every handover hop shows `match ✓`; coverage per week stays ≥99.9%.
Report the month km trend and the handover-chain result as a stakeholder line.

## Non-goals (YAGNI)

- No change to planner logic (seed/ALNS/tours/resolver/handover semantics).
- No migration of the historical `out*` dirs (kept as-is; not deleted, not moved).
- No cross-week combined map — per-week `trip_app.html` already exists.
- No general multi-month abstraction — `run_month` takes an explicit window list;
  February is a future invocation, not a new subsystem.
- No git commits (standing rule).
```
