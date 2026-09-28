"use strict";
/* Flight Tracker frontend: Leaflet map + live tracked flights (WebSocket) + ticker board. */

const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v) => typeof v === "number" && isFinite(v);
const dash = "—";

const S = {
  tracked: [],            // from WebSocket
  maxTracked: 10,
  sel: null,              // {kind, id}
  detail: null,           // full flight dict for the selection
  region: [],             // compact flights in view
  hasSchedule: false,
  usage: null,            // from /api/usage
  regionEvery: "follow",  // follow | manual | seconds (browser-side map refresh)
  regionLoaded: false,
  showFullPath: false,
  cats: {},               // category id -> label (from /api/meta)
  filters: { cats: [], airline: "", airport: "", airborne: false },
};
const CAT_COLOR = { commercial: "#a6a184", cargo: "#b090d6", military: "#d8483f", government: "#d6b23f",
                    private: "#8fae74", helicopter: "#5fb3c4", unknown: "#6e6b5a" };
const catTag = (c) => (c && S.cats[c] ? `<span class="cat-tag" style="--c:${CAT_COLOR[c]}">${esc(S.cats[c])}</span>` : "");

/* ---------- formatting ---------- */
const fmtAlt = (ft) => (num(ft) ? `${Math.round(ft).toLocaleString()} ft` : dash);
const fmtSpd = (kt) => (num(kt) ? `${Math.round(kt)} kt · ${Math.round(kt * 1.852)} km/h` : dash);
const fmtSpdShort = (kt) => (num(kt) ? `${Math.round(kt)} kt` : dash);
const fmtHdg = (deg, card) => (num(deg) ? `${Math.round(deg)}° ${card || ""}`.trim() : dash);
const fmtVr = (f) => (num(f) ? `${f > 0 ? "+" : ""}${Math.round(f)} ft/min` : dash);
const fmtKm = (km) => (num(km) ? `${Math.round(km).toLocaleString()} km` : dash);
function fmtDur(min) {
  if (!num(min)) return dash;
  const h = Math.floor(min / 60), m = Math.round(min % 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
}
function fmtTime(ts) {
  return num(ts) ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : dash;
}
function delayInfo(d) {
  const v = d?.delay_arr_min ?? d?.delay_dep_min;
  if (v == null) return { text: S.hasSchedule ? "On time" : dash, cls: "", title: S.hasSchedule ? "" : "No schedule source configured" };
  if (v <= 0) return { text: "On time", cls: "ok", title: "" };
  return { text: `+${v} min`, cls: v >= 15 ? "bad" : "warn", title: "" };
}
const STATUS = { airborne: "In flight", on_ground: "On ground", landed: "Landed", signal_lost: "Signal lost", searching: "Searching…" };

/* ---------- airline badge (logo file if present, else coloured initials) ---------- */
function hue(s) { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
function badge(al, fallback) {
  const code = (al?.iata || al?.icao || fallback || "?").toUpperCase();
  // Initials show until the logo file loads; a loaded logo replaces them (no overlap), a missing one is removed.
  const logo = al?.iata ? `<img src="logos/${esc(al.iata)}.png" alt="" loading="lazy" onload="this.parentNode.classList.add('has-logo')" onerror="this.remove()">` : "";
  return `<span class="badge" title="${esc(al?.name || code)}" style="background:hsl(${hue(code)} 45% 38%)"><span class="ini">${esc(code.slice(0, 3))}</span>${logo}</span>`;
}
function flightNo(f) { return f.flight_iata || f.callsign || f.label || f.id; }

/* ---------- API ---------- */
async function api(path, opts) {
  // no-store: a stale cached response for the same URL (e.g. re-selecting a flight, or the browser's
  // own heuristics) must never be served in place of this flight's current data.
  const r = await fetch(path, { cache: "no-store", ...opts });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* ignore */ }
    throw new Error(msg);
  }
  return r.json();
}

/* ---------- map ---------- */
const map = L.map("map", { worldCopyJump: true, minZoom: 2, zoomControl: true }).setView([39, -98], 4);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

const PLANE_SVG = '<svg viewBox="0 0 24 24"><path d="M12 1.5c.9 0 1.6.9 1.6 2v6.2l8.4 5v2.3l-8.4-2.4v4.6l2.2 1.7V22L12 21l-3.8 1v-1.6l2.2-1.7v-4.6L2 16.9v-2.3l8.4-5V3.5c0-1.1.7-2 1.6-2z"/></svg>';
function planeIcon(cls, rot, size) {
  return L.divIcon({ className: `plane ${cls}`, iconSize: [size, size], iconAnchor: [size / 2, size / 2],
    html: `<div style="width:${size}px;height:${size}px;transform:rotate(${num(rot) ? rot : 0}deg)">${PLANE_SVG}</div>` });
}
const regionLayer = L.layerGroup().addTo(map);
const trackedLayer = L.layerGroup().addTo(map);
const routeLayer = L.layerGroup().addTo(map);
const regionMarkers = new Map();   // hex -> marker
const trackedMarkers = new Map();  // id -> marker

function selKey(f) { return f.callsign ? { kind: "callsign", id: f.callsign.toUpperCase() } : { kind: "hex", id: f.hex }; }
function isSel(f) { return S.sel && (S.sel.id === (f.callsign || "").toUpperCase() || S.sel.id === f.hex); }

function renderRegion() {
  const seen = new Set();
  const trackedHex = new Set(S.tracked.map((t) => t.hex).filter(Boolean));
  const b = map.getBounds();
  let list = S.region.filter((f) => !trackedHex.has(f.hex));
  if (list.length > 700) {  // keep DOM light: nearest to centre
    const c = map.getCenter();
    list = list.map((f) => [(f.lat - c.lat) ** 2 + (f.lon - c.lng) ** 2, f]).sort((a, z) => a[0] - z[0]).slice(0, 700).map((x) => x[1]);
  }
  for (const f of list) {
    seen.add(f.hex);
    const cls = `cat-${f.cat || "commercial"} ` + (f.on_ground ? "ground " : "") + (isSel(f) ? "sel" : "");
    const size = map.getZoom() >= 8 ? 26 : map.getZoom() >= 6 ? 20 : 14;
    let m = regionMarkers.get(f.hex);
    if (!m) {
      m = L.marker([f.lat, f.lon], { icon: planeIcon(cls, f.track, size), keyboard: false });
      m.on("click", () => selectFlight(m._f));
      m.bindTooltip("", { direction: "top", offset: [0, -8] });
      m.addTo(regionLayer);
      regionMarkers.set(f.hex, m);
    } else {
      m.setLatLng([f.lat, f.lon]);
      m.setIcon(planeIcon(cls, f.track, size));
    }
    m._f = f;
    m.setTooltipContent(`<b>${esc(f.callsign || f.hex)}</b> ${f.origin || f.dest ? esc((f.origin || "?") + " → " + (f.dest || "?")) : ""}<br>${esc(S.cats[f.cat] || "")} · ${esc(f.type || "")} ${num(f.alt_ft) ? Math.round(f.alt_ft).toLocaleString() + " ft" : ""}`);
  }
  for (const [hex, m] of regionMarkers) if (!seen.has(hex)) { regionLayer.removeLayer(m); regionMarkers.delete(hex); }
}

