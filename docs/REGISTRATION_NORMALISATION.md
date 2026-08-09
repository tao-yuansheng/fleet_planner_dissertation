# Vehicle Registration Normalisation

**Last updated:** 2026-05-19  
**Relevant file:** `data_audit.py` — function `_normalise_reg(reg: str) -> str`

---

## Why normalisation is needed

ZEEFLEET data flows through three systems — Qargo (TMS), Supatrak (telematics), and Jigsaw (fuel) — each of which stores vehicle registrations in a slightly different format. Without normalisation, a cross-system join on raw registration strings produces a ~15% match rate. After normalisation it reaches ~78%.

---

## Patterns found in the data

Two systematic formatting differences were identified from the January–February 2026 audit:

### 1. Trailing trailer-index suffix — `"BD22ASZ 2"`

Qargo appends a space followed by a digit to the registration of the second unit in a tractor-trailer combination. This is a TMS convention to distinguish the trailer from the tractor within the same order record.

| Raw (Qargo) | Normalised | Notes |
|---|---|---|
| `BD22ASZ 2` | `BD22ASZ` | trailer index stripped |
| `N888RNW 2` | `N888RNW` | same pattern |
| `LL68LZE 2` | `LL68LZE` | same pattern |
| `HX17 CUA 2` | `HX17CUA` | index + space both removed |

### 2. Internal spaces — `"HV16 PXP"`

Some registrations are stored with a space between the area code and the serial letters. This is common in physical plate formatting but inconsistent across systems.

| Raw | Normalised |
|---|---|
| `HV16 PXP` | `HV16PXP` |
| `N888 WCH` | `N888WCH` |

---

## Normalisation function

```python
import re

def _normalise_reg(reg: str) -> str:
    reg = reg.strip().upper()
    reg = re.sub(r'\s+\d+$', '', reg)   # strip trailing space+digits (trailer index)
    reg = re.sub(r'\s+', '', reg)        # collapse remaining internal spaces
    return reg
```

**Order matters:** the trailing-index strip must run before the space-collapse. Otherwise `"HX17 CUA 2"` would become `"HX17CUA2"` (wrong) instead of `"HX17CUA"` (correct).

---

## Match rate improvement

| Join | Before normalisation | After normalisation |
|---|---|---|
| Qargo → Supatrak | 15.1% (raw) → 63.5% (comma-split only) | **78.2%** |
| Qargo → Jigsaw | 14.8% (raw) → 62.5% (comma-split only) | **78.2%** |
| Supatrak → Jigsaw | 69.1% | **70.1%** |
| Vehicle type coverage (Qargo orders with derived AssetType) | 79.3% | **86.1%** |

The comma-split fix (applied before normalisation) contributed the largest jump — from 15% to ~63% — by splitting compound strings like `"AB12CDE, TR99XYZ"`. Normalisation added a further ~15 percentage points.

---

## Remaining gaps (~22% of Qargo regs unmatched)

The registrations in `in_qargo_only_sample` after normalisation fall into three categories:

| Pattern | Example | Explanation |
|---|---|---|
| Subcontractor vehicles | `BX67ZFY`, `LN25NKE` | Vehicles not owned by ZEEFLEET, absent from both Supatrak and Jigsaw |
| Non-standard identifiers | `ST20`, `ST22` | Likely depot or slot codes, not real registrations |
| Double-digit suffix variants | `P888RNW2`, `S888GNW2` | No space before the `2`, so the regex doesn't strip it — these are edge cases of the trailer-index pattern |

The double-digit-suffix edge case (`P888RNW2`) could be handled by extending the regex to `r'\d+$'` (strip any trailing digits). However, this risks stripping legitimate digit-ending registrations (e.g. `N888RNW` would become `N888RN`). Without a ground-truth list it is not safe to apply this more aggressively. The 22% unmatched are predominantly subcontractors, so the Phase 1 MCTS vehicle master list will simply exclude those orders from simulation batches.

---

## Where this is applied

`_normalise_reg` is called in three places in `data_audit.py`:

1. **`_extract_qargo_regs`** — each part after comma-splitting a Qargo resource field
2. **`audit_vehicle_crossjoin`** — Supatrak `AssetName` and Jigsaw `vehicleRegistration` before building the comparison sets
3. **`_derive_vehicle_types`** — building the `reg → AssetType` lookup from Supatrak and normalising Qargo parts before lookup

Any future module that joins across these three systems (e.g. the Phase 1 `vehicle_id_mapping.csv` builder) should apply the same function.
