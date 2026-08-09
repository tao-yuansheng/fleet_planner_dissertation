"""Output folder layout for a planner run.

Each run's directory is keyed by configuration *and* planning window so runs for
different periods never collide:

  ``out/<YYYY-MM>/<window>/``

Layout (2026-07-14 restructure — the old ``{inputs,plan,reports}`` split retired):

  * the run ROOT holds the deliverables a person opens first: ``run_manifest.json``,
    ``plan_full.csv`` (the denormalised whole-plan view), ``runsheets.html``,
    ``timeline.html`` (dynamic runs), ``alns_progress.log``, ``handover.json``,
    ``validation_metrics.json`` (+ ``rolling_manifest.json`` on dynamic runs);
  * ``csv/`` — every other CSV (manifest spine, route_stops, utilization,
    snapshots, provenance, traces, …);
  * ``reports/`` — every markdown report (KPI, summaries, decision logs).
    (Named ``md/`` for runs written 2026-07-14..16; renamed 2026-07-16, user rule.)

Writers do not choose folders: :class:`RunPaths` routes ``dir / "name.ext"`` by
extension (`.csv` → ``csv/``, `.md` → ``reports/``, everything else → root;
``plan_full.csv`` is the one root CSV by contract). Readers use
:func:`find_artifact` / :func:`artifact_dir`, which also resolve the interim
``md/`` and the LEGACY ``plan/``+``reports/`` layouts so older runs stay viewable.

``inputs/`` is no longer created for planner runs (it was only ever written by
the standalone ``build_phase0`` spine build, which now makes its own folder).

A ``run_manifest.json`` at the window dir records what produced the run (window,
args, timestamp) so an output folder is self-describing.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

#: files that stay at the run root regardless of extension
_ROOT_FILES = {"plan_full.csv"}


class RunPaths:
    """Extension-routing view of one run directory.

    ``router / "x.csv"`` → ``<base>/csv/x.csv``; ``router / "x.md"`` →
    ``<base>/reports/x.md``; any other suffix (json / html / log / …) → ``<base>/x``.
    ``plan_full.csv`` routes to the root (it is the headline deliverable).
    Stringifies to the base path so log lines print the run dir.
    """

    def __init__(self, base: Path):
        self.base = Path(base)

    def __truediv__(self, name) -> Path:
        n = str(name)
        if n in _ROOT_FILES:
            return self.base / n
        suffix = Path(n).suffix.lower()
        if suffix == ".csv":
            return self.base / "csv" / n
        if suffix == ".md":
            return self.base / "reports" / n
        return self.base / n

    @property
    def parent(self) -> Path:
        """The window dir itself — legacy code reaches the window root via
        ``<plan_dir>.parent`` (run_manifest.json, the window name); a router
        over the window base must honour that idiom."""
        return self.base

    def __str__(self) -> str:
        return str(self.base)

    def __fspath__(self) -> str:
        return str(self.base)

    def __repr__(self) -> str:
        return f"RunPaths({self.base!r})"

    def __eq__(self, other) -> bool:
        return isinstance(other, RunPaths) and self.base == other.base

    def __hash__(self) -> int:
        return hash(("RunPaths", self.base))


def window_label(start: date, end: date) -> str:
    """Stable folder-safe label for a planning window: ``<start>_to_<end>``."""
    return f"{start.isoformat()}_to_{end.isoformat()}"


def flat_window_label(
    start: date, end: date, mode: str, basis: str,
    *, default_mode: str = "forward_structural", default_basis: str = "planning_window",
) -> str:
    """Window folder label for the flattened, month-grouped layout.

    The common workflow (forward_structural + planning_window) gets a clean
    ``<start>_to_<end>`` label. A non-default mode/basis is appended as a ``__suffix``
    so a different mode/basis can never collide into the same window folder.
    """
    label = window_label(start, end)
    suffix = ""
    if mode != default_mode:
        suffix += f"__{mode}"
    if basis != default_basis:
        suffix += f"__{basis}"
    return label + suffix


def run_base(out_dir: Path, window: str | None = None) -> Path:
    """The base dir for one run: ``out_dir/<window>`` when a window is given."""
    return out_dir / window if window else out_dir


def run_dirs(out_dir: Path, window: str | None = None) -> tuple[Path, RunPaths, RunPaths]:
    """Prepare a run directory; return ``(base, files, files)``.

    ``base`` is the window dir itself. The second and third elements are the
    SAME :class:`RunPaths` router returned twice so callers that historically
    unpacked ``(inputs, plan, reports)`` keep working — both names now route by
    extension into ``csv/`` / ``reports/`` / the root. ``inputs/`` is not created.
    """
    base = run_base(out_dir, window)
    for d in (base / "csv", base / "reports"):
        d.mkdir(parents=True, exist_ok=True)
    files = RunPaths(base)
    return base, files, files


def find_artifact(window_dir: Path, name: str) -> Path:
    """Locate a run artifact in the current layout or an older one.

    Search order: run root, ``csv/``, ``reports/`` (current — the name also
    serves legacy pre-2026-07-14 runs), then ``md/`` (interim 2026-07-14..16
    runs), then ``plan/`` (legacy). Raises ``FileNotFoundError`` naming every
    path tried.
    """
    base = Path(window_dir)
    candidates = [base / name, base / "csv" / name, base / "reports" / name,
                  base / "md" / name, base / "plan" / name]
    for c in candidates:
        if c.exists():
            return c
    tried = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(f"run artifact {name!r} not found; tried: {tried}")


def artifact_dir(path: Path) -> Path | RunPaths:
    """Adapt a user-supplied directory for artifact reads.

    Accepts either a current-layout WINDOW dir (returns a :class:`RunPaths`
    router so ``dir / name`` finds files in ``csv/`` / ``reports/`` / the root)
    or a legacy ``plan/`` / ``reports/`` folder (returned unchanged — joins hit
    the files directly). Detection keys on ``csv/`` (every current run has one),
    never on ``reports/`` — a LEGACY window dir has a ``reports/`` subdir too.
    """
    p = Path(path)
    if (p / "csv").is_dir() or (p / "md").is_dir() or (p / "plan_full.csv").exists():
        return RunPaths(p)
    return p


def write_run_manifest(out_dir: Path, window: str | None, payload: dict[str, Any]) -> Path:
    """Write ``run_manifest.json`` at the run's window dir; returns its path."""
    base = run_base(out_dir, window)
    base.mkdir(parents=True, exist_ok=True)
    path = base / "run_manifest.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path
