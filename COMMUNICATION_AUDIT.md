# Communication and map audit

Audit of local source, 2026-11-15. Code and deterministic tests establish software behavior; they do not establish live deployment, radio coverage, physical firmware or visual browser quality. Earlier PROJECT_CONTEXT.md and AI_HANDOFF.md are historical snapshots, not authoritative descriptions of the current event protocol.

## A. Communication status and evidence

The shipped config selects `simulator.reporting_mode: event_driven`. Core behavior was already implemented. This audit corrected misleading reporting, added broader deterministic evidence, and updated tests still expecting the older four-node deployment.

| Requirement | Status | Evidence |
|---|---|---|
| A: daily heartbeat, no normal uplinks | VERIFIED in simulation | `gateway/sim/event_firmware.py`: `EventNodeFirmware._step`; `tests/test_event_simulator.py`: daily and nine-node tests |
| B: continued local detection | VERIFIED | `_step` samples `NodeModel`, then calls the shared `NodeDetector.process` before deciding whether to transmit |
| C: explicit fire event, persistence, notification, deduplication | VERIFIED with isolated database and mock transport | `_envelope`, `Ingestor.accept`, `Pipeline.on_uplink`, `NodeDetector.control`, `ControlJob.run`, `Store.save_control`; control and injected-fire tests |
| D: daily-compatible connectivity | VERIFIED in API/detector tests; dashboard text corrected | `NodeDetector.check_offline`, `console._node`, explicit missing configuration; new boundary/recovery test |
| E: speed-up | VERIFIED for communication and detection timings | `config.apply_speedup`, event firmware duty-cycle scaling; tests at 1 and 60 |

Flow: `NodeModel.sample` -> local `NodeDetector.process` -> `EventNodeFirmware` schedule -> CLI `_envelope` -> MQTT QoS 1 -> `parse_uplink` -> `Ingestor.accept` -> `Pipeline.on_uplink` -> `ControlJob`/`Store.save_control` and `Dispatcher` -> read-only console API -> existing polling UI. Actual broker delivery and external notification credentials were not exercised.

## B. Exact transmission behavior

- Fresh simulator state: immediate HEARTBEAT establishes contact; no readiness or environmental claim. Each half-open simulated day contains one scheduled heartbeat per node, including the initial day.
- Quiet operation: sample every 60 simulated seconds; no measurement payloads. Subsequent heartbeat deadlines are anchored to the first deadline, every 1440 simulated minutes.
- At speed-up 60: samples every 1 real second, heartbeat every 24 real minutes. Thirty warm-up samples stay thirty samples; confirmation counts are unchanged.
- Fire: first local transition into ALARM queues FIRE_ALERT. It sends on the first eligible sensing tick after the radio duty-cycle gap. It does not wait for the heartbeat. A coincident heartbeat waits behind the alert until eligible; it is not removed from the daily schedule.
- Sustained local ALARM: no fresh alert on every sample. Heartbeats continue. Full local NORMAL rearms a later episode. Baseline adaptation remains the original detector behavior, so this represents algorithm episodes, not physical incident IDs.
- Fire conditions stop: no FIRE_CLEAR is transmitted. The gateway's explicit fire latch remains ALARM, including across silence, heartbeats, legacy readings and restart. A future authenticated clear/acknowledgment design remains necessary; no new web write API was introduced.
- Restart: `data/simulator-state.sqlite` retains counter, heartbeat deadline, local detector checkpoint and pending outbox. Do not delete it to restart. Redelivery retains counter/time and is deduplicated. Sleep skips missed deadlines without a catch-up burst. Changing acceleration schedules a future interval instead of sending another boot heartbeat. Dry-run uses fresh in-memory state and does not touch the live outbox.
- Reliability: no application-level periodic alert repeat is generated. Existing MQTT QoS-1 transport can redeliver until acknowledgment; this is not a bounded physical radio retry model. Outbox acknowledgment is broker acceptance, not database commit or radio reception. A prolonged outage can accumulate pending messages. No exactly-once crash guarantee is claimed.

