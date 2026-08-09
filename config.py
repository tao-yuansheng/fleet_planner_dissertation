"""Tuning knobs owned by the freight_planner pipeline.

Shared infrastructure -- depot anchors, fleet master, geographic scope --
stays in cambridge.config.

This module must stay a leaf: no imports from freight_planner (avoids
circular imports), stdlib only.
"""

import os

# --- Tour formation (multi-day batcher + vehicle choice) ---------------------
# NOTE: the tour proto's CAPACITY is deliberately NOT here — it is derived from the
# validated vehicle master via freight_planner.vehicles.fleet_capacity_ceiling(), so
# it always tracks the real fleet ceiling (28 t / 26 pal) rather than a stale copy.
TOUR_COHESION_KM: float = 200.0              # max gap to merge a stop into a tour (stops Scotland+Cornwall merging)
LIGHT_TOUR_PALLETS: float = 10.0            # below this a tour prefers a rigid over an artic (Q4)
# Vehicle-role calibration (2026-07-21): rigids are cheaper per KM (fuel 0.236 vs
# 0.327) but artics are cheaper per PALLET-km once filled — and the telematics
# split confirms reality uses them that way (tractor median 351 km/day, 62% of
# days >300 km; rigid median 160 km/day, 13% >300 km). The fuel-only cost had
# inverted this (val3 plan rigids: median 283 km/day, 44% >300 km). Two levers:
TOUR_TRACTOR_KM: float = 250.0               # a tour LONGER than this prefers a tractor even when light
DAILY_RANGE_SOFT_KM: dict = {"rigid": 387.0, "van": 166.3}   # each type's OWN real P90 daily
# range (active driving days, Jan+Feb 2026 Supatrak telematics) -- recomputed 2026-07-27 via
# dissertation/table6_daily_range_calibration.py; replaces an earlier round-number guess
# (rigid 330, van 300) whose van figure sat at reality's ~99th percentile, practically inert
# excess km beyond DAILY_RANGE_SOFT_KM are priced at the type's own
# road_cost_per_km AGAIN (see alns._range_overage_cost) -- no separate flat
# GBP/km constant (removed 2026-07-27, same rationale as the FP_MAINT_MULT
# removal: an unrelated arbitrary rate doesn't earn its complexity when the
# type's own live rate already does the job, self-scaling and recalibratable).
TOUR_ORIGIN_AT_DEPOT_RADIUS_KM: float = 8.0  # a DIRECT move whose origin is within this of a depot is depot-loadable
TOUR_DEPOT_DIRECT_AS_DELIVERY: bool = True   # a DIRECT collected AT its anchor depot is planned as a depot-loaded
                                             # delivery, so same-destination far orders consolidate onto one tour
                                             # (2026-07-15). --no-tour-depot-direct-as-delivery ablates.
DAILY_DEPOT_DIRECT_AS_DELIVERY: bool = True  # daily analogue (2026-07-17, ST4 8JB): a SAME-DAY DIRECT whose origin
                                             # is collocated with its source depot is EMITTED as a depot-loaded
                                             # delivery (single leg, no XDOCK alternative), so same-origin orders
                                             # co-load into one multi-drop run instead of atomic out-and-back arcs.
                                             # --no-daily-depot-direct-as-delivery ablates.
DAILY_ORIGIN_AT_DEPOT_RADIUS_KM: float = 2.0  # collocation radius for the DAILY rule — deliberately tighter than
                                              # the tour side's 8 km: on a daily trip an unpriced approach is real
                                              # km per order, not tour-scale noise.
COLLOCATED_STAGING_MIN: float = 30.0  # collection window open -> freight loadable at the dock; drives the
                                      # reclassified leg's departure floor (window-open anchored, NOT the
                                      # deadline+90 pessimism that killed same-day XDOCK for these orders)
