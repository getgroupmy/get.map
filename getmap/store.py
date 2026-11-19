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
                    await write_bundle(cur, bundle)
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

async def write_bundle(cur, b):
    e = b["event"]
    await cur.execute(
        """insert into map.search_events (id, source, provider, endpoint, query_text, request, status_code,
               latency_ms, error, origin_lat, origin_lng, destination_lat, destination_lng, depart_at, arrive_at,
               client_ref, app_version, response, is_cached, captured_at, processed_at)
           values (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,now())
           on conflict (id) do nothing""",
        (e["id"], e["source"], e["provider"], e["endpoint"], e["query_text"], _j(e["request"]),
         e["status_code"], e["latency_ms"], e["error"],
         _pt(e["origin"], 0), _pt(e["origin"], 1), _pt(e["destination"], 0), _pt(e["destination"], 1),
         e["depart_at"], e["arrive_at"], e["client_ref"], e["app_version"], _j(e["response"]),
         e["is_cached"], e["captured_at"]))
    if cur.rowcount == 0:
        return   # already stored (a replay)

    for row, role, rank in b.get("places", []):
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
                   lat = excluded.lat, lng = excluded.lng,
                   details = coalesce(excluded.details, map.places.details),
                   last_seen_at = greatest(map.places.last_seen_at, excluded.last_seen_at),
                   seen_count = map.places.seen_count + 1
               returning id""",
            (row["place_key"], row["provider"], row["provider_id"], row["kind"], row["name"], row["address"],
             row["street"], row["city"], row["state"], row["postal_code"], row["country"], row["lat"], row["lng"],
             _j(row["details"]), e["captured_at"], e["captured_at"]))
        place_id = (await cur.fetchone())[0]
        await cur.execute(
            """insert into map.search_event_places (search_event_id, place_id, role, rank)
               values (%s,%s,%s,%s) on conflict do nothing""", (e["id"], place_id, role, rank))

    features = b.get("features") or {}
    for r in b.get("routes", []):
        poly = r.get("polyline") or []
        box = None
        if poly:
            lats, lngs = [p[0] for p in poly], [p[1] for p in poly]
            box = [min(lats), min(lngs), max(lats), max(lngs)]
        feats = features.get(r["id"])
        await cur.execute(
            """insert into map.routes (id, search_event_id, provider, rank, name, duration_seconds, distance_meters,
                   is_fastest, is_toll, toll_price, toll_currency, traffic_status, origin_lat, origin_lng,
                   destination_lat, destination_lng, depart_at, polyline, bbox, traffic_light_count,
                   speed_camera_count, captured_at)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)""",
            (r["id"], e["id"], r["provider"], r["rank"], r["name"], r["duration_seconds"], r["distance_meters"],
             r["is_fastest"], r["is_toll"], r["toll_price"], r["toll_currency"], r["traffic_status"],
             _pt(r["origin"], 0), _pt(r["origin"], 1), _pt(r["destination"], 0), _pt(r["destination"], 1),
             r["depart_at"], _j([list(p) for p in poly]) if poly else None, box,
             None if feats is None else sum(1 for f in feats if f["feature_type"] == "traffic_light"),
             None if feats is None else sum(1 for f in feats if f["feature_type"] == "speed_camera"),
             r["captured_at"]))

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
    for route_id, rows in features.items():
        for f in rows:
            await cur.execute(
                """insert into map.route_features (route_id, feature_type, feature_id, distance_along_m, offset_m,
                       travel_bearing_deg, travel_direction, applies_to_travel)
                   values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing""",
                (route_id, f["feature_type"], f["feature_id"], f["distance_along_m"], f["offset_m"],
                 f["travel_bearing_deg"], f["travel_direction"], f["applies_to_travel"]))

    for t in b.get("traffic", []):
        await cur.execute(
            """insert into map.traffic_observations (search_event_id, route_id, provider, kind, external_id, street,
                   city, level, severity, speed_kmh, length_meters, delay_seconds, status_text, lat, lng, line,
                   reported_at, observed_at)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)""",
            (e["id"], t["route_id"], t["provider"], t["kind"], t["external_id"], t["street"], t["city"],
             t["level"], t["severity"], t["speed_kmh"], t["length_meters"], t["delay_seconds"], t["status_text"],
             t["lat"], t["lng"], _j(t["line"]), t["reported_at"], t["observed_at"]))
    for a in b.get("alerts", []):
        await cur.execute(
            """insert into map.road_alerts (search_event_id, route_id, provider, external_id, type, subtype,
                   description, street, city, lat, lng, thumbs_up, reported_at, observed_at)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (e["id"], a["route_id"], a["provider"], a["external_id"], a["type"], a["subtype"], a["description"],
             a["street"], a["city"], a["lat"], a["lng"], a["thumbs_up"], a["reported_at"], a["observed_at"]))
    for w in b.get("weather", []):
        await cur.execute(
            """insert into map.weather_observations (search_event_id, role, provider, lat, lng, observed_at,
                   is_forecast, temperature_c, apparent_temp_c, humidity_pct, precipitation_mm, rain_mm,
                   weather_code, condition, cloud_cover_pct, visibility_m, wind_speed_kmh, wind_gust_kmh,
                   is_day, raw)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
            (e["id"], w["role"], w["provider"], w["lat"], w["lng"], w["observed_at"], w["is_forecast"],
             w["temperature_c"], w["apparent_temp_c"], w["humidity_pct"], w["precipitation_mm"], w["rain_mm"],
             w["weather_code"], w["condition"], w["cloud_cover_pct"], w["visibility_m"], w["wind_speed_kmh"],
             w["wind_gust_kmh"], w["is_day"], _j(w["raw"])))
