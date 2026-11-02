# LoRaWAN Wildfire Detection Gateway (LAU VIP)

Prototype gateway software for a LoRaWAN wildfire early-detection network.
Sensor nodes (one Bosch BME688 each: temperature, humidity, pressure, gas
resistance) send readings over LoRaWAN. This software receives them, stores
them, detects smoke, raises alerts, and shows everything on an operator
console for the Bkassine Civil Defence Centre.

No hardware is needed: a simulator stands in for the nodes (including their
transmit behaviour) and for the ChirpStack network server.

```
 simulator (4 nodes)            later: real nodes -> gateway -> ChirpStack v4
   heartbeat every 10 min,                                  |
   ~1/min while a node's local alarm holds                  |
        |                                                   |
        |  MQTT  application/{app_id}/device/{dev_eui}/event/up  (ChirpStack v4 JSON)
        v                                                   v
   +--------------------------- Mosquitto -------------------------+
                                   |
                                   v
   DETECTION SERVICE  (python -m gateway.service)    <- runs on its own, always
     decode -> store -> detect -> alert (console / Telegram / SMS stub)
     -> event log for the console
                                   |
                                   v
                              PostgreSQL
                                   |  read-only role
                                   v
   WEB APP  (python -m gateway.web)   operator console, node charts, alarm history
```

The detection service and the web app are **separate processes**. Alarms are
raised whether or not anyone has the console open; the web app can only
read the database.

---

## 1. One-time setup on a clean Windows machine

All commands are for **Command Prompt** (`cmd.exe`), not PowerShell.

### 1.1 Install the tools

1. **Python 3.11 or newer**: <https://www.python.org/downloads/windows/>.
   In the installer, tick **"Add python.exe to PATH"**. Check in a new
   Command Prompt:

   ```bat
   py -3.11 --version
   ```

   (3.12 is fine too: use `py -3.12` below. 3.11 is what this was tested on.)

2. **Git**: <https://git-scm.com/download/win> (defaults are fine).

