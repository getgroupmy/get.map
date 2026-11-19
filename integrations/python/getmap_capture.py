"""Drop-in capture for the route servers: copy this one file into the repo.

    get.route          (bottle)   in run.py, after `import routes`:
                                      import getmap_capture
                                      getmap_capture.install_bottle(source="get_route")
    get.route.backup   (bottle)   same, with source="get_route_backup"
    get.route.apple    (FastAPI)  in main.py, after `app = FastAPI(...)`:
                                      import getmap_capture
                                      getmap_capture.install_fastapi(app, source="get_route_apple")

Set GETMAP_URL (e.g. http://get-map:8090) and GETMAP_KEY in the container's
.env. Without GETMAP_URL nothing is sent. Uses only the standard library.

Sending never slows or breaks an answer: events go into a small in-memory
queue and one background thread posts them in batches; if get.map is down
or the queue is full, events are dropped (counted in `stats`).
"""
import json
import os
import queue
import threading
import time
import urllib.request

ENDPOINTS = {
    "routes/search": "routes_search", "places/search": "places_search", "places/details": "places_details",
    "geocode/reverse": "geocode_reverse", "jams/search": "jams_search", "alerts/search": "alerts_search",
    "directions": "routes_search",
}
stats = {"queued": 0, "sent": 0, "dropped": 0, "failed_batches": 0}
_q = queue.Queue(maxsize=int(os.environ.get("GETMAP_QUEUE", "5000")))
_started = False
_lock = threading.Lock()


def _url():
    return (os.environ.get("GETMAP_URL") or "").rstrip("/")


def endpoint_for(path):
    clean = path.strip("/")
    if clean.startswith("waze/"):
        clean = clean[len("waze/"):]
    return ENDPOINTS.get(clean)


def submit(event):
    """Queue one capture event (see get.map's server.py for the fields)."""
    if not _url():
        return
    _ensure_thread()
    event.setdefault("captured_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    try:
        _q.put_nowait(event)
        stats["queued"] += 1
    except queue.Full:
        stats["dropped"] += 1


def _ensure_thread():
    global _started
    with _lock:
        if not _started:
            threading.Thread(target=_sender, name="getmap-capture", daemon=True).start()
            _started = True


def _post(events):
    data = json.dumps({"events": events}, ensure_ascii=False, default=str).encode()
    req = urllib.request.Request(_url() + "/capture", data=data, method="POST", headers={
        "Content-Type": "application/json", "X-Capture-Key": os.environ.get("GETMAP_KEY", "")})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def _sender():
    while True:
        batch = [_q.get()]
        deadline = time.monotonic() + 1.0
        while len(batch) < 200 and time.monotonic() < deadline:
            try:
                batch.append(_q.get(timeout=max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                break
        try:
            _post(batch)
            stats["sent"] += len(batch)
        except Exception:
            stats["failed_batches"] += 1
            stats["dropped"] += len(batch)
            time.sleep(2)


def _event(source, path, query, status, body_text, started, client_ref=None):
    endpoint = endpoint_for(path)
    if endpoint is None or status == 401:
        return None
    try:
        body = json.loads(body_text) if body_text else None
    except ValueError:
        body = None
    if endpoint == "routes_search" and path.strip("/") == "directions":
        query = {**query, "from": query.get("origin"), "to": query.get("destination")}
    return {
        "source": source, "endpoint": endpoint, "request": query, "response": body,
        "status_code": status, "latency_ms": int((time.monotonic() - started) * 1000),
        "error": body.get("error") if isinstance(body, dict) and status >= 400 else None,
        "client_ref": client_ref,   # GET.ride's hashed user id, from the X-Client-Ref header
    }


# ---- bottle (get.route, get.route.backup) ----------------------------------------------

def install_bottle(source, app=None):
    import bottle
    app = app or bottle.default_app()

    @app.hook("before_request")
    def _start():
        bottle.request.environ["getmap.started"] = time.monotonic()

    def plugin(callback):
        def wrapper(*args, **kwargs):
            body = callback(*args, **kwargs)
            try:
                if isinstance(body, (str, bytes)) and endpoint_for(bottle.request.path):
                    q = {k: bottle.request.query.getunicode(k) for k in bottle.request.query.keys()}
                    started = bottle.request.environ.get("getmap.started", time.monotonic())
                    ev = _event(source, bottle.request.path, q, bottle.response.status_code,
                                body.decode() if isinstance(body, bytes) else body, started,
                                bottle.request.get_header("X-Client-Ref"))
                    if ev:
                        submit(ev)
            except Exception:
                pass   # capture must never break an answer
            return body
        return wrapper

    app.install(plugin)


# ---- FastAPI / Starlette (get.route.apple) ---------------------------------------------

def install_fastapi(app, source):
    from starlette.responses import Response

    @app.middleware("http")
    async def _capture(request, call_next):
        started = time.monotonic()
        response = await call_next(request)
        if not endpoint_for(request.url.path):
            return response
        chunks = [c async for c in response.body_iterator]
        body = b"".join(chunks)
        try:
            ev = _event(source, request.url.path, dict(request.query_params), response.status_code,
                        body.decode("utf-8", "replace"), started, request.headers.get("X-Client-Ref"))
            if ev:
                submit(ev)
        except Exception:
            pass
        return Response(body, status_code=response.status_code, headers=dict(response.headers),
                        media_type=response.media_type)
