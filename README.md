# get.map

get.map captures and keeps every map search made by **GET.ride (get-ride)** and the three route servers:
**get.route** (Waze), **get.route.backup** (Google Maps) and **get.route.apple** (Apple Maps). It adds what
those answers don't include:

| You get | From | Stored in |
|---|---|---|
| Place names with latitude/longitude (searches, autocomplete, details, reverse geocode, trip start/end) | the apps' own answers, deduplicated | `map.places` (+ `search_event_places`) |
| Every route: time, distance, toll, traffic text, road shape | the route servers | `map.routes` |
| **Traffic lights** on each route, in order, with the driving direction there | OpenStreetMap (`highway=traffic_signals`) | `map.traffic_lights`, `map.route_features` |
| **Speed cameras** on each route, with the driving direction there, the direction the camera checks, whether it applies to the driver, and the limit | OpenStreetMap (`highway=speed_camera`, `type=enforcement` relations) | `map.speed_cameras`, `map.route_features` |
| **Traffic conditions with date and time**: jams on the route (level 0-5, speed, length, shape), each route's traffic text, area jams | Waze, Google | `map.traffic_observations` |
| Incidents with time: accidents, police, closures, hazards, weather hazards | Waze | `map.road_alerts` |
| **Weather with date and time** at the start, the end and the middle of long trips; the forecast hour for planned trips | Open-Meteo (free, no key) | `map.weather_observations` |
| The raw request and answer, latency, errors, a hashed user ref | everything | `map.search_events` |

The `map.route_conditions` view puts each route together with its jams, alerts, lights, cameras and weather.

```
 get-ride (Supabase geo / ai-route-proxy) ─┐
 get.route pool (Waze)                    ─┤   POST /capture     ┌─► normalise ─► Overpass (lights, cameras)
 get.route.backup (Google Maps)           ─┼──────────────────►  │               Open-Meteo (weather)
 get.route.apple (Apple Maps)             ─┘   (fire & forget)   └─► Postgres / Supabase `map` schema
                                                                     (or a local spool while the DB is down)
```

## Set up

1. **Database.** Run `db/migrations/001_map_capture.sql` on Supabase (SQL editor) or any Postgres 14+. Then
   run `003_supabase_security.sql` (Supabase only, keeps riders' data private), and optionally
   `002_postgis.sql` for spatial indexes.
2. **Service.** On the NAS, copy `deploy/nas/.env.example` to `.env`, fill it in, then run
   `docker compose up -d --build` in `deploy/nas/`. Add `map.getride.my → http://get-map:8090` to GET.route's
   Cloudflare tunnel so the Supabase functions can reach it.
3. **Connect the apps.** See [docs/INTEGRATION.md](docs/INTEGRATION.md): 2 lines per route server and about 25
   lines in get-ride's `geo` function.
4. **Backfill (optional).** `python -m getmap.backfill --source-db ...` imports what `fare_ai_responses`
   already holds.

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
-- speed cameras on routes riders searched this week, that apply to their direction
select c.lat, c.lng, c.max_speed_kmh, f.travel_direction, count(*) as times_on_route
from map.route_features f join map.speed_cameras c on c.id = f.feature_id
join map.routes r on r.id = f.route_id
where f.feature_type = 'speed_camera' and f.applies_to_travel is not false
  and r.captured_at > now() - interval '7 days'
group by 1,2,3,4 order by times_on_route desc;

-- the worst jams by hour of day (Malaysia time)
select extract(hour from observed_at at time zone 'Asia/Kuala_Lumpur') as hour, street,
       avg(level) as avg_level, count(*)
from map.traffic_observations where level is not null
group by 1, 2 order by avg_level desc limit 50;

-- trips in heavy rain vs dry: how much slower?
select w.condition, avg(r.duration_seconds::float / nullif(r.distance_meters, 0) * 1000) as sec_per_km
from map.routes r join map.weather_observations w on w.search_event_id = r.search_event_id and w.role = 'origin'
group by 1;
```

## How it behaves

- **Never in the way.** Senders queue and post in the background. get.map answers `202` and works through a
  bounded queue. If the queue is full, events are dropped and counted; the rider's request is never affected.
- **Never loses a capture to a database outage.** Failed writes go to `SPOOL_DIR` as JSON lines and are
  replayed every minute. Replays are idempotent because the event id is the key.
- **Cheap on outside services.** Overpass answers are kept per ~100 m box for a week (`OSM_REFRESH_HOURS`).
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
