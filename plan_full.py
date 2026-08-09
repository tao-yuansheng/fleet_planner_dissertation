"""Export ``plan_full.csv`` — one denormalized row per plan movement.

Successor convenience view of the old pipeline's single ``plan_manifest_*.csv``:
the spine is ``plan_manifest_new.csv`` (one row per movement, every order in the
universe appears, assigned or not), enriched with

  * freight endpoints (origin/destination), order_name (WT number), flow,
    windows and size from the movement-leg universe, rebuilt with exactly the
    arguments recorded in the window's ``run_manifest.json``;
  * stop-level execution detail (vehicle type/home depot, sequence, planned
    arrive/depart, leg km, load after stop) from ``route_stops.csv``.

Row count always equals ``plan_manifest_new.csv`` row count.

Usage:
  python -m freight_planner.plan_full --month freight_planner/runs/2026-01
  python -m freight_planner.plan_full --window freight_planner/runs/2026-01/2026-01-12_to_2026-01-17
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from freight_planner.build_phase0 import _load_cache, _load_qargo, _parse_date
from freight_planner.demand import build_demand_records
from freight_planner.legs import build_movement_leg_records
from freight_planner.output_layout import find_artifact
from freight_planner.paths import DEFAULT_POSTCODE_CACHE

COLUMNS = [
    "order_id", "order_name", "leg_id", "job_id",
    "manifest_kind", "status", "reason", "flow", "leg_kind",
    "origin", "destination", "origin_node", "destination_node",
    "source_depot", "target_depot",
    "vehicle_id", "vehicle_type", "vehicle_home_depot",
    "route_id", "trip_id", "trip_index", "service_date", "sequence",
    "planned_arrive", "planned_depart",
    "window_start", "window_end",
    "pallets", "weight_kg",
    "leg_km", "planned_km", "load_pallets_after",
]

_STATUS_ORDER = {"ROUTED": 0, "ACCOUNTING": 1, "UNASSIGNED": 2, "BLOCKED": 3}

DICTIONARY_MD = """# plan_full.csv — data dictionary

One denormalized row per plan movement (row count always equals
`plan_manifest_new.csv`). Every in-universe order appears at least once —
routed, accounted, or unassigned-with-reason — so the file reconciles coverage
on its own. Sorted routes-first in drive order (status, vehicle, date, trip,
sequence); unassigned/accounting rows sort to the end.

Grain notes: an XDOCK order contributes TWO rows (pickup leg + delivery leg);
a multi-day DIRECT order contributes one `direct_customer_move` row. Depot
returns are their own rows so route km reconciles. The fixed night-trunk
service itself lives in `trunk_schedule.csv`; the `*_trunk` rows here are the
per-order ledger attributions riding the network, not vehicle routes.

## Identifiers

| column | meaning |
|---|---|
| `order_id` | Qargo order UUID. Blank on depot service rows (`depot_return`/`depot_load`). |
| `order_name` | Human-facing WT number from Qargo. Blank on depot service rows. |
| `leg_id` | Movement-leg id (the freight-movement unit the planner works on). Joins `route_stops.csv`. Blank on depot service rows; shuttle/merge-synthesized legs carry generated ids absent from the raw universe. |
| `job_id` | ALNS schedulable-job id (daily portion). Joins `selected_plan_alns.csv` / `unassigned_jobs.csv`. Blank for trunk-accounted, accounting-only and depot_return rows. |
| `route_id` | One vehicle's whole planned assignment starting on a date (`ROUTE:<veh>:<date>`); normally a single day, multi-day when it is a tour (sleep-out). Joins `vehicle_routes.csv` / `route_stops.csv`. |
| `trip_id` / `trip_index` | One depot-out -> stops -> depot-back loop inside the route (`<route_id>#T<n>`); `trip_index` is the 1-based ordinal. Multi-trip routes reload at the depot between trips; `sequence` keeps counting across trips. |

## Classification