3. **Docker Desktop** (runs PostgreSQL and Mosquitto):
   <https://www.docker.com/products/docker-desktop/>. It uses WSL 2; the
   installer sets that up. A reboot is usually needed, and on some laptops
   virtualization must be enabled in the BIOS. Start Docker Desktop and wait
   until it says "Engine running". Check:

   ```bat
   docker compose version
   ```

   No Docker? See [section 8, running without Docker](#8-running-without-docker-sqlite).

### 1.2 Get the code

Put the project **outside OneDrive** (OneDrive tries to sync the thousands of
files in the virtual environment and can lock files that are in use). If the
project must stay in OneDrive, at least create the virtual environment
outside it (e.g. `py -3.11 -m venv C:\venvs\vip`).

```bat
mkdir C:\dev
cd /d C:\dev
git clone <repository URL> vip
cd vip
```

### 1.3 Python environment

```bat
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Your prompt now starts with `(.venv)`. **Every new Command Prompt window**
needs `cd /d C:\dev\vip` and `.venv\Scripts\activate` before the `python`
commands below.

Run the tests (2-4 minutes; the slow ones simulate weeks of weather):

```bat
pytest
```

---

## 2. Run it

### 2.1 Start PostgreSQL and Mosquitto

```bat
docker compose up -d
docker compose ps
```

Both containers should be "running" (Postgres also "healthy"). They keep
running in the background until `docker compose down`.

### 2.2 Demo speed-up

Real nodes report every **10 minutes**, and each node needs **30 readings**
to learn its baseline: 5 hours. For a demo, speed the clock up. In **every**
window you use (service, simulator, web, inject), first run:

```bat
set DEMO_SPEEDUP=60
```

Every duration is divided by 60 (heartbeat 10 s, warm-up ~5 min, missing
after 30 s, a 25-minute smoke event 25 s...). Sample counts and thresholds do not
change. Use the same value in all windows. Leave it unset (or `1`) for
real-time operation. See the effective settings at any time with:

```bat
python -m gateway.config
```

### 2.3 Start the three processes, each in its own window

| Window | Command | What it does |
|---|---|---|
| 1 | `python -m gateway.service` | Detection service: stores readings, detects, alerts, writes the event log |
| 2 | `python -m gateway.sim run` | Simulator: 4 nodes, heartbeat + event-driven uplinks |
| 3 | `python -m gateway.web` | Operator console |

Open <http://127.0.0.1:8000/>.

The status pill shows **WARMING UP** until the nodes have learned their
baselines (window 1 prints `warm-up complete, monitoring` per node), then
**ALL CLEAR**.

### 2.4 Simulate a fire

In a fourth window (with the same `set DEMO_SPEEDUP=60`):

```bat
python -m gateway.sim inject node-3
```

node-3 is site **F-07-03**. Within seconds (at x60):

- the simulator shows node-3 switching from `heartbeat` to `alarm-mode`
  uplinks (its local gas trigger fired);
- window 1 prints a WATCH alert, then a FIRE ALARM block;
- the console turns red: **FIRE ALARM - Block F-07**, a red F-07-03
  marker appears on the otherwise empty map, "Block F-07 - VOC up -
  temperature up - humidity down", and the event log tells the story. Zoom
  in to see all nodes and the red "sensor coverage" zone.

The smoke builds for 5 simulated minutes and holds for 20
(`--ramp-minutes` / `--hold-minutes` to change), then
the node steps back down and an "all clear" is sent. Click F-07-03 in the
node table to see its charts (pick **15 min**).

Try closing the browser and injecting into `node-1`: the alarm still fires
in window 1. Stop the simulator (Ctrl+C): after 3 missed heartbeats every
node is reported **OFFLINE** and the pill shows **NO DATA**.

### 2.5 Other useful commands

```bat
python -m gateway.status
```
Prints nodes, states and recent events (uses the web app's read-only login).

```bat
docker compose exec postgres psql -U gateway -d wildfire -c "select time, level, message from console_log order by id"
```
The console's event log, straight from the database.

```bat
docker compose exec mosquitto mosquitto_sub -t "application/#" -v
```
Shows raw MQTT uplinks as they arrive.

```bat
docker compose exec postgres psql -U webapp_ro -d wildfire -c "delete from readings"
```
Proves the web login is read-only: fails with "permission denied".

```bat
docker compose down -v
```
Stops the containers and **deletes the database** (fresh start).

---

## 3. The operator console

`http://127.0.0.1:8000/` is a dark, two-column console meant to stay open on
a control-room screen.

- **Top bar**: centre name, the **status pill**, date and time, link to
  **Alarm history**. The browser tab title gets a `[FIRE ALARM]` prefix.

  | Pill | Meaning |
  |---|---|
  | FIRE ALARM | at least one node in ALARM (always wins, even if data then stops) |
  | WATCH | at least one node on WATCH |
  | ALL CLEAR | nodes reporting and monitored, none on WATCH/ALARM. A note says if some nodes are missing |
  | WARMING UP | data arriving, but no node has finished warm-up: nothing is being detected yet |
  | NO DATA | no node heard within the missing threshold: gateway, broker or detection service is probably down |

- **Map** (left): satellite imagery (Esri), falling back to OpenStreetMap,
  centred on Bkassine. Gateways are diamonds; the civil defence centre is
  the "CD" square. The detail depends on the zoom, so the map stays readable
  with hundreds of nodes:
  - **Zoomed out (the default view)**: deliberately almost empty. Only the
    gateways and the civil defence centre, plus the one exception below. A
    line above the map says to zoom in to see single nodes.
  - **An alarm is never hidden**: every node in ALARM is a red marker at
    every zoom, including fully zoomed out, each on its own (never grouped)
    and drawn on top. It stays red even if the node then falls silent.
    Nodes in NORMAL, WARMUP or WATCH are not shown zoomed out. An alarming
    node with no map position (no site entry) is named in a red note above
    the map.
  - **Zoomed in** (from `console.map.node_detail_zoom`, 17 by default):
    every node, labelled (F-07-03) and coloured by state; missing nodes are
    grey and dashed. Nodes close together on screen collapse into a count
    badge in their worst state, which splits up as you zoom in. Nodes in
    ALARM also get a translucent red zone: this is the area the **sensors
    cover**, not the extent of the fire.

  Click a node for its
  readings, its devEUI and a link to its chart page. The map libraries
  (Leaflet 1.9.4, Leaflet.markercluster 1.5.3) are bundled, so the console
  works without internet; only the background imagery needs it.
- **Sound**: off by default; the choice is remembered in that browser. It
  beeps every 2 s during a FIRE ALARM. Browsers block sound until the page
  has been clicked, so after a reload the button reads
  **"Sound: ON - click to arm"**: click it once.
- **Sensor network** (right): total nodes, nodes in alarm, gateways online,
  one line per affected block ("Block F-07 - VOC up - temperature up -
  humidity down"), and a node table with honest "last seen" times.
  **RAPID UPDATES** (hover for an explanation) appears only while a node's
  own sensor has noticed a change and it reports about every minute instead
  of every 10; it is not a fire alarm by itself. A node on its normal
  heartbeat shows nothing. **MISSING** means 3 heartbeats have been missed. The table scrolls, alerting and missing
  nodes first. Counts come from the database, never from what the map is
  drawing (a node in the registry that has never reported is on the map but
  not in "total nodes").
- **Event log**: service start/stop, an hourly system check, every state
  change, block alarms and all-clears, and each alert channel's result
  ("telegram: sent ..." or "FAILED ..."). Newest at the bottom.
- **Thermal camera, drone UAV-01, wind station**: greyed and labelled
  **NOT CONNECTED - PLANNED PHASE 2**, with no data. They are switched by
  `console.features` in `config.yaml`; turning one on only shows "enabled,
  no data source configured" until an integration is written.

Operators see **site labels** (F-07-03) everywhere. The LoRaWAN devEUI stays
in the database and the API and is shown only in the map popup and on the
node's chart page.

### Site registry and map positions

`sites:` in `config.yaml` maps each devEUI to a label, a block and a
position, and lists the gateways and the civil defence centre. Blocks are
simply the `block` field: add F-07-04 ... F-07-12 with `block: "F-07"` and
they roll up into F-07, with no change to the console. A node not in the
registry still works: it is shown by its ChirpStack device name and is not
placed on the map. `python -m gateway.config` lists the blocks and their
node counts.

Map detail settings, under `console.map`:

| Setting | Default | Meaning |
|---|---|---|
| `zoom` | 15 | zoom when the console opens |
| `node_detail_zoom` | 17 | all nodes from this zoom in; below it only nodes in ALARM. Must be above `zoom` |
| `cluster_radius_px` | 50 | nodes closer than this on screen share one count badge |
| `alarm_zone_radius_m` | 150 | coverage drawn around each node in ALARM (zoomed in) |

> **The positions in `config.yaml` are PLACEHOLDERS**, scattered around the
> Bkassine reserve for the demo. They **must be replaced with coordinates
> from a real site survey before any deployment**: crews will be sent to
> these points.

---

## 4. Telegram alerts

1. In Telegram, talk to **@BotFather**, send `/newbot`, and copy the token.
2. Send any message to your new bot (or add it to a group and post there).
3. Find the chat id: open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and look
   for `"chat":{"id": ...}` (group ids are negative).
4. In the window where you start the detection service:

   ```bat
   set TELEGRAM_BOT_TOKEN=123456789:AA...your token...
   set TELEGRAM_CHAT_ID=-1001234567890
   ```

5. In `config.yaml`, set `enabled: [console, telegram]` under `notifiers`.
6. Start `python -m gateway.service`. It refuses to start if Telegram is
   enabled but a variable is missing, so a broken alarm channel is found
   immediately, not during a fire.

Secrets live only in environment variables, never in `config.yaml` or git.
A failed send is retried (2 s, 5 s, 15 s) without delaying detection or the
other notifiers. Each channel's final result appears in the console's
event log. Messages name nodes by site label.

**SMS** is a documented stub (`gateway/notify/sms.py`): enabling it stops
the service from starting until it is implemented. For remote sites a GSM
modem on the gateway is the recommended route (works without internet).

---

## 5. How nodes report (heartbeat + event-driven)

Settings: `timing` and `simulator.firmware` in `config.yaml`.

- **Heartbeat**: every node transmits every **10 minutes**
  (`timing.heartbeat_minutes`), enough to know it is alive, cheap on
  battery and airtime.
- **Event-driven**: the node reads its sensor every 30 s and keeps its own
  slow gas baseline. When gas resistance is 15 % below it on 2 reads in a
  row (more sensitive than the gateway's WATCH, and a one-off glitch cannot
  trigger it), the node transmits **at once**, then about **every minute**
  until 10 minutes after the trigger clears.
- **Which mode, by port**: heartbeats are sent on LoRaWAN fPort **2**,
  alarm-mode uplinks on fPort **3** (`lorawan:` in `config.yaml`; same
  payload). The console shows RAPID UPDATES from the port the node used,
  never by guessing from the time between uplinks, so it cannot be fooled
  by timing settings. The real firmware must use these ports.
- **Missing**: a node is MISSING / OFFLINE after **3 missed heartbeats**
  (`timing.missed_heartbeats_for_missing`): 30 minutes. The detection
  service and the console use the same derived value.
- **Duty cycle**: EU868 allows 1 % airtime. For this 11-byte payload the
  shortest legal gap between uplinks is:

  | SF | 7 | 8 | 9 | 10 | 11 | 12 |
  |---|---|---|---|---|---|---|
  | airtime | 62 ms | 113 ms | 206 ms | 371 ms | 823 ms | 1.48 s |
  | min gap | 6 s | 11 s | 21 s | 37 s | 82 s | 148 s |

  The simulator enforces this, so a node far from the gateway (SF11/SF12)
  cannot keep a 1-minute alarm rate.

> The node **firmware does not exist yet**. `simulator.firmware` is a
> placeholder describing the behaviour the gateway expects. Without the
> event-driven part, detection does not work: measured over 240 simulated
> smoke events, a 10-minute heartbeat alone produced **0** alarms (a 25-minute
> smoke event yields only 2-3 readings, fewer than WATCH + ALARM confirmations need).

### Measured at this cadence (simulator)

| | Result |
|---|---|
| False WATCH/ALARM, 336 node-days (typical + harsh weather) | **0** |
| Smoke reaching ALARM, 240 events, every 2 h of the day, nodes at SF7-SF11 | **238** |
| Time from smoke start to ALARM | median **5.5 min**, worst 11.5 min |
| Same, a node at SF12 (duty-cycle limited) | 59 of 60, median **17.5 min** |
| Uplinks per node per day | ~144 (148 in harsh weather) |

---

## 6. How detection works

All thresholds are in [`config.yaml`](config.yaml) under `detection`.

- **Baseline per node, per channel.** An EWMA (exponentially weighted moving
  average) tracks "normal" temperature, humidity and gas resistance (gas in
  log space). Its weight comes from the real time between readings, so
  irregular uplinks (heartbeats, alarm mode, missed packets) are handled.
- **Relative change only.** Each reading is compared with its node's
  baseline, never with an absolute value, so sensor drift over weeks is
  absorbed.
- **The BME688 gas resistance FALLS when smoke/VOCs rise.** A gas *rise* is
  never treated as smoke. The console calls a gas drop "VOC up".
- **Channels must agree.** A reading reaches WATCH or ALARM only if:
  gas resistance has dropped by the threshold **and** (temperature has risen
  **or** humidity has dropped) by its threshold.

  | | gas drop | temp rise | humidity drop |
  |---|---|---|---|
  | WATCH | 25 % | 3 C | 8 %RH points |
  | ALARM | 45 % | 4 C | 12 %RH points |

- **States with hysteresis.** WARMUP -> NORMAL -> WATCH -> ALARM, one level
  at a time. Going up needs 3 consecutive readings at the higher level;
  going down one level needs 5 below it. Warm-up is 30 readings.
- **The baseline freezes** during WATCH and ALARM so it cannot "learn" the
  smoke and cancel the alarm (released after 6 h, so a permanently changed
  sensor cannot stay stuck).
- **Cooldown.** After an ALARM message, no new ALARM message for that node
  for 30 minutes (every state change is still recorded). "All clear" is sent
  only if a message went out for that event.
- **Offline.** A node silent for 3 heartbeats raises an OFFLINE alert (a
  node destroyed by fire goes quiet).
- **Blocks.** Nodes are grouped into blocks (F-07, F-08...). The event log
  records when a block first goes on WATCH or into ALARM, when the alarm
  spreads to more of its nodes, and when it is all clear.
- **Restarts.** The service rebuilds baselines from the last 12 h of stored
  readings, so a restart does not mean another warm-up, and nothing is
  alerted twice.

### Tuning decisions (measured on the simulator)

- **Baseline half-lives**: gas 2 h, temperature and humidity **20 min**.
  With 2 h on every channel, the temperature baseline lags the evening
  cool-down by ~5 C and hides a fire's temperature rise (79 of 120 evening
  and night fires detected, vs 119 of 120 with 20 min).
- **ALARM temperature rise 4 C (was 5 C)**. Smoke starting during a rain
  front: the temperature baseline lags the rain's cooling, so the smoke's
  +6 C read as ~+4.7 C. Over 240 events, misses fell from 9 to 2; false
  alarms stayed at 0 in 336 node-days. The 45 % gas drop is still required.

### Known limitations

- **Fire starting at the onset of rain/fog** can still stop at WATCH
  (2 of 240 simulated events): rain cools and humidifies the air at the same
  time, masking the temperature/humidity signature. WATCH still alerts.
- **Slow nodes alarm later.** At SF12 the duty cycle stretches alarm-mode
  uplinks to ~2.5 min: median 17.5 min to ALARM. Place gateways so nodes
  stay at SF11 or faster.
- **Stepping down is slow at heartbeat rate.** After the node leaves alarm
  mode, "back to NORMAL" needs 5 readings: up to ~50 minutes.
- **A fire that builds more slowly than the gas baseline** (hours) is partly
  absorbed into the baseline.
- **Hot-day VOCs.** Vegetation emits terpenes on hot days, which can lower
  BME688 gas resistance while the air is hot and dry. The simulator does not
  model this; the thresholds need real field data or a controlled burn.
- **BME688 burn-in.** New sensors need about 24-48 h before gas readings are
  stable; the 30-sample warm-up does not cover that.
- **Gateway "online"** is judged from relayed uplinks only. A gateway that
  hears no node looks the same as a dead one (ChirpStack's gateway stats
  events would tell them apart).
- **Map imagery** needs internet and comes from Esri / OpenStreetMap: check
  their terms of use before production use.
- **Prototype security.** Default passwords in `docker-compose.yml`,
  anonymous MQTT, no login on the console. Fine on a laptop, not on a
  network.

---

## 7. Switching to real hardware

### ChirpStack v4 instead of the simulator

The simulator publishes exactly what ChirpStack v4 publishes, so:

1. In `chirpstack.toml`, enable JSON for the MQTT integration:
   ```toml
   [integration.mqtt]
   json = true
   ```
2. Point `mqtt.host` / `mqtt.port` in `config.yaml` at ChirpStack's broker
   (or set `MQTT_HOST` / `MQTT_PORT`).
3. Add every node and gateway to `sites:` with **surveyed** positions.
4. Do not run the simulator. The node firmware must implement the
   heartbeat + event-driven behaviour in [section 5](#5-how-nodes-report-heartbeat--event-driven),
   sending heartbeats on fPort 2 and alarm-mode uplinks on fPort 3.

### Real payload format

The 11-byte format is provisional. It lives entirely in
[`gateway/codec.py`](gateway/codec.py): replace `encode()`/`decode()` there
and update `tests/test_codec.py`. Nothing else depends on the byte layout.

| bytes | field | type | unit |
|---|---|---|---|
| 0 | format version (=1) | uint8 | |
| 1-2 | temperature | int16 | 0.01 C |
| 3-4 | humidity | uint16 | 0.01 % |
| 5-6 | pressure | uint16 | (hPa x 10) - 5000 |
| 7-9 | gas resistance | uint24 | ohm |
| 10 | battery | uint8 | 0.02 V |

All big-endian.

---

## 8. Running without Docker (SQLite)

1. Install Mosquitto: <https://mosquitto.org/download/> (Windows 64-bit
   installer). It installs a Windows service that listens on
   `localhost:1883`, which is all this needs. If it is not running, start
   it in a separate window:

   ```bat
   "C:\Program Files\mosquitto\mosquitto.exe" -c mosquitto\mosquitto.conf -v
   ```

2. In **every** window (service, web, status, tests), before running
   anything:

   ```bat
   set DATABASE_URL=sqlite:///data/wildfire.db
   set WEB_DATABASE_URL=sqlite:///data/wildfire.db
   ```

   (Or edit both URLs under `database` in `config.yaml`.)

3. Run everything as in section 2, skipping `docker compose`. The database
   is the file `data\wildfire.db`; delete it for a fresh start. The web app
   opens it in SQLite's read-only mode.

---

## 9. Tests

```bat
pytest
```

- `tests/test_detection.py`: the ones that matter most:
  `test_long_normal_operation_never_alarms` and
  `..._at_heartbeat_cadence` (two simulated weeks x 4 nodes x 3 seeds, in
  typical and harsh weather: not a single WATCH allowed), and
  `test_smoke_alarms_at_heartbeat_cadence` (every node, six times of day,
  ALARM within 15 min). Plus channel agreement, gas direction, hysteresis,
  frozen baseline, drift, cooldown, offline.
- `tests/test_simulator.py`: node model, airtime and duty cycle, the
  heartbeat / event-driven firmware, demo speed-up.
- `tests/test_config.py`: derived missing threshold, speed-up, site registry.
- `tests/test_codec.py`: payload round-trips, edge values, bad input.
- `tests/test_ingest.py`, `test_service.py`: storage, gateways, detection
  persisted, labels in messages, restart replay, the event log story.
- `tests/test_console_log.py`: event-log entries and block roll-up.
- `tests/test_web.py`, `test_web_console.py`: the read-only API, the
  console status (pill, missing, zones, no devEUI outside its fields), block
  roll-up with the worst state (including 300 nodes in 25 blocks), the
  database node count, and a fixed number of queries whatever the node count.
- `tests/test_notify.py`: console, Telegram (mocked), SMS stub, retries,
  per-channel results.

Database tests run on SQLite. To run them on the Docker Postgres too
(**this deletes the tables in that database**):

```bat
set TEST_DATABASE_URL=postgresql+psycopg://gateway:gateway@localhost:5432/wildfire
pytest
```

---

## 10. Project layout

```
config.yaml              every threshold and setting (sites, console, timing)
docker-compose.yml       PostgreSQL 16 + Mosquitto 2
db/init/                 creates the read-only web role (first start only)
mosquitto/               broker config (dev: anonymous allowed)
gateway/
  codec.py               the ONLY module that knows the payload bytes
  chirpstack.py          ChirpStack v4 topic + JSON envelope, LoRa airtime
  config.py              config loading/validation, demo speed-up
  db.py, store.py        schema and writes
  ingest.py              MQTT message -> decoded, stored reading
  detect.py              detection engine (pure logic, no I/O)
  console_log.py         operator event-log entries, block roll-up
  service.py             detection service entry point
  notify/                console, telegram, sms (stub), dispatcher
  sim/                   node model, placeholder firmware, simulator CLI
  web/                   FastAPI app, console API, static pages
    static/vendor/       Leaflet 1.9.4 + Leaflet.markercluster 1.5.3 (bundled for offline use)
  status.py              command-line status (read-only)
tests/
```

---

## 11. Troubleshooting

| Problem | Fix |
|---|---|
| `cannot reach MQTT broker at localhost:1883` | Start Docker Desktop, then `docker compose up -d` (or start Mosquitto, section 8). |
| `database not reachable yet` (repeats) | Postgres is not up: `docker compose ps`. The service keeps retrying on its own. |
| `docker compose up` says port 5432 or 1883 is already in use | A local PostgreSQL or Mosquitto service already uses it. Stop it (Services app), or use it as in section 8. |
| Pill says **NO DATA** | Is the simulator (or ChirpStack) running? Is the detection service running? |
| Pill stays **WARMING UP** | Normal for 30 readings: 5 h in real time, ~5 min with `DEMO_SPEEDUP=60`. |
| Nodes flip to MISSING in a demo | `DEMO_SPEEDUP` differs between windows. Set the same value everywhere. |
| Yellow bar: "Setup problem: Nodes report every ~10 s, but this console expects..." | The console was started without the simulator's `DEMO_SPEEDUP` (or with another value). Set the same value in every window and restart the console. |
| "Cannot load data: database unavailable" | Start the detection service once; it creates the tables (including new ones after an upgrade). |
| Web app error about column `f_port` after updating | Start the detection service once before the web app: it adds new columns to an existing database and keeps the data. |
| Map background is blank | No internet: imagery is online-only. Markers and zones still work. |
| Map shows no nodes | That is the zoomed-out view: only nodes in ALARM appear there. Zoom in to `node_detail_zoom` (17) for all nodes. |
| `node_detail_zoom must be greater than zoom` at startup | The default view must be zoomed out: raise `node_detail_zoom` or lower `zoom` in `console.map`. |
| No alarm sound | Click "Sound: ON - click to arm" once after loading the page. |
| `'py' is not recognized` | Reinstall Python with "Add to PATH", or use the full path to `python.exe`. |
| `No module named gateway` | Run commands from the project folder with the venv activated. |
| Changed `db/init/*.sql` but nothing happened | It only runs on an empty volume: `docker compose down -v`, then `up -d`. |

---

## 12. Git

Commit locally as usual. Pushing and remotes are handled by the project
owner (see `CLAUDE.md`).
