"""capture event -> normalise -> enrich (lights, cameras, weather) -> store.

Events wait in a bounded queue and a few workers process them, so the
caller (a route server or a Supabase function) never waits on enrichment.
A failed enrichment is logged and the rest is still stored.

Road shapes: each route's polyline is thinned (SHAPE_TOLERANCE_M), encoded
and hashed. A shape already stored with its lights / cameras checked within
SHAPE_FEATURES_REFRESH_DAYS is not looked up again: the same trip quoted a
thousand times costs one Overpass lookup and one stored shape.
"""
import asyncio
import hashlib
import logging
import random
import time
from datetime import timedelta

from getmap import config, geo
from getmap.enrich import osm, weather
from getmap.normalize import normalize, now_utc, strip_images

log = logging.getLogger("getmap.pipeline")


def make_shape(line):
    """A full route line -> the stored shape (thinned + encoded) and its hash."""
    thin = geo.simplify(line, config.SHAPE_TOLERANCE_M)
    encoded = geo.encode_polyline(thin)
    s, w, n, e = geo.bbox(thin)
    return {
        "hash": hashlib.md5(encoded.encode()).hexdigest(),
        "polyline": encoded,
        "point_count": len(thin),
        "length_m": int(round(geo.length_m(line))),
        "start": thin[0], "end": thin[-1],
        "bbox": [round(s, 6), round(w, 6), round(n, 6), round(e, 6)],
        "features": None,            # set when lights / cameras were looked up
    }


class Pipeline:
    def __init__(self, store, client=None, enrich_osm=None, enrich_weather=None, clock=time.monotonic):
        self.store = store
        self.client = client
        self.enrich_osm = config.ENRICH_OSM if enrich_osm is None else enrich_osm
        self.enrich_weather = config.ENRICH_WEATHER if enrich_weather is None else enrich_weather
        self.clock = clock
        self.osm_cache = osm.OsmCache(clock)
        self.weather_cache = weather.WeatherCache(clock)
        self.queue = asyncio.Queue(maxsize=config.QUEUE_SIZE)
        self.stats = {"accepted": 0, "dropped": 0, "processed": 0, "failed": 0, "enrich_errors": 0,
                      "shapes_enriched": 0, "shapes_reused": 0}
        self.last_enrich_error = None
        self.raw_paused = False          # set by the disk guard when the disk is short
        self._shapes_done = {}           # hash -> clock time features were stored / known
        self._workers = []

    # ---- queue ---------------------------------------------------------------------

    def submit(self, event):
        """Queue an event; False when the queue is full (the event is dropped,
        never the caller's request)."""
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.stats["dropped"] += 1
            return False
        self.stats["accepted"] += 1
        return True

    def start(self, n=None):
        for _ in range(n or config.WORKERS):
            self._workers.append(asyncio.create_task(self._work()))

    async def stop(self):
        await self.queue.join()
        for w in self._workers:
            w.cancel()

    async def _work(self):
        while True:
            event = await self.queue.get()
            try:
                await self.process(event)
                self.stats["processed"] += 1
            except Exception:
                self.stats["failed"] += 1
                log.exception("capture failed")
            finally:
                self.queue.task_done()

    # ---- one event -----------------------------------------------------------------

    async def process(self, event):
        bundle = normalize(event)
        bundle.update({"lights": [], "cameras": [], "shapes": {}, "weather": [],
                       "raw": self._raw_of(bundle)})
        lines = {}
        for route in bundle["routes"]:
            line = route.pop("polyline", None) or []
            route["shape_hash"] = None
            if len(line) >= 2:
                shape = make_shape(line)
                route["shape_hash"] = shape["hash"]
                bundle["shapes"].setdefault(shape["hash"], shape)
                lines.setdefault(shape["hash"], line)
        if self.client is not None and self.enrich_osm:
            await self._add_road_features(bundle, lines)
        if self.client is not None and self.enrich_weather:
            await self._add_weather(bundle, lines)
        await self.store.write(bundle)
        now = self.clock()
        for h, shape in bundle["shapes"].items():
            if shape["features"] is not None:
                self._shapes_done[h] = now
        if len(self._shapes_done) > 200_000:
            self._shapes_done.clear()
        return bundle

    def _raw_of(self, bundle):
        e = bundle["event"]
        resp = e.pop("response", None)
        failed = e["status_code"] not in (None, 200) or e["error"]
        policy = "errors" if self.raw_paused and config.STORE_RAW != "none" else config.STORE_RAW
        keep = (policy == "all"
                or (policy in ("errors", "sample") and failed)
                or (policy == "sample" and random.random() < config.RAW_SAMPLE_RATE))
        return strip_images(resp) if keep else None

    def _note(self, what, err):
        self.stats["enrich_errors"] += 1
        self.last_enrich_error = f"{what}: {type(err).__name__}: {err}"
        log.warning("enrichment failed (%s)", self.last_enrich_error)

    async def _shape_needs_features(self, h):
        seen = self._shapes_done.get(h)
        if seen is not None and self.clock() - seen < config.SHAPE_FEATURES_REFRESH_DAYS * 86400:
            return False
        needs = getattr(self.store, "shape_needs_features", None)
        if needs is None:
            return True
        try:
            if not await needs(h):
                self._shapes_done[h] = self.clock()
                return False
        except Exception:
            pass
        return True

    async def _add_road_features(self, bundle, lines):
        lights, cameras = {}, {}
        for h, line in lines.items():
            if not await self._shape_needs_features(h):
                self.stats["shapes_reused"] += 1
                continue
            try:
                ls, cs = await osm.features_for_route(self.client, line, self.osm_cache)
            except Exception as e:
                self._note("overpass", e)
                continue
            rows = osm.match_to_route(line, ls, cs)
            bundle["shapes"][h]["features"] = rows
            self.stats["shapes_enriched"] += 1
            used = {r["feature_id"] for r in rows}
            lights.update({x["id"]: x for x in ls if x["id"] in used})
            cameras.update({x["id"]: x for x in cs if x["id"] in used})
        bundle["lights"] = list(lights.values())
        bundle["cameras"] = list(cameras.values())

    def _weather_points(self, bundle, lines):
        e = bundle["event"]
        pts = []
        if e["origin"]:
            pts.append(("origin", e["origin"]))
        if e["destination"]:
            pts.append(("destination", e["destination"]))
        first = next((r for r in bundle["routes"] if r.get("shape_hash") in lines), None)
        if first and (first.get("distance_meters") or 0) >= config.WEATHER_MIDPOINT_KM * 1000:
            pts.append(("midpoint", geo.midpoint(lines[first["shape_hash"]])))
        if not pts:   # a place search / geocode: the weather where the rider looked
            for row, role, _ in bundle["places"][:1]:
                pts.append(("place", (row["lat"], row["lng"])))
        return pts

    async def _add_weather(self, bundle, lines):
        e = bundle["event"]
        when = e["depart_at"] or e["arrive_at"]
        if when is not None and when < now_utc() + timedelta(hours=1):
            when = None   # "now"
        for role, (lat, lng) in self._weather_points(bundle, lines):
            row = self.weather_cache.get(lat, lng, when)
            if row is None:
                try:
                    row = await weather.fetch(self.client, lat, lng, when)
                except Exception as err:
                    self._note("open-meteo", err)
                    continue
                if row is None:
                    continue
                self.weather_cache.put(lat, lng, when, row)
            bundle["weather"].append({**row, "role": role})