DEPOT_PINNING: bool = True  # every daily pickup/delivery is emitted with depot_bound = its depot label
                            # (pickup -> target_depot, delivery -> source_depot), enforced by the
                            # DEPOT_BOUND evaluator gate: freight is served only by vehicles homed
                            # where it physically sits; inter-depot movement rides priced trunks
                            # (2026-07-17 A1: 130/618 delivery legs teleported cross-depot, worst-case
                            # unpriced repair 12.4% of window km). --no-depot-pinning = legacy
                            # free-assignment emission (the teleport ablation).
FULLFLEET_COMBINED_STAGING: bool = True  # 2026-07-20: a multi-day FULL_FLEET order stages BOTH ends of its
                            # overnight XDOCK at the depot minimising COMBINED collect+deliver deadhead, not the
                            # collection-nearest depot. Teleport-safe (one depot both legs; we own both, no trunk).
                            # OFF = legacy per-endpoint staging (collection -> nearest-collect depot).
FLEET_DAY_START_HOUR: int = 6  # VEHICLE layer of the two-layer window (2026-07-21): the telematics
                               # movement curve shows the fleet rolling from 06:00 (60% of peak by
                               # 06:00-07:00) — vehicles may depart/load/drive from 06:00, but
                               # CUSTOMER service windows open at shared.config.CUSTOMER_DAY_START
                               # (08:00, the first-delivery wave) so plans can't deliver at hours
                               # customers demonstrably don't receive; the readiness-lag anchor
READINESS_LAG_MIN: float = 0.0  # EXPERIMENT knob (A2, 2026-07-18), default 0 = off. Floors PL_IMPORT delivery
                                # legs' depart_floor to day-start (06:00) + M, modeling import freight that
                                # lands by day-trunk rather than the 04:30 night trunk. Bounds the sensitivity
                                # of the headline to the UNOBSERVABLE per-order depot-arrival time (origin
                                # stamps are 97.7% HH:00 placeholders). Floors ONLY import deliveries — NOT the
                                # vehicle start, pickups, exports, local, or crossdock legs — so the fleet does
                                # other work in the morning and imports ride later trips (freight-availability
                                # gate, NOT a shorter operating day). --readiness-lag-min M.
SOFT_DELIVERY_WINDOWS: bool = True  # 2026-07-18: deliver-late is ALLOWED but penalized (convex tardiness +
                                    # small earliness) instead of a hard TIME_WINDOW cutoff, so the solver
                                    # delivers slightly late rather than slipping a whole day. Service
                                    # hierarchy: on-time < early < late < slip/unserved (slip/unserved handled
                                    # by the lexicographic coverage tier). --hard-time-windows ablates to the
                                    # hard cutoff. Delivery legs only; pickups keep hard windows.
TARDINESS_COEF: float = 0.05        # GBP per (minute late)^2 (convex). SEED value — calibration pending the
                                    # in-universe settle (do not cite before calibration).
TARDINESS_POWER: float = 2.0        # convex exponent (mirrors the overtime late-ramp: tolerate tiny, punish big)
EARLINESS_COEF: float = 0.1         # GBP per minute early (linear, small nudge toward on-time). SEED value.
TOUR_DAY_ELAPSED_CAP_MIN: float = 13.0 * 60.0  # tour duty day (drive+service); 05:00 + 13h = 18:00
TOUR_DAY_START_HOUR: int = 5                   # clock anchor for emitted tour stop times.
                                               # Operating window is 05:00-19:00 (user rule
                                               # 2026-07-16): start earlier -> end earlier; the
                                               # 10h driving / 13h duty caps are unchanged.
MULTIDAY_MIDLEG_OVERNIGHT: bool = True         # A: end a tour-day part-way along the leg (carry overnight coord).
                                               # Default ON 2026-07-21 (user: intended default; had shipped OFF and
                                               # was never flipped). False = park at last stop. NOTE: the split point
                                               # interpolates time∝km along the leg — approximate under OSRM per-road
                                               # durations (upgrade deferred 2026-07-15 while the flag was off).
