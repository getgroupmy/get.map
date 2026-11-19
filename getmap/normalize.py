"""A captured request + answer -> rows for the map.* tables. Pure, no I/O.

Every route server answers in GET.route's format (get.route = Waze,
get.route.backup = Google Maps, get.route.apple = Apple Maps), so one parser
covers all three:

    /routes/search    {from, to, depart_at, routes: [{name, duration_seconds,
                       distance_meters, traffic_status, toll, jams, alerts,
                       polyline: [{lat, lng}], ...}], source, fetched_at}
    /places/search    {places|results|hits: [{id, kind, name, address,
                       location: {lat, lng}}]}  (or a bare list)
    /places/details   {id, kind, name, location, address: {...}, ...}
    /geocode/reverse  {location, address, street, city, timezone}
    /jams/search      {jams: [...]}   /alerts/search  {alerts: [...]}

GET.ride's own searches (Google / OSM / its admin place search) are sent in
the same shapes by its capture helper (integrations/supabase/mapCapture.ts).
"""
import uuid
from datetime import datetime, timezone

SOURCES = ("get_ride", "get_route", "get_route_backup", "get_route_apple")
PROVIDER_BY_SOURCE = {"get_route": "waze", "get_route_backup": "google_maps", "get_route_apple": "apple_maps"}
ENDPOINTS = ("routes_search", "places_search", "places_details", "geocode_reverse",
             "jams_search", "alerts_search", "other")
JAM_LEVELS = {0: "free_flow", 1: "light", 2: "moderate", 3: "heavy", 4: "very_heavy", 5: "standstill"}


# ---- small parsers ---------------------------------------------------------------------

def now_utc():
    return datetime.now(timezone.utc)


def parse_time(value):
    """ISO 8601 / epoch seconds / epoch ms -> aware datetime, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None   # drop NaN


def _int(v):
    f = _float(v)
    return int(round(f)) if f is not None else None


def _text(v):
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def point(raw):
    """{lat,lng} | {latitude,longitude} | {y,x} | [lat,lng] | "lat,lng" -> (lat, lng) or None."""
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = raw.split(",")
        if len(parts) != 2:
            return None
        raw = parts
    if isinstance(raw, (list, tuple)):
        if len(raw) < 2:
            return None
        lat, lng = _float(raw[0]), _float(raw[1])
    elif isinstance(raw, dict):
        if "location" in raw and isinstance(raw["location"], (dict, list, str)) and "lat" not in raw:
            return point(raw["location"])
        lat = _float(raw.get("lat", raw.get("latitude", raw.get("y"))))
        lng = _float(raw.get("lng", raw.get("lon", raw.get("longitude", raw.get("x")))))
    else:
        return None
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    if lat == 0 and lng == 0:
        return None
    return lat, lng


def line(raw):
    """A polyline in any of the shapes above -> [(lat, lng), ...]."""
    if not isinstance(raw, list):
        return []
    return [p for p in (point(x) for x in raw) if p]


# ---- no images -------------------------------------------------------------------------
# get.map stores data only (names, coordinates, times, numbers). Photo / image /
# icon / tile fields that some answers carry (Waze venue `images`, Google
# `photos`, map tile links) are removed before anything is stored.
IMAGE_KEYS = {"images", "image", "photos", "photo", "photo_reference", "icon", "icon_url", "icon_mask_base_uri",
              "iconbackgroundcolor", "thumbnail", "thumbnails", "tiles", "tile", "tile_url", "screenshot",
              "static_map", "staticmap", "street_view", "streetview"}


def strip_images(value):
    """A copy of value with every image-like field removed, at any depth."""
    if isinstance(value, dict):
        return {k: strip_images(v) for k, v in value.items() if str(k).lower() not in IMAGE_KEYS}
    if isinstance(value, list):
        return [strip_images(v) for v in value]
    return value


# ---- places ----------------------------------------------------------------------------

def place_key(provider, provider_id, name, lat, lng):
    if provider_id:
        return f"{provider or 'unknown'}:{provider_id}"
    return f"{(name or '').strip().lower()}:{lat:.4f},{lng:.4f}"


def _address_fields(addr):
    if isinstance(addr, dict):
        return {
            "address": _text(addr.get("formatted")) or _text(addr.get("address")),
            "street": _text(addr.get("street")),
            "city": _text(addr.get("city")),
            "state": _text(addr.get("state")),
            "postal_code": _text(addr.get("postal_code")),
            "country": _text(addr.get("country")),
        }
    return {"address": _text(addr), "street": None, "city": None, "state": None,
            "postal_code": None, "country": None}


def _provider_of_kind(kind, default):
    return {"waze_venue": "waze", "google_place": "google", "apple_place": "apple",
            "osm": "osm"}.get(kind or "", default)


def place_row(raw, default_provider, kind=None, details=False):
    """One place-like dict -> a map.places row, or None without coordinates."""
    if not isinstance(raw, dict):
        return None
    loc = point(raw.get("location") if raw.get("location") is not None else raw)
    if not loc:
        return None
    kind = kind or _text(raw.get("kind")) or "place"
    provider = _provider_of_kind(_text(raw.get("kind")), default_provider)
    provider_id = _text(raw.get("id") or raw.get("place_id") or raw.get("provider_id"))
    name = _text(raw.get("name")) or _text(raw.get("label"))
    row = {
        "provider": provider,
        "provider_id": provider_id,
        "kind": kind,
        "name": name,
        "lat": loc[0],
        "lng": loc[1],
        **_address_fields(raw.get("address")),
        "details": None,
    }
    for k in ("street", "city", "state", "country"):
        row[k] = row[k] or _text(raw.get(k))
    if details:
        row["details"] = strip_images({k: v for k, v in raw.items()
                                       if k not in ("id", "kind", "name", "location", "address")
                                       and v not in (None, [], {})}) or None
    row["place_key"] = place_key(provider, provider_id, name, loc[0], loc[1])
    return row


def _endpoint_point_place(raw, role, provider):
    """routes_search's from / to: {lat, lng, place: {name}} -> place row."""
    if not isinstance(raw, dict):
        return None
    loc = point(raw)
    if not loc:
        return None
    place = raw.get("place") if isinstance(raw.get("place"), dict) else {}
    merged = {**place, "location": {"lat": loc[0], "lng": loc[1]}}
    merged.setdefault("name", raw.get("name") or raw.get("address"))
    return place_row(merged, provider, kind=role)


