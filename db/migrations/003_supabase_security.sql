-- Only if the map schema lives in Supabase (not needed on the Mac mini): keep the map schema private. Riders' searches are personal
-- data (locations, trip times), so no anon / authenticated access; get.map
-- writes with its own database role or the service role, and admin screens
-- read through a server-side function.

alter table map.search_events        enable row level security;
alter table map.places               enable row level security;
alter table map.raw_responses        enable row level security;
alter table map.routes               enable row level security;
alter table map.traffic_observations enable row level security;
alter table map.road_alerts          enable row level security;
alter table map.traffic_lights       enable row level security;
alter table map.speed_cameras        enable row level security;
alter table map.route_shapes         enable row level security;
alter table map.weather_observations enable row level security;

revoke all on schema map from anon, authenticated;
revoke all on all tables in schema map from anon, authenticated;
