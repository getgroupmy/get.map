import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from getmap import config
from getmap.backfill import event_from_row
from getmap.normalize import normalize
from getmap.pipeline import Pipeline
from getmap.server import create_app
from getmap.store import SpoolStore


class Recorder:
    """A store that keeps bundles in memory."""

    def __init__(self):
        self.bundles = []

    async def write(self, b):
        self.bundles.append(b)

    async def close(self):
        pass


def make_app(monkeypatch, upstream_handler=None):
    monkeypatch.setattr(config, "CAPTURE_API_KEY", "k")
    monkeypatch.setitem(config.UPSTREAMS, "get_route", "http://pool:8000")
    store = Recorder()
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler or (lambda r: httpx.Response(404))))
    pipe = Pipeline(store, client, enrich_osm=False, enrich_weather=False)
    return create_app(store=store, client=client, pipeline=pipe), store


def test_capture_needs_key_and_valid_source(monkeypatch):
    app, store = make_app(monkeypatch)
    with TestClient(app) as c:
        assert c.post("/capture", json={"source": "get_route"}).status_code == 401
        h = {"X-Capture-Key": "k"}
        assert c.post("/capture", json={"source": "nope", "endpoint": "other"}, headers=h).status_code == 400
        r = c.post("/capture", headers=h, json={"events": [
            {"source": "get_ride", "endpoint": "places_search", "request": {"q": "klcc"},
             "response": {"places": [{"name": "KLCC", "lat": 3.1579, "lng": 101.7116}]}}]})
        assert r.status_code == 202 and r.json()["accepted"] == 1
        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/health", headers=h).json()["accepted"] == 1
    assert store.bundles[0]["places"][0][0]["name"] == "KLCC"


def test_tap_passes_the_answer_through_and_records_it(monkeypatch, waze_routes):
    seen = {}

    def upstream(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("X-Route-Key")
        return httpx.Response(200, json=waze_routes)
    app, store = make_app(monkeypatch, upstream)
    with TestClient(app) as c:
        r = c.get("/tap/get_route/routes/search?from=51.5,-0.12&to=51.47,-0.45&max_routes=1",
                  headers={"X-Route-Key": "route-secret"})
        assert r.status_code == 200 and r.json() == waze_routes
    assert seen["url"].startswith("http://pool:8000/routes/search?from=51.5")
    assert seen["key"] == "route-secret"
    assert len(store.bundles) == 1 and len(store.bundles[0]["routes"]) == 3


def test_fastapi_drop_in(monkeypatch, waze_routes):
    import getmap_capture
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    sent = []
    monkeypatch.setattr(getmap_capture, "submit", sent.append)
    app = FastAPI()

    @app.get("/routes/search")
    async def rs():
        return JSONResponse(waze_routes)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    getmap_capture.install_fastapi(app, source="get_route_apple")
    with TestClient(app) as c:
        assert c.get("/routes/search?from=1,2&to=3,4").json() == waze_routes
        c.get("/health")
    assert len(sent) == 1 and sent[0]["source"] == "get_route_apple" and sent[0]["request"]["from"] == "1,2"
    assert len(normalize(sent[0])["routes"]) == 3


def test_bottle_drop_in(monkeypatch):
    import bottle
    from webtest_lite import call
    import getmap_capture

    sent = []
    monkeypatch.setattr(getmap_capture, "submit", sent.append)
    app = bottle.Bottle()

    @app.get("/waze/places/search")
    def ps():
        bottle.response.content_type = "application/json"
        return json.dumps({"places": [{"name": "KLCC", "location": {"lat": 3.1579, "lng": 101.7116}}]})

    getmap_capture.install_bottle(source="get_route", app=app)
    status, body = call(app, "/waze/places/search?query=klcc")
    assert status.startswith("200") and json.loads(body)["places"][0]["name"] == "KLCC"
    assert sent[0]["endpoint"] == "places_search" and sent[0]["request"] == {"query": "klcc"}


def test_backfill_row_with_cut_raw_response():
    import datetime
    cut = '{"from": {"lat": 3.1, "lng": 101.7}, "routes": [{"name": "E7", "polyline": [{"lat": 3.1, '
    row = ("2b0b6f3e-0000-4000-8000-000000000001", datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc),
           "waze", 3.1, 101.7, 3.0, 101.6, True, 200, 48.2, 49.0, "E7", None, 900, cut)
    ev = event_from_row(row)
    out = normalize(ev)
    assert ev["source"] == "get_route" and out["routes"][0]["distance_meters"] == 48200
    assert out["routes"][0]["duration_seconds"] == 2940
