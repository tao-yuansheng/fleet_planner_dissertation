# Fleet Replay Tool — Design

**Date:** 2026-06-01
**Status:** Approved, ready for implementation plan
**Owner:** Analyst diagnostic tool, primarily for in-house investigation

## Goal

A browser-based tool that replays per-ping ZEEFleet telematics on a real map for any chosen day, with the four ZEEFleet depots and the Palletline National Hub clearly labelled. Built to support the fleet-operations mapping document and to answer "what was vehicle X doing at time Y" style questions without dropping into a notebook.

## Scope

**In scope (v1):**
- Replay one day at a time, with three slicing modes (all fleet / by circuit / single vehicle) in one Streamlit app.
- Four interactions: click moving-position dots for ping detail, click depots for arrivals/departures, scrub the timeline, drop a postcode or order pin.
- Depot labels for: Duxford HQ, Bedford, St Ives, Palletline Birmingham — read from `depot_data/depot_addresses.json`.

**Out of scope (v1):**
- Multi-day animation in a single session (pick a new date instead).
- Editing depot definitions in the UI.
- Overlaying non-ZEEFleet vehicles (no Palletline telematics available).
- Saving views or sharing URLs.
- Server deployment — local-only via `streamlit run`.
- Order-leg attribution overlays — separate tool when needed.
- Refactoring or replacing `supatrak_geographic_mapping.py`.

## Architecture

Two new files plus one dependency add:

```
BackEnd/logistics/operational_analysis/
├── supatrak_geographic_mapping.py     (existing, unchanged)
├── fleet_replay.py                    (NEW — Streamlit app: UI, caching, map composition)
└── fleet_replay_data.py               (NEW — data loaders & derivations, no Streamlit deps)
```

- `fleet_replay.py` is thin: sidebar widgets, `@st.cache_data` wrappers, folium map assembly, `st_folium` call. No data wrangling logic.
- `fleet_replay_data.py` is plain Python: telematics filtering, trace prep, depot enter/exit detection, postcode geocoding, order lookup. Importable from notebooks and unit-testable without Streamlit.

**New dependency:** `streamlit-folium` added to `requirements.txt`.

**Launch:** `streamlit run BackEnd/logistics/operational_analysis/fleet_replay.py`.

**Data sources (read-only):**
- `data/Input/supatrak/supatrak_telematics_cleaned_*.csv` — pings
- `data/Input/supatrak/supatrak_vehicle_list_enriched.csv` — vehicle metadata
- `data/Input/orders/qargo_*.xlsx` — orders (order-ID overlay only)
- `depot_data/depot_addresses.json` — depot coordinates and labels

**UI shape:**

```
┌─ Sidebar ─────────────────────┐  ┌─ Main pane ──────────────────────────┐
│ Date          [date picker]   │  │                                       │
│ Mode          (•) All fleet   │  │            FOLIUM MAP                 │
│               ( ) By circuit  │  │   — depot markers always on           │
│               ( ) Single veh  │  │   — selected vehicles' static traces  │
│ Vehicles      [picker]        │  │   — moving cursor markers at "now"    │
│ Time          [slider]        │  │   — optional order/postcode pin       │
│ Order/PC      [input] [Pin]   │  │                                       │
│ Speed         5× / 15× / 60×  │  │                                       │
│ Legend                        │  │                                       │
└───────────────────────────────┘  └───────────────────────────────────────┘
```

## Time-cursor & map composition

Slider value `t` is a timestamp in the selected day's local time. Three map layers driven by `t`:

| Layer | Lifecycle | Content |
|---|---|---|
| **Depot markers** | Constant per session | All depots from `depot_addresses.json` as styled icons. ★ for ZEEFleet, ◆ for Palletline. Rich popups (see "Depot popups" below). |
| **Vehicle traces** | Recomputed on selection or date change (cached) | Per selected vehicle: faint `PolyLine` for the day plus small clickable `CircleMarker`s at every retained ping. Popups show time / postcode / speed / ignition / driver / odometer. Faint colour so the cursor marker stands out. |
| **Current-position markers** | Recomputed per slider tick (cheap) | Per selected vehicle, the ping with the latest `LocalTime ≤ t` (binary search on pre-sorted timestamp arrays). Bold marker labelled with the vehicle name. If the latest ping is >30 min stale relative to `t`, render greyed-out with `(stale)` label. |

