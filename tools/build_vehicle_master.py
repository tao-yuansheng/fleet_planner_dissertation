"""Build a single source-of-truth vehicle master list with payload & pallet capacity.

Inputs (existing, authoritative for what they cover):
  * Supatrak enriched list  -> reg, AssetType, make/model, GROSS weight (GVW for
    rigids/vans, GCW for tractors), fuel, CircuitName (depot), active flag.
  * Jigsaw fuel data        -> per-reg operating TONNAGE class (`vehicleGroup2_Name`,
    e.g. "26 TONNE", "44 TONNE") and make (`vehicleGroup1_Name`). The tonnage class
    is the most reliable operating-class signal and even corrects supatrak errors
    (e.g. a Mitsubishi Canter mis-listed as 26t GVW is correctly "7.5 TONNE").

The two gaps supatrak/jigsaw do NOT carry are PAYLOAD (kg) and PALLET capacity.
We fill them per UK-haulage operating CLASS from published standards (online,
2026-06) — see CLASS_SPEC sources. These are class standards, not per-reg exact
specs (per-reg payload/pallets is not reliably available online); every derived
field carries provenance + a confidence so the numbers stay honest.

    python -B -m freight_planner.tools.build_vehicle_master   # -> freight_planner/data/vehicle_master.csv
"""
from __future__ import annotations

import json

import pandas as pd

from freight_planner.shared.paths import LOGISTICS_ROOT as BASE  # data/artifact paths unchanged
SUPATRAK = BASE / "data" / "Input" / "supatrak" / "supatrak_vehicle_list_enriched.csv"
JIGSAW = [
    BASE / "data" / "Input" / "profitability" / "jigsaw_20260101_to_20260131.csv",
    BASE / "data" / "Input" / "profitability" / "jigsaw_20260201_to_20260228.csv",
]
# telematics-derived observed loads (axle weight where the vehicle has a sensor, else
# order-derived) — used to cross-check the assigned class, NOT as the capacity itself.
PROFILES_JSON = BASE / "data" / "Output" / "cambridge" / "vehicle_profiles_derived.json"
# Finalized enriched vehicle dataset lives with the pipeline's other data (enriched_orders).
OUT_CSV = BASE / "freight_planner" / "data" / "vehicle_master.csv"
MOT_RESULTS = BASE / "freight_planner" / "data" / "mot_results.csv"  # cached Selenium MOT scrape (model + expiry)

# Manually validated class corrections (telematics / reg-plate / human review). These
# WIN over jigsaw + supatrak. (reg -> operating_class, confidence, note). Append as
# rows are validated.
VALIDATED_CLASS: dict[str, tuple[str, str, str]] = {
    "N88RNW": ("18 TONNE", "HIGH", "axle-weight observed max ~9.2t/16pal -> 18t not 26t"),
    "BF65WBY": ("7.5 TONNE", "HIGH", "reg: DAF LF 150 FA = 7.5t GVW; max actual trip 2.6t/10pal"),
    "FJ72XFF": ("7.5 TONNE", "HIGH", "reg: DAF LF 150 FA = 7.5t GVW"),
    "RF22HRO": ("7.5 TONNE", "MEDIUM", "reg: DAF LF 180 FA (LF180 spans 7.5-16t); jigsaw 7.5t, no load data"),
}

# Columns kept in the finalized master. Dropped as redundant/placeholder/working data:
# gross_tonnes & weight_metric (unreliable supatrak placeholder, replaced by payload_kg);
# body_type (derivable from operating_class); capacity_source (constant); has_odometer
# (telematics flag, not a spec); supatrak_typical_tonnes (model estimate, not telematics);
# observed_pallets_max (noisy multi-trip daily totals — observed_kg_max kept as the clean
# axle-measured cross-check); shift_start & shift_end (REMOVED 2026-07-16, user rule:
# telematics shift medians are not operating constraints — the fleet works one
# operating day bounded by the driving/duty caps, not per-vehicle walls);
# median_trips_per_day & multi_trip_day_pct (REMOVED 2026-07-16, user rule: no
# trip-count cap — duty/driving feasibility is the honest limit); the four
# capacity_*_per_trip/source profile columns (REMOVED 2026-07-16: payload_kg /
# pallet_capacity are the capacity truth the planner resolves; the telematics-p95
# profile numbers sat beside them contradicting the physical payload — a 1.2t van
# carried a 10t fallback figure — and served nothing).
FINAL_COLUMNS = [
    "reg", "active", "depot", "fleet_kind", "fleet_reg",
    "profile_asset_type",
    "master_max_tonnes", "master_typical_tonnes",
    "asset_type", "make", "model", "operating_class",
    "payload_kg", "pallet_capacity", "fuel_type",
    "observed_kg_max", "has_axle_weight",
    "class_source", "confidence", "review_reason",
    "mot_model", "mot_expiry_date", "mot_expired", "mot_status",
]

