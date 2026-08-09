"""Consolidated printable runsheets for a whole plan window.

Reads only ``plan/route_stops.csv`` (the runsheet-grade per-stop table shared
with the trip app), so it stays decoupled from planner internals. One
self-contained HTML: per-vehicle sections (page-break per vehicle for browser
printing), one table per service day with depot start/return, ordered stops,
statutory-break rows and trip (reload) boundaries.
"""
from __future__ import annotations

import argparse
import html as _html
from pathlib import Path

import pandas as pd

_CSS = """
body{font-family:system-ui,-apple-system,sans-serif;font-size:12px;color:#111;margin:24px}
h1{font-size:18px;margin:0 0 2px} .sub{color:#666;font-size:11px;margin-bottom:18px}
section.vehicle{page-break-before:always;margin-top:28px}
section.vehicle:first-of-type{page-break-before:auto;margin-top:0}
h2{font-size:15px;border-bottom:2px solid #333;padding-bottom:3px;margin:0 0 2px}
.vmeta{color:#555;font-size:11px;margin-bottom:8px}
h3{font-size:12px;margin:12px 0 4px}
table{border-collapse:collapse;width:100%} th,td{border:1px solid #bbb;padding:3px 6px;text-align:left;font-size:11px}
th{background:#eee} td.num{text-align:right;font-variant-numeric:tabular-nums}
tr.break td{background:#fff6dd;color:#7a5c00;font-style:italic}
tr.depot td{background:#f2f2f2;color:#444}
tr.reload td{background:#eef4ff;color:#33507a;font-style:italic}
@media print{body{margin:8mm} a{color:inherit}}
"""


def _fmt_time(ts: str) -> str:
    s = str(ts or "")
    return s[11:16] if len(s) >= 16 else ""


def _stop_label(stop_type: str) -> str:
    return str(stop_type or "").replace("_", " ")


def build_runsheets_html(route_stops: pd.DataFrame, title: str = "Runsheets") -> str:
    df = route_stops.copy()
    if df.empty:
        return f"<html><body><h1>{_html.escape(title)}</h1><p>No routes.</p></body></html>"
    for col, default in (("vehicle_type", ""), ("break_minutes_before", 0.0),
                         ("is_tour", False), ("collect_pc", "")):
        if col not in df.columns:
            df[col] = default
    # CSV round-trips turn empty text cells into NaN; never render "nan"
    for col in ("order_id", "service_pc", "node", "planned_arrive",
                "planned_depart", "vehicle_type", "collect_pc"):
        df[col] = df[col].fillna("").astype(str).replace("nan", "")
    df["is_tour"] = df["is_tour"].astype(str).str.lower().isin(("true", "1", "1.0"))

    out: list[str] = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{_html.escape(title)}</title><style>{_CSS}</style></head><body>",
        f"<h1>{_html.escape(title)}</h1>",
    ]
    n_veh = df["vehicle_id"].nunique()
    total_km = float(df["leg_km"].fillna(0).sum())
    out.append(f"<div class=sub>{n_veh} vehicles · {total_km:,.0f} km planned · "
               f"{int((~df['stop_type'].isin(['depot_start','depot_return'])).sum())} stops</div>")

    df["_home"] = df["vehicle_home_depot"].astype(str)
    for (home, vid), vg in df.groupby(["_home", "vehicle_id"], sort=True):
        vtype = str(vg["vehicle_type"].iloc[0] or "")
        days = sorted(vg["service_date"].astype(str).unique())
        vkm = float(vg["leg_km"].fillna(0).sum())
        out.append("<section class=vehicle>")
        out.append(f"<h2>{_html.escape(str(vid))}</h2>")
        out.append(f"<div class=vmeta>{_html.escape(vtype)} · home {_html.escape(home)} · "
                   f"{len(days)} active day(s) · {vkm:,.0f} km</div>")
        for day in days:
            dg = vg[vg["service_date"].astype(str) == day]
            tour_mark = " · multi-day tour" if bool(dg["is_tour"].all()) else ""
            out.append(f"<h3>{_html.escape(day + tour_mark)}</h3>")
            out.append("<table><tr><th>#</th><th>stop</th><th>order</th><th>postcode</th>"
                       "<th>arrive</th><th>depart</th><th>leg km</th><th>on board (pal/kg)</th></tr>")
            prev_trip = None
            for r in dg.sort_values(["route_id", "trip_index", "sequence"]).itertuples(index=False):
                trip = (str(r.route_id), int(getattr(r, "trip_index", 0) or 0))
                if prev_trip is not None and trip != prev_trip:
                    out.append("<tr class=reload><td colspan=8>reload / turnaround at depot</td></tr>")
                prev_trip = trip
                brk = float(getattr(r, "break_minutes_before", 0.0) or 0.0)
                if brk > 0:
                    out.append(f"<tr class=break><td colspan=8>{brk:.0f}-min statutory break "
                               f"(EU drivers' hours: 45 min after 4.5 h driving)</td></tr>")
                stype = str(r.stop_type)
                cls = " class=depot" if stype in ("depot_start", "depot_return") else ""
                seq = int(getattr(r, "sequence", 0) or 0)
                order = str(getattr(r, "order_id", "") or "")
                pals = float(getattr(r, "load_pallets_after", 0.0) or 0.0)
                kg = float(getattr(r, "load_kg_after", 0.0) or 0.0)
                leg_km = float(getattr(r, "leg_km", 0.0) or 0.0)
                # two-point moves (collect at customer A, deliver/drop at B): emit
                # the collection point first — the drive belongs to reaching it
                collect_pc = str(getattr(r, "collect_pc", "") or "")
                if stype in ("direct_customer_move", "hub_drop") and collect_pc:
                    out.append(
                        f"<tr><td>{seq}</td>"
                        f"<td>{_html.escape(f'collect ({_stop_label(stype)})')}</td>"
                        f"<td>{_html.escape(order)}</td>"
                        f"<td>{_html.escape(collect_pc)}</td>"
                        f"<td></td><td></td>"
                        f"<td class=num>{leg_km:.1f}</td>"
                        f"<td class=num>{pals:.0f} / {kg:,.0f}</td></tr>")
                    leg_km = 0.0
                out.append(
                    f"<tr{cls}><td>{seq}</td>"
                    f"<td>{_html.escape(_stop_label(stype))}</td>"
                    f"<td>{_html.escape(order)}</td>"
                    f"<td>{_html.escape(str(getattr(r, 'service_pc', '') or getattr(r, 'node', '') or ''))}</td>"
                    f"<td>{_fmt_time(getattr(r, 'planned_arrive', ''))}</td>"
                    f"<td>{_fmt_time(getattr(r, 'planned_depart', ''))}</td>"
                    f"<td class=num>{leg_km:.1f}</td>"
                    f"<td class=num>{pals:.0f} / {kg:,.0f}</td></tr>")
            out.append("</table>")
        out.append("</section>")
    out.append("</body></html>")
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan-dir", required=True, help="a run's plan/ folder (has route_stops.csv)")
    ap.add_argument("--out", default="", help="output HTML (default: <plan>/../reports/runsheets.html)")
    ap.add_argument("--title", default="Runsheets")
    args = ap.parse_args()
    plan = Path(args.plan_dir)
    stops = pd.read_csv(plan / "route_stops.csv")
    out = Path(args.out) if args.out else plan.parent / "reports" / "runsheets.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_runsheets_html(stops, title=args.title), encoding="utf-8")
    print(f"runsheets: {stops['vehicle_id'].nunique()} vehicles -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
