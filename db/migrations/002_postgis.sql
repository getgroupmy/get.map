-- Optional: PostGIS geometry columns and spatial indexes (Supabase: Database ›
-- Extensions › postgis). Run after 001. Everything in get.map works without it;
-- with it, "what's within 200 m of here" queries become fast.

-- On Supabase PostGIS lives in the `extensions` schema; run this with the
-- default search_path (which includes it).
create extension if not exists postgis;

alter table map.places               add column if not exists geom geography(Point, 4326)
    generated always as (st_setsrid(st_makepoint(lng, lat), 4326)::geography) stored;
alter table map.traffic_lights       add column if not exists geom geography(Point, 4326)
    generated always as (st_setsrid(st_makepoint(lng, lat), 4326)::geography) stored;
alter table map.speed_cameras        add column if not exists geom geography(Point, 4326)
    generated always as (st_setsrid(st_makepoint(lng, lat), 4326)::geography) stored;
alter table map.road_alerts          add column if not exists geom geography(Point, 4326)
    generated always as (case when lat is not null and lng is not null
                              then st_setsrid(st_makepoint(lng, lat), 4326)::geography end) stored;
alter table map.traffic_observations add column if not exists geom geography(Point, 4326)
    generated always as (case when lat is not null and lng is not null
                              then st_setsrid(st_makepoint(lng, lat), 4326)::geography end) stored;
alter table map.weather_observations add column if not exists geom geography(Point, 4326)
    generated always as (st_setsrid(st_makepoint(lng, lat), 4326)::geography) stored;

create index if not exists places_geom_gix          on map.places using gist (geom);
create index if not exists traffic_lights_geom_gix  on map.traffic_lights using gist (geom);
create index if not exists speed_cameras_geom_gix   on map.speed_cameras using gist (geom);
create index if not exists road_alerts_geom_gix     on map.road_alerts using gist (geom);
create index if not exists traffic_obs_geom_gix     on map.traffic_observations using gist (geom);
create index if not exists weather_geom_gix         on map.weather_observations using gist (geom);

-- Route shapes as lines (polyline is [[lat, lng], ...])
alter table map.routes add column if not exists geom geography(LineString, 4326);
create index if not exists routes_geom_gix on map.routes using gist (geom);

create or replace function map.routes_set_geom() returns trigger language plpgsql
    set search_path = map, extensions, public as $$
begin
    if new.polyline is not null and jsonb_array_length(new.polyline) >= 2 then
        select st_makeline(array_agg(st_setsrid(st_makepoint((p->>1)::float8, (p->>0)::float8), 4326) order by i))::geography
          into new.geom
          from jsonb_array_elements(new.polyline) with ordinality as t(p, i);
    end if;
    return new;
end $$;

drop trigger if exists routes_set_geom on map.routes;
create trigger routes_set_geom before insert or update of polyline on map.routes
    for each row execute function map.routes_set_geom();
