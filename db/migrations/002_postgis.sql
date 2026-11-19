-- Optional: PostGIS geometry columns and spatial indexes (Supabase: Database ›
-- Extensions › postgis; on the Mac mini: brew install postgis). Run after 001. Everything in get.map works without it;
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

create index if not exists places_geom_gix          on map.places using gist (geom);
create index if not exists traffic_lights_geom_gix  on map.traffic_lights using gist (geom);
create index if not exists speed_cameras_geom_gix   on map.speed_cameras using gist (geom);
create index if not exists road_alerts_geom_gix     on map.road_alerts using gist (geom);
create index if not exists traffic_obs_geom_gix     on map.traffic_observations using gist (geom);

-- Road shapes as lines (route_shapes.polyline is a Google encoded polyline)
alter table map.route_shapes add column if not exists geom geography(LineString, 4326);
create index if not exists route_shapes_geom_gix on map.route_shapes using gist (geom);

create or replace function map.route_shapes_set_geom() returns trigger language plpgsql
    set search_path = map, extensions, public as $$
begin
    new.geom := st_linefromencodedpolyline(new.polyline, 5)::geography;
    return new;
end $$;

drop trigger if exists route_shapes_set_geom on map.route_shapes;
create trigger route_shapes_set_geom before insert or update of polyline on map.route_shapes
    for each row execute function map.route_shapes_set_geom();
update map.route_shapes set polyline = polyline where geom is null;
