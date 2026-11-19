# Connecting the four apps to get.map

get.map only receives data. Each app sends what it searched for and what came back to `POST /capture`. Every
send is fire-and-forget, so a slow or offline get.map never delays a rider or a fare.

| App | What it searches | Where to capture | Change |
|---|---|---|---|
| **get.route** (Waze) | routes, places, reverse geocode, jams, alerts | `pool_server.py`, once for all 16 containers | 2 lines |
| **get.route.backup** (Google Maps) | routes | `run.py` | 2 lines |
| **get.route.apple** (Apple Maps) | routes (`/routes/search`, `/directions`) | `main.py` | 2 lines |
| **get-ride** | place search, autocomplete, details, reverse geocode, route lines (`geo` function) | `supabase/functions/geo/index.ts` | ~25 lines |
| **get-ride** | fares through the route servers (`ai-route-proxy`) | already covered by the route servers; add the user link | 1 line |
| **get-ride** | Nominatim searches made directly from the phone | the app | see "Gaps" |

Use `GETMAP_KEY` = get.map's `CAPTURE_API_KEY` everywhere.

---

## 1. The three route servers

Copy `integrations/python/getmap_capture.py` into each repository (no new dependencies) and add:

**get.route**: `GET.route/pool_server.py`, after `app` is created:

```python
import getmap_capture
getmap_capture.install_bottle(source="get_route", app=app)
```

(Capturing at the dispatcher records each answer once, whichever of the 16 containers served it. If you run a
single container without the pool, put `getmap_capture.install_bottle(source="get_route")` in `run.py` after
`import routes`.)

**get.route.backup**: `run.py`, after `import routes`:

```python
import getmap_capture
getmap_capture.install_bottle(source="get_route_backup")
```

**get.route.apple**: `main.py`, after the routes are added:

```python
import getmap_capture
getmap_capture.install_fastapi(app, source="get_route_apple")
```

Then in each `deploy/nas/.env`:

```
GETMAP_URL=http://get-map:8090
GETMAP_KEY=<CAPTURE_API_KEY>
```

**No code change at all:** you can instead point the route server cards in GET.ride (Admin › Fare AI provider)
at get.map's tap, e.g. `https://map.getride.my/tap/get_route`. get.map forwards each call with its
`X-Route-Key`, returns the answer unchanged and records it. This adds one hop (a few ms on the NAS network)
to a 2 s budget, so the drop-in above is preferred.

## 2. get-ride: link searches to a (hashed) user

Copy `integrations/supabase/mapCapture.ts` to `supabase/functions/_shared/map_capture.ts` and set the
secrets:

```bash
supabase secrets set GETMAP_URL=https://map.getride.my GETMAP_KEY=... GETMAP_CLIENT_SALT=$(openssl rand -hex 32)
```

### `ai-route-proxy`: send the rider's hashed id to the route servers

In `_shared/waze_route.ts`, `askWazeServer`'s fetch headers:

```ts
headers: { "X-Route-Key": server.key, accept: "application/json",
           ...(clientRef ? { "X-Client-Ref": clientRef } : {}) },
```

(`clientRef = await clientRef(userId)` from `map_capture.ts`, passed down from `ai-route-proxy`). The route
servers' drop-in copies it onto the capture, so a fare's Waze, Google and Apple answers all carry the same
`client_ref`.

### `geo`: capture place searches, details, reverse geocodes and route lines

In `supabase/functions/geo/index.ts`, right after `const answer = await handleGeo(...)`:

```ts
import { captureMap, clientRef, ENDPOINT_OF_GEO_ACTION, geoRouteAsRoutesSearch, inBackground }
  from "../_shared/map_capture.ts";

// ...
const action = params.get("action") ?? "";
const endpoint = ENDPOINT_OF_GEO_ACTION[action];
if (endpoint) {
  const request = Object.fromEntries(params.entries());
  inBackground((async () => {
    const caller = await callerOf(req.headers.get("Authorization")).catch(() => null);
    await captureMap({
      source: "get_ride",
      endpoint,
      provider: params.get("provider") ?? undefined,
      request,
      response: endpoint === "routes_search"
        ? geoRouteAsRoutesSearch(answer.body as Record<string, unknown>, params.get("points"))
        : answer.body,
      status_code: answer.status,
      client_ref: await clientRef(caller?.id ?? null),
      app_version: params.get("v"),
    });
  })());
}
```

`callerOf` is the function's existing helper. Use whatever field holds the user id.

## 3. Backfill what GET.ride already logged

`fare_ai_responses` holds every route-server answer since the fare chain started:

```bash
DATABASE_URL=... python -m getmap.backfill --source-db "$GETRIDE_DB_URL" --since 2026-10-01
```

It cuts `raw_response` at 4,000 characters, so most old route shapes are lost. The backfill falls back to the
row's distance, time and endpoints, and it does not attach today's weather to old trips.

## Gaps this does not close by itself

- **Nominatim from the phone.** `GeoService` calls Nominatim directly (`geo_service.dart:162-172`), so those
  searches never reach a server. Either send them through `geo` (`provider=nominatim`), or have the app post
  a `places_search` event to `geo` after each one.
- **Apple MapKit on iOS** (`route_eta.dart`, MethodChannel `get_ride/route_eta`) runs on the device, so it is
  not seen either. It needs the same small "report back" call.
- **Search sessions.** To know which suggestion the rider picked, send `selected_place_id` with the
  `details` / `resolve` call, or record it with the ride request.
