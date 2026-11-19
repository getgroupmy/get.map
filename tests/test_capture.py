import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from conftest import load
from getmap import config, geo
from getmap.enrich import osm, weather
from getmap.normalize import normalize
from getmap.pipeline import Pipeline
from getmap.store import PostgresStore, SpoolStore


# ---- helpers ---------------------------------------------------------------------------

def overpass_on(line):
    """A synthetic Overpass answer built on a real route shape:
    a light on the road, a camera enforcing the driver's direction, one
    enforcing the other direction, one too far away, and an enforcement
    relation whose from -> device runs with the route."""
    def off(p, m_north):
        return p[0] + m_north / 111_320, p[1]
    a, b = line[40], line[41]
    travel = geo.bearing_deg(a, b)
    c1, c2 = line[120], line[121]
    t2 = geo.bearing_deg(c1, c2)
    d0, d1 = line[200], line[201]
    far = off(line[260], 400)
    rel_from, rel_dev = line[300], line[302]
    return {"elements": [
        {"type": "node", "id": 1, "lat": off(a, 3)[0], "lon": a[1], "tags": {"highway": "traffic_signals"}},
        {"type": "node", "id": 2, "lat": c1[0], "lon": c1[1],
         "tags": {"highway": "speed_camera", "maxspeed": "40 mph", "direction": str(round(t2))}},
        {"type": "node", "id": 3, "lat": d0[0], "lon": d0[1],
         "tags": {"highway": "speed_camera", "maxspeed": "50", "direction": str(round((geo.bearing_deg(d0, d1) + 180) % 360))}},
        {"type": "node", "id": 4, "lat": far[0], "lon": far[1], "tags": {"highway": "speed_camera"}},
        {"type": "node", "id": 5, "lat": rel_dev[0], "lon": rel_dev[1], "tags": {}},
        {"type": "relation", "id": 9, "tags": {"type": "enforcement", "enforcement": "average_speed", "maxspeed": "60"},
         "members": [{"type": "node", "ref": 5, "role": "device", "lat": rel_dev[0], "lon": rel_dev[1]},
                     {"type": "node", "ref": 6, "role": "from", "lat": rel_from[0], "lon": rel_from[1]}]},
    ]}, travel


OPEN_METEO_NOW = {"latitude": 51.5, "longitude": -0.12, "current": {
    "time": "2026-11-07T10:00", "interval": 900, "temperature_2m": 9.4, "apparent_temperature": 6.8,
    "relative_humidity_2m": 88, "precipitation": 0.6, "rain": 0.6, "weather_code": 61, "cloud_cover": 100,
    "visibility": 8000, "wind_speed_10m": 18.2, "wind_gusts_10m": 34.0, "is_day": 1}}


def fake_client(waze_line):
    body, _ = overpass_on(waze_line)
    calls = {"overpass": 0, "meteo": 0}

    def handler(request):
        if "interpreter" in str(request.url):
            calls["overpass"] += 1
            return httpx.Response(200, json=body)
        calls["meteo"] += 1
        return httpx.Response(200, json=OPEN_METEO_NOW)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


def route_event(resp, **kw):
    return {"source": "get_route", "endpoint": "routes_search",
            "request": {"from": "51.507321,-0.12803", "to": "51.469993,-0.454321"},
            "response": resp, "status_code": 200, "latency_ms": 840, **kw}


# ---- geometry --------------------------------------------------------------------------

def test_bearing_and_compass():
    assert round(geo.bearing_deg((0, 0), (1, 0))) == 0
    assert round(geo.bearing_deg((0, 0), (0, 1))) == 90
    assert geo.compass(91) == "E" and geo.compass(350) == "N" and geo.compass(225) == "SW"
    assert geo.angle_diff(350, 10) == 20


def test_nearest_on_line():
    line = [(3.0, 101.0), (3.0, 101.01)]          # eastbound, ~1.1 km
    off, along, i, bearing = geo.nearest_on_line((3.0001, 101.005), line)
    assert 10 < off < 12 and 540 < along < 570 and i == 0 and round(bearing) == 90


# ---- normalise -------------------------------------------------------------------------