| column | meaning |
|---|---|
| `manifest_kind` | Movement type: `customer_delivery` (depot -> customer last-mile leg), `customer_pickup` (customer -> depot collection leg), `direct_customer_move` (two-point move without a depot touch, incl. multi-day tour carries), `depot_load` (explicit depot-side loading movement, rare), `depot_return` (end-of-trip return to home depot), `inbound_trunk` (network trunk bringing an order INTO our depot), `outbound_trunk` (freight handed OUT to the pallet network via hub), `accounting_only` (in-universe order we do not move — record kept so coverage reconciles). |
| `status` | `ROUTED` (on a planned vehicle route) / `ACCOUNTING` (ledger attribution, no vehicle here) / `UNASSIGNED` (planner did not place it — see `reason`) / `BLOCKED` (cannot be placed — see `reason`). |
| `reason` | Blank when ROUTED. ACCOUNTING: `AT_DEPOT` (inbound trunk landed the freight at our depot), `WITH_NETWORK` (outbound freight now with the pallet network), `NO_RESOURCES` (third-party/subcontractor moved it, not our fleet), `CANCELLED`, `AMBIGUOUS_MANUAL`. UNASSIGNED reasons describe the failed constraint or dependency. BLOCKED: e.g. `MASSIVE_UNSUPPORTED` (exceeds any single vehicle). Other weeks may show `BAD_GEOCODE`, `CAPACITY_OVERFLOW`, `NO_FEASIBLE_ROUTE`, `DELIVERY_BEFORE_PICKUP`, or `NO_FEASIBLE_TOUR`. |
| `flow` | Order flow: `PL_IMPORT` (network brings in, we deliver locally), `PL_EXPORT` (we collect locally, network delivers), `FULL_FLEET` (ours end-to-end). Verified-leg corrected where applicable. |
| `leg_kind` | Leg-universe kind (upper-case mirror of `manifest_kind` for order legs; blank on depot service rows and synthesized legs). |

## Endpoints

| column | meaning |
|---|---|
| `origin` / `destination` | Freight endpoints of THIS movement: a postcode at customer ends, a depot code (`CB22`/`BEDFORD`/`STOKE`) at depot ends. `depot_return`: origin blank (wherever the route was), destination = home depot. `accounting_only`: both sides show the order's service postcode (no movement modelled). |
| `origin_node` / `destination_node` | End type: `DEPOT`, `CUSTOMER`, `B37_HUB` (Palletline national hub, Birmingham), `LE10_HUB` (Hazchem network hub). Blank on depot service and accounting-only rows. |
| `source_depot` / `target_depot` | Staging depots resolved for the leg (which depot the freight leaves from / lands at). |

### Depot codes and postcodes

Wherever `origin` / `destination` shows a depot code (source: `cambridge/config.py`
`DEPOT_ANCHORS` / `DEPOT_POSTCODES`):

| code | postcode | place | role |
|---|---|---|---|
| `CB22` | CB22 4PS | Duxford, Cambridge | main depot, full Palletline member |
| `BEDFORD` | MK42 0LF | Bedford | second depot, full Palletline member |
| `STOKE` | ST4 8HP | Stoke-on-Trent | satellite yard (single vehicle, no dock) |
| `ST_IVES` | PE27 3WR | St Ives | structural key only — fleet reassigned to CB22 |

Network hubs seen in `origin_node` / `destination_node` are handover points, not
our depots: `B37_HUB` = Palletline national hub, east Birmingham (B37 7HB;
52.4666, -1.7226); `LE10_HUB` = Hazchem network hub, Hinckley LE10
(52.5408, -1.4136).

## Assignment & timing

| column | meaning |
|---|---|
| `vehicle_id` | Assigned vehicle registration. Filled only when ROUTED. |
| `vehicle_type` | `tractor` / `rigid` / `van` (drives the per-km cost rate). |
| `vehicle_home_depot` | `CB22` / `BEDFORD` / `STOKE`. |
| `service_date` | Calendar day the movement happens. For ACCOUNTING rows this is the ledger event date and may fall outside the planning window (e.g. inbound trunk arriving the day before). |
| `sequence` | 1-based stop position within the vehicle's DAY (continues across trips). |
| `planned_arrive` / `planned_depart` | Planned timestamps at the stop. Blank on depot_return, trunk/accounting rows and a handful of tour-edge stops. |
| `window_start` / `window_end` | EFFECTIVE service window the planner enforced (after operating-day expansion / anchoring rules) — not the raw TMS stamps. Blank on depot service rows. |

## Freight & distance

