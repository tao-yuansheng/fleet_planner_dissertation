"""Telematics-verified collection-vs-delivery leg classification (enriched dataset).

The Palletline/Hazchem "import/export" tags are information-flow signals, not
freight-direction. This determines, per order, the physical leg OUR fleet did —
COLLECTION or DELIVERY — by checking where the assigned vehicle(s) actually
stopped, using telematics ground truth, with graceful fallbacks.

Decision stack (highest confidence first), per non-full-fleet order:
  1. HIGH    telematics, ASSIGNED vehicle stopped at origin (=COLLECTION) or
             destination (=DELIVERY), within the leg's match window: +-3h for a
             collection (COLLECTION_WINDOW_MIN), +-2h for a delivery
             (DELIVERY_WINDOW_MIN), further widened by PLACEHOLDER_EXTRA when the
             anchor time is a midnight/round-hour placeholder rather than a real stamp.
  2. MEDIUM  telematics, a SUBSTITUTE fleet vehicle stopped there (reassignment).
             A substitute match at a SHARED endpoint (>= SHARED_MIN_ORDERS distinct
             orders/day there) is ignored as not order-specific.
  3. LOW     inferred, distance-to-depot AND import/export tag agree.
  4. REVIEW  inferred, distance and tag disagree (distance used; flag).
  5. UNVERIFIED / NO_TELEMATICS  cannot decide / assigned vehicle untracked.
FULL_FLEET is NOT short-circuited — it is telematics-verified like every other order,
and is asserted ONLY on TWO-END telematics evidence: an assigned vehicle at both ends
(possibly a different vehicle at each end — a hub relay is still FULL_FLEET ownership),
or a substitute at both UNIQUE (non-shared) ends. A one-end confirmation records just
that single leg, not FULL_FLEET.

Matching combines GPS distance with the ping's own reverse-geocoded postcode, and
differs by vehicle role:
  * ASSIGNED vehicle (_stopped_at): a stop counts if it is <= MATCH_RADIUS_M metres
    from the endpoint's geocoded centroid OR the ping's postcode agrees with the order
    endpoint at sector precision -- the postcode path holds EVEN when the centroid is
    far (> MATCH_RADIUS_M), because a postcode CENTROID sits 250-800m from the actual
    gate at big rural/industrial yards and the assigned vehicle's OWN reverse-geocode is
    ground truth for where it stopped (recovered WT253752, a collection 800m off centroid).
  * SUBSTITUTE vehicle (_any_fleet_at): GPS distance only, with the sector-postcode
    fallback ONLY when coords are missing (endpoint ungeocoded or a ping has no lat/lon)
    -- deliberately stricter, so mere presence at a shared yard can't fabricate a match.
The postcode comparison itself:
  * normalise + resolve FC/hub codes (LTN7 -> real postcode) first;
  * SYMMETRIC SECTOR match: both the order and telematics postcodes are truncated
    to the order's SECTOR (outward + first inward digit) and compared for equality,
    where a sector is outward+1 digit -> 4 chars for a 3-char outward (KA6 5) and
    5 for a 4-char outward (CB22 4). Symmetric equality (not a startswith prefix)
    accepts a same-sector-different-unit reverse-geocode. A flat 5-char floor
    wrongly drops short-outward (Scotland/North) sectors and hides far full-fleet
    deliveries;
  * for DEPOT-area postcodes, require a first-5 (sector) match so a stop at the
    depot doesn't masquerade as a delivery to a neighbour in the same outward.
  * STOP required (GPSSpeed < 5) so a drive-through doesn't count.

New code; imports Cambridge/pipeline helpers read-only; modifies nothing existing.
    python -m freight_planner.tools.verify_legs            # full build -> CSV
    python -m freight_planner.tools.verify_legs --sample 200
"""
from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from freight_planner.shared.paths import LOGISTICS_ROOT as BASE  # data/artifact paths unchanged

from freight_planner.tools.export_replay import _std_reg, _split_regs
from freight_planner.shared.postcode_resolver import resolve_fc_alias
from freight_planner.shared.scope import _cached_coords, classify_order, _shipment_trip_count
from freight_planner.shared.config import CB22_DEPOT_ANCHOR, BEDFORD_DEPOT_ANCHOR

QARGO_PATH = BASE / "data" / "Input" / "orders" / "qargo_20260101_to_20260131.parquet"
CACHE_PATH = BASE / "data" / "Output" / "postcode_cache.json"
JAN_PARQUET = BASE / ".cache" / "telematics_202601.parquet"
FEB_CSV = BASE / "data" / "Input" / "supatrak" / "supatrak_telematics_cleaned_20260201_to_20260228.csv"
COMBINED_PARQUET = BASE / ".cache" / "telematics_202601_202602.parquet"
OUT_CSV = BASE / "freight_planner" / "data" / "verified_legs.csv"
TELEM_COLS = ["LocalTime", "AssetName", "GPSSpeed", "Latitude", "Longitude", "Location_Postcode"]
RESOURCE_COLS = ("resource_rigid", "resource_tractor", "resource_van")

