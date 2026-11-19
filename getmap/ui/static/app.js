/* get.map data viewer. Plain JS, no build step. Every value from the database
   is written with textContent / esc(), never as HTML. */
"use strict";

const S = { tz: "Asia/Kuala_Lumpur", tileUrl: "", tileAttr: "", tab: "overview", pages: {} };
const $ = (sel, root = document) => root.querySelector(sel);
const SOURCES = ["get_ride", "get_route", "get_route_backup", "get_route_apple"];
const SOURCE_NAMES = { get_ride: "GET.ride", get_route: "Waze (get.route)", get_route_backup: "Google (backup)", get_route_apple: "Apple" };
const SOURCE_COLORS = { get_ride: "var(--s1)", get_route: "var(--s2)", get_route_backup: "var(--s3)", get_route_apple: "var(--s4)" };
const ENDPOINTS = ["routes_search", "places_search", "places_details", "geocode_reverse", "jams_search", "alerts_search", "other"];
const JAM_COLORS = ["#7cb342", "#c0ca33", "#fbc02d", "#fb8c00", "#e53935", "#8e0000"];

// ---- helpers -------------------------------------------------------------------------

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtTime(iso, withDate = true) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: S.tz, day: withDate ? "2-digit" : undefined, month: withDate ? "short" : undefined,
    year: withDate ? "numeric" : undefined, hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(d);
}
const n = v => (v === null || v === undefined || v === "") ? "" : Number(v).toLocaleString("en-US");
const km = m => m == null ? "" : (m / 1000).toFixed(1) + " km";
const mins = s => s == null ? "" : Math.round(s / 60) + " min";
const ll = (lat, lng) => lat == null ? "" : `${Number(lat).toFixed(5)}, ${Number(lng).toFixed(5)}`;
function bytes(b) {
  if (b == null) return "";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; b = Number(b);
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i ? 1 : 0) + " " + u[i];
}
function period() {
  const p = $("#period").value;
  if (p !== "custom") return { hours: p };
  const out = {};
  if ($("#since").value) out.since = new Date($("#since").value).toISOString();
  if ($("#until").value) out.until = new Date($("#until").value).toISOString();
  return out;
}
function qs(params) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "" && v !== false) u.set(k, v);
  return u.toString();
}
async function api(path, params = {}) {
  const r = await fetch(`/api/ui/${path}?${qs(params)}`, { headers: { accept: "application/json" } });
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* not JSON */ }
    throw new Error(msg);
  }
  return r.json();
}
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;            // only ever given escaped / static strings
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined) e.append(kid instanceof Node ? kid : document.createTextNode(kid));
  return e;
}
function showError(root, err) { root.replaceChildren(el("div", { class: "error" }, "Could not load: " + err.message)); }
function tag(text, kind = "") { return el("span", { class: "tag " + kind }, text); }

function table(columns, items, onRow) {
  if (!items.length) return el("div", { class: "tablewrap" }, el("div", { class: "empty" }, "Nothing in this period."));
  const head = el("tr", {}, columns.map(c => el("th", { class: c.num ? "num" : "" }, c.label)));
  const body = items.map(it => {
    const tr = el("tr", { class: onRow ? "click" : "" }, columns.map(c => {
      const v = c.render ? c.render(it) : it[c.key];
      return el("td", { class: (c.num ? "num " : "") + (c.wrap ? "wrap" : "") }, v instanceof Node ? v : String(v ?? ""));
    }));
    if (onRow) tr.addEventListener("click", () => onRow(it));
    return tr;
  });
  return el("div", { class: "tablewrap" }, el("table", {}, el("thead", {}, head), el("tbody", {}, body)));
}
function pager(key, data, reload) {
  const { total, limit, offset } = data;
  const from = total ? offset + 1 : 0, to = Math.min(offset + limit, total);
  return el("div", { class: "pager" },
    `${n(from)}–${n(to)} of ${n(total)}`,
    el("button", { class: "ghost", disabled: offset <= 0 ? "" : null, onclick: () => { S.pages[key] = Math.max(0, offset - limit); reload(); } }, "‹ Prev"),
    el("button", { class: "ghost", disabled: to >= total ? "" : null, onclick: () => { S.pages[key] = offset + limit; reload(); } }, "Next ›"));
}
function csvLink(path, params) {
  return el("a", { class: "btn", href: `/api/ui/${path}?${qs({ ...params, format: "csv", limit: 50000, offset: 0 })}` }, "Download CSV");
}
function select(id, label, options, value, onchange) {
  const s = el("select", { id }, el("option", { value: "" }, "All"), options.map(o => el("option", { value: o[0] }, o[1])));
  s.value = value || "";
  s.addEventListener("change", onchange);
  return el("label", {}, label + " ", s);
}
function debounce(fn, ms = 350) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function kv(pairs) {
  return el("dl", { class: "kv" }, pairs.filter(p => p[1] !== undefined && p[1] !== null && p[1] !== "")
    .flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v instanceof Node ? v : String(v))]));
}

// ---- polyline (Google encoded, precision 5) --------------------------------------------

