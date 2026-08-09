"""Per-vehicle-type running cost.

The optimizer's first cost layer: fuel cost per km, differentiated by vehicle
type. Rates are the **measured** figures derived tank-to-tank from the Jigsaw
fuel cards, diesel fills only, over the full Jan+Feb 2026 study window:

    L/100km = 100 * SUM(litres) / SUM(odometer - previousOdometer)   (per type)
    GBP/km  = (L/100km / 100) * mean diesel price (GBP 1.0432 / L)

    44t artic (tractor)  31.4 L/100km  ->  GBP 0.327 / km
    rigid (7.5-26t avg)  22.7 L/100km  ->  GBP 0.236 / km
    van                  17.0 L/100km  ->  GBP 0.177 / km

A 44t artic burns ~1.4x the rigid average per km. These replace the old
pure-km objective so the planner stops treating a tractor as a free
capacity upgrade. (Driver-hour and standing-day costs are later layers and
use the declared rates in profitability_report/vehicle_cost_rates.json.)

DERIVATION + PROVENANCE: freight_planner/dissertation/table3_fuel_cost_card.py
(self-verifying; recomputes from the raw Jigsaw CSVs). The earlier Jan-only rates
(tractor 0.319 / rigid 0.216 / van 0.150) produced results dated before this
update; adopting Jan+Feb doubles the sample and smooths a February rigid effect.
Tractor is stable across months; van rests on a small sample (3 vehicles).
NOTE: the objective is unchanged until the next planner re-run.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from freight_planner import config as _config
from freight_planner.config import OUT_OF_AREA_KM_FACTOR

# Measured GBP/km by vehicle_type, Jan+Feb 2026 (see module docstring; derived
# by freight_planner/dissertation/table3_fuel_cost_card.py).
FUEL_GBP_PER_KM: dict[str, float] = {
    "tractor": 0.327,
    "rigid": 0.236,
    "van": 0.177,
}

# Rigid is the safe fallback for any unrecognised/blank type (it is the modal
# fleet vehicle and sits mid-range, so it neither over- nor under-prices).
DEFAULT_FUEL_GBP_PER_KM: float = FUEL_GBP_PER_KM["rigid"]


def fuel_cost_per_km(vehicle_type: str) -> float:
    """GBP of fuel per road-km for ``vehicle_type`` (case-insensitive).

    Unknown or blank types fall back to the rigid rate. Setting the env var
    ``FREIGHT_FUEL_UNIFORM`` collapses every type to the rigid rate, which makes
    the cost objective equivalent to the old pure-km objective (cost = r * km has
    the same argmin as km) — an ablation switch for controlled before/after runs.
    """
    if os.environ.get("FREIGHT_FUEL_UNIFORM", "").strip().lower() in {"1", "true", "yes"}:
        return DEFAULT_FUEL_GBP_PER_KM
    return FUEL_GBP_PER_KM.get(str(vehicle_type or "").strip().lower(), DEFAULT_FUEL_GBP_PER_KM)


# Maintenance/repair & tyres (R&M) per road-km — the SECOND per-km objective layer
# (2026-07-25). Fuel alone (0.24-0.33/km) under-prices distance: the true marginal cost of
# a km is ~2x fuel once R&M/tyres are counted, and that gap let the solver over-drive cheap
# vehicles (driver:fuel ran 4:1 vs reality's ~1.2:1). Industry ex-fuel R&M band for a mixed
# HGV fleet ~= GBP 0.10-0.20/km; adopted mid-band per type. This is a per-KM cost ONLY —
# standing costs (insurance/financing/overheads/VED) are per-vehicle-DAY and would push
# toward FEWER vehicles / MORE km, so they are deliberately NOT added here.
MAINTENANCE_GBP_PER_KM: dict[str, float] = {
    "tractor": 0.16,
    "rigid": 0.12,
    "van": 0.08,
}
DEFAULT_MAINTENANCE_GBP_PER_KM: float = MAINTENANCE_GBP_PER_KM["rigid"]


def maintenance_cost_per_km(vehicle_type: str) -> float:
    """GBP of maintenance/tyres/R&M per road-km for ``vehicle_type`` (rigid fallback).

    A ``FP_MAINT_MULT`` ablation multiplier existed here 2026-07-25 to
    2026-07-27 for stress-testing the R&M layer's weight in the objective;
    removed once sweeping it showed negligible effect on results, and the
    driver-cost recalibration corrected the objective's overall cost
    composition, so a dedicated R&M sensitivity knob no longer earned its
    complexity."""
    return MAINTENANCE_GBP_PER_KM.get(
        str(vehicle_type or "").strip().lower(), DEFAULT_MAINTENANCE_GBP_PER_KM)


def road_cost_per_km(vehicle_type: str) -> float:
    """Total per-km OBJECTIVE cost = fuel + maintenance (the price of distance the search
    optimizes). Fuel and maintenance stay separate functions for reporting/provenance.

    This is also the rate that prices the out-of-area catchment penalty's
    phantom km (``out_of_area_penalty_km``) — one live rate for both real and
    phantom distance."""
    return fuel_cost_per_km(vehicle_type) + maintenance_cost_per_km(vehicle_type)


def out_of_area_penalty_km(dist_km: float, catchment_km: float) -> float:
    """Phantom km added to the RANKING cost for a job beyond its vehicle's
    catchment: each km beyond the radius counts ``OUT_OF_AREA_KM_FACTOR`` times
    extra. Zero within the radius or when the catchment is unknown (<= 0), so
    vehicles without a calibrated radius are never penalized. Never appears in
    reported/physical km — ranking only, like the per-type fuel rates."""
    if catchment_km <= 0.0:
        return 0.0
    return OUT_OF_AREA_KM_FACTOR * max(0.0, float(dist_km) - float(catchment_km))


# --- Driver-day activation cost (spec 2026-07-14) ----------------------------
# Driver wage per on-duty hour, by licence class (adopted 2026-07-27): UK DVSA
# licence-class wage survey, "avg adjusted for hours paid" (the upper-bound column) --
#   Class B     (van, up to 3.5t)          13.48
#   Class C1    (light rigid, up to 7.5t)  13.67  \_ averaged into one "rigid" rate
#   Class C     (LGV rigid)                15.12  /  (this model has no light/heavy split)
#   Class C+E   (artic/tractor)            16.05
# The PRIOR rates (47.59 tractor / 40.97 rigid+van) came from
# profitability_report/vehicle_cost_rates.json's `driving_hourly_gbp` (v2.1) -- a
# fully-loaded/overhead-inclusive figure, not a base driving wage. Using it as the
# ROUTING objective's per-hour rate overweighted driver cost relative to distance
# (see METHODOLOGY_FORMULAS.md M3.2); that file's rates are unchanged, since it
# serves a different (profitability/invoicing) purpose where the fully-loaded
# figure may be the correct one. NB: this is the DRIVER cost only. The £70/day
# standing cost in that file is DEPRECIATION (incurred whether the vehicle is
# driven or parked = sunk) and is deliberately NOT used here — putting a sunk
# cost in the objective would penalize USING an owned vehicle. See
# docs/superpowers/specs/2026-07-14-*.
DRIVER_GBP_PER_HOUR: dict[str, float] = {
    "tractor": 16.05,
    "rigid": 14.395,
    "van": 13.48,
}
DEFAULT_DRIVER_GBP_PER_HOUR: float = DRIVER_GBP_PER_HOUR["rigid"]

_TRUE = {"1", "true", "yes"}
_FALSE = {"0", "false", "no"}



# Per-type env overrides for driver-cost sensitivity runs (added 2026-07-27): lets
# a run substitute an alternate driver-rate set (e.g. the PRIOR fully-loaded
# 47.59/40.97/40.97 rates, for a before/after comparison) without touching the
# adopted DRIVER_GBP_PER_HOUR calibration used everywhere else. Unset -> the
# adopted defaults (bit-identical to before this override existed).
_DRIVER_RATE_ENV: dict[str, str] = {
    "tractor": "FP_DRIVER_GBP_TRACTOR",
    "rigid": "FP_DRIVER_GBP_RIGID",
    "van": "FP_DRIVER_GBP_VAN",
}


def driver_hourly_gbp(vehicle_type: str) -> float:
    """GBP per on-duty hour for a driver of ``vehicle_type`` (case-insensitive,
    rigid fallback for unknown/blank types). Overridable per type via
    ``FP_DRIVER_GBP_TRACTOR`` / ``_RIGID`` / ``_VAN``."""
    vt = str(vehicle_type or "").strip().lower()
    if vt not in DRIVER_GBP_PER_HOUR:
        vt = "rigid"
    env_name = _DRIVER_RATE_ENV[vt]
    override = os.environ.get(env_name, "").strip()
    if override:
        return float(override)
    return DRIVER_GBP_PER_HOUR[vt]


def vehicle_day_cost_enabled() -> bool:
    """Is the driver-day activation cost active? Env ``FREIGHT_VEHICLE_DAY_COST``
    overrides the ``config.VEHICLE_DAY_COST_ENABLED`` default. The config module is
    referenced at call-time so CLI/runtime toggles take effect."""
    env = os.environ.get("FREIGHT_VEHICLE_DAY_COST", "").strip().lower()
    if env in _TRUE:
        return True
    if env in _FALSE:
        return False
    return bool(_config.VEHICLE_DAY_COST_ENABLED)


def guaranteed_shift_hours() -> float:
    """Paid minimum shift (hours) = the floor of the driver-day cost. Env
    ``FREIGHT_GUARANTEED_SHIFT_HOURS`` overrides ``config.GUARANTEED_SHIFT_HOURS``."""
    env = os.environ.get("FREIGHT_GUARANTEED_SHIFT_HOURS", "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return float(_config.GUARANTEED_SHIFT_HOURS)


def driver_day_cost(vehicle_type: str, duty_hours: float) -> float:
    """GBP labour cost of activating one vehicle-day: a paid guaranteed-shift
    floor plus straight time for hours beyond it (the per-chain 13h duty ceiling
    is a hard feasibility constraint in evaluate_day, so ``duty_hours`` is already
    bounded).

        cost = driver_hourly_gbp(type) * max(guaranteed_hours, duty_hours)

    This is the LEGACY/base formula; the objective sites use
    :func:`driver_day_cost_ev`, which adds the 2026-07-16 overtime + late-ramp
    surcharges on top (and reduces to this exact formula when
    ``OVERTIME_COST_ENABLED`` is off). Returns 0.0 when disabled (=> objective
    byte-identical) or for an empty vehicle-day."""
    if not vehicle_day_cost_enabled():
        return 0.0
    if duty_hours <= 0.0:
        return 0.0
    return driver_hourly_gbp(vehicle_type) * max(guaranteed_shift_hours(), float(duty_hours))


# --- overtime + fairness surcharges (spec 2026-07-16) ---------------------------

def overtime_cost_enabled() -> bool:
    """Env ``FREIGHT_OVERTIME_COST`` overrides ``config.OVERTIME_COST_ENABLED``."""
    env = os.environ.get("FREIGHT_OVERTIME_COST", "").strip().lower()
    if env in _TRUE:
        return True
    if env in _FALSE:
        return False
    return bool(_config.OVERTIME_COST_ENABLED)


def _dt(ts):
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


def span_hours(day_ev) -> float:
    """Whole-day span (first departure -> last return, depot idle INCLUDED) —
    the legacy duty measure, kept for the OVERTIME-off ablation path."""
    a, b = _dt(getattr(day_ev, "day_start", "")), _dt(getattr(day_ev, "day_end", ""))
    if a is None or b is None:
        return 0.0
    return max(0.0, (b - a).total_seconds() / 3600.0)


def _chain_spans(day_ev) -> list[tuple]:
    """Duty CHAINS of an evaluated vehicle-day: consecutive trips whose depot gap
    is under ``SPLIT_SHIFT_GAP_H`` share a chain (the 30-min reload never splits);
    a bigger gap means the driver rests or swaps — a new chain. Returns
    [(start_dt, end_dt), ...] from the trips' route_start/route_end (present even
    at detail=False)."""
    gap_h = float(_config.SPLIT_SHIFT_GAP_H)
    chains: list[tuple] = []
    cur_start = cur_end = None
    for tev in getattr(day_ev, "trip_evaluations", ()) or ():
        s, e = _dt(getattr(tev, "route_start", "")), _dt(getattr(tev, "route_end", ""))
        if s is None or e is None:
            continue
        if cur_end is not None and (s - cur_end).total_seconds() / 3600.0 < gap_h:
            cur_end = max(cur_end, e)
        else:
            if cur_start is not None:
                chains.append((cur_start, cur_end))
            cur_start, cur_end = s, e
    if cur_start is not None:
        chains.append((cur_start, cur_end))
    return chains


def working_hours(day_ev) -> float:
    """Paid working time = sum of chain spans; split-shift depot idle is unpaid."""
    return sum((e - s).total_seconds() / 3600.0 for s, e in _chain_spans(day_ev))


def late_premium_gbp(vehicle_type: str, day_ev) -> float:
    """Unsocial-hours surcharge: working minutes past ``LATE_PREMIUM_START_HOUR``
    are priced at a premium that RAMPS with clock time —
    ``hourly x (BASE + RAMP x t)`` at t hours past the start. Closed form per trip
    span: ``hourly x (BASE·(t1−t0) + RAMP/2·(t1²−t0²))``. Quadratic in the day's
    total late hours -> one vehicle's second late hour always costs more than
    another active vehicle's first (the fairness mechanism, user probe
    2026-07-16). Premium attaches to trip spans only — depot idle is never late
    work."""
    base = float(_config.LATE_PREMIUM_BASE)
    ramp = float(_config.LATE_RAMP_PER_HOUR)
    start_h = float(_config.LATE_PREMIUM_START_HOUR)
    total = 0.0
    for tev in getattr(day_ev, "trip_evaluations", ()) or ():
        s, e = _dt(getattr(tev, "route_start", "")), _dt(getattr(tev, "route_end", ""))
        if s is None or e is None or e <= s:
            continue
        day0 = s.replace(hour=0, minute=0, second=0, microsecond=0)
        anchor = day0 + timedelta(hours=start_h)
        t0 = max(0.0, (s - anchor).total_seconds() / 3600.0)
        t1 = max(0.0, (e - anchor).total_seconds() / 3600.0)
        if t1 > t0:
            total += base * (t1 - t0) + (ramp / 2.0) * (t1 * t1 - t0 * t0)
    return driver_hourly_gbp(vehicle_type) * total


def driver_day_cost_ev(vehicle_type: str, day_ev) -> float:
    """GBP labour cost of one EVALUATED vehicle-day (the objective's driver term):

        paid_base = hourly x max(floor, working_hours)      # chains; idle unpaid
        duty_ot   = hourly x (OT_DUTY_MULTIPLIER-1) x max(0, working_hours - floor)
        late      = late_premium_gbp(...)                   # ramp past 19:00

    ``OVERTIME_COST_ENABLED`` off reproduces the legacy cost exactly
    (straight time on the whole-day span, idle included). 0.0 when the
    vehicle-day cost is disabled or the day is empty."""
    if not vehicle_day_cost_enabled():
        return 0.0
    if day_ev is None:
        return 0.0
    if not overtime_cost_enabled():
        return driver_day_cost(vehicle_type, span_hours(day_ev))
    wh = working_hours(day_ev)
    if wh <= 0.0:
        return 0.0
    hourly = driver_hourly_gbp(vehicle_type)
    floor = guaranteed_shift_hours()
    base = hourly * max(floor, wh)
    ot = hourly * (float(_config.OT_DUTY_MULTIPLIER) - 1.0) * max(0.0, wh - floor)
    return base + ot + late_premium_gbp(vehicle_type, day_ev)
