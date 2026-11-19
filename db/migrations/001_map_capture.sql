-- get.map: everything GET.ride and the three route servers learn from a search.
--
-- Built to fit a single machine (a Mac mini with a 512 GB disk):
--   * the four high-volume tables are split into weekly partitions, so old
--     weeks are dropped instantly (getmap.retention) instead of deleted row by row;
--   * a route's road shape is stored once (route_shapes, encoded polyline,
--     ~2 KB) however many times the same trip is quoted, and its traffic
--     lights / speed cameras are worked out once per shape;
--   * weather is stored once per ~5 km cell and 15 minutes; a jam once per
--     15 minutes; a Waze alert once (first / last seen); places once;
--   * the raw answers sit in their own table, kept a few days (or sampled).
--
--   search_events        one row per request (any source, any endpoint)
--   raw_responses        the answer as received (short retention / sampled)
--   places               every named place seen, with lat/lng
--   routes               every route returned: time, distance, toll, traffic text
--   route_shapes         each distinct road shape, with its light / camera counts
--   route_features       the lights / cameras on a shape, in order, with direction
--   traffic_lights       traffic signals (OpenStreetMap)
--   speed_cameras        speed cameras (OpenStreetMap): lat/lng, limit, direction
--   traffic_observations jams, per 15 minutes (level, speed, length, shape)
--   road_alerts          Waze incidents: accident, police, closure, hazard ...
--   weather_observations weather per ~5 km cell and time

create schema if not exists map;

-- ---------------------------------------------------------------------------
-- Weekly partitions (UTC Monday 00:00). map.ensure_week(ts) creates the week
-- holding ts for every partitioned table; get.map calls it before writing.
-- ---------------------------------------------------------------------------
create or replace function map.ensure_week(ts timestamptz) returns date
language plpgsql as $$
declare
    week_start date := date_trunc('week', ts at time zone 'UTC')::date;
    t text;
begin
    perform pg_advisory_xact_lock(hashtext('map.ensure_week'));
    foreach t in array array['search_events', 'raw_responses', 'routes', 'traffic_observations'] loop
        execute format(
            'create table if not exists map.%I partition of map.%I for values from (%L) to (%L)',
            t || '_w' || to_char(week_start, 'YYYYMMDD'), t,
            week_start::timestamp at time zone 'UTC', (week_start + 7)::timestamp at time zone 'UTC');
    end loop;
    return week_start;
end $$;

-- The weekly partitions that exist, oldest first.
create or replace view map.partitions as
select parent.relname as table_name, child.relname as partition_name,
       to_date(right(child.relname, 8), 'YYYYMMDD') as week_start,
       pg_total_relation_size(child.oid) as bytes
from pg_inherits i
join pg_class child on child.oid = i.inhrelid
join pg_class parent on parent.oid = i.inhparent
join pg_namespace n on n.oid = parent.relnamespace
where n.nspname = 'map' and parent.relkind = 'p' and child.relkind = 'r';

-- ---------------------------------------------------------------------------
-- Places, deduplicated by provider id, else name + ~11 m cell
-- ---------------------------------------------------------------------------
create table if not exists map.places (
    id              bigserial primary key,
    place_key       text not null unique,   -- provider:id  or  name:lat4,lng4
    provider        text,                   -- waze | google | apple | osm | nas ...
    provider_id     text,
    kind            text,                   -- waze_venue | google_place | address | origin | destination | geocode ...
    name            text,
    address         text,
    street          text,
    city            text,
    state           text,
    postal_code     text,
    country         text,
    lat             double precision not null,
    lng             double precision not null,
    details         jsonb,                  -- phone, hours, categories ... from a details lookup
    first_seen_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now(),
    seen_count      integer not null default 1
);
create index if not exists places_name_idx on map.places (lower(name));
create index if not exists places_latlng_idx on map.places (lat, lng);

-- ---------------------------------------------------------------------------
-- Weather, once per ~5 km cell (lat/lng x 20, rounded) and observation time
-- ---------------------------------------------------------------------------
create table if not exists map.weather_observations (
    id               bigserial primary key,
    cell_y           integer not null,      -- round(lat * 20)
    cell_x           integer not null,      -- round(lng * 20)
    observed_at      timestamptz not null,  -- the provider's observation (or forecast) time
    is_forecast      boolean not null default false,
    provider         text not null,         -- open_meteo
    lat              double precision not null,
    lng              double precision not null,
    temperature_c    real,
    apparent_temp_c  real,
    humidity_pct     real,
    precipitation_mm real,
    rain_mm          real,
    weather_code     smallint,              -- WMO code
    condition        text,                  -- clear | partly_cloudy | rain | heavy_rain | thunderstorm ...
    cloud_cover_pct  real,
    visibility_m     real,
    wind_speed_kmh   real,
    wind_gust_kmh    real,
    is_day           boolean,
    captured_at      timestamptz not null default now(),
    unique (cell_y, cell_x, observed_at, is_forecast)
);
create index if not exists weather_observed_idx on map.weather_observations (observed_at desc);