TOUR_ATTACH_ENABLED: bool = True               # intraday: free-ride a failed far order (NO_FEASIBLE_ROUTE/TOUR/PAIR) onto an in-flight tour's mutable tail, no new tour day. Default ON 2026-07-16 — coverage-safe (only serves an otherwise-unserved order); set False to ablate.
TOUR_COMMISSION_ENABLED: bool = True           # intraday: dispatch a FRESH one-vehicle tour on an idle truck for a
                                               # far order neither the daily pass nor tour-attach can serve (1b,
                                               # 2026-07-16 — the "phone an idle driver mid-morning" move). Day-1
                                               # duty starts at the dispatch floor; the delivery promise is a hard
                                               # deadline. Config-only knob; set False to ablate.
TOUR_DIRECT_OVERNIGHT_SPLIT: bool = True       # a two-point DIRECT leg that no longer fits the day may COLLECT today,
                                               # sleep at the collection point, and deliver tomorrow (user rule
                                               # 2026-07-16 — the real overnight-direct move; before this the atomic
                                               # leg slid whole to the next day). False = atomic (legacy).
TOUR_OSRM_DURATIONS: bool = True               # tours time legs with OSRM per-road-type durations (like the daily
                                               # router) instead of flat 50 km/h (gate) / 80 km/h (executor), so the
                                               # tour boundary + scheduling track real road speed (2026-07-15).
                                               # --no-tour-osrm-durations ablates to the flat model (byte-identical).

# --- Rolling dispatcher cadence -----------------------------------------------
MICRO_EVERY_MIN: int = 30                      # minutes between micro insertion passes
                                               # (user rule 2026-07-14: a micro costs ~2 s
                                               # wall — 52 passes ≈ 100 s over a 6-day run —
                                               # so the cadence is a service-level choice,
                                               # not a compute constraint; 30 min halves a
                                               # new booking's wait for its first insertion
                                               # attempt). CLI --micro-every-min overrides;
                                               # pre-2026-07-14 replays need the old 60.

# --- Statutory breaks & waits -------------------------------------------------
DRIVE_BREAK_AFTER_MIN: float = 270.0           # EU 561/2006: break owed after 4.5h cumulative driving
DRIVE_BREAK_MIN: float = 45.0                  # statutory break length (minutes)
MAX_STOP_WAIT_MIN: float = 90.0                # daily routes: max curbside wait at a non-first stop
MAX_DUTY_H_PER_DAY: float = 13.0               # agreed operational shift: a DRIVER'S planned day never
                                               # exceeds 13h (user rule 2026-07-14). Since 2026-07-16 this
                                               # IS the day's end bound: per-vehicle telematics shift walls
                                               # were removed (fleet available from 06:00, no shift_end;
                                               # 19:00 soft — coverage first, overtime cost design pending).
                                               # Driving is capped separately (MAX_DRIVING_H_PER_DAY=10).

# --- Catchment -----------------------------------------------------------------
CATCHMENT_PERCENTILE: float = 95.0     # per-vehicle radius = P95 of its history
CATCHMENT_MIN_SAMPLES: int = 20        # fewer samples -> fall back to the type radius
CATCHMENT_RADIUS_FLOOR_KM: float = 30.0  # minimum radius even when calibration yields lower (thin/local histories)
OUT_OF_AREA_KM_FACTOR: float = float(os.environ.get("FP_OA_FACTOR", "2.6"))  # = 2 x road factor: overshoot counts as the round-trip ROAD km driven beyond the territory; env-overridable for sweeps

# Opportunity-cost surcharge (GBP) added to the ALNS insertion delta when the
# candidate vehicle is from a scarce/dedicated depot pool (shared.scope.
# SPOKE_DELIVERY_RADIUS_KM) AND a non-scarce alternative exists for the same job
# (decision-audit #9, 2026-07-26): marginal-cost-only ranking let an already-
# active, floor-sunk scarce-depot vehicle beat an idle large-depot vehicle
# purely because activating a fresh vehicle-day costs more than adding to a busy
# one -- silently consuming scarce capacity a later scarce-exclusive job then
# had none of. Sized to at least one full guaranteed-shift floor at the adopted
# rates (9h x up to ~£16.05/h tractor = ~£144), so it reliably outweighs the
# floor-sunk-cost bias that caused the original failure (the audit's own
# quoted ~£428 gap used the pre-recalibration driver rates, ~3x today's).
# Never applied when there is no alternative, so scarce-depot-only work never
# gets more expensive and coverage cannot drop because of it.
SCARCE_DEPOT_HEADROOM_GBP: float = 150.0

