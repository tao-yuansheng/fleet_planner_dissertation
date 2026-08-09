from __future__ import annotations

from pathlib import Path

LOGISTICS_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QARGO = LOGISTICS_ROOT / "data" / "Input" / "orders" / "qargo_20260101_to_20260131.parquet"
DEFAULT_FEB_QARGO = LOGISTICS_ROOT / "data" / "Input" / "orders" / "qargo_20260201_to_20260228.parquet"
DEFAULT_POSTCODE_CACHE = LOGISTICS_ROOT / "data" / "Output" / "postcode_cache.json"
DEFAULT_VERIFIED_LEGS = LOGISTICS_ROOT / "freight_planner" / "data" / "verified_legs.csv"
DEFAULT_OUT_DIR = LOGISTICS_ROOT / "freight_planner" / "runs"
DEFAULT_ENRICHED = LOGISTICS_ROOT / "freight_planner" / "data" / "enriched_orders_2026-01_2026-02.parquet"
# DEFAULT_ENRICHED (the COMBINED Jan+Feb file) is the runner default since
# 2026-07-22: the monthly files are BOOKING-month universes, so a window in a
# month's first week silently misses deliveries booked late the prior month
# (Feb 2: 521 dues live in the Jan file — the "no deliveries on the 2nd" hole).
# The combined file is a plain concat of the two monthly files and MUST be
# rebuilt whenever a monthly enriched file is regenerated (it goes stale).
# Note: vehicle-catchment radii calibrate on the WHOLE input frame, so runs on
# the combined file are not calibration-identical to runs on a monthly file —
# reproducing those requires temporarily pointing DEFAULT_ENRICHED at the
# monthly file (the --qargo CLI override was removed 2026-07-22, user rule).
# Monthly enriched files mirror the raw monthly universes 1:1 (same rows + the
# verified_* columns).
ENRICHED_2026_01 = LOGISTICS_ROOT / "freight_planner" / "data" / "enriched_orders_2026-01.parquet"
ENRICHED_2026_02 = LOGISTICS_ROOT / "freight_planner" / "data" / "enriched_orders_2026-02.parquet"