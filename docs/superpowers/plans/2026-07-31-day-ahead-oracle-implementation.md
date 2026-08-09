# W0 Day-Ahead Oracle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible W0 comparator in which every collection booking is visible at midnight on its original booking date and planning runs once per day with no re-optimisation or micro-insertions.

**Architecture:** A new preprocessing module creates an immutable oracle parquet and JSON provenance summary from the combined January-February input. A separate runner wrapper points the existing rolling dispatcher at that oracle file and enforces one `00:00` anchor plus zero micro passes without changing `run_rolling.py`, `visibility.py`, or the ordinary dataset path.

**Tech Stack:** Python 3.12, pandas/pyarrow parquet, hashlib/JSON provenance, argparse, pytest.

## Global Constraints

- The source `freight_planner/data/enriched_orders_2026-01_2026-02.parquet` is read-only and must never be overwritten.
- The output is `freight_planner/data/enriched_orders_2026-01_2026-02_DAY_AHEAD_ORACLE.parquet`.
- Preserve the original creation value in `timestamp_created_original`; floor only `timestamp_created` to 00:00 on its original date.
- Future booking dates remain invisible before their own midnight.
- Deliveries keep the existing 18:00 evening-before visibility rule.
- Planning occurs once per calendar day at `00:00`; `--micro-every-min 0` disables insertions and no noon anchor is present.
- The existing 18:00 close event remains for trunk sizing and accounting.
- Do not modify `freight_planner/run_rolling.py`, `freight_planner/visibility.py`, or `freight_planner/paths.py` while the beta continuation queue is active.
- The wrapper uses only the W0 OSRM/postcode caches and must never use the live C0 cache.

---

### Task 1: Deterministic oracle transformation and provenance

**Files:**
- Create: `freight_planner/day_ahead_oracle.py`
- Create: `tests/freight_planner/test_day_ahead_oracle.py`

**Interfaces:**
- Consumes: `pandas.DataFrame` containing `order_id` and `timestamp_created`.
- Produces: `floor_creation_to_booking_midnight(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict]`.
- Produces: `build_oracle_file(source: Path, output: Path, summary_path: Path, start: date, end: date) -> dict`.
- Produces CLI: `python -B -m freight_planner.day_ahead_oracle`.

- [ ] **Step 1: Write failing tests for flooring and preservation**

Add tests that express the public contract before the module exists:

```python
def test_floor_creation_to_booking_midnight_preserves_original_and_unrelated_fields():
    source = pd.DataFrame({
        "order_id": ["morning", "late", "missing"],
        "timestamp_created": [
            "2026-02-16T08:15:12Z",
            "2026-02-17T23:59:59Z",
            None,
        ],
        "goods_pallet_spaces": [1.0, 4.0, 2.0],
    })

    transformed, stats = floor_creation_to_booking_midnight(source)

    assert transformed["timestamp_created_original"].tolist()[:2] == source["timestamp_created"].tolist()[:2]
    assert pd.to_datetime(transformed["timestamp_created"], utc=True).tolist()[:2] == [
        pd.Timestamp("2026-02-16T00:00:00Z"),
        pd.Timestamp("2026-02-17T00:00:00Z"),
    ]
    assert pd.isna(transformed.loc[2, "timestamp_created"])
    assert transformed["goods_pallet_spaces"].tolist() == [1.0, 4.0, 2.0]
    assert stats == {"rows": 3, "non_null_created": 2, "changed": 2, "already_midnight": 0}
```