function decodePolyline(str) {
  const pts = []; let i = 0, lat = 0, lng = 0;
  while (i < str.length) {
    for (const which of [0, 1]) {
      let shift = 0, result = 0, b;
      do { b = str.charCodeAt(i++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
      const d = (result & 1) ? ~(result >> 1) : (result >> 1);
      if (which === 0) lat += d; else lng += d;
    }
    pts.push([lat / 1e5, lng / 1e5]);
  }
  return pts;
}

// ---- maps ------------------------------------------------------------------------------

function newMap(node, center = [3.139, 101.687], zoom = 12) {
  const m = L.map(node, { preferCanvas: true }).setView(center, zoom);
  if (S.tileUrl) L.tileLayer(S.tileUrl, { maxZoom: 19, attribution: esc(S.tileAttr) }).addTo(m);
  return m;
}
function cameraMarker(c, applies) {
  const cls = applies === true ? "" : applies === false ? "no" : (c.direction_deg == null ? "unk" : "");
  const arrow = c.direction_deg == null ? "●" : `<span class="arrow" style="transform:rotate(${Number(c.direction_deg)}deg)">▲</span>`;
  const icon = L.divIcon({ className: "", html: `<div class="cam ${cls}">${arrow}</div>`, iconSize: [18, 18], iconAnchor: [9, 9] });
  return L.marker([c.lat, c.lng], { icon }).bindPopup(
    `<b>Speed camera</b><br>${c.max_speed_kmh ? esc(c.max_speed_kmh) + " km/h<br>" : ""}` +
    `${c.camera_type ? esc(c.camera_type) + "<br>" : ""}` +
    `Checks: ${c.direction_deg == null ? "direction unknown" : esc(Math.round(c.direction_deg)) + "° (" + esc(c.direction_text || "") + ")"}<br>` +
    (applies === undefined ? "" : `Applies to this route: ${applies === null ? "unknown" : applies ? "yes" : "no"}<br>`) +
    `<span class="muted">${esc(c.id || c.feature_id)}</span>`);
}
function lightMarker(l) {
  return L.circleMarker([l.lat, l.lng], { radius: 4, color: "#fff", weight: 1.5, fillColor: "#2e7d32", fillOpacity: 1 })
    .bindPopup(`<b>Traffic light</b><br><span class="muted">${esc(l.id || l.feature_id)}</span>`);
}
function jamLine(j) {
  const color = JAM_COLORS[Math.max(0, Math.min(5, j.level ?? 2))];
  const pts = (j.points && j.points.length > 1) ? j.points : null;
  const popup = `<b>Jam</b> ${esc(j.street || "")}<br>Level ${esc(j.level ?? "?")} (${esc(j.severity || "")})<br>` +
    `${j.speed_kmh != null ? esc(j.speed_kmh) + " km/h, " : ""}${j.length_meters != null ? esc(j.length_meters) + " m" : ""}<br>` +
    `<span class="muted">${esc(fmtTime(j.observed_at))}</span>`;
  return pts ? L.polyline(pts, { color, weight: 5, opacity: .85 }).bindPopup(popup)
             : L.circleMarker([j.lat, j.lng], { radius: 6, color, fillOpacity: .8 }).bindPopup(popup);
}
function alertMarker(a) {
  return L.circleMarker([a.lat, a.lng], { radius: 6, color: "#fff", weight: 1.5, fillColor: "#7b1fa2", fillOpacity: 1 })
    .bindPopup(`<b>${esc(a.type || "Incident")}</b> ${esc(a.subtype || "")}<br>${esc(a.street || "")} ${esc(a.city || "")}<br>` +
      `${a.description ? esc(a.description) + "<br>" : ""}<span class="muted">first ${esc(fmtTime(a.first_seen_at))}, last ${esc(fmtTime(a.last_seen_at))}</span>`);
}
function placeMarker(p) {
  return L.circleMarker([p.lat, p.lng], { radius: 5, color: "#fff", weight: 1.5, fillColor: "#1f6feb", fillOpacity: 1 })
    .bindPopup(`<b>${esc(p.name || "Place")}</b><br>${esc(p.address || "")}<br><span class="muted">searched ${esc(n(p.seen_count))}×</span>`);
}

// ---- drawer ----------------------------------------------------------------------------

let drawerMap = null;
function openDrawer(title, body) {
  if (drawerMap) { drawerMap.remove(); drawerMap = null; }
  $("#drawer-title").textContent = title;
  $("#drawer-body").replaceChildren(body);
  $("#drawer").hidden = false;
}
function closeDrawer() { if (drawerMap) { drawerMap.remove(); drawerMap = null; } $("#drawer").hidden = true; }

// ---- charts ----------------------------------------------------------------------------

function stackedBars(series, bucket) {
  const times = [...new Set(series.map(r => r.t))].sort();
  if (!times.length) return el("div", { class: "empty" }, "No searches in this period.");
  const by = {}; let max = 1;
  for (const r of series) { (by[r.t] ||= {})[r.source] = r.n; }
  for (const t of times) max = Math.max(max, Object.values(by[t]).reduce((a, b) => a + b, 0));
  const W = 900, H = 200, pad = 28, bw = Math.max(1, (W - pad) / times.length - 1);
  let bars = "";
  times.forEach((t, i) => {
    let y = H - 18;
    for (const s of SOURCES) {
      const v = by[t][s] || 0; if (!v) continue;
      const h = (H - 30) * v / max; y -= h;
      bars += `<rect x="${pad + i * (bw + 1)}" y="${y}" width="${bw}" height="${h}" fill="${SOURCE_COLORS[s]}"><title>${esc(fmtTime(t))} · ${esc(SOURCE_NAMES[s])}: ${v}</title></rect>`;
    }
  });
  const label = (i) => esc(bucket === "hour" ? fmtTime(times[i], false) : fmtTime(times[i]).slice(0, 6));
  const ticks = [0, Math.floor(times.length / 2), times.length - 1].map(i =>
    `<text x="${pad + i * (bw + 1)}" y="${H - 3}">${label(i)}</text>`).join("");
  const svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Searches over time">
    <text x="0" y="12">${n(max)}</text><line x1="${pad}" x2="${W}" y1="${H - 18}" y2="${H - 18}" stroke="currentColor" opacity=".2"/>
    ${bars}${ticks}</svg>`;
  return el("div", {}, el("div", { html: svg }),
    el("div", { class: "chart-legend" }, SOURCES.map(s => el("span", { html: `<i style="background:${SOURCE_COLORS[s]}"></i>${esc(SOURCE_NAMES[s])}` }))));
}

// ---- tabs ------------------------------------------------------------------------------

const TABS = {};

TABS.overview = async root => {
  const d = await api("overview", { hours: period().hours || 24 });
  const c = d.counts, p = d.pipeline || {};
  const failRate = c.searches ? Math.round(100 * c.failed / c.searches) : 0;
  const card = (k, v, sub, bad) => el("div", { class: "card" + (bad ? " bad" : "") }, el("div", { class: "k" }, k), el("div", { class: "v" }, v), sub ? el("div", { class: "sub" }, sub) : null);
  root.replaceChildren(
    el("div", { class: "cards" },
      card("Searches", n(c.searches), "in this period"),
      card("Routes", n(c.routes), "in this period"),
      card("Failed", n(c.failed), failRate + "% of searches", failRate > 10),
      card("Jam readings", n(c.jams), "in this period"),
      card("Places", n(c.places), "all time"),
      card("Road shapes", n(c.route_shapes), "all time"),
      card("Traffic lights", n(c.traffic_lights), "on searched routes"),
      card("Speed cameras", n(c.speed_cameras), "on searched routes"),
      card("Incidents", n(c.road_alerts), "all time"),
      card("Weather readings", n(c.weather_observations), "all time"),
      card("Database", bytes(c.db_bytes), `limit ${d.disk.max_db_gb} GB`),
      card("Disk free", d.disk.free_gb == null ? "?" : d.disk.free_gb + " GB", `guard below ${d.disk.min_free_gb} GB`, d.disk.free_gb != null && d.disk.free_gb < d.disk.min_free_gb)),
    el("div", { class: "panel" }, el("h2", {}, "Searches over time"), stackedBars(d.series, d.bucket)),
    el("div", { class: "grid2" },
      el("div", { class: "panel" }, el("h2", {}, "By app and request"),
        table([
          { label: "App", render: r => SOURCE_NAMES[r.source] || r.source },
          { label: "Request", key: "endpoint" },
          { label: "Count", key: "n", num: true, render: r => n(r.n) },
          { label: "Failed", num: true, render: r => r.failed ? tag(n(r.failed), "bad") : "0" },
          { label: "Avg time", num: true, render: r => r.avg_latency_ms == null ? "" : n(r.avg_latency_ms) + " ms" },
        ], d.by_source)),
      el("div", { class: "panel" }, el("h2", {}, "Capture service"),
        kv([
          ["First capture", fmtTime(c.first_capture)], ["Last capture", fmtTime(c.last_capture)],
          ["Waiting in queue", n(p.queue)], ["Processed (since start)", n(p.processed)],
          ["Dropped (queue full)", p.dropped ? tag(n(p.dropped), "bad") : "0"],
          ["Failed", p.failed ? tag(n(p.failed), "bad") : "0"],
          ["Shapes looked up / reused", `${n(p.shapes_enriched)} / ${n(p.shapes_reused)}`],
          ["Enrichment errors", p.enrich_errors ? tag(n(p.enrich_errors), "warn") : "0"],
          ["Last enrichment error", p.last_enrich_error || "—"],
          ["Held while DB down", n(p.store_spooled)],
          ["Raw answers paused (disk)", p.raw_paused_for_disk ? tag("yes", "warn") : "no"],
        ]))));
};

// -- map --
let bigMap = null, bigLayer = null;
TABS.map = async () => {
  if (!bigMap) {
    bigMap = newMap($("#bigmap"));
    bigLayer = L.layerGroup().addTo(bigMap);
    bigMap.on("moveend", debounce(loadMapLayer, 250));
    document.querySelectorAll("[data-layer]").forEach(cb => cb.addEventListener("change", loadMapLayer));
  }
  setTimeout(() => { bigMap.invalidateSize(); loadMapLayer(); }, 50);
};
async function loadMapLayer() {
  if (!bigMap) return;
  const layers = [...document.querySelectorAll("[data-layer]:checked")].map(c => c.dataset.layer);
  if (bigMap.getZoom() < 11) { bigLayer.clearLayers(); $("#map-hint").textContent = "Zoom in to see data."; return; }
  const b = bigMap.getBounds();
  $("#map-hint").textContent = "Loading…";
  try {
    const d = await api("map", { bbox: [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].map(v => v.toFixed(5)).join(","),
                                 hours: period().hours || 24, layers: layers.join(",") });
    bigLayer.clearLayers();
    (d.jams || []).forEach(j => jamLine(j).addTo(bigLayer));
    (d.lights || []).forEach(l => lightMarker(l).addTo(bigLayer));
    (d.alerts || []).forEach(a => alertMarker(a).addTo(bigLayer));
    (d.places || []).forEach(p => placeMarker(p).addTo(bigLayer));
    (d.cameras || []).forEach(c => cameraMarker(c).addTo(bigLayer));
    const parts = [["cameras", "cameras"], ["lights", "lights"], ["jams", "jams"], ["alerts", "incidents"], ["places", "places"]]
      .filter(([k]) => d[k]).map(([k, label]) => `${n(d[k].length)} ${label}`);
    $("#map-hint").textContent = parts.join(" · ") + " (jams and incidents: the period above)";
  } catch (e) { $("#map-hint").textContent = "Could not load: " + e.message; }
}
function showOnMap(lat, lng, zoom = 16) {
  switchTab("map");
  setTimeout(() => bigMap && bigMap.setView([lat, lng], zoom), 80);
}

// -- searches --
TABS.searches = async root => {
  const f = S.f_search ||= { source: "", endpoint: "", q: "", failed: false };
  const load = async () => {
    const params = { ...period(), source: f.source, endpoint: f.endpoint, q: f.q, failed: f.failed, limit: 100, offset: S.pages.searches || 0 };
    const out = $("#search-out");
    try {
      const d = await api("searches", params);
      out.replaceChildren(table([
        { label: "Time", render: r => fmtTime(r.captured_at) },
        { label: "App", render: r => SOURCE_NAMES[r.source] || r.source },
        { label: "Request", key: "endpoint" },
        { label: "Provider", key: "provider" },
        { label: "Typed / from", wrap: true, render: r => r.query_text || r.origin_name || ll(r.origin_lat, r.origin_lng) },
        { label: "To", wrap: true, render: r => r.destination_name || ll(r.destination_lat, r.destination_lng) },
        { label: "Results", num: true, render: r => r.endpoint === "routes_search" ? n(r.routes) : n(r.results) },
        { label: "Status", render: r => (r.status_code >= 400 || r.error) ? tag(r.status_code || "error", "bad") : tag(r.status_code || "ok", "good") },
        { label: "Time taken", num: true, render: r => r.latency_ms == null ? "" : n(r.latency_ms) + " ms" },
      ], d.items, openSearch), pager("searches", d, load));
      $("#search-csv").replaceWith(Object.assign(csvLink("searches", params), { id: "search-csv" }));
    } catch (e) { showError(out, e); }
  };
  const reset = () => { S.pages.searches = 0; load(); };
  root.replaceChildren(
    el("div", { class: "toolbar" },
      select("f-src", "App", SOURCES.map(s => [s, SOURCE_NAMES[s]]), f.source, e => { f.source = e.target.value; reset(); }),
      select("f-ep", "Request", ENDPOINTS.map(s => [s, s]), f.endpoint, e => { f.endpoint = e.target.value; reset(); }),
      el("input", { type: "search", placeholder: "Search text or place…", value: f.q, oninput: debounce(e => { f.q = e.target.value; reset(); }) }),
      el("label", {}, el("input", { type: "checkbox", checked: f.failed ? "" : null, onchange: e => { f.failed = e.target.checked; reset(); } }), " Failed only"),
      el("span", { class: "grow" }), el("a", { id: "search-csv" })),
    el("div", { id: "search-out" }, el("div", { class: "empty" }, "Loading…")));
  load();
};
async function openSearch(r) {
  openDrawer("Search", el("div", { class: "empty" }, "Loading…"));
  try {
    const d = await api(`searches/${encodeURIComponent(r.id)}`, { at: r.captured_at });
    const e = d.event;
    const body = el("div", {},
      kv([["Time", fmtTime(e.captured_at)], ["App", SOURCE_NAMES[e.source] || e.source], ["Provider", e.provider], ["Request", e.endpoint],
          ["Typed", e.query_text], ["Status", e.status_code], ["Error", e.error], ["Time taken", e.latency_ms != null ? e.latency_ms + " ms" : ""],
          ["From", ll(e.origin_lat, e.origin_lng)], ["To", ll(e.destination_lat, e.destination_lng)],
          ["Planned for", fmtTime(e.depart_at || e.arrive_at)], ["User (hashed)", e.client_ref], ["App version", e.app_version],
          ["Search id", e.id]]),
      el("h3", {}, "Request"), el("pre", { class: "json" }, JSON.stringify(e.request, null, 2)));
    if (d.routes.length) body.append(el("h3", {}, "Routes"), table([
      { label: "#", render: x => x.rank + 1 }, { label: "Name", key: "name", wrap: true },
      { label: "Time", num: true, render: x => mins(x.duration_seconds) }, { label: "Distance", num: true, render: x => km(x.distance_meters) },
      { label: "Traffic", key: "traffic_status", wrap: true },
      { label: "Lights", num: true, key: "traffic_light_count" }, { label: "Cameras", num: true, key: "speed_camera_count" },
      { label: "Toll", num: true, render: x => x.toll_price != null ? `${x.toll_price} ${x.toll_currency || ""}` : (x.is_toll ? "yes" : "") },
    ], d.routes, x => openRoute(x)));
    if (d.places.length) body.append(el("h3", {}, "Places"), table([
      { label: "Name", key: "name", wrap: true }, { label: "Address", key: "address", wrap: true },
      { label: "Lat, lng", render: p => ll(p.lat, p.lng) }, { label: "Kind", key: "kind" },
    ], d.places, p => { closeDrawer(); showOnMap(p.lat, p.lng); }));
    if (d.weather.length) body.append(el("h3", {}, "Weather"), weatherTable(d.weather));
    if (d.raw) body.append(el("h3", {}, "Raw answer (kept for this search)"), el("pre", { class: "json" }, JSON.stringify(d.raw, null, 2)));
    openDrawer("Search · " + fmtTime(e.captured_at), body);
  } catch (err) { showError($("#drawer-body"), err); }
}

// -- routes --
TABS.routes = async root => {
  const f = S.f_route ||= { provider: "", q: "", minCam: 0 };
  const load = async () => {
    const params = { ...period(), provider: f.provider, q: f.q, min_cameras: f.minCam || "", limit: 100, offset: S.pages.routes || 0 };
    const out = $("#route-out");
    try {
      const d = await api("routes", params);
      out.replaceChildren(table([
        { label: "Time", render: r => fmtTime(r.captured_at) },
        { label: "Provider", key: "provider" },
        { label: "From", wrap: true, render: r => r.origin_name || ll(r.origin_lat, r.origin_lng) },
        { label: "To", wrap: true, render: r => r.destination_name || ll(r.destination_lat, r.destination_lng) },
        { label: "Via", key: "name", wrap: true },
        { label: "Time", num: true, render: r => mins(r.duration_seconds) },
        { label: "Distance", num: true, render: r => km(r.distance_meters) },
        { label: "Traffic", key: "traffic_status", wrap: true },
        { label: "Lights", num: true, key: "traffic_light_count" },
        { label: "Cameras", num: true, key: "speed_camera_count" },
        { label: "Weather", render: r => r.weather ? `${r.weather} ${r.temperature_c != null ? Math.round(r.temperature_c) + "°" : ""}` : "" },
      ], d.items, openRoute), pager("routes", d, load));
      $("#route-csv").replaceWith(Object.assign(csvLink("routes", params), { id: "route-csv" }));
    } catch (e) { showError(out, e); }
  };
  const reset = () => { S.pages.routes = 0; load(); };
  root.replaceChildren(
    el("div", { class: "toolbar" },
      select("f-prov", "Provider", [["waze", "Waze"], ["google_maps", "Google Maps"], ["apple_maps", "Apple Maps"], ["osrm", "OSRM"]], f.provider, e => { f.provider = e.target.value; reset(); }),
      el("input", { type: "search", placeholder: "Road or place…", value: f.q, oninput: debounce(e => { f.q = e.target.value; reset(); }) }),
      el("label", {}, "Cameras ≥ ", el("input", { type: "number", min: 0, style: "width:64px", value: f.minCam, oninput: debounce(e => { f.minCam = e.target.value; reset(); }) })),
      el("span", { class: "grow" }), el("a", { id: "route-csv" })),
    el("div", { id: "route-out" }, el("div", { class: "empty" }, "Loading…")));
  load();
};
async function openRoute(r) {
  openDrawer("Route", el("div", { class: "empty" }, "Loading…"));
  try {
    const d = await api(`routes/${encodeURIComponent(r.id)}`, { at: r.captured_at });
    const x = d.route;
    const mapNode = el("div", { class: "map" });
    const body = el("div", {},
      mapNode,
      el("div", { class: "legend" },
        el("span", { html: '<i class="bar" style="background:var(--accent)"></i> route' }),
        el("span", { html: '<i class="dot cam-yes"></i> camera for this direction' }),
        el("span", { html: '<i class="dot" style="background:#8a8f98"></i> other direction' }),
        el("span", { html: '<i class="dot" style="background:var(--warn)"></i> direction unknown' }),
        el("span", { html: '<i class="dot light"></i> traffic light' })),
      el("h3", {}, "Route"),
      kv([["Time", fmtTime(x.captured_at)], ["App", SOURCE_NAMES[x.source] || x.source], ["Provider", x.provider],
          ["From", (x.origin_name ? x.origin_name + " · " : "") + ll(x.origin_lat, x.origin_lng)],
          ["To", (x.destination_name ? x.destination_name + " · " : "") + ll(x.destination_lat, x.destination_lng)],
          ["Via", x.name], ["Drive time", mins(x.duration_seconds)], ["Distance", km(x.distance_meters)],
          ["Traffic", x.traffic_status], ["Toll", x.toll_price != null ? `${x.toll_price} ${x.toll_currency || ""}` : (x.is_toll ? "yes" : "no")],
          ["Planned for", fmtTime(x.depart_at)],
          ["Traffic lights", d.shape ? n(d.shape.traffic_light_count) : ""], ["Speed cameras", d.shape ? n(d.shape.speed_camera_count) : ""],
          ["This road shape quoted", d.shape ? n(d.shape.seen_count) + "×" : ""]]));
    const cams = d.features.filter(f => f.feature_type === "speed_camera");
    if (cams.length) body.append(el("h3", {}, "Speed cameras on the way"), table([
      { label: "After", num: true, render: f => km(f.distance_along_m) },
      { label: "Limit", num: true, render: f => f.max_speed_kmh ? f.max_speed_kmh + " km/h" : "" },
      { label: "You drive", key: "travel_direction" },
      { label: "Checks", render: f => f.direction_deg == null ? "unknown" : Math.round(f.direction_deg) + "°" },
      { label: "Applies", render: f => f.applies_to_travel == null ? tag("unknown", "warn") : f.applies_to_travel ? tag("yes", "bad") : tag("no") },
      { label: "Type", key: "camera_type" }, { label: "Lat, lng", render: f => ll(f.lat, f.lng) },
    ], cams));
    const lights = d.features.filter(f => f.feature_type === "traffic_light");
    if (lights.length) body.append(el("h3", {}, `Traffic lights on the way (${lights.length})`), table([
      { label: "After", num: true, render: f => km(f.distance_along_m) },
      { label: "You drive", key: "travel_direction" }, { label: "Lat, lng", render: f => ll(f.lat, f.lng) },
    ], lights));
    if (d.jams.length) body.append(el("h3", {}, "Jams nearby at that time"), table([
      { label: "Seen", render: j => fmtTime(j.observed_at) }, { label: "Street", key: "street", wrap: true },
      { label: "Level", num: true, key: "level" }, { label: "Speed", num: true, render: j => j.speed_kmh != null ? j.speed_kmh + " km/h" : "" },
      { label: "Length", num: true, render: j => j.length_meters != null ? n(j.length_meters) + " m" : "" },
    ], d.jams));
    if (d.weather.length) body.append(el("h3", {}, "Weather"), weatherTable(d.weather));
    openDrawer(`Route · ${x.provider || ""} · ${fmtTime(x.captured_at)}`, body);
    drawerMap = newMap(mapNode);
    const group = L.featureGroup().addTo(drawerMap);
    if (d.shape) L.polyline(decodePolyline(d.shape.polyline), { color: "#1f6feb", weight: 5, opacity: .85 }).addTo(group);
    d.jams.forEach(j => jamLine(j).addTo(group));
    lights.forEach(f => lightMarker(f).addTo(group));
    cams.forEach(f => cameraMarker(f, f.applies_to_travel).addTo(group));
    if (x.origin_lat != null) L.circleMarker([x.origin_lat, x.origin_lng], { radius: 7, color: "#fff", weight: 2, fillColor: "#2e7d32", fillOpacity: 1 }).bindPopup("Start").addTo(group);
    if (x.destination_lat != null) L.circleMarker([x.destination_lat, x.destination_lng], { radius: 7, color: "#fff", weight: 2, fillColor: "#c62828", fillOpacity: 1 }).bindPopup("End").addTo(group);
    setTimeout(() => { drawerMap.invalidateSize(); if (group.getLayers().length) drawerMap.fitBounds(group.getBounds().pad(0.1)); }, 50);
  } catch (err) { showError($("#drawer-body"), err); }
}

// -- places --
TABS.places = async root => {
  const f = S.f_place ||= { q: "", order: "seen" };
  const load = async () => {
    const params = { q: f.q, order: f.order, limit: 100, offset: S.pages.places || 0 };
    const out = $("#place-out");
    try {
      const d = await api("places", params);
      out.replaceChildren(table([
        { label: "Name", key: "name", wrap: true }, { label: "Address", key: "address", wrap: true },
        { label: "City", key: "city" }, { label: "Latitude", num: true, render: p => Number(p.lat).toFixed(6) },
        { label: "Longitude", num: true, render: p => Number(p.lng).toFixed(6) },
        { label: "Kind", key: "kind" }, { label: "Source", key: "provider" },
        { label: "Seen", num: true, render: p => n(p.seen_count) },
        { label: "Last seen", render: p => fmtTime(p.last_seen_at) },
      ], d.items, p => showOnMap(p.lat, p.lng)), pager("places", d, load));
      $("#place-csv").replaceWith(Object.assign(csvLink("places", params), { id: "place-csv" }));
    } catch (e) { showError(out, e); }
  };
  const reset = () => { S.pages.places = 0; load(); };
  const order = el("select", { onchange: e => { f.order = e.target.value; reset(); } },
    el("option", { value: "seen" }, "Most searched"), el("option", { value: "recent" }, "Most recent"), el("option", { value: "name" }, "Name"));
  order.value = f.order;
  root.replaceChildren(
    el("div", { class: "toolbar" },
      el("input", { type: "search", placeholder: "Name, address or city…", value: f.q, oninput: debounce(e => { f.q = e.target.value; reset(); }) }),
      el("label", {}, "Sort ", order), el("span", { class: "hint" }, "All time. Click a place to see it on the map."),
      el("span", { class: "grow" }), el("a", { id: "place-csv" })),
    el("div", { id: "place-out" }, el("div", { class: "empty" }, "Loading…")));
  load();
};

// -- cameras & lights --
TABS.cameras = async root => {
  const f = S.f_cam ||= { q: "" };
  const load = async () => {
    const params = { q: f.q, limit: 200, offset: S.pages.cameras || 0 };
    const out = $("#cam-out");
    try {
      const [d, lights] = await Promise.all([api("cameras", params), api("lights", { limit: 1 })]);
      $("#light-count").textContent = `${n(lights.total)} traffic lights stored.`;
      out.replaceChildren(table([
        { label: "Camera", key: "id" },
        { label: "Latitude", num: true, render: c => Number(c.lat).toFixed(6) },
        { label: "Longitude", num: true, render: c => Number(c.lng).toFixed(6) },
        { label: "Limit", num: true, render: c => c.max_speed_kmh ? c.max_speed_kmh + " km/h" : "" },
        { label: "Checks direction", render: c => c.direction_deg == null ? (c.direction_text || "unknown") : `${Math.round(c.direction_deg)}° ${c.direction_text ? "(" + c.direction_text + ")" : ""}` },
        { label: "Type", key: "camera_type" },
        { label: "On road shapes", num: true, render: c => n(c.on_route_shapes) },
        { label: "On quotes", num: true, render: c => n(c.on_quotes) },
        { label: "Last seen", render: c => fmtTime(c.last_seen_at) },
      ], d.items, c => showOnMap(c.lat, c.lng, 17)), pager("cameras", d, load));
    } catch (e) { showError(out, e); }
  };
  const reset = () => { S.pages.cameras = 0; load(); };
  root.replaceChildren(
    el("div", { class: "toolbar" },
      el("input", { type: "search", placeholder: "Camera id or tag…", value: f.q, oninput: debounce(e => { f.q = e.target.value; reset(); }) }),
      el("span", { class: "hint", id: "light-count" }),
      el("span", { class: "grow" }), csvLink("cameras", {}), csvLink("lights", {})),
    el("p", { class: "hint" }, "Speed cameras and traffic lights from OpenStreetMap, found along routes riders searched. Click a camera to see it on the map; the arrow shows the direction of traffic it checks."),
    el("div", { id: "cam-out" }, el("div", { class: "empty" }, "Loading…")));
  // the lights CSV link should say so
  root.querySelectorAll(".toolbar a.btn")[1].textContent = "Traffic lights CSV";
  root.querySelectorAll(".toolbar a.btn")[0].textContent = "Cameras CSV";
  load();
};

// -- traffic --
TABS.traffic = async root => {
  const f = S.f_traffic ||= { street: "", minLevel: 0, type: "" };
  root.replaceChildren(el("div", { class: "empty" }, "Loading…"));
  const days = Math.max(1, Math.round((Number(period().hours) || 720) / 24));
  const load = async () => {
    const params = { ...period(), street: f.street, min_level: f.minLevel, limit: 200, offset: S.pages.traffic || 0 };
    const aParams = { ...period(), type: f.type, limit: 200, offset: S.pages.alerts || 0 };
    try {
      const [byHour, jams, alerts] = await Promise.all([
        api("traffic/by-hour", { days, street: f.street }), api("traffic", params), api("alerts", aParams)]);
      $("#heat").replaceChildren(heatmap(byHour.by_hour));
      $("#streets").replaceChildren(table([
        { label: "Street", key: "street", wrap: true }, { label: "City", key: "city" },
        { label: "Jam readings", num: true, render: r => n(r.jams) },
        { label: "Avg level", num: true, key: "avg_level" }, { label: "Avg speed", num: true, render: r => r.avg_speed_kmh != null ? r.avg_speed_kmh + " km/h" : "" },
      ], byHour.streets));
      $("#jams").replaceChildren(table([
        { label: "Time (15 min)", render: r => fmtTime(r.observed_at) },
        { label: "Street", key: "street", wrap: true }, { label: "City", key: "city" },
        { label: "Level", render: r => r.level == null ? "" : el("span", { class: "tag", style: `background:${JAM_COLORS[r.level]};color:#fff` }, `${r.level} ${r.severity || ""}`) },
        { label: "Speed", num: true, render: r => r.speed_kmh != null ? r.speed_kmh + " km/h" : "" },
        { label: "Length", num: true, render: r => r.length_meters != null ? n(r.length_meters) + " m" : "" },
        { label: "Where", key: "kind" }, { label: "Seen", num: true, render: r => n(r.seen_count) },
      ], jams.items, r => r.lat != null && showOnMap(r.lat, r.lng, 15)), pager("traffic", jams, load));
      $("#alerts").replaceChildren(table([
        { label: "First seen", render: r => fmtTime(r.first_seen_at) }, { label: "Last seen", render: r => fmtTime(r.last_seen_at) },
        { label: "Type", render: r => tag(r.type || "", r.type === "ACCIDENT" || r.type === "ROAD_CLOSED" ? "bad" : r.type === "POLICE" ? "warn" : "") },
        { label: "Subtype", key: "subtype" }, { label: "Street", key: "street", wrap: true }, { label: "City", key: "city" },
        { label: "Description", key: "description", wrap: true },
      ], alerts.items, r => r.lat != null && showOnMap(r.lat, r.lng, 16)), pager("alerts", alerts, load));
      $("#jam-csv").replaceWith(Object.assign(csvLink("traffic", params), { id: "jam-csv", textContent: "Jams CSV" }));
      $("#alert-csv").replaceWith(Object.assign(csvLink("alerts", aParams), { id: "alert-csv", textContent: "Incidents CSV" }));
    } catch (e) { showError(root, e); }
  };
  const reset = () => { S.pages.traffic = 0; S.pages.alerts = 0; load(); };
  root.replaceChildren(
    el("div", { class: "toolbar" },
      el("input", { type: "search", placeholder: "Street or city…", value: f.street, oninput: debounce(e => { f.street = e.target.value; reset(); }) }),
      el("label", {}, "Level ≥ ", el("input", { type: "number", min: 0, max: 5, style: "width:56px", value: f.minLevel, oninput: debounce(e => { f.minLevel = e.target.value || 0; reset(); }) })),
      select("f-atype", "Incidents", ["ACCIDENT", "POLICE", "ROAD_CLOSED", "HAZARD", "WEATHERHAZARD", "CONSTRUCTION", "JAM", "MISC"].map(t => [t, t]), f.type, e => { f.type = e.target.value; reset(); }),
      el("span", { class: "grow" }), el("a", { id: "jam-csv" }), el("a", { id: "alert-csv" })),
    el("div", { class: "grid2" },
      el("div", { class: "panel" }, el("h2", {}, `When it jams (average level, last ${days} day${days === 1 ? "" : "s"}, ${S.tz} time)`), el("div", { id: "heat" })),
      el("div", { class: "panel" }, el("h2", {}, "Streets with the most jams"), el("div", { id: "streets" }))),
    el("h3", {}, "Jams"), el("div", { id: "jams" }),
    el("h3", {}, "Incidents (Waze)"), el("div", { id: "alerts" }));
  load();
};
function heatmap(rows) {
  if (!rows.length) return el("div", { class: "empty" }, "No jams in this period.");
  const days = ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const at = {}; rows.forEach(r => at[`${r.dow}-${r.hour}`] = r);
  const grid = el("div", { class: "heat" }, el("div"), [...Array(24).keys()].map(h => el("div", {}, h % 3 === 0 ? String(h) : "")));
  for (let d = 1; d <= 7; d++) {
    grid.append(el("div", {}, days[d]));
    for (let h = 0; h < 24; h++) {
      const r = at[`${d}-${h}`];
      const lvl = r ? Number(r.avg_level) : null;
      grid.append(el("div", { title: r ? `${days[d]} ${h}:00 · avg level ${r.avg_level} · ${r.jams} readings` : "no data",
        style: `background:${lvl == null ? "var(--line)" : JAM_COLORS[Math.min(5, Math.round(lvl))]};opacity:${lvl == null ? .4 : .55 + Math.min(.45, r.jams / 200)}` }));
    }
  }
  return grid;
}