Map bounds auto-fit to the union of (selected vehicles' pings ∪ visible depots ∪ active order pin).

Why a manual slider instead of `TimestampedGeoJson`: native plugin animation has fixed playback controls and weak popups on moving features. Manual slider supports scrubbing, exact time landing, and rich clickable history points — which is three of the four requested interactions.

## Downsampling

Traces are capped at **1,500 retained pings per vehicle per day**:
- Douglas-Peucker line simplification on the geometry (preserves visual shape).
- Always retain every `Ignition` state change so stops/starts are never invisible.

The **current-position lookup** operates on the *full* uncapped frame, so the cursor itself is always exact to the original ping cadence.

**Open assumption:** "trace visual shape preserved + every ignition flip kept" is good enough for analyst's eye. If we later want every ping plotted, removing the simplification step is a one-line change.

## Depot popups

Each depot marker's `Popup` shows arrivals and departures during the selected date.

**Geo-fence:**
- ZEEFleet depots (Duxford CB22 4PS, Bedford MK42 0LF, St Ives PE27 3WR): 200 m radius around the depot_addresses.json coordinate.
- Palletline Birmingham (B37 7HB): 300 m radius (larger site).

These radii are conservative; we have prior evidence that >57% of Bedford-Artic stationary pings collapse onto a ~50 m square at MK42 0LF.

**Derivation algorithm** (per vehicle per date per depot):
1. Mark each ping `inside = haversine(ping, depot_centre) ≤ radius`.
2. Collapse to runs of consecutive same-state pings → each `False→True` transition is an *arrival*, each `True→False` is a *departure*.
3. Record dwell time and whether ignition was off at any point during the dwell (label drive-throughs vs real stops).

**Popup format:**

```
Bedford depot (MK42 0LF) — 2026-01-07
─────────────────────────────────────────
Vehicle    Arrived  Departed  Dwell    Stop?
HX17CUA    00:15    19:55     19h40m   ✓
W888RNW    06:06    11:32     05h26m   ✓
R888GNW    —        06:58     —        (started here)
```

`—` for visits where the arrival or departure falls outside the selected date.

## Order / postcode overlay

Sidebar input accepts either a UK postcode (`IP6 0LW`) or a qargo order ID (`WT253245`).

**Resolution:**
- Postcode → `postcodes.io` free API (no key, ~100 ms). Cache results. Fallback to free-text `lat,lon` input if the API is unreachable.
- Order ID → look up in the qargo file for the selected month. Drop *two* pins (origin + destination). Pre-populate slider to `destination_timestamp_local`.

**Visual:** distinctive icon (push-pin / magenta) with popup showing typed input + resolved postcode + lat/lon. For order ID, a thin dashed line connects origin and destination to make the direct geometry visible.

**No persistence.** Pin clears when input is cleared or the date changes.

**Failure modes:**
- Postcodes.io unreachable → sidebar `⚠ Geocoder unavailable — enter lat,lon directly`.
- Order ID not in loaded month → sidebar `⚠ Order not in YYYY-MM data — switch month`.

## Performance plan

Target: **slider tick → map update < 500 ms** with up to ~50 selected vehicles.

| Cost | Scale | Mitigation |
|---|---|---|
| Telematics CSV read | Once per session per month (~22 MB, ~600k rows) | `@st.cache_data(ttl=3600)`. Convert to parquet on first load (`.cache/telematics_YYYYMM.parquet`) → faster re-reads. |
| Per-vehicle trace prep | Once per (date, vehicle selection) | `@st.cache_data` keyed on `(date, frozenset(vehicle_names))`. 1,500-ping cap (see Downsampling). |
| Per-tick current-ping lookup | Every slider movement | `np.searchsorted` on pre-sorted per-vehicle timestamp arrays. Sub-ms per vehicle. |
| Per-tick map rebuild | Every slider movement | Folium map with ~150 markers + cached polylines: ~150–250 ms. `st_folium(returned_objects=[])` unless click events are needed for the interaction. |
| Depot enter/exit | Once per (date, depot) | Cached. ~50 ms per depot per day. |

**Hard caps:**
- Max 100 vehicles plotted at once (UI hint if more selected).
- Single-day windows only in v1.
- Auto-play step capped via 5× / 15× / 60× speed selector.

## Testing plan

**Unit tests** in `operational_analysis/tests/test_fleet_replay_data.py`:
- Arrival/departure derivation with synthetic fixtures: enters then leaves; starts inside; never enters; drives through without stopping.
- Douglas-Peucker downsampling preserves polyline shape (within tolerance) and never drops an ignition transition.
- Postcode resolver fallback when API call is mocked to fail.

**Manual smoke checks** documented at the top of `fleet_replay.py`:
- Date = 2026-01-07, vehicle = HX17CUA → verify the morning Bedford trace and the B37 7HB overnight stop both appear.
- Drop pin on `IP6 0LW`, date = 2026-01-06 → confirm pin at correct location and no ZEEFleet vehicle's cursor lands within 500 m during morning.

No browser automation in v1 — disproportionate to value.

## Risks & open questions

- **Streamlit re-execution model:** Streamlit reruns the script top-to-bottom on every widget change. With `@st.cache_data` on the heavy work this is fine, but if anything proves slower than the 500 ms target we may need to switch the slider to a `st.fragment`-scoped widget (Streamlit 1.32+) so the rest of the page isn't re-run on every tick.
- **postcodes.io rate limits:** Free tier is generous but not unlimited. Caching mitigates; explicit failure mode covers the rest.
- **Stale ping threshold (30 min):** Chosen by feel from the telematics cadence. May want to tune per vehicle type if rigids have different polling intervals than artics.

## Dependencies

- New: `streamlit-folium` (MIT, mainstream Streamlit↔Folium adapter, no native code).
- All other libraries already installed (`streamlit`, `folium`, `pandas`, `numpy`).

## Reference: existing related code

- `BackEnd/logistics/operational_analysis/supatrak_geographic_mapping.py` — existing folium-based static map pipeline (heatmap / route maps / service coverage). The trace prep and downsampling helpers there are referenced for style consistency but not reused directly.
- `BackEnd/logistics/depot_data/depot_addresses.json` — depot list, including the Palletline hub entry.
- Background context that informed this design (stored in personal memory, not in-repo): `palletline-hub-at-b37-7hb` and `zeefleet-operation-is-groupage-network`.
