// Operator console: map, status pill, sensor network, event log, audible alarm.
// Reads /api/console/*. Operators see site labels; devEUIs appear only in a
// map popup and on the node detail page.
"use strict";

const STATE_COLOR = { NORMAL: "#3fbf6f", WATCH: "#f2b23a", ALARM: "#e5322b", WARMUP: "#6e7886",
                      MISSING: "#6e7886", NO_DATA: "#6e7886", NEVER: "#6e7886" };
const MISSING_COLOR = "#6e7886";
// Worst first. Blocks and clusters take the worst state of the nodes inside,
// so an alarm reads red at every zoom level. (Same order as the server.)
const STATE_RANK = ["ALARM", "WATCH", "MISSING", "WARMUP", "NORMAL", "NO_DATA", "NEVER"];
const rankOf = st => { const i = STATE_RANK.indexOf(st); return i < 0 ? STATE_RANK.length : i; };
const worstOf = states => states.reduce((a, b) => (rankOf(b) < rankOf(a) ? b : a), "NEVER");
const PILL_TEXT = { FIRE_ALARM: "FIRE ALARM", WATCH: "WATCH", ALL_CLEAR: "ALL CLEAR", NO_DATA: "NO DATA",
                    WARMING_UP: "WARMING UP" };
const BASE_TITLE = document.title;
// Shown only while a node is in alarm-mode transmission (never on its normal heartbeat).
const RAPID_TIP = "This node's own sensor noticed a change, so it is now reporting about every " +
  "minute instead of on its normal schedule. This is not a fire alarm by itself: the console " +
  "raises WATCH or FIRE ALARM only when the readings confirm it.";

let CC = null;          // /api/console/config
let map = null, layers = null;

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
  map = L.map("map", { zoomControl: true }).setView([m.centre_lat, m.centre_lon], m.zoom);

  // Satellite first; if it never delivers a tile, fall back to the street map;
  // if that fails too we are offline: markers and zones still draw.
  const layerFor = t => L.tileLayer(t.url, { maxZoom: t.max_zoom, attribution: t.attribution });
  const primary = layerFor(m.primary_tiles), fallback = layerFor(m.fallback_tiles);
  watchTiles(primary, () => {
    map.removeLayer(primary);
    fallback.addTo(map);
    setMapNote("Satellite imagery unavailable: street map shown.");
    watchTiles(fallback, () => setMapNote("No map imagery (no internet?). Markers are still placed correctly."));
  });
  primary.addTo(map);

  // Level of detail. Below node_detail_zoom (the default view) the map is
  // almost empty: gateways, the centre, and ONLY nodes in ALARM, each as its
  // own red marker (never grouped, so a fire is visible at every zoom).
  // From node_detail_zoom in, every node, nearby ones grouped into a badge.
  layers = {
    zones: L.layerGroup(),                // red alarm zones (zoomed in)
    gateways: L.layerGroup().addTo(map),
    alarms: L.layerGroup(),               // zoomed out: nodes in ALARM only
    nodes: L.markerClusterGroup({         // zoomed in
      maxClusterRadius: m.cluster_radius_px,
      showCoverageOnHover: false,
      spiderfyOnMaxZoom: true,
      iconCreateFunction: clusterIcon,
    }),
  };
  map.on("zoomend", applyDetail);
  applyDetail();
  const c = CC.sites.centre;
  L.marker([c.lat, c.lon], {
    icon: L.divIcon({ className: "", html: '<div class="centre-icon">CD</div>', iconSize: [22, 22], iconAnchor: [11, 11] }),
    keyboard: false,
    zIndexOffset: -2000,  // never drawn over a node marker
  }).bindTooltip(esc(c.label), { direction: "top", offset: [0, -10] }).addTo(map);

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
      <div><i class="sq" style="background:var(--centre);transform:none"></i>Civil defence centre</div>
      <div><i class="zone"></i>Alarm: sensor coverage</div>
      <div class="note">Zoomed out, only nodes in ALARM are shown.
        Zoom in to ${CC.map.node_detail_zoom} to see single nodes.
        Positions are PLACEHOLDERS until a site survey.</div>`;
    return el;
  };
  legend.addTo(map);
  setMapNote("");
  // The map sits in a flex layout: re-measure whenever its box changes size.
  new ResizeObserver(() => map.invalidateSize()).observe(document.getElementById("map"));
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
    ${n.rapid_updates ? `<span class="rapid" title="${esc(RAPID_TIP)}">RAPID UPDATES</span> reporting about every minute<br>` : ""}
    Block ${esc(n.block || "-")} · last seen ${esc(fmtAgo(n.last_seen))}<br>
    T ${fmt(r.temperature_c, 1)} &deg;C · RH ${fmt(r.humidity_pct, 0)} % · gas ${gasVs(r)}<br>
    <span class="muted small">devEUI ${esc(n.dev_eui)}</span><br>
    <a href="/node/${encodeURIComponent(n.dev_eui)}">Open node detail</a>`;
}