// -- weather --
function weatherTable(items) {
  return table([
    { label: "Time", render: w => fmtTime(w.observed_at) + (w.is_forecast ? " (forecast)" : "") },
    { label: "Condition", key: "condition" },
    { label: "Temp", num: true, render: w => w.temperature_c != null ? w.temperature_c + " °C" : "" },
    { label: "Feels", num: true, render: w => w.apparent_temp_c != null ? w.apparent_temp_c + " °C" : "" },
    { label: "Rain", num: true, render: w => w.precipitation_mm != null ? w.precipitation_mm + " mm" : "" },
    { label: "Wind", num: true, render: w => w.wind_speed_kmh != null ? w.wind_speed_kmh + " km/h" : "" },
    { label: "Visibility", num: true, render: w => w.visibility_m != null ? n(w.visibility_m) + " m" : "" },
    { label: "Lat, lng", render: w => ll(w.lat, w.lng) },
  ], items, w => showOnMap(w.lat, w.lng, 13));
}
TABS.weather = async root => {
  const f = S.f_weather ||= { condition: "" };
  const load = async () => {
    const params = { ...period(), condition: f.condition, limit: 200, offset: S.pages.weather || 0 };
    try {
      const d = await api("weather", params);
      $("#w-sum").replaceChildren(table([
        { label: "Condition", key: "condition" }, { label: "Readings", num: true, render: r => n(r.n) },
        { label: "Avg temp", num: true, render: r => r.avg_temp_c != null ? r.avg_temp_c + " °C" : "" },
        { label: "Total rain", num: true, render: r => r.total_precip_mm != null ? r.total_precip_mm + " mm" : "" },
      ], d.summary));
      $("#w-out").replaceChildren(weatherTable(d.items), pager("weather", d, load));
      $("#w-csv").replaceWith(Object.assign(csvLink("weather", params), { id: "w-csv" }));
    } catch (e) { showError($("#w-out"), e); }
  };
  root.replaceChildren(
    el("div", { class: "toolbar" },
      select("f-cond", "Condition", ["clear", "partly_cloudy", "overcast", "fog", "drizzle", "rain", "heavy_rain", "thunderstorm"].map(c => [c, c]), f.condition,
        e => { f.condition = e.target.value; S.pages.weather = 0; load(); }),
      el("span", { class: "hint" }, "Weather at the start, end and middle of searched trips (per ~5 km, 15 min)."),
      el("span", { class: "grow" }), el("a", { id: "w-csv" })),
    el("div", { class: "panel" }, el("h2", {}, "Summary"), el("div", { id: "w-sum" })),
    el("h3", {}, "Readings"), el("div", { id: "w-out" }, el("div", { class: "empty" }, "Loading…")));
  load();
};