# Match windows, empirically tuned (planning_agent offset study vs ACTUAL telematics
# stops): the real stop lands within ~2h of *_timestamp_local for deliveries (94% are
# real, second-precise times) and ~3h for collections (origin times are 51% planned
# round-hour slots). The old single +-8h window was ~4x too wide and a false-match
# source. *_requested_start_timestamp_local is a midnight placeholder (+10-13h off the
# stop) and is no longer used as an anchor.
DELIVERY_WINDOW_MIN = 120     # +-2h around the (actual) delivery time
COLLECTION_WINDOW_MIN = 180   # +-3h: origin times are noisier
PLACEHOLDER_EXTRA = 180       # widen by this when the anchor is a midnight/round-hour slot
STOP_SPEED_KMH = 5.0      # below this = stopped (a service stop, not a drive-through)
MIN_PREFIX = 5            # depot-area precision: depots are 4-char outwards -> 5-char sector
MIN_SECTOR_LEN = 3        # min compact UK sector kept in the index (2-char outward + 1 digit, e.g. G1 2 -> G12)
DEPOT_RADIUS_M = 350.0    # for detecting which postcode sector a depot sits in
MATCH_RADIUS_M = 500.0        # GPS distance that counts as "stopped at" an endpoint.
# 500m (not 250m): postcode-CENTROID geocodes sit 250-600m from where a truck actually
# parks at large yards/estates. Measured on the Jan+Feb rebuild, 250m rejected ~45% of
# legitimate stops (offset centroid) while everything genuinely elsewhere is >1km, so 500m
# recovers the offset parks and still rejects the far false matches.
SHARED_MIN_ORDERS = 5         # >= this many distinct orders/day at an endpoint => shared


# ── postcode helpers ──────────────────────────────────────────────────────────

def _norm(pc) -> str:
    """Compact uppercase postcode with FC/hub codes resolved to real postcodes."""
    if pc is None or (isinstance(pc, float) and pd.isna(pc)):
        return ""
    return re.sub(r"\s+", "", str(resolve_fc_alias(str(pc).strip())).upper())


def _norm_raw(pc) -> str:
    if pc is None or (isinstance(pc, float) and pd.isna(pc)):
        return ""
    return re.sub(r"\s+", "", str(pc).strip().upper())


def _outward(pc_norm: str) -> str:
    """Outward portion of a compact postcode (everything but the last 3 = inward)."""
    return pc_norm[:-3] if len(pc_norm) > 3 else pc_norm


def _sector_len(order_pc: str) -> int:
    """Compact length of a postcode SECTOR (outward + first inward digit). A UK
    sector is 4 chars for a 3-char outward (KA6 5 -> KA65) and 5 for a 4-char
    outward (CB22 4 -> CB224). A flat char-count floor wrongly drops the short-
    outward (Scotland/North) sectors and hides far full-fleet deliveries."""
    return max(MIN_SECTOR_LEN, len(_outward(order_pc)) + 1)


# ── endpoint shared-ness precompute ────────────────────────────────────────────

def build_endpoint_order_counts(df: pd.DataFrame) -> dict:
    """{(compact_postcode, iso_date): distinct-order count} for every endpoint (origin and
    destination) across the frame. An endpoint's date is that side's anchor date. Used to
    tell a shared shipper (many orders/day) from a unique customer."""
    counts: dict = {}
    seen: dict = {}
    for side in ("origin", "destination"):
        pc = df[f"{side}_postal_code"].map(_norm)
        dt = pd.to_datetime(df[f"{side}_timestamp_local"], errors="coerce").dt.date.astype(str)
        oid = df["order_id"].astype(str)
        for p, d, o in zip(pc, dt, oid):
            if not p or d == "NaT":
                continue
            key = (p, d)
            bucket = seen.setdefault(key, set())
            if o not in bucket:
                bucket.add(o)
                counts[key] = counts.get(key, 0) + 1
    return counts


def _endpoint_is_shared(pc, iso_date, counts, threshold: int = SHARED_MIN_ORDERS) -> bool:
    """True when >= threshold distinct orders use this endpoint on this date."""
    return counts.get((_norm(pc), str(iso_date)), 0) >= threshold


# ── timestamp anchors (best actual-time column + adaptive window) ───────────────

def _anchor_ts(row, side: str):
    """Best actual-time anchor for a side ('origin'/'destination'): prefer
    ``*_timestamp_local``, fall back to ``*_end_timestamp_local``; never the
    ``*_requested_start_timestamp_local`` placeholder (it sits +10-13h off the stop)."""
    for col in (f"{side}_timestamp_local", f"{side}_end_timestamp_local"):
        ts = pd.to_datetime(row.get(col), errors="coerce")
        if pd.notna(ts):
            return ts
    return pd.NaT


def _is_placeholder(ts) -> bool:
    """Midnight or an exact round-hour stamp is a planned slot, not an actual time."""
    if ts is None or pd.isna(ts):
        return True
    ts = pd.Timestamp(ts)
    return ts.minute == 0 and ts.second == 0


def _window_for(ts, base_min: int) -> int:
    """Match window (minutes): tight for a real time, widened for a placeholder slot."""
    return base_min + (PLACEHOLDER_EXTRA if _is_placeholder(ts) else 0)


def _svc_date(ts) -> str:
    """ISO date of a matched stop (or anchor); '' when unknown."""
    return "" if ts is None or pd.isna(ts) else str(pd.Timestamp(ts).date())


def _first_date_str(row, side: str) -> str:
    ts = pd.to_datetime(row.get(f"{side}_timestamp_local"), errors="coerce")
    return "" if pd.isna(ts) else str(ts.date())


