# Self-Hosted OSRM for ZEEFLEET Routing

The dispatcher's `--routing osrm` mode queries a local OSRM server. OSRM is not
bundled; stand it up once with Docker.

## One-time build (Great Britain, car profile)

```bash
mkdir osrm && cd osrm
# 1. Download a GB extract from Geofabrik
curl -O https://download.geofabrik.de/europe/great-britain-latest.osm.pbf
# 2. Pre-process (car profile bundled in the Docker image)
docker run -t -v "${PWD}:/data" osrm/osrm-backend osrm-extract -p /opt/car.lua /data/great-britain-latest.osm.pbf
docker run -t -v "${PWD}:/data" osrm/osrm-backend osrm-partition /data/great-britain-latest.osrm
docker run -t -v "${PWD}:/data" osrm/osrm-backend osrm-customize /data/great-britain-latest.osrm
```

## Run the server

```bash
docker run -t -i -p 5000:5000 -v "${PWD}:/data" osrm/osrm-backend \
  osrm-routed --algorithm mld --max-table-size 1000 /data/great-britain-latest.osrm
```

`--max-table-size 1000` raises the per-request coordinate limit so big days need
fewer chunks (the app also chunks automatically, so any value works).

Verify: `curl "http://localhost:5000/table/v1/driving/-0.12,51.5;0.16,52.1?annotations=distance,duration"`

## Use it from the dispatcher

```bash
python -m run_daily_batch --alns --budget 120 --window-hours 24 --date 2026-01-02 --fresh --routing osrm
```

Default `--osrm-url` is `http://localhost:5000`. The first run for a new set of
postcodes populates `data/Output/osrm_cache.json`; later runs reuse it. Without
`--routing osrm`, the dispatcher uses the straight-line Haversine model (default).

## Truck realism (optional)

- Quick: set `TRUCK_DURATION_FACTOR` in `routing.py` above 1.0 to scale car
  durations toward HGV speeds.
- Faithful: replace `/opt/car.lua` with a custom Lua profile that lowers max
  speeds to HGV limits (50/60 mph), then re-run extract/partition/customize.
  Full HGV restriction routing (weight/height/bridge bans) is out of scope —
  see the design spec.