# UK haulage operating-class standards (payload kg, single-layer pallet spaces, body).
# Sourced 2026-06 from rinconservices.co.uk/lorry-sizes-uk, sussextransport.com payload
# guide, dsv.com trailer sizes, vansdirect Renault Master. Ranges in comments; we record
# a representative MAX payload (a capacity cap) + the typical single-layer pallet count.
CLASS_SPEC: dict[str, dict] = {
    "44 TONNE":  {"payload_kg": 28000, "pallets": 26, "body": "artic 13.6m trailer", "nominal_t": 44.0},  # 24-29t, 26 UK pallets
    "26 TONNE":  {"payload_kg": 15000, "pallets": 18, "body": "rigid 9-10m 3-axle", "nominal_t": 26.0},   # 14-15t, 16-20 pallets
    "18 TONNE":  {"payload_kg": 9500,  "pallets": 14, "body": "rigid ~7.5m 2-axle", "nominal_t": 18.0},   # up to 9t, 12-16 pallets
    "12 TONNE":  {"payload_kg": 6500,  "pallets": 12, "body": "rigid 2-axle", "nominal_t": 12.0},         # interpolated 7.5<->18
    "7.5 TONNE": {"payload_kg": 3500,  "pallets": 8,  "body": "rigid box/curtain", "nominal_t": 7.5},     # 3-4t, 6-8 pallets
    "3 TONNE":   {"payload_kg": 1300,  "pallets": 3,  "body": "van", "nominal_t": 3.5},                   # 3.5t van ~1.2-2.0t
    "VAN":       {"payload_kg": 1200,  "pallets": 3,  "body": "van", "nominal_t": 3.5},
}
VALID_CLASSES = set(CLASS_SPEC)


def _norm_reg(v) -> str:
    return str(v or "").upper().replace(" ", "").strip()


def _clean_class(v) -> str:
    c = str(v or "").strip().upper()
    return c if c in VALID_CLASSES else ""


def _fallback_class(asset_type: str, gvw: float, desc: str) -> str:
    """Operating class when jigsaw has none, from AssetType + supatrak gross weight."""
    at = (asset_type or "").lower()
    d = (desc or "").upper()
    if "canter" in d.lower() or "n75" in d.lower():
        return "7.5 TONNE"
    if "tractor" in at:
        return "44 TONNE"
    if "van" in at or (gvw and gvw <= 5.0):
        return "VAN"
    if "mini" in at:
        return "7.5 TONNE" if gvw and gvw >= 6.0 else "VAN"
    # Lorry / Rigid Truck: band by GVW
    if gvw and gvw <= 8.6:
        return "7.5 TONNE"
    if gvw and gvw <= 13.0:
        return "12 TONNE"
    if gvw and gvw <= 20.0:
        return "18 TONNE"
    return "26 TONNE"   # 26t and 29t (8-wheeler) -> 26t class


def _asset_class_ok(asset_type: str, op_class: str) -> bool:
    """AssetType is the hard guardrail: a Tractor Unit IS an artic, a Lorry is a
    rigid, a Van is a van. jigsaw's operating class is only trusted when it does not
    contradict this (it mis-groups some rigids as 44 TONNE / 7.5 TONNE)."""
    at = (asset_type or "").lower()
    if "tractor" in at:
        return op_class == "44 TONNE"
    if "van" in at:
        return op_class in ("VAN", "3 TONNE")
    if "mini" in at:
        return op_class in ("7.5 TONNE", "VAN", "3 TONNE")
    return op_class in ("7.5 TONNE", "12 TONNE", "18 TONNE", "26 TONNE")  # lorry / rigid truck


def _str(v) -> str:
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)


def _make_model(desc, jig_make) -> tuple[str, str]:
    d = " ".join(_str(desc).split())
    make = _str(jig_make).strip().title()
    if not make and d:
        make = d.split()[0].title()
    return make, d


def _mode(series: pd.Series) -> str:
    m = series.dropna().astype(str)
    m = m[m.str.strip().ne("")]
    return m.mode().iat[0] if len(m) else ""


