# Project context

Inspected 2026-11-15. Scope: the project root containing this file, not its enclosing download folder. Facts below are verified from local source unless marked otherwise. Runtime operation, deployed configuration and test success were not established by this documentation review. Existing documentation supplies design intent; source is authoritative for behavior.

## Executive summary

LoRaWAN Wildfire Detection Gateway (LAU VIP), package `wildfire-gateway` version 0.1.0, is a prototype for detecting smoke from BME688 environmental readings and presenting an operator console for Bkassine Civil Defence. A simulator supplies ChirpStack-compatible uplinks without hardware. The independently running detection service learns per-node baselines, raises WATCH/ALARM and connectivity events, stores history and dispatches notifications. The web process only reads persisted data; closing a browser never stops detection.

Requirements evidenced by implementation: relative multichannel detection, baseline warm-up, hysteresis, offline monitoring, persistent history, operator-friendly site labels, visible alarms at any map zoom, and detection continuity during database outages after startup. This is a simulation-backed prototype, not evidence of field-validated fire detection.

## Technology stack

| Area | Verified implementation |
|---|---|
| Runtime | Python >=3.11, declared in `pyproject.toml` |
| HTTP | FastAPI, Uvicorn; factory `create_app` in `gateway/web/app.py` |
| Data | SQLAlchemy 2 Core tables and explicit transactions, not declarative ORM entities; psycopg driver; PostgreSQL 16; SQLite fallback |
| MQTT | paho-mqtt 2 callback API, Mosquitto 2 container, ChirpStack v4 JSON envelopes |
| Validation | Pydantic 2 frozen configuration models with extra keys forbidden; PyYAML |
| Frontend | Plain HTML, CSS and JavaScript; bundled Leaflet and marker clustering; custom SVG charts |
| State | Python detector instances; relational history; browser in-memory state and localStorage for sound preference |
| Notifications | Console and Telegram via httpx; SMS is a deliberately failing stub |
| Tests/build | pytest, FastAPI test clients and mocks; requirements.txt minimum versions; no frontend build pipeline or dependency lockfile found |
| Deployment | Docker Compose starts database and broker, not the Python application processes |

No React, Next.js, Redux, Tailwind, UI component framework, browser login, session system or role-management application was found. No CI/CD workflows were found in this local copy. These are observations about supplied files, not claims about GitHub.

## Directory and module map

| Path | Responsibility and important symbols |
|---|---|
| `gateway/config.py` | `Config`, `load_config`, `apply_speedup`; strict validation and derived timings |
| `config.yaml` | Thresholds, timing, simulator nodes, site registry, console settings and feature flags; do not copy connection secrets into documentation |
| `gateway/codec.py` | `SensorReading`, `encode`, `decode`; provisional 11-byte payload |
| `gateway/chirpstack.py` | `Uplink`, `parse_uplink`, `build_uplink`; envelope and radio-airtime helpers |
| `gateway/mqtt.py` | `connect`, `BrokerUnavailable`; shared connection/reconnect behavior |
| `gateway/ingest.py` | `Ingestor.accept`; decode and bounded in-memory deduplication |
| `gateway/detect.py` | Pure `NodeDetector`, `DetectionEngine`, `Event`, `Evaluation` |
| `gateway/service.py` | `Pipeline`, `run`, startup replay, timers, main processing loop |
| `gateway/db.py` | SQLAlchemy tables, `UTCDateTime`, `make_engine`, `init_schema` |
| `gateway/store.py` | `Store`, `NodeState`, `uplink_key`; transactional persistence |
| `gateway/writer.py` | `Writer`, `ReadingJob`, `EventsJob`, `NotifiedJob`, `LogJob`, `EventRef` |
| `gateway/console_log.py` | `BlockTracker`, node/block/system/notification log records |
| `gateway/notify/` | `Notifier`, `Dispatcher`, console/Telegram adapters and SMS stub |
| `gateway/sim/` | Model, simulated firmware behavior and run/inject CLI |
| `gateway/web/app.py` | Pages, static assets, general read-only APIs, health check |
| `gateway/web/console.py` | `add_console_routes`, operator status aggregation and map data |
| `gateway/web/static/` | HTML pages, shared helpers, console script, SVG charts and CSS |
| `gateway/status.py` | Read-only command-line status |
| `db/init/01-readonly-role.sql` | Database read-only role and future-table SELECT grants |
| `docker-compose.yml`, `mosquitto/mosquitto.conf` | Infrastructure, persistence, broker listener |
| `start_all.bat` | Windows launcher; virtual environment, demo speed, Docker readiness, sequential windows with five-second delays |
| `tests/` | Behavioral unit and integration tests |
| `docs/` | Codebase guide, design rationale, demo briefing and runbook |

## Architecture and data flow

```text
simulated sensors (or future nodes through ChirpStack)
  -> Mosquitto -> mqtt.connect -> service inbox
  -> Ingestor.accept -> DetectionEngine -> Pipeline
       -> Dispatcher -> notification channels
       -> Writer queue -> Store -> PostgreSQL / SQLite
                                      -> read-only FastAPI -> polling browser
```