def test_waze_routes(waze_routes):
    out = normalize(route_event(waze_routes))
    assert out["event"]["provider"] == "waze"
    assert len(out["routes"]) == 3
    r = out["routes"][0]
    assert r["duration_seconds"] == 2606 and len(r["polyline"]) == 378
    assert r["origin"] == (51.507321, -0.12803)
    jams = [t for t in out["traffic"] if t["kind"] == "route_jam"]
    assert len(jams) == 3 and jams[0]["lat"] is not None
    roles = sorted(role for _, role, _ in out["places"])
    assert roles == ["destination", "origin"]
    dest = next(p for p, role, _ in out["places"] if role == "destination")
    assert dest["name"] == "Westminster Abbey"


def test_google_and_apple_shapes():
    google = {"from": {"lat": 3.1579, "lng": 101.7116, "place": None}, "to": {"lat": 3.134, "lng": 101.6862, "place": None},
              "routes": [{"name": "Jalan Tun Razak", "duration_seconds": 840, "distance_meters": 5400,
                          "traffic_status": "Fastest route, the usual traffic",
                          "polyline": [{"lat": 3.1579, "lng": 101.7116}, {"lat": 3.134, "lng": 101.6862}]}],
              "source": "google_maps"}
    out = normalize({"source": "get_route_backup", "endpoint": "routes_search", "request": {}, "response": google})
    assert out["event"]["provider"] == "google_maps"
    assert out["routes"][0]["traffic_status"] == "Fastest route, the usual traffic"

    apple = {**google, "source": "apple_maps", "stale": True}
    out = normalize({"source": "get_route_apple", "endpoint": "routes_search", "request": {}, "response": apple})
    assert out["event"]["provider"] == "apple_maps" and out["event"]["is_cached"] is True


def test_get_ride_place_search_and_reverse():
    resp = {"places": [
        {"id": "ChIJ1", "name": "KLCC", "address": "Kuala Lumpur", "lat": 3.1579, "lng": 101.7116, "types": ["mall"]},
        {"id": "ChIJ2", "name": "Pavilion", "address": "Bukit Bintang"},        # autocomplete: no coordinates yet
    ]}
    out = normalize({"source": "get_ride", "endpoint": "places_search", "provider": "google",
                     "request": {"q": "klcc"}, "response": resp})
    assert out["event"]["query_text"] == "klcc"
    assert [p["name"] for p, _, _ in out["places"]] == ["KLCC"]
    assert out["places"][0][0]["place_key"] == "google:ChIJ1"

    rev = normalize({"source": "get_ride", "endpoint": "geocode_reverse", "provider": "nas",
                     "request": {"lat": "3.15", "lng": "101.7"},
                     "response": {"place": {"name": "Jalan Ampang", "address": "Jalan Ampang, KL", "lat": 3.15, "lng": 101.7}}})
    assert rev["places"][0][0]["name"] == "Jalan Ampang"


def test_jams_and_alerts_search():
    out = normalize({"source": "get_route", "endpoint": "jams_search", "request": {"location": "51.5,-0.12"},
                     "response": load("get_route_jams_alerts_london.json")})
    assert len(out["traffic"]) == 10 and all(t["kind"] == "area_jam" for t in out["traffic"])
    assert out["traffic"][0]["speed_kmh"] is not None
    assert len(out["alerts"]) == 15 and out["alerts"][0]["type"] == "ROAD_CLOSED"


def test_errors_keep_the_event_only():
    out = normalize({"source": "get_route", "endpoint": "routes_search", "request": {}, "status_code": 400,
                     "response": {"error": "no route"}})
    assert out["routes"] == [] and out["event"]["status_code"] == 400


# ---- OSM -------------------------------------------------------------------------------

def test_osm_match(waze_routes):
    line = [(p["lat"], p["lng"]) for p in waze_routes["routes"][0]["polyline"]]
    body, travel = overpass_on(line)
    lights, cams = osm.parse_overpass(body)
    assert [x["id"] for x in lights] == ["osm:node/1"]
    by_id = {c["id"]: c for c in cams}
    assert by_id["osm:node/2"]["max_speed_kmh"] == 64            # 40 mph
    assert by_id["osm:node/5"]["camera_type"] == "average_speed" and by_id["osm:node/5"]["direction_deg"] is not None
    rows = osm.match_to_route(line, lights, cams)
    got = {r["feature_id"]: r for r in rows}
    assert "osm:node/4" not in got                              # 400 m away
    assert got["osm:node/1"]["feature_type"] == "traffic_light"
    assert got["osm:node/1"]["travel_direction"] == geo.compass(travel)
    assert got["osm:node/2"]["applies_to_travel"] is True       # enforces our direction
    assert got["osm:node/3"]["applies_to_travel"] is False      # the other carriageway
    assert got["osm:node/5"]["applies_to_travel"] is True
    assert [r["distance_along_m"] for r in rows] == sorted(r["distance_along_m"] for r in rows)


