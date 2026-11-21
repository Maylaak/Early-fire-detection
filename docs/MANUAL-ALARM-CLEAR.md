# Manual fire-alarm clearing

The dashboard can now clear an alarm deliberately. Clearing is an operator decision, not evidence that a fire has physically ended. It preserves history, offline status, local sensing and daily heartbeat scheduling. This feature supersedes older audit statements that no manual clear exists.

## Enable the local demonstration

The feature is disabled unless `WILDFIRE_ADMIN_TOKEN` is set in **both the detection service and web process**, with the same value. Nothing is hardcoded, written to YAML, published in public configuration, or stored in browser localStorage. Use a randomly generated, private token, 32–256 characters using letters, digits, underscores or hyphens.

In an activated CMD, generate a token once:

```bat
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Keep that value private. In each CMD that will launch service/web, set:

```bat
set WILDFIRE_ADMIN_TOKEN=YOUR_GENERATED_VALUE
set WILDFIRE_COMMAND_PORT=8766
set DEMO_SPEEDUP=60
```

`YOUR_GENERATED_VALUE` is a placeholder, not an acceptable example secret. Port 8766 is the default; change it consistently in both processes if occupied. The simulator does not need the token. These environment variables are read on process startup. The implementation task did **not** restart running services. When you choose to activate the change, stop existing service/web processes before launching their replacements; do not start a second detection service. A launcher started from the configured CMD inherits its environment.

Open the dashboard on `http://127.0.0.1:8000/` and hard-refresh. Active alarms with persisted event IDs have a **Clear Alarm** button in the node table and map popup. Click, read the confirmation, enter the token in the password field, then Confirm. Cancel makes no request. The exact confirmation is:

> Are you sure you want to clear this fire alarm? Only clear it after the situation has been checked. This action will not delete the alarm history or disable future fire detection.

The browser removes no alarm optimistically. Only after a committed clear is confirmed does it fetch current status and history without reloading the page. If another real alert has arrived, the current status may already show ALARM again. Failure leaves the alarm visible and supplies an error. The token field is emptied after each attempt and on dismissal. The browser never saves it in application storage; browser extensions/password managers remain outside application control.

## Security and command path

1. The browser sends `POST /api/console/nodes/{dev_eui}/clear-alarm` with an `Authorization: Bearer ...` header and JSON `{"alarm_event_id": <current event ID>}`.
2. The web app validates its configured token with constant-time comparison, node ID and event ID. Cross-origin/cross-site requests are rejected. Plain HTTP is accepted only for a loopback client; remote operators must use HTTPS with correctly configured trusted proxy handling. No cookie-based authentication or CORS exception is added.
3. The web forwards the bounded command to a fixed loopback URL, `127.0.0.1:WILDFIRE_COMMAND_PORT`. It does not use environment HTTP proxies, redirects or automatic retries. The detection service's stdlib HTTP listener verifies the token again and places the request in a bounded queue. This port must not be exposed through a proxy or Docker port mapping.
4. The service main loop submits a nondroppable `ClearAlarmJob` to the existing Writer. Other nodes continue normal processing. Uplinks for the selected node are serialized behind the clear until its outcome is resolved, avoiding stale memory overwrites or lost new alerts.
5. The Writer locks/checks the node and latest ALARM event ID, then atomically stores NORMAL, clears the explicit latch, inserts a `MANUAL_CLEAR` history event and an operator-console audit entry. Contact/online fields are untouched. The main loop updates its detector only after commit and then processes deferred uplinks.

The web DB connection remains read-only. No new dependencies, DB columns or tables are required: the existing events table supports the new event kind. The user explicitly authorized this small command-proxy exception to the prior GET-only HTTP interface; it does not grant database write permissions to the web account.

Token holders share an administrative role. The audit records `operator: admin_token`, the affected alarm event ID, node, action and time; it does not falsely identify an individual person. Treat the local machine and its administrators as trusted. Keep tokens out of source control, command screenshots, logs and URLs. For a multi-user deployment, individual identities and role management would be separate work.

## Ordering, idempotency and restart

- A confirmation names the current stored ALARM event ID. A newer alert causes a stale clear to return 409; refresh and make a new decision.
- Repeating a clear for the same already-cleared alarm returns success with `cleared: false`, without duplicating audit events. Concurrent requests can return “clear already in progress.”
- Requests expire after six seconds before a new clear may be initiated. If service/database processing is unavailable, the UI receives no success. An HTTP timeout can mean the outcome is unknown, not necessarily that a commit was rolled back: refresh before retrying. Safe retries use the same alarm event ID.
- On a transient database failure the Writer retries outcome resolution. After expiry it may confirm a previously committed clear, but cannot initiate an expired clear. A lost commit acknowledgment is reconciled before releasing queued node traffic.
- Restart restoration reads persisted node state and manual-clear history. Old replayed readings cannot resurrect a cleared legacy alarm. Persisted uplink keys continue suppressing exact duplicates. Delayed explicit alerts timestamped at/before the last manual clear are recorded as receipts without re-latching.
- The original FIRE_ALERT event and every reading remain intact. `GET /api/events?kind=alarms` includes `MANUAL_CLEAR`, and the dashboard event log shows the action. Manual clear itself sends no external “all clear” notification that could imply a measured end to a fire.