`codec.py` owns binary encoding. `chirpstack.py` owns network-envelope interpretation. `detect.py` takes readings and timestamps without MQTT, database or wall-clock dependencies. `service.py` coordinates those boundaries. Paho delivers messages on its network thread; the main loop consumes an inbox sequentially. Writer and notification dispatcher have background threads so database/notification latency is not placed directly on the detection path.

Startup validates notifier setup, initializes schema and replays stored readings to reconstruct detector state before connecting for live work. Database startup failures retry. After startup, database failure queues writes while detection continues. The web console cannot show fresh data during that outage.

Deduplication identity is node devEUI + frame counter + uplink time (`uplink_key`), not ChirpStack deduplicationId alone. Ingestor tracks recent keys; database uniqueness provides persistence-level protection. Detector rejects timestamps at or before its previous reading time. Preserve these layers when changing replay or ingestion.

## Detection and implementation status

Implemented: EWMA baselines (gas in log space), warm-up, relative gas-drop plus temperature-rise OR humidity-drop evidence, ordered WARMUP/NORMAL/WATCH/ALARM state changes, consecutive-sample confirmation, baseline freezing, cooldown, offline/online events, replay, asynchronous persistence, block summaries, operator logs, charts and simulated fire injection.

The simulator includes weather/noise/drift and event-driven firmware behavior with duty-cycle limits. It is an executable model of expected firmware, not delivered embedded firmware. `DEMO_SPEEDUP` scales durations, not thresholds or sample counts. Missing timeout derives from heartbeat times missed-heartbeat count. All participating processes must use the same effective configuration.

Implemented but requiring external setup: Telegram credentials and connectivity; PostgreSQL read-only grants; real ChirpStack JSON forwarding. No live account or hardware integration was verified.

Incomplete by explicit evidence: SMS (`gateway/notify/sms.py` raises `NotifierConfigError`); thermal camera, drone and wind panels are feature-flagged placeholders without data sources; surveyed site coordinates and actual node firmware are absent. No literal TODO/FIXME markers were found in searched application, test and docs directories; incomplete work is documented in prose and stubs instead.

## Database overview

`gateway/db.py` declares five tables:

- `nodes`: devEUI primary key, identity, first/last seen, radio/battery observations and current detector state.
- `readings`: generated ID, node foreign key, uplink and reception timestamps, unique uplink key, frame/port, environmental values, baseline snapshots and state. Composite node/time index supports history queries.
- `events`: node foreign key, time, kind, state transition, message, JSON details and notified flag.
- `gateways`: gateway ID key, observed first/last times, radio values and uplink count.
- `console_log`: chronological operator messages, level/category and optional node/block identifiers.

`UTCDateTime` requires aware datetimes and normalizes UTC, including SQLite conversion. SQLite enables WAL for writers and uses a read-only file connection for the web reader. PostgreSQL permissions rely on the configured database role: passing read_only to make_engine is not itself PostgreSQL enforcement.

Only detection initializes schema. `init_schema` creates missing tables and adds missing nullable columns; required-column additions raise an error. There is no versioned migration framework. Changes to required columns, types, indexes or constraints need an explicit compatibility/migration plan. Never treat destructive database recreation as a routine upgrade.

## API architecture

All application endpoints inspected are GET/read-only, with JSON dictionaries produced by synchronous handlers. Interactive API docs are at `/api/docs`.

| Route | Contract |
|---|---|
| `/`, `/node/{dev_eui}`, `/alarms` | Static HTML served by FastAPI |
| `/api/config` | Polling, chart and detection display settings |
| `/api/status` | Node readings and alarm/connectivity summary |
| `/api/nodes/{dev_eui}/readings` | Positive hours bounded at 744; default from config; unknown node 404; chronological chart points |
| `/api/events` | kind = alarms/connectivity/all; optional dev_eui; limit 1..2000, default 200; newest first |
| `/api/console/config` | Site/map/features/timing configuration; explicitly marks positions as placeholders |
| `/api/console/status` | Pill, nodes, never-seen sites, gateways, network/block totals, alarm zones and timing warning |
| `/api/console/log` | after_id >=0; limit 1..1000, default 200; entries oldest first |
| `/healthz` | Checks database readability; not an MQTT or detector liveness guarantee |

Query failures become HTTP 503; FastAPI validates query types/ranges. Chart thinning targets about 1500 points but preserves WATCH/ALARM readings, so this is not a strict output cap. General status performs per-node latest-reading queries; console status uses a fixed number of queries. The console source comment says three queries, but the inspected body executes four including gateways; preserve the fixed-query property rather than repeating the stale count.

## Frontend architecture and shared conventions

`index.html`/`console.js`/`console.css` form the operator console; `node.html` and `alarms.html` use shared assets. `common.js` supplies `getJSON`, `esc`, `labelOf`, banners, formatters and `startPolling`. `charts.js` supplies SVG `lineChart`. Reuse these functions instead of introducing a parallel UI stack.

