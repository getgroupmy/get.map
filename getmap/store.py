"""Writing a processed capture ("bundle") to Postgres, or to a JSON-lines
spool when there is no database or it is down. The spool is replayed into
the database later, so a database outage never loses a search.

A bundle is normalize.normalize()'s dict plus what the pipeline adds:
    lights, cameras        OSM features seen around the routes
    features               {route_id: [route_features rows]}
    weather                [weather_observations rows, with `role`]
"""
import asyncio
import json
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("getmap.store")


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def dumps(value):
    return json.dumps(value, default=_json_default, ensure_ascii=False)


def _j(value):
    """A value for a jsonb column."""
    return None if value is None else dumps(value)


def _pt(p, i):
    return p[i] if p else None


# ---------------------------------------------------------------------------

class SpoolStore:
    """Append-only JSON lines, one file per UTC day."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _path(self):
        return self.dir / f"capture-{time.strftime('%Y-%m-%d', time.gmtime())}.jsonl"

    async def write(self, bundle):
        line = dumps(bundle) + "\n"
        async with self._lock:
            await asyncio.to_thread(self._append, self._path(), line)

    @staticmethod
    def _append(path, line):
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    def files(self):
        return sorted(self.dir.glob("capture-*.jsonl"))

    async def close(self):
        pass


class PostgresStore:
    """psycopg 3, a few async connections; falls back to the spool on error."""

    def __init__(self, dsn, spool, size=4):
        self.dsn = dsn
        self.spool = spool
        self.size = size
        self._pool = None
        self.last_error = None
        self.written = 0
        self.spooled = 0
        self.weeks = set()       # weekly partitions known to exist

    async def _conn(self):
        import psycopg
        return await psycopg.AsyncConnection.connect(self.dsn, autocommit=False, prepare_threshold=None)

    async def _ensure_pool(self):
        if self._pool is None:
            self._pool = asyncio.Queue()
            for _ in range(self.size):
                self._pool.put_nowait(None)    # connections open lazily

    async def write(self, bundle):
        try:
            await self.write_db(bundle)
            self.written += 1
        except Exception as e:  # database down, bad row ...: keep it in the spool
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("database write failed, spooled: %s", self.last_error)
            await self.spool.write(bundle)
            self.spooled += 1

    async def write_db(self, bundle):
        await self._ensure_pool()
        conn = await self._pool.get()
        try:
            if conn is None or conn.closed:
                conn = await self._conn()
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await write_bundle(cur, bundle, self.weeks)
        except Exception:
            if conn is not None and not conn.closed:
                await conn.close()
            conn = None
            raise
        finally:
            self._pool.put_nowait(conn)

    async def shape_needs_features(self, shape_hash):
        """True unless the shape is stored with its lights / cameras checked recently."""
        from getmap import config
        await self._ensure_pool()
        conn = await self._pool.get()
        try:
            if conn is None or conn.closed:
                conn = await self._conn()
            cur = await conn.execute(
                """select 1 from map.route_shapes where shape_hash = %s
                   and features_checked_at > now() - make_interval(days => %s)""",
                (shape_hash, int(config.SHAPE_FEATURES_REFRESH_DAYS)))
            row = await cur.fetchone()
            await conn.commit()
            return row is None
        except Exception:
            if conn is not None and not conn.closed:
                await conn.close()
            conn = None
            raise
        finally:
            self._pool.put_nowait(conn)

    async def replay_spool(self):
        """Move spooled bundles into the database. Returns how many."""
        moved = 0
        for path in self.spool.files():
            work = path.with_suffix(".replaying")
            os.replace(path, work)
            left = []
            with open(work, encoding="utf-8") as f:
                lines = [ln for ln in f if ln.strip()]
            for i, ln in enumerate(lines):
                try:
                    await self.write_db(json.loads(ln))
                    moved += 1
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"
                    left = lines[i:]
                    break
            if left:
                with open(path, "a", encoding="utf-8") as f:
                    f.writelines(left)
            work.unlink()
            if left:
                break
        return moved

    async def close(self):
        if self._pool:
            while not self._pool.empty():
                c = self._pool.get_nowait()
                if c is not None and not c.closed:
                    await c.close()


# ---------------------------------------------------------------------------

def week_of(ts):
    from datetime import datetime, timedelta, timezone
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    d = ts.astimezone(timezone.utc).date()
    return d - timedelta(days=d.weekday())


async def _ensure_week(cur, ts, weeks):
    wk = week_of(ts)
    if wk not in weeks:
        await cur.execute("select map.ensure_week(%s)", (ts,))
        weeks.add(wk)


def _bucket(ts, minutes=15):
    from datetime import datetime
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return ts.replace(minute=ts.minute - ts.minute % minutes, second=0, microsecond=0)


def _line_text(line):
    from getmap import geo
    return geo.encode_polyline([tuple(p) for p in line]) if line else None


async def _place(cur, row, seen_at):
    await cur.execute(
        """insert into map.places (place_key, provider, provider_id, kind, name, address, street, city, state,
               postal_code, country, lat, lng, details, first_seen_at, last_seen_at)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
           on conflict (place_key) do update set
               name = coalesce(excluded.name, map.places.name),
               address = coalesce(excluded.address, map.places.address),
               street = coalesce(excluded.street, map.places.street),
               city = coalesce(excluded.city, map.places.city),
               state = coalesce(excluded.state, map.places.state),
               postal_code = coalesce(excluded.postal_code, map.places.postal_code),
               country = coalesce(excluded.country, map.places.country),
               details = coalesce(excluded.details, map.places.details),
               last_seen_at = greatest(map.places.last_seen_at, excluded.last_seen_at),
               seen_count = map.places.seen_count + 1
           returning id""",
        (row["place_key"], row["provider"], row["provider_id"], row["kind"], row["name"], row["address"],
         row["street"], row["city"], row["state"], row["postal_code"], row["country"], row["lat"], row["lng"],
         _j(row["details"]), seen_at, seen_at))
    return (await cur.fetchone())[0]


async def _weather(cur, w):
    await cur.execute(
        """insert into map.weather_observations (cell_y, cell_x, observed_at, is_forecast, provider, lat, lng,
               temperature_c, apparent_temp_c, humidity_pct, precipitation_mm, rain_mm, weather_code, condition,
               cloud_cover_pct, visibility_m, wind_speed_kmh, wind_gust_kmh, is_day)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           on conflict (cell_y, cell_x, observed_at, is_forecast) do update set captured_at = map.weather_observations.captured_at
           returning id""",
        (round(w["lat"] * 20), round(w["lng"] * 20), w["observed_at"], w["is_forecast"], w["provider"],
         w["lat"], w["lng"], w["temperature_c"], w["apparent_temp_c"], w["humidity_pct"], w["precipitation_mm"],
         w["rain_mm"], w["weather_code"], w["condition"], w["cloud_cover_pct"], w["visibility_m"],
         w["wind_speed_kmh"], w["wind_gust_kmh"], w["is_day"]))
    return (await cur.fetchone())[0]


async def _shape(cur, sh, seen_at):
    feats = sh.get("features")
    lights = None if feats is None else sum(1 for f in feats if f["feature_type"] == "traffic_light")
    cams = None if feats is None else sum(1 for f in feats if f["feature_type"] == "speed_camera")
    packed = None if feats is None else [
        ["L" if f["feature_type"] == "traffic_light" else "C", f["feature_id"], f["distance_along_m"], f["offset_m"],
         f["travel_bearing_deg"], f["travel_direction"], f["applies_to_travel"]] for f in feats]
    await cur.execute(
        """insert into map.route_shapes (shape_hash, polyline, point_count, length_m, start_lat, start_lng,
               end_lat, end_lng, bbox, traffic_light_count, speed_camera_count, features, features_checked_at,
               first_seen_at, last_seen_at)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,case when %s then now() end,%s,%s)
           on conflict (shape_hash) do update set
               last_seen_at = greatest(map.route_shapes.last_seen_at, excluded.last_seen_at),
               seen_count = map.route_shapes.seen_count + 1,
               traffic_light_count = coalesce(excluded.traffic_light_count, map.route_shapes.traffic_light_count),
               speed_camera_count = coalesce(excluded.speed_camera_count, map.route_shapes.speed_camera_count),
               features = coalesce(excluded.features, map.route_shapes.features),
               features_checked_at = coalesce(excluded.features_checked_at, map.route_shapes.features_checked_at)
           returning id""",
        (sh["hash"], sh["polyline"], sh["point_count"], sh["length_m"], sh["start"][0], sh["start"][1],
         sh["end"][0], sh["end"][1], sh["bbox"], lights, cams, _j(packed), feats is not None, seen_at, seen_at))
    return (await cur.fetchone())[0]


async def write_bundle(cur, b, weeks=None):
    weeks = set() if weeks is None else weeks
    e = b["event"]
    at = e["captured_at"]
    await _ensure_week(cur, at, weeks)
    await cur.execute("select 1 from map.search_events where captured_at = %s and id = %s", (at, e["id"]))
    if await cur.fetchone():
        return   # already stored (a replay)

    origin_place = dest_place = None
    results = []
    for row, role, rank in b.get("places", []):
        pid = await _place(cur, row, at)
        if role == "origin":
            origin_place = pid
        elif role == "destination":
            dest_place = pid
        else:
            results.append(pid)

    weather_ids = {}
    for w in b.get("weather", []):
        wid = await _weather(cur, w)
        weather_ids.setdefault("place" if w["role"] == "place" else w["role"], wid)

    for x in b.get("lights", []):
        await cur.execute(
            """insert into map.traffic_lights (id, lat, lng, tags) values (%s,%s,%s,%s::jsonb)
               on conflict (id) do update set lat = excluded.lat, lng = excluded.lng, tags = excluded.tags,
                   last_seen_at = now()""",
            (x["id"], x["lat"], x["lng"], _j(x.get("tags"))))
    for x in b.get("cameras", []):
        await cur.execute(
            """insert into map.speed_cameras (id, source, lat, lng, max_speed_kmh, direction_deg, direction_text,
                   camera_type, tags) values (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
               on conflict (id) do update set lat = excluded.lat, lng = excluded.lng,
                   max_speed_kmh = excluded.max_speed_kmh, direction_deg = excluded.direction_deg,
                   direction_text = excluded.direction_text, camera_type = excluded.camera_type,
                   tags = excluded.tags, last_seen_at = now()""",
            (x["id"], x["source"], x["lat"], x["lng"], x.get("max_speed_kmh"), x.get("direction_deg"),
             x.get("direction_text"), x.get("camera_type"), _j(x.get("tags"))))
    shape_ids = {}
    for h, sh in (b.get("shapes") or {}).items():
        shape_ids[h] = await _shape(cur, sh, at)

    raw = b.get("raw")
    await cur.execute(
        """insert into map.search_events (id, captured_at, source, provider, endpoint, query_text, request,
               status_code, latency_ms, error, origin_lat, origin_lng, destination_lat, destination_lng,
               depart_at, arrive_at, client_ref, app_version, is_cached, origin_place_id, destination_place_id,
               place_ids, origin_weather_id, destination_weather_id, midpoint_weather_id, has_raw)
           values (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (e["id"], at, e["source"], e["provider"], e["endpoint"], e["query_text"], _j(e["request"]),
         e["status_code"], e["latency_ms"], e["error"],
         _pt(e["origin"], 0), _pt(e["origin"], 1), _pt(e["destination"], 0), _pt(e["destination"], 1),
         e["depart_at"], e["arrive_at"], e["client_ref"], e["app_version"], e["is_cached"],
         origin_place, dest_place, results or None,
         weather_ids.get("origin") or weather_ids.get("place"), weather_ids.get("destination"),
         weather_ids.get("midpoint"), raw is not None))
    if raw is not None:
        await cur.execute("insert into map.raw_responses (captured_at, search_event_id, response) values (%s,%s,%s::jsonb)",
                          (at, e["id"], _j(raw)))

    for r in b.get("routes", []):
        await cur.execute(
            """insert into map.routes (id, captured_at, search_event_id, provider, rank, name, duration_seconds,
                   distance_meters, is_fastest, is_toll, toll_price, toll_currency, traffic_status, depart_at, shape_id)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (r["id"], at, e["id"], r["provider"], r["rank"], r["name"], r["duration_seconds"], r["distance_meters"],
             r["is_fastest"], r["is_toll"], r["toll_price"], r["toll_currency"], r["traffic_status"], r["depart_at"],
             shape_ids.get(r.get("shape_hash"))))

    for t in b.get("traffic", []):
        bucket = _bucket(t["observed_at"])
        await _ensure_week(cur, bucket, weeks)
        await cur.execute(
            """insert into map.traffic_observations (observed_at, provider, kind, external_id, street, city, level,
                   severity, speed_kmh, length_meters, delay_seconds, lat, lng, line, reported_at, last_seen_at)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict (observed_at, provider, external_id) do update set
                   level = coalesce(excluded.level, map.traffic_observations.level),
                   severity = coalesce(excluded.severity, map.traffic_observations.severity),
                   speed_kmh = coalesce(excluded.speed_kmh, map.traffic_observations.speed_kmh),
                   length_meters = coalesce(excluded.length_meters, map.traffic_observations.length_meters),
                   street = coalesce(map.traffic_observations.street, excluded.street),
                   last_seen_at = greatest(map.traffic_observations.last_seen_at, excluded.last_seen_at),
                   seen_count = map.traffic_observations.seen_count + 1""",
            (bucket, t["provider"], t["kind"], t["external_id"], t["street"], t["city"], t["level"], t["severity"],
             t["speed_kmh"], t["length_meters"], t["delay_seconds"], t["lat"], t["lng"], _line_text(t["line"]),
             t["reported_at"], t["observed_at"]))
    for a in b.get("alerts", []):
        sql = """insert into map.road_alerts (provider, external_id, type, subtype, description, street, city, lat, lng,
                     thumbs_up, reported_at, first_seen_at, last_seen_at)
                 values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""
        if a["external_id"]:
            sql += """ on conflict (provider, external_id) do update set
                     last_seen_at = greatest(map.road_alerts.last_seen_at, excluded.last_seen_at),
                     thumbs_up = coalesce(excluded.thumbs_up, map.road_alerts.thumbs_up),
                     seen_count = map.road_alerts.seen_count + 1"""
        await cur.execute(sql, (a["provider"], a["external_id"], a["type"], a["subtype"], a["description"], a["street"],
                                a["city"], a["lat"], a["lng"], a["thumbs_up"], a["reported_at"], a["observed_at"],
                                a["observed_at"]))