# ---- fleet assignment: CircuitName -> (depot, kind) ------------------------
# The mapping LIVES HERE now (2026-07-13 consolidation): shared/config reads
# depot/fleet_kind/profile_* from the emitted master, and regenerating the
# master derives them from supatrak + the profiles JSON directly — no circular
# import. Replicates shared/config._load_all_depot_fleets EXACTLY.
CIRCUIT_TO_DEPOT = {
    "Duxford - Rigid":   ("CB22",    "rigid"),
    "Duxford - Artic":   ("CB22",    "tractor"),
    "Bedford - Rigid":   ("BEDFORD", "rigid"),
    "Bedford - Artic":   ("BEDFORD", "tractor"),
    "St Ives - Rigid":   ("CB22",    "rigid"),
    "St Ives - Artic":   ("CB22",    "tractor"),
    "Stoke":             ("STOKE",   "tractor"),
    "Bedford - Service": ("BEDFORD", "rigid"),
}
STOKE_RECENTLY_RELEASED = {"BX67ZFV", "BU69XGK"}
RIGID_ASSET_TYPES = {"Lorry", "Mini Truck", "Rigid Truck", "Service Van"}
TRACTOR_ASSET_TYPES = {"Tractor Unit"}


def _fleet_assignment(reg: str, circuit, asset_type) -> tuple[str, str]:
    """(depot, kind) for a fleet vehicle, or ("", "") when outside the dispatch pool."""
    c = str(circuit or "").strip()
    a = str(asset_type or "").strip()
    if not reg or "Subscription Expired" in c:
        return "", ""
    if "Recently Released" in c:
        depot = "STOKE" if reg in STOKE_RECENTLY_RELEASED else "CB22"
        if a in RIGID_ASSET_TYPES:
            return depot, "rigid"
        if a in TRACTOR_ASSET_TYPES:
            return depot, "tractor"
        return "", ""
    entry = CIRCUIT_TO_DEPOT.get(c)
    if entry is None:
        return "", ""
    depot, pool = entry
    if pool == "rigid" and a in RIGID_ASSET_TYPES:
        return depot, "rigid"
    if pool == "tractor" and a in TRACTOR_ASSET_TYPES:
        return depot, "tractor"
    return "", ""


# v1.5 fallback profiles for fleet vehicles missing from the derived JSON —
# mirrors shared/config._ASSET_TYPE_FALLBACKS (kept in sync by the byte-gate).
_PROFILE_FALLBACKS = {
    "Lorry":        {"kg": 10000, "pal": 15, "ss": "07:00", "se": "17:00", "med": 1, "mtp": 40.0},
    "Rigid Truck":  {"kg": 10000, "pal": 15, "ss": "07:00", "se": "17:00", "med": 1, "mtp": 40.0},
    "Mini Truck":   {"kg": 2500,  "pal": 8,  "ss": "07:30", "se": "16:30", "med": 2, "mtp": 60.0},
    "Tractor Unit": {"kg": 24000, "pal": 30, "ss": "06:00", "se": "18:00", "med": 1, "mtp": 30.0},
}


def _profile_fields(reg: str, asset_type: str, raw_profiles: dict,
                    master_max_t, master_typ_t) -> dict:
    """The consolidated dispatcher profile — replicates shared/config's
    _build_vehicle_profiles + _capacity_profile_fields value-for-value."""
    p = raw_profiles.get(reg)
    if p is not None:
        atype = p["asset_type"]
        fb = _PROFILE_FALLBACKS.get(atype, _PROFILE_FALLBACKS["Lorry"])
        kg_p95 = p.get("derived_capacity_kg_p95")
        pal_p95 = p.get("derived_capacity_pallets_p95")
        cap_kg, kg_src = ((int(kg_p95), "observed_p95") if kg_p95 is not None
                          else (fb["kg"], "asset_type_default"))
        cap_pal, pal_src = ((int(pal_p95), "observed_p95") if pal_p95 is not None
                            else (fb["pal"], "asset_type_default"))
        med, mtp = int(p["median_trips_per_day"]), float(p["multi_trip_day_pct"])
    else:
        atype = (asset_type or "").strip() or "Lorry"
        fb = _PROFILE_FALLBACKS.get(atype, _PROFILE_FALLBACKS["Lorry"])
        cap_kg, kg_src = fb["kg"], "asset_type_default"
        cap_pal, pal_src = fb["pal"], "asset_type_default"
        med, mtp = fb["med"], fb["mtp"]
    return {
        "profile_asset_type": atype,
        "median_trips_per_day": med, "multi_trip_day_pct": mtp,
        "capacity_kg_per_trip": cap_kg, "capacity_kg_source": kg_src,
        "capacity_pallets_per_trip": cap_pal, "capacity_pallets_source": pal_src,
        "master_max_tonnes": master_max_t, "master_typical_tonnes": master_typ_t,
    }