# ---- the main entry --------------------------------------------------------------------

def normalize(event):
    """A capture event (see server.CaptureEvent) -> a dict of row lists.

    The event is {source, endpoint, request, response, status_code, ...}.
    Never raises on odd answers: whatever can't be read is skipped and the
    raw answer is still stored on the search_events row.
    """
    source = event["source"]
    endpoint = event.get("endpoint") or "other"
    req = event.get("request") or {}
    resp = event.get("response")
    observed = parse_time(event.get("captured_at")) or now_utc()
    provider = (event.get("provider")
                or (resp.get("source") if isinstance(resp, dict) else None)
                or PROVIDER_BY_SOURCE.get(source)
                or "unknown")
    event_id = event.get("id") or str(uuid.uuid4())

    origin = point(req.get("from") or req.get("origin"))
    dest = point(req.get("to") or req.get("destination"))
    if isinstance(resp, dict):
        origin = point(resp.get("from")) or origin
        dest = point(resp.get("to")) or dest

    out = {
        "event": {
            "id": event_id,
            "source": source,
            "provider": provider,
            "endpoint": endpoint,
            "query_text": _text(req.get("query") or req.get("q") or req.get("text")),
            "request": req,
            "status_code": event.get("status_code"),
            "latency_ms": _int(event.get("latency_ms")),
            "error": _text(event.get("error")),
            "origin": origin,
            "destination": dest,
            "depart_at": parse_time(req.get("depart_at") or (resp.get("depart_at") if isinstance(resp, dict) else None)),
            "arrive_at": parse_time(req.get("arrive_at")),
            "client_ref": _text(event.get("client_ref")),
            "app_version": _text(event.get("app_version")),
            "response": resp,
            "is_cached": bool(resp.get("stale") or resp.get("cached")) if isinstance(resp, dict) else None,
            "captured_at": observed,
        },
        "places": [],        # (row, role, rank)
        "routes": [],
        "traffic": [],
        "alerts": [],
    }
    ok = event.get("status_code") in (None, 200) and resp is not None
    if not ok:
        return out

    if endpoint == "routes_search":
        _routes(out, resp, provider, observed, req)
    elif endpoint == "places_search":
        hits = resp if isinstance(resp, list) else (
            resp.get("places") or resp.get("results") or resp.get("hits") or resp.get("predictions") or [])
        for rank, hit in enumerate(hits if isinstance(hits, list) else []):
            row = place_row(hit, provider, kind=None)
            if row:
                out["places"].append((row, "result", rank))
    elif endpoint == "places_details":
        body = resp.get("place") if isinstance(resp, dict) and isinstance(resp.get("place"), dict) else resp
        row = place_row(body, provider, details=True)
        if row:
            out["places"].append((row, "details", 0))
    elif endpoint == "geocode_reverse":
        if isinstance(resp, dict) and isinstance(resp.get("place"), dict):
            row = place_row(resp["place"], provider, kind="geocode")
            if row:
                out["places"].append((row, "reverse_geocode", 0))
        elif isinstance(resp, dict):
            loc = point(resp.get("location")) or point(req.get("location")) or point(req)
            if loc:
                addr = resp.get("address")
                name = _text(addr if isinstance(addr, str) else None) or _text(resp.get("street"))
                row = place_row({"name": name, "address": addr, "street": resp.get("street"),
                                 "city": resp.get("city"), "location": {"lat": loc[0], "lng": loc[1]}},
                                provider, kind="geocode")
                if row:
                    out["places"].append((row, "reverse_geocode", 0))
    elif endpoint in ("jams_search", "alerts_search"):
        if isinstance(resp, dict):
            for jam in resp.get("jams") or []:
                t = traffic_from_jam(jam, provider, observed, kind="area_jam")
                if t:
                    out["traffic"].append(t)
            for alert in resp.get("alerts") or []:
                a = alert_row(alert, provider, observed)
                if a:
                    out["alerts"].append(a)

    # origin / destination as places too (they are what riders searched for)
    if endpoint == "routes_search" and isinstance(resp, dict):
        for role in ("from", "to"):
            row = _endpoint_point_place(resp.get(role), "origin" if role == "from" else "destination", provider)
            if row:
                out["places"].append((row, "origin" if role == "from" else "destination", 0))
    return out