function renderTrackedMarkers() {
  const seen = new Set();
  for (const t of S.tracked) {
    const s = t.state;
    if (!s || !num(s.lat)) continue;
    seen.add(t.id);
    const cls = "tr " + (S.sel && S.sel.id === t.id ? "sel" : "");
    let m = trackedMarkers.get(t.id);
    if (!m) {
      m = L.marker([s.lat, s.lon], { icon: planeIcon(cls, s.track, 28), zIndexOffset: 1000, keyboard: false });
      m.on("click", () => selectTracked(m._t.id));
      m.bindTooltip("", { direction: "top", offset: [0, -10] });
      m.addTo(trackedLayer);
      trackedMarkers.set(t.id, m);
    } else {
      m.setLatLng([s.lat, s.lon]);
      m.setIcon(planeIcon(cls, s.track, 28));
    }
    m._t = t;
    m.setTooltipContent(`<b>${esc(flightNo(t))}</b>`);
  }
  for (const [id, m] of trackedMarkers) if (!seen.has(id)) { trackedLayer.removeLayer(m); trackedMarkers.delete(id); }
}

/* great-circle points with continuous (unwrapped) longitude so Leaflet draws across the antimeridian */
function greatCircle(a, b, n = 64) {
  const r = Math.PI / 180, d2r = (x) => x * r;
  const p1 = d2r(a.lat), l1 = d2r(a.lon), p2 = d2r(b.lat), l2 = d2r(b.lon);
  const d = 2 * Math.asin(Math.sqrt(Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin((l2 - l1) / 2) ** 2));
  if (!d) return [[a.lat, a.lon]];
  const pts = [];
  let prev = a.lon;
  for (let i = 0; i <= n; i++) {
    const f = i / n, A = Math.sin((1 - f) * d) / Math.sin(d), B = Math.sin(f * d) / Math.sin(d);
    const x = A * Math.cos(p1) * Math.cos(l1) + B * Math.cos(p2) * Math.cos(l2);
    const y = A * Math.cos(p1) * Math.sin(l1) + B * Math.cos(p2) * Math.sin(l2);
    const z = A * Math.sin(p1) + B * Math.sin(p2);
    const lat = Math.atan2(z, Math.hypot(x, y)) / r;
    let lon = Math.atan2(y, x) / r;
    while (lon - prev > 180) lon -= 360;
    while (lon - prev < -180) lon += 360;
    pts.push([lat, lon]);
    prev = lon;
  }
  return pts;
}
function unwrap(pts) {
  let prev = null;
  return pts.map(([lat, lon]) => {
    if (prev !== null) { while (lon - prev > 180) lon -= 360; while (lon - prev < -180) lon += 360; }
    prev = lon;
    return [lat, lon];
  });
}

/* Keep pins/lines mutually consistent when a route's shorter great-circle path crosses the antimeridian
   (e.g. Tokyo->LA): draw everything in the same "world copy" as the aircraft, so the route line lands on
   the airport pins instead of 360deg away, and fitBounds sees the short way round instead of the long one. */
function nearestLon(ref, lon) { return ref + (((lon - ref + 180) % 360 + 360) % 360 - 180); }
function rebase(pts, ref, anchor = 0) {
  if (!pts.length) return pts;
  const shift = pts[anchor][1] - nearestLon(ref, pts[anchor][1]);
  return shift ? pts.map(([lat, lon]) => [lat, lon - shift]) : pts;
}

let fitted = null;
/* Selecting a flight always draws its full path (trail flown + reference line + remaining leg), but the
   camera starts zoomed in close on the aircraft rather than zooming out to fit the whole origin-to-
   destination line, which for a long-haul flight would leave a barely-readable world view. "Show full
   flight path" (below) re-fits the map to the whole route on request; picking a new flight always resets
   back to the close-up camera. */
function renderRoute() {
  routeLayer.clearLayers();
  const f = S.detail;
  if (!f || !f.state) return;
  const s = f.state, o = f.route?.origin, d = f.route?.destination;
  const cur = { lat: s.lat, lon: s.lon };
  const ref = cur.lon;  // anchor: every feature below is drawn relative to the aircraft's own copy of the world
  const bounds = [[s.lat, s.lon]];
  if (f.trail?.length > 1) {
    const trail = unwrap(f.trail.map((p) => [p[0], p[1]]));
    L.polyline(rebase(trail, ref, trail.length - 1), { color: "#e8a33d", weight: 3, opacity: .9 }).addTo(routeLayer);
  }
  const pin = (ap, label) => {
    if (!ap || !num(ap.lat)) return;
    const lon = nearestLon(ref, ap.lon);
    L.marker([ap.lat, lon], { icon: L.divIcon({ className: "", html: '<div class="ap-pin"></div>', iconSize: [12, 12], iconAnchor: [6, 6] }) })
      .bindTooltip(`<b>${esc(ap.iata || ap.icao)}</b> ${esc(ap.name || "")}<br>${esc(label)}`, { permanent: false }).addTo(routeLayer);
    bounds.push([ap.lat, lon]);
  };
  const hasRoute = o && num(o.lat) && d && num(d.lat);
  if (hasRoute) {
    // Full planned route (faint), flown part (solid, from origin), remaining part (dashed, already anchored at `ref`).
    L.polyline(rebase(greatCircle(o, d), ref), { color: "#928f78", weight: 1.5, opacity: .6 }).addTo(routeLayer);
    if (!f.trail || f.trail.length < 2) L.polyline(rebase(greatCircle(o, cur, 40), ref), { color: "#e8a33d", weight: 3 }).addTo(routeLayer);
    L.polyline(greatCircle(cur, d, 40), { color: "#e8a33d", weight: 2.5, dashArray: "6 8" }).addTo(routeLayer);
    pin(o, "Origin"); pin(d, "Destination");
  }
  if (fitted !== f.id) {
    if (S.showFullPath && bounds.length > 1) {
      map.fitBounds(bounds, { padding: [60, 60], maxZoom: 9 });
    } else {
      map.setView([s.lat, s.lon], Math.max(map.getZoom(), 8));
    }
    fitted = f.id;
  }
}
function togglePath(show) {
  S.showFullPath = show;
  fitted = null;   // force a re-fit for the new view
  renderRoute();
  renderDetail();  // swap the button's label/state
  renderFlightPage();
}

