# Demo briefing

For study the night before. Every number here is from the simulator, unless it says otherwise.

---

## 1. What the system does

Small battery-powered sensor boxes in the forest measure the air once a minute: temperature, humidity and a gas reading that changes when there is smoke. Each box learns what normal air looks like at its own spot, and decides for itself that there is a fire only when the air is smokier, hotter and drier at the same time. It then sends a fire alert by long-range radio to a receiver at the edge of the forest; otherwise it only sends a short "I'm alive" message once a day, which saves battery. The alert reaches the operators at once on a map screen and by Telegram, and stays on screen until an operator has checked the situation and cleared it.

---

## 2. One reading, from node to alarm

1. The node measures the air once a minute and compares each reading with its own learned normal: the gas must have dropped by enough, and at the same time the temperature must have risen or the humidity fallen.
2. Three such readings in a row put the node on WATCH, and three more stronger ones put it in ALARM; only then does it transmit a 2-byte FIRE ALERT, as soon as the radio rules allow.
3. The radio message reaches the LoRaWAN gateway, and the network server passes it on to a "post office" for messages called the MQTT broker.
4. Our detection service picks the message up, throws away any copy it has already seen, and marks that node as in FIRE ALARM.
5. It sends the alert at once to the operator's screen and to Telegram, and saves the event in the database.
6. The operator console reads the database every 5 seconds, so the map and status turn red, and they stay red until an operator clears the alarm; the clear is recorded, and the history is never deleted.

---

## 3. The ten likely questions

**1. Why LoRaWAN?**
A forest has no Wi-Fi and often no mobile signal. LoRaWAN reaches several kilometres on a tiny battery, and one gateway can serve hundreds of nodes. The cost is that each node can only send a few short messages: by law, under 1 % of the time on air. That's why a node sends almost nothing: a 2-byte "I'm alive" message once a day, and a 2-byte alert if it detects a fire. *(LoRaWAN was a given in the project brief. These are the standard reasons for it.)*

**2. Why these sensors (BME688)?**
One small, cheap, low-power chip measures temperature, humidity, pressure and gas. Smoke shows up on all three of the ones we use. Its gas sensor's resistance falls when there are more burning-related gases in the air. That's the main smoke signal, and heat and dryness confirm it.

**3. Why not a fixed threshold, like "alarm above 50 °C"?**
Every one of these gas sensors reads differently in clean air, often by several times, and each one drifts over weeks. One fixed number would be too sensitive for some nodes and blind for others. So each node learns its own normal, and we alarm on the change from that normal: for example, gas down 45 % and temperature up 4 °C.

**4. How do you avoid false alarms?**
Three ways:
- **Two signs must agree.** Gas alone isn't enough: rain lowers the gas reading too, but rain is cool and wet, not hot and dry.
- **The change must last.** Three readings in a row are needed, so a single glitch does nothing.
- **A person confirms the end.** The alarm never clears by itself; an operator clears it after checking.
- **Per-node normal.** Every node is judged against its own normal.

In simulation that gave zero false alarms over 336 node-days, including bad weather. That's simulated weather, though, not a real forest.

**5. How fast does it detect a fire?**
In simulation, about 8 minutes from smoke reaching the node to its fire alert, whether the node is near the gateway or far from it: the node decides by itself and then sends one short message, which the radio rules don't slow down. We could get about 5.5 minutes by reading every 30 seconds, but our power study shows that halves the battery life, from about 3 years to about 1.6, so we chose once a minute.

**6. What happens if a node dies, for example burns?**
Every node sends an "I'm alive" message once a day. If one misses three of them, the system raises an OFFLINE alert and the map shows it as missing. Be honest about the cost: that takes **three days**, a choice made to save battery. A node that burns before it detects anything would go unnoticed that long, which is why neighbouring nodes matter. The interval is a single setting and can be shortened at some battery cost.

**7. What if the gateway computer or the network goes down?**
- **Database down:** if the database stops, detection and Telegram alerts keep working, and the saved data catches up afterwards.
- **Our software restarts:** the broker holds the messages and hands them over when it's back, so alerts aren't lost. An alarm that was on stays on after a restart.
- **Whole computer down:** if the whole gateway computer is off, nothing is detected. The status then shows NO DATA, never a false "all clear".

**8. Why is there no camera or drone yet?**
They are phase 2, and there's no hardware yet. The screen keeps their place, clearly marked "NOT CONNECTED". We chose not to fake live numbers for them, because a fire console must never show data that isn't real.

**9. How did you test it without a forest or a fire?**
We built a simulator that behaves like real nodes:
- **Weather:** day and night, rain and fog.
- **Sensor faults:** drift and glitches.
- **Radio limits:** the real rules on how often a node may transmit.
- **Smoke:** smoke events can be injected on demand.

Automated tests then run weeks of simulated weather with no fire to prove there are no false alarms, and hundreds of simulated fires to measure detection: 239 of 240 reached ALARM. The honest limit is that the smoke itself is our model, not a measurement.

**10. Why does a new node need time before it's useful?**
It needs 30 readings, half an hour at one a minute, to learn what normal looks like at its spot. Alarming before that would be guessing. The node keeps what it has learned, so it doesn't start over after a restart.

