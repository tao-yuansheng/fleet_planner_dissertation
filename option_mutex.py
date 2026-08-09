"""Mutual-exclusion for DIRECT-vs-XDOCK option sets (2026-07-23).

An option set (keyed by ``option_set``, == freight_id) offers alternative ways to
serve one freight unit: option_group DIRECT (one leg) or XDOCK (a pickup + a
delivery leg). At most ONE group may be assigned per set. This tracker is the
single source of that invariant, used by the seed and every ALNS insertion site.
"""
from __future__ import annotations


def _set(cand: dict) -> str:
    return str((cand or {}).get("option_set", "") or "")


def _group(cand: dict) -> str:
    return str((cand or {}).get("option_group", "") or "")


class OptionMutex:
    def __init__(self) -> None:
        self._active: dict[str, str] = {}   # option_set -> active option_group

    def active_group(self, option_set: str) -> str | None:
        return self._active.get(str(option_set)) or None

    def insertable(self, cand: dict) -> bool:
        """True unless the leg's option_set already has a DIFFERENT group active."""
        s, g = _set(cand), _group(cand)
        if not s or not g:
            return True                      # non-optional leg: never constrained
        cur = self._active.get(s)
        return cur is None or cur == g

    def assign(self, cand: dict) -> None:
        s, g = _set(cand), _group(cand)
        if s and g:
            self._active[s] = g

    def release(self, option_set: str) -> None:
        self._active.pop(str(option_set), None)

    def rival_legs(self, cand: dict, legs: list[dict]) -> list[dict]:
        """Legs in ``legs`` sharing this option_set but a DIFFERENT group."""
        s, g = _set(cand), _group(cand)
        if not s or not g:
            return []
        return [l for l in legs if _set(l) == s and _group(l) not in ("", g)]

    def seed_from_assigned(self, assigned_cands) -> None:
        """Initialise active groups from already-placed/committed candidates
        (rolling-horizon mode-lock: a committed XC locks the set to XDOCK)."""
        for cand in assigned_cands:
            self.assign(cand)
