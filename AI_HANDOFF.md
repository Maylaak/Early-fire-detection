# AI handoff: planning future Codex tasks

Snapshot: 2026-11-15, inspected local source. Read this first, use `PROJECT_CONTEXT.md` for detail, and direct Codex to inspect current source before editing. This is architecture context, not proof that the live system or test suite currently passes.

## Project in one minute

LAU VIP LoRaWAN Wildfire Detection Gateway is a Python prototype for BME688 smoke detection and a Bkassine operator console. Simulator-generated ChirpStack v4 envelopes travel through Mosquitto to a standalone detection service. That service learns per-node baselines, emits WATCH/ALARM/connectivity events, sends notifications and asynchronously persists history. A separate FastAPI process serves a read-only monitoring UI.

Python >=3.11; FastAPI/Uvicorn; Pydantic 2/YAML; paho-mqtt; SQLAlchemy Core with PostgreSQL 16 or SQLite; httpx notifications; pytest. Frontend is plain HTML/CSS/JavaScript with bundled Leaflet/marker clustering and custom SVG charts. There is no React build, component framework, browser login or web write API. Docker Compose runs PostgreSQL and Mosquitto, while Python processes run separately.

## Architectural rules every implementation prompt should preserve

1. Detection cannot depend on a browser or web server. Keep algorithm code pure and I/O coordination outside it.
2. Web database access stays read-only. Only detection initializes schema. A schema/API change requires compatibility analysis across service, web and tests.
3. Keep UTC-aware timestamps and the deduplication identity: devEUI + frame counter + uplink timestamp. Counter reset is a legitimate new packet when time changes.
4. One detection service uses the persistent MQTT client ID per broker. Duplicate services cause repeated session takeover. Randomizing IDs is not a valid duplicate-process fix.
5. DEMO_SPEEDUP must match in service, simulator, web and inject commands. Configuration derives the missing threshold. Scale durations, not thresholds/sample counts.
6. Existing writer and notification queues keep blocking work off detection. Preserve ordering and the delayed event-ID/notified relationship.
7. UI must not hide alarms because of zoom, missing readings or clustering. Registry labels are for people; devEUI is the API/storage identity.
8. An alarm zone depicts sensor coverage, not predicted fire extent. RAPID UPDATES describes the node transmission mode from fPort, not a confirmed gateway alarm.

## Where Codex should look

| Task type | First implementation and tests to inspect |
|---|---|
| Detection thresholds/state changes | `gateway/detect.py`, `gateway/config.py`, `config.yaml`, `tests/test_detection.py` |
| Payload/network integration | `gateway/codec.py`, `gateway/chirpstack.py`, `gateway/ingest.py`, `gateway/mqtt.py`, `tests/test_codec.py`, `tests/test_ingest.py` |
| Startup/replay/outage behavior | `gateway/service.py`, `gateway/store.py`, `gateway/writer.py`, `tests/test_service.py` |
| Schema/persistence | `gateway/db.py`, `gateway/store.py`, `db/init/01-readonly-role.sql`, `tests/conftest.py`, `tests/test_ingest.py` |
| Notification channel | `gateway/notify/__init__.py`, `gateway/notify/telegram.py`, `gateway/notify/sms.py`, `tests/test_notify.py` |
| Simulator/firmware model | `gateway/sim/model.py`, `gateway/sim/firmware.py`, `gateway/sim/__main__.py`, `tests/test_simulator.py` |
| Operator console | `gateway/web/console.py`, `gateway/web/static/console.js`, `gateway/web/static/console.css`, `gateway/web/static/index.html`, `tests/test_web_console.py` |
| Chart/history/general API | `gateway/web/app.py`, `gateway/web/static/node.html`, `gateway/web/static/alarms.html`, `gateway/web/static/charts.js`, `gateway/web/static/common.js`, `tests/test_web.py` |
| Block/operator logs | `gateway/console_log.py`, `tests/test_console_log.py` |
| Startup scripts/infrastructure | `start_all.bat`, `docker-compose.yml`, `mosquitto/mosquitto.conf`, `README.md` |

Use `docs/CODEBASE-GUIDE.md` for navigation and `docs/DESIGN-RATIONALE.md` for documented tradeoffs. The rationale explicitly says simulations test the team's own fire model; do not convert those measurements into field-performance claims.

## Database and API conventions

SQLAlchemy Core tables, not declarative ORM models: nodes, readings, events, gateways and console_log. Readings store measurements, baseline snapshots, fPort and state, with a unique uplink key. Events carry transitions/connectivity and notification status. Store methods use transactions; immutable NodeState snapshots protect queued work from subsequent detector mutation.

`init_schema` creates tables and adds nullable columns, but has no general versioned migration system. Required-column/type/constraint changes need a deliberate plan. PostgreSQL read-only enforcement comes from grants on the web role; SQLite uses read-only file opening. PostgreSQL init scripts only apply when the database volume is initialized.