-- ---------------------------------------------------------------------------
-- Every captured request (weekly partitions)
-- ---------------------------------------------------------------------------
create table if not exists map.search_events (
    id                     uuid not null,
    captured_at            timestamptz not null,
    source                 text not null,   -- get_ride | get_route | get_route_backup | get_route_apple
    provider               text,            -- waze | google_maps | apple_maps | google | nas | ...
    endpoint               text not null,   -- routes_search | places_search | places_details | geocode_reverse | jams_search | alerts_search | other
    query_text             text,            -- what the rider typed
    request                jsonb,
    status_code            smallint,
    latency_ms             integer,
    error                  text,
    origin_lat             double precision,
    origin_lng             double precision,
    destination_lat        double precision,
    destination_lng        double precision,
    depart_at              timestamptz,
    arrive_at              timestamptz,
    client_ref             text,            -- hashed user id, never a raw one
    app_version            text,
    is_cached              boolean,
    origin_place_id        bigint,
    destination_place_id   bigint,
    place_ids              bigint[],        -- search results, in rank order
    origin_weather_id      bigint,
    destination_weather_id bigint,
    midpoint_weather_id    bigint,
    has_raw                boolean not null default false,
    primary key (captured_at, id)
) partition by range (captured_at);
create index if not exists search_events_source_idx on map.search_events (source, endpoint, captured_at);
create index if not exists search_events_client_idx on map.search_events (client_ref, captured_at) where client_ref is not null;

create table if not exists map.raw_responses (
    captured_at     timestamptz not null,
    search_event_id uuid not null,
    response        jsonb,
    primary key (captured_at, search_event_id)
) partition by range (captured_at);

-- ---------------------------------------------------------------------------
-- Road shapes, stored once; their lights and cameras worked out once
-- ---------------------------------------------------------------------------
create table if not exists map.route_shapes (
    id                  bigserial primary key,
    shape_hash          text not null unique,    -- md5 of the encoded polyline
    polyline            text not null,           -- Google encoded polyline, precision 5
    point_count         integer not null,
    length_m            integer,
    start_lat           double precision,
    start_lng           double precision,
    end_lat             double precision,
    end_lng             double precision,
    bbox                real[],                  -- south, west, north, east
    traffic_light_count integer,                 -- null until checked
    speed_camera_count  integer,
    features_checked_at timestamptz,
    -- the lights / cameras on this shape, in order along it (see the
    -- map.route_features view): [["L"|"C", feature id, metres along,
    -- metres off the road, driving bearing, driving direction, camera applies]]
    features            jsonb,
    first_seen_at       timestamptz not null default now(),
    last_seen_at        timestamptz not null default now(),
    seen_count          integer not null default 1
);
create index if not exists route_shapes_last_seen_idx on map.route_shapes (last_seen_at);

create table if not exists map.routes (
    id                uuid not null,
    captured_at       timestamptz not null,
    search_event_id   uuid not null,
    provider          text,
    rank              smallint not null,     -- 0 = first alternative
    name              text,
    duration_seconds  integer,
    distance_meters   integer,
    is_fastest        boolean,
    is_toll           boolean,
    toll_price        numeric(10,2),
    toll_currency     text,
    traffic_status    text,                  -- e.g. Google's "Fastest route, the usual traffic"
    depart_at         timestamptz,
    shape_id          bigint,                -- map.route_shapes
    primary key (captured_at, id)
) partition by range (captured_at);
create index if not exists routes_event_idx on map.routes (search_event_id);
create index if not exists routes_shape_idx on map.routes (shape_id);

-- ---------------------------------------------------------------------------
-- Road furniture (OpenStreetMap)
-- ---------------------------------------------------------------------------
create table if not exists map.traffic_lights (
    id              text primary key,       -- osm:node/123
    lat             double precision not null,
    lng             double precision not null,
    tags            jsonb,
    first_seen_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now()
);
create index if not exists traffic_lights_latlng_idx on map.traffic_lights (lat, lng);

