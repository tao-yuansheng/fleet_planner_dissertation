from __future__ import annotations

from pathlib import Path

import pandas as pd

CUSTOMER_LEG_KINDS = {"CUSTOMER_PICKUP", "CUSTOMER_DELIVERY", "DIRECT_CUSTOMER_MOVE"}
OUT_OF_SCOPE_REASONS = {"CANCELLED", "NO_RESOURCES", "CRANE_HIRE", "SPECIALIST_MOVEMENT"}

LEG_TO_CLASS = {
    ("PL_IMPORT", "CUSTOMER_DELIVERY"): "IMPORT_DELIVERY",
    ("PL_EXPORT", "CUSTOMER_PICKUP"): "EXPORT_COLLECTION",
    ("FULL_FLEET", "DIRECT_CUSTOMER_MOVE"): "FF_DIRECT",
    ("FULL_FLEET", "CUSTOMER_PICKUP"): "FF_XDOCK_COLLECT",
    ("FULL_FLEET", "CUSTOMER_DELIVERY"): "FF_XDOCK_DELIVER",
    ("LOCAL_COLLECT", "CUSTOMER_PICKUP"): "LOCAL_COLLECT",
    ("LOCAL_DELIVER", "CUSTOMER_DELIVERY"): "LOCAL_DELIVER",
}


def class_for_leg(row) -> str:
    return LEG_TO_CLASS.get((str(row.flow), str(row.leg_kind)), "OTHER")


def _counts(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=cols + ["count"])
    return df.groupby(cols, dropna=False).size().reset_index(name="count").sort_values(cols)


def _render_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "(none)"
    return df.to_string(index=False)


def _first_existing(df: pd.DataFrame, candidates: list[str]) -> str:
    for candidate in candidates:
        if candidate in df.columns:
            return candidate
    return ""


def _series_get(record, column: str) -> object:
    if not column:
        return ""
    try:
        value = record.get(column, "")
    except AttributeError:
        return ""
    return "" if pd.isna(value) else value