/* ---------- selection & detail ---------- */
let detailTimer = null;
async function selectFlight(f) { return select(selKey(f)); }
async function selectTracked(id) { const t = S.tracked.find((x) => x.id === id); return select({ kind: t?.kind || "callsign", id }); }
async function select(sel) {
  S.sel = sel; S.detail = null; fitted = null; S.showFullPath = false;
  go("map");
  renderDetail(); renderTracked(); renderRegion(); renderTrackedMarkers(); routeLayer.clearLayers();
  await refreshDetail();
  clearInterval(detailTimer);
  detailTimer = setInterval(refreshDetail, 10000);
}
async function refreshDetail() {
  if (!S.sel) return;
  const sel = S.sel;
  try {
    const d = await api(`/api/flight/${sel.kind}/${encodeURIComponent(sel.id)}`);
    if (S.sel !== sel) return;
    S.detail = d;
  } catch (e) {
    if (S.sel !== sel) return;
    S.detail = { id: sel.id, error: e.message };
  }
  renderDetail(); renderRoute(); renderTrackedMarkers();
}
function deselect() {
  S.sel = null; S.detail = null; clearInterval(detailTimer);
  routeLayer.clearLayers(); renderDetail(); renderTracked(); renderRegion(); renderTrackedMarkers();
}

function routeSuspectNote(d, o, dst) {
  if (d.route_corrected) {
    const c = d.route_corrected;
    return `<div class="alert info"><b>ℹ Route corrected using today's schedule.</b> The free per-callsign route lookup said
      ${esc(c.origin || "?")}→${esc(c.destination || "?")} (it can simply be out of date - common for reassigned mainline
      flight numbers); showing ${esc(o?.iata || "?")}→${esc(dst?.iata || "?")} from your schedule provider instead.</div>`;
  }
  if (d.route_suspect) {
    return `<div class="alert warn"><b>⚠ Route may be wrong for this flight.</b> The live position is ~${Math.round(d.route_suspect / 1.852)} NM
      off the ${esc(o?.iata || "?")}→${esc(dst?.iata || "?")} line - more than the 200 NM a normal flight ever deviates by. Free route
      lookups match by callsign only, so a reused or stale callsign (common for aircraft flying several sectors a day) can show the
      right aircraft with the wrong route. Progress, ETA and total time below are unreliable until this clears.</div>`;
  }
  if (d.route_unverified) {
    return `<p class="muted small">Route not checked against a live schedule - the free per-callsign lookup can simply be wrong
      for reassigned flight numbers${S.hasSchedule ? " (no schedule found for this flight)" : " (add an AviationStack key to check it)"}.</p>`;
  }
  return "";
}
function renderDetail() {
  const el = $("#detail");
  const f = S.detail;
  if (!S.sel) { el.hidden = true; return; }
  el.hidden = false;
  if (!f) { el.innerHTML = `<p class="muted">Loading ${esc(S.sel.id)}…</p>`; return; }
  if (f.error) {
    el.innerHTML = `<div class="row"><b class="grow">${esc(S.sel.id)}</b><button id="d-close" class="x">×</button></div><p class="muted">${esc(f.error)}</p>`;
    $("#d-close").onclick = deselect; return;
  }
  const s = f.state || {}, d = f.derived || {}, o = f.route?.origin, dst = f.route?.destination, ac = f.aircraft;
  const tracked = S.tracked.some((t) => t.id === f.id);
  const dl = delayInfo(d);
  const prog = num(d.progress) ? Math.round(d.progress * 100) : null;
  const etaLabel = d.eta_source === "groundspeed" ? " (est.)" : "";
  el.innerHTML = `
    <div class="row">${badge(f.airline, f.callsign)}
      <div class="grow"><b style="font-size:18px">${esc(flightNo(f))}</b>
        <div class="muted ell">${esc(f.airline?.name || "Unknown airline")}${f.callsign && f.flight_iata ? " · " + esc(f.callsign) : ""}</div>${catTag(f.category)}</div>
      <button id="d-close" class="x" title="Close">×</button></div>
    ${d.emergency ? `<div class="alert bad"><b>⚠ ${esc(d.emergency)}</b></div>` : ""}
    ${routeSuspectNote(d, o, dst)}
    <div class="route">
      <div class="ap">${esc(o?.iata || o?.icao || "?")}<small>${esc(o?.city || o?.name || "Origin unknown")}</small></div>
      <div class="ap" style="text-align:right">${esc(dst?.iata || dst?.icao || "?")}<small>${esc(dst?.city || dst?.name || "Destination unknown")}</small></div>
    </div>
    ${prog != null ? `<div class="bar"><i style="width:${prog}%"></i></div><div class="muted" style="font-size:12px;margin-top:3px">${prog}% · ${fmtKm(d.dist_flown_km)} flown · ${fmtKm(d.dist_remaining_km)} to go</div>` : ""}
    <div class="grid">
      <div><small>Status</small><b>${esc(STATUS[f.status] || f.status)}${d.phase && f.status === "airborne" ? " · " + esc(d.phase) : ""}</b></div>
      <div><small>Arrives${etaLabel}</small><b>${fmtTime(d.eta_ts)}</b>${num(d.time_left_min) ? `<span class="muted"> in ${fmtDur(d.time_left_min)}</span>` : ""}</div>
      <div><small>Delay</small><b class="${dl.cls}" title="${esc(dl.title)}">${esc(dl.text)}</b></div>
      <div><small>Total flight time${d.total_source === "estimate" ? " (est.)" : ""}</small><b>${fmtDur(d.total_min)}</b></div>
      <div><small>Altitude</small><b>${fmtAlt(s.alt_ft)}</b></div>
      <div><small>Ground speed</small><b>${fmtSpd(s.gs_kt)}</b></div>
      <div><small>Heading</small><b>${fmtHdg(s.track, d.heading_cardinal)}</b></div>
      <div><small>Vertical rate</small><b>${fmtVr(s.vrate_fpm)}</b></div>
      <div><small>Aircraft</small><b>${esc(ac?.type || s.type || dash)}</b></div>
      <div><small>Registration</small><b>${esc(ac?.registration || s.reg || dash)}</b></div>
      <div><small>Squawk</small><b>${esc(s.squawk || dash)}</b></div>
      <div><small>Last seen</small><b>${f.last_seen ? Math.max(0, Math.round(Date.now() / 1000 - f.last_seen)) + "s ago" : dash}</b></div>
    </div>
    <div class="row wrap">
      ${tracked ? '<button id="d-untrack" class="danger">Stop tracking</button>' : '<button id="d-track" class="primary">Track this flight</button>'}
      <button id="d-center">Center</button>
      ${num(o?.lat) && num(dst?.lat) ? `<button id="d-path">${S.showFullPath ? "Zoom to aircraft" : "Zoom to full flight path"}</button>` : ""}
    </div>
    ${ac?.photo ? `<img class="photo" src="${esc(ac.photo)}" alt="Aircraft photo" referrerpolicy="no-referrer" onerror="this.remove()">` : ""}`;
  $("#d-close").onclick = deselect;
  $("#d-center").onclick = () => { if (num(s.lat)) map.setView([s.lat, s.lon], Math.max(map.getZoom(), 8)); };
  const t = $("#d-track"); if (t) t.onclick = () => track(f);
  const u = $("#d-untrack"); if (u) u.onclick = () => untrack(f.id);
  const p = $("#d-path"); if (p) p.onclick = () => togglePath(!S.showFullPath);
}