Wire format: version 2 is exactly two bytes: `02 01` HEARTBEAT, `02 02` FIRE_ALERT. Node identity, frame counter, timestamp, gateway and radio metadata are in the ChirpStack envelope. Version-1 11-byte environmental payloads retain their meaning. `control_uplinks` stores receipts; `events` stores alerts. Heartbeats do not create `readings` rows. Duplicate identity is devEUI + frame counter + UTC uplink time. Persisted keys are loaded by `service._replay`.

## C. Detection and connectivity

Event-mode simulated nodes reuse `NodeDetector` warm-up, gas/temperature/humidity baselines, thresholds, freezing, hysteresis and confirmation. The gateway trusts explicit FIRE_ALERT without requiring its own baseline. Legacy readings still enter gateway `DetectionEngine.process`.

The default tolerance is three heartbeat intervals: more than 72 simulated hours since valid contact, or more than 72 real minutes at speed-up 60. Equality is still online. A valid later heartbeat or fire alert restores contact; older messages do not regress its timestamp. The dashboard derives missing state from timestamps; detection checks on its existing timer, so its offline log can lag expiration by about 30 real seconds. Never-seen nodes remain separate from known-online nodes.

Weather/day-night and battery drift retain the model's wall-clock behavior; acceleration is not a universal virtual-world clock. Model noise/weather/smoke injection state is not fully checkpointed across process restart, unlike detector and transmission state. Physical node firmware and field detection performance remain unverified.

## D. Original requirements

VERIFIED below means inspected code plus relevant automated checks, not a visual browser acceptance test.

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Nine sensors | VERIFIED | `_apply_demo_layout`, layout tests |
| 2 | Nine distinct cells | VERIFIED | `geometry.grid_positions`, 3x3 indexed offsets |
| 3 | 100 m square | VERIFIED | `grid_boundary`, independent haversine tests |
| 4 | Cell-centered sensors | VERIFIED | `test_grid_cell_centres_and_extent` |
| 5 | Gateway 500 m east | VERIFIED | `destination`, shipped-layout offset test |
| 6 | Configurable drone position | VERIFIED | `demo_layout.drone`, offset tests |
| 7 | Configurable main control unit | VERIFIED | `demo_layout.control_unit`, reused centre identity |
| 8 | Planned assets labeled honestly | VERIFIED | `plannedMarker`, no operational telemetry popup, JS checks |
| 9 | All nine at close zoom | VERIFIED | `drawNodes`, clustering disabled at detail threshold, JS checks |
| 10 | Twelve neighbor connections | VERIFIED | `initNetwork`, distinct adjacency tests |
| 11 | Lightweight entrance animation | PARTIALLY IMPLEMENTED | finite CSS fade/stagger and reduced-motion rules exist; perceived polish unverified |
| 12 | Detail versus overview | VERIFIED | `applyDetail` layer switching tests |
| 13 | Cell-contained alarm circles | VERIFIED in configured geometry | `alarmRadiusMeters`: 15 m vs 16.67 m half-cell; geographic `L.circle`, no stroke/expanding glow; nine-alarm tests. Browser rendering remains unverified |
| 14 | Alarm visibility at all zooms | VERIFIED | individual overview alarms and summary; detail red nodes/circles; missing alarm test |
| 15 | Overview summary | VERIFIED | `networkIcon`, `updateNetwork` tests |
| 16 | Distinct gateway | VERIFIED in implementation | antenna symbol, keyed marker and truthful status |
| 17 | Visible planned assets | VERIFIED in implementation | separate persistent drone/control markers; no flyover telemetry |
| 18 | Click summary to grid | VERIFIED | click handler and reduced-motion navigation tests |
| 19 | No duplicates/stale layers/flicker | PARTIALLY IMPLEMENTED | repeated transitions and pruning pass; actual flicker/browser performance unverified |
| 20 | Connections labeled illustrative | VERIFIED | legend/map info explicitly reject measured-radio interpretation |

Sensor coverage remains the original 150 m geometry, separately selectable and muted in grid mode. Alarm circles indicate alarm location, not fire extent. No layout or sensor identities were changed in this audit.

## E. Changes

