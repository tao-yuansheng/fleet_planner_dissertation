# MCTS Logistics Data Audit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `data_audit.py` — a single script that profiles all four MCTS-relevant datasets, reports core column quality and vehicle ID match rates, and writes `data/Output/data_audit_report.json`.

**Architecture:** Four pure functions (one per dataset section) each accept DataFrames and return a dict. A loader assembles the DataFrames from disk; `main()` orchestrates and writes JSON. Functions are independently testable with synthetic DataFrames — no file I/O in tests.

**Tech Stack:** Python 3.12, pandas, json, glob, pathlib (no new dependencies beyond existing requirements.txt)

**Python executable:** `"C:\Users\Yuansheng Tao\AppData\Local\Programs\Python\Python312\python.exe"`

**Working directory for all commands:** `E:\BEAT\ZECURE-Phase2-main\BackEnd\logistics`

---

### Task 1: Test scaffolding

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_data_audit.py`

- [ ] **Step 1: Create the tests directory and init file**

```
mkdir tests
```

Then create `tests/__init__.py` as an empty file.

- [ ] **Step 2: Create the test file with imports only**

Create `tests/test_data_audit.py`:

```python
import pandas as pd
import pytest
```

- [ ] **Step 3: Verify the test file is found**

```
"C:\Users\Yuansheng Tao\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/ --collect-only
```

Expected: `0 tests collected` with no errors.

---

### Task 2: Vehicle ID cross-join function

**Files:**
- Create: `data_audit.py`
- Modify: `tests/test_data_audit.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_data_audit.py`:

```python
def test_vehicle_crossjoin_match_rate():
    qargo = pd.DataFrame({
        'resource_tractor':      ['AB12CDE', 'FG34HIJ', None],
        'resource_rigid':        [None,       None,      'KL56MNO'],
        'resource_van':          [None,       None,      None],
        'resource_trailer':      [None,       None,      None],
        'resource_drawbartrailer': [None,     None,      None],
    })
    supatrak = pd.DataFrame({'AssetName': ['AB12CDE', 'KL56MNO', 'XX99ZZZ']})
    jigsaw   = pd.DataFrame({'vehicleRegistration': ['AB12CDE', 'FG34HIJ']})

    from data_audit import audit_vehicle_crossjoin
    result = audit_vehicle_crossjoin(qargo, supatrak, jigsaw)

    # 3 unique regs extracted from qargo: AB12CDE, FG34HIJ, KL56MNO
    assert result['qargo_unique_regs'] == 3
    assert result['supatrak_unique_regs'] == 3
    assert result['jigsaw_unique_regs'] == 2
    # AB12CDE + KL56MNO match supatrak → 2/3
    assert result['qargo_to_supatrak_match_rate'] == pytest.approx(2 / 3)
    # AB12CDE + FG34HIJ match jigsaw → 2/3
    assert result['qargo_to_jigsaw_match_rate'] == pytest.approx(2 / 3)
    # AB12CDE matches jigsaw → 1/3
    assert result['supatrak_to_jigsaw_match_rate'] == pytest.approx(1 / 3)
```

- [ ] **Step 2: Run test to verify it fails**

```
"C:\Users\Yuansheng Tao\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_data_audit.py::test_vehicle_crossjoin_match_rate -v
```

Expected: `FAILED` — `ModuleNotFoundError: No module named 'data_audit'`

- [ ] **Step 3: Create `data_audit.py` with the cross-join function**

Create `data_audit.py`:

```python
import pandas as pd
import json
import glob
from pathlib import Path
from datetime import datetime

RESOURCE_VEHICLE_COLS = [
    'resource_tractor', 'resource_rigid', 'resource_van',
    'resource_drawbartrailer', 'resource_trailer',
]

QARGO_CORE_COLS = [
    'order_id', 'goods_weight',
    'origin_postal_code', 'origin_city',
    'destination_postal_code', 'destination_city',
    'origin_requested_start_timestamp_local',
    'origin_time_window_value',
    'vehicle_category_name',
    'total_revenue_tenant_currency',
    'metrics_distance_total',
]