# Known Qargo reg typos → the real (telematics-tracked) plate.
REG_FIXES = {"BX67ZFY": "BX67ZFV"}


def _order_regs(row) -> list[str]:
    regs: list[str] = []
    for c in RESOURCE_COLS:
        v = row.get(c)
        if v is not None and not (isinstance(v, float) and pd.isna(v)):
            regs += [REG_FIXES.get(r, r) for r in _split_regs(v)]
    return list(dict.fromkeys(r for r in regs if r))


def _haversine_km(a, b, c, d) -> float:
    p = math.pi / 180
    return 2 * 6371 * math.asin(math.sqrt(
        math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2))


def _within_m(lat1, lon1, lat2, lon2, radius_m: float) -> bool:
    """True when the two points are within radius_m metres (haversine)."""
    if lat1 is None or lat2 is None:
        return False
    if any(pd.isna(v) for v in (lat1, lon1, lat2, lon2)):
        return False
    return _haversine_km(lat1, lon1, lat2, lon2) * 1000.0 <= radius_m


def _endpoint_coords(pc, cache):
    """(lat, lon) for an order endpoint postcode via the postcode cache, or None."""
    co = _cached_coords(str(resolve_fc_alias(str(pc or "").strip())), cache)
    if co is None:
        return None
    return (co["lat"], co["lon"]) if isinstance(co, dict) else (co[0], co[1])


# ── telematics loading + indexing ─────────────────────────────────────────────

def load_combined_telematics() -> pd.DataFrame:
    if not COMBINED_PARQUET.exists():
        print("  building combined Jan+Feb telematics cache ...")
        jan = pd.read_parquet(JAN_PARQUET, columns=TELEM_COLS)
        feb = pd.read_csv(FEB_CSV, usecols=TELEM_COLS)
        comb = pd.concat([jan, feb], ignore_index=True)
        comb["LocalTime"] = pd.to_datetime(comb["LocalTime"], errors="coerce")
        comb.to_parquet(COMBINED_PARQUET)
    return pd.read_parquet(COMBINED_PARQUET)


def build_indexes(telem: pd.DataFrame):
    """Return (per-vehicle stopped index, global stopped arrays, depot sectors)."""
    t = telem.dropna(subset=["LocalTime", "Latitude", "Longitude"]).copy()
    t = t[t["GPSSpeed"] < STOP_SPEED_KMH]
    t["reg"] = t["AssetName"].astype(str).map(_std_reg)
    t["pcn"] = t["Location_Postcode"].map(_norm_raw)
    t = t[t["pcn"].str.len() >= MIN_SECTOR_LEN]
    t = t.sort_values("LocalTime")
    t["lat"] = pd.to_numeric(t["Latitude"], errors="coerce")
    t["lon"] = pd.to_numeric(t["Longitude"], errors="coerce")

    vidx = {}
    for reg, g in t.groupby("reg"):
        vidx[reg] = (
            g["LocalTime"].to_numpy("datetime64[ns]"),
            g["pcn"].to_numpy(),
            g["lat"].to_numpy(dtype=float),
            g["lon"].to_numpy(dtype=float),
        )
    g_time = t["LocalTime"].to_numpy("datetime64[ns]")
    g_pcn = t["pcn"].to_numpy()
    g_reg = t["reg"].to_numpy()
    g_lat = t["lat"].to_numpy(dtype=float)
    g_lon = t["lon"].to_numpy(dtype=float)

    # depot sectors: the single DOMINANT stopped-postcode sector within
    # DEPOT_RADIUS of each depot anchor (vectorised; one sector per depot).
    lat_a = t["lat"].to_numpy(dtype=float)
    lon_a = t["lon"].to_numpy(dtype=float)
    pcn_a = t["pcn"].to_numpy()
    depot_sectors = set()
    for lat, lon in (CB22_DEPOT_ANCHOR, BEDFORD_DEPOT_ANCHOR):
        p = math.pi / 180
        dl = (lat_a - lat) * p
        dn = (lon_a - lon) * p
        dm = 2 * 6371000 * np.arcsin(np.sqrt(
            np.sin(dl / 2) ** 2 + math.cos(lat * p) * np.cos(lat_a * p) * np.sin(dn / 2) ** 2))
        near = pcn_a[dm <= DEPOT_RADIUS_M]
        sect = Counter(x[:5] for x in near if len(x) >= 5)
        if sect:
            depot_sectors.add(sect.most_common(1)[0][0])
    return vidx, (g_time, g_pcn, g_reg, g_lat, g_lon), depot_sectors


# ── matching ──────────────────────────────────────────────────────────────────

def _pc_matches(order_pc: str, telem_pc: str, depot_area: bool) -> bool:
    """Does a telematics stop postcode confirm the order's point? Sector-precision,
    symmetric: both are truncated to the order's SECTOR (outward + first inward digit)
    and compared for equality, provided the telematics code is at least sector-length.
    A depot-area point still needs a first-5 (finer) match so a depot stop can't
    masquerade as a neighbour in the same outward."""
    if not order_pc or not telem_pc:
        return False
    if depot_area:
        return len(telem_pc) >= 5 and order_pc[:5] == telem_pc[:5]
    n = _sector_len(order_pc)
    return len(telem_pc) >= n and order_pc[:n] == telem_pc[:n]