# --- Driver-day activation cost (vehicle-day fixed cost, spec 2026-07-14) -------
# The optimizer's objective is otherwise fuel-per-km only, so it has no reason to
# prefer reusing an already-working vehicle over opening a fresh one for a small
# job. This models the marginal cost of activating one more DRIVER for the day.
# Depreciation/standing cost is deliberately excluded (it is sunk: incurred whether
# the vehicle is driven or parked). The rate table and cost function live in
# vehicle_cost.py (driver_day_cost).
# DEFAULT ON since 2026-07-15: a converged week (2026-01-12..18) cut vehicle-days
# 229->198 (-13.5%) at IDENTICAL coverage vs the fuel-only objective. Set False (or
# --no-vehicle-day-cost / FREIGHT_VEHICLE_DAY_COST=0) for the fuel-only ablation.
VEHICLE_DAY_COST_ENABLED: bool = True
# Guaranteed paid minimum shift (hours). Drivers are paid at least this per active
# day regardless of load, so it is the FLOOR of the driver-day cost; hours worked
# beyond it (up to the 13h duty cap) are paid as overtime. 9.0 = P25 of per-driver
# telematics duty spans (supatrak Jan+Feb 2026, ~2,100 driver-days; median day ~10h,
# only ~13% under 8h) — the low end of a normal driver day. A conservative floor;
# confirm the contracted minimum with payroll if a firmer number is needed.
GUARANTEED_SHIFT_HOURS: float = 9.0

# --- Overtime + fairness cost (spec 2026-07-16) ---------------------------------
# With the telematics shift walls removed (19:00 soft, coverage first), late work
# is bounded only by the duty/driving caps — this block PRICES it. Two stacked
# surcharges on the straight-time driver-day cost: payroll overtime beyond the 9h
# paid floor, and an unsocial-hours premium that RAMPS with clock time past 19:00
# (x1.5 at 19:00 rising +0.25/h -> x2.0 at 21:00). The ramp makes late cost
# QUADRATIC in a vehicle-day's late hours, so spreading evening work across active
# vehicles is strictly cheaper than piling it on one — fairness emerges from the
# objective, no bookkeeping. Serve-first is lexicographic: the premium shapes WHERE
# late work lands, never WHETHER an order is served. Defaults are UK-haulage
# convention; replace with payroll figures when available.
OVERTIME_COST_ENABLED: bool = True     # False (or --no-overtime-cost) = pre-2026-07-16 cost
OT_DUTY_MULTIPLIER: float = 1.5        # pay rate for working hours beyond the paid floor
LATE_PREMIUM_START_HOUR: float = 19.0  # clock hour the unsocial ramp starts
LATE_PREMIUM_BASE: float = 0.5         # premium at ramp start (+50% -> x1.5)
LATE_RAMP_PER_HOUR: float = 0.25       # multiplier slope per hour past the start
# A depot gap >= this many hours ends a duty CHAIN: the 13h duty cap applies per
# chain (split shift — the driver rests or swaps), and chain gaps are unpaid.
SPLIT_SHIFT_GAP_H: float = 3.0

# --- Mega-shipper shuttle carve-out (K1, spec 2026-07-03) -------------------
# An address-day qualifies for dedicated shuttle trips when its same-direction
# volume reaches one artic load; a packed bin only ships as a shuttle trip when
# nearly full (exact-full is unattainable with 1-5-pallet orders).
SHUTTLE_ENABLED: bool = True
SHUTTLE_MIN_PALLETS: float = 26.0
SHUTTLE_MIN_FILL: float = 0.9

# --- Zero-cost same-address merge sweep (K1 component 2) --------------------
# Post-ALNS pass collapsing same-day same-address split visits when the merge is
# feasible and net-km >= 0. Operational realism, not a km saver (replay-proven
# km-neutral) — never applies a net-negative merge.
MERGE_SWEEP_ENABLED: bool = True