// -- storage --
TABS.storage = async root => {
  const d = await api("storage");
  const used = Number(d.db_bytes) / 2 ** 30, max = d.limits.max_db_gb;
  const W = 900, H = 160, weeks = d.weeks;
  const top = Math.max(1, ...weeks.map(w => Number(w.bytes)));
  const bw = weeks.length ? Math.min(48, Math.max(2, (W - 10) / weeks.length - 2)) : 0;
  const bars = weeks.map((w, i) => {
    const h = (H - 24) * Number(w.bytes) / top;
    return `<rect x="${5 + i * (bw + 2)}" y="${H - 18 - h}" width="${bw}" height="${h}" fill="var(--accent)"><title>week of ${esc(w.week_start)}: ${esc(bytes(w.bytes))}</title></rect>`;
  }).join("");
  const tick = weeks.length ? `<text x="5" y="${H - 3}">${esc(weeks[0].week_start)}</text><text x="${W - 80}" y="${H - 3}">${esc(weeks[weeks.length - 1].week_start)}</text>` : "";
  root.replaceChildren(
    el("div", { class: "cards" },
      el("div", { class: "card" }, el("div", { class: "k" }, "Database"), el("div", { class: "v" }, bytes(d.db_bytes)), el("div", { class: "sub" }, `of ${max} GB allowed`)),
      el("div", { class: "card" + (d.free_gb != null && d.free_gb < d.limits.min_free_disk_gb ? " bad" : "") }, el("div", { class: "k" }, "Disk free"), el("div", { class: "v" }, d.free_gb == null ? "?" : d.free_gb + " GB"), el("div", { class: "sub" }, `oldest weeks dropped below ${d.limits.min_free_disk_gb} GB`)),
      el("div", { class: "card" }, el("div", { class: "k" }, "Raw answers"), el("div", { class: "v" }, d.raw_policy), el("div", { class: "sub" }, `kept ${d.retention_days.raw} days`))),
    el("div", { class: "panel" }, el("h2", {}, "Database use"),
      el("div", { class: "meter" }, el("span", { style: `width:${Math.min(100, 100 * used / max)}%;background:${used / max > .85 ? "var(--bad)" : "var(--accent)"}` })),
      el("p", { class: "hint" }, `${used.toFixed(1)} GB of ${max} GB (${Math.round(100 * used / max)}%).`)),
    el("div", { class: "panel" }, el("h2", {}, "Size per week"), weeks.length ? el("div", { html: `<svg class="chart" viewBox="0 0 ${W} ${H}">${bars}${tick}</svg>` }) : el("div", { class: "empty" }, "No weeks yet.")),
    el("div", { class: "grid2" },
      el("div", { class: "panel" }, el("h2", {}, "Tables"), table([
        { label: "Table", key: "table_name" }, { label: "Size", num: true, render: t => bytes(t.bytes) },
        { label: "Weeks", num: true, render: t => t.weeks ? n(t.weeks) : "" },
        { label: "Oldest week", render: t => t.oldest_week || "" },
      ], d.tables)),
      el("div", { class: "panel" }, el("h2", {}, "Kept for (days)"), kv(Object.entries(d.retention_days).map(([k, v]) => [k.replaceAll("_", " "), v ? n(v) : "forever"])),
        el("p", { class: "hint" }, "Places, traffic lights and speed cameras are kept forever. Change these in getmap.env."))));
};