def _stopped_at(times, pcns, lats, lons, order_pc, endpoint_co, depot_area, ts, window_min):
    """Matched stop TIME if the (assigned) vehicle was stopped at the endpoint within
    window_min of ts, else None. Matches on EITHER GPS distance <= MATCH_RADIUS_M to
    endpoint_co OR a symmetric sector postcode agreement between the ping's own
    reverse-geocoded postcode and the order endpoint. The postcode path is ground truth
    for the assigned vehicle and holds even past the radius (postcode CENTROIDS sit far
    from the actual gate at big rural/industrial yards); substitutes go through
    _any_fleet_at, which stays distance-gated to avoid fabricating shared-endpoint matches."""
    if pd.isna(ts):
        return None
    t0 = np.datetime64(pd.Timestamp(ts))
    w = np.timedelta64(int(window_min), "m")
    m = (times >= t0 - w) & (times <= t0 + w)
    if not m.any():
        return None
    mt, mp, mla, mlo = times[m], pcns[m], lats[m], lons[m]
    for i in range(len(mt)):
        if endpoint_co is not None and _within_m(endpoint_co[0], endpoint_co[1],
                                                 mla[i], mlo[i], MATCH_RADIUS_M):
            return mt[i]
        # The assigned vehicle's OWN reverse-geocoded postcode agreeing with the order
        # endpoint (sector precision) is ground truth for where it stopped — accept it
        # even when the cached CENTROID is >MATCH_RADIUS_M away (big rural/industrial
        # postcodes whose centroid sits far from the actual gate). Assigned-vehicle-only:
        # _stopped_at is never called for substitutes (see _any_fleet_at, distance-gated).
        if order_pc and _pc_matches(order_pc, mp[i], depot_area):
            return mt[i]
    return None


def _any_fleet_at(garr, order_pc, endpoint_co, depot_area, ts, exclude_regs, window_min):
    """(reg, stop_time) of a non-assigned fleet vehicle stopped at the endpoint within
    window_min of ts, else (None, None). Distance-first with sector fallback, mirroring
    _stopped_at."""
    g_time, g_pcn, g_reg, g_lat, g_lon = garr
    if pd.isna(ts):
        return None, None
    t0 = np.datetime64(pd.Timestamp(ts))
    w = np.timedelta64(int(window_min), "m")
    idxs = np.where((g_time >= t0 - w) & (g_time <= t0 + w))[0]
    for i in idxs:
        if g_reg[i] in exclude_regs:
            continue
        if endpoint_co is not None and _within_m(endpoint_co[0], endpoint_co[1],
                                                 g_lat[i], g_lon[i], MATCH_RADIUS_M):
            return g_reg[i], g_time[i]
        if (endpoint_co is None or pd.isna(g_lat[i])) and order_pc and _pc_matches(
                order_pc, g_pcn[i], depot_area):
            return g_reg[i], g_time[i]
    return None, None


def _all_fleet_at(garr, order_pc, endpoint_co, depot_area, ts, exclude_regs, window_min):
    """EVERY distinct fleet vehicle stopped at the endpoint within window_min of ts.

    Same gating as _any_fleet_at (distance <= MATCH_RADIUS_M, with the sector-postcode
    fallback ONLY when coords are missing — deliberately strict so mere presence at a
    shared yard can't fabricate a match), but returns the full list instead of the
    first hit, so a caller can tell whether the SAME reg appears at two endpoints.

    Returns: list of (reg, stop_time, dist_m); dist_m is None when the match came
    from the postcode fallback (no usable coords). One entry per reg (closest ping)."""
    g_time, g_pcn, g_reg, g_lat, g_lon = garr
    if pd.isna(ts):
        return []
    t0 = np.datetime64(pd.Timestamp(ts))
    w = np.timedelta64(int(window_min), "m")
    idxs = np.where((g_time >= t0 - w) & (g_time <= t0 + w))[0]
    best: dict = {}   # reg -> (stop_time, dist_m, sort_key)
    for i in idxs:
        reg = g_reg[i]
        if reg in exclude_regs:
            continue
        dist_m = None
        matched = False
        if endpoint_co is not None and not pd.isna(g_lat[i]):
            dkm = _haversine_km(endpoint_co[0], endpoint_co[1], g_lat[i], g_lon[i])
            if dkm * 1000.0 <= MATCH_RADIUS_M:
                dist_m, matched = dkm * 1000.0, True
        if not matched and (endpoint_co is None or pd.isna(g_lat[i])) and order_pc \
                and _pc_matches(order_pc, g_pcn[i], depot_area):
            matched = True   # postcode fallback; distance unknown
        if not matched:
            continue
        key = dist_m if dist_m is not None else 1e9
        if reg not in best or key < best[reg][2]:
            best[reg] = (g_time[i], dist_m, key)
    return [(reg, v[0], v[1]) for reg, v in best.items()]


# ── per-order classification ──────────────────────────────────────────────────

def _structural_decision(flow, ships: int, powered: int, direction):
    """Single-vs-full from comma-split shipment vs powered-vehicle counts, used when
    telematics gives no direct evidence. Validated against the telematics ground truth:
    ``ships > powered`` => a single leg is ours (99.6%); ``ships == powered`` is only
    ~30% truly full, so it is NOT asserted as full here. Returns
    (leg, confidence, method) or None to defer to distance/api inference.
    ``direction`` is the best COLLECTION/DELIVERY guess (distance, then api tag)."""
    if ships > powered:                       # more shipments than vehicles -> one leg is ours
        if direction:
            return direction, "MEDIUM", "structural_single"
        return "UNVERIFIED", "LOW", "structural_single_no_dir"
    if flow == "FULL_FLEET":                  # ships<=powered, untracked: keep but LOW (30% truly full)
        return "FULL_FLEET", "LOW", "flow_full_fleet"
    return None


