"""The data viewer: a read-only web page (/ui) and its JSON API (/api/ui/...).

Sign-in is HTTP Basic (UI_USER / UI_PASSWORD). Without UI_PASSWORD the viewer
answers only on the Mac mini itself. Every query runs in a read-only
transaction with a time limit, so the viewer can never change or block the
captured data.
"""
import csv
import hmac
import io
import ipaddress
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from getmap import config, geo

STATIC = Path(__file__).parent / "static"
basic = HTTPBasic(auto_error=False)
MAX_LIMIT = 1000
CSV_LIMIT = 50_000


# ---- access ----------------------------------------------------------------------------

def _is_local(request):
    host = request.client.host if request.client else ""
    if host in ("testclient", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_user(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)):
    if not config.UI_PASSWORD:
        if _is_local(request):
            return "local"
        raise HTTPException(403, "Set UI_PASSWORD in getmap.env to open the viewer from other computers.")
    ok = creds is not None and hmac.compare_digest(creds.username.encode(), config.UI_USER.encode()) \
        and hmac.compare_digest(creds.password.encode(), config.UI_PASSWORD.encode())
    if not ok:
        raise HTTPException(401, "Sign in", headers={"WWW-Authenticate": 'Basic realm="get.map"'})
    return creds.username


# ---- helpers ---------------------------------------------------------------------------

def _time(value, name):
    if value in (None, ""):
        return None
    text = value.strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        t = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(400, f"{name} must be an ISO 8601 date-time")
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def window(since=None, until=None, hours=None, default_hours=24):
    """(since, until) from explicit times or `hours` back from now."""
    now = datetime.now(timezone.utc)
    end = _time(until, "until") or now
    start = _time(since, "since")
    if start is None:
        h = hours if hours is not None else default_hours
        start = end - timedelta(hours=max(0.0, float(h)))
    if start > end:
        raise HTTPException(400, "since must be before until")
    return start, end


def _bbox(text):
    if not text:
        return None
    try:
        s, w, n, e = (float(x) for x in text.split(","))
    except ValueError:
        raise HTTPException(400, "bbox must be south,west,north,east")
    if not (-90 <= s <= n <= 90 and -180 <= w <= 180 and -180 <= e <= 180):
        raise HTTPException(400, "bbox out of range")
    return s, w, n, e


def _jsonable(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if hasattr(v, "is_finite"):           # Decimal
        return float(v)
    return v


def rows_out(cur_rows, cols):
    return [{c: _jsonable(v) for c, v in zip(cols, r)} for r in cur_rows]


def as_csv(items, filename):
    buf = io.StringIO()
    if items:
        w = csv.DictWriter(buf, fieldnames=list(items[0].keys()))
        w.writeheader()
        for it in items:
            w.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                        for k, v in it.items()})
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'})


class Db:
    """A read-only connection per request."""

    def __init__(self, dsn):
        self.dsn = dsn

    async def query(self, sql, params=()):
        import psycopg
        async with await psycopg.AsyncConnection.connect(
                self.dsn, autocommit=True,
                options="-c default_transaction_read_only=on -c statement_timeout=20000") as conn:
            cur = await conn.execute(sql, params)
            cols = [d.name for d in cur.description] if cur.description else []
            return rows_out(await cur.fetchall(), cols) if cols else []

    async def one(self, sql, params=()):
        rows = await self.query(sql, params)
        return rows[0] if rows else None


def listing(items, total, limit, offset, fmt, name):
    if fmt == "csv":
        return as_csv(items, name)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ---- the router ------------------------------------------------------------------------