Browser fetches same-origin JSON, with polling and retry/error display rather than WebSockets. Scripts/styles revalidate; page asset references get modification-time version stamps. Operator-facing names use registry site labels; devEUI remains the stable technical identifier. Sound preference uses localStorage and audio needs user interaction to arm. Dynamic HTML must retain escaping.

Alarm markers stay visible at all zooms; normal nodes appear at configured detail zoom and cluster. Coverage zones do not represent fire extent. Counts come from database observations, not visible map markers. WATCH/ALARM persists visually when a node goes missing. RAPID UPDATES is derived from fPort, not inferred from short intervals. Leaflet assets are local; background tile imagery requires external connectivity. `map_blocks` remains an API compatibility field although the current console does not draw it.

## Authentication and permissions

There is no browser authentication or application authorization flow. Read-only database credentials and SQLite file mode constrain the web process. Mosquitto configuration permits anonymous connections; Compose contains development connection defaults, intentionally omitted here. This is not a production security boundary. Secrets belong in environment variables, never documents or source. Telegram transport/logging must avoid exposing token-bearing URLs.

## Development patterns and checks

Python uses snake_case functions/modules, PascalCase classes, constants in uppercase, type hints, dataclasses for records and strict Pydantic models for configuration. Business logic is deliberately separable from I/O. Recoverable malformed uplinks log and skip; setup errors fail clearly; operational database errors retry. JavaScript uses ordinary functions and shared helpers. No formatter/linter configuration or formal contribution/PR template was found. The interface is a monitoring console, not a CRUD/form application; validation chiefly occurs in config, CLI and API inputs.

Windows CMD setup, when needed: `python -m venv .venv`, `.venv\Scripts\activate`, `python -m pip install -r requirements.txt`. Use a supported installed Python; do not assume the launcher has Python 3.11 installed.

Run from this project root: `docker compose up -d`, then in separate activated windows set `DEMO_SPEEDUP=60` and run `python -m gateway.service`, `python -m gateway.sim run`, `python -m gateway.web`. Open localhost port 8000. `python -m gateway.config` prints effective settings; `python -m gateway.status` reads status. `python -m gateway.sim inject node-3` requests a simulated smoke event. `start_all.bat` automates startup but does not prevent duplicate service instances or prove application readiness after its delays.

Run `python -m pytest` for the suite, or select relevant files under `tests/`. Codec/config/ingest tests cover parsing and compatibility; detection/simulator tests use seeded simulations; service tests cover replay/outages/queues; notification tests mock external transport; web tests verify JSON and console contracts. `tests/conftest.py` uses temporary SQLite and optionally PostgreSQL. TEST_DATABASE_URL causes table deletion/recreation: only use an explicitly designated disposable test database. TEST_MQTT_HOST and TEST_MQTT_PORT enable a real-broker test; use a scratch broker. Do not inherit these integration settings blindly.

## Constraints, debt and uncertainty

- Exactly one detection service per broker/client ID. Persistent sessions intentionally use a fixed ID; competing instances take over each other's session. Changing IDs to hide duplication changes delivery semantics.
- Queue contents are volatile; process termination during an outage loses pending writes. MQTT acknowledges after inbox acceptance, before durable storage. This is not end-to-end exactly-once delivery.
- Writer drops oldest reading jobs under pressure but preserves events/logs. If only nondroppable jobs remain, it allows growth beyond the configured limit; the cap is not an absolute memory bound. Inbox is also unbounded.
- Database must be reachable for startup replay. Running detection can survive a later outage; web visibility cannot.
- Required-column migration support, production auth, durable outbox and dependency locking are not implemented in the inspected files.
- Simulation results in README/design rationale are not independent field measurements. Real fire/rain behavior, burn-in, radio coverage, battery estimation and site coordinates require validation.
- Live deployment, current MQTT process count, current database contents, installed dependency versions and team branch policies remain unknown. No application tests were executed for this documentation-only review.

## Git and collaboration

No `.git` metadata is present in this root or its enclosing working folder; read-only Git probes report not a repository. Current branch, remote names, remote-tracking branches and initial clean/dirty status are therefore unavailable, not empty/clean. The supplied directory appears to be an extracted source copy (inference). Obtain the actual clone before branch-aware tasks; do not initialize Git or invent remote settings.

Existing `CLAUDE.md` prohibits pushing, remote changes and gh CLI use, and permits local commits generally. For this task no commits are authorized; new `AGENTS.md` requires explicit instruction before commits/history operations. No Git mutations were performed. Preserve teammates' changes; in an actual clone inspect status/diffs before edits, make only scoped changes and report verification. GitHub protections, reviewers and deployment workflows cannot be inferred from this copy.

Further context: `README.md`, `docs/CODEBASE-GUIDE.md`, `docs/DESIGN-RATIONALE.md`, `docs/DEMO-RUNBOOK.md`, `docs/DEMO-BRIEFING.md`. Design-rationale authorship labels are documentary claims, not independently verified team decisions.
