"""Traffic lights and speed cameras along a route, from OpenStreetMap (Overpass).

For each route: split it into boxes, ask Overpass once per box for
  - nodes highway=traffic_signals                       (traffic lights)
  - nodes highway=speed_camera                          (speed cameras)
  - relations type=enforcement (maxspeed / average_speed / traffic_signals),
    whose `device` member is the camera and whose `from` -> `device` gives
    the direction of traffic it checks
then keep the ones within LIGHT_MATCH_M / CAMERA_MATCH_M of the road shape and
work out, for each, the driving direction at that point and (cameras) whether
it enforces that direction.

Parsing and matching are pure functions; only `fetch_overpass` does I/O.
"""
import re

from getmap import config, geo

CARDINAL = {"N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5,
            "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5, "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5}
_NUM = re.compile(r"^-?\d+(\.\d+)?$")


def overpass_query(box):
    s, w, n, e = box
    b = f"{s:.6f},{w:.6f},{n:.6f},{e:.6f}"
    return (f"[out:json][timeout:25];("
            f"node[highway=traffic_signals]({b});"
            f"node[highway=speed_camera]({b});"
            f"node[\"enforcement\"]({b});"
            f"relation[type=enforcement]({b});"
            f");out geom;")


def route_boxes(line, chunk_m=None, pad_m=None):
    """Boxes covering the route in pieces of about chunk_m, padded so roadside
    features are included."""
    chunk_m = chunk_m or config.OSM_CHUNK_M
    pad = pad_m if pad_m is not None else max(config.LIGHT_MATCH_M, config.CAMERA_MATCH_M) + 10
    if len(line) < 2:
        return []
    boxes, piece, run = [], [line[0]], 0.0
    for a, b in zip(line, line[1:]):
        piece.append(b)
        run += geo.haversine_m(a, b)
        if run >= chunk_m:
            boxes.append(geo.bbox(piece, pad))
            piece, run = [b], 0.0
    if len(piece) >= 2:
        boxes.append(geo.bbox(piece, pad))
    return boxes


def parse_direction(value):
    """An OSM direction tag -> (degrees or None, original text)."""
    if value is None:
        return None, None
    text = str(value).strip()
    first = text.split(";")[0].strip().upper()
    if _NUM.match(first):
        return float(first) % 360, text
    if first in CARDINAL:
        return float(CARDINAL[first]), text
    return None, text   # forward / backward / both: relative to a way we don't have


def _maxspeed(tags):
    raw = tags.get("maxspeed") or tags.get("maxspeed:forward") or ""
    m = re.match(r"^\s*(\d+)\s*(mph)?", str(raw))
    if not m:
        return None
    v = int(m.group(1))
    return round(v * 1.609) if m.group(2) else v


