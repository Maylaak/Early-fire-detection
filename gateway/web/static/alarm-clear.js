"use strict";

function clearAlarmButton(node, enabled) {
  if (!enabled || node.state !== 'ALARM' || !Number.isSafeInteger(node.alarm_event_id)) return '';
  return `<button type="button" class="clear-alarm" data-node="${esc(node.dev_eui)}" data-alarm="${node.alarm_event_id}" data-label="${esc(node.label)}">Clear alarm</button>`;
}

// Token exists only in the password input/request, never localStorage or a URL.
(() => {
  const dialog = document.getElementById('clear-dialog');
  const form = document.getElementById('clear-form');
  const token = document.getElementById('clear-token');
  const error = document.getElementById('clear-error');
  const confirm = document.getElementById('clear-confirm');
  const cancel = document.getElementById('clear-cancel');
  const notice = document.getElementById('clear-notice');
  let selected = null, busy = false;

  document.addEventListener('click', ev => {
    const button = ev.target.closest('.clear-alarm');
    if (!button || busy) return;
    ev.preventDefault();
    selected = { node: button.dataset.node, alarm: Number(button.dataset.alarm) };
    document.getElementById('clear-node').textContent = button.dataset.label;
    token.value = ''; error.textContent = ''; notice.textContent = '';
    dialog.showModal();
    token.focus();
  });
  cancel.addEventListener('click', () => { if (!busy) dialog.close(); });
  dialog.addEventListener('cancel', ev => { if (busy) ev.preventDefault(); });
  dialog.addEventListener('close', () => { token.value = ''; selected = null; });
  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    if (busy || !selected) return;
    busy = true; confirm.disabled = cancel.disabled = true;
    error.textContent = 'Waiting for the detection service to confirm the clear…';
    try {
      const response = await fetch(`/api/console/nodes/${encodeURIComponent(selected.node)}/clear-alarm`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token.value}` },
        body: JSON.stringify({ alarm_event_id: selected.alarm }), signal: AbortSignal.timeout(15000)
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Clear rejected. Refresh status and try again.');
      dialog.close();
      notice.textContent = 'Alarm clear confirmed. History is preserved; refreshing current status.';
      window.dispatchEvent(new Event('alarm-cleared'));
    } catch (err) {
      error.textContent = err.name === 'TimeoutError'
        ? 'Clear not confirmed. Refresh status before retrying.' : err.message;
    } finally {
      token.value = ''; busy = false; confirm.disabled = cancel.disabled = false;
    }
  });
})();