/* ---------- tracked list ---------- */
async function track(f) {
  try {
    const kind = f.kind || (f.callsign ? "callsign" : "hex");
    await api("/api/tracked", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: f.id, kind }) });
  } catch (e) { alert(e.message); }
}
async function untrack(id) {
  try { await api(`/api/tracked/${encodeURIComponent(id)}`, { method: "DELETE" }); } catch (e) { alert(e.message); }
}
function renderTracked() {
  $("#tracked-count").textContent = `${S.tracked.length}/${S.maxTracked}`;
  const el = $("#tracked-list");
  if (!S.tracked.length) { el.innerHTML = '<p class="muted">Nothing tracked. Search for a flight or click a plane on the map.</p>'; return; }
  el.innerHTML = S.tracked.map((t) => {
    const o = t.route?.origin, d = t.route?.destination;
    return `<div class="card row ${S.sel?.id === t.id ? "sel" : ""}" data-id="${esc(t.id)}">
      ${badge(t.airline, t.callsign)}
      <div class="grow"><b>${esc(flightNo(t))}</b> <span class="dot ${esc(t.status)}"></span>
        <div class="muted ell">${o || d ? esc((o?.iata || "?") + " → " + (d?.iata || "?")) : "Route unknown"} · ${esc(STATUS[t.status] || t.status)}</div></div>
      <button class="x" data-x="${esc(t.id)}" title="Stop tracking">×</button></div>`;
  }).join("");
  el.querySelectorAll(".card").forEach((c) => (c.onclick = () => selectTracked(c.dataset.id)));
  el.querySelectorAll("[data-x]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); untrack(b.dataset.x); }));
}

/* ---------- ticker ---------- */
function renderTicker() {
  const body = $("#ticker tbody");
  $("#ticker-empty").hidden = S.tracked.length > 0;
  body.innerHTML = S.tracked.map((t) => {
    const s = t.state || {}, d = t.derived || {}, o = t.route?.origin, dst = t.route?.destination, dl = delayInfo(d);
    const arr = d.eta_ts ? `${fmtTime(d.eta_ts)}${d.eta_source === "groundspeed" ? "*" : ""}` : dash;
    return `<tr data-id="${esc(t.id)}">
      <td><div class="row">${badge(t.airline, t.callsign)}<span>${esc(t.airline?.name || dash)}</span></div></td>
      <td class="flight">${esc(flightNo(t))}</td>
      <td>${esc((o?.iata || "?") + " → " + (dst?.iata || "?"))}</td>
      <td>${arr}${num(d.time_left_min) ? ` <span class="muted">(${fmtDur(d.time_left_min)})</span>` : ""}</td>
      <td class="${dl.cls}" title="${esc(dl.title)}">${esc(dl.text)}</td>
      <td>${fmtDur(d.total_min)}${d.total_source === "estimate" ? "*" : ""}</td>
      <td>${fmtHdg(s.track, d.heading_cardinal)}</td>
      <td>${fmtSpdShort(s.gs_kt)}</td>
      <td>${fmtAlt(s.alt_ft)}</td>
      <td><span class="dot ${esc(t.status)}"></span> ${esc(STATUS[t.status] || t.status)}</td>
      <td><button class="x" data-x="${esc(t.id)}" title="Stop tracking">×</button></td></tr>`;
  }).join("");
  body.querySelectorAll("tr").forEach((r) => (r.onclick = () => { location.hash = `#/flight/${encodeURIComponent(r.dataset.id)}`; }));
  body.querySelectorAll("[data-x]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); untrack(b.dataset.x); }));
}

/* ---------- live stream ---------- */
let ws, wsDelay = 1000, wsSeen = false;
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/stream`);
  ws.onopen = () => { wsDelay = 1000; setStatus(modeLabel(), true); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type !== "tracked") return;
    wsSeen = true;
    S.tracked = m.flights;
    if (S.sel) {
      const cur = S.tracked.find((t) => t.id === S.sel.id);
      if (cur && S.detail && !S.detail.error) { S.detail = cur; renderDetail(); renderRoute(); }
    }
    renderTracked(); renderTicker(); renderTrackedMarkers(); renderRegion(); renderFlightPage();
    setStatus(m.error ? `${modeLabel()} · ${m.error}` : `${modeLabel()} · updated ${clock(m.last_poll)}`, !m.error);
  };
  ws.onclose = () => { setStatus("reconnecting…", false); setTimeout(connect, wsDelay); wsDelay = Math.min(wsDelay * 2, 15000); };
  ws.onerror = () => ws.close();
}
function setStatus(text, ok) { const e = $("#status"); e.textContent = text; e.className = "status " + (ok ? "ok" : "bad"); }

/* ---------- region / global loading ---------- */
let regionTimer = null, regionSeq = 0, pendingTimer = null, regionLoop = null;
const clock = (ts) => (num(ts) ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : dash);
const modeLabel = () => ({ live: "live", saver: "data saver", manual: "manual" }[S.usage?.mode] || "live");
function note(text) { const n = $("#map-note"); n.hidden = !text; n.textContent = text || ""; }

/* seconds between automatic map refreshes, or null when the user wants manual updates only */
function regionSeconds() {
  const mode = S.usage?.mode || "saver";
  if (S.regionEvery === "manual" || mode === "manual") return null;
  if (typeof S.regionEvery === "number") return S.regionEvery;
  return mode === "saver" ? Math.max(15, Math.round(S.usage?.region_interval || 15)) : 15;
}
/* o.force: user-initiated; o.fresh: bypass server caches (costs API calls); o.cached: reuse cached data (filter changes) */
async function loadRegion(o = {}) {
  if ($("#view-map").hidden) return;
  if (regionSeconds() === null && !o.force && S.regionLoaded) return;  // manual mode: only on request
  const seq = ++regionSeq, z = map.getZoom(), b = map.getBounds(), secs = regionSeconds();
  const age = o.fresh ? 0 : o.cached ? 600 : Math.max(8, Math.round((secs || 30) * 0.8));
  try {
    let res;
    if (z < 5) {
      res = await api(`/api/flights/global?${filterQuery()}${o.fresh ? "&fresh=true" : ""}`);
      note(`Worldwide: ${res.total.toLocaleString()} ${res.filtered ? "matching " : ""}flights airborne (sampled) · data from ${clock(res.ts)}. Zoom in on a region to see all traffic there.${pendingNote(res)}`);
    } else {
      const wrap = (x) => ((x + 180) % 360 + 360) % 360 - 180;
      const span = b.getEast() - b.getWest();
      const west = span >= 360 ? -180 : wrap(b.getWest()), east = span >= 360 ? 180 : wrap(b.getEast());
      const q = new URLSearchParams({ south: Math.max(-90, b.getSouth()), north: Math.min(90, b.getNorth()), west, east, max_age: age });
      if (o.fresh) q.set("fresh", "true");
      res = await api(`/api/flights/region?${q}&${filterQuery()}`);
      const base = res.filtered ? `${res.total.toLocaleString()} of ${res.in_view.toLocaleString()} flights match filters` : `${res.total.toLocaleString()} flight${res.total === 1 ? "" : "s"} in view`;
      note(`${base}${res.note ? " · " + res.note : ""} · updated ${clock(res.ts)}${pendingNote(res)}`);
    }
    if (seq !== regionSeq) return;
    S.region = res.flights; S.regionLoaded = true; setStale(false);
    renderRegion();
    clearTimeout(pendingTimer);
    if (res.routes_pending) pendingTimer = setTimeout(() => loadRegion({ force: true, cached: true }), 4000);  // routes resolve in the background
  } catch (e) {
    if (seq === regionSeq) note(`Map data unavailable: ${e.message}`);
  }
}
function setStale(on) { $("#refresh").classList.toggle("stale", !!on); }
function scheduleRegion(delay = 700, o = {}) { clearTimeout(regionTimer); regionTimer = setTimeout(() => loadRegion(o), delay); }
function restartRegionLoop() {
  clearTimeout(regionLoop);
  const s = regionSeconds();
  regionLoop = setTimeout(() => { if (regionSeconds() !== null) loadRegion(); restartRegionLoop(); }, (s || 30) * 1000);
}
map.on("moveend", () => {
  renderRegion();
  if (regionSeconds() === null && S.regionLoaded) { setStale(true); return; }  // manual: wait for the Refresh button
  scheduleRegion();
});

/* ---------- search ---------- */
$("#search-form").onsubmit = async (e) => {
  e.preventDefault();
  const q = $("#search").value.trim();
  const box = $("#search-results");
  if (q.length < 2) return;
  box.hidden = false; box.innerHTML = '<p class="muted">Searching…</p>';
  try {
    const r = await api(`/api/flights/search?q=${encodeURIComponent(q)}`);
    if (!r.results.length) { box.innerHTML = `<p class="muted">${esc(r.note || "No matching flights found.")}</p>`; return; }
    if (r.results.length === 1 && r.parsed.kind !== "airline") { box.hidden = true; return selectFlight(r.results[0]); }
    box.innerHTML = (r.total > r.results.length ? `<p class="muted">Showing ${r.results.length} of ${r.total}</p>` : "") +
      r.results.map((f, i) => `<div class="card row" data-i="${i}">${badge({ iata: f.airline_iata, icao: f.airline_icao }, f.callsign)}
        <div class="grow"><b>${esc(f.callsign || f.hex)}</b><div class="muted">${esc(f.type || "")} ${fmtAlt(f.alt_ft)}</div></div></div>`).join("");
    box.querySelectorAll(".card").forEach((c) => (c.onclick = () => { selectFlight(r.results[c.dataset.i]); box.hidden = true; }));
  } catch (err) { box.innerHTML = `<p class="bad">${esc(err.message)}</p>`; }
};

/* ---------- place jump (Nominatim geocoder) ---------- */
$("#place-form").onsubmit = async (e) => {
  e.preventDefault();
  const q = $("#place").value.trim();
  if (!q) return;
  try {
    const r = await fetch(`https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q=${encodeURIComponent(q)}`);
    const [hit] = await r.json();
    if (!hit) return note(`Place not found: ${q}`);
    const bb = hit.boundingbox.map(Number);  // south, north, west, east
    map.fitBounds([[bb[0], bb[2]], [bb[1], bb[3]]], { maxZoom: 9 });
  } catch { note("Place search unavailable"); }
};

/* ---------- filters ---------- */
const FKEY = "ft.filters";
function loadFilters() { try { Object.assign(S.filters, JSON.parse(localStorage.getItem(FKEY) || "{}")); } catch { /* ignore */ } }
function saveFilters() { try { localStorage.setItem(FKEY, JSON.stringify(S.filters)); } catch { /* ignore */ } }
function filterQuery() {
  const f = S.filters, q = new URLSearchParams();
  if (f.cats.length) q.set("cats", f.cats.join(","));
  if (f.airline.trim()) q.set("airline", f.airline.trim());
  if (f.airport.trim()) q.set("airport", f.airport.trim());
  if (f.airborne) q.set("airborne", "true");
  return q.toString();
}
function pendingNote(res) { return res.routes_pending ? ` · looking up routes for ${res.routes_pending} flight${res.routes_pending === 1 ? "" : "s"}…` : ""; }
const blankFilters = () => ({ cats: [], airline: "", airport: "", airborne: false });
let draft = null;   // what the form shows; only applied to the map on Search, so typing costs no API calls
const dirty = () => JSON.stringify(normFilters(draft)) !== JSON.stringify(normFilters(S.filters));
const normFilters = (f) => ({ cats: [...f.cats].sort(), airline: f.airline.trim(), airport: f.airport.trim().toUpperCase(), airborne: !!f.airborne });
function renderFilters(syncInputs = true) {
  if (!draft) draft = { ...blankFilters(), ...S.filters, cats: [...S.filters.cats] };
  $("#cat-chips").innerHTML = Object.entries(S.cats).map(([id, label]) =>
    `<span class="chip ${draft.cats.includes(id) ? "on" : ""}" data-c="${esc(id)}" style="--c:${CAT_COLOR[id]}"><i></i>${esc(label)}</span>`).join("");
  $("#cat-chips").querySelectorAll(".chip").forEach((c) => (c.onclick = () => {
    const id = c.dataset.c;
    draft.cats = draft.cats.includes(id) ? draft.cats.filter((x) => x !== id) : [...draft.cats, id];
    renderFilters(false);
  }));
  if (syncInputs) { $("#f-airline").value = draft.airline; $("#f-airport").value = draft.airport; $("#f-airborne").checked = draft.airborne; }
  document.querySelectorAll(".fclear").forEach((b) => (b.hidden = !$(`#f-${b.dataset.clear}`).value));
  $("#f-clear-cats").hidden = !draft.cats.length;
  $("#f-search").classList.toggle("pending", dirty());
  const f = S.filters, n = f.cats.length + (f.airline.trim() ? 1 : 0) + (f.airport.trim() ? 1 : 0) + (f.airborne ? 1 : 0);
  $("#filter-count").textContent = n ? `(${n} active)` : "";
  $("#f-reset").disabled = !n && !dirty();
  const hint = $("#f-hint");
  hint.hidden = !draft.airport.trim();
  hint.textContent = "Airport matching needs each flight's route, so results fill in over a few seconds as routes are looked up (commercial and cargo flights only).";
}
function filtersChanged() { saveFilters(); renderFilters(); S.region = []; renderRegion(); scheduleRegion(200, { force: true, cached: true }); }
function applyFilters() {
  const n = normFilters(draft);
  S.filters = { cats: n.cats, airline: n.airline, airport: n.airport, airborne: n.airborne };
  draft.airport = n.airport;
  filtersChanged();
}
/* Clearing a field removes it from the draft and from the applied filters immediately. */
function clearField(name) {
  draft[name] = ""; S.filters[name] = "";
  filtersChanged();
}
$("#f-form").onsubmit = (e) => { e.preventDefault(); applyFilters(); };
$("#f-airline").oninput = () => { draft.airline = $("#f-airline").value; renderFilters(false); };
$("#f-airport").oninput = () => { draft.airport = $("#f-airport").value; renderFilters(false); };
$("#f-airborne").onchange = () => { draft.airborne = $("#f-airborne").checked; renderFilters(false); };
document.querySelectorAll(".fclear").forEach((b) => (b.onclick = () => { clearField(b.dataset.clear); $(`#f-${b.dataset.clear}`).focus(); }));
$("#f-clear-cats").onclick = () => { draft.cats = []; S.filters.cats = []; filtersChanged(); };
$("#f-reset").onclick = () => { draft = blankFilters(); S.filters = blankFilters(); filtersChanged(); };
async function loadMeta() {
  try {
    const m = await api("/api/meta");
    S.cats = m.categories;
    $("#airline-list").innerHTML = m.airlines.map((a) => `<option value="${esc(a.name)}">${esc(a.iata)} / ${esc(a.icao)}</option>`).join("");
    renderFilters();
  } catch { /* filters just stay hidden of categories */ }
}
loadFilters();

/* ---------- flight page (full-screen view of one tracked flight) ---------- */
let pageId = null, rotateTimer = null;
function pageList() { return S.tracked; }
function pageStep(dir) {
  const l = pageList(); if (l.length < 2) return;
  const i = Math.max(0, l.findIndex((t) => t.id === pageId));
  location.hash = `#/flight/${encodeURIComponent(l[(i + dir + l.length) % l.length].id)}`;
}
function tile(label, value, sub = "", cls = "", extra = "") {
  return `<div class="tile"><small>${esc(label)}</small>${extra}<b class="${cls}">${value}</b>${sub ? `<span>${sub}</span>` : ""}</div>`;
}
function renderFlightPage() {
  if ($("#view-flight").hidden) return;
  const body = $("#fp-body"), l = pageList();
  const i = l.findIndex((t) => t.id === pageId), f = l[i];
  $("#fp-pos").textContent = f ? `${i + 1} / ${l.length}` : "";
  $("#fp-prev").disabled = $("#fp-next").disabled = l.length < 2;
  if (!f) {
    body.innerHTML = `<p class="muted" style="font-size:18px">${S.tracked.length || wsSeen ? `${esc(pageId)} is not in your tracked flights.` : "Loading…"}
      <a href="#/ticker">Back to the ticker</a></p>`;
    return;
  }
  const s = f.state || {}, d = f.derived || {}, o = f.route?.origin, dst = f.route?.destination, ac = f.aircraft, dl = delayInfo(d);
  const prog = num(d.progress) ? Math.max(0, Math.min(100, d.progress * 100)) : null;
  const arrow = num(s.track) ? `<svg class="arrow" viewBox="0 0 24 24" style="transform:rotate(${s.track}deg)"><path d="M12 2l6 18-6-4-6 4z"/></svg>` : "";
  const photo = ac?.photo_large || ac?.photo;
  body.innerHTML = `
    <div class="fp-hero">
      ${badge(f.airline, f.callsign).replace('class="badge"', 'class="badge xl"')}
      <div class="fp-id"><h2>${esc(flightNo(f))}</h2><div class="al ell">${esc(f.airline?.name || "Unknown airline")}${f.callsign && f.flight_iata ? " · " + esc(f.callsign) : ""}</div>
        <div class="fp-pills"><span class="pill"><span class="dot ${esc(f.status)}"></span> ${esc(STATUS[f.status] || f.status)}${d.phase && f.status === "airborne" ? " · " + esc(d.phase) : ""}</span>${catTag(f.category)}
        ${d.emergency ? `<span class="pill bad">⚠ ${esc(d.emergency)}</span>` : ""}</div></div>
      ${photo ? `<img class="fp-photo" src="${esc(photo)}" alt="Aircraft photo" referrerpolicy="no-referrer" onerror="this.remove()">` : ""}
    </div>
${d.route_corrected ? `<div class="alert info" style="font-size:14px"><b>ℹ Route corrected</b> — free lookup said ${esc(d.route_corrected.origin || "?")}→${esc(d.route_corrected.destination || "?")}; showing ${esc(o?.iata || "?")}→${esc(dst?.iata || "?")} from today's schedule.</div>`
      : d.route_suspect ? `<div class="alert warn" style="font-size:14px"><b>⚠ Route may be wrong</b> — live position is ~${Math.round(d.route_suspect / 1.852)} NM off the ${esc(o?.iata || "?")}→${esc(dst?.iata || "?")} line (likely a reused callsign).</div>` : ""}
    <div class="fp-route">
      <div class="ap">${esc(o?.iata || o?.icao || "?")}<small class="ell">${esc(o?.city || o?.name || "Origin unknown")}</small></div>
      <div><div class="track">${prog != null ? `<i style="width:${prog}%"></i><div class="pl" style="left:${prog}%">${PLANE_SVG}</div>` : ""}</div>
        <div class="track-cap">${prog != null ? `${Math.round(prog)}% · ${fmtKm(d.dist_flown_km)} flown · ${fmtKm(d.dist_remaining_km)} to go` : "Route progress unavailable"}</div></div>
      <div class="ap r">${esc(dst?.iata || dst?.icao || "?")}<small class="ell">${esc(dst?.city || dst?.name || "Destination unknown")}</small></div>
    </div>
    <div class="tiles">
      ${tile("Arrives" + (d.eta_source === "groundspeed" ? " (est.)" : ""), fmtTime(d.eta_ts), num(d.time_left_min) ? `in ${fmtDur(d.time_left_min)}` : "")}
      ${tile("Delay", esc(dl.text), esc(dl.title), dl.cls)}
      ${tile("Total flight time" + (d.total_source === "estimate" ? " (est.)" : ""), fmtDur(d.total_min))}
      ${tile("Distance", fmtKm(d.dist_total_km), num(d.dist_total_km) ? `${Math.round(d.dist_total_km * 0.539957).toLocaleString()} nm` : "")}
      ${tile("Heading", fmtHdg(s.track, d.heading_cardinal), num(d.bearing_to_dest) ? `Destination bears ${d.bearing_to_dest}°` : "", "", arrow)}
      ${tile("Ground speed", fmtSpdShort(s.gs_kt), num(s.gs_kt) ? `${Math.round(s.gs_kt * 1.852)} km/h · ${Math.round(s.gs_kt * 1.15078)} mph` : "")}
      ${tile("Altitude", fmtAlt(s.alt_ft), num(s.alt_ft) ? `${Math.round(s.alt_ft * 0.3048).toLocaleString()} m` : "")}
      ${tile("Vertical rate", fmtVr(s.vrate_fpm))}
      ${tile("Aircraft", esc(ac?.type || s.type || dash), esc(ac?.manufacturer || ""))}
      ${tile("Registration", esc(ac?.registration || s.reg || dash), esc(ac?.owner || ""))}
      ${tile("Position", num(s.lat) ? `${s.lat.toFixed(2)}, ${s.lon.toFixed(2)}` : dash, `Squawk ${esc(s.squawk || dash)}`)}
      ${tile("Last seen", f.last_seen ? Math.max(0, Math.round(Date.now() / 1000 - f.last_seen)) + "s ago" : dash, esc(s.source || ""))}
    </div>`;
}

/* ---------- tabs / routing ---------- */
function showTab(name, id = null) {
  pageId = name === "flight" ? id : null;
  $("#view-map").hidden = name !== "map"; $("#view-ticker").hidden = name !== "ticker"; $("#view-flight").hidden = name !== "flight";
  $("#tab-map").classList.toggle("active", name === "map"); $("#tab-ticker").classList.toggle("active", name !== "map");
  clearInterval(rotateTimer); rotateTimer = null;
  if (name === "map") { map.invalidateSize(); scheduleRegion(100); }
  else if (name === "flight") { renderFlightPage(); if ($("#fp-rotate").checked) rotateTimer = setInterval(() => pageStep(1), 15000); }
  else renderTicker();
}
function go(name) { const h = `#/${name}`; if (location.hash !== h) history.pushState(null, "", h); showTab(name); }
function routeFromHash() {
  const h = location.hash || "#/map", m = h.match(/^#\/flight\/(.+)$/);
  if (m) return showTab("flight", decodeURIComponent(m[1]));
  showTab(h === "#/ticker" ? "ticker" : "map");
}
window.addEventListener("hashchange", routeFromHash);
$("#tab-map").onclick = () => go("map");
$("#tab-ticker").onclick = () => go("ticker");
$("#fp-back").onclick = () => go("ticker");
$("#fp-prev").onclick = () => pageStep(-1); $("#fp-next").onclick = () => pageStep(1);
$("#fp-map").onclick = () => { const f = S.tracked.find((t) => t.id === pageId); if (f) select({ kind: f.kind, id: f.id }); };
$("#fp-untrack").onclick = async () => {
  const id = pageId, remaining = pageList().filter((t) => t.id !== id);
  await untrack(id);
  location.hash = remaining.length ? `#/flight/${encodeURIComponent(remaining[0].id)}` : "#/ticker";
};
$("#fp-rotate").onchange = () => showTab("flight", pageId);
function toggleFullscreen() {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().then(() => document.body.classList.add("full")).catch(() => {});
}
$("#fullscreen").onclick = toggleFullscreen; $("#fp-full").onclick = toggleFullscreen;
document.addEventListener("fullscreenchange", () => { if (!document.fullscreenElement) document.body.classList.remove("full"); });
document.addEventListener("keydown", (e) => {
  if ($("#view-flight").hidden || /INPUT|TEXTAREA/.test(e.target.tagName)) return;
  if (e.key === "ArrowRight") pageStep(1);
  else if (e.key === "ArrowLeft") pageStep(-1);
  else if (e.key === "f" || e.key === "F") toggleFullscreen();
  else if (e.key === "Escape" && !document.fullscreenElement) go("ticker");
});
setInterval(() => { renderTicker(); renderFlightPage(); }, 30000);  // keep countdowns fresh between pushes

/* ---------- data usage, refresh settings ---------- */
const POLL_CHOICES = [[10, "Every 10 s"], [30, "Every 30 s"], [60, "Every minute"], [120, "Every 2 minutes"], [300, "Every 5 minutes"], [900, "Every 15 minutes"]];
const REGION_KEY = "ft.regionEvery";
try { const v = localStorage.getItem(REGION_KEY); if (v) S.regionEvery = v === "follow" || v === "manual" ? v : Number(v) || "follow"; } catch { /* ignore */ }
const humanEvery = (s) => (s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);
const untilReset = (ts) => { const m = (ts - Date.now() / 1000) / 60; return m > 2880 ? `${Math.round(m / 1440)} days` : fmtDur(m); };

async function loadUsage() {
  try { S.usage = await api("/api/usage"); } catch { return; }
  renderUsageBadge();
  if ($("#data-panel").open) renderDataPanel();
}
function renderUsageBadge() {
  const b = $("#usage-badge");
  const limited = (S.usage?.providers || []).filter((p) => p.limit && p.remaining != null);
  if (!limited.length) { b.hidden = true; return; }
  const worst = limited.map((p) => ({ p, frac: p.remaining / p.limit })).sort((a, z) => a.frac - z.frac)[0];
  b.hidden = false;
  b.textContent = `${worst.p.label.split(" (")[0]}: ${Math.round(worst.p.remaining)} left`;
  b.className = "badge-btn " + (worst.frac <= 0.1 ? "bad" : worst.frac <= 0.3 ? "warn" : "");
}
function renderDataPanel() {
  const u = S.usage; if (!u) return;
  document.querySelectorAll('input[name="mode"]').forEach((r) => (r.checked = r.value === u.mode));
  const sel = $("#dp-poll"), opts = POLL_CHOICES.some(([v]) => v === u.poll_interval) ? POLL_CHOICES : [...POLL_CHOICES, [u.poll_interval, `Every ${u.poll_interval} s`]];
  sel.innerHTML = opts.map(([v, l]) => `<option value="${v}">${l}</option>`).join(""); sel.value = String(u.poll_interval);
  sel.disabled = u.mode === "manual";
  $("#dp-region").value = String(S.regionEvery);
  const eff = u.effective_poll_interval, rs = regionSeconds();
  $("#dp-effective").textContent =
    (u.mode === "manual" ? "Tracked flights: only when you press Refresh. " :
      `Tracked flights refresh every ${humanEvery(eff)}${eff > u.poll_interval + 1 ? " (stretched to fit your plan)" : ""}. `) +
    (rs === null ? "Map: only when you press Refresh." : `Map refreshes every ${humanEvery(rs)}.`);
  $("#dp-usage").innerHTML = u.providers.map((p) => {
    const pct = p.limit ? Math.min(100, (p.used / p.limit) * 100) : 0;
    const left = p.remaining == null ? null : p.remaining / p.limit;
    const cls = left == null ? "" : left <= 0.1 ? "bad" : left <= 0.3 ? "warn" : "";
    const nice = p.by_purpose && Object.keys(p.by_purpose).length ? Object.entries(p.by_purpose).map(([k, v]) => `${k} ${Math.round(v)}`).join(" · ") : "";
    return `<div class="use"><div class="top"><b>${esc(p.label)}</b>
        <span>${p.limit ? `${Math.round(p.remaining ?? p.limit - p.used)} of ${p.limit} left` : `${p.calls} call${p.calls === 1 ? "" : "s"} · no limit`}</span></div>
      ${p.limit ? `<div class="meter"><i class="${cls}" style="width:${pct}%"></i></div>` : ""}
      <small>${p.limit ? `${Math.round(p.used)} used this ${p.period} · resets in ${untilReset(p.resets_at)}` : esc(p.note)}${p.reported_by_provider ? " · reported by provider" : ""}
        ${p.blocked_until ? ` · <span class="warn">rate-limited, retrying in ${Math.ceil(p.blocked_until - Date.now() / 1000)} s</span>` : ""}
        ${nice ? `<br>by use: ${esc(nice)}` : ""}</small></div>`;
  }).join("");
}
async function saveSettings(body) {
  try { S.usage = await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }
  catch (e) { alert(e.message); return; }
  renderUsageBadge(); renderDataPanel(); restartRegionLoop();
}
document.querySelectorAll('input[name="mode"]').forEach((r) => (r.onchange = () => saveSettings({ mode: r.value })));
$("#dp-poll").onchange = () => saveSettings({ poll_interval: Number($("#dp-poll").value) });
$("#dp-region").onchange = () => {
  const v = $("#dp-region").value;
  S.regionEvery = v === "follow" || v === "manual" ? v : Number(v);
  try { localStorage.setItem(REGION_KEY, String(S.regionEvery)); } catch { /* ignore */ }
  renderDataPanel(); restartRegionLoop(); if (regionSeconds() !== null) scheduleRegion(200);
};
$("#data-btn").onclick = () => { renderDataPanel(); $("#data-panel").showModal(); loadUsage(); };
$("#usage-badge").onclick = $("#data-btn").onclick;
$("#dp-close").onclick = () => $("#data-panel").close();
$("#data-panel").addEventListener("click", (e) => { if (e.target === $("#data-panel")) $("#data-panel").close(); });

let refreshing = false;
async function refreshAll() {
  if (refreshing) return;
  refreshing = true; const btn = $("#refresh"); btn.classList.add("busy");
  try {
    await Promise.allSettled([api("/api/refresh", { method: "POST" }), loadRegion({ force: true, fresh: true })]);
    if (S.sel) refreshDetail();
  } finally {
    await loadUsage();
    setTimeout(() => { refreshing = false; btn.classList.remove("busy"); }, 3000);  // matches the server-side throttle
  }
}
$("#refresh").onclick = refreshAll; $("#dp-refresh").onclick = refreshAll;
setInterval(loadUsage, 60000);

/* ---------- boot ---------- */
api("/api/health").then((h) => { S.maxTracked = h.max_tracked; S.hasSchedule = h.has_schedule; renderTracked();
  if (h.demo) setStatus("demo mode", true); }).catch(() => {});
renderTracked();
renderFilters();
loadMeta();
connect();
routeFromHash();
loadUsage().then(() => { scheduleRegion(200); restartRegionLoop(); });