def _load_observed() -> dict[str, dict]:
    """Per-reg telematics-derived observed load (p95/max kg & pallets)."""
    if not PROFILES_JSON.exists():
        return {}
    raw = json.loads(PROFILES_JSON.read_text())
    out: dict[str, dict] = {}
    for reg, p in raw.items():
        out[_norm_reg(reg)] = {
            "kg_max": p.get("derived_capacity_kg_max"),
            "pal_max": p.get("derived_capacity_pallets_max"),
        }
    return out


def build() -> pd.DataFrame:
    s = pd.read_csv(SUPATRAK)
    s["reg"] = s["AssetName"].map(_norm_reg)
    s["active"] = ~s["CircuitName"].astype(str).str.contains("Subscription Expired", na=False)

    # jigsaw per-reg operating class + make (mode across all fuel transactions)
    frames = [pd.read_csv(p, low_memory=False) for p in JIGSAW if p.exists()]
    if frames:
        j = pd.concat(frames, ignore_index=True)
        j["reg"] = j["vehicleRegistration"].map(_norm_reg)
        jg = j.groupby("reg").agg(
            jig_make=("vehicleGroup1_Name", _mode),
            jig_tonnage=("vehicleGroup2_Name", _mode),
        ).reset_index()
    else:
        jg = pd.DataFrame(columns=["reg", "jig_make", "jig_tonnage"])
    m = s.merge(jg, on="reg", how="left")

    observed = _load_observed()
    raw_profiles = {}
    if PROFILES_JSON.exists():
        raw_profiles = {_norm_reg(k): v for k, v in json.loads(PROFILES_JSON.read_text()).items()}

    rows = []
    for r in m.itertuples(index=False):
        gvw = float(getattr(r, "max_tonnes", 0.0) or 0.0)
        asset_type = str(getattr(r, "AssetType", "") or "")
        jig_class = _clean_class(getattr(r, "jig_tonnage", ""))
        review_reason = ""
        if jig_class and _asset_class_ok(asset_type, jig_class):
            op_class, class_source, conf = jig_class, "jigsaw", "HIGH"
        else:
            op_class = _fallback_class(asset_type, gvw, getattr(r, "Description", ""))
            class_source, conf = "supatrak_fallback", "MEDIUM"
            if jig_class:   # jigsaw had a class but it contradicts the AssetType
                review_reason = f"jigsaw '{jig_class}' contradicts AssetType '{asset_type}'"
                conf = "REVIEW"
        # validated correction (telematics / reg-plate / human review) wins over both sources
        if r.reg in VALIDATED_CLASS:
            op_class, conf, review_reason = VALIDATED_CLASS[r.reg]   # note carried in review_reason
            class_source = "validated"
        spec = CLASS_SPEC[op_class]
        make, model = _make_model(getattr(r, "Description", ""), getattr(r, "jig_make", ""))
        payload = spec["payload_kg"]
        pallets = spec["pallets"]
        if "TRAFIC" in model.upper():
            payload, pallets = 1000, 2   # smaller 3.0t van
        # DAF LF spans 7.5t..18t -> the specific variant is ambiguous from our data.
        if not review_reason and "LF" in model.upper().split():
            review_reason = "DAF LF variant (7.5-18t) ambiguous"
            if conf == "HIGH":
                conf = "REVIEW"
        # cross-check against telematics observed load (axle weight where present);
        # flag when a vehicle demonstrably carried MORE than its assigned class allows.
        obs = observed.get(r.reg, {})
        okg, opal = obs.get("kg_max"), obs.get("pal_max")
        if okg is not None and (okg <= 0 or okg > 60000):
            okg = None   # drop telematics glitches (e.g. 5,991,730 kg) from the reference
        has_axle = str(getattr(r, "has_AxleWeight", "")).strip().upper() in ("TRUE", "1", "YES")
        # Only the AXLE-MEASURED weight is a trustworthy cross-check: pallet maxes are
        # multi-trip daily totals / double-stacks and order-derived kg is noisy, so they
        # are kept as reference columns only. Flag a vehicle that *measured* over its cap.
        if not review_reason and has_axle and okg and okg > payload * 1.05:
            review_reason = f"axle-measured {okg:.0f}kg exceeds class cap {payload}"
            if conf == "HIGH":
                conf = "REVIEW"
        fleet_reg = str(getattr(r, "AssetName", "") or "").strip()
        depot, fleet_kind = _fleet_assignment(fleet_reg, getattr(r, "CircuitName", ""), asset_type)
        _mmax = getattr(r, "max_tonnes", None)
        _mtyp = getattr(r, "typical_tonnes", None)
        _mmax = None if pd.isna(_mmax) else float(_mmax)
        _mtyp = None if pd.isna(_mtyp) else float(_mtyp)
        profile = (_profile_fields(fleet_reg, asset_type, raw_profiles, _mmax, _mtyp)
                   if depot else {})
        rows.append({
            "reg": r.reg,
            "active": bool(r.active),
            "depot": depot,
            "fleet_kind": fleet_kind,
            "fleet_reg": fleet_reg,
            **profile,
            "asset_type": str(getattr(r, "AssetType", "") or ""),
            "make": make,
            "model": model,
            "operating_class": op_class,
            "weight_metric": str(getattr(r, "metric", "") or ""),     # GVW (rigid/van) or GCW (tractor)
            "gross_tonnes": gvw,
            "payload_kg": payload,
            "pallet_capacity": pallets,
            "body_type": spec["body"],
            "fuel_type": str(getattr(r, "fuel_type", "") or ""),
            "has_odometer": bool(getattr(r, "has_Odometer", False)),
            "class_source": class_source,         # jigsaw (operating class) | supatrak_fallback
            "capacity_source": "uk_class_standard",  # payload/pallets from published class standards
            "confidence": conf,                   # HIGH = jigsaw class | MEDIUM = inferred | REVIEW = conflict
            "review_reason": review_reason,
            "observed_kg_max": okg,               # telematics observed (axle if has_axle_weight else order-derived)
            "observed_pallets_max": opal,
            "has_axle_weight": has_axle,
            "supatrak_typical_tonnes": float(getattr(r, "typical_tonnes", 0.0) or 0.0),
        })
    out = pd.DataFrame(rows).sort_values(["active", "depot", "operating_class", "reg"],
                                         ascending=[False, True, True, True])
    # join cached MOT scrape (model + expiry) if present — see scrape note in QUEST_LOG
    if MOT_RESULTS.exists():
        mot = pd.read_csv(MOT_RESULTS)
        mot["reg"] = mot["reg"].map(_norm_reg)
        keep = [c for c in ["reg", "mot_model", "mot_expiry_date", "mot_expired", "mot_status"] if c in mot.columns]
        out = out.merge(mot[keep].drop_duplicates("reg"), on="reg", how="left")
    out = out[[c for c in FINAL_COLUMNS if c in out.columns]]
    return out


