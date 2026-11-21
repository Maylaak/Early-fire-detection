// Operator console: map, status pill, sensor network, event log, audible alarm.
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
const PILL_TEXT = { FIRE_ALARM: "FIRE ALARM", WATCH: "WATCH", ALL_CLEAR: "ALL CLEAR", NO_DATA: "NO DATA",
                    WARMING_UP: "WARMING UP" };
const BASE_TITLE = document.title;
// Shown only while a node is in alarm-mode transmission (never on its normal heartbeat).
const RAPID_TIP = "This node's own sensor noticed a change, so it is now reporting every minute " +
  "instead of on its normal schedule. This is not a fire alarm by itself: the console " +
  "raises WATCH or FIRE ALARM only when the readings confirm it.";

let CC = null;          // /api/console/config
let map = null, layers = null;
let networkMarker = null, networkSignature = "", lastDetailMode = null;
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
  L.control.layers({ "Satellite": primary, "Street map": fallback }, null, { collapsed: true }).addTo(map);

  // Overview: network summary, infrastructure and individual unclustered alarms.
  // Detail: show every grid cell without clustering; other deployments retain
  // the existing clustering behavior. Reuse layers across zoom transitions.
  layers = {
    overview: L.layerGroup(),
    connections: L.layerGroup(),
    coverage: L.layerGroup(),
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
  L.control.layers(null, { "Sensor coverage (not fire extent)": layers.coverage }, { collapsed: true }).addTo(map);
  map.on("zoomend", applyDetail);
  const c = CC.sites.centre;
  const plannedMarker = (entity, css, symbol, role) => {
    if (!entity) return;
    L.marker([entity.lat, entity.lon], {
      icon: L.divIcon({ className: "", html: `<div class="${css}">${symbol}</div>`,
                        iconSize: [28, 28], iconAnchor: [14, 14] }),
      zIndexOffset: -2000,
    }).bindTooltip(`${esc(role)} · planned`, {
      direction: css === "control-icon" ? "bottom" : "top",
      offset: [0, css === "control-icon" ? 15 : -15], permanent: true, className: "asset-label"
    })
      .bindPopup(`<b>${esc(entity.label)}</b><br>${esc(entity.entity_id)}<br>${esc(role)}<br>` +
                 "Planned physical component; no operational telemetry.<br>Provisional demonstration position; not surveyed.")
      .addTo(map);
  };
  plannedMarker(c, "control-icon", '<svg viewBox="0 0 28 28" aria-hidden="true"><rect x="4" y="5" width="20" height="14" rx="2"/><path d="M14 19v4M8 24h12M7 13h3l2-4 4 7 2-3h3"/></svg>', c.role);
  const droneSymbol = '<svg viewBox="0 0 28 28" aria-hidden="true"><path d="M7 7L21 21M7 21L21 7" stroke="white" stroke-width="2"/><g fill="none" stroke="white" stroke-width="2"><circle cx="7" cy="7" r="4"/><circle cx="21" cy="7" r="4"/><circle cx="7" cy="21" r="4"/><circle cx="21" cy="21" r="4"/></g></svg>';
  plannedMarker(CC.sites.drone, "drone-icon", droneSymbol, CC.sites.drone?.label || "Drone");
  if (CC.demo_layout) {
    L.polygon(CC.demo_layout.boundary, { color: "#91c6cf", weight: 1, dashArray: "5 5",
      fill: true, fillColor: "#6cb2c1", fillOpacity: 0.07, interactive: false }).addTo(map);
    initNetwork();
    const entities = [...CC.sites.nodes, ...CC.sites.gateways, c, CC.sites.drone].filter(Boolean);
    const bounds = L.latLngBounds([...CC.demo_layout.boundary, ...entities.map(e => [e.lat, e.lon])]);
    map.fitBounds(bounds, { padding: [45, 45], maxZoom: Math.min(m.node_detail_zoom, detailZoom() - 1), animate: !reducedMotion() });
  }

  applyDetail();
  const legend = L.control({ position: "topright" });
  legend.onAdd = () => {
    // Collapsible, so it does not hide markers on small screens.
    const el = L.DomUtil.create("details", "legend");
    el.open = window.innerWidth >= 1200 && window.innerHeight >= 800;
    L.DomEvent.disableClickPropagation(el);
    el.innerHTML = `<summary>Legend</summary>
      <div><i style="background:${STATE_COLOR.NORMAL}"></i>Normal</div>
      <div><i style="background:${STATE_COLOR.WATCH}"></i>Watch</div>
      <div><i style="background:${STATE_COLOR.ALARM}"></i>Alarm</div>
      <div><i style="background:${MISSING_COLOR}"></i>Missing / warming up</div>
      <div><i class="hollow"></i>Never reported</div>
      <div><i class="clu">5</i>Group of nodes, worst state</div>
      <div><i class="sq" style="background:var(--gateway)"></i>Gateway</div>
      <div><i class="sq" style="background:var(--centre);transform:none"></i>Main Control Unit</div>
      <div>✣ Drone (planned)</div>
      <div><i class="zone"></i>${CC.demo_layout ? "Alarm location (not fire extent)" : "Alarm: sensor coverage"}</div>
      <div class="note">Click the network summary to inspect the grid. Lines illustrate cell adjacency, not measured radio links.
        Zoom in to ${detailZoom()} to see single nodes.
        Positions are PLACEHOLDERS until a site survey.</div>`;
    return el;
  };
  legend.addTo(map);
  setMapNote("");
  // The map sits in a flex layout: re-measure whenever its box changes size.
  new ResizeObserver(() => map.invalidateSize()).observe(document.getElementById("map"));
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

function networkIcon(state, status) {
  return L.divIcon({ className: "network-anchor", iconSize: [220,70], iconAnchor: [110,85],
    html: `<div class="network-summary ${esc(state)}"><span class="network-glyph" aria-hidden="true">▦</span><div><strong>Sensor Network — ${CC.sites.nodes.length} Nodes</strong><small>${esc(status)}</small><em>Inspect grid ↗</em></div></div>` });
}

function updateNetwork(status) {
  if (!networkMarker) return;
  const ids = new Set(CC.sites.nodes.map(n => n.dev_eui));
  const observed = status.nodes.filter(n => ids.has(n.dev_eui));
  const alarm = observed.filter(n => n.state === "ALARM").length;
  const watch = observed.filter(n => n.state === "WATCH").length;
  const missing = observed.filter(n => n.missing).length;
  const text = [alarm ? `${alarm} FIRE ALARM` : null, watch ? `${watch} WATCH` : null,
    `${observed.length-missing} reporting`, missing ? `${missing} missing` : null,
    observed.length < ids.size ? `${ids.size-observed.length} never seen` : null].filter(Boolean).join(" · ");
  const state = worstOf(observed.map(nodeMapState));
  const signature = state + text;
  if (signature !== networkSignature) {
    networkMarker.setIcon(networkIcon(state, text));
    networkSignature = signature;
  }
}

function gatewayIcon(online) {
  return L.divIcon({ className: "", iconSize: [30,30], iconAnchor: [15,15],
    html: `<div class="gateway-asset ${online ? 'online' : 'offline'}"><svg viewBox="0 0 28 28" aria-hidden="true"><path d="M14 8v16M9 24h10M11 19h6M10 8a4 4 0 0 1 8 0M6 8a8 8 0 0 1 16 0"/><circle cx="14" cy="8" r="2"/></svg></div>` });
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
    Block ${esc(n.block || "-")} · last seen ${esc(fmtAgo(n.last_seen))}<br>
    T ${fmt(r.temperature_c, 1)} &deg;C · RH ${fmt(r.humidity_pct, 0)} % · gas ${gasVs(r)}<br>
    <span class="muted small">devEUI ${esc(n.dev_eui)}</span><br>
    <a href="/node/${encodeURIComponent(n.dev_eui)}">Open node detail</a><br>${clearAlarmButton(n, CC.clear_alarm_enabled)}`;
}

const detailMode = () => map.getZoom() >= detailZoom();

function applyDetail() {
  const detail = detailMode();
  if (detail === lastDetailMode) return;
  lastDetailMode = detail;
  document.getElementById("map").classList.toggle("network-detail", detail);
  if (detail) {
    map.removeLayer(layers.overview);
    map.addLayer(layers.connections);
    map.removeLayer(layers.alarms);
    map.addLayer(layers.zones);
    if (!map.hasLayer(layers.nodes)) { map.addLayer(layers.nodes); layers.nodes.refreshClusters(); }
  } else {
    map.removeLayer(layers.connections);
    map.addLayer(layers.overview);
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
  const order = Math.max(0, CC.sites.nodes.findIndex(n => n.label === label));
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
    place(n.dev_eui, "NEVER", n.label, true, [n.lat, n.lon], null, `${esc(n.label)} - never reported`);
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
  const mode = detailMode()
    ? (CC.demo_layout ? "Grid detail · lines show cell adjacency only, not wireless links" : "Showing nodes (nearby nodes grouped)")
    : (CC.demo_layout ? "System overview · click Sensor Network to inspect. Active alarms remain visible."
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
  renderMapInfo();
  lastAlarmNodes = s.nodes.filter(n => n.state === "ALARM");
  lastZones = s.alarm_zones;
  drawZones();

  const keepGateways = new Set();
  for (const g of s.gateways) {
    if (g.lat === null) continue;
    keepGateways.add(g.gateway_id);
    const state = g.online ? "online" : (g.last_seen ? "OFFLINE" : "never heard");
    const popup = `<b>${esc(g.label)}</b><br>Gateway ${esc(g.gateway_id)}<br>${esc(state)} · last heard ${esc(fmtAgo(g.last_seen))}<br>Provisional position; offset is not proof of radio range.`;
    let mk = gatewayMarkers.get(g.gateway_id);
    if (!mk) {
      mk = L.marker([g.lat, g.lon], { zIndexOffset: -1000, icon: gatewayIcon(g.online) })
        .bindPopup(popup).bindTooltip('', { permanent: true, direction: "top", offset: [0, -17], className: "asset-label" })
        .addTo(layers.gateways);
      gatewayMarkers.set(g.gateway_id, mk);
    }
    if (mk.options.online !== g.online) { mk.setIcon(gatewayIcon(g.online)); mk.options.online = g.online; }
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
    ? "NO FIRE REPORTED" : PILL_TEXT[pill] || pill;
  document.getElementById("pill-note").textContent = note || "";
  document.title = pill === "FIRE_ALARM" ? `[FIRE ALARM] ${BASE_TITLE}`
    : pill === "WATCH" ? `[WATCH] ${BASE_TITLE}` : BASE_TITLE;
  sound.setAlarm(pill === "FIRE_ALARM");
}

function renderStatus(s) {
  const net = s.network;
  let note = s.pill_note;
  if (s.pill === "FIRE_ALARM") note = s.blocks.filter(b => b.state === "ALARM").map(b => `Block ${b.block}`).join(", ");
  if (s.pill === "NO_DATA") note = net.latest_data_at ? `nothing heard for ${fmtAgo(net.latest_data_at).replace(" ago", "")}` : "no node has reported yet";
  if (s.pill === "WARMING_UP") note = "learning baselines - not yet detecting fire";
  setPill(s.pill, note);

  const tw = document.getElementById("timing-warn");
  tw.hidden = !s.timing_warning;
  tw.textContent = s.timing_warning ? `Setup problem: ${s.timing_warning}` : "";

  document.getElementById("st-total").textContent = net.total_nodes;
  document.getElementById("st-alarm").textContent = net.in_alarm;
  document.getElementById("st-alarm-box").classList.toggle("hot", net.in_alarm > 0);
  document.getElementById("st-gw").textContent = `${net.gateways_online}/${net.gateways_total}`;

  document.getElementById("blocks").innerHTML = s.blocks.map(b =>
    `<div class="block-line ${esc(b.state)}">${esc(b.line)}</div>`).join("");

  // Alarm, watch and missing nodes first, then by label.
  const ordered = [...s.nodes].sort((a, b) =>
    rankOf(nodeMapState(a)) - rankOf(nodeMapState(b)) || a.label.localeCompare(b.label));
  const rows = ordered.map(n => {
    const r = n.latest || {};
    return `<tr class="${esc(n.state)}" data-eui="${esc(n.dev_eui)}">
      <td><b>${esc(n.label)}</b>${n.rapid_updates ? `<span class="rapid" title="${esc(RAPID_TIP)}">RAPID UPDATES</span>` : ""}</td>
      <td>${badge(n.state)} ${n.missing ? badge("MISSING") : ""} ${clearAlarmButton(n, CC.clear_alarm_enabled)}</td>
      <td title="${esc(n.last_seen && fmtTime(n.last_seen))}">${esc(fmtAgo(n.last_seen))}</td>
      <td class="num">${fmt(r.temperature_c, 1)}</td>
      <td class="num">${fmt(r.humidity_pct, 0)}</td>
      <td class="num">${gasVs(r)}</td></tr>`;
  }).concat(s.never_seen.map(n =>
    `<tr><td><b>${esc(n.label)}</b></td><td>${badge("NEVER")}</td><td>never reported</td><td></td><td></td><td></td></tr>`));
  document.getElementById("nodes").innerHTML = rows.join("") ||
    `<tr><td colspan="6" class="muted">No nodes yet. Start the detection service and the simulator.</td></tr>`;

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
    const on = CC.features[p.dataset.feature];
    p.classList.toggle("enabled", on);
    p.querySelector(".phase2-body").textContent = on
      ? "ENABLED - NO DATA SOURCE CONFIGURED" : "NOT CONNECTED - PLANNED PHASE 2";
  });
}

// --- event log ---------------------------------------------------------------

let lastLogId = null;

function logTime(iso) {
  const d = new Date(iso), today = new Date().toDateString() === d.toDateString();
  const t = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  return today ? t : `${d.toLocaleDateString(undefined, { day: "2-digit", month: "short" })} ${t}`;
}

async function refreshLog() {
  const q = lastLogId === null ? "limit=200" : `after_id=${lastLogId}&limit=500`;
  const { entries } = await getJSON(`/api/console/log?${q}`);
  const ol = document.getElementById("log");
  if (lastLogId === null && !entries.length) {
    ol.innerHTML = '<li><span class="empty">No events yet.</span></li>';
  }
  if (!entries.length) { if (lastLogId === null) lastLogId = 0; return; }
  if (lastLogId === null || lastLogId === 0) ol.innerHTML = "";
  const atBottom = ol.scrollHeight - ol.scrollTop - ol.clientHeight < 40;
  for (const e of entries) {
    const li = document.createElement("li");
    li.className = `${e.level} ${e.category}`;
    li.innerHTML = `<time title="${esc(fmtTime(e.time))}">${esc(logTime(e.time))}</time><span>${esc(e.message)}</span>`;
    ol.appendChild(li);
    lastLogId = e.id;
  }
  while (ol.children.length > 500) ol.removeChild(ol.firstChild);
  if (atBottom || entries.length === ol.children.length) ol.scrollTop = ol.scrollHeight;
}

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

// --- polling ----------------------------------------------------------------

function showConn(err) {
  const el = document.getElementById("conn");
  if (!err) { el.hidden = true; return; }
  el.hidden = false;
  el.textContent = `Cannot load data: ${err.message} (retrying)`;
  setPill("NO_DATA", "console cannot reach the database");
}

document.getElementById("nodes").addEventListener("click", ev => {
  if (ev.target.closest('.clear-alarm')) return;
  const tr = ev.target.closest("tr[data-eui]");
  if (tr) location.href = `/node/${encodeURIComponent(tr.dataset.eui)}`;
});

window.addEventListener('alarm-cleared', async () => {
  try {
    renderStatus(await getJSON('/api/console/status'));
    await refreshLog();
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
      await refreshLog();
      showConn(null);
    } catch (err) {
      showConn(err);
    }
    setTimeout(tick, CC.poll_seconds * 1000);
  };
  tick();
}

start();
