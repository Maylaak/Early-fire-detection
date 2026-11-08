# Codebase guide

A map of this repository for a new teammate who knows Java but not Python.
It describes the code as it is. For how to install and run it, see
[README.md](../README.md).

Contents:

0. [Python for Java developers (just enough to read this code)](#0-python-for-java-developers)
1. [Every file and directory](#1-every-file-and-directory)
2. [The runtime picture](#2-the-runtime-picture)
3. [One sensor reading, end to end](#3-one-sensor-reading-end-to-end) (the most important section)
4. [Where state lives](#4-where-state-lives)
5. [Where you are most likely to break something](#5-where-you-are-most-likely-to-break-something)

---

## 0. Python for Java developers

| In this code | Java equivalent | Notes |
|---|---|---|
| A `.py` file (a *module*) | A class file, but it can hold many classes and plain functions | Functions do not need to live in a class. |
| A folder with `__init__.py` (a *package*) | A Java package | `gateway/notify/__init__.py` also holds code: it is the package's own module. |
| `python -m gateway.service` | `java -cp . gateway.service.Main` | Runs the module's `if __name__ == "__main__":` block, which calls `main()`. `-m gateway.sim` runs `gateway/sim/__main__.py`. |
| `from gateway.codec import decode` | `import static gateway.Codec.decode;` | Imports are resolved at run time, not compile time. |
| `@dataclass(frozen=True) class Uplink:` | `record Uplink(...)` | Immutable value class, generated constructor, `equals`, `toString`. |
| Pydantic `BaseModel` (in `config.py`) | A Jackson-bound POJO with Bean Validation | Parses YAML into typed objects and rejects bad values at startup. |
| `@app.get("/api/console/status")` above a function | `@GetMapping("/api/console/status")` in Spring | FastAPI is the web framework; the returned `dict` becomes JSON. |
| SQLAlchemy Core (`select(nodes).where(...)`) | jOOQ | Type-safe SQL builders. No ORM entities: rows come back as tuples with named fields (`row.last_seen`). |
| `with engine.begin() as conn:` | try-with-resources around a transaction | Commits at the end of the block, rolls back on an exception. |
| `queue.Queue` | `LinkedBlockingQueue` | Used to pass work between threads. |
| `threading.Thread(target=..., daemon=True)` | A daemon `Thread` | |
| `lambda msg: inbox.put(...)` | `msg -> inbox.put(...)` | Python lambdas are a single expression. |
| A `def` inside another `def` (for example `fetch` in `console_status`) | A local class or lambda that captures variables | It can read the enclosing function's local variables. |
| `X \| None` in a type hint | `@Nullable X` or `Optional<X>` | Type hints are documentation only: nothing checks them at run time. |
| `self` | `this` | Written explicitly as the first parameter of every method. |
| A name starting with `_` (for example `_node`) | `private` | Only a convention: Python does not enforce it. |
| `@property def baseline(self)` | `getBaseline()` | Read as `d.baseline`, with no parentheses. |
| `@staticmethod` | `static` | |
| A virtual environment (`C:\venvs\vip`) | The project's classpath, Maven's local repo | Holds the libraries from `requirements.txt`. You must activate it before running anything. |
| `pytest` | JUnit | Test files are `tests/test_*.py`; test functions are `test_*`. |

---

## 1. Every file and directory

Top level

| Path | Responsible for |
|---|---|
| `.claude/settings.json` | Claude Code permissions for this repo: blocks `git push`, remote edits and the `gh` CLI. |
| `.gitignore` | Keeps caches, virtual environments, the SQLite `data/` folder, `.env` and editor files out of git. |
| `CLAUDE.md` | Rules for Claude Code in this repo (no pushing, no remote changes). |
| `README.md` | Setup, how to run the three processes, demo steps, configuration reference. |
| `config.yaml` | The only configuration file: broker, database, timing, LoRaWAN ports, simulator, detection thresholds, notifiers, web, site registry, console map. |
| `docker-compose.yml` | Starts the two pieces of infrastructure: PostgreSQL 16 and the Mosquitto MQTT broker. |
| `pyproject.toml` | Project name and Python version; tells pytest where the tests are. |
| `requirements.txt` | Third-party libraries (like a Maven `pom.xml` dependency list). |
| `db/init/01-readonly-role.sql` | Run once by Postgres when its volume is first created: makes the read-only `webapp_ro` role the web app logs in as. |
| `mosquitto/mosquitto.conf` | Broker config for development: port 1883, anonymous clients, no persistence. |
| `docs/CODEBASE-GUIDE.md` | This file. |

`gateway/`: the application (one Python package)

| Path | Responsible for |
|---|---|
| `gateway/__init__.py` | Marks `gateway` as a package. No code. |
| `gateway/config.py` | Loads `config.yaml` into typed, validated objects (`Config` and its sections), applies environment-variable overrides and the demo speed-up (`load_config`, `apply_speedup`). `python -m gateway.config` prints the effective settings (`summary`). |
| `gateway/codec.py` | The 11-byte sensor payload format: `encode` and `decode`, and the `SensorReading` record. The only code that knows the byte layout. |
| `gateway/chirpstack.py` | The ChirpStack v4 MQTT message format: builds uplinks (`build_uplink`, used by the simulator), parses them (`parse_uplink`, used by the service), the topic name (`uplink_topic`), and LoRa airtime / duty-cycle maths. |
| `gateway/mqtt.py` | Thin wrapper around the paho MQTT client: `connect` connects, subscribes, and starts paho's network thread. Shared by the simulator and the service. |
| `gateway/db.py` | The database schema (five tables, defined in code), engine creation for Postgres or SQLite (`make_engine`), and schema creation and upgrade (`init_schema`). |
| `gateway/store.py` | Every database **write** the detection service makes (`Store`), plus the few reads it needs on startup. `uplink_key` (the duplicate key) and `NodeState` (a snapshot of a node's state for a queued write). |
| `gateway/ingest.py` | Turns one raw MQTT message into a decoded reading: parse, decode, drop duplicates (`Ingestor.accept`, in memory). `Ingestor.handle` also stores it (tests and tools). Rejects bad messages without stopping. |
| `gateway/detect.py` | The fire detection logic: baselines, thresholds, the WARMUP/NORMAL/WATCH/ALARM state machine, offline detection. Pure logic: no database, no network, no clock. |
| `gateway/console_log.py` | Builds the plain-English lines of the operator console's event log (node events, block roll-ups, system checks, notification results). Pure logic. |
| `gateway/service.py` | The detection service process: wires MQTT, ingest, detection, storage, notifiers and the console log together in one main loop. `Pipeline` is what it does per uplink and per timer, without MQTT. |
| `gateway/writer.py` | The database write queue (`Writer`) and its job kinds (`ReadingJob`, `EventsJob`, `NotifiedJob`, `LogJob`). One thread writes them in order and waits out a database outage; the cap drops old readings, never events. |
| `gateway/status.py` | `python -m gateway.status`: prints what is in the database, using the read-only connection. A debugging tool. |
| `gateway/notify/__init__.py` | The notifier interface (`Notifier`), the alert text (`headline`, `format_event`), creating the enabled notifiers (`build_notifiers`), and the background `Dispatcher` that sends alerts with retries. |
| `gateway/notify/console.py` | Notifier that prints a loud block (and rings the terminal bell on ALARM) in the detection service's window. |
| `gateway/notify/telegram.py` | Notifier that sends a Telegram message through the Bot API. Secrets come from environment variables. |
| `gateway/notify/sms.py` | SMS notifier **stub**: enabling it makes the service refuse to start. Comments describe how to implement it. |
| `gateway/sim/__init__.py` | Marks the simulator package. No code. |
| `gateway/sim/__main__.py` | The simulator process: `run` publishes simulated uplinks; `inject` tells a running simulator to start a smoke event. |
| `gateway/sim/model.py` | Physics of one simulated BME688 node: drift, day/night cycle, rain, noise, injected smoke (`NodeModel.sample`). |
| `gateway/sim/firmware.py` | **Placeholder** for the real node firmware: decides *when* a node transmits (heartbeat, alarm mode, duty cycle) and on which fPort (`NodeFirmware.step`). |
| `gateway/web/__init__.py` | Marks the web package. No code. |
| `gateway/web/__main__.py` | The web app process: `python -m gateway.web` starts the uvicorn HTTP server with the FastAPI app. |
| `gateway/web/app.py` | The FastAPI application (`create_app`): HTML pages, the general JSON API (`/api/status`, `/api/nodes/.../readings`, `/api/events`), static files with cache headers. Read-only. |
| `gateway/web/console.py` | The operator console's JSON API (`/api/console/config`, `/api/console/status`, `/api/console/log`): status pill, node list, gateways, blocks, alarm zones, the timing warning. Read-only. |
| `gateway/web/static/index.html` | The operator console page (map, status pill, sensor network table, event log). |
| `gateway/web/static/console.js` | Everything the console page does: polls the API, draws the pill, table, map markers, alarm zones and log, plays the alarm sound. |
| `gateway/web/static/console.css` | Styles for the console page. |
| `gateway/web/static/common.js` | Helpers shared by all pages: `getJSON`, formatting, badges, the banner, polling. |
| `gateway/web/static/node.html` | Node detail page: charts of one node's readings and its events. |
| `gateway/web/static/charts.js` | A small hand-written SVG line-chart library used by `node.html`. |
| `gateway/web/static/alarms.html` | Event history page (alarms, connectivity, all). |
| `gateway/web/static/style.css` | Styles for `node.html` and `alarms.html`. |
| `gateway/web/static/vendor/leaflet/` | Leaflet 1.9.4 map library (JS, CSS, licence), stored in the repo so the console works without a CDN. |
| `gateway/web/static/vendor/markercluster/` | Leaflet.markercluster 1.5.3 (groups nearby markers into a count badge): JS, its core CSS, licence. The plugin's default badge stylesheet is not included, because the console draws its own badges (`clusterIcon` in `console.js`). |

`tests/`: pytest suite

| Path | Tests |
|---|---|
| `tests/conftest.py` | Shared fixtures: an `engine` that is a temporary SQLite file, and also Postgres when `TEST_DATABASE_URL` is set. |
| `tests/test_codec.py` | Payload encode/decode round trips, limits, bad input. |
| `tests/test_config.py` | Config validation, environment overrides, demo speed-up scaling. |
| `tests/test_console_log.py` | Event-log wording, block roll-ups, storage of log entries. |
| `tests/test_detection.py` | The detection engine: thresholds, hysteresis, baseline freeze, cooldowns, offline. |
| `tests/test_ingest.py` | Parsing, decoding, storing, duplicate handling (redelivery, frame-counter reset, duplicate already in the database). |
| `tests/test_notify.py` | Notifiers, dispatcher retries, Telegram request shape. |
| `tests/test_service.py` | Detection wired to storage; restart replay rebuilds state; redeliveries counted once; a database outage mid-alarm (alerts still go out, everything is written afterwards); the write-queue cap; the persistent MQTT session (one test needs a scratch broker: `TEST_MQTT_HOST`). |
| `tests/test_simulator.py` | Simulator physics, firmware timing, duty cycle, fPorts, envelopes. |
| `tests/test_web.py` | The general web API and pages; that the web app is read-only. |
| `tests/test_web_console.py` | The console API: labels instead of devEUIs, missing/NO DATA logic, zones, log, timing warning, rapid updates. |

---

## 2. The runtime picture

Five processes run during a demo. Two are Docker containers; three are Python
processes you start yourself, each in its own Command Prompt window.

```
  Window 3                                      Window 1                              Window 2
  python -m gateway.sim run                     python -m gateway.service             python -m gateway.web
  ┌──────────────┐  MQTT publish   ┌───────────┐ MQTT     ┌────────────────────┐ SQL   ┌──────────────┐ SQL (read-only) ┌──────────────┐  HTTP poll   ┌─────────┐
  │  simulator   │ ──────────────▶ │ Mosquitto │ ───────▶ │ detection service  │ ────▶ │  PostgreSQL  │ ◀────────────── │   web app    │ ◀─────────── │ browser │
  │ (fake nodes) │  application/…  │  :1883    │ subscribe│ ingest+detect+alert│ write │    :5432     │   webapp_ro     │ FastAPI:8000 │  every 5 s   │ console │
  └──────────────┘    /event/up    └───────────┘          └────────────────────┘       └──────────────┘                 └──────────────┘              └─────────┘
        ▲                                │                          │ HTTPS (optional)
        │ sim/control/inject             │                          ▼
  python -m gateway.sim inject node-2 ───┘                     Telegram Bot API
```

| Process | Started by | Talks to | How |
|---|---|---|---|
| PostgreSQL | `docker compose up -d` | — | Listens on port 5432. Data lives in the Docker volume `pgdata`. |
| Mosquitto | `docker compose up -d` | — | Listens on port 1883. Routes messages by topic; keeps the detection service's session and queued messages on disk (volume `mosquitto-data`). |
| Detection service | `python -m gateway.service` → `main()` → `run(cfg)` in `gateway/service.py` | Mosquitto, PostgreSQL, Telegram | Subscribes to `application/+/device/+/event/up`. Connects to Postgres as `gateway` (owner, read/write). The only process that writes to the database, and the only one that creates tables. |
| Web app | `python -m gateway.web` → `main()` in `gateway/web/__main__.py` → `uvicorn.run(create_app(cfg))` | PostgreSQL | Connects as `webapp_ro` (Postgres) or opens SQLite read-only. Has no write endpoints. Serves pages and JSON on port 8000. |
| Simulator | `python -m gateway.sim run` → `main()` → `cmd_run` in `gateway/sim/__main__.py` | Mosquitto | Publishes uplinks exactly as a real ChirpStack v4 server would; listens on `sim/control/inject` for smoke commands. |
| Browser | You open `http://127.0.0.1:8000/` | Web app | `console.js` polls `/api/console/status` and `/api/console/log` every `web.poll_seconds` (5 s). |
| `python -m gateway.sim inject NODE` (short-lived) | You, in a fourth window | Mosquitto | Publishes one JSON command to `sim/control/inject` and exits. |

Key points:

- **The detection service and the web app never talk to each other.** The
  database is their only link. Alarms are raised and sent with no browser
  open, and the web app keeps working (showing "missing" nodes) if the
  service is down.
- **The simulator stands in for the real LoRaWAN network.** In production,
  nodes transmit over radio to a LoRaWAN gateway, ChirpStack decodes the
  LoRaWAN frame and publishes the same JSON to MQTT. Swapping the simulator
  for ChirpStack is a config change (`mqtt.host`, `mqtt.port`), not a code change.
- **Every Python process reads `config.yaml` itself**, once, at startup, through
  `load_config()` in `gateway/config.py`. Environment variables
  (`DATABASE_URL`, `WEB_DATABASE_URL`, `MQTT_HOST`, `MQTT_PORT`, `DEMO_SPEEDUP`,
  `WILDFIRE_CONFIG`) override it per process.

Threads inside the detection service (it is not single-threaded):

| Thread | Created by | Does |
|---|---|---|
| Main thread | Python | The loop in `run()`: ingest, detect, alert, offline checks, system checks (`Pipeline`). **Never touches the database** after start-up: it hands writes to the writer thread. |
| paho network thread | `client.loop_start()` in `connect()` (`gateway/mqtt.py`) | Reads from the socket and calls the `on_message` callback, which **only** puts the message on the `inbox` queue. |
| Notifier thread | `Dispatcher.start()` (`gateway/notify/__init__.py`) | Sends alerts, retries, then queues `events.notified` and a console-log line through the callbacks given to the `Dispatcher`. |
| Writer thread (`db-writer`) | `Writer.start()` (`gateway/writer.py`) | Every database write, in submission order. While the database is down it retries the job at the head every 2 s; the other threads carry on. |

The simulator has the same pattern: paho's thread puts inject commands on a
`commands` queue, and the main loop in `cmd_run` handles them.

---

## 3. One sensor reading, end to end

This follows one reading from the simulator to a red FIRE ALARM in the
browser. It is the reading that **tips a node from WATCH into ALARM**.

Why that one: no single reading can raise an alarm on its own. A new node
first needs `detection.warmup_samples` (30) readings to learn its baseline.
After that a node moves up **one level at a time**, and each move needs
`confirm_samples` (3) readings in a row at the higher level: NORMAL to
WATCH, then WATCH to ALARM. So the path below is the same for every reading;
only on this one does step 14 change the state to ALARM.

Why it arrives quickly: on a plain heartbeat every 10 minutes, six confirming
readings would take an hour. The node's own sensor notices the gas drop
first (step 3) and switches to **alarm mode**, transmitting every minute
(every read) while its trigger holds and for 10 minutes after, on fPort 3.

### Process 1: the simulator (`python -m gateway.sim run`)

1. **`cmd_run(cfg, args)`**, `gateway/sim/__main__.py`
   The main loop. Every `simulator.firmware.sample_seconds` (60 s) it calls
   `step` on each node's `NodeFirmware`. (At startup it built one
   `NodeFirmware(NodeModel(...))` per node and called `connect()` from
   `gateway/mqtt.py`.)

2. **`NodeFirmware.step(t)`**, `gateway/sim/firmware.py`
   One sensor read. Calls:
   - **`NodeModel.sample(t)`**, `gateway/sim/model.py`. Computes the physical
     reading: baseline + drift (`_advance_drift`) + day/night (`_diurnal`) +
     rain (`_maybe_start_rain`) + noise + the injected smoke level. Returns a
     `Sample` holding a `SensorReading` (the record from `gateway/codec.py`),
     RSSI and SNR.

3. **`NodeFirmware._local_trigger(t, gas_ohm)`**, `gateway/sim/firmware.py`
   The node's own crude check: gas resistance 15 % below its own slow
   baseline (an EWMA using `ewma_alpha` from `gateway/detect.py`) for 2 reads
   in a row. When it fires, `step` sets `_alarm_until` and the node is in
   alarm mode.

4. **Back in `NodeFirmware.step`**: is a transmission due? Yes if this is the
   first one, the trigger just fired, or the interval has passed:
   `interval_while_triggered_seconds` (60 s) while the trigger holds,
   `interval_after_trigger_minutes` (1 min) for the rest of alarm mode, the 10 min
   heartbeat otherwise, each with half a read of slack for clock jitter.
   **No** if less than the EU868
   duty-cycle gap (`min_gap`, computed once in `__init__` from
   `duty_cycle_gap_seconds` → `lora_airtime_seconds` in `gateway/chirpstack.py`)
   has passed since the last one. If due, it increments the frame counter and
   returns `Transmission(sample, reason)`, with `reason` = `ALARM_MODE`.
   Otherwise it returns `None` and nothing is sent.

5. **`_envelope(cfg, model, sample, reason)`**, `gateway/sim/__main__.py`
   Picks the fPort from the reason: `lorawan.alarm_mode_f_port` (3) here,
   `lorawan.heartbeat_f_port` (2) for a heartbeat. Calls:
   - **`encode(reading)`**, `gateway/codec.py`: 11 bytes, big-endian
     (version, temperature, humidity, pressure, gas, battery).
   - **`build_uplink(...)`**, `gateway/chirpstack.py`: the ChirpStack v4 JSON
     envelope as a `dict`, with the payload base64-encoded in `"data"`, a fresh
     `deduplicationId`, and `rxInfo` naming the gateway. Uses `eu868_data_rate`.

6. **`uplink_topic(application_id, dev_eui)`**, `gateway/chirpstack.py`, then
   **`client.publish(topic, json.dumps(env), qos=1)`** (paho). The topic is
   `application/{application_id}/device/{dev_eui}/event/up`.
   **`_log_sample(model, tx)`** prints the line you see in the simulator window.

### The broker (Mosquitto)

7. Mosquitto matches the topic against the detection service's subscription
   `application/+/device/+/event/up` (`mqtt.uplink_topic` in `config.yaml`)
   and forwards the message at QoS 1. The service holds a **persistent
   session** (fixed client id `mqtt.service_client_id`, `clean_session=False`,
   set by `connect(..., persistent=True)` in `gateway/mqtt.py`), so while the
   service is stopped Mosquitto queues its messages (up to
   `max_queued_messages`, kept on disk in volume `mosquitto-data`) and
   delivers them when it reconnects. A message leaves that queue once the
   service has acknowledged it.

### Process 2: the detection service (`python -m gateway.service`)

8. **paho network thread** → the `on_message` lambda set in **`connect()`**
   (`gateway/mqtt.py`) → the lambda passed in from **`run()`**
   (`gateway/service.py`): `inbox.put((msg.topic, msg.payload, utcnow()))`.
   `utcnow()` (`gateway/db.py`) stamps the time this gateway received it.
   paho acknowledges the message to the broker once this callback returns,
   i.e. **before** anything is stored. Nothing else happens on this thread.

9. **`run(cfg)`** main loop, `gateway/service.py`: `inbox.get(timeout=1.0)`
   takes the message off the queue on the main thread.

10. **`Ingestor.accept(topic, body, received_at)`**, `gateway/ingest.py`.
    In memory only; it never touches the database:
    - **`parse_uplink(topic, body, received_at)`**, `gateway/chirpstack.py`.
      Checks the topic against `UPLINK_TOPIC_RE`, parses JSON, checks the
      devEUI matches the topic, base64-decodes `"data"`, picks the best
      gateway by RSSI, reads `fPort`, parses `"time"` with `_parse_time`.
      Returns an `Uplink` record. Raises `EnvelopeError` on bad input.
    - **`decode(up.data)`**, `gateway/codec.py`. 11 bytes back into a
      `SensorReading`. Raises `CodecError` on a wrong length or version.
      (Either error: the message is counted as rejected, logged, and dropped.
      The loop carries on with the next message.)
    - **`Ingestor._first_time(up)`**: is **`uplink_key(up)`**
      (`gateway/store.py`: devEUI + frame counter + uplink time) among the
      last 256 keys seen for this node? If so it is a redelivery (QoS 1 can
      deliver twice): counted as a duplicate and dropped, so it is never
      detected or alerted twice. A frame-counter reset is not a duplicate,
      because the time is new.
    - Returns `Accepted(uplink, reading)`.

11. **`Pipeline.on_uplink(got, received_at)`**, `gateway/service.py`.
    Updates the in-memory `gateways_seen` times (used by the system check),
    then:

12. **`_label(cfg, dev_eui, device_name)`**, `gateway/service.py` →
    **`SitesConfig.label_for`**, `gateway/config.py`: the operator name
    (`F-07-03`) from the site registry, else the ChirpStack device name.

13. **`DetectionEngine.process(dev_eui, name, t, reading)`**, `gateway/detect.py`
    → **`DetectionEngine.node(dev_eui, name)`** finds (or creates) this node's
    `NodeDetector` in the in-memory dict `engine.nodes`.

14. **`NodeDetector.process(t, r)`**, `gateway/detect.py`. The decision:
    - Skips a reading not newer than the last one (`Evaluation(evaluated=False)`).
      This also stops a redelivery that got past step 10 (for example after
      a restart, when the in-memory keys are empty) from counting twice.
    - If the node was offline, adds an `ONLINE` event.
    - Builds a **`Deviation`** against the baseline as it was *before* this
      reading: gas % below baseline (in log space), °C above, %RH points below.
    - **`Deviation.meets(cfg.alarm)`** / **`Deviation.meets(cfg.watch)`**: a
      level is met when gas drop ≥ threshold **and** (temperature rise ≥
      threshold **or** humidity drop ≥ threshold). This reading meets ALARM
      (45 % / 4 °C / 12 points).
    - **`NodeDetector._hysteresis(level)`**: counts consecutive readings
      above the current state. This is the third in a row at ALARM level
      while in WATCH, so it returns `ALARM`.
    - **`NodeDetector._transition(t, ALARM, dev)`**: sets `state` and
      `state_since`, decides whether to notify with
      **`NodeDetector._notified_within`** (30 min cooldown per node), and
      builds the `Event` (kind `STATE`, `WATCH -> ALARM`, message
      `"F-07-03: FIRE ALARM (WATCH -> ALARM); gas -52% vs baseline, ..."`)
      through **`NodeDetector._event`**.
    - Baseline: because the node is in a freeze state (WATCH/ALARM),
      **`NodeDetector._update_baseline`** is *not* called, so the baseline
      does not chase the smoke (unless it has been frozen for over 6 h).
    - Returns an **`Evaluation`** (state, level, baseline used, deviation, events).

15. Back in **`Pipeline.on_uplink`**: `log_reading` prints the reading, and
    **`writer.submit(ReadingJob(up, reading, received_at, ev, NodeState.of(...)))`**
    puts it on the write queue (`gateway/writer.py`). `NodeState` is a copy
    of the node's state *now*, so the job writes the right state even if it
    is only written minutes later.

16. **`Pipeline._raise(ev.events, t)`**, `gateway/service.py`:
    - makes one **`EventRef`** per event (a stand-in for the database id,
      filled in when the event is written) and submits
      **`EventsJob(events, refs, states)`**;
    - **`_alert(dispatcher, events, refs)`**: logs `ALERT ...` and calls
      **`Dispatcher.submit(event, ref)`**, which only puts it on the notifier
      queue. The main loop does not wait for Telegram **or for the
      database**: the alert goes out even while the database is down;
    - **`node_entries(evs, sites)`**, `gateway/console_log.py`: one
      `LogEntry` per event, level `alarm`, with the block from `_block_of`;
    - **`BlockTracker.update(engine.nodes, t)`**, `gateway/console_log.py`:
      recomputes every block's worst state (`_picture`, `_in_state`) and,
      because block F-07 just went from WATCH to ALARM, adds
      `"BLOCK F-07 FIRE ALARM: F-07-03 (1 of 3 nodes)"`;
    - submits both lines as one **`LogJob`**.

### The database writer thread (inside the detection service)

17. **`Writer._run()`**, `gateway/writer.py`, takes jobs off its queue in
    the order they were submitted and calls **`Writer._write(job)`** →
    `job.run(store)`:
    - **`ReadingJob.run`** → **`Store.save_reading(up, reading, received_at, ev, node)`**,
      `gateway/store.py`. **One transaction**:
      - **`Store._upsert_node`**: inserts or updates the `nodes` row
        (`last_seen` = the uplink's time, RSSI, SNR, battery). Never moves
        `last_seen` backwards for a late packet.
      - **`Store._upsert_gateways`**: inserts or updates the `gateways` rows.
      - inserts the `readings` row (with `f_port`, the baseline it was
        compared with and `state = 'ALARM'`), with `uplink_key(up)` as a
        unique key. If the key already exists (a redelivery the in-memory
        check could not know about), the transaction is rolled back and the
        job logs "already stored".
      - **`Store._write_node_state`**: `nodes.state = 'ALARM'`, `state_since`,
        `online`. **This is the value the browser will show.**
    - **`EventsJob.run`** → **`Store.save_events`** → **`Store._insert_events`**:
      inserts the `events` row (`notified = false`) and sets `ref.id`.
    - **`LogJob.run`** → **`Store.append_log(entries)`**: inserts the lines
      into `console_log`.

    If the database is unreachable (`OperationalError`), `_write` logs
    "database unavailable ... detection and alerts continue" **once**, then
    retries the same job every 2 s; everything behind it waits, so nothing
    is written out of order. Any other database error skips that one job
    (logged). The queue holds up to `database.max_queued_writes` jobs; past
    that, **`Writer._make_room`** drops the oldest `ReadingJob` (the only
    `droppable` kind). Events, notified marks and log lines are never dropped.

### The notifier thread (inside the detection service)

18. **`Dispatcher._run()`** takes the event off its queue →
    **`Dispatcher.deliver(event, ref)`**, `gateway/notify/__init__.py`:
    - for each enabled notifier, **`ConsoleNotifier.send(event)`**
      (`gateway/notify/console.py`) and/or **`TelegramNotifier.send(event)`**
      (`gateway/notify/telegram.py`); both build their text with
      **`format_event`** → **`headline`**. Failed channels are retried after
      2, 5 and 15 s; working channels are not held up.
    - after the first success, `on_delivered(ref)` → the lambda from
      `run()` → **`writer.submit(NotifiedJob(ref))`**. That job is queued
      behind the `EventsJob`, so when it runs `ref.id` is set, and
      **`Store.mark_notified`** sets `events.notified = true`.
    - per channel, **`Dispatcher._report`** → `on_result` (a nested function in
      `run()`) → **`notification(...)`** (`gateway/console_log.py`) →
      `writer.submit(LogJob(...))`: `"console: sent "FIRE ALARM" for F-07-03"`.

At this point the alarm is in the database (once the writer has caught up:
normally within milliseconds). Nothing has been pushed to any browser: the
browser asks.

### Process 3: the web app (`python -m gateway.web`)

19. **The browser asks** (see step 22 for who asks): `GET /api/console/status`.

20. **`console_status()`**, `gateway/web/console.py`, registered by
    **`add_console_routes(app, cfg, query, clock)`**, which **`create_app`**
    (`gateway/web/app.py`) calls at startup.
    - **`query(fetch)`**, the nested function in `create_app`: opens a
      read-only connection and turns database errors into HTTP 503.
    - **`fetch(conn)`**, nested in `console_status`. A fixed number of
      queries, whatever the node count:
      all `nodes`; each node's latest `readings` row (joined on
      `readings.time == nodes.last_seen`); the time of each node's previous
      reading (for the timing check); all `gateways`.
    - **`_node(n, r, now)`** for each node: label, block and position from the
      site registry, `state` (now `ALARM`, read from `nodes.state`),
      `missing`, `rapid_updates` (true because the latest reading has
      `f_port == 3`), the latest values versus baseline, and
      **`_signals(r)`** ("VOC up", "temperature up", "humidity down").
    - Inline in `console_status`: the **pill**. Any node in ALARM →
      `FIRE_ALARM` (order: FIRE_ALARM > WATCH > NO_DATA > WARMING_UP > ALL_CLEAR).
    - **`_gateways`**, **`_blocks`** (the "Block F-07 - VOC up - ..." lines),
      **`_map_blocks`** (per-block aggregates; still returned, but the current
      map does not draw them), **`_unplaced`**, **`_alarm_zones`** (polygon
      from **`_circle`** and **`_hull`** around each alarmed node),
      **`_timing_warning`**.
    - Returns a `dict`; FastAPI turns it into JSON.

### Process 4: the browser (`gateway/web/static/console.js`)

21. On page load, **`start()`** fetches `/api/console/config` once (into the
    global `CC`), then **`renderPhase2()`** and **`initMap()`**, which builds
    the Leaflet map and its layers (`layers.alarms`, `layers.nodes` as a
    `markerClusterGroup`, `layers.zones`, `layers.gateways`).

22. **`tick`** (a function inside `start`) runs every `CC.poll_seconds` (5 s):
    **`getJSON("/api/console/status")`** (`common.js`) — this is the request
    in step 19 — then:

23. **`renderStatus(s)`**:
    - **`setPill("FIRE_ALARM", "Block F-07")`**: the big red pill, the tab
      title becomes `[FIRE ALARM] ...`, and **`sound.setAlarm(true)`** starts
      the beeping if sound is on and armed.
    - the sensor network table: the node's row sorted to the top with
      **`badge("ALARM")`** and a **RAPID UPDATES** tag (tooltip `RAPID_TIP`).
    - the yellow setup-problem bar if `timing_warning` is set.
    - **`drawMap(s)`**:
      - **`drawAlarmMarkers(s)`**: a red marker for every node in ALARM, in
        `layers.alarms` (visible when zoomed out, so a fire is never hidden).
      - **`drawNodes(s)`**: updates the node's marker in place (keyed by
        devEUI) with **`nodeMapState`** and **`nodeIcon`**, then
        `refreshClusters()` so its cluster badge (**`clusterIcon`**) turns red.
      - **`renderMapInfo()`**, **`drawZones()`** (the red sensor-coverage polygon,
        shown zoomed in). **`applyDetail()`**, run on every zoom, decides which
        layers are visible.

24. **`refreshLog()`**: **`getJSON("/api/console/log?after_id=N")`** →
    **`console_log_api()`** in `gateway/web/console.py` → the new
    `console_log` rows from steps 17 and 18 appear at the bottom of the event
    log.

**Worst-case delay** from the node's sensor read to the red pill: up to one
`sample_seconds` (60 s) before the read happens, under 1 s in the service,
and up to one `poll_seconds` (5 s) before the browser asks again.

---

## 4. Where state lives

### In the database (PostgreSQL in Docker volume `pgdata`, or `data/wildfire.db` with SQLite)

Defined in `gateway/db.py`. Written only by the detection service (`gateway/store.py`).

| Table | One row per | Holds |
|---|---|---|
| `nodes` | node ever heard | name, first/last seen, last frame counter, RSSI, SNR, battery, **current state**, `state_since`, `online`. |
| `readings` | uplink stored | time, received time, `uplink_key` (unique, for de-duplication), `f_cnt`, `f_port`, the five sensor values, gas saturated flag, radio values, and, after detection, the baseline it was compared with and the state after it. |
| `events` | state change, OFFLINE or ONLINE | kind, from/to state, message, details (JSON: deviations and baselines), `notified`. |
| `gateways` | LoRaWAN gateway seen in `rxInfo` | first/last seen, last RSSI/SNR, uplink count. |
| `console_log` | event-log line | time, level, category, message, devEUI, block. |

This survives every process restart and `docker compose down`. It is **deleted
by `docker compose down -v`** (the `-v` removes the volume).

### In memory (lost when the process stops)

Detection service (`gateway/service.py`):

| What | Where | On restart |
|---|---|---|
| Each node's baselines (temperature, humidity, log gas), sample count, hysteresis counters (`_up`, `_down`), baseline-freeze start | `NodeDetector`, in `DetectionEngine.nodes` (`gateway/detect.py`) | **Rebuilt** by `_replay()`: it re-runs the last `replay_hours_on_startup` (12 h) of stored readings through the engine, without re-alerting, and writes the resulting states to `nodes`. A node silent for longer than that starts warm-up again (30 readings). |
| Notification cooldowns (`_last_notified`) and "did we alert this episode" (`_episode_notified`) | `NodeDetector` | **Rebuilt as a side effect of the replay**, because the replay runs the same `_transition` code. |
| Each block's last known picture | `BlockTracker._last` (`gateway/console_log.py`) | Re-primed from the replayed states by `BlockTracker.prime`, so a restart logs no false "block alarm". |
| Alerts waiting to be sent or retried | `Dispatcher._queue` (`gateway/notify/__init__.py`) | On Ctrl+C, `dispatcher.stop()` gives them up to 10 s. **If the process is killed, they are lost**, but the event is still in `events` with `notified = false`. |
| Messages received but not yet processed | `inbox` queue in `run()` | Lost if the process is killed (already acknowledged to the broker). Usually empty. |
| Database writes not yet done | `Writer._queue` (`gateway/writer.py`) | On Ctrl+C, `writer.stop()` gives them up to 10 s. Normally empty; **during a database outage it holds every write since the outage began, and if the process is killed then, those writes are lost** (their alerts already went out). |
| Recent uplink keys, for duplicates | `Ingestor._recent` (last 256 per node) | Start empty. A redelivery after a restart is caught by the detector (not newer than its last reading) and by the `readings` unique key instead. |
| Counters (accepted / duplicates / rejected), writes waiting and dropped | `Ingestor.stats`, `Writer.pending`, `Writer.dropped` | Reset to 0. Only used in the "alive" log line. |
| Last time each gateway was heard | `Pipeline.gateways_seen` | Seeded from the `gateways` table at start-up. Used by the system check, so it works during an outage. |

Simulator (`gateway/sim/`):

| What | Where | On restart |
|---|---|---|
| Drift, rain, active smoke events, battery | `NodeModel` | Start fresh. **An injected smoke event is lost.** |
| Frame counter, local-trigger baseline, alarm-mode timer, last transmit time | `NodeFirmware` | Start fresh. The frame counter restarts at 0; harmless, because the duplicate key also includes the uplink time, which is new. |

Web app: no state of its own. It computes `block_shapes` once at startup from
the site registry. Everything else is read from the database on each request.

Browser: the config (`CC`), the map markers (`nodeMarkers`, `alarmMarkers`),
the last log line id (`lastLogId`): all rebuilt on page reload. The sound
on/off choice is in `localStorage`.

Mosquitto: the detection service's **persistent session** and the messages
queued for it (`persistence true`, saved every 60 s to volume
`mosquitto-data`). Uplinks published while the service is stopped are
delivered when it reconnects, up to `max_queued_messages` (100,000); the
session expires after 7 days without the service
(`persistent_client_expiration`). The service logs "resumed session" or
"new session" when it connects. A Mosquitto crash can lose up to the last
60 s of queued messages. Other clients (simulator, web) use clean sessions:
nothing is kept for them.

### In configuration (read once at startup; a change needs a restart)

| Source | Holds |
|---|---|
| `config.yaml` | Everything tunable: broker address, database URLs, timing (heartbeat, missed heartbeats, speed-up), LoRaWAN ports, simulator nodes and physics, detection thresholds, notifiers, web port, **the site registry** (labels, blocks, map positions — all **PLACEHOLDERS**), console title and map settings. |
| Environment variables | `DATABASE_URL`, `WEB_DATABASE_URL`, `MQTT_HOST`, `MQTT_PORT`, `DEMO_SPEEDUP` (override `config.yaml`), `WILDFIRE_CONFIG` (another config file), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (secrets: never in the YAML). They apply only to the window they are set in. |
| `db/init/01-readonly-role.sql` | The `webapp_ro` role and its grants. Runs only when the Postgres volume is created. |

---

## 5. Where you are most likely to break something

### 1. Processes that disagree about time (`DEMO_SPEEDUP`, `config.yaml`)

Each process calls `load_config()` on its own, and `apply_speedup()` divides
every duration (heartbeat, half-lives, cooldowns, missing threshold, replay
window) by `DEMO_SPEEDUP` **in that process only**. Set `DEMO_SPEEDUP=60` in
the simulator window and forget it in the web app window, and the console
judges nodes against a 10-minute heartbeat while they report every 10 s, so
every timing shown is wrong. The detection service with the wrong value
learns its baselines and decides OFFLINE on the wrong time scale.

The same goes for editing `config.yaml` while processes run: each keeps the
values it read at startup.

Protection that exists: the console's yellow "Setup problem" bar
(`_timing_warning` in `gateway/web/console.py`) and the speed-up shown in the
service's "started" log line. **Rule: same `DEMO_SPEEDUP` in every window,
restart all three after any config change.**

### 2. The contract with the node firmware (`codec.py`, fPorts, `firmware.py`)

Three things must match, byte for byte, between the gateway and the real
node firmware that does not exist yet:

- the payload layout in `gateway/codec.py` (11 bytes, big-endian, version byte 1);
- the fPorts in `config.yaml` → `lorawan:` (2 = heartbeat, 3 = alarm mode);
- the transmit behaviour that `gateway/sim/firmware.py` imitates.

If someone changes `encode` without `decode` (or the firmware team changes
the format without telling you), **`decode` raises `CodecError` and every
reading is silently rejected**: you only see `rejected message` warnings in
the service window, and the console turns to MISSING/NO DATA. Change the
layout only together with a new `FORMAT_VERSION`, and update
`tests/test_codec.py`. If the firmware sends alarm mode on another port,
RAPID UPDATES never shows, and the timing warning starts counting
alarm-mode uplinks as too-fast heartbeats.

The simulator passes the tests because it uses the same `encode` the gateway
decodes against. That proves nothing about the real firmware: test against
real captured payloads when they exist.

### 3. Writes to the database outside `Store`, and the `last_seen` rule

The status endpoint finds each node's latest reading with a join on
`readings.time == nodes.last_seen` (`fetch` in `console_status`). That works
only because `Store._upsert_node` sets `last_seen` to exactly the time of the
newest stored reading, in the same transaction as the insert. If anyone
updates `nodes.last_seen` any other way (a "touch" on a heartbeat with no
reading, a manual SQL fix, a second writer process), the join finds nothing:
the node loses its latest values, its `rapid_updates` flag and its alarm
signals in the console, without any error.

More generally:

- **Only the detection service writes.** The web app connects as
  `webapp_ro`; a write endpoint added there fails with a permission error on
  Postgres, by design. Do not "fix" that by giving the web app the owner login.
- **State lives in two places:** `NodeDetector` in memory is the truth;
  `nodes.state` is a copy written by `Store._write_node_state`. Changing
  `nodes.state` in SQL does not change the detector, and the next reading
  overwrites it.
- **Do not write to the database from the main loop.** Go through
  `writer.submit(...)` with a job. A direct `Store` call there would block
  detection and alerts for the whole of a database outage, which is the
  problem the writer exists to solve. A new job kind must decide whether it
  is `droppable`; only plain readings are.
- **Do not change the client id or `clean_session` of the service's MQTT
  connection** without thinking about the broker's queue: a new client id
  starts a new, empty session (the old one keeps queueing until it expires),
  and a second service with the same id takes the session over.
- **Do not do work on the paho thread.** The MQTT callback only puts the
  message on `inbox`. Database writes or detection on that thread would run
  at the same time as the main loop and corrupt the detectors' state, which
  has no locks.

### 4. Schema changes

There is no migration tool. `init_schema()` (`gateway/db.py`) creates
missing tables and, through `_add_missing_columns`, adds **nullable** new
columns to existing tables with `ALTER TABLE`. Consequences:

- A new column declared `nullable=False` makes the service refuse to start
  on an existing database (`RuntimeError: ... recreate the database`), and the
  only fix is `docker compose down -v`, **which deletes all data**. Add new
  columns as nullable and handle `NULL` for old rows (as `readings.f_port` does).
- Renaming, retyping or dropping a column is not handled at all: the code and
  the existing table silently disagree until a query fails.
- Only the detection service runs `init_schema`. After a schema change,
  start the service **before** the web app, or the web app queries a column
  that does not exist yet (HTTP 503).
- The read-only grants come from `db/init/01-readonly-role.sql`, which runs
  only when the volume is first created; new tables are covered by its
  `ALTER DEFAULT PRIVILEGES` only if the `gateway` role creates them.
- Tests run on SQLite by default. Something that works on SQLite can fail on
  Postgres (types, strictness). Run the suite with `TEST_DATABASE_URL`
  pointing at a **throwaway** Postgres database before merging a schema change.

### Also worth knowing

- **Site positions are made up.** Every `lat`/`lon` in `config.yaml` →
  `sites` is a placeholder. Operators will send crews to those points; they
  must come from a site survey before any deployment.
- **Warm-up is 30 readings on purpose.** Lowering it makes demos faster and
  baselines noisier (more false alarms). Use `DEMO_SPEEDUP` for demos instead.
- **Browser caching.** `page()` in `gateway/web/app.py` stamps each
  `/static/...` URL with the file's modification time, and `_RevalidatingStatic`
  sends `Cache-Control: no-cache`. A new page or script must be referenced as
  `src="/static/..."` or `href="/static/..."` in the HTML for the stamp to apply.
- **Docstrings can lag the code.** `map_blocks` in `/api/console/status` is
  a leftover from the earlier per-block zoomed-out map: still computed and
  tested, no longer drawn. When a comment and the code disagree, the code
  (and its test) wins.