def classify_leg(row, cache, vidx, garr, depot_sectors, counts) -> dict:
    flow = classify_order(row)
    name = str(row.get("name") or "")
    oid = str(row.get("order_id") or "")
    base = {"order_id": oid, "order_name": name, "api_flow": flow or "UNKNOWN_FLOW", "service_date": ""}

    # NB: full-fleet is NOT short-circuited — it is telematics-checked like every other
    # order. FULL_FLEET is asserted ONLY on TWO-END telematics evidence (an assigned vehicle
    # at both ends, or a substitute at both UNIQUE ends): that is a genuine point-to-point
    # direct. One confirmed end records just that single leg (the unconfirmed leg was the
    # network/groupage portion). With no telematics, ships>powered => single leg (99.6%
    # validated); only ships<=powered raw full-fleet is kept, at LOW confidence.

    o_pc = _norm(row.get("origin_postal_code"))
    d_pc = _norm(row.get("destination_postal_code"))
    o_depot = len(o_pc) >= 5 and o_pc[:5] in depot_sectors
    d_depot = len(d_pc) >= 5 and d_pc[:5] in depot_sectors
    o_co = _endpoint_coords(row.get("origin_postal_code"), cache)
    d_co = _endpoint_coords(row.get("destination_postal_code"), cache)
    o_ts = _anchor_ts(row, "origin")
    d_ts = _anchor_ts(row, "destination")
    o_win = _window_for(o_ts, COLLECTION_WINDOW_MIN)
    d_win = _window_for(d_ts, DELIVERY_WINDOW_MIN)
    # Key the shared-ness lookup on the same field build_endpoint_order_counts keys on
    # (origin/destination_timestamp_local), NOT the match anchor o_ts/d_ts (which can be an
    # end-timestamp or placeholder-widened), so the (pc, date) lookup aligns with the counts.
    o_date = _first_date_str(row, "origin")
    d_date = _first_date_str(row, "destination")
    o_shared = _endpoint_is_shared(row.get("origin_postal_code"), o_date, counts)
    d_shared = _endpoint_is_shared(row.get("destination_postal_code"), d_date, counts)

    regs = _order_regs(row)
    tracked = [r for r in regs if r in vidx]
    ships = _shipment_trip_count(row)          # comma-split shipment ("Trip-...") count
    powered = len(regs)                         # comma-split unique powered vehicles
    struct_single = ships > powered             # validated 99.6% => a single leg is ours
    dist_pred = _distance_pred(row, cache)
    api_pred = {"PL_IMPORT": "DELIVERY", "PL_EXPORT": "COLLECTION"}.get(flow)
    direction = dist_pred or api_pred           # best COLLECTION/DELIVERY guess
    def _anc(leg):  # planned anchor date for a leg we did not date from telematics
        return _svc_date(d_ts if leg == "DELIVERY" else o_ts)

    # 1) assigned-vehicle telematics (direct observation; a tight two-end match is trusted full)
    o_hits = [t for r in tracked
              if (t := _stopped_at(*vidx[r], o_pc, o_co, o_depot, o_ts, o_win)) is not None]
    d_hits = [t for r in tracked
              if (t := _stopped_at(*vidx[r], d_pc, d_co, d_depot, d_ts, d_win)) is not None]
    at_o, at_d = bool(o_hits), bool(d_hits)
    if at_o and at_d:
        # Two ends confirmed => FULL_FLEET. NOTE: the two ends may be matched by DIFFERENT
        # assigned vehicles (one collects, one delivers) — a hub relay. That is intentional:
        # FULL_FLEET is an OWNERSHIP label (our fleet did the entire origin->dest journey, no
        # Palletline leg), not a claim of a single-vehicle non-stop run. Whether the planner
        # routes it as a straight direct or through a hub is the downstream DIRECT-vs-XDOCK
        # resolver's job, not this classifier's.
        return {**base, "leg": "FULL_FLEET", "confidence": "HIGH", "method": "telematics_assigned",
                "matched_vehicle": ",".join(tracked), "service_date": _svc_date(min(o_hits))}
    if at_o or at_d:
        # Two-end telematics is REQUIRED to assert FULL_FLEET (a genuine point-to-point
        # direct). With only ONE end confirmed we record just that leg: the unconfirmed leg
        # was the network/groupage portion (multi-vehicle consolidation + hub trunk), so
        # modelling the whole order as a direct would inflate km over the consolidated
        # reality. This holds even for a FULL_FLEET booking — the booking says we own both
        # legs, but the km-honest single-leg is what telematics can prove we ran discretely.
        leg = "COLLECTION" if at_o else "DELIVERY"
        svc = min(o_hits) if at_o else min(d_hits)
        return {**base, "leg": leg, "confidence": "HIGH", "method": "telematics_assigned",
                "matched_vehicle": ",".join(tracked), "service_date": _svc_date(svc)}

    # 2) substitute fleet vehicle — gate a two-end (full-fleet) call by the structural rule
    sub_o, t_o = _any_fleet_at(garr, o_pc, o_co, o_depot, o_ts, set(regs), o_win)
    sub_d, t_d = _any_fleet_at(garr, d_pc, d_co, d_depot, d_ts, set(regs), d_win)
    if o_shared:
        sub_o, t_o = None, None      # presence at a shared origin isn't order-specific
    if d_shared:
        sub_d, t_d = None, None      # presence at a shared destination isn't order-specific
    if sub_o or sub_d:
        if sub_o and sub_d and not struct_single:
            leg, svc, mv = "FULL_FLEET", min(t_o, t_d), f"{sub_o},{sub_d}"
        elif sub_o and sub_d:                   # both matched but structure says single
            leg = direction or "DELIVERY"
            svc, mv = (t_o, sub_o) if leg == "COLLECTION" else (t_d, sub_d)
        else:
            leg = "COLLECTION" if sub_o else "DELIVERY"
            svc, mv = (t_o, sub_o) if sub_o else (t_d, sub_d)
        return {**base, "leg": leg, "confidence": "MEDIUM", "method": "telematics_substitute",
                "matched_vehicle": mv, "service_date": _svc_date(svc)}

    no_telem = (not tracked)

    # 3) structural rule (no telematics): shipment vs powered-vehicle counts.
    #    ships>powered => single (direction from distance/api); raw full-fleet with
    #    ships<=powered kept but LOW. ships==powered is NOT asserted full (only ~30% are).
    sd = _structural_decision(flow, ships, powered, direction)
    if sd is not None:
        leg, conf, method = sd
        return {**base, "leg": leg, "confidence": conf, "method": method,
                "matched_vehicle": "", "service_date": _anc(leg) if leg in ("COLLECTION", "DELIVERY") else ""}

    # 4) inferred fallback: distance-to-depot + import/export tag
    if dist_pred and api_pred:
        if dist_pred == api_pred:
            return {**base, "leg": dist_pred, "confidence": "LOW",
                    "method": "inferred_distance_api_agree", "matched_vehicle": "", "service_date": _anc(dist_pred)}
        return {**base, "leg": dist_pred, "confidence": "REVIEW",
                "method": "inferred_distance_api_disagree", "matched_vehicle": "", "service_date": _anc(dist_pred)}
    if dist_pred:
        return {**base, "leg": dist_pred, "confidence": "REVIEW",
                "method": "inferred_distance_only", "matched_vehicle": "", "service_date": _anc(dist_pred)}
    if api_pred:
        return {**base, "leg": api_pred, "confidence": "REVIEW",
                "method": "inferred_api_only", "matched_vehicle": "", "service_date": _anc(api_pred)}

    return {**base, "leg": "UNVERIFIED",
            "confidence": "NONE", "method": "no_telematics" if no_telem else "unresolved",
            "matched_vehicle": ",".join(regs) if no_telem else ""}


