import asyncio
import base64

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import load
from getmap import config
from getmap.pipeline import Pipeline
from getmap.server import create_app
from getmap.store import PostgresStore, SpoolStore
from test_capture import fake_client, route_event


def auth(user="admin", pw="secret"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def seeded(db_url, tmp_path, waze_routes, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", db_url)
    monkeypatch.setattr(config, "STORE_RAW", "all")

    async def run():
        line = [(p["lat"], p["lng"]) for p in waze_routes["routes"][0]["polyline"]]
        client, _ = fake_client(line)
        store = PostgresStore(db_url, SpoolStore(tmp_path))
        p = Pipeline(store, client)
        await p.process(route_event(waze_routes, client_ref="abc123"))
        await p.process({"source": "get_ride", "endpoint": "places_search", "provider": "google",
                         "request": {"q": "klcc"},
                         "response": {"places": [{"id": "ChIJ1", "name": "KLCC", "address": "Kuala Lumpur",
                                                  "lat": 3.1579, "lng": 101.7116}]}})
        await p.process({"source": "get_route", "endpoint": "jams_search", "request": {},
                         "response": load("get_route_jams_alerts_london.json")})
        await p.process({"source": "get_route_apple", "endpoint": "routes_search", "request": {},
                         "status_code": 503, "response": {"error": "quota"}})
        await store.close()
    asyncio.run(run())
    return db_url


def client_for(monkeypatch, password="secret"):
    monkeypatch.setattr(config, "UI_PASSWORD", password)
    store = SpoolStore("/tmp/getmap-ui-test-spool")
    app = create_app(store=store, client=httpx.AsyncClient(), pipeline=Pipeline(store, None))
    return TestClient(app)


def test_sign_in_rules(monkeypatch):
    with client_for(monkeypatch, password="secret") as c:
        assert c.get("/ui").status_code == 401
        assert c.get("/ui", headers=auth(pw="wrong")).status_code == 401
        r = c.get("/ui", headers=auth())
        assert r.status_code == 200 and "get.map" in r.text
        assert c.get("/api/ui/settings").status_code == 401
        assert c.get("/ui/static/vendor/leaflet/leaflet.js").status_code == 200   # no data in static files
    with client_for(monkeypatch, password="") as c:
        assert c.get("/ui").status_code == 200                                  # TestClient counts as this machine
        monkeypatch.setattr("getmap.ui._is_local", lambda request: False)
        assert c.get("/ui").status_code == 403                                  # others need UI_PASSWORD


def test_every_view(seeded, monkeypatch):
    h = auth()
    with client_for(monkeypatch) as c:
        ov = c.get("/api/ui/overview?hours=24", headers=h).json()
        assert ov["counts"]["searches"] == 4 and ov["counts"]["routes"] == 3 and ov["counts"]["failed"] == 1
        assert {r["source"] for r in ov["by_source"]} == {"get_route", "get_ride", "get_route_apple"}
        assert ov["series"]

        s = c.get("/api/ui/searches?hours=24", headers=h).json()
        assert s["total"] == 4
        assert c.get("/api/ui/searches?hours=24&failed=true", headers=h).json()["total"] == 1
        assert c.get("/api/ui/searches?hours=24&q=klcc", headers=h).json()["total"] == 1
        assert c.get("/api/ui/searches?hours=24&client_ref=abc123", headers=h).json()["total"] == 1
        route_search = next(x for x in s["items"] if x["endpoint"] == "routes_search" and x["routes"])
        d = c.get(f"/api/ui/searches/{route_search['id']}", params={"at": route_search["captured_at"]}, headers=h).json()
        assert len(d["routes"]) == 3 and {p["name"] for p in d["places"]} >= {"Westminster Abbey"}
        assert d["weather"] and d["raw"]["routes"]

        r = c.get("/api/ui/routes?hours=24", headers=h).json()
        assert r["total"] == 3 and r["items"][0]["weather"] == "rain"
        assert c.get("/api/ui/routes?hours=24&min_cameras=1", headers=h).json()["total"] >= 1
        first = r["items"][0]
        rd = c.get(f"/api/ui/routes/{first['id']}", params={"at": first["captured_at"]}, headers=h).json()
        assert rd["shape"]["polyline"] and rd["features"]
        cams = [f for f in rd["features"] if f["feature_type"] == "speed_camera"]
        assert all(f["lat"] is not None for f in rd["features"]) and any(f["applies_to_travel"] for f in cams)

        p = c.get("/api/ui/places?q=klcc", headers=h).json()
        assert p["items"][0]["name"] == "KLCC" and p["items"][0]["lat"] == 3.1579

        m = c.get("/api/ui/map?bbox=51.3,-0.6,51.6,0.1&hours=24", headers=h).json()
        assert len(m["cameras"]) == 3 and len(m["lights"]) == 1 and m["jams"] and m["alerts"]
        assert m["jams"][0]["points"]

        cams = c.get("/api/ui/cameras", headers=h).json()
        assert cams["total"] == 3 and max(x["on_quotes"] for x in cams["items"]) >= 1
        assert c.get("/api/ui/lights", headers=h).json()["total"] == 1

        t = c.get("/api/ui/traffic?hours=24&min_level=0", headers=h).json()
        assert t["total"] >= 10
        bh = c.get("/api/ui/traffic/by-hour?days=2", headers=h).json()
        assert bh["by_hour"]
        assert c.get("/api/ui/alerts?hours=24&type=ROAD_CLOSED", headers=h).json()["total"] == 15

        w = c.get("/api/ui/weather?hours=100000", headers=h).json()
        assert w["total"] == 4 and w["summary"][0]["condition"] == "rain"   # route: start/end/middle + the KLCC search

        st = c.get("/api/ui/storage", headers=h).json()
        assert st["db_bytes"] > 0 and any(x["table_name"] == "search_events" for x in st["tables"])


def test_csv_and_bad_input(seeded, monkeypatch):
    h = auth()
    with client_for(monkeypatch) as c:
        r = c.get("/api/ui/places?format=csv", headers=h)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        head, *rows = r.text.strip().splitlines()
        assert "lat" in head and "lng" in head and len(rows) == 3
        assert c.get("/api/ui/routes?format=csv&hours=24", headers=h).text.count("\n") == 4
        assert c.get("/api/ui/map?bbox=nope", headers=h).status_code == 400
        assert c.get("/api/ui/searches?since=yesterday", headers=h).status_code == 400


def test_viewer_cannot_write(seeded):
    from getmap.ui import Db
    with pytest.raises(Exception, match="read-only"):
        asyncio.run(Db(seeded).query("delete from map.places"))
