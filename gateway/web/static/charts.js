// Minimal SVG time-series chart. No library, so the dashboard works offline.
//
// lineChart(el, {
//   series: [{ label, values: [number|null...], color: "var(--x)", dashed }],
//   times:  [Date...]                    (shared x values)
//   bands:  [{ from: Date, to: Date, color }]
//   xMin, xMax: Date, unit: "C", digits: 1,
// })
"use strict";

function niceTicks(min, max, count = 5) {
  const span = max - min || Math.abs(max) || 1;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) ticks.push(v);
  return ticks;
}

function timeLabel(d, spanMs) {
  if (spanMs > 36 * 3600e3) {
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
      d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function lineChart(el, opts) {
  const { series, times, bands = [], unit = "", digits = 1 } = opts;
  const W = Math.max(el.clientWidth, 280), H = 200;
  const m = { l: 56, r: 12, t: 8, b: 24 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const x0 = +opts.xMin, x1 = +opts.xMax;

  const all = series.flatMap(s => s.values).filter(v => v !== null && Number.isFinite(v));
  if (!all.length) {
    el.innerHTML = `<div class="empty">No readings in this period</div>`;
    return;
  }
  let lo = Math.min(...all), hi = Math.max(...all);
  const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.02 || 1;
  lo -= pad; hi += pad;
  const ticks = niceTicks(lo, hi);
  lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);

  const X = t => m.l + ((+t - x0) / (x1 - x0)) * iw;
  const Y = v => m.t + (1 - (v - lo) / (hi - lo)) * ih;

  // Break lines across gaps (missed packets, node offline).
  const dts = [];
  for (let i = 1; i < times.length; i++) dts.push(times[i] - times[i - 1]);
  dts.sort((a, b) => a - b);
  const gap = Math.max((dts[Math.floor(dts.length / 2)] || 60e3) * 5, 5 * 60e3);

  let svg = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img">`;
  for (const b of bands) {
    const bx0 = Math.max(X(b.from), m.l), bx1 = Math.min(X(b.to), m.l + iw);
    if (bx1 > bx0) svg += `<rect x="${bx0}" y="${m.t}" width="${bx1 - bx0}" height="${ih}" style="fill:${b.color}"/>`;
  }
  const step = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
  const tickDigits = (String(+step.toPrecision(6)).split(".")[1] || "").length;
  for (const v of ticks) {
    const y = Y(v);
    svg += `<line class="grid" x1="${m.l}" x2="${m.l + iw}" y1="${y}" y2="${y}"/>`;
    svg += `<text class="axis" x="${m.l - 6}" y="${y + 4}" text-anchor="end">${v.toFixed(tickDigits)}</text>`;
  }
  for (const t of niceTimeTicks(x0, x1)) {
    svg += `<text class="axis" x="${X(t)}" y="${H - 6}" text-anchor="middle">${esc(timeLabel(new Date(t), x1 - x0))}</text>`;
  }
  for (const s of series) {
    let d = "", pen = false;
    s.values.forEach((v, i) => {
      if (v === null || !Number.isFinite(v)) { pen = false; return; }
      const brk = i > 0 && times[i] - times[i - 1] > gap;
      d += `${pen && !brk ? "L" : "M"}${X(times[i]).toFixed(1)},${Y(v).toFixed(1)}`;
      pen = true;
    });
    svg += `<path d="${d}" fill="none" style="stroke:${s.color}" stroke-width="${s.dashed ? 1.5 : 1.8}"` +
      `${s.dashed ? ' stroke-dasharray="5 4"' : ""} stroke-linejoin="round"/>`;
  }
  svg += `<line class="cursor" x1="0" x2="0" y1="${m.t}" y2="${m.t + ih}" style="stroke:var(--muted);display:none"/>`;
  svg += `</svg><div class="tip" style="display:none"></div>`;
  el.innerHTML = svg;

  // Hover: nearest reading, all series values.
  const svgEl = el.querySelector("svg"), cursor = el.querySelector(".cursor"), tip = el.querySelector(".tip");
  svgEl.addEventListener("mousemove", ev => {
    const r = svgEl.getBoundingClientRect();
    const px = (ev.clientX - r.left) * (W / r.width);
    const t = x0 + ((px - m.l) / iw) * (x1 - x0);
    let i = bisect(times, t);
    if (i > 0 && (i >= times.length || t - times[i - 1] < times[i] - t)) i--;
    if (i < 0 || i >= times.length) return;
    const cx = X(times[i]);
    cursor.setAttribute("x1", cx); cursor.setAttribute("x2", cx); cursor.style.display = "";
    tip.innerHTML = `<b>${esc(fmtTime(times[i]))}</b><br>` + series.map(s =>
      `${esc(s.label)}: ${fmt(s.values[i], digits)} ${esc(unit)}`).join("<br>");
    tip.style.display = "";
    const left = (cx / W) * r.width;
    tip.style.left = `${Math.min(left + 10, r.width - tip.offsetWidth - 4)}px`;
    tip.style.top = "8px";
  });
  svgEl.addEventListener("mouseleave", () => { cursor.style.display = "none"; tip.style.display = "none"; });
}

function niceTimeTicks(x0, x1) {
  const span = x1 - x0;
  const steps = [5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880].map(mn => mn * 60e3);
  const step = steps.find(s => span / s <= 6) || span / 6;
  const tzOffset = new Date(x0).getTimezoneOffset() * 60e3; // align ticks to local clock
  const out = [];
  for (let t = Math.ceil((x0 - tzOffset) / step) * step + tzOffset; t <= x1; t += step) out.push(t);
  return out;
}

function bisect(arr, t) {
  let lo = 0, hi = arr.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (+arr[mid] < t) lo = mid + 1; else hi = mid; }
  return lo;
}

// Contiguous WATCH / ALARM runs -> shaded bands.
function stateBands(times, states) {
  const colors = { WATCH: "var(--band-watch)", ALARM: "var(--band-alarm)" };
  const bands = [];
  let cur = null;
  states.forEach((s, i) => {
    if (cur && cur.state === s) { cur.to = times[i]; return; }
    if (cur) { cur.to = times[i]; bands.push(cur); cur = null; }
    if (colors[s]) cur = { state: s, from: times[i], to: times[i], color: colors[s] };
  });
  if (cur) bands.push(cur);
  return bands;
}
