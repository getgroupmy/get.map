-- get.map: everything GET.ride and the three route servers learn from a search.
--
-- One row in map.search_events per request (any source, any endpoint), with
-- the raw answer kept so nothing is lost if a parser changes later. The
-- other tables are the same data pulled apart so it can be queried:
--
--   places               every named place seen, with lat/lng (deduplicated)
--   routes               every route returned, with its road shape
--   traffic_observations jams / traffic status, with the time they were seen
--   road_alerts          Waze incidents (accident, police, closure, hazard ...)
--   traffic_lights       traffic signals (OpenStreetMap), lat/lng
--   speed_cameras        speed cameras (OpenStreetMap), lat/lng + direction
--   route_features       which lights / cameras a route passes, in what direction
--   weather_observations weather at a point and time
--
-- Plain lat/lng columns keep this runnable on any Postgres 14+; 002 adds
-- PostGIS geometry columns and spatial indexes where PostGIS is available
-- (Supabase has it).

create schema if not exists map;

-- ---------------------------------------------------------------------------
-- Every captured request
-- ---------------------------------------------------------------------------
create table if not exists map.search_events (
    id              uuid primary key,
    source          text not null,          -- get_ride | get_route | get_route_backup | get_route_apple
    provider        text,                   -- waze | google_maps | apple_maps | osrm | ai | ...
    endpoint        text not null,          -- routes_search | places_search | places_details | geocode_reverse | jams_search | alerts_search | other
    query_text      text,                   -- what the user typed, when there was text
    request         jsonb not null default '{}'::jsonb,
    status_code     integer,
    latency_ms      integer,
    error           text,
    origin_lat      double precision,
    origin_lng      double precision,
    destination_lat double precision,
    destination_lng double precision,
    depart_at       timestamptz,
    arrive_at       timestamptz,
    client_ref      text,                   -- hashed user / session / trip id, never a raw user id
    app_version     text,
    response        jsonb,                  -- the raw answer, as received
    is_cached       boolean,                -- answered from the source's cache
    captured_at     timestamptz not null default now(),
    processed_at    timestamptz             -- set once normalised and enriched
);
create index if not exists search_events_captured_idx on map.search_events (captured_at desc);
create index if not exists search_events_source_idx on map.search_events (source, endpoint, captured_at desc);
create index if not exists search_events_unprocessed_idx on map.search_events (captured_at) where processed_at is null;

-- ---------------------------------------------------------------------------
-- Places (name + lat/lng), deduplicated by provider id, else name + ~11 m cell
-- ---------------------------------------------------------------------------
create table if not exists map.places (
    id              bigserial primary key,
    place_key       text not null unique,   -- provider:id  or  name:lat4,lng4
    provider        text,                   -- waze | google | apple | osm | text
    provider_id     text,
    kind            text,                   -- waze_venue | google_place | address | pickup | dropoff | geocode ...
    name            text,
    address         text,
    street          text,
    city            text,
    state           text,
    postal_code     text,
    country         text,
    lat             double precision not null,
    lng             double precision not null,
    details         jsonb,                  -- phone, hours, categories ... when a details lookup was made
    first_seen_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now(),
    seen_count      integer not null default 1
);
create index if not exists places_name_idx on map.places (lower(name));
create index if not exists places_latlng_idx on map.places (lat, lng);

create table if not exists map.search_event_places (
    search_event_id uuid not null references map.search_events(id) on delete cascade,
    place_id        bigint not null references map.places(id) on delete cascade,
    role            text not null,          -- result | origin | destination | reverse_geocode | details
    rank            integer,
    primary key (search_event_id, place_id, role)
);

-- ---------------------------------------------------------------------------
-- Routes
-- ---------------------------------------------------------------------------
create table if not exists map.routes (
    id                  uuid primary key,
    search_event_id     uuid not null references map.search_events(id) on delete cascade,
    provider            text,
    rank                integer not null,   -- 0 = first alternative
    name                text,
    duration_seconds    integer,
    distance_meters     integer,
    is_fastest          boolean,
    is_toll             boolean,
    toll_price          numeric(10,2),
    toll_currency       text,
    traffic_status      text,
    origin_lat          double precision,
    origin_lng          double precision,
    destination_lat     double precision,
    destination_lng     double precision,
    depart_at           timestamptz,
    polyline            jsonb,              -- [[lat, lng], ...]
    bbox                double precision[], -- south, west, north, east
    traffic_light_count integer,
    speed_camera_count  integer,
    captured_at         timestamptz not null default now()
);
create index if not exists routes_event_idx on map.routes (search_event_id);
create index if not exists routes_captured_idx on map.routes (captured_at desc);

