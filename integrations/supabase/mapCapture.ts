// get.map capture for GET.ride's Supabase Edge Functions.
// Copy to supabase/functions/_shared/map_capture.ts in getgroupmy/get-ride.
//
// Secrets (supabase secrets set ...):
//   GETMAP_URL          https://map.getride.my   (get.map behind the NAS tunnel)
//   GETMAP_KEY          the same value as get.map's CAPTURE_API_KEY
//   GETMAP_CLIENT_SALT  any long random string: user ids are hashed with it,
//                       so get.map never holds a raw user id
//
// Never awaited on the answer path: hand the promise to EdgeRuntime.waitUntil
// (or the function's own `background(...)`), so a slow or down get.map never
// delays a rider.

type Json = Record<string, unknown>;

export type MapSource = "get_ride" | "get_route" | "get_route_backup" | "get_route_apple";
export type MapEndpoint =
  | "routes_search" | "places_search" | "places_details" | "geocode_reverse"
  | "jams_search" | "alerts_search" | "other";

export interface MapCaptureEvent {
  source: MapSource;
  endpoint: MapEndpoint;
  provider?: string;
  request: Json;
  response: unknown;
  status_code?: number | null;
  latency_ms?: number | null;
  error?: string | null;
  client_ref?: string | null;
  app_version?: string | null;
  captured_at?: string;
}

/** The fare chain's provider names -> get.map sources. */
export const SOURCE_OF_PROVIDER: Record<string, MapSource> = {
  waze: "get_route",
  gmap: "get_route_backup",
  apple: "get_route_apple",
};

/** The `geo` function's actions -> get.map endpoints (others are not captured). */
export const ENDPOINT_OF_GEO_ACTION: Record<string, MapEndpoint> = {
  search: "places_search",
  locate: "places_search",
  details: "places_details",
  reverse: "geocode_reverse",
  route: "routes_search",
};

export async function clientRef(userId: string | null | undefined): Promise<string | null> {
  const salt = Deno.env.get("GETMAP_CLIENT_SALT");
  if (!userId || !salt) return null;
  const bytes = new TextEncoder().encode(`${salt}:${userId}`);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return Array.from(digest.slice(0, 16), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** geo's {route:{distanceKm, durationMin, points}} -> GET.route's {from, to, routes}. */
export function geoRouteAsRoutesSearch(answer: Json, pointsParam: string | null): Json {
  const route = (answer?.route ?? null) as Json | null;
  const pts = (pointsParam ?? "").split(";").map((p) => p.split(",").map(Number));
  const at = (p?: number[]) => (p && p.length === 2 && p.every(Number.isFinite) ? { lat: p[0], lng: p[1] } : null);
  return {
    from: at(pts[0]),
    to: at(pts[pts.length - 1]),
    routes: route
      ? [{
        duration_seconds: Math.round(Number(route.durationMin) * 60) || null,
        distance_meters: Math.round(Number(route.distanceKm) * 1000) || null,
        polyline: route.points ?? null,
      }]
      : [],
  };
}

/** Send one or more events. Resolves (never rejects) within 5 s. */
export async function captureMap(events: MapCaptureEvent | MapCaptureEvent[]): Promise<void> {
  const url = Deno.env.get("GETMAP_URL");
  if (!url) return;
  const list = (Array.isArray(events) ? events : [events]).map((e) => ({
    captured_at: new Date().toISOString(),
    ...e,
  }));
  try {
    const res = await fetch(`${url.replace(/\/$/, "")}/capture`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Capture-Key": Deno.env.get("GETMAP_KEY") ?? "" },
      body: JSON.stringify({ events: list }),
      signal: AbortSignal.timeout(5000),
    });
    await res.body?.cancel();
  } catch (e) {
    console.warn("get.map capture failed:", e instanceof Error ? e.message : "failed");
  }
}

/** Run in the background when the runtime allows it. */
export function inBackground(p: Promise<unknown>): void {
  // deno-lint-ignore no-explicit-any
  const rt = (globalThis as any).EdgeRuntime;
  if (rt?.waitUntil) rt.waitUntil(p);
  else p.catch(() => {});
}