**Likely extra question: why doesn't the alarm clear itself?**
Because the smoke fading doesn't prove the fire is out. The alarm stays until an operator has checked and clears it, with the password-protected Clear Alarm button or, in the demo, with a timed command. The clear is logged, and the original alarm stays in the history.

---

## 4. Five things we can't answer yet

| Question | Honest answer |
|---|---|
| "Has it ever detected a real fire?" | No. It has only been tested against our own simulated smoke; the next step is a controlled burn near a real node. |
| "On a hot day pine trees release gases that look like smoke. Won't it false-alarm?" | Possibly. Our simulator doesn't model those gases, so we don't know yet; it needs weeks of real data from the forest on hot days. |
| "How many nodes per hectare, and how far apart?" | We don't know yet. Spacing and gateway placement need a site survey and a radio range test; the positions on the map are placeholders. |
| "How long will the batteries last?" | Our power study says about 3.1 years. Nodes now transmit only once a day plus rare alerts, so the radio adds almost nothing; but it's a calculation, not measured on real hardware yet. |
| "Does it work with the real network server and the real node firmware?" | Not tested. Detection now runs on the node itself, so it depends on firmware that doesn't exist yet; and we copied the real network server's (ChirpStack's) message format without connecting a real one. |

Other limits, if pushed:
- **Distant smoke:** smoke that arrives cool, from a fire further away, doesn't trigger, because we also require heat or dryness.
- **Rain:** a fire that starts as it rains can stop at WATCH (1 of 240 in simulation). WATCH stays inside the node, so the operators see nothing.
- **Dead nodes are noticed slowly:** three missed daily messages, so three days.
- **No readings on screen:** nodes send no routine readings, so the node charts stay empty; we can't look back at the air before an alarm.
- **Security:** only clearing an alarm is password-protected; the broker and viewing the screen have no passwords yet.

---

## 5. The tools we used, and why

**The order things run in:**
simulator (stands in for nodes and gateway) → **Mosquitto** (broker) → **detection service** (Python) → **PostgreSQL** (database) → **web console** (Python + browser).
Telegram gets alerts directly from the detection service.

| Tool | What it is, in plain words | How we use it, and why |
|---|---|---|
| **Docker** | Runs a program inside a sealed "container" that already has everything that program needs, so it runs the same on any computer. | We use it to run the database (PostgreSQL) and the broker (Mosquitto) without installing them on Windows. Nobody has to install and configure them by hand; it's one command, and everyone gets the same versions. |
| **Docker Compose** | A file (`docker-compose.yml`) listing several containers to start together. | `docker compose up -d` starts both containers; `docker compose down -v` wipes them. Data is kept in Docker "volumes" (named storage) so it survives restarts. |
| **MQTT** | A standard way for devices to send messages through a central "post office". Senders publish to a topic (an address); whoever subscribed to it receives them. | It's what LoRaWAN network servers (ChirpStack) use to hand on sensor messages. Senders and receivers never talk directly, so each can restart on its own. |
| **Mosquitto** | The most widely used MQTT broker: the "post office" program itself. Small and free. | It receives every reading from the simulator (later, the real network server) and delivers it to the detection service. Set to keep messages for the detection service while it's stopped, so nothing is lost on a restart. |
| **ChirpStack** (not installed) | The open-source LoRaWAN network server: it sits behind the radio gateway and turns radio packets into MQTT messages. | We copied its exact message format, so the real one can replace the simulator by changing settings, not code. We didn't install it, because it needs its own database and setup and adds nothing to testing detection. |
| **PostgreSQL** | A database: stores data in tables, safely, for several programs at once. | It stores every reading, node, event and log line. The detection service writes; the web console only reads, with a login that is *not allowed* to change anything. |
| **SQLite** | A tiny database stored in a single file, with no server. | A fallback for running without Docker, and what the automated tests use. |
| **Python 3.11** | The programming language for all our code. | Quick to write, and has good libraries for everything below. |
| **paho-mqtt** | Python library for talking to an MQTT broker. | The simulator sends with it; the detection service receives with it. |
| **SQLAlchemy** + **psycopg** | Python libraries for talking to databases. | One code path works on both PostgreSQL and SQLite. |
| **FastAPI** + **uvicorn** | A Python library for building a website's back end, and the small web server that runs it. | They serve the operator console and the data it asks for every 5 s. |
| **pydantic** + **PyYAML** | Read the settings file and check every value. | All thresholds and settings live in `config.yaml`. A typo stops the program at start instead of being silently ignored. |
| **httpx** | Python library for calling websites. | Sends the Telegram messages. |
| **Telegram Bot API** | Telegram's free service for programs to send messages. | Alert channel to phones. The secret key is kept outside the code. |
| **Leaflet** (+ markercluster) | A JavaScript map library for web pages. | Draws the map, node markers and alarm zones. Saved inside the project, so it works without internet. |
| **Esri / OpenStreetMap** | Online map image providers. | The satellite and street background. This is the only part of the screen that needs internet. |
| **pytest** | Python's testing tool. | Runs about 330 automated tests, including weeks of simulated weather. |
| **Node.js** | Runs JavaScript outside the browser. | Only for two small tests of the console's map and Clear Alarm code. |
| **Git** | Keeps the history of every change to the code. | Version control. |

**Why it is split into separate programs** (simulator, detection service, web console): the alarm must not depend on anyone having the web page open, and a problem in the screen must not stop detection.