create table if not exists map.speed_cameras (
    id              text primary key,       -- osm:node/123 | manual:...
    source          text not null,          -- osm | manual | official
    lat             double precision not null,
    lng             double precision not null,
    max_speed_kmh   integer,
    direction_deg   real,                   -- the direction of travel it checks, 0 = north, clockwise; null = unknown / both
    direction_text  text,                   -- as tagged: 'forward', 'NE', '45', 'from relation' ...
    camera_type     text,                   -- maxspeed | average_speed | traffic_signals (red light) ...
    tags            jsonb,
    first_seen_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now()
);
create index if not exists speed_cameras_latlng_idx on map.speed_cameras (lat, lng);


-- ---------------------------------------------------------------------------
-- Traffic (weekly partitions): one row per jam per 15 minutes
-- ---------------------------------------------------------------------------
create table if not exists map.traffic_observations (
    observed_at     timestamptz not null,   -- 15-minute bucket
    id              bigint generated always as identity,
    provider        text not null,
    kind            text not null,          -- route_jam | area_jam
    external_id     text,
    street          text,
    city            text,
    level           smallint,               -- Waze 0 free flow .. 5 standstill
    severity        text,
    speed_kmh       real,
    length_meters   integer,
    delay_seconds   integer,
    lat             double precision,       -- the jam's midpoint
    lng             double precision,
    line            text,                   -- encoded polyline
    reported_at     timestamptz,            -- the provider's own update time
    last_seen_at    timestamptz not null,
    seen_count      integer not null default 1,
    primary key (observed_at, id)
) partition by range (observed_at);
create unique index if not exists traffic_obs_jam_uq on map.traffic_observations (observed_at, provider, external_id);
create index if not exists traffic_obs_latlng_idx on map.traffic_observations (lat, lng);

-- Waze incidents: one row per alert, first / last time seen
create table if not exists map.road_alerts (
    id              bigserial primary key,
    provider        text not null,
    external_id     text,
    type            text,                   -- ACCIDENT | POLICE | ROAD_CLOSED | HAZARD | JAM | WEATHERHAZARD | CONSTRUCTION | MISC
    subtype         text,
    description     text,
    street          text,
    city            text,
    lat             double precision,
    lng             double precision,
    thumbs_up       integer,
    reported_at     timestamptz,
    first_seen_at   timestamptz not null,
    last_seen_at    timestamptz not null,
    seen_count      integer not null default 1
);
create unique index if not exists road_alerts_ext_uq on map.road_alerts (provider, external_id);
create index if not exists road_alerts_type_idx on map.road_alerts (type, last_seen_at desc);
create index if not exists road_alerts_latlng_idx on map.road_alerts (lat, lng);

-- ---------------------------------------------------------------------------
-- Handy views
-- ---------------------------------------------------------------------------
create or replace view map.route_conditions as
select r.id as route_id, e.source, r.provider, r.name, r.captured_at,
       e.origin_lat, e.origin_lng, e.destination_lat, e.destination_lng,
       r.duration_seconds, r.distance_meters, r.is_toll, r.toll_price, r.traffic_status,
       s.traffic_light_count, s.speed_camera_count, s.length_m as shape_length_m,
       w.condition as weather, w.temperature_c, w.precipitation_mm, w.observed_at as weather_at
from map.routes r
join map.search_events e on e.id = r.search_event_id and e.captured_at = r.captured_at
left join map.route_shapes s on s.id = r.shape_id
left join map.weather_observations w on w.id = e.origin_weather_id;

-- One row per light / camera on a shape (expanded from route_shapes.features,
-- which keeps them compact: one compressed value per shape instead of ~25 rows).
create or replace view map.route_features as
select s.id as shape_id,
       case f->>0 when 'L' then 'traffic_light' else 'speed_camera' end as feature_type,
       f->>1 as feature_id,
       (f->>2)::integer as distance_along_m,
       (f->>3)::real as offset_m,
       (f->>4)::real as travel_bearing_deg,
       f->>5 as travel_direction,
       (f->>6)::boolean as applies_to_travel
from map.route_shapes s, jsonb_array_elements(s.features) f
where s.features is not null;

create or replace view map.speed_cameras_on_routes as
select s.id as shape_id, f.distance_along_m, f.travel_direction, f.applies_to_travel,
       c.id as camera_id, c.lat, c.lng, c.max_speed_kmh, c.camera_type, c.direction_deg
from map.route_features f
join map.route_shapes s on s.id = f.shape_id
join map.speed_cameras c on c.id = f.feature_id
where f.feature_type = 'speed_camera';
