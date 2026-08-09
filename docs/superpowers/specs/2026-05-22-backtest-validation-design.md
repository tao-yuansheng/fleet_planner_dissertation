# Backtest Validation Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate the ZEEFLEET dispatcher by re-running it on historical dates and comparing planned KM, cost, and order assignment rate against Supatrak telematics, Jigsaw fuel, and Qargo actuals.

**Architecture:** A standalone `run_backtest.py` script re-runs the dispatcher on a historical date's orders, then calls a new `simulation/actuals_loader.py` module to extract ground-truth figures from the three data sources. The two sides are printed side-by-side and saved as JSON. The actuals loader is a pure data function with no dispatcher dependency, keeping it independently testable.

**Tech Stack:** Python, pandas, existing `run_batch` / `load_datasets` / `pdp_route` infrastructure, pgeocode (already installed), OSRM optional.

---

## Scope

This spec covers **cost-priority backtest validation** only. Explainability annotations (per-order/per-route reasoning) are out of scope and will be a separate spec after this is validated.

---

## Components

### 1. `run_backtest.py` (new top-level script)

Mirrors `run_daily_batch.py` structure. Accepts the same date/window/routing flags, plus a `--days N` flag to loop over N consecutive dates and print an aggregate summary at the end.

```
python run_backtest.py --date 2026-01-02 [--alns] [--budget 60] [--routing osrm] [--days 5]
```

`--days` defaults to 1. When `--days N`, the script iterates from `--date` forward N consecutive dates, prints a comparison block per day, then prints an aggregate summary.

Responsibilities:
- Load datasets (same `load_datasets()` call as `run_daily_batch`)
- For each date in range: run dispatcher, load actuals, print comparison, write JSON
- If `--days > 1`: print aggregate summary across all dates at the end

### 2. `simulation/actuals_loader.py` (new module)

Pure function: `load_actuals(date_str, telem_df, jigsaw_df, qargo_df, orders) -> dict`

Returns a dict with keys:
- `active_vehicles_total` — count of distinct `AssetName` with ≥1 ping on that date
- `active_artics` — count where AssetType == 'Tractor Unit'
- `active_rigids` — count where AssetType in ('Lorry', 'Rigid Truck', 'Mini Truck', 'Service Van')
- `actual_km` — GPS-track sum (see note below)
- `actual_fuel_gbp` — diesel-only spend from Jigsaw (see note below)
- `orders_assigned_actual` — count of orders in window where any of `resource_tractor`, `resource_rigid`, `resource_van`, `resource_drawbartrailer`, `resource_trailer` is non-null (i.e. Qargo recorded a vehicle assignment)
- `assignment_rate_actual` — orders_assigned_actual / len(orders)
- `orders_on_time_actual` — count where `destination_end_timestamp_local ≤ destination_requested_start_timestamp_local` (delivery date ≤ deadline date)
- `on_time_rate_actual` — orders_on_time_actual / orders_assigned_actual (only for orders that were actually delivered)

**Trade-off notes (must be in code comments):**

**KM calculation:** Supatrak has no reliable odometer column at the row level. KM is computed as the sum of consecutive Haversine distances between sorted pings per vehicle per day. This undercounts slightly (ping interval ~2-5 min; straight-line between pings misses curves). It is consistent and reproducible.
> [NOTE] OSRM is already implemented in `routing.py` for route planning via the `/table` endpoint. However, improving GPS track accuracy would require OSRM's `/match` endpoint (map-matching), which is a different service and is not currently set up. The Haversine-between-pings method is therefore the correct choice here until `/match` is available.

**Cost comparison:** The `vehicle_cost_rates.json` separates `fuel_gbp_per_mile` and `driver_mileage_gbp_per_mile` as distinct fields (verified in current build: tractor unit £0.55/mile fuel, £0.65/mile allowance; rigid £0.42/mile fuel, £0.60/mile allowance). The planned **fuel component** is therefore directly computable as `sum(fuel_gbp_per_mile[type] × miles)` per route, and this is directly comparable to Jigsaw diesel spend. The backtest reports **planned fuel GBP** (not total planned cost) against **actual fuel GBP** from Jigsaw. Driver mileage allowance is shown separately as an informational line and not used in the delta calculation.

**SLA / on-time rate:** Qargo contains `destination_end_timestamp_local` (actual delivery completion) and `destination_requested_start_timestamp_local` (the deadline). On-time = actual ≤ deadline, both compared at **date granularity** (the actual delivery column is a date, not a datetime — so hour-level SLA is not computable, but day-level is). This is the same field used in the profitability report to identify late deliveries.
> [NOTE] `QARGO_CORE_COLS` in `data_audit.py` is a minimal audit subset and does not include `destination_end_timestamp_local`. Load with `pd.read_excel` / `_read_glob_excel` directly and select the full column set, or extend `QARGO_CORE_COLS` when loading for the backtest.

**Vehicle identity mismatch:** The dispatcher assigns orders to specific registered vehicles. In reality, a different vehicle of the same type may have done the job. Vehicle-level comparison is therefore not attempted. All comparisons are at fleet-aggregate or vehicle-type level.

### 3. `simulation/report.py` (extend)

Add `print_backtest(date_str, planned, actual, flags)` and `print_backtest_summary(all_results)` functions following the same print style as `print_day` / `print_summary`.

