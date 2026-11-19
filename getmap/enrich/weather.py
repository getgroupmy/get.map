"""Weather at a point and time, from Open-Meteo (free, no key).

Now: the `current` block. A planned trip (depart_at more than an hour ahead):
the hourly forecast for that hour, marked is_forecast.
"""
from datetime import datetime, timezone

from getmap import config

CURRENT = ("temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,rain,weather_code,"
           "cloud_cover,visibility,wind_speed_10m,wind_gusts_10m,is_day")

# WMO weather codes -> a short condition
def condition(code):
    if code is None:
        return None
    code = int(code)
    if code == 0:
        return "clear"
    if code in (1, 2):
        return "partly_cloudy"
    if code == 3:
        return "overcast"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 57:
        return "drizzle"
    if 61 <= code <= 67 or 80 <= code <= 82:
        return "heavy_rain" if code in (65, 67, 82) else "rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if code >= 95:
        return "thunderstorm"
    return "unknown"


def params(lat, lng, when=None):
    p = {"latitude": f"{lat:.4f}", "longitude": f"{lng:.4f}", "timezone": "UTC",
         "wind_speed_unit": "kmh"}
    if when is None:
        p["current"] = CURRENT
    else:
        p["hourly"] = CURRENT
        p["start_hour"] = when.strftime("%Y-%m-%dT%H:00")
        p["end_hour"] = when.strftime("%Y-%m-%dT%H:00")
    return p


def _row(values, lat, lng, observed_at, is_forecast, raw):
    def g(k):
        v = values.get(k)
        return float(v) if isinstance(v, (int, float)) else None
    code = values.get("weather_code")
    return {
        "provider": "open_meteo", "lat": lat, "lng": lng,
        "observed_at": observed_at, "is_forecast": is_forecast,
        "temperature_c": g("temperature_2m"), "apparent_temp_c": g("apparent_temperature"),
        "humidity_pct": g("relative_humidity_2m"), "precipitation_mm": g("precipitation"),
        "rain_mm": g("rain"), "weather_code": int(code) if isinstance(code, (int, float)) else None,
        "condition": condition(code), "cloud_cover_pct": g("cloud_cover"),
        "visibility_m": g("visibility"), "wind_speed_kmh": g("wind_speed_10m"),
        "wind_gust_kmh": g("wind_gusts_10m"),
        "is_day": bool(values["is_day"]) if values.get("is_day") is not None else None,
        "raw": raw,
    }


def _time(text):
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse(body, lat, lng):
    """An Open-Meteo answer -> a weather_observations row (without role / event)."""
    body = body or {}
    if isinstance(body.get("current"), dict):
        cur = body["current"]
        return _row(cur, lat, lng, _time(cur["time"]), False, cur)
    hourly = body.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return None
    values = {k: (v[0] if isinstance(v, list) and v else None) for k, v in hourly.items() if k != "time"}
    return _row(values, lat, lng, _time(times[0]), True, {"time": times[0], **values})


async def fetch(client, lat, lng, when=None):
    r = await client.get(config.OPEN_METEO_URL, params=params(lat, lng, when), timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    return parse(r.json(), lat, lng)


class WeatherCache:
    """One reading per ~5 km cell and hour bucket for WEATHER_CACHE_MINUTES."""

    def __init__(self, clock):
        self._clock = clock
        self._data = {}

    @staticmethod
    def _key(lat, lng, when):
        return round(lat / 0.05), round(lng / 0.05), when.strftime("%Y%m%d%H") if when else "now"

    def get(self, lat, lng, when):
        hit = self._data.get(self._key(lat, lng, when))
        if hit and self._clock() - hit[0] < config.WEATHER_CACHE_MINUTES * 60:
            return hit[1]
        return None

    def put(self, lat, lng, when, row):
        if len(self._data) > 20000:
            self._data.clear()
        self._data[self._key(lat, lng, when)] = (self._clock(), row)
