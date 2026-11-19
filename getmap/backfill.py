"""Backfill from GET.ride's fare_ai_responses: every route-server answer
(Waze / Google Maps / Apple Maps) GET.ride has already logged.

    python -m getmap.backfill --source-db "$GETRIDE_DATABASE_URL" [--since 2026-10-01] [--limit 100000]

Notes:
- fare_ai_responses.raw_response is cut at 4,000 characters (2,000 per leg),
  so most route shapes (polyline) are lost; when the JSON can't be read,
  the row's own columns (origin, destination, distance, time) are used.
- Weather and traffic are NOT looked up for old rows: today's weather is
  not the weather of the trip. Traffic lights and cameras ARE matched when
  the route shape survived.
- Rows already imported are skipped (the event id is the row id).
"""
import argparse
import asyncio
import json

from getmap import config
from getmap.pipeline import Pipeline
from getmap.store import PostgresStore, SpoolStore

SOURCE = {"waze": "get_route", "gmap": "get_route_backup", "apple": "get_route_apple"}
PROVIDER = {"waze": "waze", "gmap": "google_maps", "apple": "apple_maps"}

QUERY = """
select id::text, created_at, provider, origin_lat, origin_lng, dest_lat, dest_lng, success, http_status,
       distance_km, duration_min, summary, error, latency_ms, raw_response
from public.fare_ai_responses
where provider = any(%s) and created_at >= %s
  and coalesce(is_test, false) = false
order by created_at
limit %s
"""


def event_from_row(row):
    (rid, created, provider, olat, olng, dlat, dlng, success, status, km, mins, summary, error, latency, raw) = row
    body = None
    if raw:
        first = raw.split("\n")[0]          # one line per leg; the first leg is a whole trip when there are no stops
        try:
            body = json.loads(first)
        except ValueError:
            body = None
    frm = {"lat": olat, "lng": olng} if olat is not None else None
    to = {"lat": dlat, "lng": dlng} if dlat is not None else None
    if not isinstance(body, dict) or not isinstance(body.get("routes"), list):
        body = None
        if success:
            body = {"from": frm, "to": to, "routes": [{
                "name": summary,
                "duration_seconds": round(mins * 60) if mins is not None else None,
                "distance_meters": round(km * 1000) if km is not None else None,
                "is_fastest": True,
            }], "backfilled_from_columns": True}
    return {
        "id": rid,
        "source": SOURCE[provider],
        "provider": PROVIDER[provider],
        "endpoint": "routes_search",
        "request": {"from": frm, "to": to, "backfill": "fare_ai_responses"},
        "response": body,
        "status_code": status if status is not None else (200 if success else None),
        "latency_ms": latency,
        "error": error,
        "captured_at": created.isoformat() if created else None,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-db", required=True, help="GET.ride's Postgres URL (read-only role is enough)")
    ap.add_argument("--since", default="1970-01-01")
    ap.add_argument("--limit", type=int, default=100000)
    ap.add_argument("--osm", action="store_true", help="also match traffic lights / cameras (slow, uses Overpass)")
    args = ap.parse_args()

    import httpx
    import psycopg

    with psycopg.connect(args.source_db) as src:
        rows = src.execute(QUERY, (list(SOURCE), args.since, args.limit)).fetchall()
    print(f"{len(rows)} rows to import")

    spool = SpoolStore(config.SPOOL_DIR)
    store = PostgresStore(config.DATABASE_URL, spool) if config.DATABASE_URL else spool
    async with httpx.AsyncClient() as client:
        pipe = Pipeline(store, client, enrich_osm=args.osm, enrich_weather=False)
        for i, row in enumerate(rows, 1):
            await pipe.process(event_from_row(row))
            if i % 500 == 0:
                print(f"  {i}/{len(rows)}")
    await store.close()
    print("done; spooled (database errors):", getattr(store, "spooled", 0))


if __name__ == "__main__":
    asyncio.run(main())