## Subsequent fire behavior

An actual new FIRE_ALERT after the clear can latch and notify again. A heartbeat cannot clear an alarm or manufacture a new one. The simulated node is not commanded to reset: while its existing local ALARM episode remains active, it sends no invented second FIRE_ALERT merely because an operator cleared the gateway. Its usual return to NORMAL rearms the next local episode. Legacy measurement detection also continues with its existing thresholds/confirmation rules.

An offline node remains offline/missing after clearing; NORMAL indicates no active recorded alarm, not a newly verified live sensor. Never clear an unchecked physical situation merely to reset a demonstration.

## Verification

Use isolated fixtures with TEST_DATABASE_URL, TEST_MQTT_HOST and TEST_MQTT_PORT unset. New tests cover active/offline clearing, other-node isolation, immutable history, audit entries, restart, delayed/repeated alerts, legitimate new alerts and notifications, stale confirmations, writer failure/expiry/lost commit acknowledgment, real SQLite read-only web access, invalid/unauthorized requests and temporary loopback HTTP end-to-end transport. No production broker or notification service is used.

```text
python -m pytest tests/test_manual_clear.py tests/test_control_messages.py tests/test_event_simulator.py tests/test_web.py tests/test_web_console.py -q
node --check gateway/web/static/alarm-clear.js
node --check gateway/web/static/console.js
node tests/test_alarm_clear.cjs
node tests/test_network_map.cjs
```

Frontend tests cover active-only buttons, escaped labels, Cancel, disabled controls while waiting, token cleanup, confirmed success, failure and timeout without optimistic alarm removal. Real browser visual acceptance and activation of the feature in the running demo require the user's planned service/web restart; no automatic restart was performed.

### Verified results for this change

With external test-service environment variables empty, `python -B -m pytest -q -p no:cacheprovider --tb=short` completed with **319 passed, 60 skipped, 0 failed**. Skips cover optional external PostgreSQL/MQTT fixtures. There was one existing Starlette/httpx TestClient deprecation warning. The two JavaScript syntax checks and both Node test commands above passed. The new backend test file alone passed 9 cases, with 8 PostgreSQL variants skipped. Tests used disposable SQLite databases, mocked notification transports and temporary loopback command listeners, not the running detection service.

### Manual activation and acceptance

When ready, use Ctrl+C in the existing **service and web** CMD windows. In each window, from the project root, activate `.venv` with `call .venv\Scripts\activate.bat` and set the environment variables above. Run `python -m gateway.service` in the service window and `python -m gateway.web` in the web window. Leave the simulator and Docker alone; do not create duplicate service instances.

1. Hard-refresh the dashboard. For an active alarm, open its popup or find its table row; Clear Alarm should appear. Healthy nodes should have no button.
2. Click Clear Alarm and Cancel: the alarm must remain.
3. Try an incorrect token: an authorization error must appear and the alarm must remain.
4. Enter the configured token and Confirm: after backend confirmation the selected alarm circle/indicator must disappear; other alarms must remain. A missing node must remain missing. Check the event log/history for the manual clear and the original fire event.
5. Restart the service/web yourself and verify the clear persists. A subsequent genuine fire episode must alarm again. Clearing during the same sustained local ALARM episode does not force the simulator to send another alert.

### Changed-file inventory

- Command transport/security: new `gateway/commands.py` and `gateway/web/commands.py`.
- Detection, serialization and persistence: `gateway/detect.py`, `gateway/service.py`, `gateway/store.py`, `gateway/writer.py`.
- API wiring/status/history: `gateway/web/app.py`, `gateway/web/console.py`.
- Dashboard: new `gateway/web/static/alarm-clear.js`; updated `gateway/web/static/console.js`, `gateway/web/static/console.css`, `gateway/web/static/index.html`.
- Verification: new `tests/test_manual_clear.py`, `tests/test_alarm_clear.cjs`; updated `tests/test_web.py`, `tests/test_network_map.cjs`.
- Documentation: new `docs/MANUAL-ALARM-CLEAR.md`.

No schema, dependencies, YAML configuration, simulator schedule or notification transport was changed. No Git commands or running-service restarts were performed.