# --- Same-address dwell merge (evaluator-level, spec 2026-07-12) -------------
# Customer service dwell is a per-VISIT cost, but the model emits one leg per
# order. When True, contiguous pickup/delivery rows at the same coordinates
# share one fixed vehicle-type dwell instead of charging it once per order.
# Read in evaluate_route and evaluate_tour, so it covers daily routes, tours
# and intraday inserts uniformly.
SAME_ADDRESS_DWELL_MERGE: bool = True   # default ON 2026-07-21 (user: designed to be on;
                                        # had shipped OFF awaiting a flag-on smoke that never ran)

# --- Nightly B37 hub trunk as a fixed scheduled service (T1, spec 2026-07-04) ---
# TRUNK_DECK_PALLETS = 52 (double-deck trailer, RESTORED 2026-07-21). The 2026-07-12
# single-deck 26 was a proxy — "2x night trips stand in for the un-modelled day
# flow" — chosen when the day trunk couldn't be quantified. The hub dock-visit
# census now measures it (~5.0 night + ~3.9 day round-trips/day), and the proxy's
# cost is the 21:00-03:00 km overshoot (45 trips vs reality's ~25-27/3d). Decisive
# arithmetic: reality's ~9 trips/day can only carry the ~500-pallet nightly demand
# at double-deck capacity (9x26=234 < 314 peak-leg pallets; 9x52=468 covers it) —
# at 26 the freight physically couldn't all ride our trucks. Weight-safe: pallet-
# weighted density 398 kg/pallet -> full deck ~20.7t < ~28t artic payload (val3
# lanes run 27-50 pallets/trip = 11-20t). Night-only trunking is KEPT as a stated
# simplification: the model runs all ~9 trips overnight where the incumbent rosters
# ~5 by night and folds ~4 into daytime duties — TOTAL trips/km now match reality;
# only the night-time concentration is simplified (state the driver-roster cost).
# TRUNK_DEPOTS: Bedford (9 regs / 49 night reg-nights) and CB22 (12 / 44) verified
# from January telematics; STOKE deliberately absent — zero night visits, its two
# B37 visitors run 10:00-17:00 daytime hub drops inside normal routes.
TRUNK_ENABLED: bool = True
TRUNK_DECK_PALLETS: float = 52.0
TRUNK_DEPOTS: tuple = ("BEDFORD", "CB22")
# Depots with a SAME-DAY trunk to the hub (no night trunk). Stoke: telematics shows
# zero night B37 visits — its artics run 10:00-17:00 daytime hub drops — so a Stoke
# PL_EXPORT collected day N is day-trunked to B37 the SAME day (else it strands at
# the depot with no onward leg). Export-only for now. See spec 2026-07-12.
TRUNK_DAY_DEPOTS: tuple = ("STOKE",)
TRUNK_NEXT_DAY_START: str = "10:00"
# Trunk next-day availability HOLD (spec 2026-07-12). Default OFF: Jan telematics
# shows the heavy Bedford trunk artics run FULL customer days (busiest hour 08:00,
# ~78% of morning movement out on routes, ~14.6 active h/day) AND trunk at night
# with driver swaps (1.41 drivers/vehicle-day, 54% of days >=2 drivers) — vehicle
# != driver, so the tractor is NOT held. Holding it to 10:00 UNDER-utilises it by
# ~4 h/day. True restores the legacy 10:00 next-day start (a driver-rest proxy).
TRUNK_NEXT_DAY_HOLD: bool = False

# --- OSRM travel-time model (v1.1, spec 2026-07-09) -------------------------
# When True, the DAILY evaluator times each leg by OSRM road-type duration x a
# per-vehicle-type factor instead of km / AVG_SPEED_KMH. Default ON as of 2026-07-09
# (stakeholder decision: OSRM road-type times were the intended model). The
# constant-speed model remains available (set False) and is what the done E3/E5
# ablations were measured on; road_minutes falls back to it when no OSRM router is
# installed (offline/tests), so the offline path is unchanged.
USE_OSRM_DURATIONS: bool = True