| column | meaning |
|---|---|
| `pallets` / `weight_kg` | Freight size of this leg (massive orders may be pre-split into parts). Blank on depot service rows. |
| `leg_km` | Road km driven INTO this stop from the previous stop. Blank on non-stop rows. |
| `planned_km` | Manifest's km attribution for the movement; equals `leg_km` on routed rows. Blank on trunk/accounting rows (their km is the trunk service's, not the order's). |
| `load_pallets_after` | Vehicle load on departure from this stop. |
"""


def _leg_endpoints(row: pd.Series) -> tuple[str, str]:
    origin_pc = str(row.get("origin_pc") or "")
    service_pc = str(row.get("service_pc") or "")
    source_depot = str(row.get("source_depot") or "")
    target_depot = str(row.get("target_depot") or "")
    hub = str(row.get("hub") or "")
    origin_node = str(row.get("origin_node") or "")
    destination_node = str(row.get("destination_node") or "")

    if origin_pc:
        origin = origin_pc
    elif origin_node == "DEPOT":
        origin = source_depot
    else:
        origin = service_pc
    if destination_node == "CUSTOMER":
        destination = service_pc
    else:
        destination = target_depot or hub or service_pc
    return origin, destination


def _legs_frame(window_dir: Path, qargo_cache: dict) -> pd.DataFrame:
    with open(window_dir / "run_manifest.json", encoding="utf-8") as f:
        rm = json.load(f)
    qargo_path = str(rm["qargo"])
    if qargo_path not in qargo_cache:
        qargo_cache[qargo_path] = _load_qargo(Path(qargo_path))
    qargo_df = qargo_cache[qargo_path]
    postcode_cache = _load_cache(DEFAULT_POSTCODE_CACHE)
    demand = build_demand_records(
        qargo_df, _parse_date(rm["start"]), _parse_date(rm["end"]),
        responsibility_mode=rm.get("responsibility_mode", "forward_structural"),
    )
    legs = pd.DataFrame([r.to_dict() for r in build_movement_leg_records(qargo_df, demand, postcode_cache)])
    endpoints = legs.apply(_leg_endpoints, axis=1, result_type="expand")
    legs["origin"], legs["destination"] = endpoints[0], endpoints[1]
    keep = ["leg_id", "order_name", "flow", "leg_kind", "origin", "destination",
            "origin_node", "destination_node", "source_depot", "target_depot",
            "effective_window_start", "effective_window_end", "pallets", "weight_kg"]
    legs = legs[keep].rename(columns={
        "effective_window_start": "window_start", "effective_window_end": "window_end"})
    return legs.drop_duplicates(subset="leg_id", keep="first")


def build_plan_full(window_dir: Path, qargo_cache: dict | None = None) -> pd.DataFrame:
    manifest = pd.read_csv(find_artifact(window_dir, "plan_manifest_new.csv"), dtype={"leg_id": str})
    stops = pd.read_csv(find_artifact(window_dir, "route_stops.csv"), dtype={"leg_id": str})
    legs = _legs_frame(window_dir, qargo_cache if qargo_cache is not None else {})

    stop_cols = ["vehicle_type", "vehicle_home_depot", "sequence",
                 "planned_arrive", "planned_depart", "leg_km", "load_pallets_after",
                 "service_pc", "collect_pc", "node"]

    # leg-carrying stops join 1:1 on (route_id, leg_id)
    leg_stops = stops[stops["leg_id"].notna() & (stops["leg_id"] != "")]
    leg_stops = leg_stops.drop_duplicates(subset=["route_id", "leg_id"], keep="first")
    out = manifest.merge(
        leg_stops[["route_id", "leg_id"] + stop_cols],
        on=["route_id", "leg_id"], how="left")

    # depot_return / depot_load manifest rows have no leg_id: join their stop
    # detail per (route_id, trip_index, stop_type) — multi-trip routes emit one
    # depot_return per trip, so route_id alone under-keys
    def _trip(v) -> int:
        return int(v) if pd.notna(v) else -1

    depot_stops = stops[stops["stop_type"].isin(("depot_return", "depot_load"))]
    depot_detail = {}
    for _, srow in depot_stops.iterrows():
        depot_detail.setdefault((srow["route_id"], _trip(srow.get("trip_index")), srow["stop_type"]), srow)
        depot_detail.setdefault((srow["route_id"], None, srow["stop_type"]), srow)
    for kind in ("depot_return", "depot_load"):
        mask = (out["manifest_kind"] == kind) & (out["leg_id"].isna() | (out["leg_id"] == ""))
        for i in out.index[mask]:
            rid = out.at[i, "route_id"]
            srow = depot_detail.get((rid, _trip(out.at[i, "trip_index"]), kind),
                                    depot_detail.get((rid, None, kind)))
            if srow is not None:
                for c in stop_cols:
                    out.at[i, c] = srow[c]
                # depot stops carry the depot code in `node`, not service_pc
                out.at[i, "destination"] = srow["service_pc"] if pd.notna(srow["service_pc"]) and srow["service_pc"] else srow["node"]

    out = out.merge(legs, on="leg_id", how="left", suffixes=("", "_leg"))
    # depot rows set destination before the merge; recover it past the suffix
    if "destination_leg" in out.columns:
        out["destination"] = out["destination"].where(
            out["destination"].notna() & (out["destination"] != ""), out["destination_leg"])
        out = out.drop(columns=[c for c in out.columns if c.endswith("_leg")])

    # synthesized legs (shuttle/merge) are absent from the rebuilt universe:
    # fall back to the stop's own postcodes for their endpoints
    def _fill(col: str, src: str) -> None:
        empty = out[col].isna() | (out[col].astype(str) == "")
        have = out[src].notna() & (out[src].astype(str) != "")
        out.loc[empty & have, col] = out.loc[empty & have, src]
    _fill("destination", "service_pc")
    _fill("origin", "collect_pc")
    out = out.drop(columns=["service_pc", "collect_pc", "node"], errors="ignore")

    for c in COLUMNS:
        if c not in out.columns:
            out[c] = ""
    out["_rank"] = out["status"].map(_STATUS_ORDER).fillna(9)
    out = out.sort_values(
        ["_rank", "vehicle_id", "service_date", "route_id", "trip_index", "sequence"],
        na_position="last")
    return out[COLUMNS].reset_index(drop=True)


def emit_plan_full(window_dir: Path, runlog, qargo_cache: dict | None = None) -> bool:
    """Auto-emission hook for the runners: write ``<window>/plan_full.csv`` (the
    root deliverable) + the data dictionary in ``reports/`` (``md/`` on interim
    2026-07-14..16 runs). A failure here is a derived-view problem, not a plan
    problem — log LOUDLY, never raise (a crash after an hour-long solve must not
    lose the run)."""
    window_dir = Path(window_dir)
    try:
        df = build_plan_full(window_dir, qargo_cache if qargo_cache is not None else {})
        out_path = window_dir / "plan_full.csv"
        df.to_csv(out_path, index=False)
        rep_dir = next((d for d in (window_dir / "reports", window_dir / "md")
                        if d.is_dir()), window_dir)
        dict_path = rep_dir / "08_plan_full_dictionary.md"
        dict_path.write_text(DICTIONARY_MD, encoding="utf-8")
        runlog.log(f"plan_full: {len(df)} rows -> {out_path}")
        return True
    except Exception as e:  # noqa: BLE001 — deliberate: derived view must not kill the run
        runlog.log(f"plan_full FAILED (plan outputs are intact): {type(e).__name__}: {e}")
        return False


def _summarize(window_dir: Path, df: pd.DataFrame, manifest_rows: int) -> str:
    routed = df[df["status"] == "ROUTED"]
    leg_routed = routed[routed["leg_id"].notna() & (routed["leg_id"] != "")]
    missing_stop = int(leg_routed["sequence"].isna().sum())
    ep_ok = int(((df["origin"].fillna("") != "") | (df["destination"].fillna("") != "")).sum())
    return (f"{window_dir.name}: {len(df)} rows (manifest {manifest_rows}) | "
            f"routed leg rows missing stop detail: {missing_stop} | "
            f"rows with an endpoint: {ep_ok}/{len(df)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", help="month dir containing window subdirs")
    parser.add_argument("--window", help="single window dir")
    args = parser.parse_args()

    if args.window:
        windows = [Path(args.window)]
    elif args.month:
        windows = sorted(p for p in Path(args.month).iterdir()
                         if p.is_dir() and (p / "run_manifest.json").exists())
    else:
        parser.error("pass --month or --window")

    qargo_cache: dict = {}
    for w in windows:
        manifest_rows = len(pd.read_csv(find_artifact(w, "plan_manifest_new.csv")))
        df = build_plan_full(w, qargo_cache)
        out_path = w / "plan_full.csv"   # root deliverable (legacy runs get it there too)
        df.to_csv(out_path, index=False)
        print(_summarize(w, df, manifest_rows))
        print(f"  -> {out_path}")

    dict_dir = Path(args.month) if args.month else windows[0]
    dict_path = dict_dir / "08_plan_full_dictionary.md"
    dict_path.write_text(DICTIONARY_MD, encoding="utf-8")
    print(f"data dictionary -> {dict_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