def _distance_pred(row, cache) -> str | None:
    def dk(pc):
        co = _cached_coords(str(resolve_fc_alias(str(pc or "").strip())), cache)
        if co is None:
            return None
        la, lo = (co["lat"], co["lon"]) if isinstance(co, dict) else (co[0], co[1])
        return min(_haversine_km(CB22_DEPOT_ANCHOR[0], CB22_DEPOT_ANCHOR[1], la, lo),
                   _haversine_km(BEDFORD_DEPOT_ANCHOR[0], BEDFORD_DEPOT_ANCHOR[1], la, lo))
    o, d = dk(row.get("origin_postal_code")), dk(row.get("destination_postal_code"))
    if o is None or d is None:
        return None
    return "COLLECTION" if o < d else "DELIVERY"


def _eligible(df: pd.DataFrame) -> pd.DataFrame:
    """In-universe orders only: not cancelled, has a fleet vehicle, and not an
    out-of-scope service line (Specialist Movement / Crane Hire) — matching the
    dispatch pipeline's classify_order scope rule."""
    status = df["status"].astype(str).str.upper()
    transport = df["transport_service"].astype(str).fillna("")
    out_of_scope_service = transport.str.contains("Specialist Movement|Crane Hire", regex=True)
    keep = (
        (status != "CANCELLED")
        & (~out_of_scope_service)
        & df.apply(lambda r: len(_order_regs(r)) > 0, axis=1)
    )
    return df[keep].copy()


RECOVERY_CSV = BASE / "freight_planner" / "data" / "no_resources_recovery.csv"


def _no_resources_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """NO_RESOURCES orders the universe currently drops: no powered fleet vehicle,
    not cancelled, not a Specialist/Crane service line. These are the recovery pool."""
    status = df["status"].astype(str).str.upper()
    transport = df["transport_service"].astype(str).fillna("")
    spec = transport.str.contains("Specialist Movement|Crane Hire", regex=True)
    noreg = df.apply(lambda r: len(_order_regs(r)) == 0, axis=1)
    return df[(status != "CANCELLED") & (~spec) & noreg].copy()


