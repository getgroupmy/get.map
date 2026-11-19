# get.map

get.map captures and keeps every map search made by **GET.ride (get-ride)** and the three route servers:
**get.route** (Waze), **get.route.backup** (Google Maps) and **get.route.apple** (Apple Maps). It adds what
those answers don't include:

| You get | From | Stored in |
|---|---|---|
| Place names with latitude/longitude (searches, autocomplete, details, reverse geocode, trip start/end) | the apps' own answers, deduplicated | `map.places` |
| Every route: time, distance, toll, traffic text, road shape | the route servers | `map.routes`, `map.route_shapes` |
| **Traffic lights** on each route, in order, with the driving direction there | OpenStreetMap (`highway=traffic_signals`) | `map.traffic_lights`, `map.route_features` |
| **Speed cameras** on each route, with the driving direction there, the direction the camera checks, whether it applies to the driver, and the limit | OpenStreetMap (`highway=speed_camera`, `type=enforcement` relations) | `map.speed_cameras`, `map.route_features` |
| **Traffic conditions with date and time**: jams on the route (level 0-5, speed, length, shape), each route's traffic text, area jams | Waze, Google | `map.traffic_observations` (per 15 min) |
| Incidents with time: accidents, police, closures, hazards, weather hazards | Waze | `map.road_alerts` (first / last seen) |
| **Weather with date and time** at the start, the end and the middle of long trips; the forecast hour for planned trips | Open-Meteo (free, no key) | `map.weather_observations` (per ~5 km, 15 min) |
| The request, latency, errors, a hashed user ref (+ the raw answer: failures, and a 1% sample, for 7 days) | everything | `map.search_events`, `map.raw_responses` |

The `map.route_conditions` view puts each route together with its lights, cameras and weather; `map.speed_cameras_on_routes` lists every camera on every route shape.

It runs on **one Mac mini (512 GB)**: the database is compact (~2.5 KB per fare quote), split into weekly partitions, and a disk guard drops the oldest weeks before the disk can fill. See [docs/STORAGE.md](docs/STORAGE.md).

```
 get-ride (Supabase geo / ai-route-proxy) ─┐
 get.route pool (Waze)                    ─┤   POST /capture     ┌─► normalise ─► Overpass (lights, cameras)
 get.route.backup (Google Maps)           ─┼──────────────────►  │               Open-Meteo (weather)
 get.route.apple (Apple Maps)             ─┘   (fire & forget)   └─► Postgres 17 `map` schema
                                                                     on the Mac mini (or a local spool while the DB is down)
```

## Data only, in a local database

Everything is stored as **data** (text, numbers, coordinates and times) in **one Postgres database on the Mac
mini**. There's no cloud database, and get.map saves no files except while the database is down.

- **No map images.** get.map never downloads or saves map tiles, screenshots, static maps, Street View or
  photos. A road shape is a list of coordinates (an encoded polyline), not a picture.
- **Photos and icons are removed.** Some answers carry photo links and references, venue images and icons.
  `normalize.strip_images` removes those fields before anything is stored, including from the sampled raw
  answers.
- **What gets drawn on a map** (routes, lights, cameras, jams) is rebuilt from the coordinates when you need
  it, for example in QGIS, or on the admin map from `map.route_shapes.polyline`.
- **The only files** are `SPOOL_DIR` (JSON text, held while the database is unreachable and then moved into
  it) and the nightly backup dump on the NAS.

## Set up (Mac mini)

1. **Mac mini:** follow [deploy/macmini/README.md](deploy/macmini/README.md). `setup.sh` installs Postgres
   17, applies the migrations and starts get.map plus a nightly backup to the NAS.
2. **Connect the apps:** see [docs/INTEGRATION.md](docs/INTEGRATION.md). Each route server needs 2 lines,
   and get-ride's `geo` function needs about 25.
3. **Backfill (optional):** `python -m getmap.backfill --source-db ...` imports what `fare_ai_responses`
   already holds.

(`deploy/nas/` runs get.map as a container on the NAS instead; `003_supabase_security.sql` applies only if
you put the `map` schema in Supabase.)

## API

| | |
|---|---|
| `POST /capture` | One event, or `{"events": [...]}` (up to 500). `X-Capture-Key`. Returns `202` at once. |
| `GET /tap/<source>/<path>` | Transparent proxy to a route server that records the answer (no code change needed in that server). |
| `GET /health` | Always open. With the key it also shows queue, processed, dropped, enrichment errors and store state. |

Event fields: `source` (`get_ride`, `get_route`, `get_route_backup` or `get_route_apple`), `endpoint`
(`routes_search`, `places_search`, `places_details`, `geocode_reverse`, `jams_search`, `alerts_search` or
`other`), `request`, `response`, and optionally `provider`, `status_code`, `latency_ms`, `error`,
`client_ref`, `app_version` and `captured_at`.

## Example queries

```sql
-- speed cameras on routes quoted this week that check the rider's direction, busiest first
select c.camera_id, c.lat, c.lng, c.max_speed_kmh, c.travel_direction, count(*) as quotes
from map.routes r join map.speed_cameras_on_routes c on c.shape_id = r.shape_id
where r.captured_at > now() - interval '7 days' and c.applies_to_travel is not false
group by 1,2,3,4,5 order by quotes desc;

-- the worst jams by hour of day (Malaysia time)
select extract(hour from observed_at at time zone 'Asia/Kuala_Lumpur') as hour, street,
       avg(level) as avg_level, count(*)
from map.traffic_observations where level is not null
group by 1, 2 order by avg_level desc limit 50;

-- how much slower trips are in rain
select weather, avg(duration_seconds::float / nullif(distance_meters, 0) * 1000) as sec_per_km, count(*)
from map.route_conditions where captured_at > now() - interval '30 days'
group by 1 order by 2 desc;

-- the places riders search for most
select name, address, lat, lng, seen_count from map.places order by seen_count desc limit 50;
```

## How it behaves

- **Never in the way.** Senders queue and post in the background. get.map answers `202` and works through a
  bounded queue. If the queue is full, events are dropped and counted; the rider's request is never affected.
- **Never loses a capture to a database outage.** Failed writes go to `SPOOL_DIR` as JSON lines and are
  replayed every minute. Replays are idempotent because the event id is the key.
- **Fits one disk.** Weekly partitions, shapes / weather / jams / alerts stored once, raw answers sampled;
  retention per kind of data and an hourly disk guard (`MAX_DB_GB`, `MIN_FREE_DISK_GB`).
- **Cheap on outside services.** Lights and cameras are looked up once per road shape (again after 30
  days); Overpass answers are kept per ~100 m box for a week (`OSM_REFRESH_HOURS`).
  Weather is kept per ~5 km cell for 15 minutes. Long routes are split into 8 km boxes.
- **Camera direction.** A camera "applies" when the direction it checks is within 60° of the driving
  direction there (`CAMERA_ANGLE_TOLERANCE`). An enforcement relation's `from → device` gives the checked
  direction. A numeric `direction` tag is read as the direction of travel. Set
  `CAMERA_DIRECTION_MEANS=facing` if local mappers tag where the lens points. If there is no direction, the
  camera is still listed on the route with `applies_to_travel = null`.

## Development

```bash
pip install -r requirements-dev.txt
pytest                                   # unit tests
TEST_DATABASE_URL=postgresql://... pytest # plus the end-to-end Postgres tests
uvicorn getmap.server:app --port 8090
```

The tests use a real Waze answer (London to Heathrow, from get.route's fixtures), synthetic Overpass data laid
on that route, and an Open-Meteo answer.
