// Operator console: map, status pill, sensor network (gateway -> cluster -> node),
// audible alarm. The event log is no longer shown (the API still serves it).
// Reads /api/console/*. Operators see site labels; devEUIs appear only in a
// map popup and on the node detail page.
"use strict";

const STATE_COLOR = { NORMAL: "#3fbf6f", WATCH: "#f2b23a", ALARM: "#e5322b", WARMUP: "#6e7886",
                      MISSING: "#6e7886", NO_DATA: "#6e7886", NEVER: "#6e7886" };
const MISSING_COLOR = "#6e7886";
// Worst first. A cluster badge (zoomed in) takes the worst state of the
// nodes inside it, so a group holding an alarm reads red. (Same order as the server.)
const STATE_RANK = ["ALARM", "WATCH", "MISSING", "WARMUP", "NORMAL", "NO_DATA", "NEVER"];
const rankOf = st => { const i = STATE_RANK.indexOf(st); return i < 0 ? STATE_RANK.length : i; };
const worstOf = states => states.reduce((a, b) => (rankOf(b) < rankOf(a) ? b : a), "NEVER");
const PILL_TEXT = { FIRE_ALARM: "Fire alarm", WATCH: "Watch", ALL_CLEAR: "All clear", NO_DATA: "No data received",
                    WARMING_UP: "Warming up" };
const BASE_TITLE = document.title;
// Shown only while a node is in alarm-mode transmission (never on its normal heartbeat).
const RAPID_TIP = "This node's own sensor noticed a change, so it is now reporting every minute " +
  "instead of on its normal schedule. This is not a fire alarm by itself: the console " +
  "raises WATCH or FIRE ALARM only when the readings confirm it.";

let CC = null;          // /api/console/config
let map = null, layers = null;
let networkMarker = null, networkSignature = "", lastDetailMode = null, droneBase = null;
const gatewayMarkers = new Map();
const reducedMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
// At zoom 18 the 33 m cell spacing leaves room for each operator label.
const detailZoom = () => CC.demo_layout
  ? Math.min(Math.max(CC.map.node_detail_zoom, 18), CC.map.primary_tiles.max_zoom, CC.map.fallback_tiles.max_zoom)
  : CC.map.node_detail_zoom;

// --- clock ----------------------------------------------------------------

