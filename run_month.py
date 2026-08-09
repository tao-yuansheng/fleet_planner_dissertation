"""Run a handover-chained sequence of weekly windows into one month folder.

Shells ``run_alns`` per window (so a single week stays independently runnable),
wiring each week's ``--handover-in`` to the prior week's emitted handover.json.
After the chain, emits per-week viz and the month rollup. NO planner logic here.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

from freight_planner.demand import FORWARD_STRUCTURAL
from freight_planner.month_summary import build_month_summary
from freight_planner.output_layout import find_artifact, flat_window_label
from freight_planner.paths import DEFAULT_OUT_DIR


def parse_window(s: str) -> tuple[date, date]:
    a, b = s.split(":")
    return date.fromisoformat(a), date.fromisoformat(b)


def window_dir_for(out_root: Path, start: date, end: date, mode: str, basis: str) -> Path:
    win = flat_window_label(start, end, mode, basis)
    return out_root / f"{start:%Y-%m}" / win


def _run(cmd: list[str]) -> int:
    print(">>", " ".join(cmd), flush=True)
    return subprocess.run(cmd).returncode


def main(argv=None):
    ap = argparse.ArgumentParser(description="Handover-chained month run")
    ap.add_argument("--windows", nargs="+", required=True, help="START:END ... in order")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--time-budget", type=float, default=120.0)
    # --qargo REMOVED 2026-07-22: orders input is fixed to the combined enriched
    # parquet in the runners (user rule — no per-run override).
    ap.add_argument("--initial-handover", default=None,
                    help="handover.json seeding the FIRST window (e.g. prior month's last week)")
    ap.add_argument("--responsibility-mode", default=FORWARD_STRUCTURAL)
    ap.add_argument("--date-basis", default="planning_window")
    ap.add_argument("--day-flex", action="store_true",
                    help="pass --day-flex (K2 earlier-only day flexibility) to every window")
    ap.add_argument("--no-viz", dest="viz", action="store_false")
    ap.add_argument("--summary-only", action="store_true",
                    help="skip running; just (re)build the month rollup for the windows' month")
    args = ap.parse_args(argv)

    out_root = Path(args.out_dir)
    windows = [parse_window(w) for w in args.windows]
    months = {f"{s:%Y-%m}" for s, _ in windows}

    if not args.summary_only:
        prev_handover: Path | None = Path(args.initial_handover) if args.initial_handover else None
        for start, end in windows:
            window_dir = window_dir_for(out_root, start, end, args.responsibility_mode, args.date_basis)
            cmd = [sys.executable, "-m", "freight_planner.run_alns",
                   "--start", start.isoformat(), "--end", end.isoformat(),
                   "--out-dir", str(out_root), "--time-budget", str(args.time_budget),
                   "--responsibility-mode", args.responsibility_mode, "--date-basis", args.date_basis]
            if args.day_flex:
                cmd += ["--day-flex"]
            if prev_handover is not None:
                cmd += ["--handover-in", str(prev_handover)]
            if _run(cmd) != 0:
                print(f"!! run failed for {start}..{end}; stopping chain", file=sys.stderr)
                return 1
            # the emitted handover chains into the next window wherever the
            # layout put it (run root now; plan/ on legacy layouts)
            prev_handover = find_artifact(window_dir, "handover.json")
            if args.viz:
                # best-effort: a viz failure must not break the run chain
                _run([sys.executable, "-m", "freight_planner.viz_app",
                      "--plan-dir", str(window_dir), "--validate"])

    for m in sorted(months):
        path = build_month_summary(out_root / m)
        print(f"month summary -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