const detailMode = () => map.getZoom() >= CC.map.node_detail_zoom;

function applyDetail() {
  if (detailMode()) {
    map.removeLayer(layers.alarms);
    map.addLayer(layers.zones);
    if (!map.hasLayer(layers.nodes)) { map.addLayer(layers.nodes); layers.nodes.refreshClusters(); }
  } else {
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
  return L.divIcon({ className: "anchor-icon", iconSize: [0, 0],
    html: `<div class="node-pin ${esc(state)}${hollow ? " hollow" : ""}"><i></i><span>${esc(label)}</span></div>` });
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
    ? "Showing single nodes (nearby nodes grouped)"
    : `Zoom in to ${CC.map.node_detail_zoom} to see single nodes. Nodes in ALARM are always shown.`;
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
  lastUnplaced = s.unplaced;
  renderMapInfo();
  lastZones = s.alarm_zones;
  drawZones();

  layers.gateways.clearLayers();
  for (const g of s.gateways) {
    if (g.lat === null) continue;
    L.marker([g.lat, g.lon], {
      zIndexOffset: -1000,
      icon: L.divIcon({ className: "", iconSize: [14, 14], iconAnchor: [7, 7],
        html: `<div class="gw-icon" style="background:${g.online ? "var(--gateway)" : MISSING_COLOR}"></div>` }),
    }).bindTooltip(`${esc(g.label)} - ${g.online ? "online" : "OFFLINE"}<br>last heard ${esc(fmtAgo(g.last_seen))}`,
      { direction: "top", offset: [0, -8] }).addTo(layers.gateways);
  }
}

let lastZones = [];

function drawZones() {
  layers.zones.clearLayers();
  for (const z of lastZones) {
    L.polygon(z.polygon, { color: "#e5322b", weight: 2, fillColor: "#e5322b", fillOpacity: 0.28 })
      .addTo(layers.zones);
    // Label above the zone, so it never covers the node markers inside it.
    const top = z.polygon.reduce((a, p) => (p[0] > a[0] ? p : a));
    L.tooltip({ permanent: true, direction: "top", className: "zone-tip", offset: [0, -2] })
      .setLatLng(top).setContent(esc(z.label)).addTo(layers.zones);
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
  document.getElementById("pill-text").textContent = PILL_TEXT[pill] || pill;
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
      <td>${badge(n.state)} ${n.missing ? badge("MISSING") : ""}</td>
      <td title="${esc(n.last_seen && fmtTime(n.last_seen))}">${esc(fmtAgo(n.last_seen))}</td>
      <td class="num">${fmt(r.temperature_c, 1)}</td>
      <td class="num">${fmt(r.humidity_pct, 0)}</td>
      <td class="num">${gasVs(r)}</td></tr>`;
  }).concat(s.never_seen.map(n =>
    `<tr><td><b>${esc(n.label)}</b></td><td>${badge("NEVER")}</td><td>never reported</td><td></td><td></td><td></td></tr>`));
  document.getElementById("nodes").innerHTML = rows.join("") ||
    `<tr><td colspan="6" class="muted">No nodes yet. Start the detection service and the simulator.</td></tr>`;

  const k = CC.demo_speedup;
  document.getElementById("net-foot").textContent =
    `Heartbeat every ${minutes(CC.heartbeat_minutes)}; a node is MISSING after ${minutes(CC.missing_after_minutes)} of silence.` +
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
  const tr = ev.target.closest("tr[data-eui]");
  if (tr) location.href = `/node/${encodeURIComponent(tr.dataset.eui)}`;
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