function tickClock() {
  const d = new Date();
  document.getElementById("clock").textContent =
    d.toLocaleDateString(undefined, { weekday: "short", day: "2-digit", month: "short", year: "numeric" }) +
    "  " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

// --- formatting shared by the map and the panel --------------------------------

// GPS to 5 decimals (about 1 m), hemisphere spelled out.
function fmtGPS(lat, lon) {
  if (lat === null || lat === undefined || lon === null || lon === undefined) return "not on the map";
  return `${Math.abs(lat).toFixed(5)}° ${lat >= 0 ? "N" : "S"}, ${Math.abs(lon).toFixed(5)}° ${lon >= 0 ? "E" : "W"}`;
}

// Days of silence (simulated days under DEMO_SPEEDUP), from the server.
function daysText(days) {
  if (days === null || days === undefined) return "missing";
  return days < 1 ? "missing <1 day" : `missing ${days} day${days === 1 ? "" : "s"}`;
}

// A node that never reported: days since monitoring began, once that is a day or more.
function unheardText(n) {
  return n.days_missing >= 1 ? `${daysText(n.days_missing)} (never reported)` : "not heard from yet";
}

// --- map ------------------------------------------------------------------

function initMap() {
  if (!window.L) { setMapNote("Map library failed to load"); return; }
  const m = CC.map;
  map = L.map("map", { zoomControl: true, zoomAnimation: !reducedMotion(),
    fadeAnimation: !reducedMotion(), markerZoomAnimation: !reducedMotion()
  }).setView([m.centre_lat, m.centre_lon], m.zoom);

  // Satellite first; if it never delivers a tile, fall back to the street map;
  // if that fails too we are offline: markers and zones still draw.
  const layerFor = t => L.tileLayer(t.url, { maxZoom: t.max_zoom, maxNativeZoom: t.max_native_zoom ?? t.max_zoom, attribution: t.attribution });
  const primary = layerFor(m.primary_tiles), fallback = layerFor(m.fallback_tiles);
  watchTiles(primary, () => {
    map.removeLayer(primary);
    fallback.addTo(map);
    setMapNote("Satellite imagery unavailable: street map shown.");
    watchTiles(fallback, () => setMapNote("No map imagery (no internet?). Markers are still placed correctly."));
  });
  primary.addTo(map);

  // Overview: network summary, infrastructure and individual unclustered alarms.
  // Detail: show every grid cell without clustering; other deployments retain
  // the existing clustering behavior. Reuse layers across zoom transitions.
  layers = {
    overview: L.layerGroup(),
    connections: L.layerGroup(),
    links: L.layerGroup().addTo(map),     // dashed cluster -> gateway lines, every zoom
    clusterTags: L.layerGroup(),          // clickable cluster chips (gateway focus or zoomed in)
    focus: L.layerGroup().addTo(map),     // outline of the cluster being inspected
    coverage: L.layerGroup(),
    reserve: L.layerGroup().addTo(map),   // reserve outline and gateway sectors
    range: L.layerGroup(),                // theoretical gateway range circles
    zones: L.layerGroup(),                // red alarm zones (zoomed in)
    gateways: L.layerGroup().addTo(map),
    alarms: L.layerGroup(),               // zoomed out: nodes in ALARM only
    nodes: L.markerClusterGroup({         // zoomed in
      maxClusterRadius: m.cluster_radius_px,
      disableClusteringAtZoom: CC.demo_layout ? detailZoom() : undefined,
      showCoverageOnHover: false,
      spiderfyOnMaxZoom: true,
      iconCreateFunction: clusterIcon,
    }),
  };
  // One control: background imagery, plus the overlays that actually exist.
  const overlays = {};
  if (CC.demo_layout?.reserve) {
    overlays["Reserve and gateway sectors"] = layers.reserve;
    if (CC.demo_layout.gateway_range_m) {
      overlays[`Gateway range (theory, ${Math.round(CC.demo_layout.gateway_range_m)} m)`] = layers.range;
      layers.range.addTo(map);
    }
  }
  if (!CC.demo_layout) overlays["Sensor coverage (not fire extent)"] = layers.coverage;
  L.control.layers({ "Satellite": primary, "Street map": fallback }, overlays, { collapsed: true }).addTo(map);
  map.on("zoomend", applyDetail);
  const c = CC.sites.centre;
  const plannedMarker = (entity, css, symbol, role) => {
    if (!entity) return null;
    return L.marker([entity.lat, entity.lon], {
      icon: L.divIcon({ className: "", html: `<div class="${css}">${symbol}</div>`,
                        iconSize: [28, 28], iconAnchor: [14, 14] }),
      zIndexOffset: -2000,
    }).bindTooltip(`${esc(role)} · planned`, {
      direction: css === "control-icon" ? "bottom" : "top",
      offset: [0, css === "control-icon" ? 15 : -15], permanent: true, className: "asset-label"
    })
      .bindPopup(`<b>${esc(entity.label)}</b><br>${esc(entity.entity_id)}<br>${esc(role)}<br>` +
                 `GPS ${esc(fmtGPS(entity.lat, entity.lon))}<br>` +
                 "Planned physical component; no operational telemetry.<br>Provisional demonstration position; not surveyed.")
      .addTo(map);
  };
  plannedMarker(c, "control-icon", '<svg viewBox="0 0 28 28" aria-hidden="true"><rect x="4" y="5" width="20" height="14" rx="2"/><path d="M14 19v4M8 24h12M7 13h3l2-4 4 7 2-3h3"/></svg>', c.role);
  const droneSymbol = '<svg viewBox="0 0 28 28" aria-hidden="true"><path d="M7 7L21 21M7 21L21 7" stroke="white" stroke-width="2"/><g fill="none" stroke="white" stroke-width="2"><circle cx="7" cy="7" r="4"/><circle cx="21" cy="7" r="4"/><circle cx="7" cy="21" r="4"/><circle cx="21" cy="21" r="4"/></g></svg>';
  droneBase = plannedMarker(CC.sites.drone, "drone-icon", droneSymbol, `${CC.sites.drone?.label || "Drone"} base`);
  if (CC.demo_layout) {
    initReserve();
    L.polygon(CC.demo_layout.boundary, { color: "#91c6cf", weight: 1, dashArray: "5 5",
      fill: true, fillColor: "#6cb2c1", fillOpacity: 0.07, interactive: false }).addTo(map);
    initNetwork();
    initClusterLinks();
    const entities = [...CC.sites.nodes, ...CC.sites.gateways, c, CC.sites.drone].filter(Boolean);
    const bounds = L.latLngBounds([...CC.demo_layout.boundary, ...(CC.demo_layout.reserve || []),
                                   ...entities.map(e => [e.lat, e.lon])]);
    siteView = { bounds, options: { padding: [45, 45], maxZoom: Math.min(m.node_detail_zoom, detailZoom() - 1) } };
    map.fitBounds(bounds, { ...siteView.options, animate: !reducedMotion() });
  }

  applyDetail();
  const legend = L.control({ position: "topright" });
  legend.onAdd = () => {
    // Collapsible, so it does not hide markers on small screens.
    const el = L.DomUtil.create("details", "legend");
    el.open = window.innerWidth >= 1200 && window.innerHeight >= 800;
    L.DomEvent.disableClickPropagation(el);
    el.innerHTML = `<summary>Legend</summary>${legendRows().join("")}`;
    return el;
  };
  legend.addTo(map);
  // Back to the whole site, from any zoom or drill-down level.
  const reset = L.control({ position: "topleft" });
  reset.onAdd = () => {
    const el = L.DomUtil.create("button", "leaflet-bar map-reset");
    el.type = "button";
    el.title = "Show the whole site";
    el.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 6V2h4M10 2h4v4M14 10v4h-4M6 14H2v-4"/></svg>Reset view';
    L.DomEvent.disableClickPropagation(el);
    el.onclick = () => openNetwork(null, null);
    return el;
  };
  reset.addTo(map);
  const crumbs = L.control({ position: "topleft" });
  crumbs.onAdd = () => {
    const el = L.DomUtil.create("nav", "map-crumbs");
    el.id = "map-crumbs";
    el.setAttribute?.("aria-label", "Map drill-down");
    L.DomEvent.disableClickPropagation(el);
    el.onclick = ev => {
      const b = ev.target.closest?.("button");
      if (b) openNetwork(b.dataset.gw, b.dataset.cluster);
    };
    mapCrumbs = el;
    return el;
  };
  crumbs.addTo(map);
  renderMapCrumbs();
  setMapNote("");
  // The map sits in a flex layout: re-measure whenever its box changes size.
  new ResizeObserver(() => map.invalidateSize()).observe(document.getElementById("map"));
}

// Only what this map can actually show: states that this reporting mode can
// produce, and layers that exist in this configuration.
function legendRows() {
  const legacy = CC.reporting_mode !== "event_driven", demo = !!CC.demo_layout;
  const planned = CC.sites.gateways.some(g => g.planned);
  const row = (icon, text) => `<div>${icon}${text}</div>`;
  return [
    row(`<i style="background:${STATE_COLOR.NORMAL}"></i>`, "Node: normal"),
    legacy ? row(`<i style="background:${STATE_COLOR.WATCH}"></i>`, "Node: watch") : "",
    row(`<i style="background:${STATE_COLOR.ALARM}"></i>`, "Node: fire alarm"),
    row(`<i class="missing"></i>`, legacy ? "Node: missing / warming up" : "Node: missing"),
    row(`<i class="hollow"></i>`, "Node: not heard from yet"),
    demo ? "" : row(`<i class="clu">5</i>`, "Group of nodes, worst state"),
    row(`<i class="gw"></i>`, "Gateway"),
    planned ? row(`<i class="gw planned"></i>`, "Gateway: planned, no nodes yet") : "",
    row(`<i class="link"></i>`, "Cluster to its gateway"),
    CC.demo_layout?.reserve ? row(`<i class="reserve"></i>`, "Reserve boundary") : "",
    CC.demo_layout?.reserve ? row(`<i class="sector"></i>`, "Gateway sector") : "",
    CC.demo_layout?.gateway_range_m ? row(`<i class="range"></i>`, `Gateway range, theory (${Math.round(CC.demo_layout.gateway_range_m)} m)`) : "",
    row(`<i class="mcu"></i>`, "Main Control Unit"),
    CC.sites.drone ? row(`<i class="drone"></i>`, CC.features?.drone_simulation ? "Drone base (simulated flights)" : "Drone base (planned)") : "",
    row(`<i class="zone"></i>`, demo ? "Fire alarm location" : "Alarm: sensor coverage"),
    `<div class="note">Press a gateway for its clusters. Positions are planned, not surveyed.</div>`,
  ].filter(Boolean);
}

// Reserve outline, one sector per gateway, and each gateway's theoretical
// range. From the coverage study (docs/GATEWAY-COVERAGE.md), not measurements.
function initReserve() {
  const d = CC.demo_layout;
  if (!d.reserve) return;
  L.polygon(d.reserve, { color: "#e8d36a", weight: 2, fill: false, interactive: false, className: "reserve-line" })
    .addTo(layers.reserve);
  for (const s of d.sectors || []) {
    if (s.polygon.length < 3) continue;
    sectorShapes.set(s.gateway_id, { ring: s.polygon,
      shape: L.polygon(s.polygon, { ...SECTOR_OFF, interactive: false }).addTo(layers.reserve) });
  }
  if (!d.gateway_range_m) return;
  for (const g of CC.sites.gateways) {
    L.circle([g.lat, g.lon], { radius: d.gateway_range_m, color: g.planned ? "#8aa4b8" : "#4aa3ff",
      weight: 1, opacity: 0.6, dashArray: "6 6", fill: true, fillOpacity: 0.04, interactive: false })
      .addTo(layers.range);
  }
}

// Geometry is supplied by the API; sorted devEUI is the configured NW-to-SE cell order.
function initNetwork() {
  const grid = [...CC.sites.nodes].sort((a,b) => a.dev_eui.toLowerCase().localeCompare(b.dev_eui.toLowerCase()));
  const { rows, columns, boundary } = CC.demo_layout;
  if (rows * columns !== grid.length) return;
  for (let row=0; row<rows; row++) for (let col=0; col<columns; col++) {
    const index = row * columns + col;
    for (const neighbor of [col+1 < columns ? index+1 : -1, row+1 < rows ? index+columns : -1]) {
      if (neighbor < 0) continue;
      L.polyline([grid[index], grid[neighbor]].map(n => [n.lat,n.lon]), {
        color: "#9ccbd3", weight: 1.5, opacity: 0.45, interactive: false,
        className: "grid-connection",
      }).addTo(layers.connections);
    }
  }
  const bounds = L.latLngBounds(boundary);
  networkMarker = L.marker(bounds.getCenter(), { icon: networkIcon("NEVER", "Awaiting contact"),
    zIndexOffset: -3000, title: "Inspect sensor network", keyboard: true })
    .on("click", () => {
      const zoom = Math.min(CC.map.primary_tiles.max_zoom, CC.map.fallback_tiles.max_zoom,
        Math.max(detailZoom(), map.getBoundsZoom(bounds, false, L.point(130,130))));
      if (reducedMotion()) map.setView(bounds.getCenter(), zoom, { animate: false });
      else map.flyTo(bounds.getCenter(), zoom, { duration: 0.45 });
    }).addTo(layers.overview);
}

// One dashed line from each cluster (the mean position of its nodes) to its
// gateway. Assignment from the site registry, not a measured radio link.
function initClusterLinks() {
  const gws = new Map(CC.sites.gateways.map(g => [g.gateway_id, g]));
  const clusters = new Map();
  for (const n of CC.sites.nodes) {
    if (!n.block || !gws.has(n.gateway_id)) continue;
    const key = `${n.gateway_id}|${n.block}`;
    if (!clusters.has(key)) clusters.set(key, { block: n.block, gw: gws.get(n.gateway_id), pts: [] });
    clusters.get(key).pts.push([n.lat, n.lon]);
  }
  for (const c of clusters.values()) {
    const mid = [c.pts.reduce((a, p) => a + p[0], 0) / c.pts.length, c.pts.reduce((a, p) => a + p[1], 0) / c.pts.length];
    L.polyline([mid, [c.gw.lat, c.gw.lon]], { color: "#4aa3ff", weight: 2, opacity: 0.8, dashArray: "8 6",
      interactive: false, className: "cluster-link" }).addTo(layers.links);
    // A chip at the cluster's west end, clear of the node labels: press it
    // to inspect the cluster's nodes.
    const west = c.pts.reduce((a, p) => (p[1] < a[1] ? p : a));
    const gw = c.gw.gateway_id, block = c.block;
    const chip = L.marker(west, { icon: chipIcon(block, c.pts.length, "NEVER", false), zIndexOffset: 4000,
      keyboard: true, title: `Inspect cluster ${block}`, gw, sig: "" })
      .on("click", () => openNetwork(gw, block));
    clusterGeo.set(`${gw}|${block}`, { gw, block, pts: c.pts, chip });
  }
}

// --- map drill-down: all gateways -> one gateway -> one cluster -> its nodes ---
// Driven by openNetwork, so the map and the Sensor Network panel always show
// the same gateway and cluster.

const SECTOR_OFF = { color: "#e8d36a", weight: 1, opacity: 0.55, dashArray: "2 6", fill: false };
const SECTOR_ON = { color: "#ffe36e", weight: 3, opacity: 1, dashArray: null, fill: true, fillColor: "#ffe36e", fillOpacity: 0.12 };
const mapFocus = { gw: null, cluster: null };
const clusterGeo = new Map();    // "gateway|cluster" -> { gw, block, pts, chip }
const sectorShapes = new Map();  // gateway_id -> { ring, shape }
let siteView = null, mapCrumbs = null, lastTree = [];

function chipIcon(block, count, state, selected) {
  const what = { ALARM: "Fire alarm", WATCH: "Watch", MISSING: "missing", NEVER: "no contact" }[state];
  return L.divIcon({ className: "anchor-icon", iconSize: [0, 0],
    html: `<button type="button" class="cluster-chip ${esc(state)}${selected ? " selected" : ""}"><b>Cluster ${esc(block)}</b>` +
          `<small>${count} nodes${what ? ` · ${what}` : ""}</small></button>` });
}

// Chips: every cluster once zoomed in to nodes; the focused gateway's
// clusters when zoomed out. Icons are rebuilt only when their state changes.
function drawClusterChips() {
  if (!layers) return;
  const detail = detailMode();
  const states = new Map(lastTree.flatMap(g => g.clusters.map(c => [`${g.gateway_id}|${c.cluster}`, c.state])));
  for (const [key, c] of clusterGeo) {
    const show = detail || c.gw === mapFocus.gw;
    const state = states.get(key) || "NEVER";
    const selected = c.gw === mapFocus.gw && c.block === mapFocus.cluster;
    const sig = `${state}|${selected}`;
    if (c.chip.options.sig !== sig) { c.chip.setIcon(chipIcon(c.block, c.pts.length, state, selected)); c.chip.options.sig = sig; }
    if (show) layers.clusterTags.addLayer(c.chip); else layers.clusterTags.removeLayer(c.chip);
  }
}

function gatewayLabel(id) {
  return CC.sites.gateways.find(g => g.gateway_id === id)?.label || id;
}

function renderMapCrumbs() {
  if (!mapCrumbs) return;
  const { gw, cluster } = mapFocus;
  mapCrumbs.hidden = !gw;
  if (!gw) { mapCrumbs.innerHTML = ""; return; }
  mapCrumbs.innerHTML = [
    `<button type="button">‹ All gateways</button>`,
    `<button type="button" data-gw="${esc(gw)}"${cluster ? "" : ' aria-current="page"'}>${esc(gatewayLabel(gw))}</button>`,
    cluster ? `<button type="button" aria-current="page" data-gw="${esc(gw)}" data-cluster="${esc(cluster)}">Cluster ${esc(cluster)}</button>` : "",
  ].filter(Boolean).join('<span aria-hidden="true">›</span>');
}

function flyToPoints(points, minZoom, maxZoom) {
  const bounds = L.latLngBounds(points);
  const top = Math.min(maxZoom, CC.map.primary_tiles.max_zoom, CC.map.fallback_tiles.max_zoom);
  const zoom = Math.min(top, Math.max(minZoom, map.getBoundsZoom(bounds, false, L.point(110, 110))));
  if (reducedMotion()) map.setView(bounds.getCenter(), zoom, { animate: false });
  else map.flyTo(bounds.getCenter(), zoom, { duration: 0.6 });
}

function focusMap(gw, cluster) {
  if (!map || !layers) return;
  const g = CC.sites.gateways.find(x => x.gateway_id === gw);
  mapFocus.gw = g ? gw : null;
  mapFocus.cluster = g && clusterGeo.has(`${gw}|${cluster}`) ? cluster : null;
  for (const [id, s] of sectorShapes) s.shape.setStyle(id === mapFocus.gw ? SECTOR_ON : SECTOR_OFF);
  layers.focus.clearLayers();
  const own = [...clusterGeo.values()].filter(c => c.gw === mapFocus.gw);
  if (mapFocus.cluster) {
    // Cluster: outline its nodes and zoom in far enough to show each one.
    const pts = clusterGeo.get(`${gw}|${cluster}`).pts, pad = 0.00012;
    const lats = pts.map(p => p[0]), lons = pts.map(p => p[1]);
    const [s, n, w, e] = [Math.min(...lats) - pad, Math.max(...lats) + pad, Math.min(...lons) - pad, Math.max(...lons) + pad];
    L.polygon([[s, w], [n, w], [n, e], [s, e]], { color: "#ffe36e", weight: 2, dashArray: "4 4", fill: false,
      interactive: false, className: "cluster-focus" }).addTo(layers.focus);
    flyToPoints(pts, detailZoom(), 99);
  } else if (g) {
    // Gateway: its sector and its clusters, just short of node detail.
    const pts = [[g.lat, g.lon], ...own.flatMap(c => c.pts)];
    if (!own.length) pts.push(...(sectorShapes.get(gw)?.ring || []));
    flyToPoints(pts, 0, detailZoom() - 1);
  } else if (siteView) {
    map.fitBounds(siteView.bounds, { ...siteView.options, animate: !reducedMotion() });
  } else {
    map.setView([CC.map.centre_lat, CC.map.centre_lon], CC.map.zoom, { animate: !reducedMotion() });
  }
  applyDetail(true);
  renderMapCrumbs();
}

function networkIcon(state, status) {
  return L.divIcon({ className: "network-anchor", iconSize: [220,70], iconAnchor: [110,100],
    html: `<div class="network-summary ${esc(state)}"><div class="ns-head"><strong>Sensor network</strong>` +
          `<span>${CC.sites.nodes.length} nodes</span></div><small>${esc(status)}</small><em>Select to view the grid</em></div>` });
}

function updateNetwork(status) {
  if (!networkMarker) return;
  const ids = new Set(CC.sites.nodes.map(n => n.dev_eui));
  const observed = status.nodes.filter(n => ids.has(n.dev_eui));
  const alarm = observed.filter(n => n.state === "ALARM").length;
  const watch = observed.filter(n => n.state === "WATCH").length;
  const missing = observed.filter(n => n.missing).length;
  const text = [alarm ? `${alarm} in fire alarm` : null, watch ? `${watch} on watch` : null,
    `${observed.length-missing} reporting`, missing ? `${missing} missing` : null,
    observed.length < ids.size ? `${ids.size-observed.length} not yet heard` : null].filter(Boolean).join(" · ");
  const state = worstOf(observed.map(nodeMapState));
  const signature = state + text;
  if (signature !== networkSignature) {
    networkMarker.setIcon(networkIcon(state, text));
    networkSignature = signature;
  }
}

function gatewayIcon(online, planned) {
  return L.divIcon({ className: "", iconSize: [30,30], iconAnchor: [15,15],
    html: `<div class="gateway-asset ${planned ? 'planned' : online ? 'online' : 'offline'}"><svg viewBox="0 0 28 28" aria-hidden="true"><path d="M14 8v16M9 24h10M11 19h6M10 8a4 4 0 0 1 8 0M6 8a8 8 0 0 1 16 0"/><circle cx="14" cy="8" r="2"/></svg></div>` });
}

function watchTiles(layer, onDead) {
  let loaded = 0, errors = 0, fired = false;
  layer.on("tileload", () => { loaded++; });
  layer.on("tileerror", () => {
    if (++errors >= 4 && loaded === 0 && !fired) { fired = true; onDead(); }
  });
}

function setMapNote(text) {
  document.getElementById("map-note").textContent = text || "Bkassine forest - positions are PLACEHOLDERS";
}

function nodePopup(n) {
  const r = n.latest || {};
  return `<b>${esc(n.label)}</b> ${badge(n.state)} ${n.missing ? badge("MISSING") : ""}<br>
    ${n.rapid_updates ? `<span class="rapid" title="${esc(RAPID_TIP)}">RAPID UPDATES</span> reporting every minute<br>` : ""}
    ${n.last_message_type ? `Last message: ${esc(n.last_message_type)}<br>` : ""}
    Cluster ${esc(n.block || "-")} · gateway ${esc(n.gateway_label || "-")}<br>
    Last seen ${esc(fmtAgo(n.last_seen))}${n.missing ? ` · ${esc(daysText(n.days_missing))}` : ""}<br>
    GPS ${esc(fmtGPS(n.lat, n.lon))} <span class="muted small">(${n.surveyed ? "surveyed" : "planned grid position"})</span><br>
    T ${fmt(r.temperature_c, 1)} &deg;C · RH ${fmt(r.humidity_pct, 0)} % · gas ${gasVs(r)}<br>
    <span class="muted small">devEUI ${esc(n.dev_eui)}</span><br>
    <a href="/node/${encodeURIComponent(n.dev_eui)}">Open node detail</a><br>${clearAlarmButton(n, CC.clear_alarm_enabled)}`;
}

const detailMode = () => map.getZoom() >= detailZoom();

function applyDetail(force) {
  const detail = detailMode();
  drawClusterChips();
  if (detail === lastDetailMode && !force) return;
  lastDetailMode = detail;
  document.getElementById("map").classList.toggle("network-detail", detail);
  if (detail) {
    map.removeLayer(layers.overview);
    map.addLayer(layers.connections);
    map.addLayer(layers.clusterTags);
    map.removeLayer(layers.alarms);
    map.addLayer(layers.zones);
    if (!map.hasLayer(layers.nodes)) { map.addLayer(layers.nodes); layers.nodes.refreshClusters(); }
  } else {
    map.removeLayer(layers.connections);
    // A focused gateway shows its clusters in place of the network summary.
    if (mapFocus.gw) { map.removeLayer(layers.overview); map.addLayer(layers.clusterTags); }
    else { map.addLayer(layers.overview); map.removeLayer(layers.clusterTags); }
    map.removeLayer(layers.nodes);
    map.removeLayer(layers.zones);
    map.addLayer(layers.alarms);
  }
  renderMapInfo();
}

// Node markers (zoomed in) and alarm markers (zoomed out) are kept and
// updated in place, keyed by devEUI: an open popup survives the 5 s refresh,
// and adding nodes to the site registry needs no change here.
const nodeMarkers = new Map(), alarmMarkers = new Map();
let lastUnplaced = null;

function nodeIcon(state, label, hollow) {
  const order = Math.max(0, CC.sites.nodes.findIndex(n => n.label === label));  // stagger only
  return L.divIcon({ className: "anchor-icon", iconSize: [0, 0],
    html: `<div style="--node-delay:${Math.min(order, 8) * 25}ms" class="node-pin ${esc(state)}${hollow ? " hollow" : ""}"><i></i><span>${esc(label)}</span></div>` });
}

function clusterIcon(cluster) {
  const state = worstOf(cluster.getAllChildMarkers().map(mk => mk.options.mapState));
  return L.divIcon({ className: "anchor-icon", iconSize: [0, 0],
    html: `<div class="cluster-badge ${esc(state)}">${cluster.getChildCount()}</div>` });
}

function nodeMapState(n) {
  if (n.state === "ALARM" || n.state === "WATCH") return n.state;  // silence never hides an alarm
  return n.missing ? "MISSING" : n.state;
}

function prune(store, keep, group) {
  for (const [k, layer] of store) if (!keep.has(k)) { group.removeLayer(layer); store.delete(k); }
}

// Zoomed out: every node in ALARM, each its own red marker (no grouping, no
// other states). A node in ALARM stays here even if it then falls silent.
function drawAlarmMarkers(s) {
  const keep = new Set();
  for (const n of s.nodes) {
    if (n.state !== "ALARM" || n.lat === null) continue;
    keep.add(n.dev_eui);
    const mk = alarmMarkers.get(n.dev_eui);
    if (mk) { mk.setPopupContent(nodePopup(n)); continue; }
    alarmMarkers.set(n.dev_eui, L.marker([n.lat, n.lon], {
      icon: nodeIcon("ALARM", n.label, false), zIndexOffset: 5000,
    }).bindPopup(nodePopup(n)).addTo(layers.alarms));
  }
  prune(alarmMarkers, keep, layers.alarms);
}

function drawNodes(s) {
  const keep = new Set(), add = [];
  let changed = false;
  const place = (key, state, label, hollow, latlng, popup, tip) => {
    keep.add(key);
    let prev = nodeMarkers.get(key);
    if (prev && prev.options.hollow !== hollow) {  // first uplink from a never-seen node: rebuild with a popup
      layers.nodes.removeLayer(prev);
      nodeMarkers.delete(key);
      prev = null;
      changed = true;
    }
    if (!prev) {
      const mk = L.marker(latlng, { icon: nodeIcon(state, label, hollow), mapState: state, hollow,
                                    zIndexOffset: 1000 * (STATE_RANK.length - rankOf(state)) });
      if (popup) mk.bindPopup(popup); else mk.bindTooltip(tip, { direction: "right", offset: [12, 0] });
      nodeMarkers.set(key, mk);
      add.push(mk);
      return;
    }
    if (prev.options.mapState !== state) {
      prev.options.mapState = state;
      prev.setIcon(nodeIcon(state, label, hollow));
      prev.setZIndexOffset(1000 * (STATE_RANK.length - rankOf(state)));
      changed = true;
    }
    if (popup && prev.getPopup()) prev.setPopupContent(popup);
  };
  for (const n of s.nodes) {
    if (n.lat === null) continue;
    place(n.dev_eui, nodeMapState(n), n.label, false, [n.lat, n.lon], nodePopup(n));
  }
  for (const n of s.never_seen) {
    place(n.dev_eui, "NEVER", n.label, true, [n.lat, n.lon], null, `${esc(n.label)} - ${esc(unheardText(n))}`);
  }
  const before = nodeMarkers.size;
  prune(nodeMarkers, keep, layers.nodes);
  if (add.length) layers.nodes.addLayers(add);
  // A node turning red must turn its cluster badge red too. (Off the map the
  // group has no clusters yet; applyDetail refreshes when it is shown.)
  if (map.hasLayer(layers.nodes) && (changed || add.length || nodeMarkers.size !== before)) {
    layers.nodes.refreshClusters();
  }
}

function renderMapInfo() {
  const el = document.getElementById("map-info");
  if (!el) return;
  const u = lastUnplaced;
  const fg = mapFocus.gw && CC.sites.gateways.find(g => g.gateway_id === mapFocus.gw);
  const mode = mapFocus.cluster
    ? `Cluster ${mapFocus.cluster} · press a node for its readings`
    : fg ? (fg.planned ? `${fg.label} · planned, no nodes in its sector yet`
                       : `${fg.label} · its sector is highlighted; press a cluster for its nodes`)
    : detailMode()
    ? (CC.demo_layout ? "Grid detail · lines show cell adjacency only, not wireless links" : "Showing nodes (nearby nodes grouped)")
    : (CC.demo_layout ? "Overview · press the sensor network to zoom to the pilot grid, or a gateway for its clusters."
      : "System overview · showing infrastructure and active alarms. Zoom in to see nodes.");
  let warn = "";
  if (u && u.alerting.length) {
    warn = `<div class="unplaced alert">${u.in_alarm ? "ALARM" : "WATCH"} on a node not on this map ` +
      `(no site entry): ${esc(u.alerting.join(", "))}</div>`;
  } else if (u && u.count) {
    warn = `<div class="unplaced">${u.count} node${u.count === 1 ? "" : "s"} not on the map (no site entry)</div>`;
  }
  el.innerHTML = `<div>${esc(mode)}</div>${warn}`;
}

function drawMap(s) {
  if (!map) return;
  drawAlarmMarkers(s);
  drawNodes(s);
  updateNetwork(s);
  lastUnplaced = s.unplaced;
  lastTree = s.network_tree || [];
  drawClusterChips();
  renderMapInfo();
  lastAlarmNodes = s.nodes.filter(n => n.state === "ALARM");
  lastZones = s.alarm_zones;
  drawZones();

  const keepGateways = new Set();
  for (const g of s.gateways) {
    if (g.lat === null) continue;
    keepGateways.add(g.gateway_id);
    const state = g.planned ? "planned" : g.online ? "online" : (g.last_seen ? "OFFLINE" : "never heard");
    const sector = CC.demo_layout?.sectors?.find(x => x.gateway_id === g.gateway_id);
    const range = CC.demo_layout?.gateway_range_m;
    const popup = `<b>${esc(g.label)}</b><br>Gateway ${esc(g.gateway_id)}<br>` +
      (g.planned ? "Planned: no nodes in its sector yet<br>" : `${esc(state)} · last heard ${esc(fmtAgo(g.last_seen))}<br>`) +
      (sector ? `Sector ${sector.area_km2} km²` : "") + (range ? ` · range in forest ≈ ${Math.round(range)} m (theory)` : "") +
      `<br>GPS ${esc(fmtGPS(g.lat, g.lon))}<br>Planned position from the coverage study; not surveyed.`;
    let mk = gatewayMarkers.get(g.gateway_id);
    if (!mk) {
      const id = g.gateway_id;
      mk = L.marker([g.lat, g.lon], { zIndexOffset: -1000, icon: gatewayIcon(g.online, g.planned) })
        .bindPopup(popup, { autoPan: false }).bindTooltip('', { permanent: true, direction: "top", offset: [0, -17], className: "asset-label" })
        // First press drills in to the gateway; pressing it again shows its details.
        .on("click", function () {
          if (mapFocus.gw === id && !mapFocus.cluster) return;
          this.closePopup?.();
          openNetwork(id, null);
        })
        .addTo(layers.gateways);
      gatewayMarkers.set(g.gateway_id, mk);
    }
    if (mk.options.online !== g.online) { mk.setIcon(gatewayIcon(g.online, g.planned)); mk.options.online = g.online; }
    mk.setLatLng([g.lat, g.lon]);
    mk.setPopupContent(popup);
    mk.setTooltipContent(`${esc(g.label)} · ${esc(state)}`);
  }
  prune(gatewayMarkers, keepGateways, layers.gateways);
}

let lastZones = [], lastAlarmNodes = [];

function alarmRadiusMeters() {
  const g = CC.demo_layout;
  return Math.min(15, 0.45 * g.square_size_m / Math.max(g.rows, g.columns));
}

function drawZones() {
  layers.zones.clearLayers();
  layers.coverage.clearLayers();
  if (CC.demo_layout) {
    const registered = new Map(CC.sites.nodes.map(n => [n.dev_eui, n]));
    for (const alarm of lastAlarmNodes) {
      const n = registered.get(alarm.dev_eui);
      if (!n) continue;
      // Geographic radius; no pixel stroke, shadow or expanding animation.
      L.circle([n.lat, n.lon], { radius: alarmRadiusMeters(), stroke: false,
        fillColor: "#e5322b", fillOpacity: 0.3, interactive: false,
        className: "cell-alarm-highlight" }).addTo(layers.zones);
    }
  }
  const coverageLayer = CC.demo_layout ? layers.coverage : layers.zones;
  for (const z of lastZones) {
    L.polygon(z.polygon, { color: CC.demo_layout ? "#79a8bf" : "#e5322b", weight: 1,
      dashArray: "4 4", fill: !CC.demo_layout, fillColor: "#e5322b", fillOpacity: 0.28, interactive: false })
      .addTo(coverageLayer);
    // Label above the zone, so it never covers the node markers inside it.
    const top = z.polygon.reduce((a, p) => (p[0] > a[0] ? p : a));
    L.tooltip({ permanent: true, direction: "top", className: CC.demo_layout ? "coverage-tip" : "zone-tip", offset: [0, -2] })
      .setLatLng(top).setContent(esc(z.label)).addTo(coverageLayer);
  }
}

// --- status -----------------------------------------------------------------

function gasVs(r) {
  const v = r && r.gas_vs_baseline_pct;
  if (v === null || v === undefined) return "-";
  return `<span class="${v < -10 ? "down" : ""}">${v > 0 ? "+" : ""}${v.toFixed(0)}%</span>`;
}

function setPill(pill, note) {
  const el = document.getElementById("pill");
  el.className = `pill ${pill}`;
  document.getElementById("pill-text").textContent = pill === "ALL_CLEAR" && CC.reporting_mode === "event_driven"
    ? "No fire reported" : PILL_TEXT[pill] || pill;
  document.getElementById("pill-note").textContent = note || "";
  document.title = pill === "FIRE_ALARM" ? `[FIRE ALARM] ${BASE_TITLE}`
    : pill === "WATCH" ? `[WATCH] ${BASE_TITLE}` : BASE_TITLE;
  sound.setAlarm(pill === "FIRE_ALARM");
}

function renderStatus(s) {
  const net = s.network;
  let note = s.pill_note;
  if (s.pill === "FIRE_ALARM") note = s.blocks.filter(b => b.state === "ALARM")
    .map(b => `Cluster ${b.block}${b.gateways?.length ? ` · ${b.gateways.join(", ")}` : ""}`).join("; ") + " · select to view";
  if (s.pill === "NO_DATA") note = net.latest_data_at ? `Last transmission received ${fmtAgo(net.latest_data_at)}` : "No transmissions received yet";
  if (s.pill === "WARMING_UP") note = "Learning baselines; fire detection not yet active";
  setPill(s.pill, note);

  const tw = document.getElementById("timing-warn");
  tw.hidden = !s.timing_warning;
  tw.textContent = s.timing_warning ? `Setup problem: ${s.timing_warning}` : "";

  document.getElementById("st-total").textContent = net.total_nodes;
  document.getElementById("st-alarm").textContent = net.in_alarm;
  document.getElementById("st-alarm-box").classList.toggle("hot", net.in_alarm > 0);
  // Planned gateways have no nodes yet, so they are not expected to be heard.
  document.getElementById("st-gw").textContent = `${net.gateways_online}/${net.gateways_total - net.gateways_planned}`;
  document.getElementById("st-gw-note").textContent = net.gateways_planned
    ? `gateways online · +${net.gateways_planned} planned` : "gateways online";

  // Which cluster and gateway; pressing a line opens that cluster's nodes.
  document.getElementById("blocks").innerHTML = s.blocks.map(b => {
    const gw = s.network_tree.find(g => g.clusters.some(c => c.cluster === b.block));
    const who = b.alerting.map(a => `${esc(a.label)}${a.state === "WATCH" ? " (watch)" : ""}`).join(", ");
    return `<button type="button" class="block-line ${esc(b.state)}" data-gw="${esc(gw?.gateway_id || "")}" data-cluster="${esc(b.block)}">
      ${b.state === "ALARM" ? "Fire alarm" : "Watch"} · Cluster ${esc(b.block)} · ${esc(b.gateways.join(", ") || "no gateway")}
      <small>Registered by ${who}${b.signals.length ? ` · ${esc(b.signals.join(", "))}` : ""} · select to view</small></button>`;
  }).join("");

  lastStatus = s;
  renderNetwork();
  desktopAlerts.update(s);
  droneSim.update(s);

  const k = CC.demo_speedup;
  const eventMode = CC.reporting_mode === "event_driven";
  document.getElementById("net-foot").textContent =
    `Heartbeat every ${minutes(eventMode ? CC.explicit_heartbeat_minutes : CC.heartbeat_minutes)}; a node is MISSING after ${minutes(eventMode ? CC.explicit_missing_minutes : CC.missing_after_minutes)} of silence.` +
    (eventMode ? " Local sensing continues between contacts; heartbeat confirms contact, not detector readiness. No routine measurement uplinks." : "") +
    (k !== 1 ? ` DEMO SPEED-UP x${k}.` : "");
  drawMap(s);
}

function minutes(m) {
  return m < 2 ? `${Math.round(m * 60)} s` : `${+m.toFixed(1)} min`;
}

function renderPhase2() {
  document.querySelectorAll(".phase2").forEach(p => {
    if (p.dataset.feature === "drone" && CC.features.drone_simulation && CC.sites.drone) {
      p.classList.add("sim");  // drawn by droneSim, never as live data
      droneSim.render();
      return;
    }
    const on = CC.features[p.dataset.feature];
    p.classList.toggle("enabled", on);
    p.querySelector(".phase2-body").textContent = on
      ? "Enabled · no data source configured" : "Not connected · planned for phase 2";
  });
}

// --- sensor network drill-down ------------------------------------------------
// Gateways first; press one for its clusters; press a cluster for its nodes.
// The choice survives the 5 s refresh.

let lastStatus = null;
const netView = { gw: null, cluster: null };

// Every press (panel, map, alarm line, notification) moves the map as well.
function openNetwork(gw, cluster) {
  netView.gw = gw || null; netView.cluster = cluster || null;
  renderNetwork();
  focusMap(netView.gw, netView.cluster);
}

const stateTag = stateLabel;

function card(cls, state, attrs, title, detail) {
  return `<button type="button" class="net-card ${cls} ${esc(state)}" ${attrs}>
    <span class="net-dot" aria-hidden="true"></span>
    <span class="net-main"><b>${title}</b><small>${detail}</small></span>
    <span class="badge ${esc(state)}">${esc(stateTag(state))}</span></button>`;
}

function missingBadge(n) {
  const t = daysText(n.days_missing);
  return `<span class="badge MISSING">${esc(t[0].toUpperCase() + t.slice(1))}</span>`;
}

function nodeRow(n) {
  if (!("state" in n)) {  // never reported
    return `<tr class="NEVER"><td><b>${esc(n.label)}</b><small class="gps">${esc(fmtGPS(n.lat, n.lon))}</small></td>
      <td>${n.days_missing >= 1 ? missingBadge(n) : badge("NEVER")}</td><td>${esc(unheardText(n))}</td><td></td><td></td><td></td></tr>`;
  }
  const r = n.latest || {};
  return `<tr class="${esc(n.state)}" data-eui="${esc(n.dev_eui)}">
    <td><b>${esc(n.label)}</b>${n.rapid_updates ? `<span class="rapid" title="${esc(RAPID_TIP)}">RAPID UPDATES</span>` : ""}
      <small class="gps" title="${n.surveyed ? "Surveyed GPS position" : "Planned grid position"}">${esc(fmtGPS(n.lat, n.lon))}</small></td>
    <td>${badge(n.state)} ${n.missing ? missingBadge(n) : ""} ${clearAlarmButton(n, CC.clear_alarm_enabled)}</td>
    <td title="${esc(n.last_seen && fmtTime(n.last_seen))}">${esc(fmtAgo(n.last_seen))}</td>
    <td class="num">${fmt(r.temperature_c, 1)}</td>
    <td class="num">${fmt(r.humidity_pct, 0)}</td>
    <td class="num">${gasVs(r)}</td></tr>`;
}

function renderNetwork() {
  const s = lastStatus;
  if (!s) return;
  const tree = s.network_tree;
  const gw = tree.find(g => g.gateway_id === netView.gw) || null;
  const cl = gw ? gw.clusters.find(c => c.cluster === netView.cluster) || null : null;
  if (!gw) netView.gw = null;
  if (!cl) netView.cluster = null;

  const crumbs = [`<button type="button"${gw ? "" : ' aria-current="page"'}>All gateways</button>`];
  if (gw) crumbs.push(`<button type="button" data-gw="${esc(gw.gateway_id)}"${cl ? "" : ' aria-current="page"'}>${esc(gw.label)}</button>`);
  if (cl) crumbs.push(`<button type="button" aria-current="page" data-gw="${esc(gw.gateway_id)}" data-cluster="${esc(cl.cluster)}">Cluster ${esc(cl.cluster)}</button>`);
  document.getElementById("net-crumbs").innerHTML = crumbs.join('<span aria-hidden="true">›</span>');

  const view = document.getElementById("net-view");
  if (!tree.length) {
    view.innerHTML = '<p class="muted">No gateways registered.</p>';
  } else if (!gw) {
    view.innerHTML = tree.map(g => g.planned ? card("gw planned", "PLANNED", `data-gw="${esc(g.gateway_id)}"`,
      `${esc(g.label)} <span class="muted">· planned</span>`, "No nodes in its sector yet") :
      card("gw", g.state, `data-gw="${esc(g.gateway_id)}"`,
      `${esc(g.label)} <span class="muted">· ${g.online ? "online" : "offline"}</span>`,
      `${g.clusters.length} cluster${g.clusters.length === 1 ? "" : "s"} · ${g.nodes} nodes` +
      (g.in_alarm ? ` · <strong class="hot">Fire alarm in cluster ${esc(g.clusters.filter(c => c.in_alarm).map(c => c.cluster).join(", "))}</strong>` : "") +
      (g.missing ? ` · ${g.missing} missing` : "") + " · press for clusters")).join("");
  } else if (!cl) {
    view.innerHTML = gw.clusters.map(c => card("cluster", c.state, `data-gw="${esc(gw.gateway_id)}" data-cluster="${esc(c.cluster)}"`,
      `Cluster ${esc(c.cluster)}`,
      `${c.nodes.length} nodes` + (c.alerting.length ? ` · <strong class="hot">${esc(c.alerting.join(", "))}</strong>` : "") +
      (c.missing ? ` · ${c.missing} missing` : "") + " · press for nodes")).join("") ||
      '<p class="muted">No clusters on this gateway.</p>';
  } else {
    const all = new Map([...s.nodes, ...s.never_seen].map(n => [n.dev_eui, n]));
    const rank = n => ("state" in n ? rankOf(nodeMapState(n)) : STATE_RANK.length);
    const members = cl.nodes.map(id => all.get(id)).filter(Boolean)
      .sort((a, b) => rank(a) - rank(b) || a.label.localeCompare(b.label));
    view.innerHTML = `<div class="node-list"><table class="nodes">
      <thead><tr><th>Node · GPS</th><th>State</th><th>Last seen</th><th class="num">T &deg;C</th><th class="num">RH %</th><th class="num">Gas vs base</th></tr></thead>
      <tbody id="nodes">${members.map(nodeRow).join("")}</tbody></table></div>`;
  }
}

document.getElementById("net-crumbs").addEventListener("click", ev => {
  const b = ev.target.closest("button");
  if (b) openNetwork(b.dataset.gw, b.dataset.cluster);
});

document.getElementById("net-view").addEventListener("click", ev => {
  if (ev.target.closest(".clear-alarm")) return;
  const tr = ev.target.closest("tr[data-eui]");
  if (tr) { location.href = `/node/${encodeURIComponent(tr.dataset.eui)}`; return; }
  const b = ev.target.closest(".net-card");
  if (b) openNetwork(b.dataset.gw, b.dataset.cluster);
});

// An alarm line, or the FIRE ALARM pill, opens the cluster with the alarm.
function showAlarmCluster(gw, cluster) {
  if (!gw) return;
  openNetwork(gw, cluster);
  document.getElementById("network").scrollIntoView({ block: "nearest", behavior: reducedMotion() ? "auto" : "smooth" });
}

document.getElementById("blocks").addEventListener("click", ev => {
  const b = ev.target.closest(".block-line");
  if (b) showAlarmCluster(b.dataset.gw, b.dataset.cluster);
});

document.getElementById("pill").addEventListener("click", () => {
  const first = document.querySelector("#blocks .block-line.ALARM");
  if (first) showAlarmCluster(first.dataset.gw, first.dataset.cluster);
});

// --- audible alarm ---------------------------------------------------------
// Off by default; the choice is remembered in this browser. Browsers block
// sound until the page has been clicked, so after a reload the button asks
// to be clicked once ("click to arm").

const sound = (() => {
  const KEY = "wildfire.console.sound";
  const btn = document.getElementById("sound");
  let on = false, ctx = null, timer = null, alarm = false;
  try { on = localStorage.getItem(KEY) === "on"; } catch (_) { /* storage blocked */ }

  const armed = () => ctx !== null && ctx.state === "running";

  function render() {
    btn.setAttribute("aria-pressed", String(on));
    btn.classList.toggle("needs-arm", on && !armed());
    btn.textContent = !on ? "Sound: OFF" : armed() ? "Sound: ON" : "Sound: ON - click to arm";
  }

  function arm() {
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      ctx.resume().then(() => { render(); update(); });
    } catch (_) { /* no Web Audio */ }
  }

  function beep() {
    if (!armed()) return;
    const t = ctx.currentTime;
    [880, 660, 880, 660].forEach((f, i) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "square"; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t + i * 0.25);
      g.gain.exponentialRampToValueAtTime(0.25, t + i * 0.25 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.25 + 0.23);
      o.connect(g).connect(ctx.destination);
      o.start(t + i * 0.25); o.stop(t + i * 0.25 + 0.24);
    });
  }

  function update() {
    const should = on && alarm && armed();
    if (should && !timer) { beep(); timer = setInterval(beep, 2000); }
    if (!should && timer) { clearInterval(timer); timer = null; }
  }

  btn.addEventListener("click", ev => {
    ev.stopPropagation();
    if (on && !armed()) { arm(); return; }   // "click to arm"
    on = !on;
    try { localStorage.setItem(KEY, on ? "on" : "off"); } catch (_) { /* not remembered */ }
    if (on) arm();
    render(); update();
  });
  // Any click on the page also counts as the user gesture that arms sound.
  document.addEventListener("click", () => { if (on && !armed()) arm(); });
  render();

  return { setAlarm(active) { alarm = active; update(); } };
})();