def recover_no_resources(df, cache, vidx, garr, depot_sectors, counts) -> pd.DataFrame:
    """Diagnostic: for every NO_RESOURCES order, ask whether a fleet vehicle
    demonstrably stopped near the origin (~origin_timestamp) and/or destination
    (~destination_timestamp). Reports the evidence tier, endpoint trust, and the
    flow each candidate maps to. Reads only; folds nothing into the universe."""
    cand = _no_resources_candidates(df)
    _TIER_LEG = {"same_vehicle_both": "FULL_FLEET", "any_both": "FULL_FLEET",
                 "origin_only": "COLLECTION", "dest_only": "DELIVERY", "no_match": ""}
    rows = []
    for _, row in cand.iterrows():
        o_pc, d_pc = _norm(row.get("origin_postal_code")), _norm(row.get("destination_postal_code"))
        o_depot = len(o_pc) >= 5 and o_pc[:5] in depot_sectors
        d_depot = len(d_pc) >= 5 and d_pc[:5] in depot_sectors
        o_co = _endpoint_coords(row.get("origin_postal_code"), cache)
        d_co = _endpoint_coords(row.get("destination_postal_code"), cache)
        o_ts, d_ts = _anchor_ts(row, "origin"), _anchor_ts(row, "destination")
        o_win, d_win = _window_for(o_ts, COLLECTION_WINDOW_MIN), _window_for(d_ts, DELIVERY_WINDOW_MIN)
        o_shared = _endpoint_is_shared(row.get("origin_postal_code"), _first_date_str(row, "origin"), counts)
        d_shared = _endpoint_is_shared(row.get("destination_postal_code"), _first_date_str(row, "destination"), counts)

        o_m = _all_fleet_at(garr, o_pc, o_co, o_depot, o_ts, set(), o_win)
        d_m = _all_fleet_at(garr, d_pc, d_co, d_depot, d_ts, set(), d_win)
        regs_o, regs_d = {m[0] for m in o_m}, {m[0] for m in d_m}
        same = regs_o & regs_d

        if regs_o and regs_d:
            tier = "same_vehicle_both" if same else "any_both"
        elif regs_o:
            tier = "origin_only"
        elif regs_d:
            tier = "dest_only"
        else:
            tier = "no_match"

        matched_shared = bool(regs_o and o_shared) or bool(regs_d and d_shared)
        endpoint_quality = "n/a" if tier == "no_match" else ("shared" if matched_shared else "unique")

        # Flow-aware "meaningful end": for a PL_IMPORT we deliver (destination is the
        # end that proves service); for a PL_EXPORT we collect (origin is the proof);
        # a FULL_FLEET is proven by either end. An origin hit on a PL_IMPORT is just a
        # truck at the hub/depot, so it does NOT count. `defensible` additionally
        # requires that meaningful end to be a UNIQUE (non-shared) customer address AND
        # NOT one of our own depot sectors (every fleet truck parks at the depot, so a
        # depot-endpoint match is never order-specific) — the honest recovery, stripped
        # of shared-endpoint and depot coincidence.
        o_ok = bool(regs_o) and not o_shared and not o_depot   # trustworthy origin evidence
        d_ok = bool(regs_d) and not d_shared and not d_depot   # trustworthy destination evidence
        api_flow = classify_order(row) or "UNKNOWN_FLOW"
        if api_flow == "PL_IMPORT":
            meaningful, defensible = bool(regs_d), d_ok
        elif api_flow == "PL_EXPORT":
            meaningful, defensible = bool(regs_o), o_ok
        elif api_flow == "FULL_FLEET":
            meaningful, defensible = bool(regs_o or regs_d), (o_ok or d_ok)
        else:  # UNKNOWN_FLOW — can't attribute a meaningful end
            meaningful, defensible = False, False

        def _min_dist(ms):
            ds = [m[2] for m in ms if m[2] is not None]
            return round(min(ds)) if ds else ""

        rows.append({
            "order_id": str(row.get("order_id") or ""),
            "name": str(row.get("name") or ""),
            "status": str(row.get("status") or ""),
            "api_flow": api_flow,
            "tier": tier,
            "endpoint_quality": endpoint_quality,
            "meaningful_end_matched": meaningful,
            "defensible": defensible,
            "implied_leg": _TIER_LEG[tier],
            "same_vehicle": ",".join(sorted(same)),
            "matched_regs_origin": ",".join(sorted(regs_o)),
            "matched_regs_dest": ",".join(sorted(regs_d)),
            "stop_date_origin": _svc_date(min((m[1] for m in o_m))) if o_m else "",
            "stop_date_dest": _svc_date(min((m[1] for m in d_m))) if d_m else "",
            "dist_m_origin": _min_dist(o_m),
            "dist_m_dest": _min_dist(d_m),
            "origin_pc": row.get("origin_postal_code"),
            "destination_pc": row.get("destination_postal_code"),
            "origin_shared": o_shared,
            "dest_shared": d_shared,
        })
    return pd.DataFrame(rows)