def _routes(out, resp, provider, observed, req):
    if not isinstance(resp, dict):
        return
    origin, dest = out["event"]["origin"], out["event"]["destination"]
    depart = out["event"]["depart_at"]
    for rank, r in enumerate(resp.get("routes") or []):
        if not isinstance(r, dict):
            continue
        poly = line(r.get("polyline") or r.get("points") or r.get("coords") or r.get("line"))
        toll = r.get("toll") if isinstance(r.get("toll"), dict) else {}
        route_id = str(uuid.uuid4())
        out["routes"].append({
            "id": route_id,
            "provider": provider,
            "rank": rank,
            "name": _text(r.get("name")),
            "duration_seconds": _int(r.get("duration_seconds")),
            "distance_meters": _int(r.get("distance_meters")),
            "is_fastest": r.get("is_fastest"),
            "is_toll": r.get("is_toll"),
            "toll_price": _float(toll.get("price")),
            "toll_currency": _text(toll.get("currency")),
            "traffic_status": _text(r.get("traffic_status")),
            "origin": origin or (poly[0] if poly else None),
            "destination": dest or (poly[-1] if poly else None),
            "depart_at": depart,
            "polyline": poly,
            "captured_at": observed,
        })
        # the route's own traffic text stays on the route row (routes.traffic_status)
        for jam in r.get("jams") or []:
            t = traffic_from_jam(jam, provider, observed, kind="route_jam", route_id=route_id)
            if t:
                out["traffic"].append(t)
        for alert in r.get("alerts") or []:
            a = alert_row(alert, provider, observed, route_id=route_id)
            if a:
                out["alerts"].append(a)


def traffic_from_jam(jam, provider, observed, kind, route_id=None):
    if not isinstance(jam, dict):
        return None
    pts = line(jam.get("line"))
    mid = pts[len(pts) // 2] if pts else point(jam.get("location"))
    level = _int(jam.get("level"))
    if level is None:
        level = _int(jam.get("severity"))   # on-route jams carry `severity` 0..5
    severity = _text(jam.get("severity")) if isinstance(jam.get("severity"), str) else JAM_LEVELS.get(level)
    return {
        "route_id": route_id, "provider": provider, "kind": kind,
        "external_id": _text(jam.get("id")),
        "street": _text(jam.get("street")), "city": _text(jam.get("city")),
        "level": level, "severity": severity,
        "speed_kmh": _float(jam.get("speed_kmh")),
        "length_meters": _int(jam.get("length_meters")),
        "delay_seconds": _int(jam.get("delay_seconds")),
        "status_text": None,
        "lat": mid[0] if mid else None, "lng": mid[1] if mid else None,
        "line": [list(p) for p in pts] or None,
        "reported_at": parse_time(jam.get("updated_at")),
        "observed_at": observed,
    }


def alert_row(alert, provider, observed, route_id=None):
    if not isinstance(alert, dict):
        return None
    loc = point(alert.get("location"))
    return {
        "route_id": route_id, "provider": provider,
        "external_id": _text(alert.get("id")),
        "type": _text(alert.get("type")), "subtype": _text(alert.get("subtype")),
        "description": _text(alert.get("description")),
        "street": _text(alert.get("street")), "city": _text(alert.get("city")),
        "lat": loc[0] if loc else None, "lng": loc[1] if loc else None,
        "thumbs_up": _int(alert.get("thumbs_up_count")),
        "reported_at": parse_time(alert.get("published_at")),
        "observed_at": observed,
    }