def _key_details(
    canonical: pd.DataFrame,
    manifest: pd.DataFrame,
    missing_in_manifest: set[tuple[str, str]],
    extra_in_manifest: set[tuple[str, str]],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    manifest_date_col = _first_existing(manifest, ["service_date", "plan_date", "date"])
    manifest_reason_col = _first_existing(manifest, ["unassigned_reason"])
    manifest_status_col = _first_existing(manifest, ["plan_status"])
    manifest_order_name_col = _first_existing(manifest, ["order_name", "name"])

    canonical_index = canonical.set_index(["order_id", "order_class"], drop=False) if not canonical.empty else canonical
    manifest_index = manifest.set_index(["order_id", "order_class"], drop=False) if not manifest.empty else manifest

    for key in sorted(missing_in_manifest):
        record = canonical_index.loc[key]
        if isinstance(record, pd.DataFrame):
            record = record.iloc[0]
        rows.append({
            "difference": "CANONICAL_MISSING_IN_MANIFEST",
            "order_id": key[0],
            "order_name": _series_get(record, "order_name"),
            "order_class": key[1],
            "canonical_service_date": _series_get(record, "service_date"),
            "canonical_leg_kind": _series_get(record, "leg_kind"),
            "canonical_status": _series_get(record, "planner_status"),
            "canonical_flow": _series_get(record, "flow"),
            "canonical_responsibility": _series_get(record, "responsibility_shape"),
            "manifest_service_date": "",
            "manifest_status": "",
            "manifest_unassigned_reason": "",
        })

    for key in sorted(extra_in_manifest):
        record = manifest_index.loc[key]
        if isinstance(record, pd.DataFrame):
            record = record.iloc[0]
        rows.append({
            "difference": "MANIFEST_MISSING_IN_CANONICAL",
            "order_id": key[0],
            "order_name": _series_get(record, manifest_order_name_col),
            "order_class": key[1],
            "canonical_service_date": "",
            "canonical_leg_kind": "",
            "canonical_status": "",
            "canonical_flow": "",
            "canonical_responsibility": "",
            "manifest_service_date": _series_get(record, manifest_date_col),
            "manifest_status": _series_get(record, manifest_status_col),
            "manifest_unassigned_reason": _series_get(record, manifest_reason_col),
        })

    return pd.DataFrame(rows)


def reconcile_manifest(legs: pd.DataFrame, manifest: pd.DataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    customer = legs[legs["leg_kind"].isin(CUSTOMER_LEG_KINDS)].copy() if not legs.empty else legs.copy()
    if not customer.empty:
        customer["order_class"] = customer.apply(class_for_leg, axis=1)
        customer["in_scope_for_dispatch"] = customer["planner_status"].eq("DISPATCHABLE")

    manifest_customer = manifest.copy()
    if "order_class" not in manifest_customer.columns:
        manifest_customer["order_class"] = ""
    if "unassigned_reason" not in manifest_customer.columns:
        manifest_customer["unassigned_reason"] = ""

    manifest_customer["is_out_of_scope"] = manifest_customer["unassigned_reason"].fillna("").isin(OUT_OF_SCOPE_REASONS)
    manifest_customer["is_assigned"] = manifest_customer["plan_status"].eq("ASSIGNED")

    lines: list[str] = ["# Phase 0 Manifest Reconciliation\n"]
    lines.append("This compares canonical movement legs against the existing manifest. It is diagnostic only; the two universes do not use the exact same date basis yet.\n")

    lines.append("## Topline\n")
    lines.append(f"- canonical customer legs: {len(customer)}")
    lines.append(f"- canonical dispatchable customer legs: {int(customer['in_scope_for_dispatch'].sum()) if not customer.empty else 0}")
    lines.append(f"- manifest rows: {len(manifest_customer)}")
    lines.append(f"- manifest assigned rows: {int(manifest_customer['is_assigned'].sum())}")
    lines.append(f"- manifest out-of-scope rows: {int(manifest_customer['is_out_of_scope'].sum())}")
    lines.append("")

    lines.append("## Canonical Customer Legs By Class And Status\n")
    lines.append("```text")
    lines.append(_render_table(_counts(customer, ["order_class", "planner_status"])))
    lines.append("```\n")

    lines.append("## Existing Manifest Rows By Class And Status\n")
    lines.append("```text")
    lines.append(_render_table(_counts(manifest_customer, ["order_class", "plan_status"])))
    lines.append("```\n")

    lines.append("## Canonical Dispatchable Legs By Service Date\n")
    dispatchable = customer[customer["in_scope_for_dispatch"]] if not customer.empty else customer
    lines.append("```text")
    lines.append(_render_table(_counts(dispatchable, ["service_date", "order_class"])))
    lines.append("```\n")

    lines.append("## Manifest Unassigned Reasons\n")
    unassigned = manifest_customer[~manifest_customer["is_assigned"]]
    lines.append("```text")
    lines.append(_render_table(_counts(unassigned, ["unassigned_reason", "order_class"])))
    lines.append("```\n")

    canonical_keys = set(zip(customer["order_id"].astype(str), customer["order_class"].astype(str))) if not customer.empty else set()
    manifest_keys = set(zip(manifest_customer["order_id"].astype(str), manifest_customer["order_class"].astype(str))) if not manifest_customer.empty else set()
    missing_in_manifest = canonical_keys - manifest_keys
    extra_in_manifest = manifest_keys - canonical_keys
    detail_path = out_path.with_name(out_path.stem + "_key_differences.csv")
    key_details = _key_details(customer, manifest_customer, missing_in_manifest, extra_in_manifest)
    key_details.to_csv(detail_path, index=False)

    lines.append("## Key Set Differences\n")
    lines.append(f"- canonical order/class keys missing in manifest: {len(missing_in_manifest)}")
    lines.append(f"- manifest order/class keys missing in canonical: {len(extra_in_manifest)}")
    lines.append(f"- detail csv: `{detail_path.name}`")
    lines.append("")

    if missing_in_manifest:
        sample = pd.DataFrame(list(missing_in_manifest), columns=["order_id", "order_class"]).head(30)
        lines.append("### Missing In Manifest Sample\n")
        lines.append("```text")
        lines.append(sample.to_string(index=False))
        lines.append("```\n")
    if extra_in_manifest:
        sample = pd.DataFrame(list(extra_in_manifest), columns=["order_id", "order_class"]).head(30)
        lines.append("### Missing In Canonical Sample\n")
        lines.append("```text")
        lines.append(sample.to_string(index=False))
        lines.append("```\n")

    out_path.write_text("\n".join(lines), encoding="utf-8")