def _print_recovery_summary(rec: pd.DataFrame, n_candidates: int) -> None:
    tier_order = ["same_vehicle_both", "any_both", "origin_only", "dest_only", "no_match"]
    rec = rec.copy()
    rec["tier"] = pd.Categorical(rec["tier"], categories=tier_order, ordered=True)
    matched = rec[rec["tier"] != "no_match"]
    print("\n" + "=" * 70)
    print("NO_RESOURCES TELEMATICS RECOVERY (diagnostic — nothing folded in)")
    print(f"Candidates (no powered veh, not cancelled/specialist): {n_candidates:,}")
    print(f"Matched at >=1 end: {len(matched):,}  ({100*len(matched)/max(1,n_candidates):.1f}%)")
    print("=" * 70)

    print("\nBy evidence tier:")
    for t in tier_order:
        n = int((rec["tier"] == t).sum())
        print(f"   {t:<20}{n:>7,}  ({100*n/max(1,n_candidates):5.1f}% of candidates)")

    print("\nBy tier x endpoint trust (matched only):")
    piv = matched.pivot_table(index="tier", columns="endpoint_quality",
                              values="order_id", aggfunc="count", fill_value=0, observed=True)
    print("   " + piv.to_string().replace("\n", "\n   "))

    print("\nBy tier x api_flow (matched only):")
    piv2 = matched.pivot_table(index="tier", columns="api_flow",
                               values="order_id", aggfunc="count", fill_value=0, observed=True)
    print("   " + piv2.to_string().replace("\n", "\n   "))

    print("\n" + "-" * 70)
    print("FLOW-AWARE DEFENSIBLE RECOVERY  (the honest number)")
    print("  meaningful end per flow: PL_IMPORT=destination, PL_EXPORT=origin,")
    print("  FULL_FLEET=either; UNKNOWN excluded. 'defensible' also needs that end")
    print("  to be a UNIQUE (non-shared) address, stripping shared-endpoint noise.")
    print("-" * 70)
    meaningful = rec[rec["meaningful_end_matched"]]
    defensible = rec[rec["defensible"]]
    print(f"  meaningful-end matched (incl. shared): {len(meaningful):,}")
    print(f"  DEFENSIBLE (meaningful end, unique addr): {len(defensible):,}"
          f"  ({100*len(defensible)/max(1,n_candidates):.1f}% of candidates)")
    if len(defensible):
        print("     by flow: " + ", ".join(
            f"{k}={v}" for k, v in defensible["api_flow"].value_counts().items()))
        print("     by status: " + ", ".join(
            f"{k}={v}" for k, v in defensible["status"].value_counts().items()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="0 = all eligible orders")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--qargo", action="append", default=None,
                    help="Order parquet path(s); repeat for multiple months. "
                         "Default: January only (preserves original behaviour).")
    ap.add_argument("--recover-no-resources", action="store_true",
                    help="Diagnostic-only: probe whether a fleet vehicle stopped at the "
                         "endpoints of NO_RESOURCES orders. Writes no_resources_recovery.csv "
                         "and NEVER touches verified_legs.csv.")
    args = ap.parse_args()

    # The recovery probe is a read-only diagnostic — do NOT snapshot or write OUT_CSV.
    if not args.recover_no_resources and OUT_CSV.exists():
        baseline = OUT_CSV.with_name("verified_legs.before_gpsmatch.csv")
        if not baseline.exists():
            import shutil
            shutil.copy2(OUT_CSV, baseline)   # copy2 preserves mtime
            print(f"  snapshotted baseline -> {baseline.name}")

    print("Loading qargo + cache + telematics (Jan+Feb) ...")
    cache = json.load(open(CACHE_PATH, encoding="utf-8"))
    qargo_paths = [Path(p) for p in (args.qargo or [QARGO_PATH])]
    df = pd.concat([pd.read_parquet(p) for p in qargo_paths], ignore_index=True)
    print(f"  orders loaded: {len(df)} from {[p.name for p in qargo_paths]}")
    telem = load_combined_telematics()
    print(f"  telematics rows: {len(telem):,}; indexing stopped pings ...")
    vidx, garr, depot_sectors = build_indexes(telem)
    print(f"  tracked vehicles: {len(vidx)} | depot sectors: {sorted(depot_sectors)}")

    if args.recover_no_resources:
        # Shared-ness keyed over the FULL order set (endpoint busyness is independent
        # of whether a vehicle was recorded on any single order).
        counts = build_endpoint_order_counts(df)
        cand = _no_resources_candidates(df)
        print(f"  probing {len(cand)} NO_RESOURCES candidates ...")
        rec = recover_no_resources(df, cache, vidx, garr, depot_sectors, counts)
        rec.to_csv(RECOVERY_CSV, index=False)
        _print_recovery_summary(rec, len(cand))
        print(f"\n  -> {RECOVERY_CSV}")
        return

    elig = _eligible(df)
    if args.sample and args.sample < len(elig):
        elig = elig.sample(args.sample, random_state=args.seed)
    print(f"  classifying {len(elig)} eligible orders ...")

    counts = build_endpoint_order_counts(elig)
    rows = [classify_leg(r, cache, vidx, garr, depot_sectors, counts) for _, r in elig.iterrows()]
    out = pd.DataFrame(rows)
    out.to_csv(OUT_CSV, index=False)

    print(f"\n=== leg distribution ===\n{out['leg'].value_counts().to_string()}")
    print(f"\n=== confidence distribution ===\n{out['confidence'].value_counts().to_string()}")
    print(f"\n=== method distribution ===\n{out['method'].value_counts().to_string()}")
    tel = out['method'].str.startswith('telematics').sum()
    print(f"\n  telematics-verified: {tel} ({100*tel/len(out):.0f}%) | "
          f"inferred: {out['method'].str.startswith('inferred').sum()} | "
          f"full_fleet: {(out['method']=='flow_full_fleet').sum()} | "
          f"undecided: {(out['leg']=='UNVERIFIED').sum()}")
    print(f"\n  -> {OUT_CSV}")


if __name__ == "__main__":
    main()