// ---- shell -----------------------------------------------------------------------------

function switchTab(name) {
  S.tab = name;
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("on", t.id === "tab-" + name));
  $("#range").style.visibility = ["places", "cameras", "storage"].includes(name) ? "hidden" : "visible";
  try { localStorage.setItem("getmap.tab", name); } catch (e) { /* private mode */ }
  render();
}
async function render() {
  const root = $("#tab-" + S.tab);
  try { await TABS[S.tab](root); } catch (e) { if (S.tab !== "map") showError(root, e); }
}

async function start() {
  try {
    const s = await api("settings");
    S.tz = s.timezone; S.tileUrl = s.tile_url; S.tileAttr = s.tile_attribution;
  } catch (e) { /* fall back to defaults */ }
  $("#tabs").addEventListener("click", e => { const b = e.target.closest("button[data-tab]"); if (b) switchTab(b.dataset.tab); });
  $("#period").addEventListener("change", () => { $("#custom").hidden = $("#period").value !== "custom"; S.pages = {}; render(); });
  $("#since").addEventListener("change", () => { S.pages = {}; render(); });
  $("#until").addEventListener("change", () => { S.pages = {}; render(); });
  $("#refresh").addEventListener("click", render);
  $("#drawer-close").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawer(); });
  let tab = "overview";
  try { tab = localStorage.getItem("getmap.tab") || tab; } catch (e) { /* private mode */ }
  switchTab(TABS[tab] ? tab : "overview");
}
start();