def main() -> None:
    out = build()
    out.to_csv(OUT_CSV, index=False)
    act = out[out["active"]]
    print(f"vehicle master: {len(out)} vehicles ({act['active'].sum()} active) -> {OUT_CSV}")
    print("\n=== operating_class x count (active) ===")
    print(act.groupby("operating_class").agg(n=("reg", "size"),
          payload_kg=("payload_kg", "first"), pallets=("pallet_capacity", "first")).to_string())
    print("\n=== class source (active) ===")
    print(act["class_source"].value_counts().to_string())
    print("\n=== confidence (active) ===")
    print(act["confidence"].value_counts().to_string())
    nval = int((act["class_source"] == "validated").sum())
    nobs = int(act["observed_kg_max"].notna().sum())
    naxle = int(act["has_axle_weight"].sum())
    print(f"\nvalidated overrides: {nval} | observed-load available: {nobs}/{len(act)} "
          f"({naxle} with axle-weight sensor)")
    if "mot_status" in act.columns:
        print(f"MOT (active): valid {int((act['mot_status'] == 'MOTd').sum())} | "
              f"expired {int((act['mot_expired'] == 'yes').sum())} | "
              f"no record {int((act['mot_status'] == 'no_record').sum())} (HGV annual-test / cherished plates)")
    rev = act[act["confidence"] == "REVIEW"]
    if len(rev):
        print(f"\n=== {len(rev)} active vehicles still flagged REVIEW ===")
        print(rev[["reg", "model", "operating_class", "payload_kg", "pallet_capacity",
                   "observed_kg_max", "observed_pallets_max", "has_axle_weight",
                   "review_reason"]].to_string(index=False))


if __name__ == "__main__":
    main()