def _extract_qargo_regs(qargo_df: pd.DataFrame) -> set:
    """Collect all non-null registration values from Qargo resource columns."""
    regs = set()
    for col in RESOURCE_VEHICLE_COLS:
        if col in qargo_df.columns:
            vals = qargo_df[col].dropna().astype(str).str.strip()
            vals = vals[vals != '']
            regs.update(vals.str.upper())
    return regs


def audit_vehicle_crossjoin(
    qargo_df: pd.DataFrame,
    supatrak_vehicles_df: pd.DataFrame,
    jigsaw_df: pd.DataFrame,
) -> dict:
    qargo_regs    = _extract_qargo_regs(qargo_df)
    supatrak_regs = set(supatrak_vehicles_df['AssetName'].dropna().astype(str).str.strip().str.upper())
    jigsaw_regs   = set(jigsaw_df['vehicleRegistration'].dropna().astype(str).str.strip().str.upper())

    def match_rate(a: set, b: set) -> float:
        return round(len(a & b) / len(a), 4) if a else 0.0

    return {
        'qargo_unique_regs':              len(qargo_regs),
        'supatrak_unique_regs':           len(supatrak_regs),
        'jigsaw_unique_regs':             len(jigsaw_regs),
        'qargo_to_supatrak_match_rate':   match_rate(qargo_regs, supatrak_regs),
        'qargo_to_jigsaw_match_rate':     match_rate(qargo_regs, jigsaw_regs),
        'supatrak_to_jigsaw_match_rate':  match_rate(supatrak_regs, jigsaw_regs),
        'in_qargo_only_sample':   sorted(qargo_regs - supatrak_regs - jigsaw_regs)[:10],
        'in_supatrak_only_sample': sorted(supatrak_regs - qargo_regs)[:10],
        'in_jigsaw_only_sample':  sorted(jigsaw_regs - qargo_regs)[:10],
    }
```

- [ ] **Step 4: Run test to verify it passes**

```
"C:\Users\Yuansheng Tao\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_data_audit.py::test_vehicle_crossjoin_match_rate -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```
git add data_audit.py tests/
git commit -m "feat: add vehicle ID cross-join audit function"
```

---

### Task 3: Qargo core column audit

**Files:**
- Modify: `data_audit.py`
- Modify: `tests/test_data_audit.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_data_audit.py`:

```python
def test_audit_qargo_null_rates():
    qargo = pd.DataFrame({
        'order_id':   ['A', 'B', 'C'],
        'goods_weight': [100.0, None, 200.0],
        'origin_postal_code': ['SW1A1AA', None, 'EC1A1BB'],
        'origin_city': ['London', 'Manchester', None],
        'destination_postal_code': ['M11AE', 'B11AA', 'E11BB'],
        'destination_city': ['Manchester', 'Birmingham', 'London'],
        'origin_requested_start_timestamp_local': ['2026-01-05 09:00', None, '2026-01-06 10:00'],
        'origin_time_window_value': [24, 24, None],
        'vehicle_category_name': ['Tractor Unit', 'Tractor Unit', None],
        'total_revenue_tenant_currency': [149.0, None, 0.0],
        'metrics_distance_total': [243.0, 180.0, None],
    })

    from data_audit import audit_qargo
    result = audit_qargo(qargo)

    assert result['total_orders'] == 3
    assert result['columns']['goods_weight']['null_rate'] == pytest.approx(1 / 3)
    # revenue_coverage: only 149.0 qualifies (non-null AND > 0) → 1/3
    assert result['revenue_coverage_rate'] == pytest.approx(1 / 3)


def test_audit_qargo_vehicle_category_and_window():
    qargo = pd.DataFrame({
        'order_id': ['A', 'B', 'C', 'D'],
        'goods_weight': [100.0, 200.0, 300.0, 400.0],
        'origin_postal_code': ['SW1', 'EC1', 'M1', 'B1'],
        'origin_city': ['London', 'London', 'Manchester', 'Birmingham'],
        'destination_postal_code': ['M1', 'B1', 'SW1', 'EC1'],
        'destination_city': ['Manchester', 'Birmingham', 'London', 'London'],
        'origin_requested_start_timestamp_local': ['2026-01-05 09:00'] * 4,
        'origin_time_window_value': [24, 48, 24, 24],
        'vehicle_category_name': ['Tractor Unit', 'Tractor Unit', 'Rigid', None],
        'total_revenue_tenant_currency': [149.0, 200.0, None, 100.0],
        'metrics_distance_total': [243.0, 180.0, 90.0, 310.0],
    })

    from data_audit import audit_qargo
    result = audit_qargo(qargo)

    assert result['vehicle_category_counts']['Tractor Unit'] == 2
    assert result['vehicle_category_counts']['Rigid'] == 1
    # window values: [24, 48, 24, 24] → mean = 30.0
    assert result['columns']['origin_time_window_value']['mean'] == pytest.approx(30.0)
