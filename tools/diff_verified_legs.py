"""Order-by-order comparison of two verified_legs snapshots (baseline vs rebuilt).

    python -m freight_planner.tools.diff_verified_legs \
        --old freight_planner/data/verified_legs.before_gpsmatch.csv \
        --new freight_planner/data/verified_legs.csv \
        --out freight_planner/data/verified_legs_diff.csv
"""
from __future__ import annotations

import argparse

import pandas as pd

_COLS = ["leg", "confidence", "method"]


def build_diff(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    old = old.drop_duplicates(subset="order_id", keep="first")
    new = new.drop_duplicates(subset="order_id", keep="first")
    o = old.set_index(old["order_id"].astype(str))
    n = new.set_index(new["order_id"].astype(str))
    ids = list(dict.fromkeys(list(o.index) + list(n.index)))
    rows = []
    for oid in ids:
        orow = o.loc[oid] if oid in o.index else None
        nrow = n.loc[oid] if oid in n.index else None
        base = nrow if nrow is not None else orow
        rec = {
            "order_id": oid,
            "order_name": base.get("order_name", ""),
            "api_flow": base.get("api_flow", ""),
        }
        for c in _COLS:
            rec[f"old_{c}"] = "" if orow is None else str(orow.get(c, "") or "")
            rec[f"new_{c}"] = "" if nrow is None else str(nrow.get(c, "") or "")
        rec["changed"] = any(rec[f"old_{c}"] != rec[f"new_{c}"] for c in _COLS)
        rows.append(rec)
    return pd.DataFrame(rows)


def transition_matrix(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    old = old.drop_duplicates(subset="order_id", keep="first")
    new = new.drop_duplicates(subset="order_id", keep="first")
    o = old.set_index(old["order_id"].astype(str))["leg"]
    n = new.set_index(new["order_id"].astype(str))["leg"]
    j = pd.DataFrame({"old": o, "new": n}).dropna()
    return pd.crosstab(j["old"], j["new"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--out", default="freight_planner/data/verified_legs_diff.csv")
    args = ap.parse_args()
    old = pd.read_csv(args.old, dtype=str)
    new = pd.read_csv(args.new, dtype=str)
    diff = build_diff(old, new)
    diff.to_csv(args.out, index=False)
    print(f"rows: {len(diff)} | changed: {int(diff['changed'].sum())}")
    print("\n=== leg transition matrix (old rows x new cols) ===")
    print(transition_matrix(old, new).to_string())
    print("\n=== method transitions (top 15) ===")
    mt = diff.groupby(["old_method", "new_method"]).size().sort_values(ascending=False)
    print(mt.head(15).to_string())
    print(f"\n-> {args.out}")


if __name__ == "__main__":
    main()
