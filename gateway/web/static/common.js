// Shared helpers: fetching, formatting, the alarm banner, and polling.
"use strict";

async function getJSON(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return r.json();
}

let _config = null;
let _labels = {};  // dev_eui -> site label ("F-07-03"), from the site registry
async function getConfig() {
  if (!_config) {
    const [cfg, console] = await Promise.all([getJSON("/api/config"), getJSON("/api/console/config")]);
    for (const n of console.sites.nodes) _labels[n.dev_eui] = n.label;
    _config = cfg;
  }
  return _config;
}

// What operators call a node: its site label, else the ChirpStack device name.
function labelOf(devEui, fallback) {
  return _labels[devEui] || fallback;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtAgo(iso, now = Date.now()) {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((now - new Date(iso)) / 1000));
  if (s < 90) return `${s}s ago`;
  if (s < 90 * 60) return `${Math.round(s / 60)} min ago`;
  if (s < 48 * 3600) return `${(s / 3600).toFixed(1)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

function fmtTime(iso) {
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function fmt(v, digits = 1) {
  return v === null || v === undefined || Number.isNaN(v) ? "-" : Number(v).toFixed(digits);
}

function badge(state) {
  return `<span class="badge ${esc(state)}">${esc(state)}</span>`;
}

// Banners shown on every page. A fire alarm must be impossible to miss.
function renderBanners(status) {
  const el = document.getElementById("banners");
  const s = status.summary;
  const names = pick => status.nodes.filter(pick).map(n => labelOf(n.dev_eui, n.name)).join(", ");
  const parts = [];
  if (s.alarm.length) parts.push(["alarm", `FIRE ALARM: ${names(n => n.state === "ALARM")}`]);
  if (s.watch.length) parts.push(["watch", `WATCH (possible smoke): ${names(n => n.state === "WATCH")}`]);
  if (s.offline.length) parts.push(["offline", `Offline: ${names(n => !n.online)}`]);
  if (s.stale) {
    const since = s.latest_data_at ? `since ${fmtAgo(s.latest_data_at)}` : "yet";
    parts.push(["stale", `No data from any node ${since}. Is the detection service, broker or gateway running?`]);
  }
  if (!parts.length) parts.push(["ok", `All ${status.nodes.length} nodes normal`]);
  el.innerHTML = parts.map(([cls, text]) => `<div class="banner ${cls}">${esc(text)}</div>`).join("");

  const base = document.title.replace(/^\[[A-Z ]+\] /, "");
  document.title = s.alarm.length ? `[FIRE ALARM] ${base}` : s.watch.length ? `[WATCH] ${base}` : base;
}

function showError(err) {
  document.getElementById("banners").innerHTML =
    `<div class="banner error">Cannot load data: ${esc(err.message)} (retrying)</div>`;
}

function setUpdated() {
  const el = document.getElementById("updated");
  if (el) el.textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

// Run `refresh` now and then every poll_seconds. Errors show in the banner and
// polling continues, so the page recovers on its own when the server is back.
async function startPolling(refresh) {
  let seconds = 5;
  try { seconds = (await getConfig()).poll_seconds; } catch (_) { /* use default */ }
  const tick = async () => {
    try {
      await refresh();
      setUpdated();
    } catch (err) {
      showError(err);
    }
    setTimeout(tick, seconds * 1000);
  };
  tick();
}