def test_direction_tags():
    assert osm.parse_direction("45") == (45.0, "45")
    assert osm.parse_direction("NE") == (45.0, "NE")
    assert osm.parse_direction("forward") == (None, "forward")


def test_route_boxes_split_long_routes(waze_routes):
    line = [(p["lat"], p["lng"]) for p in waze_routes["routes"][0]["polyline"]]
    assert len(osm.route_boxes(line)) >= 3                      # ~30 km in 8 km pieces


# ---- weather ---------------------------------------------------------------------------

def test_weather_parse_now_and_forecast():
    row = weather.parse(OPEN_METEO_NOW, 51.5, -0.12)
    assert row["condition"] == "rain" and row["temperature_c"] == 9.4 and row["is_forecast"] is False
    assert row["observed_at"] == datetime(2026, 11, 7, 10, tzinfo=timezone.utc)
    hourly = {"hourly": {"time": ["2026-11-20T08:00"], "temperature_2m": [27.0], "weather_code": [95],
                         "precipitation": [12.5], "is_day": [1]}}
    f = weather.parse(hourly, 3.15, 101.7)
    assert f["is_forecast"] is True and f["condition"] == "thunderstorm" and f["precipitation_mm"] == 12.5


# ---- pipeline + stores -----------------------------------------------------------------

def test_pipeline_to_spool(tmp_path, waze_routes):
    async def run():
        line = [(p["lat"], p["lng"]) for p in waze_routes["routes"][0]["polyline"]]
        client, calls = fake_client(line)
        store = SpoolStore(tmp_path)
        b = await Pipeline(store, client).process(route_event(waze_routes))
        b2 = await Pipeline(store, client).process(route_event(waze_routes))
        return b, b2, calls, store
    b, _, calls, store = asyncio.run(run())
    shape = b["shapes"][b["routes"][0]["shape_hash"]]
    kinds = {f["feature_type"] for f in shape["features"]}
    assert kinds == {"traffic_light", "speed_camera"}
    assert shape["point_count"] < 378 and shape["length_m"] > 25000          # thinned, full length kept
    assert {w["role"] for w in b["weather"]} == {"origin", "destination", "midpoint"}
    assert len(store.files()) == 1 and len(store.files()[0].read_text().splitlines()) == 2


def test_future_trip_asks_the_forecast(tmp_path):
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"hourly": {"time": ["2026-11-20T08:00"], "weather_code": [3]}})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        when = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        ev = {"source": "get_route_apple", "endpoint": "routes_search", "request": {"depart_at": when},
              "response": {"from": {"lat": 3.15, "lng": 101.7}, "to": {"lat": 3.0, "lng": 101.6}, "routes": []}}
        return await Pipeline(SpoolStore(tmp_path), client, enrich_osm=False).process(ev)
    b = asyncio.run(run())
    assert all("hourly" in p and "start_hour" in p for p in seen)
    assert b["weather"][0]["is_forecast"] is True


def test_enrichment_failure_still_stores(tmp_path, waze_routes):
    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(504)))
        p = Pipeline(SpoolStore(tmp_path), client)
        b = await p.process(route_event(waze_routes))
        return p, b
    p, b = asyncio.run(run())
    assert p.stats["enrich_errors"] > 0 and len(b["routes"]) == 3 and b["weather"] == []


