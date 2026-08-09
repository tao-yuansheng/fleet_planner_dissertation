"""Shared data-contract types for the two-phase planning architecture.

Phase 1 (WeeklyPlan) → Phase 2 (DayExecutionResult) data flow.
See docs/two-phase-planning/data-contract.md for full specification.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class OrderClass(str, Enum):
    LOCAL = 'LOCAL'   # same-day out-and-back from home depot
    TOUR  = 'TOUR'    # multi-day vehicle deployment to remote region
    # Compatibility label for freight that creates hub linehaul demand. Today this
    # primarily means PL_EXPORT customer pickup obligations; the pickup is still
    # routed locally before trunk_planner handles the outbound hub movement.
    TRUNK = 'TRUNK'


@dataclass
class Tour:
    """A multi-day vehicle assignment for TOUR-class orders."""
    tour_id: str
    vehicle_id: str
    home_depot: str                              # 'CB22' | 'BEDFORD'
    region: str                                  # e.g. 'SW_ENGLAND'
    order_ids: list[str]
    depart_date: date
    return_date: date
    # Phase 1 estimates; Phase 2 updates with actuals each evening
    planned_overnight_pcs: dict[str, str] = field(default_factory=dict)
    # Key is ISO date string (JSON-safe), value is postcode
    # B2: the multi-day solver's per-day leg assignment (ISO date string -> order_ids).
    # When present, Phase 1b routes each day with ONLY that day's legs (no cross-day
    # re-optimization / dropping). Empty for the legacy build_tours path.
    planned_day_order_ids: dict[str, list[str]] = field(default_factory=dict)

    @property
    def duration_days(self) -> int:
        return (self.return_date - self.depart_date).days + 1

    @property
    def job_id(self) -> str:
        return f'TOUR-{self.depart_date.isoformat()}-{self.home_depot}-{self.vehicle_id}'

    def dates_occupied(self) -> list[date]:
        from datetime import timedelta
        days = []
        d = self.depart_date
        while d <= self.return_date:
            days.append(d)
            d += timedelta(days=1)
        return days


@dataclass
class DepotDayBudget:
    """What Phase 2 may work with for one depot on one day."""
    depot_id: str
    date: date

    available_vehicles: list[str]
    # Where each vehicle starts the day
    vehicle_start_positions: dict[str, str]
    # Orders already loaded on mid-tour vehicles (exclude from local pool)
    pre_assigned_manifests: dict[str, list[str]]  # vehicle_id -> [order_ids]
    # Phase 1 preferred LOCAL order pool for this depot/day. Phase 2 treats this
    # as advisory unless a caller adds an explicit binding policy around plan_day().
    local_order_pool: list[str]                   # order_ids

    pl_import_pallet_budget: float = 0.0
    pl_import_order_count: int = 0


@dataclass
class WeeklyPlan:
    """Top-level Phase 1 output consumed by Phase 2 each morning."""
    plan_id: str
    created_at: datetime
    planning_start: date
    planning_end: date

    tours: list[Tour] = field(default_factory=list)
    # Key: "{date_iso}:{depot_id}" (JSON-safe)
    daily_allocations: dict[str, DepotDayBudget] = field(default_factory=dict)

    unassigned_order_ids: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    # B1 (runtime-only, NOT serialised): per-vehicle TourRoutePlan materialised
    # directly from the multi-day solver's route walk, so Phase 1b does not re-solve.
    # Empty on the legacy build_tours path (then Phase 1b routes the tours as before).
    tour_route_plans: dict = field(default_factory=dict)

    @staticmethod
    def allocation_key(d: date, depot_id: str) -> str:
        return f'{d.isoformat()}:{depot_id}'

    def get_allocation(self, d: date, depot_id: str) -> Optional[DepotDayBudget]:
        return self.daily_allocations.get(self.allocation_key(d, depot_id))

    def save(self, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f'week_plan_{self.planning_start}.json'
        path.write_text(_serialise(self), encoding='utf-8')
        return path

    @classmethod
    def load(cls, path: Path) -> 'WeeklyPlan':
        return _deserialise(json.loads(path.read_text(encoding='utf-8')))


@dataclass
class DayExecutionResult:
    """Phase 2 output fed back to Phase 1 for state carry-forward."""
    date: date
    depot_id: str

    # vehicle_id -> end-of-day postcode or '{DEPOT}_DEPOT'
    end_of_day_positions: dict[str, str] = field(default_factory=dict)
    # Orders confirmed delivered today
    delivered_order_ids: list[str] = field(default_factory=list)
    # vehicle_id -> orders still on board at end of day (mid-tour only)
    still_on_board: dict[str, list[str]] = field(default_factory=dict)
    # Orders Phase 2 could not route
    unassigned_order_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# JSON serialisation helpers
# ---------------------------------------------------------------------------

def _serialise(plan: WeeklyPlan) -> str:
    def _convert(obj):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, dict):
            return {str(k): _convert(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_convert(i) for i in obj]
        if hasattr(obj, '__dict__'):
            return {k: _convert(v) for k, v in obj.__dict__.items()
                    if not k.startswith('_')}
        return obj

    data = {
        'plan_id': plan.plan_id,
        'created_at': plan.created_at.isoformat(),
        'planning_start': plan.planning_start.isoformat(),
        'planning_end': plan.planning_end.isoformat(),
        'tours': [_convert(t) for t in plan.tours],
        'daily_allocations': {k: _convert(v)
                              for k, v in plan.daily_allocations.items()},
        'unassigned_order_ids': plan.unassigned_order_ids,
        'metrics': plan.metrics,
    }
    return json.dumps(data, indent=2)


def _deserialise(data: dict) -> WeeklyPlan:
    from datetime import date as _date, datetime as _dt

    def _d(s): return _date.fromisoformat(s)
    def _dt_(s): return _dt.fromisoformat(s)

    tours = []
    for t in data.get('tours', []):
        tours.append(Tour(
            tour_id=t['tour_id'],
            vehicle_id=t['vehicle_id'],
            home_depot=t['home_depot'],
            region=t['region'],
            order_ids=t['order_ids'],
            depart_date=_d(t['depart_date']),
            return_date=_d(t['return_date']),
            planned_overnight_pcs=t.get('planned_overnight_pcs', {}),
            planned_day_order_ids=t.get('planned_day_order_ids', {}),
        ))

    allocs = {}
    for key, a in data.get('daily_allocations', {}).items():
        allocs[key] = DepotDayBudget(
            depot_id=a['depot_id'],
            date=_d(a['date']),
            available_vehicles=a['available_vehicles'],
            vehicle_start_positions=a['vehicle_start_positions'],
            pre_assigned_manifests=a['pre_assigned_manifests'],
            local_order_pool=a['local_order_pool'],
            pl_import_pallet_budget=a.get('pl_import_pallet_budget', 0.0),
            pl_import_order_count=a.get('pl_import_order_count', 0),
        )

    return WeeklyPlan(
        plan_id=data['plan_id'],
        created_at=_dt_(data['created_at']),
        planning_start=_d(data['planning_start']),
        planning_end=_d(data['planning_end']),
        tours=tours,
        daily_allocations=allocs,
        unassigned_order_ids=data.get('unassigned_order_ids', []),
        metrics=data.get('metrics', {}),
    )