Read-only GET APIs: `/api/config`, `/api/status`, `/api/nodes/{dev_eui}/readings`, `/api/events`, `/api/console/config`, `/api/console/status`, `/api/console/log`. `/api/docs` exposes interactive docs. `/healthz` tests DB readability, not whether the detector or broker is alive. Pages are `/`, `/node/{dev_eui}` and `/alarms`.

Query validation uses FastAPI Query/Literal; invalid inputs produce validation errors, unknown chart nodes 404 and database failures 503. History is chronological; events newest first; console logs oldest first and support after_id pagination. Chart thinning preserves alarm/watch samples and can exceed its approximate 1500-point target. Avoid assuming identical response shapes between general and console status APIs.

## Shared UI conventions

Reuse `common.js`: getJSON, esc, labelOf, formatting, banners and polling. Use `charts.js` lineChart rather than adding a chart dependency. The console has separate polling/rendering logic and CSS. Keep escaped dynamic HTML and existing CSS state classes. Sound preference persists in localStorage; browser interaction is needed to enable audio playback.

At low zoom only alarms, gateways and centre appear; at detail zoom ordinary nodes and clustering appear. Network counts are database observations, not visible-marker counts. Missing alarming nodes retain their alarm state. Never-seen registry nodes are distinguished from observed nodes. Map-block aggregates still exist in the API although the current UI no longer draws them. Leaflet runs from bundled assets, while tile imagery needs network access. Static caching uses revalidation and page-level versioned asset URLs.

## Integration dependencies and pitfalls

- Database is required at detection startup for schema/replay. Later outages permit detection/alerts but delay persistence and blind the console.
- Writer state is in memory. MQTT acknowledgement happens before durable storage. A crash can lose accepted or queued readings; this is not exactly-once durability.
- Queue pressure drops oldest reading jobs, preserving events/logs. Nondroppable jobs can grow beyond the nominal queue cap; do not describe it as a strict memory bound.
- The batch launcher waits for infrastructure and spaces process launches five seconds apart, but does not enforce singleton services or confirm each Python process is healthy. Repeated launches can create duplicates. Startup delays do not solve client-ID collisions.
- External notifications require configured environment secrets. Do not copy secret values, credential-bearing URLs or private local settings into prompts.
- Anonymous MQTT and absent UI login are development defaults, not a production security design.
- Real ChirpStack integration requires compatible JSON envelopes and agreed firmware payload/ports. Embedded firmware is not supplied.

## Implementation status and open questions

Implemented in code: simulator, relative multichannel detection, warm-up/hysteresis, cooldown/offline monitoring, replay, asynchronous writes, console/Telegram adapters, site/block displays, event history, charting, map clustering and sound controls.

Explicitly incomplete: SMS fails at startup when enabled; thermal camera/drone/wind features have display placeholders only; site coordinates need a survey; node firmware is modeled, not implemented. Physical sensor burn-in, coverage, environmental false positives, radio range and battery lifetime are not established by software simulation.

Unknown: current deployment state, actual installed dependency versions, whether all tests pass now, live Telegram setup, GitHub branch/review policies and future hardware decisions. No TODO/FIXME markers were found in searched application/tests/docs; prose describes the limitations instead.

## Development, collaboration and prompt construction

The actual project root is this directory, nested inside another similarly named folder. This copy has no Git metadata: branch, remotes, tracking branches and initial cleanliness cannot be reported. Do not ask Codex to fetch, initialize, change remotes or assume a default branch. Team work should use the actual clone when available.

Read `AGENTS.md` and `CLAUDE.md`. Preserve teammates' work. No commits, merges or history rewrites without explicit instruction; existing guidance additionally prohibits pushing, remote edits and gh CLI use.

Run commands in the project root with the virtual environment activated. Development entry points: python -m gateway.config, python -m gateway.service, python -m gateway.sim run, python -m gateway.web, python -m gateway.status. Infrastructure starts via docker compose up -d. Tests use python -m pytest, preferably scoped to changed behavior before broader checks.

TEST_DATABASE_URL enables destructive PostgreSQL fixtures; only target disposable databases. TEST_MQTT_HOST/TEST_MQTT_PORT must refer to a scratch broker. Avoid launching a second service or sending real notifications just to validate unrelated changes.

A useful Codex prompt specifies: the observable behavior to change, relevant files to inspect, compatibility invariants, scope/non-goals, deterministic acceptance examples, appropriate tests, and the required report of changed files/results/limitations. Ask for inspection before implementation and resolution of uncertainty through code evidence. Do not pre-prescribe a new framework, table, endpoint or dependency when an existing facility may already solve the problem. Request field/hardware confirmation separately from code completion.
