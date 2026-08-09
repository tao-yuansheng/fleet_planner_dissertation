"""Postcode coordinate resolution for routing-grade dispatch inputs."""
from __future__ import annotations

import re
from typing import Any

import requests

POSTCODES_IO_URL = "https://api.postcodes.io/postcodes/{postcode}"
POSTCODES_IO_TERMINATED_URL = "https://api.postcodes.io/terminated_postcodes/{postcode}"
POSTCODES_IO_SOURCE = "postcodes.io"
POSTCODES_IO_TERMINATED_SOURCE = "postcodes.io/terminated"
POSTCODE_UNIT_PRECISION = "postcode_unit"


def postcode_key(postcode: str) -> str:
    """Return the compact uppercase cache key for a UK postcode-like string."""
    return re.sub(r"\s+", "", str(postcode or "").strip().upper())


# FC / hub / facility codes that appear in Qargo postcode fields but are not
# valid UK postcodes. Map each to its real UK postcode so geocoding and
# territory assignment resolve correctly the first time the pipeline touches a
# geocode. This is the single source of truth — scope.py and export_replay.py
# import from here; do not redefine elsewhere. Add a code when Qargo data shows
# a new non-standard facility postcode.
FC_CODE_ALIASES: dict[str, str] = {
    'LTN7': 'MK43 9ST',   # Amazon LTN7, Unit 6 Bedford Commercial Park, Bedford
    'LPL2': 'L33 7TJ',    # Amazon LPL2, Marl Road, Knowsley Industrial Park, Liverpool
    'XBH9': 'BS10 7SD',   # Amazon XBH9 AFTX FC, Panattoni Park, Avonmouth, Bristol
    'CHUB': 'B37 7HB',    # Palletline national hub, Starley Way, Birmingham
    'BHX4': 'CV5 9DQ',    # Amazon BHX4, Sayer Drive, Lyons Park, Coventry
    'LBA4': 'DN11 0BF',   # Amazon LBA4, Toronto Way, iPort, Doncaster
    'EMA3': 'NG16 3UA',   # Amazon EMA3, 10 Oyster Road, Eastwood, Nottingham
}

# Facility codes seen in Qargo with no confirmed UK postcode yet — left
# unresolved (kept raw). Promote into FC_CODE_ALIASES once the postcode is known.
NON_STANDARD_PCS: frozenset[str] = frozenset({'XUKS', 'PUKG'})


def resolve_fc_alias(postcode: str) -> str:
    """Map an FC/hub facility code to its real UK postcode, else return input.

    Matching is on the compact uppercase form so spaced/unspaced inputs both
    resolve. Returns the aliased postcode string when matched, otherwise the
    original input unchanged.
    """
    return FC_CODE_ALIASES.get(postcode_key(postcode), postcode)


def coords_from_cache_entry(entry: Any) -> tuple[float, float] | None:
    """Read coordinates from any cache shape used by the project."""
    if not entry:
        return None
    if isinstance(entry, dict):
        lat = entry.get("lat")
        lon = entry.get("lon")
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


def _lookup_postcodes_io(postcode: str) -> dict[str, Any] | None:
    """Resolve an exact UK postcode with postcodes.io."""
    key = postcode_key(postcode)
    if not key:
        return None
    try:
        response = requests.get(POSTCODES_IO_URL.format(postcode=key), timeout=10)
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
    lat = result.get("latitude")
    lon = result.get("longitude")
    if lat is None or lon is None:
        return None
    return cache_entry(
        float(lat),
        float(lon),
        postcode=result.get("postcode") or key,
    )


def _lookup_terminated(postcode: str) -> dict[str, Any] | None:
    """Resolve a *retired* UK postcode via postcodes.io terminated endpoint.

    Royal Mail retires postcode units while the physical address often still
    exists and receives deliveries. postcodes.io's main endpoint 404s these, but
    ``/terminated_postcodes/`` returns the unit's former coordinates — routing
    grade, not a coarse centroid. Used only as a fallback after the live lookup.
    """
    key = postcode_key(postcode)
    if not key:
        return None
    try:
        response = requests.get(POSTCODES_IO_TERMINATED_URL.format(postcode=key), timeout=10)
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
    lat = result.get("latitude")
    lon = result.get("longitude")
    if lat is None or lon is None:
        return None
    return cache_entry(
        float(lat),
        float(lon),
        source=POSTCODES_IO_TERMINATED_SOURCE,
        postcode=result.get("postcode") or key,
    )


def geocode_postcode(postcode: str, cache: dict) -> tuple[float, float] | None:
    """Resolve a postcode from cache or postcodes.io and persist the result.

    The cache key is the compact postcode (for example ``CB224PS``). This avoids
    duplicate spaced/unspaced entries while callers can still pass either form.
    Negative lookups are cached as ``None`` to avoid repeated network attempts
    during a run. FC/hub facility codes (e.g. ``CHUB``, ``BHX4``) are resolved to
    their real UK postcode before lookup, so callers never have to special-case
    them. A postcode that 404s on the live endpoint is retried against the
    terminated-postcode endpoint (retired units whose address still exists).
    """
    key = postcode_key(resolve_fc_alias(postcode))
    if not key:
        return None
    if key in cache:
        return coords_from_cache_entry(cache[key])

    resolved = _lookup_postcodes_io(key) or _lookup_terminated(key)
    if resolved is None:
        cache[key] = None
        return None

    cache[key] = resolved
    return coords_from_cache_entry(resolved)
