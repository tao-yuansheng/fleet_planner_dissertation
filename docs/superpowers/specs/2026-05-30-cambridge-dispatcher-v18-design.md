# Cambridge Dispatcher v1.8 — Window-Aware Solver + Honest Cost Reporting

**Status:** design
**Predecessor:** v1.7 (OSRM live; median km gap +6.1 %; planned on-time inflated due to broken comparison granularity on actual side)
**Goal:** Make the Cambridge simulation respect customer delivery windows when scheduling, report on-time at the same granularity on both planned and actual sides, separate activation cost from fuel cost, and let per-stop service time vary by load.

---

## Why v1.8

v1.7 fixed routing accuracy. The validation report still has three honesty gaps:

1. **Solver ignores per-stop windows.** `feasible()` only checks `shift_end`. The solver picks orderings purely by cost. As a result the planned on-time count is a post-hoc readout, not a constraint the planner respects. Real dispatchers re-order stops to meet customer windows.
2. **On-time metric is unhonest on the actual side.** [actuals_loader.py:106-108](../../simulation/actuals_loader.py#L106-L108) says the comparison is at date granularity, but [line 144-146](../../simulation/actuals_loader.py#L144-L146) does full-datetime comparison. Most Qargo `destination_requested_start_timestamp_local` rows are date-only (parsed as 00:00). Actual delivery times sit between 07:49 and 13:59. Under datetime comparison every same-day delivery looks late (13:00 > 00:00). Re-doing the calculation at date granularity shows network-wide on-time = 98.6 % for Jan 7, not the reported 5 %.
3. **Planned cost mislabeled as "Fuel cost"; actual fuel is fleet-wide, not CB22-only.** The "Fuel cost GBP" column in the day report adds `VEHICLE_ACTIVATION_COST × vehicles` to fuel × km. The actual side sums Jigsaw diesel transactions for the whole company that day, with no filter to CB22 rigid registrations. Days like Jan 9 show "more planned km, less planned fuel" because activation overhead dominates.

v1.8 fixes all three plus adds service-time variation.

## Architecture

Five tasks, each touching a small surface:

1. **Task 1 — Window-aware solver + reconciled on-time metric.** Plumb `delivery_window` (the existing `(start, end)` tuple from `ScopedOrder`) into `DeliveryStop`. `_walk_schedule` already produces per-stop arrivals; add cumulative lateness (`max(0, arrival - window_end)` summed over stops) to `RouteSchedule`. Modify `fleet_objective` to include a `LATENESS_PENALTY × total_lateness_minutes` term. Rebuild `compute_planned_on_time` and `_qargo_actuals.on_time_rate_actual` to use the three-case rule (see D2). **Also fix the actuals to use the correct delivery field:** today it reads `destination_end_timestamp_local` (DEPARTURE from destination), but the right field per the Qargo data dictionary is `destination_timestamp_local` (ARRIVAL = "delivery time"). Filter to actually-delivered statuses (`INVOICE_POSTED`, `DONT_INVOICE`, `INVOICED`, `INVOICE_READY`, `COMPLETED`) — exclude `TO_PLAN`, `PLANNED`, `QUOTE`, `IN_TRANSIT`, `DEPARTED`, `CANCELLED`, `TEMPLATE` (these are scheduled / imputed, not observed). Validated Jan 7: 410 delivered orders, 397 on-time = **96.8 %** under the three-case rule.
2. **Task 2 — Cost split + Jigsaw filter.** Split `route_cost` reporting into `activation_gbp` (vehicle opening) and `fuel_gbp` (km × rate). Update report columns. Add a `vehicle_regs` filter argument to `_jigsaw_fuel_gbp` so Cambridge can pass `CB22_RIGIDS` and get CB22-only diesel.
3. **Task 3 — Per-stop service time from load.** Replace the flat `_SERVICE_HOURS_PER_STOP = 20 min` with `service_h = SERVICE_BASE_MIN + SERVICE_PER_PALLET_MIN × pallets`. Defaults derived from telematics dwell-time study (deferred sub-task if dwell data isn't readily available; ship with conservative defaults).
4. **Task 4 — Tractor pool verification.** Run the same telematics home-depot test that confirmed `CB22_RIGIDS` (`AssetType == 'Tractor Unit'` AND overnight at CB22 ≥ 90 % of Jan–Feb). Replace the unverified `CB22_TRACTORS` constant with the derived set; write the result to a new derived JSON so the test is reproducible.
5. **Task 5 — Actual-side stop matcher → fix postcode Jaccard.** Today `actual_per_veh.dest_districts` is hardcoded `set()` ([backtest.py:362-364](../../cambridge/backtest.py#L362-L364)), so Postcode Jaccard is always 0. Build a stop matcher: for each rigid on day X, detect dwell points in telematics (consecutive pings within 100 m for ≥5 min), then reverse-lookup each dwell to the nearest postcode-district (CB2, SG6, etc.) via `postcode_cache`. Populate `dest_districts` from those matches; Jaccard becomes meaningful.

The per-vehicle planning view (`investigations/show_vehicle_plan.py`) inherits Task 1's window honesty without code changes.

**Note on PL_IMPORT freight readiness (cut from v1.8 scope):** an earlier draft included a per-order rolling-horizon readiness task using `origin_end_timestamp_local`. Data review showed `origin_end` is unreliable — 184 of 198 Jan 7 PL_IMPORT rows have it set to a hardcoded `06:00:00` placeholder (zero-dwell origin pickup, no real timestamp recorded). The hub-leg field that would correctly answer this question (`load_unload_container_*_timestamp_local`) is 0/21449 populated. Telematics carries the trunk-arrival signal but mapping individual orders to specific trunks requires an order_id → trunk_id link that doesn't exist in the data. Conclusion: the current `day_start (06:00)` default is the right baseline given the available data — most Cambridge inbound freight does arrive overnight before 06:00. See Non-Goals.

## Design Decisions

### D1 — Soft window with lateness penalty

`LATENESS_PENALTY` in £/minute added to `fleet_objective`. Starts at a small calibrated value (suggestion: £1/minute — to be tuned by sensitivity sweep). Solver tries to fit windows; if no insertion saves enough cost to avoid late delivery, the order is still dispatched but penalised. Aligns with the project's "deliver everything, then minimise lateness" rule.

Trade-off accepted: hard windows would conflict with the "deliver everything" priority and would push some orders to unassigned. Conditional (hard for timed, soft for date-only) is more realistic but adds branching the solver doesn't need yet.

### D2 — Three-case on-time logic (matches real operations rule)

Both `compute_planned_on_time` (Cambridge backtest) and `_qargo_actuals` (actuals loader) use the SAME three-case rule:

```python
def is_on_time(arrival, requested_start, time_window_value):
    """
    Case A — has destination_time_window_value (e.g. "10:00 - 12:00"):
        on-time = arrival <= window_end at the requested date.
    Case B — no window value, but requested_start has a real time (not midnight):
        on-time = arrival <= requested_start (delivered at or before the
        customer's requested moment).
    Case C — no window, requested_start = 00:00 (date-only, no time specified):
        on-time = arrival.date() <= requested_start.date() (delivered on or
        before the requested day).
    """
```

**Why three cases, not one:** the rule a real dispatcher uses depends on what the customer told the system:
- Some customers booked a specific slot ("10:00 - 12:00") — that's a hard window.
- Some asked for "as early as possible" with a specific cut-off time ("11:00") — soft "deliver by".
- Most just said "deliver on this day" — Qargo stores those as 00:00 midnight (the date-only marker).

The earlier v1.7 logic (`actual <= deadline` at datetime granularity) treated EVERY date-only entry as "deadline = midnight", which made every same-day delivery look late. The new rule treats date-only as "any time that day is fine," matching the operational reality.

**Two corrections from v1.7** to the actuals_loader logic:

1. Use `destination_timestamp_local` (arrival, i.e. delivery time per Qargo data dictionary). The current code reads `destination_end_timestamp_local` (truck departure AFTER delivery) — that's the wrong field. The data dictionary entry for `delivery_timestamp` says: "Delivery time (observed or scheduled, depending on source). Missing for undelivered orders; can be inferred/imputed in some pipelines."
2. Filter by `status` to count observed-delivered rows only. `DELIVERED_STATUSES = {'INVOICE_POSTED', 'DONT_INVOICE', 'INVOICED', 'INVOICE_READY', 'COMPLETED'}`. Exclude `TO_PLAN`, `PLANNED`, `QUOTE`, `IN_TRANSIT`, `DEPARTED`, `CANCELLED`, `TEMPLATE` — those rows have scheduled / imputed timestamps that aren't actual performance.

**Validated Jan 7 numbers under the new rule:**

| Cohort | Count | On-time | Rate |
|---|---|---|---|
| All requested Jan 7 | 432 | (mixed) | — |
| Delivered statuses only | 410 | 397 | **96.8 %** |
| Case A (windowed) | 84 | 75 | 89.3 % |
| Case B (timed request, no window) | 0 | — | — |
| Case C (date-only) | 326 | 322 | 98.8 % |
| Not yet delivered (rolled forward) | 22 | n/a | n/a |

Windowed orders (89.3 %) carry stricter SLAs; date-only orders (98.8 %) effectively only need to arrive same day. The dispatcher report adds `orders_not_yet_delivered` so the user can see how many requested-for-today orders are still pre-delivery.

**Plumbing required:** `destination_time_window_value` is added to `ScopedOrder` (string column from Qargo, 28 % populated). `parse_window_end(value, requested_start) -> datetime | None` is a shared helper used by both planned and actual sides so the logic stays in one place.

### D3 — Cost split

Each route's output dict gains:

```python
'activation_gbp': VEHICLE_ACTIVATION_COST if route.stops else 0.0,
'fuel_gbp': fuel_rate × route_km,
```

Day-level totals add `planned_activation_gbp` and `planned_fuel_gbp` (sum across routes). Existing `planned_cost_gbp` is preserved as their sum so legacy consumers don't break.

Report shows three columns instead of one: Activation, Fuel, Total. The "Fuel cost GBP" label that currently exists is renamed to "Total cost GBP" and the split is shown explicitly.

### D4 — Jigsaw filter to CB22 rigids

`_jigsaw_fuel_gbp(jigsaw_df, date_str, vehicle_regs=None)`. When `vehicle_regs` is provided, filter the dataframe by registration before summing. Cambridge passes `CB22_RIGIDS`. Legacy callers pass `None` and get the existing whole-fleet behaviour.

### D5a — Tractor pool derivation

A new script `investigations/derive_v18_parameters.py` runs the same overnight-home-depot test that confirmed `CB22_RIGIDS`, filtered to `AssetType == 'Tractor Unit'` across Jan–Feb. Cutoff: a tractor is "Cambridge-pool" if ≥ 90 % of overnight stationary periods are within 1 km of `CB22_DEPOT_ANCHOR`. Output `data/Output/cambridge/tractors_derived.json` lists the qualifying registrations + their per-vehicle overnight-share percentage.

`cambridge/config.py::CB22_TRACTORS` is replaced by a loader that reads this JSON at import time. If the JSON is missing the current hard-coded set is used (graceful fallback for fresh checkouts).

### D5c — Actual-side stop matcher (postcode Jaccard fix)

A new helper `actual_dest_districts(vehicle_id, telem_df, day, postcode_cache) -> set[str]`:

1. Filter `telem_df` to the (vehicle, day) pair.
2. Detect dwell points: consecutive pings whose pairwise distance < 100 m AND total span ≥ 5 min AND GPSSpeed median during the dwell < 2 km/h.
3. For each dwell, compute the centroid (lat, lon).
4. Reverse-match against `postcode_cache`: find the nearest postcode whose coords are within 500 m; emit its outward part (e.g., "CB22" → "CB22").
5. Skip dwells within 500 m of `CB22_DEPOT_ANCHOR` (depot dwell ≠ a delivery).
6. Return the set of outward postcodes.

`backtest.py` calls this for each active vehicle and writes the result into `actual_per_veh[vid]['dest_districts']`. `level1_metrics` already computes Jaccard over these sets, so once the actual side is non-empty the metric becomes meaningful.

### D5 — Service time = base + per-pallet

```python
SERVICE_BASE_MIN: float = 10.0          # check-in / paperwork
SERVICE_PER_PALLET_MIN: float = 6.0     # per-pallet tail-lift handling
service_h = (SERVICE_BASE_MIN + SERVICE_PER_PALLET_MIN * stop.pallets) / 60.0
```

Default values are calibrated guesses pending the telematics dwell study (sub-task in the plan). For a typical 2-pallet drop, service = 10 + 12 = 22 min ≈ 0.37 h. For a 5-pallet drop, 10 + 30 = 40 min ≈ 0.67 h. For a 0-pallet (parcel) drop, 10 min. Matches operational rules of thumb; the dwell study can refine the constants later.

Wired through `DeliveryStop.service_h` (already optional in the dataclass) — Cambridge dispatcher computes `service_h` per ScopedOrder when constructing `DeliveryStop`.

## Files Touched

| File | Change |
|---|---|
| `simulation/vrptw_engine.py` | Add `window_end: datetime` to `DeliveryStop` (optional, defaults to None); extend `RouteSchedule` with `total_lateness_minutes`; add `LATENESS_PENALTY` and `set_lateness_penalty()`; modify `fleet_objective` |
| `simulation/vrptw_alns.py` | Plumb `lateness_minutes` into route output dict; if window_end provided, emit it in stop dict for the planning view |
| `cambridge/dispatcher.py` | When building `DeliveryStop` from ScopedOrder, copy `delivery_window[1]` to `stop.window_end`; compute `service_h` via the per-pallet formula; in `build_freight_availability`, use Qargo `origin_end_timestamp_local + CROSS_DOCK_BUFFER_MIN` instead of `day_start` for PL_IMPORT orders |
| `cambridge/backtest.py` | Rebuild `compute_planned_on_time` to use `arrival.date() <= delivery_window[1].date()`; add `planned_activation_gbp`, `planned_fuel_gbp`, `planned_lateness_minutes` to the report; update `print_report` for cost split; integrate actual-side stop matcher and populate `actual_per_veh.dest_districts` from telematics dwell |
| `simulation/actuals_loader.py` | Fix `_qargo_actuals` to use `.dt.date` comparison; add `vehicle_regs` argument to `_jigsaw_fuel_gbp`; add `actual_dest_districts(vehicle_id, telem_df, day, postcode_cache)` returning the set of districts the vehicle dwelt at |
| `cambridge/scope.py` | Capture `origin_end_timestamp_local` per ScopedOrder so the dispatcher can read it (currently the column isn't kept once scoped) |
| `cambridge/config.py` | Add `LATENESS_PENALTY_GBP_PER_MIN`, `SERVICE_BASE_MIN`, `SERVICE_PER_PALLET_MIN` constants; replace `CB22_TRACTORS` literal with derived set loaded from `tractors_derived.json` |
| `investigations/derive_v18_parameters.py` | NEW — runs the telematics home-depot test for tractors, writes `tractors_derived.json` |
| `data/Output/cambridge/tractors_derived.json` | NEW — generated by the script above |
| `tests/test_vrptw_engine.py` | Tests for window-aware schedule and lateness in objective |
| `tests/cambridge/test_backtest.py` | Tests for date-granularity on-time, cost split, freight readiness from origin_end, stop matcher populates dest_districts |
| `tests/test_actuals_loader.py` | New test file (or extend existing) — date-granularity on-time, Jigsaw filter, dwell detection |
| `investigations/show_vehicle_plan.py` | Update flag function to use date granularity (matches the new metric) |

## Validation Plan

After implementation:

1. **Jan 7 OSRM rerun** (CAMBRIDGE_OSRM=1):
   - Planned km unchanged from v1.7 (no routing change).
   - Planned `total_cost_gbp` ≈ v1.7 `planned_cost_gbp`. Activation/fuel split adds up.
   - `planned_on_time` rises from current 147 toward 100 % at date granularity (most planned arrivals ARE on the planning day).
   - `actual_on_time` rises from 7 toward ~99 % once the date-granularity fix lands and the Jigsaw filter narrows to CB22.
   - Lateness penalty causes solver to reorder L88GNW's last two stops (currently late at 18:04 and 18:54) — net minute-lateness reduces vs v1.7.
   - **Postcode Jaccard ≥ 0.5** on Jan 7 (replacing 0.0). Exact value depends on how cleanly telematics dwells map to delivery districts.
   - **PL_IMPORT freight readiness:** a non-trivial subset of PL_IMPORT orders now have `ready_at > 06:00`. Report the fallback counter (orders falling through to day_start). If counter is high, the `origin_end_timestamp_local` field may not be populated for trunks — investigate.
2. **5-day backtest Jan 7–11**:
   - On-time rates on both sides should be in the 90-100 % range (match the network-wide 98.6 % at date granularity).
   - Cost split makes the "fuel" delta sensible (planned fuel rises with km, doesn't flat-line because of activation overhead).
   - Postcode Jaccard median ≥ 0.5 across the 5 days (was 0.0).
3. **Per-vehicle view (L88GNW)**:
   - Shows lateness in minutes per stop, not just on/off.
   - Date-granularity flag matches the metric.
4. **Tractor pool derivation:**
   - Generated `tractors_derived.json` shows per-tractor overnight-at-CB22 percentage.
   - Diff against current `CB22_TRACTORS = {AR02DEX, N8GNW, Y88RNW, N88GNW, S88RNW, R88GNW}`. Document additions/removals in the v1.8 update doc.

## Non-Goals

Out of scope for v1.8 (deferred):

- **Hard time windows for booked-slot customers.** Soft penalty is sufficient until we have data identifying which orders have hard slots.
- **Time-of-day speed variation.** Flat `TRUCK_DURATION_FACTOR = 1.24`. Tractable but separate sub-project.
- **Solver tuning under heavy load** (Jan 8 +54 % km outlier). Comes after we have the window-aware solver to see if window respect changes the picture.
- **Groupage hub routing.** Architectural; v2.x.
- **Scope-filter widening.** Deferred v1.5 item — still appropriate given subcontractor orders aren't really for our fleet.

## Risks

- **R1 — Lateness penalty too high → solver drops orders to avoid lateness.** Mitigation: start with £1/min; sweep to find the inflection. UNASSIGNED_PENALTY (£50,000) is still much higher, so dropping should never beat late delivery.
- **R2 — Date-granularity on-time loses signal.** If both sides hit ~99 %, the metric stops differentiating good plans from bad. Mitigation: track `planned_lateness_minutes` alongside the on-time count — it captures intra-day late tails the date-granularity metric misses.
- **R3 — Service time inflation breaks shift feasibility.** Going from 20 min flat to load-dependent (~10-40 min depending on pallets) could push some routes past `shift_end`. SHIFT_OVERRUN_HOURS (4h) absorbs most of this. Worst case: orders move to multi-trip / next event.
- **R4 — Jigsaw filter cuts actual fuel to near-zero.** If CB22 rigids aren't all on the Jigsaw fuel card (some might be on alternate accounts), filtering by CB22_RIGIDS could under-count. Fallback: if filtered total is suspiciously low, fall back to whole-fleet and flag.
- **R5 — `origin_end_timestamp_local` for PL_IMPORT is sparse or wrong semantics.** Hopeful interpretation: it's the trunk-leg completion time. Pessimistic: it's the original origin-pickup time at the consignor's site (irrelevant for our PL_IMPORT readiness). Mitigation: instrument the fallback counter; if > 50 % of PL_IMPORT orders fall through to day_start, treat the field as unreliable and document the gap. v1.9 candidate: derive trunk arrival from telematics on trunk vehicles directly.
- **R6 — Telematics dwell points map ambiguously to postcodes.** A dwell within 500 m of a postcode boundary could match two districts. Mitigation: pick the closest only; postcodes are dense enough in Cambridge service area that ambiguity is rare. Worst case Jaccard understates rather than overstates.
- **R7 — Tractor pool derivation needs different overnight-detection thresholds.** Tractors may behave differently than rigids (longer trips, multi-day patterns). Mitigation: use the same Jan–Feb window; report any tractor with overnight-at-CB22 between 50–90 % as "borderline" so the user can decide whether to include.

## Success Criteria

v1.8 ships when:

1. Both planned and actual on-time rates compute at date granularity and converge to similar values for the 5-day window.
2. Cost report shows activation + fuel + total separately; activation/total ratio is sensible (~30-60 % for normal days, higher when solver opens many vehicles).
3. Per-stop service time scales with pallets in the per-vehicle view.
4. Lateness penalty causes measurable reordering on at least one Jan-7-like day (L88GNW's late tail should shrink).
5. Full test suite green (was 153 + new tests).
6. No commits — local per project rule.