Output format:
```
============================================================
  BACKTEST  2026-01-02   (ALNS, 120s budget, OSRM routing)
============================================================
                              PLANNED        ACTUAL
  ----------------------------------------------------------
  Vehicles used  (artic)           4             3
  Vehicles used  (rigid)           2             4
  Vehicles used  (total)           6             7
  Total distance KM          6,402.4         5,891.2  [1]
  Fuel cost GBP              2,681.30        2,540.40  [2]
  Driver allowance GBP       2,862.10            n/a  [3]
  Orders in window               163             163
  Orders assigned                154             142  [4]
  Assignment rate              94.5%           87.1%
  On-time deliveries              154              98  [5]
  On-time rate                100.0%           63.6%
  ----------------------------------------------------------
  Fuel delta (planned vs actual):    +5.5%
  On-time delta (planned vs actual): +36.4pp
  ----------------------------------------------------------
  [1] Planned = closed-loop road distance (OSRM). Actual = GPS-track
      Haversine sum between pings; undercounts curves slightly.
  [2] Planned fuel = sum(fuel_gbp_per_mile[type] x miles) from
      vehicle_cost_rates.json. Actual = Jigsaw diesel spend (excl.
      Adblue, zero-price rows). Directly comparable.
  [3] Driver mileage allowance shown for reference only; not in Jigsaw.
  [4] Actual = orders with any non-null resource field in Qargo.
  [5] On-time = destination_end_timestamp_local <= deadline (date-level
      only; hour-level SLA not available). Denominator = orders assigned.
      Planned on-time rate is 100% by construction — the dispatcher only
      places orders it deems feasible within deadline.
============================================================
```

### 4. `tests/test_actuals_loader.py` (new)

Unit tests for `load_actuals` using small synthetic DataFrames:
- GPS-track KM calculation (two pings with known Haversine distance)
- Jigsaw fuel sum (diesel-only, excludes Adblue and zero-price rows)
- Order assignment rate (mix of null and non-null resource fields)
- On-time rate: actual delivery date ≤ deadline → on-time; actual > deadline → late
- On-time rate denominator is assigned orders only (unassigned orders excluded)
- Empty DataFrame handling (all metrics return 0 / 0.0, no crash)

---

## Data Flow

```
run_backtest.py --date 2026-01-02
  │
  ├── load_datasets(base_dir)
  │     → qargo_df, telem_df, vehicles_df, jigsaw_df
  │
  ├── [for each date in range]
  │     │
  │     ├── build_orders(qargo_df, date, window_hours)   [existing]
  │     ├── build_vehicles(vehicles_df)                  [existing]
  │     ├── run_batch(orders, vehicles, ...)              [existing]
  │     │     → planned: vehicles_used, km, cost_gbp, assignment_rate
  │     │
  │     ├── load_actuals(date, telem_df, jigsaw_df, qargo_df, orders)
  │     │     → actual: vehicles, km, fuel_gbp, assignment_rate
  │     │
  │     ├── print_backtest(date, planned, actual, flags)
  │     └── write JSON → data/Output/backtest_YYYY-MM-DD.json
  │
  └── [if --days > 1]  print_backtest_summary(all_results)
```

---

## JSON Output Schema

`data/Output/backtest_YYYY-MM-DD.json`:
```json
{
  "date": "2026-01-02",
  "flags": {"algorithm": "alns", "budget_seconds": 120, "routing": "osrm"},
  "planned": {
    "vehicles_total": 6,
    "vehicles_artic": 4,
    "vehicles_rigid": 2,
    "distance_km": 6402.4,
    "fuel_gbp": 2681.30,
    "driver_allowance_gbp": 2862.10,
    "orders_in_window": 163,
    "orders_assigned": 154,
    "assignment_rate": 0.945
  },
  "actual": {
    "vehicles_total": 7,
    "vehicles_artic": 3,
    "vehicles_rigid": 4,
    "gps_track_km": 5891.2,
    "fuel_spend_gbp": 2540.40,
    "orders_assigned": 142,
    "assignment_rate": 0.871,
    "orders_on_time": 98,
    "on_time_rate": 0.636
  },
  "fuel_delta_pct": 5.5,
  "on_time_delta_pp": 36.4,
  "notes": {
    "km_method": "Actual = GPS-track Haversine sum between sorted pings per vehicle; undercounts curves slightly. Planned = closed-loop OSRM road distance.",
    "fuel_comparability": "Planned fuel = fuel_gbp_per_mile[type] x miles from vehicle_cost_rates.json. Actual = Jigsaw diesel-only spend. Directly comparable.",
    "driver_allowance": "Shown for reference; not present in Jigsaw data and excluded from fuel_delta_pct.",
    "sla": "on_time = destination_end_timestamp_local <= destination_requested_start_timestamp_local at date granularity. Planned on-time rate = 100% by construction (dispatcher only places feasible orders)."
  }
}
```

---

## Error Handling

- If telematics data has no pings for the target date: `actual_km = 0`, warn to console.
- If Jigsaw has no transactions for the target date: `actual_fuel_gbp = 0`, warn to console.
- If dispatcher produces no routes (all orders unassigned): print backtest with planned zeros, still load and show actuals.
- No crash on empty DataFrames — `load_actuals` returns zeros with a warning.

---

## What This Does NOT Cover

- Per-vehicle plan vs actuality (vehicle identity mismatch — different vehicle, same type)
- Hour-level SLA (only date-level on-time computable from `destination_end_timestamp_local`)
- Explainability annotations per order/route (separate spec)
- Phase 3 rolling multi-day commit (separate spec)
