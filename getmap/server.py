"""get.map capture API.

    POST /capture                 one event, or {"events": [...]}  (X-Capture-Key)
    GET  /tap/<source>/<path>     forwards to that route server, answers with its
                                  reply unchanged, and records it  (X-Capture-Key
                                  or the route server's own X-Route-Key)
    GET  /health                  always open; queue and store counters with the key
    GET  /ui                      the data viewer (UI_USER / UI_PASSWORD; see getmap/ui)

An event:
    {
      "source":   "get_ride" | "get_route" | "get_route_backup" | "get_route_apple",
      "endpoint": "routes_search" | "places_search" | "places_details" |
                  "geocode_reverse" | "jams_search" | "alerts_search" | "other",
      "provider": "waze" | "google_maps" | "apple_maps" | "google" | "osm" | ...   (optional)
      "request":  {...the parameters asked...},
      "response": {...the answer, as sent...},
      "status_code": 200, "latency_ms": 840, "error": null,
      "client_ref": "<hashed user/session id>", "app_version": "5.2.0",
      "captured_at": "2026-11-19T08:00:00Z"   (optional, default now)
    }
Capture is answered 202 at once; the work happens in the background.
"""
import asyncio
import hmac
import logging
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from getmap import config, retention
from getmap.normalize import ENDPOINTS, SOURCES
from getmap.pipeline import Pipeline
from getmap.store import PostgresStore, SpoolStore

log = logging.getLogger("getmap")

TAP_ENDPOINTS = {
    "routes/search": "routes_search", "places/search": "places_search", "places/details": "places_details",
    "geocode/reverse": "geocode_reverse", "jams/search": "jams_search", "alerts/search": "alerts_search",
}


def build_store():
    spool = SpoolStore(config.SPOOL_DIR)
    return PostgresStore(config.DATABASE_URL, spool, size=config.WORKERS) if config.DATABASE_URL else spool


async def _replay_loop(store):
    while True:
        await asyncio.sleep(60)
        try:
            moved = await store.replay_spool()
            if moved:
                log.info("replayed %d spooled captures", moved)
        except Exception:
            log.exception("spool replay failed")


def create_app(store=None, client=None, pipeline=None):
    state = {}

    @asynccontextmanager
    async def lifespan(app):
        state["client"] = client or httpx.AsyncClient(headers={"User-Agent": "get.map/1.0"})
        state["store"] = store or build_store()
        state["pipeline"] = pipeline or Pipeline(state["store"], state["client"])
        state["pipeline"].start()
        tasks = []
        if isinstance(state["store"], PostgresStore):
            tasks.append(asyncio.create_task(_replay_loop(state["store"])))
            tasks.append(asyncio.create_task(retention.loop(state["store"].dsn, state["pipeline"])))
        yield
        await state["pipeline"].stop()
        for t in tasks:
            t.cancel()
        await state["store"].close()
        if client is None:
            await state["client"].aclose()

    app = FastAPI(title="get.map capture", lifespan=lifespan)
    app.state.ctx = state
    from getmap import ui
    ui.mount(app, state)

    @app.get("/", include_in_schema=False)
    async def root():
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/ui")

    def key_ok(request):
        expected = config.CAPTURE_API_KEY
        if not expected:
            return True
        given = request.headers.get("X-Capture-Key") or ""
        return hmac.compare_digest(given.encode(), expected.encode())

    def check_event(ev):
        if not isinstance(ev, dict):
            raise HTTPException(400, "each event must be a JSON object")
        if ev.get("source") not in SOURCES:
            raise HTTPException(400, f"source must be one of {', '.join(SOURCES)}")
        if ev.get("endpoint", "other") not in ENDPOINTS:
            raise HTTPException(400, f"endpoint must be one of {', '.join(ENDPOINTS)}")
        ev.setdefault("id", str(uuid.uuid4()))
        return ev

    @app.post("/capture", status_code=202)
    async def capture(request: Request):
        if not key_ok(request):
            raise HTTPException(401, "missing or wrong X-Capture-Key")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "body must be JSON")
        events = body.get("events") if isinstance(body, dict) and "events" in body else [body]
        if not isinstance(events, list) or len(events) > 500:
            raise HTTPException(400, "events must be a list of at most 500")
        events = [check_event(ev) for ev in events]
        accepted = sum(1 for ev in events if state["pipeline"].submit(ev))
        return {"accepted": accepted, "dropped": len(events) - accepted, "ids": [ev["id"] for ev in events]}

    @app.get("/tap/{source}/{path:path}")
    async def tap(source: str, path: str, request: Request):
        upstream = config.UPSTREAMS.get(source)
        if not upstream:
            raise HTTPException(404, f"no upstream configured for {source}")
        route_key = request.headers.get("X-Route-Key")
        if config.CAPTURE_API_KEY and not key_ok(request) and not route_key:
            raise HTTPException(401, "missing X-Capture-Key or X-Route-Key")
        headers = {"X-Route-Key": route_key} if route_key else {}
        started = time.monotonic()
        try:
            r = await state["client"].get(f"{upstream.rstrip('/')}/{path}", params=list(request.query_params.multi_items()),
                                          headers=headers, timeout=config.HTTP_TIMEOUT)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"upstream failed: {type(e).__name__}")
        latency = int((time.monotonic() - started) * 1000)
        clean = path.strip("/").removeprefix("waze/")
        endpoint = TAP_ENDPOINTS.get(clean)
        if endpoint and r.status_code != 401:
            try:
                body = r.json()
            except ValueError:
                body = None
            state["pipeline"].submit({
                "id": str(uuid.uuid4()), "source": source, "endpoint": endpoint,
                "request": dict(request.query_params), "response": body,
                "status_code": r.status_code, "latency_ms": latency,
                "error": body.get("error") if isinstance(body, dict) and r.status_code >= 400 else None,
                "client_ref": request.headers.get("X-Client-Ref"),
            })
        passthrough = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "retry-after")}
        return Response(r.content, status_code=r.status_code, headers=passthrough)

    @app.get("/health")
    async def health(request: Request):
        out = {"status": "ok"}
        if key_ok(request):
            p, s = state["pipeline"], state["store"]
            out.update({
                "queue": p.queue.qsize(), **p.stats, "last_enrich_error": p.last_enrich_error,
                "raw_paused_for_disk": p.raw_paused,
                "store": "postgres" if isinstance(s, PostgresStore) else "spool",
                "store_written": getattr(s, "written", None), "store_spooled": getattr(s, "spooled", None),
                "store_last_error": getattr(s, "last_error", None),
                "taps": [k for k, v in config.UPSTREAMS.items() if v],
            })
        return JSONResponse(out)

    return app


app = create_app()