// --- simulated drone dispatch ------------------------------------------------
// Demo only (console.features.drone_simulation). On a fire alarm the drone
// "flies" from its base to the first node in alarm, and back when the alarm
// is cleared. Everything here is labelled SIMULATION: there is no drone link.

const droneSim = (() => {
  const SPEED_MS = 15;  // assumed cruise speed, m/s
  let target = null, marker = null, route = null, anim = null, phase = "base", since = null, info = "";

  const base = () => droneBase?.getLatLng();
  const el = () => document.querySelector("#p-drone .phase2-body");

  function render() {
    const body = el();
    if (!body || !CC.features.drone_simulation) return;
    const label = esc(CC.sites.drone?.label || "Drone");
    const channels = (CC.alert_channels || []).join(", ") || "none configured";
    const text = {
      base: `${label} at base, ready.`,
      enroute: `${label} dispatched ${info} · en route`,
      onscene: `${label} on scene ${info}`,
      returning: `${label} returning to base`,
    }[phase];
    body.innerHTML = `<span class="sim-tag">SIMULATION</span><span>${text}</span>` +
      (phase === "enroute" || phase === "onscene"
        ? `<small>Civil defence alerted through: ${esc(channels)}${since ? ` · ${esc(new Date(since).toLocaleTimeString(undefined, { hour12: false }))}` : ""}</small>` : "");
    document.getElementById("p-drone").classList.toggle("active", phase === "enroute" || phase === "onscene");
  }

  function fly(to, done) {
    cancelAnimationFrame(anim);
    const from = marker.getLatLng();
    const metres = map.distance(from, to);
    // Real flight time divided by the demo speed-up, shown over 4-12 s.
    const ms = reducedMotion() ? 0 : Math.min(12000, Math.max(4000, metres / SPEED_MS * 1000 / CC.demo_speedup));
    const t0 = performance.now();
    const step = now => {
      const k = ms ? Math.min(1, (now - t0) / ms) : 1;
      marker.setLatLng([from.lat + (to.lat - from.lat) * k, from.lng + (to.lng - from.lng) * k]);
      if (k < 1) anim = requestAnimationFrame(step); else done();
    };
    anim = requestAnimationFrame(step);
  }

  function ensureMarker() {
    if (marker || !base()) return;
    marker = L.marker(base(), { zIndexOffset: 6000, interactive: false,
      icon: L.divIcon({ className: "", iconSize: [34, 34], iconAnchor: [17, 17],
        html: '<div class="drone-flying" title="Simulated drone">✣</div>' }) });
  }

  function update(s) {
    if (!CC.features.drone_simulation || !map || !base()) return;
    ensureMarker();
    const alarm = s.nodes.filter(n => n.state === "ALARM" && n.lat !== null)
      .sort((a, b) => String(a.state_since).localeCompare(String(b.state_since)))[0];
    if (alarm && alarm.dev_eui !== target) {
      target = alarm.dev_eui;
      const to = L.latLng(alarm.lat, alarm.lon);
      const metres = map.distance(base(), to);
      info = `to ${esc(alarm.label)} (cluster ${esc(alarm.block || "-")}, ${esc(alarm.gateway_label || "-")}) · ` +
             `${Math.round(metres)} m · flight ≈ ${Math.round(metres / SPEED_MS)} s at ${SPEED_MS} m/s`;
      phase = "enroute"; since = Date.now();
      if (!map.hasLayer(marker)) marker.addTo(map);
      route?.remove();
      route = L.polyline([base(), to], { color: "#ff8f88", weight: 2, dashArray: "4 6", interactive: false }).addTo(map);
      render();
      fly(to, () => { phase = "onscene"; render(); });
    } else if (!alarm && target) {
      target = null; phase = "returning"; route?.remove(); route = null; render();
      fly(base(), () => { phase = "base"; marker.remove(); render(); });
    }
  }

  return { update, render };
})();

