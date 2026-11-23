// Run from project root: node tests/test_desktop_alerts.cjs
// Desktop (browser) notifications: permission flow, one notification per new
// alarm, missing nodes only after page load, nothing when off. No browser needed.
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');

const shown = [];
class FakeNotification {
  constructor(title, opts) { this.title = title; this.opts = opts; shown.push(this); }
  close() {}
  static requestPermission() { FakeNotification.permission = FakeNotification.answer; return Promise.resolve(FakeNotification.permission); }
}
FakeNotification.permission = 'default';
FakeNotification.answer = 'granted';

const handlers = {};
const btn = { textContent: '', title: '', disabled: false, attrs: {},
  classList: { toggle() {} }, setAttribute(k, v) { this.attrs[k] = v; },
  addEventListener(k, f) { handlers[k] = f; } };
const store = {};
const ctx = { assert, console, Promise,
  window: { Notification: FakeNotification, focus() {} }, Notification: FakeNotification,
  localStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; } },
  document: { getElementById: () => btn },
  fmtGPS: (a, b) => `${a}, ${b}`, daysText: d => `missing ${d} days`, showAlarmCluster() {} };
vm.createContext(ctx);
const src = fs.readFileSync('gateway/web/static/console.js', 'utf8');
const start = src.indexOf('// --- desktop (browser) notifications'), end = src.indexOf('// --- polling');
assert(start > 0 && end > start);
vm.runInContext(src.slice(start, end) + '\nthis.desktopAlerts = desktopAlerts;', ctx);

const node = (id, extra) => ({ dev_eui: id, label: 'F-07-0' + id, block: 'F-07', gateway_label: 'GW-01',
  lat: 33.5, lon: 35.5, state: 'NORMAL', missing: false, alarm_event_id: null, ...extra });
const status = nodes => ({ nodes, network_tree: [{ gateway_id: 'gw', clusters: [{ nodes: ['1', '2', '3'] }] }] });

(async () => {
  assert.equal(btn.textContent, 'Desktop alerts: OFF');
  // Off: an alarm on page load is remembered but not shown.
  ctx.desktopAlerts.update(status([node('1'), node('2', { missing: true, days_missing: 3 })]));
  assert.equal(shown.length, 0);

  // Turning it on asks the browser once, then confirms with one notification.
  await handlers.click({ stopPropagation() {} });
  assert.equal(FakeNotification.permission, 'granted');
  assert.equal(btn.textContent, 'Desktop alerts: ON');
  assert.equal(store['wildfire.console.desktop-alerts'], 'on');
  assert.equal(shown.length, 1); shown.length = 0;

  // A new alarm: one sticky notification naming cluster and gateway; not repeated on the next poll.
  const alarm = status([node('1', { state: 'ALARM', alarm_event_id: 7 }), node('2', { missing: true, days_missing: 3 })]);
  ctx.desktopAlerts.update(alarm); ctx.desktopAlerts.update(alarm);
  assert.equal(shown.length, 1);
  assert.equal(shown[0].title, 'FIRE ALARM - F-07-01');
  assert.match(shown[0].opts.body, /Cluster F-07 · GW-01/);
  assert.equal(shown[0].opts.requireInteraction, true);
  // F-07-02 was already missing when the page opened: no notification for it.
  assert(!shown.some(n => n.title.includes('F-07-02')));

  // A node that goes missing after load: one notification with its days.
  shown.length = 0;
  ctx.desktopAlerts.update(status([node('1', { state: 'ALARM', alarm_event_id: 7 }), node('2', { missing: true, days_missing: 3 }),
    node('3', { missing: true, days_missing: 3 })]));
  assert.equal(shown.length, 1);
  assert.equal(shown[0].title, 'Node missing - F-07-03');
  assert.match(shown[0].opts.body, /missing 3 days/);

  // A second alarm on the same node (new event id) notifies again.
  shown.length = 0;
  ctx.desktopAlerts.update(status([node('1', { state: 'ALARM', alarm_event_id: 9 })]));
  assert.equal(shown.length, 1);

  // Off again: nothing shown.
  await handlers.click({ stopPropagation() {} });
  assert.equal(btn.textContent, 'Desktop alerts: OFF');
  shown.length = 0;
  ctx.desktopAlerts.update(status([node('1', { state: 'ALARM', alarm_event_id: 11 })]));
  assert.equal(shown.length, 0);

  // Blocked by the browser: the button says so.
  FakeNotification.permission = 'denied';
  await handlers.click({ stopPropagation() {} });
  assert.equal(btn.textContent, 'Desktop alerts: BLOCKED');
  console.log('PASS: permission flow, one notification per new alarm, missing after load only, off and blocked states.');
})().catch(e => { console.error(e); process.exit(1); });
