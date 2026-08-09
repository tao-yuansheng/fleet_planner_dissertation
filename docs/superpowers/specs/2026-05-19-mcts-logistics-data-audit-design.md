---
name: mcts-logistics-data-audit
description: Design spec for Phase 1 data audit script — profiles all four MCTS-relevant datasets in the logistics folder and validates core column quality before building MCTS infrastructure
metadata:
  type: project
---

# MCTS Logistics — Data Audit Design

**Date:** 2026-05-19
**Context:** Phase 1 of the ZEEFLEET MCTS Rolling Freight Dispatch System (see `freight/mcts_logistics_plan_v2.docx`). Before building any MCTS infrastructure, we audit the four datasets to confirm core columns are usable and establish vehicle ID match rates across systems.

## What We Are Building

A single flat script: `logistics/data_audit.py`

Output: console report + `data/Output/data_audit_report.json`

## Datasets

| Dataset | Path | Format |
|---|---|---|
| Qargo orders | `data/Input/orders/qargo_*.xlsx` | Excel (106 columns) |
| Supatrak telematics (cleaned) | `data/Input/supatrak/supatrak_telematics_cleaned_*.csv` | CSV |
| Supatrak vehicle list | `data/Input/supatrak/supatrak_vehicle_list_enriched.csv` | CSV |
| Jigsaw fuel | `data/Input/profitability/jigsaw_*.csv` | CSV |

Script auto-discovers all matching files and concatenates Jan + Feb for each dataset.

## Four Audit Sections

### 1. Vehicle ID Cross-Join
Join Qargo `resource_*` columns (resource_tractor, resource_rigid, resource_van, resource_trailer, resource_drawbartrailer) → Supatrak `AssetName` → Jigsaw `vehicleRegistration`, all on registration number.

Report:
- Unique registrations in each system
- Qargo→Supatrak match rate
- Qargo→Jigsaw match rate
- Supatrak→Jigsaw match rate
- Any registrations present in one system but not others (sample up to 10)

### 2. Qargo Core Column Audit
Core columns needed for MCTS: `order_id`, `goods_weight`, `origin_postal_code`, `origin_city`, `destination_postal_code`, `destination_city`, `origin_requested_start_timestamp_local`, `origin_time_window_value`, `vehicle_category_name`, `total_revenue_tenant_currency`, `metrics_distance_total`.

Report per column:
- Null/blank rate
- For numeric columns: min, mean, median, max, zero-rate
- `vehicle_category_name`: value counts
- `origin_time_window_value`: distribution of window widths
- Revenue: coverage rate (non-null, non-zero)

### 3. Supatrak Telematics Audit
Core columns: `AssetName`, `LocalTime`, `Latitude`, `Longitude`, `Ignition`, `GPSSpeed`.

Report:
- Null rates for Lat/Lon
- Date range covered
- Unique vehicles with GPS pings
- Median pings per vehicle per day
- Vehicles with fewer than 10 pings/day (flag as low coverage)

### 4. Jigsaw Fuel Audit
Core columns: `vehicleRegistration`, `quantity`, `unitPrice`, `transactionDateTime`.

Report:
- Null rates
- Unique vehicle registrations
- Cross-check against Supatrak vehicle list: how many vehicles have fuel records
- Implied fuel cost per litre: mean, range
- Date range covered

## Constraints

- Python standard library + pandas only (no new dependencies)
- Run from `logistics/` directory
- Graceful handling of missing files — skip and warn, do not crash
- Output JSON mirrors the console sections for downstream use
