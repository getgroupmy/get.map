"""Every setting, from environment variables."""
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name, default):
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


# Where captured data goes. Postgres / Supabase (session pooler URL) when set;
# otherwise, and whenever the database is unreachable, JSON lines in SPOOL_DIR
# that are replayed into the database once it is back.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
SPOOL_DIR = os.environ.get("SPOOL_DIR", "./spool")

# Callers send this in X-Capture-Key. Leave empty only on your own computer.
CAPTURE_API_KEY = os.environ.get("CAPTURE_API_KEY", "")

# Enrichment
ENRICH_OSM = _bool("ENRICH_OSM", True)
ENRICH_WEATHER = _bool("ENRICH_WEATHER", True)
OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
OPEN_METEO_URL = os.environ.get("OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast")
HTTP_TIMEOUT = _float("HTTP_TIMEOUT", 25.0)

# A traffic light / camera counts as "on the route" within this many metres of
# the route's road shape (cameras stand at the roadside, lights at junctions).
LIGHT_MATCH_M = _float("LIGHT_MATCH_M", 25.0)
CAMERA_MATCH_M = _float("CAMERA_MATCH_M", 45.0)
# A camera enforces the driver's direction when its direction is within this
# many degrees of the direction of travel.
CAMERA_ANGLE_TOLERANCE = _float("CAMERA_ANGLE_TOLERANCE", 60.0)
# What a camera's numeric `direction` tag means in your OSM area: "travel" (the
# direction of the traffic it checks, the usual mapping) or "facing" (where the
# lens points, i.e. towards oncoming traffic: travel = facing + 180).
CAMERA_DIRECTION_MEANS = os.environ.get("CAMERA_DIRECTION_MEANS", "travel")
# Overpass boxes: long routes are split into pieces about this long.
OSM_CHUNK_M = _float("OSM_CHUNK_M", 8000.0)
# Road furniture changes slowly: an area looked up within this many hours is
# not asked again (per ~1 km cell).
OSM_REFRESH_HOURS = _float("OSM_REFRESH_HOURS", 24 * 7)

# Weather: one reading per ~5 km cell per WEATHER_CACHE_MINUTES.
WEATHER_CACHE_MINUTES = _int("WEATHER_CACHE_MINUTES", 15)
WEATHER_MIDPOINT_KM = _float("WEATHER_MIDPOINT_KM", 25.0)   # also sample the middle of longer routes

# Worker
QUEUE_SIZE = _int("QUEUE_SIZE", 10000)
WORKERS = _int("WORKERS", 4)

# Transparent tap: GET /tap/<source>/<path> forwards to these and records the answer.
UPSTREAMS = {
    "get_route": os.environ.get("UPSTREAM_GET_ROUTE", ""),               # e.g. http://get-route-pool:8000
    "get_route_backup": os.environ.get("UPSTREAM_GET_ROUTE_BACKUP", ""),  # e.g. http://traffic-scraper:8000
    "get_route_apple": os.environ.get("UPSTREAM_GET_ROUTE_APPLE", ""),    # e.g. http://get-route-apple:8000
}