- `gateway/web/console.py`: additive reporting-mode field; preserves legacy API timing fields.
- `gateway/web/static/console.js`: event-mode footer uses explicit daily timings, labels recent message type, explains readiness uncertainty; displays NO FIRE REPORTED instead of implying safety from a heartbeat.
- `gateway/config.py`: effective event-mode summary reports daily heartbeat, local sampling and correct missing interval.
- `gateway/sim/__main__.py`: accurate mode documentation; control transmission logs no longer print internal environmental samples as though transmitted.
- `tests/test_event_simulator.py`: two-day nine-node tests, backend persistence/mock notification/dedup evidence, connectivity boundary/recovery/API tests, summary/log assertions.
- `tests/test_console_log.py`: correct obsolete four-node expectations to nine nodes and three sensors in F-08.
- This audit record. No schema, detector, scheduler, dependencies or live-service changes were needed.

## F. Verification commands and scope

Run `python -m pytest -q` with TEST_DATABASE_URL, TEST_MQTT_HOST and TEST_MQTT_PORT unset. Tests use temporary SQLite, controlled timestamps and mock notification transports. PostgreSQL/broker integration cases are intentionally skipped. `node --check gateway/web/static/console.js` and `node tests/test_network_map.cjs` verify syntax and map behavior with a Leaflet/DOM test double. Full suite: 308 passed, 52 skipped. After the final summary/log test was added, the event-simulator subset passed 11 tests with 2 skipped. JavaScript syntax and map tests passed. One existing Starlette/httpx deprecation warning remains. Browser screenshots/interactions, real broker delivery, external notification delivery and physical hardware were not tested.

Important cases: `test_nine_nodes_two_quiet_days`, `test_daily_schedule_and_local_sampling`, `test_fire_is_prompt_bounded_and_duty_cycle_limited`, `test_injected_fire_through_backend`, `test_restart_reschedule_and_duplicate_ticks`, `test_outbox_restart_ack_and_saved_detector`, `test_connectivity_boundary_recovery_and_dashboard`, `test_alert_heartbeat_restart_and_legacy`, `test_bad_control_payload`, legacy codec/ingestion/detection tests and geographic layout tests.

## G. Remaining work

Browser visual/performance acceptance; real MQTT and external transport acceptance; physical firmware, surveyed coordinates and radio/coverage validation; explicit authenticated alarm-clear semantics. Durability across gateway crashes between delivery, notification and database commit is not guaranteed by the existing in-memory writer. Persistent control-key growth and outbox backlog limits deserve a separate reliability task. No requirement for a new clear API or broader redesign was inferred here.

## H. Operator demonstration

Use the already-running single simulator/service/web setup with matching DEMO_SPEEDUP. Do not launch duplicate detection services. New Python code/config display changes become active only after a restart you choose to perform; none was performed by the audit.

In a separate CMD at the project root, activate `.venv`, set `DEMO_SPEEDUP=60`, and run `python -m gateway.config`. For a standalone harmless boot-envelope check run `python -m gateway.sim run --dry-run --count 9`; this prints nine two-byte control envelopes without MQTT or state-file writes.

Observe the existing simulator window: only HEARTBEAT/FIRE_ALERT transmissions should be logged, with frame counters; quiet local samples do not generate output. Existing state means startup may wait until the retained heartbeat deadline. Every 24 real minutes there should be one new heartbeat per node under normal operation. Deterministic tests prove this without waiting a day.

After at least 30 local samples (about 30 seconds at x60), use `python -m gateway.sim inject node-3`. Expect an alert after the configured smoke ramp and confirmation ticks, subject to duty cycle. Observe gateway alarm output, dashboard red node/summary and alarm history. Configured real notification transports may send on this manual injection. A previously latched node will not notify again merely because another episode occurs.

To demonstrate missing contact, intentionally stop only the simulator yourself with Ctrl+C, leave service/web running, and wait more than 72 real minutes after its last contact at x60. The dashboard should show MISSING; any alarm remains. Restarting the simulator preserves its schedule, so contact recovery occurs on the next due heartbeat or fire message, not necessarily immediately. Use the deterministic connectivity test for a fast, non-disruptive alternative. Do not erase database or simulator state to clear an alarm.
