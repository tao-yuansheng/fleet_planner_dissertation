"""Self-contained postcode geocoding for the freight planner.

Unlike the read-only cache lookups the pipeline used before, this resolves a
*new* postcode live against postcodes.io on a cache miss and persists it, so the
planner no longer depends on the shared cache being pre-populated by the old
pipeline. The implementation is deliberately standalone (it shares no code with
``simulation.postcode_resolver``) per the 2026-06-24 design decision; newly
geocoded postcodes are still written back to the shared ``postcode_cache.json``
so the work is reused by both pipelines.

Design notes:
  * the cache key is the compact uppercase postcode (e.g. ``CB224PS``); reads
    accept spaced or compact keys so any pre-seeded cache shape resolves;
  * network calls are gated by a UK-postcode format check, so facility codes or
    sentinel strings never reach the API;
  * unresolvable keys are cached as a VERSIONED failure marker
    (``{"failed": True, "chain": 2}``) and persisted, so known-dead keys are
    never re-queried; LEGACY plain ``None`` negatives (written by the old
    live+terminated-only chain) are retried once by the full chain when the
    network is enabled, then upgraded in place;
  * ``set_network_enabled(False)`` makes it behave as a pure cache reader (used
    by the test suite for determinism).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import requests

POSTCODES_IO_URL = "https://api.postcodes.io/postcodes/{postcode}"
POSTCODES_IO_TERMINATED_URL = "https://api.postcodes.io/terminated_postcodes/{postcode}"
POSTCODES_IO_OUTCODE_URL = "https://api.postcodes.io/outcodes/{outcode}"
POSTCODES_IO_SOURCE = "postcodes.io"
POSTCODES_IO_TERMINATED_SOURCE = "postcodes.io/terminated"
POSTCODES_IO_OUTCODE_SOURCE = "postcodes.io/outcodes"
POSTCODE_UNIT_PRECISION = "postcode_unit"
OUTCODE_DISTRICT_PRECISION = "outcode_district"
_TIMEOUT_SECONDS = 10

# Compact-form UK postcode pattern (whitespace already stripped). Gates network
# calls so FC/facility codes and test sentinels never reach postcodes.io.
_UK_POSTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}$")

# Strict structural pattern with the postcode's two parts captured separately:
# outward = 1-2 area letters + district (digit + optional digit/letter);
# inward  = exactly one digit followed by exactly two letters.
# A compact key that matches this is already structurally valid and is never
# repaired, no matter which letters/digits it contains (e.g. "SO160AS": the O
# sits in the outward AREA-letter slot and the S sits in an inward LETTER slot
# — both legitimate).
_UK_STRICT_RE = re.compile(r"^([A-Z]{1,2})([0-9][0-9A-Z]?)([0-9])([A-Z]{2})$")

# Outward-only (district) pattern for inputs with no inward part, e.g. "SG8".
_UK_DISTRICT_RE = re.compile(r"^([A-Z]{1,2})([0-9][0-9A-Z]?)$")

# Lenient pattern: same slot layout as _UK_STRICT_RE, but each slot also
# accepts the visual twin of its expected class (so a typo'd postcode still
# parses into outward/inward parts to drive positional repair). A slot that
# used its twin form is exactly where the strict pattern fails.
_UK_LENIENT_RE = re.compile(r"^([A-Z]{1,2})([0-9A-Z][0-9A-Z]?)([0-9A-Z])([A-Z0-9]{2})$")

# Visual-twin substitutions: letter -> digit for a digit slot, digit -> letter
# for a letter slot. Only characters in this mapping are ever substituted, and
# only at a position whose class violates the strict pattern.
_TO_DIGIT = {"O": "0", "I": "1", "S": "5", "B": "8"}
_TO_LETTER = {v: k for k, v in _TO_DIGIT.items()}

# Versioned failure marker for unresolvable postcodes. Chain version 2 = the
# full live -> terminated -> structural repair -> outcode-centroid chain.
# Legacy caches negative-cached unresolvable keys as plain ``null`` under the
# chain-1 (live+terminated only) resolver; those must NOT be authoritative for
# a network-enabled run — the new chain gets one shot at upgrading them (to a
# resolution or to this marker). The marker itself IS authoritative: version 2
# already tried everything, so it is never retried.
FAILED_CHAIN_VERSION = 2


def failed_entry() -> dict[str, Any]:
    """Cache entry recording that the full v2 chain failed for this key."""
    return {"failed": True, "chain": FAILED_CHAIN_VERSION}


def is_failed_entry(entry: Any) -> bool:
    """True for a versioned failure marker (authoritative negative)."""
    return isinstance(entry, dict) and bool(entry.get("failed"))

# FC / hub / facility codes that appear in Qargo postcode fields but are not valid
# UK postcodes. Each maps to its real UK postcode so geocoding and territory
# assignment resolve correctly. This is a deliberate local copy (mirrors
# simulation.postcode_resolver.FC_CODE_ALIASES) to keep the freight planner's
# geocoder self-contained; keep the two in sync when a new facility appears.
FC_CODE_ALIASES: dict[str, str] = {
    "LTN7": "MK43 9ST",   # Amazon LTN7, Bedford Commercial Park, Bedford
    "LPL2": "L33 7TJ",    # Amazon LPL2, Knowsley Industrial Park, Liverpool
    "XBH9": "BS10 7SD",   # Amazon XBH9 AFTX FC, Avonmouth, Bristol
    "CHUB": "B37 7HB",    # Palletline national hub, Starley Way, Birmingham
    "BHX4": "CV5 9DQ",    # Amazon BHX4, Lyons Park, Coventry
    "LBA4": "DN11 0BF",   # Amazon LBA4, iPort, Doncaster
    "EMA3": "NG16 3UA",   # Amazon EMA3, Eastwood, Nottingham
}

# Facility codes seen in Qargo with no confirmed UK postcode yet — they are
# *known* non-standard and intentionally left unresolved (kept raw, never sent to
# the API). Promote a code into FC_CODE_ALIASES once its postcode is established.
# Local copy of simulation.postcode_resolver.NON_STANDARD_PCS; keep in sync.
NON_STANDARD_PCS: frozenset[str] = frozenset({"XUKS", "PUKG"})

_NETWORK_ENABLED = True


def set_network_enabled(enabled: bool) -> None:
    """Enable/disable live postcodes.io lookups (cache reads always work)."""
    global _NETWORK_ENABLED
    _NETWORK_ENABLED = bool(enabled)


def network_enabled() -> bool:
    return _NETWORK_ENABLED


def postcode_key(postcode: str) -> str:
    """Compact uppercase cache key for a UK postcode-like string."""
    return re.sub(r"\s+", "", str(postcode or "").strip().upper())


def resolve_fc_alias(postcode: str) -> str:
    """Map an FC/hub facility code to its real UK postcode, else return input."""
    return FC_CODE_ALIASES.get(postcode_key(postcode), postcode)


def is_non_standard(postcode: str) -> bool:
    """True for a known facility code with no confirmed postcode (kept raw)."""
    return postcode_key(postcode) in NON_STANDARD_PCS


def coords_from_cache_entry(entry: Any) -> tuple[float, float] | None:
    """Read coordinates from any cache shape used by the project."""
    if not entry:
        return None
    if isinstance(entry, dict):
        lat, lon = entry.get("lat"), entry.get("lon")
        if lat is None or lon is None:
            return None
        return (float(lat), float(lon))
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        return (float(entry[0]), float(entry[1]))
    return None


def cache_entry(
    lat: float,
    lon: float,
    *,
    source: str = POSTCODES_IO_SOURCE,
    precision: str = POSTCODE_UNIT_PRECISION,
    postcode: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "lat": float(lat),
        "lon": float(lon),
        "source": source,
        "precision": precision,
    }
    if postcode:
        entry["postcode"] = str(postcode).strip().upper()
    return entry


def _read_cache(postcode: str, cache: dict) -> tuple[bool, tuple[float, float] | None]:
    """Return (found, coords) trying the resolved and compact key forms.

    Negative entries come in two flavours:
      * a versioned failure marker (``{"failed": True, "chain": 2}``) means the
        FULL current chain already failed for this key -> authoritative, found;
      * a LEGACY plain ``None`` was negative-cached by the old chain-1 resolver
        (live+terminated only, no repair/outcode). With the network enabled it
        is treated as a MISS so the caller re-runs the full chain and upgrades
        the entry; with the network disabled it stays a cached negative exactly
        as before.
    """
    resolved = resolve_fc_alias(postcode)
    for key in (resolved, postcode_key(resolved)):
        if key not in cache:
            continue
        entry = cache[key]
        if entry is None and _NETWORK_ENABLED:
            continue  # legacy chain-1 negative -> let the v2 chain retry it
        return True, coords_from_cache_entry(entry)
    return False, None


def _lookup(url: str, source: str) -> dict[str, Any] | None:
    try:
        response = requests.get(url, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    if not result:
        return None
    lat, lon = result.get("latitude"), result.get("longitude")
    if lat is None or lon is None:
        return None
    return cache_entry(float(lat), float(lon), source=source, postcode=result.get("postcode"))


def _lookup_outcode(outcode: str) -> dict[str, Any] | None:
    """Resolve a district (outcode) centroid, e.g. "PE19" -> its area centroid.

    Used both as a direct fallback for district-only input ("SG8") and as the
    last resort for a structurally valid but unissued unit ("PE19 0UL").
    """
    try:
        response = requests.get(POSTCODES_IO_OUTCODE_URL.format(outcode=outcode), timeout=_TIMEOUT_SECONDS)
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    if not result:
        return None
    lat, lon = result.get("latitude"), result.get("longitude")
    if lat is None or lon is None:
        return None
    return cache_entry(
        float(lat), float(lon),
        source=POSTCODES_IO_OUTCODE_SOURCE,
        precision=OUTCODE_DISTRICT_PRECISION,
        postcode=result.get("outcode"),
    )


def _structural_repairs(key: str) -> list[str]:
    """Candidate repairs of a compact postcode key using UK postcode structure.

    The strict pattern fixes which slot each character must belong to: outward
    = 1-2 area letters + district (digit + optional digit/letter); inward =
    exactly one digit then exactly two letters. If ``key`` already matches the
    strict pattern it is left alone (returns ``[]`` — nothing to repair). Only
    when it does NOT match, we reparse with the lenient pattern (which also
    accepts each slot's visual twin) and substitute the twin character back to
    its expected class ONLY at the specific position(s) that violate the
    strict slot -- never at a position that is already valid for its slot.
    """
    if _UK_STRICT_RE.match(key):
        return []  # already structurally valid -> never touched

    match = _UK_LENIENT_RE.match(key)
    if not match:
        return []  # not repairable by this scheme (wrong overall shape)

    area, district, inward_digit, inward_letters = match.groups()

    # -- outward district: last char may be a digit or a letter (e.g. "9A");
    #    the district digits before it must be digits. Only its own slot rules
    #    say a letter is acceptable there, so no twin-repair happens in area
    #    or the flexible last district slot.
    # -- inward digit slot: must be a digit; repair a twin letter -> digit.
    digit_char = inward_digit
    digit_candidates = [digit_char]
    if digit_char in _TO_DIGIT:
        digit_candidates = [_TO_DIGIT[digit_char]]

    # -- inward letter slots: each of the two chars must be a letter; repair a
    #    twin digit -> letter, position by position.
    letter_chars = list(inward_letters)
    letter_candidates: list[str] = []
    for ch in letter_chars:
        letter_candidates.append(_TO_LETTER[ch] if ch in _TO_LETTER else ch)

    repaired_inward_digit = digit_candidates[0]
    repaired_inward_letters = "".join(letter_candidates)
    candidate = f"{area}{district}{repaired_inward_digit}{repaired_inward_letters}"

    if candidate == key:
        return []  # lenient match but no slot actually violated strict -> no-op
    if not _UK_STRICT_RE.match(candidate):
        return []  # repair didn't produce a structurally valid result
    return [candidate]


def _district_from_key(key: str) -> str | None:
    """Outward/district portion of a compact key, for the outcode fallback."""
    strict = _UK_STRICT_RE.match(key)
    if strict:
        return strict.group(1) + strict.group(2)
    district_only = _UK_DISTRICT_RE.match(key)
    if district_only:
        return key
    lenient = _UK_LENIENT_RE.match(key)
    if lenient:
        return lenient.group(1) + lenient.group(2)
    return None


def geocode(postcode: str, cache: dict) -> tuple[float, float] | None:
    """Resolve a postcode from cache, then live postcodes.io, caching the result.

    FC/hub codes are resolved first. On a miss for a real UK postcode the live
    endpoint is queried, then the terminated-postcode endpoint (retired units
    whose address still receives deliveries). If the unit is still unresolved:
      * when the key is structurally INVALID (a letter sits in a digit slot or
        vice versa), structural repair substitutes ONLY the violating
        character(s) using the visual-twin mapping (O<->0, I<->1, S<->5,
        B<->8), and the repaired candidate is verified live then terminated —
        a postcode that already matches the strict UK pattern is never
        mutated;
      * when the unit is still unresolved (including a structurally valid but
        unissued unit, or a district-only input with no inward part at all),
        the outcode district centroid is used as a last resort, cached with
        precision "outcode_district".
    Every resolution is cached under the ORIGINAL (compact) key; the entry's
    ``source`` records which path resolved it.
    """
    if not postcode:
        return None
    found, coords = _read_cache(postcode, cache)
    if found:
        return coords

    key = postcode_key(resolve_fc_alias(postcode))
    if not key or key in NON_STANDARD_PCS:
        return None  # known non-standard facility code -> never geocode
    if not _NETWORK_ENABLED:
        return None

    entry: dict[str, Any] | None = None
    is_unit_shaped = bool(_UK_POSTCODE_RE.match(key)) or bool(_UK_LENIENT_RE.match(key))

    if _UK_POSTCODE_RE.match(key):
        entry = (_lookup(POSTCODES_IO_URL.format(postcode=key), POSTCODES_IO_SOURCE)
                 or _lookup(POSTCODES_IO_TERMINATED_URL.format(postcode=key), POSTCODES_IO_TERMINATED_SOURCE))

    if entry is None and is_unit_shaped:
        for candidate in _structural_repairs(key):
            found_entry = (
                _lookup(POSTCODES_IO_URL.format(postcode=candidate), POSTCODES_IO_SOURCE)
                or _lookup(POSTCODES_IO_TERMINATED_URL.format(postcode=candidate), POSTCODES_IO_TERMINATED_SOURCE)
            )
            if found_entry is not None:
                found_entry["source"] = f"{found_entry['source']} (repaired {candidate})"
                entry = found_entry
                break

    if entry is None:
        if is_unit_shaped or _UK_DISTRICT_RE.match(key):
            district = _district_from_key(key)
            if district:
                entry = _lookup_outcode(district)

    # Unresolvable -> cache the VERSIONED failure marker, never a plain None:
    # the marker persists to disk (save_cache) and is authoritative, whereas a
    # plain None would be re-queried by every future network-enabled run.
    cache[key] = entry if entry is not None else failed_entry()
    return coords_from_cache_entry(entry)


# Convenience wrappers matching the call shapes used across the pipeline.

def coords(postcode: str, cache: dict) -> tuple[float, float] | None:
    return geocode(postcode, cache)


def latlon(postcode: str, cache: dict) -> tuple[float | None, float | None]:
    found = geocode(postcode, cache)
    return (found[0], found[1]) if found is not None else (None, None)


def geocode_ok(postcode: str, cache: dict) -> bool:
    return bool(postcode) and geocode(postcode, cache) is not None


def load_cache(path: Path | str) -> dict:
    path = Path(path)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict, path: Path | str) -> None:
    """Persist entries, merged onto whatever is already on disk.

    Plain ``None`` values (legacy in-run negatives) are skipped; versioned
    failure markers (``{"failed": True, "chain": ...}``) ARE persisted so
    future runs don't re-query keys the full chain already proved dead.
    """
    path = Path(path)
    disk = load_cache(path)
    for key, value in cache.items():
        if value is not None:
            disk[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(disk, ensure_ascii=False), encoding="utf-8")