-- ---------------------------------------------------------------------------
-- Traffic (time-stamped)
-- ---------------------------------------------------------------------------
create table if not exists map.traffic_observations (
    id              bigserial primary key,
    search_event_id uuid references map.search_events(id) on delete cascade,
    route_id        uuid references map.routes(id) on delete cascade,
    provider        text,
    kind            text not null,          -- route_status | route_jam | area_jam
    external_id     text,
    street          text,
    city            text,
    level           integer,                -- Waze 0 free flow .. 5 standstill
    severity        text,
    speed_kmh       real,
    length_meters   integer,
    delay_seconds   integer,
    status_text     text,                   -- e.g. Google's "Fastest route, the usual traffic"
    lat             double precision,       -- a representative point (jam midpoint)
    lng             double precision,
    line            jsonb,                  -- [[lat, lng], ...]
    reported_at     timestamptz,            -- the provider's own time, when given
    observed_at     timestamptz not null    -- when we saw it
);
create index if not exists traffic_observed_idx on map.traffic_observations (observed_at desc);
create index if not exists traffic_latlng_idx on map.traffic_observations (lat, lng);

create table if not exists map.road_alerts (
    id              bigserial primary key,
    search_event_id uuid references map.search_events(id) on delete cascade,
    route_id        uuid references map.routes(id) on delete cascade,
    provider        text,
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
    observed_at     timestamptz not null
);
create index if not exists road_alerts_observed_idx on map.road_alerts (observed_at desc);
create index if not exists road_alerts_type_idx on map.road_alerts (type, observed_at desc);
create index if not exists road_alerts_ext_idx on map.road_alerts (provider, external_id);

-- ---------------------------------------------------------------------------
-- Road furniture (OpenStreetMap), refreshed when a route passes nearby
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
    id              text primary key,       -- osm:node/123 | waze:<alert id>
    source          text not null,          -- osm | waze_alert | manual
    lat             double precision not null,
    lng             double precision not null,
    max_speed_kmh   integer,
    direction_deg   real,                   -- the direction of travel it enforces, 0 = north, clockwise; null = unknown / both
    direction_text  text,                   -- as tagged: 'forward', 'NE', '45', 'both' ...
    camera_type     text,                   -- maxspeed | average_speed | traffic_signals (red light) | ...
    tags            jsonb,
    first_seen_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now()
);
create index if not exists speed_cameras_latlng_idx on map.speed_cameras (lat, lng);

-- Which lights / cameras a route passes, where along it and in which direction
create table if not exists map.route_features (
    route_id            uuid not null references map.routes(id) on delete cascade,
    feature_type        text not null,      -- traffic_light | speed_camera
    feature_id          text not null,
    distance_along_m    integer,            -- from the route start
    offset_m            real,               -- how far the feature is from the road shape
    travel_bearing_deg  real,               -- the driving direction at that point
    travel_direction    text,               -- N, NE, E ... of the driving direction
    applies_to_travel   boolean,            -- camera: enforces this direction (null = unknown)
    primary key (route_id, feature_type, feature_id)
);

-- ---------------------------------------------------------------------------
-- Weather (time-stamped)
-- ---------------------------------------------------------------------------
create table if not exists map.weather_observations (
    id               bigserial primary key,
    search_event_id  uuid references map.search_events(id) on delete cascade,
    role             text,                  -- origin | destination | midpoint | place
    provider         text not null,         -- open_meteo
    lat              double precision not null,
    lng              double precision not null,
    observed_at      timestamptz not null,  -- the provider's observation (or forecast) time
    is_forecast      boolean not null default false,  -- true for a planned trip's departure hour
    captured_at      timestamptz not null default now(),
    temperature_c    real,
    apparent_temp_c  real,
    humidity_pct     real,
    precipitation_mm real,
    rain_mm          real,
    weather_code     integer,               -- WMO code
    condition        text,                  -- clear | rain | thunderstorm ...
    cloud_cover_pct  real,
    visibility_m     real,
    wind_speed_kmh   real,
    wind_gust_kmh    real,
    is_day           boolean,
    raw              jsonb
);
create index if not exists weather_observed_idx on map.weather_observations (observed_at desc);
create index if not exists weather_latlng_idx on map.weather_observations (lat, lng);

-- ---------------------------------------------------------------------------
-- Handy views
-- ---------------------------------------------------------------------------
create or replace view map.route_conditions as
select r.id as route_id, e.source, r.provider, r.name, e.captured_at,
       r.origin_lat, r.origin_lng, r.destination_lat, r.destination_lng,
       r.duration_seconds, r.distance_meters, r.traffic_status,
       r.traffic_light_count, r.speed_camera_count,
       (select count(*) from map.traffic_observations t where t.route_id = r.id and t.kind = 'route_jam') as jam_count,
       (select count(*) from map.road_alerts a where a.route_id = r.id) as alert_count,
       w.condition as weather, w.temperature_c, w.precipitation_mm
from map.routes r
join map.search_events e on e.id = r.search_event_id
left join lateral (
    select * from map.weather_observations w
    where w.search_event_id = r.search_event_id
    order by (w.role = 'origin') desc limit 1
) w on true;