def parse_overpass(body):
    """Overpass JSON -> (lights, cameras). Lights: {id, lat, lng, tags}.
    Cameras: {id, source, lat, lng, max_speed_kmh, direction_deg,
    direction_text, camera_type, tags}."""
    elements = (body or {}).get("elements") or []
    nodes = {el["id"]: el for el in elements if el.get("type") == "node" and "lat" in el}
    lights, cameras = {}, {}

    for el in nodes.values():
        tags = el.get("tags") or {}
        oid = f"osm:node/{el['id']}"
        if tags.get("highway") == "traffic_signals":
            lights[oid] = {"id": oid, "lat": el["lat"], "lng": el["lon"], "tags": tags}
        if tags.get("highway") == "speed_camera" or tags.get("enforcement") in ("maxspeed", "average_speed"):
            deg, text = parse_direction(tags.get("direction") or tags.get("camera:direction"))
            if deg is not None and config.CAMERA_DIRECTION_MEANS == "facing":
                deg = (deg + 180) % 360
            cameras[oid] = {
                "id": oid, "source": "osm", "lat": el["lat"], "lng": el["lon"],
                "max_speed_kmh": _maxspeed(tags), "direction_deg": deg, "direction_text": text,
                "camera_type": tags.get("enforcement") or "maxspeed", "tags": tags,
            }

    for el in elements:
        if el.get("type") != "relation" or (el.get("tags") or {}).get("type") != "enforcement":
            continue
        tags = el.get("tags") or {}
        if tags.get("enforcement") not in ("maxspeed", "average_speed", "traffic_signals", "mindistance"):
            continue
        members = el.get("members") or []
        device = next((m for m in members if m.get("role") == "device" and m.get("type") == "node"), None)
        frm = next((m for m in members if m.get("role") == "from"), None)
        if not device:
            continue
        dlat, dlng = device.get("lat"), device.get("lon")
        if dlat is None and device.get("ref") in nodes:
            dlat, dlng = nodes[device["ref"]]["lat"], nodes[device["ref"]]["lon"]
        if dlat is None:
            continue
        deg = None
        if frm:
            flat, flng = frm.get("lat"), frm.get("lon")
            if flat is None and isinstance(frm.get("geometry"), list) and frm["geometry"]:
                g = frm["geometry"][-1]
                flat, flng = g.get("lat"), g.get("lon")
            if flat is not None and (flat, flng) != (dlat, dlng):
                deg = geo.bearing_deg((flat, flng), (dlat, dlng))   # traffic flows from -> device
        oid = f"osm:node/{device['ref']}"
        cam = cameras.get(oid) or {"id": oid, "source": "osm", "lat": dlat, "lng": dlng, "tags": {},
                                   "direction_text": None, "max_speed_kmh": None}
        cam.update({
            "camera_type": tags.get("enforcement"),
            "max_speed_kmh": _maxspeed(tags) or cam.get("max_speed_kmh"),
            "direction_deg": deg if deg is not None else cam.get("direction_deg"),
            "direction_text": cam.get("direction_text") or ("from relation" if deg is not None else None),
            "tags": {**cam.get("tags", {}), **{f"relation:{k}": v for k, v in tags.items()},
                     "relation:id": str(el.get("id"))},
        })
        cameras[oid] = cam
    return list(lights.values()), list(cameras.values())


def match_to_route(line, lights, cameras):
    """Features on the route -> route_features rows (without route_id),
    ordered by distance along the route."""
    rows = []
    for kind, items, limit in (("traffic_light", lights, config.LIGHT_MATCH_M),
                               ("speed_camera", cameras, config.CAMERA_MATCH_M)):
        for f in items:
            hit = geo.nearest_on_line((f["lat"], f["lng"]), line)
            if not hit or hit[0] > limit:
                continue
            offset, along, _, travel = hit
            applies = None
            if kind == "speed_camera" and f.get("direction_deg") is not None:
                applies = geo.angle_diff(f["direction_deg"], travel) <= config.CAMERA_ANGLE_TOLERANCE
            rows.append({
                "feature_type": kind,
                "feature_id": f["id"],
                "distance_along_m": int(round(along)),
                "offset_m": round(offset, 1),
                "travel_bearing_deg": round(travel, 1),
                "travel_direction": geo.compass(travel),
                "applies_to_travel": applies,
            })
    rows.sort(key=lambda r: r["distance_along_m"])
    return rows


async def fetch_overpass(client, box):
    r = await client.post(config.OVERPASS_URL, data={"data": overpass_query(box)},
                          timeout=config.HTTP_TIMEOUT, headers={"User-Agent": "get.map/1.0"})
    r.raise_for_status()
    return r.json()


async def features_for_route(client, line, cache=None):
    """All lights and cameras in the route's boxes (deduplicated), using
    `cache` (an OsmCache) to skip boxes looked up recently."""
    lights, cameras = {}, {}
    for box in route_boxes(line):
        body = cache.get(box) if cache else None
        if body is None:
            body = await fetch_overpass(client, box)
            if cache:
                cache.put(box, body)
        ls, cs = parse_overpass(body)
        lights.update({x["id"]: x for x in ls})
        cameras.update({x["id"]: x for x in cs})
    return list(lights.values()), list(cameras.values())


class OsmCache:
    """In-memory Overpass answers by box (rounded to ~100 m), for OSM_REFRESH_HOURS."""

    def __init__(self, clock):
        self._clock = clock
        self._data = {}

    @staticmethod
    def _key(box):
        return tuple(round(v, 3) for v in box)

    def get(self, box):
        hit = self._data.get(self._key(box))
        if hit and self._clock() - hit[0] < config.OSM_REFRESH_HOURS * 3600:
            return hit[1]
        return None

    def put(self, box, body):
        if len(self._data) > 5000:
            self._data.clear()
        self._data[self._key(box)] = (self._clock(), body)
