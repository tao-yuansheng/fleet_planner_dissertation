"""Plan-disturbance scoring for the dynamic dispatcher (spec 2026-07-11 §5).

``disturbance(candidate, reference)`` = weighted count of jobs that MOVED from the
plan being warm-started: reassigned (changed vehicle-day) counts full, resequenced
(same vehicle-day, different position) counts ``gamma``. New jobs (absent from the
reference) are additions, not disturbances, and do not count. ``imminence_weights``
gives each job a weight that is high when it is about to dispatch and decays with
lead time, so changes to soon-to-happen stops hurt more. Pure; no I/O.
"""
from __future__ import annotations

from datetime import datetime


def _as_trips(v):
    return [v] if v and hasattr(v[0], "job_id") else list(v or [])


def job_positions(plan: dict) -> dict:
    """job_id -> (key, flat position within its vehicle-day)."""
    out: dict = {}
    for key, trips in plan.items():
        pos = 0
        for t in _as_trips(trips):
            for j in t:
                out[j.job_id] = (key, pos)
                pos += 1
    return out


def key_disturbance(key, trips, ref_positions: dict, *, gamma: float = 0.5,
                    weight: dict | None = None) -> float:
    """Disturbance contribution of ONE vehicle-day vs the reference positions.

    Summing this over a solution's keys equals ``disturbance``; the incremental
    ALNS objective sums it over only the keys a move changed.
    """
    w = weight or {}
    total = 0.0
    pos = 0
    for t in _as_trips(trips):
        for j in t:
            rp = ref_positions.get(j.job_id)
            if rp is not None:
                jw = float(w.get(j.job_id, 1.0))
                if rp[0] != key:
                    total += jw
                elif rp[1] != pos:
                    total += gamma * jw
            pos += 1
    return total


def disturbance(candidate: dict, reference: dict, *, gamma: float = 0.5,
                weight: dict | None = None) -> float:
    """Whole-plan disturbance of candidate vs reference."""
    ref = job_positions(reference or {})
    return sum(key_disturbance(key, trips, ref, gamma=gamma, weight=weight)
               for key, trips in candidate.items())


def disturbance_breakdown(candidate: dict, reference: dict, *, gamma: float = 0.5,
                          weight: dict | None = None) -> dict:
    """The report-side anchor for tuning beta (2026-07-17): the objective's own
    disturbance score plus its parts. Walks the candidate exactly like
    ``disturbance`` — reassigned (changed vehicle-day) counts its full weight,
    resequenced counts gamma x weight, additions (absent from the reference) are
    free — and also returns the counts and the weighted comparable base, so the
    score normalizes into a scale-free percentage (score <= weighted_comparable
    by construction). Pure; no I/O."""
    ref = job_positions(reference or {})
    w = weight or {}
    score = 0.0
    reassigned = resequenced = comparable = 0
    weighted_comparable = 0.0
    for key, trips in (candidate or {}).items():
        pos = 0
        for t in _as_trips(trips):
            for j in t:
                rp = ref.get(j.job_id)
                if rp is not None:
                    jw = float(w.get(j.job_id, 1.0))
                    comparable += 1
                    weighted_comparable += jw
                    if rp[0] != key:
                        reassigned += 1
                        score += jw
                    elif rp[1] != pos:
                        resequenced += 1
                        score += gamma * jw
                pos += 1
    return {"score": score, "reassigned": reassigned, "resequenced": resequenced,
            "comparable": comparable, "weighted_comparable": weighted_comparable}


def imminence_weights(dispatch_iso: dict, now: datetime,
                      horizon_min: float = 720.0) -> dict:
    """job_id -> imminence weight in [0, 1]: 1.0 for a job dispatching now (or in
    the past), decaying linearly to 0 at ``horizon_min`` minutes ahead. Missing or
    unparseable times weight 1.0 (conservative: treat unknown as imminent)."""
    out: dict = {}
    for jid, iso in dispatch_iso.items():
        try:
            dt = datetime.fromisoformat(str(iso)) if iso else None
        except ValueError:
            dt = None
        if dt is None:
            out[jid] = 1.0
            continue
        lead = max(0.0, (dt - now).total_seconds() / 60.0)
        out[jid] = max(0.0, 1.0 - lead / horizon_min)
    return out