def test_postgres_end_to_end(db_url, tmp_path, waze_routes, monkeypatch):
    import psycopg
    monkeypatch.setattr(config, "STORE_RAW", "errors")

    async def run():
        line = [(p["lat"], p["lng"]) for p in waze_routes["routes"][0]["polyline"]]
        client, calls = fake_client(line)
        store = PostgresStore(db_url, SpoolStore(tmp_path))
        pipe = Pipeline(store, client)
        ev = route_event(waze_routes, id="11111111-1111-1111-1111-111111111111",
                         captured_at="2026-11-18T08:05:00Z")
        await pipe.process(dict(ev))
        first = calls["overpass"]
        await pipe.process(dict(ev))                       # the same event again: stored once
        await pipe.process(route_event(waze_routes))       # the same trip quoted again
        await Pipeline(store, client).process(route_event(waze_routes))   # ... by a fresh worker (asks the DB)
        await pipe.process({"source": "get_route", "endpoint": "jams_search", "request": {},
                            "response": load("get_route_jams_alerts_london.json")})
        await pipe.process({"source": "get_route", "endpoint": "jams_search", "request": {},
                            "response": load("get_route_jams_alerts_london.json")})   # same jams, same 15 min
        await pipe.process({"source": "get_route", "endpoint": "routes_search", "request": {},
                            "status_code": 500, "response": {"error": "waze failed"}})
        await store.close()
        return store, first, calls["overpass"]
    store, first, total = asyncio.run(run())
    assert store.spooled == 0, store.last_error
    assert first > 0 and total == first      # lights / cameras looked up once per shape, never again
    with psycopg.connect(db_url) as c:
        q = lambda sql: c.execute(sql).fetchone()[0]
        assert q("select count(*) from map.search_events") == 6
        assert q("select count(*) from map.routes") == 9
        assert q("select count(*) from map.route_shapes") == 3                # stored once each
        assert q("select seen_count from map.route_shapes order by id limit 1") == 3
        assert q("select count(*) from map.places") == 2
        assert q("select seen_count from map.places where name = 'Westminster Abbey'") == 3
        assert q("select count(*) from map.traffic_lights") == 1
        assert q("select count(*) from map.speed_cameras") == 3
        assert q("select count(*) from map.speed_cameras_on_routes where applies_to_travel") >= 2
        assert q("select traffic_light_count from map.route_shapes order by id limit 1") == 1
        assert q("select count(*) from map.road_alerts") == 15
        assert q("select max(seen_count) from map.road_alerts") == 2
        assert q("select count(*) from map.traffic_observations where kind = 'area_jam'") == 10
        assert q("select count(*) from map.weather_observations") == 3   # start, end, middle: 3 cells, stored once for 4 quotes
        assert q("select count(*) from map.route_conditions where weather = 'rain'") == 9
        assert q("select count(*) from map.raw_responses") == 1               # only the failed request
        enc = q("select polyline from map.route_shapes order by id limit 1")
        assert geo.decode_polyline(enc)[0] == (51.50732, -0.12803)


def test_database_down_goes_to_spool_and_replays(db_url, tmp_path, waze_routes):
    import psycopg

    async def run():
        spool = SpoolStore(tmp_path)
        down = PostgresStore("postgresql://nobody@127.0.0.1:1/none?connect_timeout=1", spool)
        p = Pipeline(down, None)
        await p.process(route_event(waze_routes))
        assert down.spooled == 1
        up = PostgresStore(db_url, spool)
        moved = await up.replay_spool()
        await up.close()
        return moved
    assert asyncio.run(run()) == 1
    with psycopg.connect(db_url) as c:
        assert c.execute("select count(*) from map.routes").fetchone()[0] == 3
        assert c.execute("select count(*) from map.route_shapes").fetchone()[0] == 3


def test_raw_policy(tmp_path, waze_routes, monkeypatch):
    async def run(policy, paused=False, status=200):
        monkeypatch.setattr(config, "STORE_RAW", policy)
        p = Pipeline(SpoolStore(tmp_path), None)
        p.raw_paused = paused
        return (await p.process(route_event(waze_routes, status_code=status)))["raw"]
    assert asyncio.run(run("all")) is not None
    assert asyncio.run(run("none", status=500)) is None
    assert asyncio.run(run("errors")) is None
    assert asyncio.run(run("errors", status=500)) is not None
    assert asyncio.run(run("all", paused=True)) is None                  # disk short: errors only
    monkeypatch.setattr(config, "RAW_SAMPLE_RATE", 1.0)
    assert asyncio.run(run("sample")) is not None


def test_polyline_encoding_and_simplify(waze_routes):
    line = [(p["lat"], p["lng"]) for p in waze_routes["routes"][0]["polyline"]]
    thin = geo.simplify(line, 10)
    assert thin[0] == line[0] and thin[-1] == line[-1] and len(thin) < len(line)
    # every dropped point is within ~10 m of the thinned line
    assert max(geo.nearest_on_line(p, thin)[0] for p in line) <= 10.5
    back = geo.decode_polyline(geo.encode_polyline(thin))
    assert all(abs(a[0] - b[0]) < 1e-5 and abs(a[1] - b[1]) < 1e-5 for a, b in zip(thin, back))