Add a second test asserting that an input already containing
`timestamp_created_original` raises `ValueError`, preventing accidental
double-transformation.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
& 'E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe' -m pytest -q tests/freight_planner/test_day_ahead_oracle.py
```

Expected: collection fails because `freight_planner.day_ahead_oracle` does not exist.

- [ ] **Step 3: Implement the pure transformation**

Create `floor_creation_to_booking_midnight` with these exact rules:

```python
def floor_creation_to_booking_midnight(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    if "timestamp_created" not in frame.columns:
        raise ValueError("source is missing timestamp_created")
    if "timestamp_created_original" in frame.columns:
        raise ValueError("source already contains timestamp_created_original")
    out = frame.copy()
    out["timestamp_created_original"] = out["timestamp_created"]
    parsed = pd.to_datetime(out["timestamp_created"], errors="coerce", utc=True)
    floored = parsed.dt.floor("D")
    non_null = parsed.notna()
    changed = non_null & parsed.ne(floored)
    out["timestamp_created"] = floored
    stats = {
        "rows": int(len(out)),
        "non_null_created": int(non_null.sum()),
        "changed": int(changed.sum()),
        "already_midnight": int((non_null & ~changed).sum()),
    }
    return out, stats
```

- [ ] **Step 4: Re-run the focused tests and verify GREEN**

Run the Task 1 command and require zero failures.

- [ ] **Step 5: Write failing file/provenance tests**

Use a temporary parquet and assert that `build_oracle_file`:

- refuses `source == output`;
- writes the transformed parquet and summary JSON;
- preserves row count and every unrelated column;
- records `source_sha256`, source/output paths, `start`, `end`, and transformation counts;
- writes via temporary sibling files followed by `Path.replace`, so interrupted preparation cannot leave a valid-looking partial output.

- [ ] **Step 6: Run the file tests and verify RED**

Run the same focused pytest command. Expected: FAIL because
`build_oracle_file` is missing.

- [ ] **Step 7: Implement atomic parquet/JSON generation and CLI**

Use a streaming SHA-256 helper:

```python
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
```

`build_oracle_file` reads the source, applies the pure transform, writes
`<output>.tmp` and `<summary>.tmp`, verifies the output row count, then replaces
the final paths. The CLI defaults to the Global Constraints paths and W0 dates
`2026-02-16` through `2026-02-22`; add `--force` only to replace a previously
generated oracle whose recorded source fingerprint matches the current source.

- [ ] **Step 8: Verify Task 1 GREEN and commit**

Run the focused tests, then:

```powershell
git add freight_planner/day_ahead_oracle.py tests/freight_planner/test_day_ahead_oracle.py
git commit -m "feat: build reproducible day-ahead oracle data"
```

---

### Task 2: Prove the daily visibility contract

**Files:**
- Modify: `freight_planner/day_ahead_oracle.py`
- Modify: `tests/freight_planner/test_day_ahead_oracle.py`

**Interfaces:**
- Consumes: transformed Qargo frame and demand frame accepted by `build_order_meta`.
- Produces: `daily_visibility_census(qargo_df: pd.DataFrame, demand_df: pd.DataFrame, start: date, end: date) -> list[dict]`.

- [ ] **Step 1: Write failing visibility tests**

Construct collection rows booked at 08:15 and 23:59 on 16 February, one
collection booked on 17 February, and one delivery due on 17 February. After
transformation, assert:

```python
meta = build_order_meta(transformed, demand)
at_16_midnight = visible_order_ids(meta, datetime(2026, 2, 16, 0, 0))
assert {"collect-am", "collect-pm"} <= at_16_midnight
assert "collect-tomorrow" not in at_16_midnight
assert "delivery-17" in at_16_midnight
```

Also assert the two 16 February collections are absent at 00:00 when using the
untouched source timestamps.

- [ ] **Step 2: Run and verify RED for the census**

Run the focused Task 1 pytest command. Expected: tests for existing visibility
behavior pass, while the census test fails because `daily_visibility_census`
does not exist.

- [ ] **Step 3: Implement the census**

For each date from `start` through `end`, build metadata with
`build_order_meta`, calculate visibility at midnight, and return rows containing:

```python
{
    "date": "2026-02-16",
    "collections_booked_that_date": 42,
    "collections_visible_at_midnight": 42,
    "missing_collection_ids": [],
    "future_collection_ids_visible_early": [],
}
```

Collection flows are exactly `visibility.COLLECT_FLOWS`. Raise `ValueError` if
any daily row has a missing or early-visible collection; this turns the census
into a generation gate rather than a descriptive report.

- [ ] **Step 4: Add the census to provenance output**

In the real-data CLI, build demand records with:

```python
records = build_demand_records(transformed, start, end)
demand_df = pd.DataFrame([record.to_dict() for record in records])
```

Store the successful census under `daily_visibility_census` in the JSON
summary before atomically publishing it.

- [ ] **Step 5: Run focused tests and commit**

Require zero failures, then:

```powershell
git add freight_planner/day_ahead_oracle.py tests/freight_planner/test_day_ahead_oracle.py
git commit -m "test: gate oracle data on midnight visibility"
```

---

### Task 3: Dedicated single-epoch wrapper

**Files:**
- Create: `freight_planner/run_static_oracle.py`
- Create: `tests/freight_planner/test_run_static_oracle.py`

**Interfaces:**
- Produces: `build_run_argv(args: argparse.Namespace) -> list[str]`.
- Produces CLI: `python -B -m freight_planner.run_static_oracle`.
- Delegates to: `freight_planner.run_rolling.main(argv)` after setting only the
  wrapper module's imported `run_rolling.DEFAULT_ENRICHED` binding.

- [ ] **Step 1: Write failing wrapper-argument tests**

Assert that default wrapper arguments build an underlying command containing:

```python
assert argv[argv.index("--start") + 1] == "2026-02-16"
assert argv[argv.index("--end") + 1] == "2026-02-22"
assert argv[argv.index("--epochs") + 1] == "00:00"
assert argv[argv.index("--micro-every-min") + 1] == "0"
assert argv[argv.index("--converge-pct") + 1] == "0.15"
assert argv[argv.index("--converge-window") + 1] == "500"
assert argv[argv.index("--converge-min-iters") + 1] == "1500"
```

Assert W0 isolated postcode/OSRM paths are included and no C0 cache path appears.

- [ ] **Step 2: Run wrapper tests and verify RED**

Run:

```powershell
& 'E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe' -m pytest -q tests/freight_planner/test_run_static_oracle.py
```

Expected: import failure because the wrapper does not exist.

- [ ] **Step 3: Implement the dedicated parser and underlying argv**

Expose only `--out-dir`, `--iterations`, `--seed`, `--delta-r1-min`,
`--converge-pct`, `--converge-window`, and `--converge-min-iters`. Keep W0 dates,
`--epochs 00:00`, `--micro-every-min 0`, oracle input and W0 cache paths fixed.
Default output is `freight_planner/result_runs/W0_static_oracle`.

- [ ] **Step 4: Write failing delegation and fail-closed tests**

Monkeypatch `run_rolling.main` and assert the wrapper:

- refuses to run when oracle parquet or summary JSON is absent;
- verifies the summary source fingerprint against the live combined source;
- sets `run_rolling.DEFAULT_ENRICHED` to the oracle path;
- calls `run_rolling.main` exactly once with the enforced argv.

- [ ] **Step 5: Implement delegation and provenance checks**

The wrapper reads the summary, recomputes the source SHA-256, requires matching
W0 dates and zero census violations, then performs:

```python
run_rolling.DEFAULT_ENRICHED = ORACLE_PATH
return run_rolling.main(build_run_argv(args))
```

No other module global or environment variable is changed.

- [ ] **Step 6: Run focused tests and commit**

Require zero failures, then:

```powershell
git add freight_planner/run_static_oracle.py tests/freight_planner/test_run_static_oracle.py
git commit -m "feat: add isolated W0 day-ahead oracle runner"
```

---

### Task 4: Generate, audit and register the oracle artefact

**Files:**
- Create through CLI: `freight_planner/data/enriched_orders_2026-01_2026-02_DAY_AHEAD_ORACLE.parquet`
- Create through CLI: `freight_planner/data/enriched_orders_2026-01_2026-02_DAY_AHEAD_ORACLE.summary.json`
- Modify: `freight_planner/result_runs/manifest.json`
- Modify: `freight_planner/experiments/RESULTS_OUTLINE.md`

**Interfaces:**
- Consumes: Tasks 1-3 and the untouched combined input.
- Produces: audited oracle input and the authoritative run command.

- [ ] **Step 1: Confirm safe execution conditions**

Before generating data, confirm C0 and beta processes may continue because the
command reads the combined parquet and writes only new oracle paths. Before
launching the oracle solver itself, require the W0 beta continuation controller
to have exited so no two processes write the W0 cache concurrently.

- [ ] **Step 2: Generate the real oracle parquet**

Run from the logistics repository root:

```powershell
& 'E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe' -B -m freight_planner.day_ahead_oracle
```

Require a zero exit code and a summary whose seven census rows each have an
empty `missing_collection_ids` and `future_collection_ids_visible_early`.

- [ ] **Step 3: Independently reconcile the generated file**

Run a read-only verification that asserts:

- source and output row counts match;
- `order_id` sequence matches exactly;
- `timestamp_created_original` equals the source `timestamp_created`;
- every non-null oracle `timestamp_created` has hour/minute/second equal to zero;
- unrelated columns are identical by hashing `pd.util.hash_pandas_object` after
  excluding `timestamp_created` and `timestamp_created_original`.

- [ ] **Step 4: Correct the campaign documentation**

Change `W0_static_oracle` from `blocked_needs_data_prep` to `ready`. Replace the
stale whole-week wording with “daily day-ahead oracle”; change its command to:

```text
python -B -m freight_planner.run_static_oracle
```

In `RESULTS_OUTLINE.md`, rename the comparator “Day-ahead oracle” and state that
all collections booked during day `d` are revealed at `d 00:00`; do not claim
the full week is visible on 16 February.

- [ ] **Step 5: Run focused and full regression tests**

Run:

```powershell
& 'E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe' -m pytest -q tests/freight_planner/test_day_ahead_oracle.py tests/freight_planner/test_run_static_oracle.py tests/freight_planner/test_visibility.py
& 'E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe' -m pytest -q tests/freight_planner
```

Require zero failures.

- [ ] **Step 6: Commit registration and generated provenance**

Do not commit the large generated parquet unless the repository's existing data
policy explicitly tracks comparable enriched parquet files. Always commit the
summary and documentation:

```powershell
git add freight_planner/data/enriched_orders_2026-01_2026-02_DAY_AHEAD_ORACLE.summary.json freight_planner/result_runs/manifest.json freight_planner/experiments/RESULTS_OUTLINE.md
git commit -m "docs: register W0 day-ahead oracle comparator"
```

The actual full W0 oracle solver run is a separate campaign action after the
beta queue has ended; do not launch it as part of implementation without an
explicit run instruction.
