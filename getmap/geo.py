"""Small geometry helpers on (lat, lng) pairs. Pure functions, no I/O.

Distances use a local equirectangular projection, which is accurate to well
under a metre at the scales used here (a feature within tens of metres of a
road segment).
"""
import math

EARTH_M = 6_371_008.8
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def haversine_m(a, b):
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_M * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(a, b):
    """Initial bearing from a to b: 0 = north, 90 = east."""
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    y = math.sin(lng2 - lng1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lng2 - lng1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(deg):
    if deg is None:
        return None
    return COMPASS[int(((deg % 360) + 22.5) // 45) % 8]


def angle_diff(a, b):
    """Smallest difference between two bearings, 0..180."""
    d = abs((a - b) % 360)
    return 360 - d if d > 180 else d


def _xy(origin, p):
    """p in metres east / north of origin."""
    x = math.radians(p[1] - origin[1]) * math.cos(math.radians(origin[0])) * EARTH_M
    y = math.radians(p[0] - origin[0]) * EARTH_M
    return x, y


def nearest_on_line(point, line):
    """The closest place on a polyline to `point`.

    Returns (offset_m, distance_along_m, segment_index, bearing_deg) or None
    for a line with fewer than two points. bearing_deg is the direction of
    travel along the segment found.
    """
    if not line or len(line) < 2:
        return None
    best = None
    along = 0.0
    for i in range(len(line) - 1):
        a, b = line[i], line[i + 1]
        bx, by = _xy(a, b)
        px, py = _xy(a, point)
        seg2 = bx * bx + by * by
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, (px * bx + py * by) / seg2))
        dx, dy = px - t * bx, py - t * by
        dist = math.hypot(dx, dy)
        seg_len = math.sqrt(seg2)
        if best is None or dist < best[0]:
            best = (dist, along + t * seg_len, i)
        along += seg_len
    dist, at, i = best
    return dist, at, i, bearing_deg(line[i], line[i + 1])


def bbox(line, pad_m=0.0):
    """(south, west, north, east) around the points, padded by pad_m metres."""
    lats = [p[0] for p in line]
    lngs = [p[1] for p in line]
    s, n, w, e = min(lats), max(lats), min(lngs), max(lngs)
    if pad_m:
        dlat = math.degrees(pad_m / EARTH_M)
        dlng = math.degrees(pad_m / (EARTH_M * max(0.01, math.cos(math.radians((s + n) / 2)))))
        s, n, w, e = s - dlat, n + dlat, w - dlng, e + dlng
    return s, w, n, e


def thin(line, every_m):
    """Keep a point every `every_m` metres (plus the last): enough for weather
    sampling or for splitting a long route into Overpass boxes."""
    if not line:
        return []
    out, run = [line[0]], 0.0
    for prev, cur in zip(line, line[1:]):
        run += haversine_m(prev, cur)
        if run >= every_m:
            out.append(cur)
            run = 0.0
    if out[-1] != line[-1]:
        out.append(line[-1])
    return out


def midpoint(line):
    """The point halfway along a polyline, by distance."""
    if not line:
        return None
    if len(line) == 1:
        return line[0]
    total = sum(haversine_m(a, b) for a, b in zip(line, line[1:]))
    run = 0.0
    for a, b in zip(line, line[1:]):
        seg = haversine_m(a, b)
        if run + seg >= total / 2 and seg > 0:
            t = (total / 2 - run) / seg
            return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
        run += seg
    return line[-1]
