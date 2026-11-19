"""capture event -> normalise -> enrich (lights, cameras, weather) -> store.

Events wait in a bounded queue and a few workers process them, so the
caller (a route server or a Supabase function) never waits on enrichment.
A failed enrichment is logged and the rest is still stored.
"""
import asyncio
import logging
import time
from datetime import timedelta

from getmap import config, geo
from getmap.enrich import osm, weather
from getmap.normalize import normalize, now_utc

log = logging.getLogger("getmap.pipeline")


class Pipeline:
    def __init__(self, store, client=None, enrich_osm=None, enrich_weather=None, clock=time.monotonic):
        self.store = store
        self.client = client
        self.enrich_osm = config.ENRICH_OSM if enrich_osm is None else enrich_osm
        self.enrich_weather = config.ENRICH_WEATHER if enrich_weather is None else enrich_weather
        self.osm_cache = osm.OsmCache(clock)
        self.weather_cache = weather.WeatherCache(clock)
        self.queue = asyncio.Queue(maxsize=config.QUEUE_SIZE)
        self.stats = {"accepted": 0, "dropped": 0, "processed": 0, "failed": 0, "enrich_errors": 0}
        self.last_enrich_error = None
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
        bundle.update({"lights": [], "cameras": [], "features": {}, "weather": []})
        if self.client is not None and self.enrich_osm:
            await self._add_road_features(bundle)
        if self.client is not None and self.enrich_weather:
            await self._add_weather(bundle)
        await self.store.write(bundle)
        return bundle

    def _note(self, what, err):
        self.stats["enrich_errors"] += 1
        self.last_enrich_error = f"{what}: {type(err).__name__}: {err}"
        log.warning("enrichment failed (%s)", self.last_enrich_error)

    async def _add_road_features(self, bundle):
        lights, cameras = {}, {}
        for route in bundle["routes"]:
            line = route.get("polyline") or []
            if len(line) < 2:
                continue
            try:
                ls, cs = await osm.features_for_route(self.client, line, self.osm_cache)
            except Exception as e:
                self._note("overpass", e)
                continue
            rows = osm.match_to_route(line, ls, cs)
            bundle["features"][route["id"]] = rows
            used = {r["feature_id"] for r in rows}
            # keep only what is on (or right next to) a route searched
            lights.update({x["id"]: x for x in ls if x["id"] in used})
            cameras.update({x["id"]: x for x in cs if x["id"] in used})
        bundle["lights"] = list(lights.values())
        bundle["cameras"] = list(cameras.values())

    def _weather_points(self, bundle):
        e = bundle["event"]
        pts = []
        if e["origin"]:
            pts.append(("origin", e["origin"]))
        if e["destination"]:
            pts.append(("destination", e["destination"]))
        fastest = next((r for r in bundle["routes"] if r.get("polyline")), None)
        if fastest and (fastest.get("distance_meters") or 0) >= config.WEATHER_MIDPOINT_KM * 1000:
            pts.append(("midpoint", geo.midpoint(fastest["polyline"])))
        if not pts:   # a place search / geocode: the weather where the rider looked
            for row, role, _ in bundle["places"][:1]:
                pts.append(("place", (row["lat"], row["lng"])))
        return pts

    async def _add_weather(self, bundle):
        e = bundle["event"]
        when = e["depart_at"] or e["arrive_at"]
        if when is not None and when < now_utc() + timedelta(hours=1):
            when = None   # "now"
        for role, (lat, lng) in self._weather_points(bundle):
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
            bundle["weather"].append({**row, "lat": lat, "lng": lng, "role": role})