```

- [ ] **Step 2: Run to verify they fail**

```
"C:\Users\Yuansheng Tao\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_data_audit.py::test_audit_qargo_null_rates tests/test_data_audit.py::test_audit_qargo_vehicle_category_and_window -v
```

Expected: `FAILED` — `ImportError`

- [ ] **Step 3: Implement `audit_qargo` — add to `data_audit.py` after `audit_vehicle_crossjoin`**

```python
def _col_stats(series: pd.Series) -> dict:
    total = len(series)
    null_count = int(series.isna().sum())
    stats: dict = {'null_rate': round(null_count / total, 4) if total else 0.0}
    numeric = pd.to_numeric(series, errors='coerce')
    if numeric.notna().any():
        stats['min']       = round(float(numeric.min()),    2)
        stats['mean']      = round(float(numeric.mean()),   2)
        stats['median']    = round(float(numeric.median()), 2)
        stats['max']       = round(float(numeric.max()),    2)
        stats['zero_rate'] = round(float((numeric == 0).sum() / total), 4)
    return stats


def audit_qargo(qargo_df: pd.DataFrame) -> dict:
    col_stats = {}
    for col in QARGO_CORE_COLS:
        if col in qargo_df.columns:
            col_stats[col] = _col_stats(qargo_df[col])

    rev = pd.to_numeric(
        qargo_df.get('total_revenue_tenant_currency', pd.Series(dtype=float)),
        errors='coerce',
    )
    revenue_coverage_count = int(((rev.notna()) & (rev > 0)).sum())

    vehicle_cats = (
        qargo_df['vehicle_category_name'].dropna().value_counts().to_dict()
        if 'vehicle_category_name' in qargo_df.columns else {}
    )

    return {
        'total_orders':            len(qargo_df),
        'columns':                 col_stats,
        'revenue_coverage_count':  revenue_coverage_count,
        'revenue_coverage_rate':   round(revenue_coverage_count / len(qargo_df), 4) if len(qargo_df) else 0.0,
        'vehicle_category_counts': vehicle_cats,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```
"C:\Users\Yuansheng Tao\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_data_audit.py::test_audit_qargo_null_rates tests/test_data_audit.py::test_audit_qargo_vehicle_category_and_window -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```
git add data_audit.py tests/test_data_audit.py
git commit -m "feat: add Qargo core column audit function"
```

---

### Task 4: Supatrak telematics audit

**Files:**
- Modify: `data_audit.py`
- Modify: `tests/test_data_audit.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_data_audit.py`:

```python
def test_audit_supatrak_telematics():
    telem = pd.DataFrame({
        'LocalTime': pd.to_datetime([
            '2026-01-05 08:00', '2026-01-05 09:00', '2026-01-05 10:00',
            '2026-01-05 08:30', '2026-01-05 09:30',
            '2026-01-05 08:00',
        ]),
        'AssetName': ['VEH001', 'VEH001', 'VEH001', 'VEH002', 'VEH002', 'VEH003'],
        'Latitude':  [51.5,     51.6,     None,      52.0,     52.1,     None],
        'Longitude': [-0.1,     -0.2,     None,      -1.0,     -1.1,     None],
        'Ignition':  ['ON',     'ON',     'ON',      'OFF',    'ON',     'ON'],
        'GPSSpeed':  [30.0,     35.0,     0.0,       0.0,      40.0,     None],
    })

    from data_audit import audit_supatrak_telematics
    result = audit_supatrak_telematics(telem)

    assert result['total_pings'] == 6
    assert result['unique_vehicles'] == 3
    assert result['lat_null_rate'] == pytest.approx(2 / 6)
    assert result['lon_null_rate'] == pytest.approx(2 / 6)
    assert result['date_range']['start'] == '2026-01-05'
    assert result['date_range']['end']   == '2026-01-05'
```

- [ ] **Step 2: Run to verify it fails**

```
"C:\Users\Yuansheng Tao\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_data_audit.py::test_audit_supatrak_telematics -v
```

Expected: `FAILED` — `ImportError`

- [ ] **Step 3: Implement `audit_supatrak_telematics` — add to `data_audit.py`**

```python
def audit_supatrak_telematics(telem_df: pd.DataFrame) -> dict:
    total = len(telem_df)

    lat_null = int(telem_df['Latitude'].isna().sum())  if 'Latitude'  in telem_df.columns else total
    lon_null = int(telem_df['Longitude'].isna().sum()) if 'Longitude' in telem_df.columns else total

    times = (
        pd.to_datetime(telem_df['LocalTime'], errors='coerce')
        if 'LocalTime' in telem_df.columns
        else pd.Series(dtype='datetime64[ns]')
    )
    date_range = {
        'start': str(times.min().date()) if times.notna().any() else None,
        'end':   str(times.max().date()) if times.notna().any() else None,
    }

    unique_vehicles = int(telem_df['AssetName'].nunique()) if 'AssetName' in telem_df.columns else 0

    pings_per_vehicle_day: pd.Series = pd.Series(dtype=float)
    if 'AssetName' in telem_df.columns and times.notna().any():
        tmp = telem_df[['AssetName']].copy()
        tmp['_date'] = times.dt.date
        pings_per_vehicle_day = tmp.groupby(['AssetName', '_date']).size()

    low_coverage: list = []
    if not pings_per_vehicle_day.empty:
        median_per_veh = pings_per_vehicle_day.groupby('AssetName').median()
        low_coverage = sorted(median_per_veh[median_per_veh < 10].index.tolist())

    return {
        'total_pings':                     total,
        'unique_vehicles':                 unique_vehicles,
        'lat_null_rate':                   round(lat_null / total, 4) if total else 0.0,
        'lon_null_rate':                   round(lon_null / total, 4) if total else 0.0,
        'date_range':                      date_range,
        'median_pings_per_vehicle_day':    (
            round(float(pings_per_vehicle_day.median()), 1)
            if not pings_per_vehicle_day.empty else None
        ),
        'low_coverage_vehicle_sample':     low_coverage[:10],
    }
```

- [ ] **Step 4: Run test to verify it passes**

```
"C:\Users\Yuansheng Tao\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_data_audit.py::test_audit_supatrak_telematics -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```
git add data_audit.py tests/test_data_audit.py
git commit -m "feat: add Supatrak telematics audit function"
```

---

### Task 5: Jigsaw fuel audit

**Files:**
- Modify: `data_audit.py`
- Modify: `tests/test_data_audit.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_data_audit.py`:

```python
def test_audit_jigsaw():
    jigsaw = pd.DataFrame({
        'vehicleRegistration':  ['AB12CDE', 'AB12CDE', 'FG34HIJ', None],
        'quantity':             [50.0,       60.0,      45.0,      30.0],
        'unitPrice':            [1.45,       1.46,      1.44,      None],
        'transactionDateTime':  ['2026-01-05', '2026-01-10', '2026-01-07', '2026-01-08'],
    })
    vehicle_list = pd.DataFrame({'AssetName': ['AB12CDE', 'FG34HIJ', 'KL56MNO']})

    from data_audit import audit_jigsaw
    result = audit_jigsaw(jigsaw, vehicle_list)

    assert result['total_transactions'] == 4
    assert result['unique_vehicles'] == 2          # excludes null row
    assert result['reg_null_rate'] == pytest.approx(1 / 4)
    assert result['vehicles_with_fuel_records'] == 2   # AB12CDE + FG34HIJ in vehicle_list
    assert result['vehicles_without_fuel_records'] == 1  # KL56MNO missing from jigsaw
    assert result['cost_per_litre']['mean'] == pytest.approx((1.45 + 1.46 + 1.44) / 3, abs=0.01)
```

- [ ] **Step 2: Run to verify it fails**

```
"C:\Users\Yuansheng Tao\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_data_audit.py::test_audit_jigsaw -v
```

Expected: `FAILED` — `ImportError`

- [ ] **Step 3: Implement `audit_jigsaw` — add to `data_audit.py`**

```python
def audit_jigsaw(jigsaw_df: pd.DataFrame, supatrak_vehicles_df: pd.DataFrame) -> dict:
    total   = len(jigsaw_df)
    reg_col = 'vehicleRegistration'

    reg_null    = int(jigsaw_df[reg_col].isna().sum()) if reg_col in jigsaw_df.columns else total
    unique_vehs = int(jigsaw_df[reg_col].dropna().nunique()) if reg_col in jigsaw_df.columns else 0

    veh_list_regs = set(supatrak_vehicles_df['AssetName'].dropna().astype(str).str.strip().str.upper())
    jigsaw_regs   = (
        set(jigsaw_df[reg_col].dropna().astype(str).str.strip().str.upper())
        if reg_col in jigsaw_df.columns else set()
    )

    with_fuel    = len(veh_list_regs & jigsaw_regs)
    without_fuel = len(veh_list_regs - jigsaw_regs)

    price = pd.to_numeric(
        jigsaw_df.get('unitPrice', pd.Series(dtype=float)), errors='coerce'
    ).dropna()
    cost_per_litre = {
        'mean': round(float(price.mean()), 4) if len(price) else None,
        'min':  round(float(price.min()),  4) if len(price) else None,
        'max':  round(float(price.max()),  4) if len(price) else None,
    }

    times = pd.to_datetime(
        jigsaw_df.get('transactionDateTime', pd.Series(dtype=str)), errors='coerce'
    )
    date_range = {
        'start': str(times.min().date()) if times.notna().any() else None,
        'end':   str(times.max().date()) if times.notna().any() else None,
    }

    return {
        'total_transactions':          total,
        'unique_vehicles':             unique_vehs,
        'reg_null_rate':               round(reg_null / total, 4) if total else 0.0,
        'vehicles_with_fuel_records':  with_fuel,
        'vehicles_without_fuel_records': without_fuel,
        'cost_per_litre':              cost_per_litre,
        'date_range':                  date_range,
    }
```

- [ ] **Step 4: Run test to verify it passes**

```
"C:\Users\Yuansheng Tao\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_data_audit.py::test_audit_jigsaw -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```
git add data_audit.py tests/test_data_audit.py
git commit -m "feat: add Jigsaw fuel audit function"
```

---

### Task 6: Dataset loader and `main()` orchestrator

**Files:**
- Modify: `data_audit.py`
- Modify: `tests/test_data_audit.py`

- [ ] **Step 1: Write failing test for loader**

Append to `tests/test_data_audit.py`:

```python
def test_load_datasets_missing_dir(tmp_path):
    """Loader returns empty DataFrames and does not crash when files are absent."""
    from data_audit import load_datasets
    result = load_datasets(base_dir=str(tmp_path))
    assert isinstance(result['qargo'],               pd.DataFrame)
    assert isinstance(result['supatrak_telematics'], pd.DataFrame)
    assert isinstance(result['supatrak_vehicles'],   pd.DataFrame)
    assert isinstance(result['jigsaw'],              pd.DataFrame)
```

- [ ] **Step 2: Run to verify it fails**

```
"C:\Users\Yuansheng Tao\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_data_audit.py::test_load_datasets_missing_dir -v
```

Expected: `FAILED` — `ImportError`

- [ ] **Step 3: Implement loader and `main()` — append to `data_audit.py`**

```python
def _read_glob_csv(pattern: str) -> pd.DataFrame:
    files = sorted(glob.glob(pattern))
    if not files:
        return pd.DataFrame()
    parts = []
    for f in files:
        try:
            parts.append(pd.read_csv(f, low_memory=False))
        except Exception as e:
            print(f"  WARNING: could not read {f}: {e}")
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _read_glob_excel(pattern: str) -> pd.DataFrame:
    files = sorted(glob.glob(pattern))
    if not files:
        return pd.DataFrame()
    parts = []
    for f in files:
        try:
            parts.append(pd.read_excel(f))
        except Exception as e:
            print(f"  WARNING: could not read {f}: {e}")
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def load_datasets(base_dir: str = '.') -> dict:
    base = Path(base_dir)
    veh_path = base / 'data/Input/supatrak/supatrak_vehicle_list_enriched.csv'
    return {
        'qargo':               _read_glob_excel(str(base / 'data/Input/orders/qargo_*.xlsx')),
        'supatrak_telematics': _read_glob_csv(str(base / 'data/Input/supatrak/supatrak_telematics_cleaned_*.csv')),
        'supatrak_vehicles':   pd.read_csv(str(veh_path)) if veh_path.exists() else pd.DataFrame(),
        'jigsaw':              _read_glob_csv(str(base / 'data/Input/profitability/jigsaw_*.csv')),
    }


def _print_section(title: str, data: dict) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")
    for k, v in data.items():
        if isinstance(v, dict):
            print(f"  {k}:")
            for kk, vv in v.items():
                print(f"    {kk}: {vv}")
        elif isinstance(v, list):
            print(f"  {k}: {v}")
        else:
            print(f"  {k}: {v}")


def main(base_dir: str = '.') -> dict:
    print(f"\nZEEFLEET MCTS Logistics — Data Audit")
    print(f"Run at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("Loading datasets...")

    ds = load_datasets(base_dir)
    qargo, telem, vehicles, jigsaw = ds['qargo'], ds['supatrak_telematics'], ds['supatrak_vehicles'], ds['jigsaw']

    print(f"  Qargo orders:         {len(qargo):,} rows")
    print(f"  Supatrak telematics:  {len(telem):,} rows")
    print(f"  Supatrak vehicles:    {len(vehicles):,} rows")
    print(f"  Jigsaw fuel:          {len(jigsaw):,} rows")

    report: dict = {'generated_at': datetime.now().isoformat()}

    if not qargo.empty and not vehicles.empty and not jigsaw.empty:
        s = audit_vehicle_crossjoin(qargo, vehicles, jigsaw)
        _print_section('1. VEHICLE ID CROSS-JOIN', s)
        report['vehicle_crossjoin'] = s
    else:
        print("\n  SKIP vehicle cross-join — one or more datasets empty")

    if not qargo.empty:
        s = audit_qargo(qargo)
        _print_section('2. QARGO CORE COLUMNS', s)
        report['qargo'] = s
    else:
        print("\n  SKIP Qargo audit — dataset empty")

    if not telem.empty:
        s = audit_supatrak_telematics(telem)
        _print_section('3. SUPATRAK TELEMATICS', s)
        report['supatrak_telematics'] = s
    else:
        print("\n  SKIP Supatrak telematics audit — dataset empty")

    if not jigsaw.empty and not vehicles.empty:
        s = audit_jigsaw(jigsaw, vehicles)
        _print_section('4. JIGSAW FUEL', s)
        report['jigsaw'] = s
    else:
        print("\n  SKIP Jigsaw audit — dataset empty")

    out_dir = Path(base_dir) / 'data/Output'
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / 'data_audit_report.json'
    with open(out_path, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nReport written to: {out_path}")

    return report


if __name__ == '__main__':
    main()
```

- [ ] **Step 4: Run loader test to verify it passes**

```
"C:\Users\Yuansheng Tao\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_data_audit.py::test_load_datasets_missing_dir -v
```

Expected: `PASSED`

- [ ] **Step 5: Run full test suite — all tests must pass**

```
"C:\Users\Yuansheng Tao\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_data_audit.py -v
```

Expected: 6 tests, all `PASSED`

- [ ] **Step 6: Commit**

```
git add data_audit.py tests/test_data_audit.py
git commit -m "feat: add dataset loader and main() orchestrator"
```

---

### Task 7: Run audit against real data

**Files:**
- Produces: `data/Output/data_audit_report.json`

- [ ] **Step 1: Run the script from the `logistics/` directory**

```
"C:\Users\Yuansheng Tao\AppData\Local\Programs\Python\Python312\python.exe" data_audit.py
```

Expected: four sections printed to console, no crash.

- [ ] **Step 2: Verify the JSON report exists and has all four sections**

```
"C:\Users\Yuansheng Tao\AppData\Local\Programs\Python\Python312\python.exe" -c "import json; r=json.load(open('data/Output/data_audit_report.json')); print(list(r.keys()))"
```

Expected output:

```
['generated_at', 'vehicle_crossjoin', 'qargo', 'supatrak_telematics', 'jigsaw']
```

- [ ] **Step 3: Commit report**

```
git add data/Output/data_audit_report.json
git commit -m "chore: run initial MCTS data audit — report generated"
```