def build_router(state):
    router = APIRouter()

    def db():
        url = config.DATABASE_URL
        if not url:
            raise HTTPException(503, "No database: set DATABASE_URL (the viewer reads the local Postgres).")
        return Db(url)

    def page_args(limit, offset, fmt):
        limit = min(max(1, limit), CSV_LIMIT if fmt == "csv" else MAX_LIMIT)
        return limit, max(0, offset)

    # -- pages -----------------------------------------------------------------------

    @router.get("/ui", include_in_schema=False)
    @router.get("/ui/", include_in_schema=False)
    async def page(_=Depends(require_user)):
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @router.get("/api/ui/settings")
    async def settings(_=Depends(require_user)):
        return {"timezone": config.UI_TIMEZONE, "tile_url": config.UI_TILE_URL,
                "tile_attribution": config.UI_TILE_ATTRIBUTION, "has_database": bool(config.DATABASE_URL)}

    # -- overview --------------------------------------------------------------------

    @router.get("/api/ui/overview")
    async def overview(hours: float = 24, _=Depends(require_user)):
        d = db()
        start, end = window(hours=hours)
        counts = await d.one("""
            select (select count(*) from map.places) as places,
                   (select count(*) from map.route_shapes) as route_shapes,
                   (select count(*) from map.traffic_lights) as traffic_lights,
                   (select count(*) from map.speed_cameras) as speed_cameras,
                   (select count(*) from map.road_alerts) as road_alerts,
                   (select count(*) from map.weather_observations) as weather_observations,
                   (select count(*) from map.search_events where captured_at >= %(s)s and captured_at < %(e)s) as searches,
                   (select count(*) from map.routes where captured_at >= %(s)s and captured_at < %(e)s) as routes,
                   (select count(*) from map.search_events where captured_at >= %(s)s and captured_at < %(e)s
                        and (status_code >= 400 or error is not null)) as failed,
                   (select count(*) from map.traffic_observations where observed_at >= %(s)s and observed_at < %(e)s) as jams,
                   (select min(captured_at) from map.search_events) as first_capture,
                   (select max(captured_at) from map.search_events) as last_capture,
                   pg_database_size(current_database()) as db_bytes
        """, {"s": start, "e": end})
        by_source = await d.query("""
            select source, endpoint, count(*) as n, round(avg(latency_ms)) as avg_latency_ms,
                   count(*) filter (where status_code >= 400 or error is not null) as failed
            from map.search_events where captured_at >= %s and captured_at < %s
            group by 1, 2 order by n desc""", (start, end))
        bucket = "hour" if hours <= 72 else "day"
        series = await d.query(f"""
            select date_trunc('{bucket}', captured_at) as t, source, count(*) as n
            from map.search_events where captured_at >= %s and captured_at < %s
            group by 1, 2 order by 1""", (start, end))
        p = state.get("pipeline")
        s = state.get("store")
        pipeline = None
        if p is not None:
            pipeline = {"queue": p.queue.qsize(), **p.stats, "raw_paused_for_disk": p.raw_paused,
                        "last_enrich_error": p.last_enrich_error,
                        "store_last_error": getattr(s, "last_error", None),
                        "store_spooled": getattr(s, "spooled", None)}
        from getmap import retention
        try:
            free_gb = round(retention.disk_free_gb(), 1)
        except OSError:
            free_gb = None
        return {"since": start.isoformat(), "until": end.isoformat(), "bucket": bucket, "counts": counts,
                "by_source": by_source, "series": series, "pipeline": pipeline,
                "disk": {"free_gb": free_gb, "max_db_gb": config.MAX_DB_GB, "min_free_gb": config.MIN_FREE_DISK_GB}}

    # -- searches --------------------------------------------------------------------

    @router.get("/api/ui/searches")
    async def searches(since: str | None = None, until: str | None = None, hours: float | None = None,
                       source: str | None = None, endpoint: str | None = None, q: str | None = None,
                       failed: bool = False, client_ref: str | None = None,
                       limit: int = 100, offset: int = 0, format: str = "json", _=Depends(require_user)):
        start, end = window(since, until, hours)
        limit, offset = page_args(limit, offset, format)
        where = ["e.captured_at >= %(s)s", "e.captured_at < %(e)s"]
        args = {"s": start, "e": end, "limit": limit, "offset": offset}
        if source:
            where.append("e.source = %(source)s"); args["source"] = source
        if endpoint:
            where.append("e.endpoint = %(endpoint)s"); args["endpoint"] = endpoint
        if q:
            where.append("(e.query_text ilike %(q)s or op.name ilike %(q)s or dp.name ilike %(q)s)")
            args["q"] = f"%{q}%"
        if failed:
            where.append("(e.status_code >= 400 or e.error is not null)")
        if client_ref:
            where.append("e.client_ref = %(client_ref)s"); args["client_ref"] = client_ref
        base = f"""from map.search_events e
                   left join map.places op on op.id = e.origin_place_id
                   left join map.places dp on dp.id = e.destination_place_id
                   where {' and '.join(where)}"""
        d = db()
        items = await d.query(f"""
            select e.id, e.captured_at, e.source, e.provider, e.endpoint, e.query_text, e.status_code, e.error,
                   e.latency_ms, e.origin_lat, e.origin_lng, op.name as origin_name,
                   e.destination_lat, e.destination_lng, dp.name as destination_name,
                   coalesce(array_length(e.place_ids, 1), 0) as results, e.client_ref, e.has_raw,
                   (select count(*) from map.routes r where r.search_event_id = e.id and r.captured_at = e.captured_at) as routes
            {base} order by e.captured_at desc limit %(limit)s offset %(offset)s""", args)
        total = None if format == "csv" else (await d.one(f"select count(*) as n {base}", args))["n"]
        return listing(items, total, limit, offset, format, "searches")

    @router.get("/api/ui/searches/{event_id}")
    async def search_detail(event_id: str, at: str, _=Depends(require_user)):
        d = db()
        t = _time(at, "at")
        ev = await d.one("select * from map.search_events where id = %s and captured_at = %s", (event_id, t))
        if not ev:
            raise HTTPException(404, "not found")
        ids = [i for i in [ev["origin_place_id"], ev["destination_place_id"]] + (ev["place_ids"] or []) if i]
        places = await d.query("select * from map.places where id = any(%s)", (ids,)) if ids else []
        order = {pid: i for i, pid in enumerate(ids)}
        places.sort(key=lambda p: order.get(p["id"], 999))
        routes = await d.query("""
            select r.id, r.captured_at, r.rank, r.provider, r.name, r.duration_seconds, r.distance_meters,
                   r.is_fastest, r.is_toll, r.toll_price, r.toll_currency, r.traffic_status,
                   s.traffic_light_count, s.speed_camera_count
            from map.routes r left join map.route_shapes s on s.id = r.shape_id
            where r.search_event_id = %s and r.captured_at = %s order by r.rank""", (event_id, t))
        wids = [ev[k] for k in ("origin_weather_id", "destination_weather_id", "midpoint_weather_id") if ev[k]]
        weather = await d.query("select * from map.weather_observations where id = any(%s)", (wids,)) if wids else []
        raw = None
        if ev["has_raw"]:
            r = await d.one("select response from map.raw_responses where search_event_id = %s and captured_at = %s",
                            (event_id, t))
            raw = r["response"] if r else None
        return {"event": ev, "places": places, "routes": routes, "weather": weather, "raw": raw}

    # -- places ----------------------------------------------------------------------

    @router.get("/api/ui/places")
    async def places(q: str | None = None, kind: str | None = None, order: str = "seen", bbox: str | None = None,
                     limit: int = 100, offset: int = 0, format: str = "json", _=Depends(require_user)):
        limit, offset = page_args(limit, offset, format)
        where, args = ["true"], {"limit": limit, "offset": offset}
        if q:
            where.append("(name ilike %(q)s or address ilike %(q)s or city ilike %(q)s)"); args["q"] = f"%{q}%"
        if kind:
            where.append("kind = %(kind)s"); args["kind"] = kind
        b = _bbox(bbox)
        if b:
            where.append("lat between %(s)s and %(n)s and lng between %(w)s and %(e)s")
            args.update(s=b[0], w=b[1], n=b[2], e=b[3])
        sort = {"seen": "seen_count desc", "recent": "last_seen_at desc", "name": "name"}.get(order, "seen_count desc")
        d = db()
        base = f"from map.places where {' and '.join(where)}"
        items = await d.query(f"""select id, name, address, city, state, country, lat, lng, kind, provider, provider_id,
                                  seen_count, first_seen_at, last_seen_at, details is not null as has_details
                                  {base} order by {sort} limit %(limit)s offset %(offset)s""", args)
        total = None if format == "csv" else (await d.one(f"select count(*) as n {base}", args))["n"]
        return listing(items, total, limit, offset, format, "places")

    # -- routes ----------------------------------------------------------------------

    @router.get("/api/ui/routes")
    async def routes(since: str | None = None, until: str | None = None, hours: float | None = None,
                     provider: str | None = None, min_cameras: int = 0, q: str | None = None,
                     limit: int = 100, offset: int = 0, format: str = "json", _=Depends(require_user)):
        start, end = window(since, until, hours)
        limit, offset = page_args(limit, offset, format)
        where = ["r.captured_at >= %(s)s", "r.captured_at < %(e)s"]
        args = {"s": start, "e": end, "limit": limit, "offset": offset}
        if provider:
            where.append("r.provider = %(provider)s"); args["provider"] = provider
        if min_cameras:
            where.append("coalesce(s.speed_camera_count, 0) >= %(mc)s"); args["mc"] = min_cameras
        if q:
            where.append("(r.name ilike %(q)s or op.name ilike %(q)s or dp.name ilike %(q)s)"); args["q"] = f"%{q}%"
        base = f"""from map.routes r
                   join map.search_events e on e.id = r.search_event_id and e.captured_at = r.captured_at
                   left join map.route_shapes s on s.id = r.shape_id
                   left join map.places op on op.id = e.origin_place_id
                   left join map.places dp on dp.id = e.destination_place_id
                   left join map.weather_observations w on w.id = e.origin_weather_id
                   where {' and '.join(where)}"""
        d = db()
        items = await d.query(f"""
            select r.id, r.captured_at, r.provider, r.rank, r.name, r.duration_seconds, r.distance_meters,
                   r.is_toll, r.toll_price, r.toll_currency, r.traffic_status, r.depart_at,
                   op.name as origin_name, e.origin_lat, e.origin_lng,
                   dp.name as destination_name, e.destination_lat, e.destination_lng,
                   s.traffic_light_count, s.speed_camera_count, w.condition as weather, w.temperature_c,
                   e.id as search_id
            {base} order by r.captured_at desc, r.rank limit %(limit)s offset %(offset)s""", args)
        total = None if format == "csv" else (await d.one(f"select count(*) as n {base}", args))["n"]
        return listing(items, total, limit, offset, format, "routes")

    @router.get("/api/ui/routes/{route_id}")
    async def route_detail(route_id: str, at: str, _=Depends(require_user)):
        d = db()
        t = _time(at, "at")
        r = await d.one("""
            select r.*, e.source, e.origin_lat, e.origin_lng, e.destination_lat, e.destination_lng,
                   e.origin_weather_id, e.destination_weather_id, e.midpoint_weather_id,
                   op.name as origin_name, dp.name as destination_name
            from map.routes r join map.search_events e on e.id = r.search_event_id and e.captured_at = r.captured_at
            left join map.places op on op.id = e.origin_place_id
            left join map.places dp on dp.id = e.destination_place_id
            where r.id = %s and r.captured_at = %s""", (route_id, t))
        if not r:
            raise HTTPException(404, "not found")
        shape, features, jams = None, [], []
        if r["shape_id"]:
            shape = await d.one("""select id, polyline, point_count, length_m, bbox, traffic_light_count,
                                      speed_camera_count, features_checked_at, seen_count, first_seen_at, last_seen_at
                                   from map.route_shapes where id = %s""", (r["shape_id"],))
            features = await d.query("""
                select f.*, coalesce(l.lat, c.lat) as lat, coalesce(l.lng, c.lng) as lng,
                       c.max_speed_kmh, c.camera_type, c.direction_deg
                from map.route_features f
                left join map.traffic_lights l on f.feature_type = 'traffic_light' and l.id = f.feature_id
                left join map.speed_cameras c on f.feature_type = 'speed_camera' and c.id = f.feature_id
                where f.shape_id = %s order by f.distance_along_m""", (r["shape_id"],))
            if shape and shape["bbox"]:
                s, w, n, e = shape["bbox"]
                jams = await d.query("""
                    select observed_at, street, level, severity, speed_kmh, length_meters, lat, lng, line, kind
                    from map.traffic_observations
                    where observed_at between %s and %s and lat between %s and %s and lng between %s and %s
                    order by level desc nulls last limit 200""",
                    (t - timedelta(minutes=15), t + timedelta(minutes=15), s, n, w, e))
        wids = [r[k] for k in ("origin_weather_id", "destination_weather_id", "midpoint_weather_id") if r[k]]
        weather = await d.query("select * from map.weather_observations where id = any(%s)", (wids,)) if wids else []
        for j in jams:
            j["points"] = geo.decode_polyline(j.pop("line")) if j.get("line") else []
        return {"route": r, "shape": shape, "features": features, "jams": jams, "weather": weather}

    # -- map (everything in a box) -----------------------------------------------------

    @router.get("/api/ui/map")
    async def map_layer(bbox: str, hours: float = 24, layers: str = "cameras,lights,jams,alerts",
                        _=Depends(require_user)):
        b = _bbox(bbox)
        if not b:
            raise HTTPException(400, "bbox is required")
        start, end = window(hours=hours)
        want = set(layers.split(","))
        s, w, n, e = b
        d = db()
        out = {}
        box = "lat between %(s)s and %(n)s and lng between %(w)s and %(e)s"
        args = {"s": s, "w": w, "n": n, "e": e, "since": start, "until": end}
        if "cameras" in want:
            out["cameras"] = await d.query(f"""select id, lat, lng, max_speed_kmh, direction_deg, direction_text,
                    camera_type, source from map.speed_cameras where {box} limit 3000""", args)
        if "lights" in want:
            out["lights"] = await d.query(f"select id, lat, lng from map.traffic_lights where {box} limit 5000", args)
        if "jams" in want:
            jams = await d.query(f"""
                select distinct on (coalesce(external_id, id::text)) observed_at, street, city, level, severity,
                       speed_kmh, length_meters, lat, lng, line, kind
                from map.traffic_observations
                where observed_at >= %(since)s and observed_at < %(until)s and {box}
                order by coalesce(external_id, id::text), observed_at desc limit 2000""", args)
            for j in jams:
                j["points"] = geo.decode_polyline(j.pop("line")) if j.get("line") else []
            out["jams"] = jams
        if "alerts" in want:
            out["alerts"] = await d.query(f"""select id, type, subtype, description, street, city, lat, lng,
                    reported_at, first_seen_at, last_seen_at, seen_count from map.road_alerts
                    where last_seen_at >= %(since)s and {box} order by last_seen_at desc limit 2000""", args)
        if "places" in want:
            out["places"] = await d.query(f"""select id, name, address, lat, lng, seen_count from map.places
                    where {box} order by seen_count desc limit 1000""", args)
        return out

    # -- cameras / lights ------------------------------------------------------------

    @router.get("/api/ui/cameras")
    async def cameras(q: str | None = None, limit: int = 200, offset: int = 0, format: str = "json",
                      _=Depends(require_user)):
        limit, offset = page_args(limit, offset, format)
        args = {"limit": limit, "offset": offset}
        where = "true"
        if q:
            where = "(c.id ilike %(q)s or c.tags::text ilike %(q)s)"; args["q"] = f"%{q}%"
        d = db()
        items = await d.query(f"""
            with on_routes as (
                select f->>1 as id, count(*) as shapes, sum(s.seen_count) as quotes
                from map.route_shapes s, jsonb_array_elements(s.features) f
                where s.speed_camera_count > 0 and f->>0 = 'C' group by 1)
            select c.id, c.lat, c.lng, c.max_speed_kmh, c.direction_deg, c.direction_text, c.camera_type, c.source,
                   c.first_seen_at, c.last_seen_at, coalesce(o.shapes, 0) as on_route_shapes,
                   coalesce(o.quotes, 0) as on_quotes
            from map.speed_cameras c left join on_routes o on o.id = c.id
            where {where} order by on_quotes desc, c.id
            limit %(limit)s offset %(offset)s""", args)
        total = None if format == "csv" else (await d.one(f"select count(*) as n from map.speed_cameras c where {where}", args))["n"]
        return listing(items, total, limit, offset, format, "speed_cameras")

    @router.get("/api/ui/lights")
    async def lights(limit: int = 200, offset: int = 0, format: str = "json", _=Depends(require_user)):
        limit, offset = page_args(limit, offset, format)
        d = db()
        items = await d.query("""select id, lat, lng, first_seen_at, last_seen_at from map.traffic_lights
                                 order by last_seen_at desc limit %s offset %s""", (limit, offset))
        total = None if format == "csv" else (await d.one("select count(*) as n from map.traffic_lights"))["n"]
        return listing(items, total, limit, offset, format, "traffic_lights")

    # -- traffic ---------------------------------------------------------------------

    @router.get("/api/ui/traffic")
    async def traffic(since: str | None = None, until: str | None = None, hours: float | None = None,
                      street: str | None = None, min_level: int = 0, limit: int = 200, offset: int = 0,
                      format: str = "json", _=Depends(require_user)):
        start, end = window(since, until, hours)
        limit, offset = page_args(limit, offset, format)
        where = ["observed_at >= %(s)s", "observed_at < %(e)s", "coalesce(level, 0) >= %(ml)s"]
        args = {"s": start, "e": end, "ml": min_level, "limit": limit, "offset": offset}
        if street:
            where.append("(street ilike %(st)s or city ilike %(st)s)"); args["st"] = f"%{street}%"
        base = f"from map.traffic_observations where {' and '.join(where)}"
        d = db()
        items = await d.query(f"""select observed_at, provider, kind, external_id, street, city, level, severity,
                                  speed_kmh, length_meters, lat, lng, seen_count, last_seen_at
                                  {base} order by observed_at desc, level desc nulls last
                                  limit %(limit)s offset %(offset)s""", args)
        total = None if format == "csv" else (await d.one(f"select count(*) as n {base}", args))["n"]
        return listing(items, total, limit, offset, format, "traffic")

    @router.get("/api/ui/traffic/by-hour")
    async def traffic_by_hour(days: int = 30, street: str | None = None, _=Depends(require_user)):
        start, end = window(hours=days * 24)
        args = {"s": start, "e": end, "tz": config.UI_TIMEZONE}
        extra = ""
        if street:
            extra = "and (street ilike %(st)s or city ilike %(st)s)"; args["st"] = f"%{street}%"
        d = db()
        hours = await d.query(f"""
            select extract(isodow from observed_at at time zone %(tz)s)::int as dow,
                   extract(hour from observed_at at time zone %(tz)s)::int as hour,
                   count(*) as jams, round(avg(level)::numeric, 2) as avg_level
            from map.traffic_observations where observed_at >= %(s)s and observed_at < %(e)s {extra}
            group by 1, 2 order by 1, 2""", args)
        streets = await d.query(f"""
            select street, city, count(*) as jams, round(avg(level)::numeric, 2) as avg_level,
                   round(avg(speed_kmh)::numeric, 1) as avg_speed_kmh
            from map.traffic_observations where observed_at >= %(s)s and observed_at < %(e)s and street is not null {extra}
            group by 1, 2 order by jams desc limit 25""", args)
        return {"by_hour": hours, "streets": streets}

    @router.get("/api/ui/alerts")
    async def alerts(since: str | None = None, until: str | None = None, hours: float | None = None,
                     type: str | None = None, limit: int = 200, offset: int = 0, format: str = "json",
                     _=Depends(require_user)):
        start, end = window(since, until, hours)
        limit, offset = page_args(limit, offset, format)
        where = ["last_seen_at >= %(s)s", "first_seen_at < %(e)s"]
        args = {"s": start, "e": end, "limit": limit, "offset": offset}
        if type:
            where.append("type = %(type)s"); args["type"] = type
        base = f"from map.road_alerts where {' and '.join(where)}"
        d = db()
        items = await d.query(f"""select id, type, subtype, description, street, city, lat, lng, thumbs_up,
                                  reported_at, first_seen_at, last_seen_at, seen_count
                                  {base} order by last_seen_at desc limit %(limit)s offset %(offset)s""", args)
        total = None if format == "csv" else (await d.one(f"select count(*) as n {base}", args))["n"]
        return listing(items, total, limit, offset, format, "road_alerts")

    # -- weather ---------------------------------------------------------------------

    @router.get("/api/ui/weather")
    async def weather_list(since: str | None = None, until: str | None = None, hours: float | None = None,
                           condition: str | None = None, forecast: bool | None = None,
                           limit: int = 200, offset: int = 0, format: str = "json", _=Depends(require_user)):
        start, end = window(since, until, hours)
        limit, offset = page_args(limit, offset, format)
        where = ["observed_at >= %(s)s", "observed_at < %(e)s"]
        args = {"s": start, "e": end, "limit": limit, "offset": offset}
        if condition:
            where.append("condition = %(c)s"); args["c"] = condition
        if forecast is not None:
            where.append("is_forecast = %(f)s"); args["f"] = forecast
        base = f"from map.weather_observations where {' and '.join(where)}"
        d = db()
        items = await d.query(f"""select id, observed_at, is_forecast, lat, lng, condition, temperature_c,
                                  apparent_temp_c, humidity_pct, precipitation_mm, rain_mm, weather_code,
                                  cloud_cover_pct, visibility_m, wind_speed_kmh, wind_gust_kmh, is_day
                                  {base} order by observed_at desc limit %(limit)s offset %(offset)s""", args)
        total = None if format == "csv" else (await d.one(f"select count(*) as n {base}", args))["n"]
        summary = [] if format == "csv" else await d.query(f"""
            select condition, count(*) as n, round(avg(temperature_c)::numeric, 1) as avg_temp_c,
                   round(sum(precipitation_mm)::numeric, 1) as total_precip_mm
            {base} group by 1 order by n desc""", args)
        if format == "csv":
            return as_csv(items, "weather")
        return {"items": items, "total": total, "limit": limit, "offset": offset, "summary": summary}

    # -- storage ---------------------------------------------------------------------

    @router.get("/api/ui/storage")
    async def storage(_=Depends(require_user)):
        d = db()
        tables = await d.query("""
            select coalesce(p.table_name, c.relname) as table_name, sum(pg_total_relation_size(c.oid))::bigint as bytes,
                   count(p.partition_name) as weeks, min(p.week_start) as oldest_week, max(p.week_start) as newest_week
            from pg_class c join pg_namespace n on n.oid = c.relnamespace
            left join map.partitions p on p.partition_name = c.relname
            where n.nspname = 'map' and c.relkind = 'r' group by 1 order by 2 desc""")
        weeks = await d.query("""select week_start, sum(bytes)::bigint as bytes from map.partitions
                                 group by 1 order by 1""")
        db_bytes = (await d.one("select pg_database_size(current_database()) as b"))["b"]
        from getmap import retention
        try:
            free_gb = round(retention.disk_free_gb(), 1)
        except OSError:
            free_gb = None
        return {"db_bytes": db_bytes, "free_gb": free_gb, "tables": tables, "weeks": weeks,
                "limits": {"max_db_gb": config.MAX_DB_GB, "min_free_disk_gb": config.MIN_FREE_DISK_GB},
                "retention_days": {"raw": config.RETAIN_RAW_DAYS, "searches_and_routes": config.RETAIN_EVENTS_DAYS,
                                   "traffic": config.RETAIN_TRAFFIC_DAYS, "shapes": config.RETAIN_SHAPES_DAYS,
                                   "alerts": config.RETAIN_ALERTS_DAYS, "weather": config.RETAIN_WEATHER_DAYS},
                "raw_policy": config.STORE_RAW}

    return router


def mount(app, state):
    app.include_router(build_router(state))
    app.mount("/ui/static", StaticFiles(directory=STATIC), name="ui-static")
