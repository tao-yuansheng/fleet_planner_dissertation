"""A tiny tailable run log: per-stage timing + live progress checkpoints.

Each line is written and flushed immediately (append mode, open/close per write)
so a long run can be inspected with `tail`/`cat` while it is still going, instead
of waiting blind for stdout that only appears at the end.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class RunLog:
    def __init__(self, path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.t0 = time.perf_counter()
        # truncate / start fresh
        self.path.write_text("", encoding="utf-8")
        self.log(f"run started {datetime.now().isoformat(sep=' ', timespec='seconds')}")

    def _write(self, line: str) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def log(self, message: str) -> None:
        elapsed = time.perf_counter() - self.t0
        self._write(f"[{elapsed:8.1f}s] {message}")

    @contextmanager
    def stage(self, name: str):
        self.log(f"START  {name}")
        start = time.perf_counter()
        try:
            yield
        finally:
            self.log(f"END    {name} ({time.perf_counter() - start:.1f}s)")