# Per-type multiplier on OSRM CAR free-flow duration (car_freeflow_h x factor =
# planned truck drive time). Calibrated per (vehicle type x road class) from telematics
# moving hops (freight_planner/data/calibration/speed_factors.json): OSRM car free-flow
# already matches realized HGV truck time across all road classes (per-class factors
# 0.99-1.03), so HGVs need no correction (1.0); vans are ~25% faster (0.75). See
# speed_calibration.py.
FREIGHT_DURATION_FACTOR: dict = {"tractor": 1.0, "rigid": 1.0, "van": 0.75, "EV": 1.0}

# Long-leg (trunk) correction (2026-07-20, WT267756): the flat per-type factor is
# calibrated on each type's TYPICAL road mix — for vans that's short urban/A hops
# (per-class 0.63/0.84), so applying 0.75 to a motorway-dominated trunk leg plans
# the van ~20% faster than its own motorway calibration (0.90) — a 972-km Wales
# round trip "fit" one van day at ~117 km/h. Legs ramp linearly from the base
# factor at TRUNK_RAMP_KM[0] to the trunk factor at TRUNK_RAMP_KM[1] (road km).
# HGVs need no entry (per-class 0.99-1.03 ≈ flat 1.0).
FREIGHT_DURATION_FACTOR_TRUNK: dict = {"van": 0.90}
TRUNK_RAMP_KM: tuple = (40.0, 120.0)

# Generous reach-screen speed used ONLY when USE_OSRM_DURATIONS is on: a permissive
# upper bound so the screen never rejects a job the per-segment OSRM evaluator would
# accept (the evaluator is the real time authority). See spec Part B "screen safety".
OSRM_SCREEN_SPEED_KMH: float = 100.0

# Robustness slack on ALL planned travel times (2026-07-22): the duration factors
# above are calibrated to REALIZED door-to-door telematics times — average traffic
# is already priced in — so slack > 1 plans against worse-than-average days
# (speed x0.85 == slack 1/0.85 ~= 1.176). Multiplies duration_factor_for (every
# OSRM-duration consumer: daily routes, tours, trunk timing) and the constant-
# speed fallback in route_costs.drive_minutes. 1.0 = calibrated average (default).
# Set per-run via run_rolling --travel-slack.
TRAVEL_TIME_SLACK: float = 1.0


def duration_factor_for(vehicle_type: str, leg_km: float = 0.0) -> float:
    vt = str(vehicle_type).lower()
    base = FREIGHT_DURATION_FACTOR.get(vt, FREIGHT_DURATION_FACTOR["tractor"])
    trunk = FREIGHT_DURATION_FACTOR_TRUNK.get(vt)
    lo, hi = TRUNK_RAMP_KM
    if trunk is None or leg_km <= lo:
        return base * TRAVEL_TIME_SLACK
    t = min(1.0, (float(leg_km) - lo) / (hi - lo))
    return (base + t * (trunk - base)) * TRAVEL_TIME_SLACK

# ---- ALNS convergence gate (user rule 2026-07-13) ---------------------------
# Stop the search when the BEST objective improved by less than this many
# PERCENT over the last window of iterations (checked in whole windows).
# Anchored on the budget sweeps (120s->85.9k km, 818s->75.5k, 1800s->73.2k,
# 3600s->72.7k PLATEAU): 0.05% of a ~75k-km weekly plan = ~37 km per 500
# iterations — the honest "not worth the wall-clock" line. The old absolute
# no-improve patience never fired because tiny gains kept resetting it.
# A served-count increase always counts as improvement (coverage is never
# traded for wall-clock). Set the pct to 0 to disable (fixed-budget replays).
ALNS_CONVERGE_PCT: float = 0.15      # % best-km gain per window that keeps the run alive (2026-07-15: 0.05->0.15; the last ~2400 iters at 0.05 bought ~1%)
ALNS_CONVERGE_WINDOW: int = 500      # iterations per convergence check
ALNS_CONVERGE_MIN_ITERS: int = 1500  # never stop before this many iterations