// --- desktop (browser) notifications -----------------------------------------
// Off by default; the choice is remembered in this browser. Once on, the
// operating system shows a notification for every NEW fire alarm (stays until
// dismissed) and every node that newly goes missing, even when this tab is in
// the background or minimised. The console tab must stay open. Clicking a
// notification brings the console forward on that cluster.

const desktopAlerts = (() => {
  const KEY = "wildfire.console.desktop-alerts";
  const btn = document.getElementById("desktop-alerts");
  const supported = "Notification" in window;
  let on = false, first = true;
  const alarmsSeen = new Set(), missingSeen = new Set();
  try { on = localStorage.getItem(KEY) === "on"; } catch (_) { /* storage blocked */ }

  const permission = () => (supported ? Notification.permission : "unsupported");
  const active = () => on && permission() === "granted";

  function render() {
    const p = permission();
    btn.disabled = p === "unsupported";
    btn.setAttribute("aria-pressed", String(active()));
    btn.classList.toggle("needs-arm", on && p !== "granted");
    btn.textContent = p === "unsupported" ? "Desktop alerts: not supported"
      : p === "denied" ? "Desktop alerts: BLOCKED"
      : active() ? "Desktop alerts: ON" : on ? "Desktop alerts: click to allow" : "Desktop alerts: OFF";
    btn.title = p === "denied"
      ? "The browser blocks notifications for this page. Allow them in the site settings (padlock icon), then reload."
      : "Show a Windows notification for every new fire alarm and every node that goes missing.";
  }

  function show(title, body, tag, sticky, gw, cluster) {
    try {
      const n = new Notification(title, { body, tag, requireInteraction: sticky });
      n.onclick = () => { window.focus(); if (gw) showAlarmCluster(gw, cluster); n.close(); };
    } catch (_) { /* e.g. a browser that only allows service-worker notifications */ }
  }

  function update(s) {
    const gwOf = id => s.network_tree.find(g => g.clusters.some(c => c.nodes.includes(id)));
    for (const n of s.nodes) {
      const key = `${n.dev_eui}:${n.alarm_event_id}`;
      if (n.state === "ALARM" && !alarmsSeen.has(key)) {
        alarmsSeen.add(key);
        if (active()) show(`Fire alarm: ${n.label}`,
          `Cluster ${n.block || "-"} · ${n.gateway_label || "no gateway"}\nGPS ${fmtGPS(n.lat, n.lon)}`,
          `alarm-${key}`, true, gwOf(n.dev_eui)?.gateway_id, n.block);
      }
      if (n.missing && !missingSeen.has(n.dev_eui)) {
        missingSeen.add(n.dev_eui);
        // Not for nodes already missing when the page opened: no flood on reload.
        if (active() && !first) show(`Node missing - ${n.label}`,
          `${daysText(n.days_missing)} · cluster ${n.block || "-"} · ${n.gateway_label || "no gateway"}`,
          `missing-${n.dev_eui}`, false, gwOf(n.dev_eui)?.gateway_id, n.block);
      }
      if (!n.missing) missingSeen.delete(n.dev_eui);
    }
    first = false;
  }

  btn.addEventListener("click", async ev => {
    ev.stopPropagation();
    if (active()) on = false;
    else {
      on = true;
      if (permission() === "default") { try { await Notification.requestPermission(); } catch (_) { /* old API */ } }
      if (permission() === "granted") show("Desktop alerts are on",
        "This console will notify you of new fire alarms and missing nodes. Keep this tab open.", "desktop-alerts-on", false);
    }
    try { localStorage.setItem(KEY, on ? "on" : "off"); } catch (_) { /* not remembered */ }
    render();
  });
  render();
  return { update };
})();

// --- polling ----------------------------------------------------------------

function showConn(err) {
  const el = document.getElementById("conn");
  if (!err) { el.hidden = true; return; }
  el.hidden = false;
  el.textContent = `Cannot load data: ${err.message} (retrying)`;
  setPill("NO_DATA", "Database connection unavailable");
}

window.addEventListener('alarm-cleared', async () => {
  try {
    renderStatus(await getJSON('/api/console/status'));
  } catch (err) { showConn(err); }
});

async function start() {
  tickClock();
  setInterval(tickClock, 1000);
  while (!CC) {
    try { CC = await getJSON("/api/console/config"); }
    catch (err) { showConn(err); await new Promise(r => setTimeout(r, 5000)); }
  }
  document.getElementById("title").textContent = CC.title;
  document.getElementById("subtitle").textContent = CC.subtitle;
  renderPhase2();
  initMap();

  const tick = async () => {
    try {
      const s = await getJSON("/api/console/status");
      renderStatus(s);
      showConn(null);
    } catch (err) {
      showConn(err);
    }
    setTimeout(tick, CC.poll_seconds * 1000);
  };
  tick();
}

start();
