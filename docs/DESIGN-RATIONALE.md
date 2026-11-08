# Design rationale

Why the gateway is built the way it is, so each decision can be defended at a
design review, or challenged.

For each decision:

- **Decision**: what we did.
- **Who decided**: see "About authorship" below.
- **Alternatives**: what else was on the table.
- **Why this one**: the argument for it.
- **What it costs**: the price we pay.
- **What would change our minds**: the evidence that should reopen it.
- **Weak spot**: where a reviewer would be right to push back, when there is one.

All numbers come from the simulator unless stated otherwise. **The simulator
was written as part of this project, so every measured number here measures
the detector against our own model of a fire, not a real one.** That is the
single biggest weakness in this design (see [the last section](#the-challenge-that-applies-to-everything)).

## About authorship

"Who decided" uses three labels:

- **Spec**: in the written requirements given to Claude Code (the build spec
  of 1 Nov 2026 and the change requests that followed). The session record
  does not show which of those requirements came from you and which from
  Abdul Rahman, so **every "Spec" item is yours or his: please re-attribute
  each one before the review.**
- **Your call**: a choice you made when Claude Code offered options or proposed
  something, including the times you overruled a proposal.
- **Claude Code**: proposed by Claude Code, usually with a measurement, and
  accepted by you. These are the ones you most need to be able to argue
  yourself, or disown.

## Summary

| # | Decision | Who decided | Weak spot? |
|---|---|---|---|
| 1 | Detection service separate from the web app | Spec. Read-only database role: Claude Code | Partly: fixed while running, but the service cannot start without the database, and the console is blind during an outage |
| 2 | MQTT, and mimicking the ChirpStack envelope instead of installing ChirpStack | Spec | Yes: compatibility is untested against a real ChirpStack |
| 3 | 11-byte packed payload instead of JSON | Spec (provisional format from the firmware side) | Minor |
| 4 | EWMA baselines, relative thresholds, never absolute | Spec. Per-channel half-lives, log-space gas, frozen baseline: Claude Code | Yes: thresholds are not scaled to each sensor's noise |
| 5 | Channels must agree before WATCH or ALARM | Spec | Yes: smoke with no heat or dryness at the sensor never even reaches WATCH |
| 6 | Hysteresis and a notification cooldown | Spec. The counts (3 / 3 / 5) and 30 min: Claude Code | Minor |
| 7 | Warm-up of 30 readings | Your call (you overruled a proposal for 12) | Partly: 12 was never measured |
| 8 | Heartbeat plus alarm-mode intervals, within duty cycle | Spec (heartbeat + event-driven). Alarm-mode interval, duty-cycle enforcement, fPort signalling: Claude Code | Yes: it depends on firmware that does not exist |
| 9 | A gas resistance DROP is the smoke/VOC signal | Spec | Yes: other things lower gas resistance too |
| 10 | Zoomed out, only gateways and the centre, but nodes in ALARM always shown | Spec, refined by your own change request | Yes: WATCH and MISSING nodes are invisible zoomed out |
| 11 | Thermal camera, drone, wind station shown as NOT CONNECTED | Spec | No |
| 12 | Smaller decisions (NO DATA pill, replay on restart, OFFLINE alerts, ...) | Mostly Claude Code | See each |

---

## 1. Separating the detection service from the web app

**Decision.** Two processes. The detection service (`python -m gateway.service`)
subscribes to MQTT, decodes, stores, detects and alerts. The web app
(`python -m gateway.web`) only reads the database. They share nothing but
the database. On Postgres the web app logs in as `webapp_ro`, a role with
SELECT permission only, so "read-only" is enforced by the database, not by
programmer discipline.

**Who decided.** Spec: "This separation is required. The detection service
must keep running and keep alerting whether or not anyone has the web page
open." The read-only database role was Claude Code's way of enforcing it.
Skipping the suppression-request button, because it would have needed the
web app to write, was Claude Code's recommendation and your call.

**Alternatives.**
- One process: detection running as a background task inside the web server.
- Detection in the browser (it alarms only while a page is open).
- Detection inside ChirpStack itself (its JavaScript payload codecs can run
  per-uplink code, but they have no memory between uplinks, so no baselines).

**Why this one.**
- An alarm must not depend on an operator having a tab open, a web server
  staying up, or a browser not being put to sleep.
- A web bug (a slow query, a crash on a bad request, an upgrade) cannot stop
  detection.
- The web app can be restarted, redeployed or exposed to more users without
  touching the alarm path.
- A read-only role means a compromised or buggy web app cannot change alarm
  history or node state. Tested: the web role's `delete from readings` is
  refused with "permission denied".

**What it costs.**
- Two processes to start, monitor and keep in step. They read `config.yaml`
  separately; the "TX FAST on every node" bug of 2 Nov was exactly this (the
  web app was started without the same `DEMO_SPEEDUP`).
- The database is now the integration point, so both sides depend on its
  schema. A schema change has to be deployed to both.
- No push from detection to the browser: the console polls every 5 s, so an
  alarm can take up to 5 s to appear on screen.
- State exists twice: the detector's state in memory and a copy in `nodes.state`.

**What would change our minds.** Needing to run on a device too small for
two processes plus Postgres; or needing sub-second display latency (then add
a push channel, but keep the separation).

**Weak spot, now fixed while the service runs.** It used to be that if
Postgres went down, the service retried the current message forever, so the
main loop stopped: no detection and no new alerts until the database was
back. Now detection and alerting run first, from memory, and every database
write goes to a queue with its own thread (`gateway/writer.py`), retried
every 2 s and written in order when the database returns.

- **Who decided.** Proposed by Claude Code; the details approved by you:
  messages are acknowledged to the broker before the database write, and the
  queue is capped at 200,000 jobs (`database.max_queued_writes`, about four
  days of 300 nodes) and past that drops the oldest *readings*, never events,
  notified marks or event-log lines.
- **What it costs.** The queue is in memory: if the service is killed during
  an outage, the writes waiting in it are lost, and because they were
  acknowledged the broker will not resend them. Their alerts already went
  out. A long enough outage loses old readings (logged, counted in the
  "alive" line).

**Two limits that remain, recorded rather than fixed:**

- **The service cannot start without the database.** At start-up it rebuilds
  every node's baseline from the last 12 h of stored readings; without them
  every node would need a fresh 30-reading warm-up (5 h blind). So start-up
  waits until the database answers, and nothing is detected or alerted
  until then. Uplinks meanwhile wait in Mosquitto. Fixing it would mean a
  local snapshot of the baselines, or starting in warm-up and replaying
  later; neither is worth it for a prototype.
- **The console goes blind during an outage while Telegram keeps working.**
  The web app reads only the database, so during an outage it shows
  "console cannot reach the database" (NO DATA), while the service is still
  detecting and the console window and Telegram still alert. An operator
  watching only the web console would see no alarm. When the database is
  back the queued writes land and the console catches up. A reviewer would
  be right to point out that the operator's main screen is the part that
  fails; the answer for now is that Telegram is the channel that must not
  fail, and it does not depend on the database.

---

## 2. MQTT as transport, and mimicking the ChirpStack envelope

**Decision.** The simulator publishes to MQTT (Mosquitto) on ChirpStack v4's
uplink topic, `application/{application_id}/device/{dev_eui}/event/up`,
with ChirpStack v4's JSON event body. The detection service subscribes to
that topic. ChirpStack itself is not installed.

**Who decided.** Spec: "Do NOT install ChirpStack tonight. Instead, the
simulator publishes to the exact topic and JSON envelope ChirpStack v4 uses,
so it can be swapped in later with a config change only."

**Alternatives.**
- HTTP: ChirpStack's HTTP integration POSTs each event to a URL we host.
- ChirpStack's gRPC API, or reading its database.
- Running ChirpStack plus a simulated LoRaWAN gateway (for example the
  ChirpStack simulator) from day one.
- Our own simpler message format, translated later.

**Why this one.**
- MQTT is ChirpStack's main integration, so the production path and the
  prototype path are the same code: `parse_uplink` in `gateway/chirpstack.py`
  does not know whether the sender is the simulator or ChirpStack.
- A broker decouples the processes: the simulator, a real ChirpStack, and
  later other subscribers (a logger, a second gateway service) can come and go.
- Installing ChirpStack means its own Postgres, Redis, a gateway bridge and
  device provisioning. That was not achievable "tonight, with no hardware",
  and it adds nothing to testing detection.
- Mimicking the real envelope, rather than inventing one, means no
  translation layer has to be written and tested later.

**What it costs.**
- We depend on Claude Code's reading of ChirpStack's documentation for the
  envelope's details (UUID `applicationId`, lowercase `devEui`, `time`,
  possibly-missing zero fields, several `rxInfo` entries). `parse_uplink` is
  written defensively for that, but it has never seen a real message.
- ChirpStack must have JSON output enabled (`json = true` in its MQTT
  integration). Otherwise it sends Protobuf and every message is rejected.
- **MQTT used to lose data when the service was down; now fixed.**
  Mosquitto ran with `persistence false` and the service connected with a
  clean session, so uplinks published while it was stopped were gone. Now
  the service connects with a fixed client id (`mqtt.service_client_id`) and
  a persistent session at QoS 1, and Mosquitto keeps the session and its
  queue on disk (`persistence true`, volume `mosquitto-data`, up to 100,000
  messages, sessions forgotten after 7 days). A reading is identified by
  node + frame counter + uplink time (proposed by Claude Code, approved by
  you; it no longer relies on ChirpStack's `deduplicationId`), so a
  redelivery is dropped before detection, and a frame-counter reset after a
  rejoin is not mistaken for one. Costs: only one detection service per
  broker (a second one with the same id takes the session over); a broker
  crash can lose up to the last 60 s of its queue (`autosave_interval`).
  Real ChirpStack also keeps its own copy, but we do not fetch missed ones.
- An anonymous, unencrypted broker. Fine on a laptop only.

**What would change our minds.** A real ChirpStack message that `parse_uplink`
rejects; a deployment where the gateway machine is offline for longer
than the broker's queue covers (then fetching missed uplinks from
ChirpStack's own database, or its HTTP integration with retries, becomes
worth it).

**Weak spot.** "Swappable with a config change" is a claim until it has been
tested once against a real ChirpStack v4 with one real node. A reviewer would
be right to call it unverified. Recording a few real uplinks and adding them
to `tests/test_ingest.py` closes this.

---

## 3. An 11-byte packed payload instead of JSON

**Decision.** Each uplink carries 11 bytes, big-endian: version (1 byte),
temperature (int16, 0.01 °C), humidity (uint16, 0.01 %), pressure (uint16,
hPa × 10 − 5000), gas resistance (uint24, ohms), battery (uint8, 0.02 V). All
of it lives in `gateway/codec.py` and nowhere else.

**Who decided.** Spec, as a provisional format "until the firmware team fixes
the real format". Claude Code added the edge-case handling: pressure below
500 hPa is rejected rather than wrapped, and gas above 16.7 MΩ is clamped and
flagged `gas_saturated`.

**Alternatives.**
- JSON in the payload.
- CayenneLPP, a standard self-describing LoRaWAN format (about 2 bytes of
  type information per value, so roughly 18–20 bytes here).
- Protobuf or CBOR.

**Why this one.** LoRaWAN payloads are tiny and airtime is rationed:

| Payload | Bytes | Airtime at SF12 | Shortest legal gap at 1 % duty cycle |
|---|---|---|---|
| This format | 11 | 1.48 s | 148 s |
| JSON with one-letter keys (`{"t":23.45,"h":45.67,...}`) | 51 | 2.79 s | 279 s |
| JSON with readable keys | 110 | does not fit | — |

(Computed with `lora_airtime_seconds` in `gateway/chirpstack.py`.) At SF12
(EU868 DR0) the maximum application payload is 51 bytes, so readable JSON
does not fit at all, and even minimal JSON nearly doubles the gap between
uplinks. That would push alarm-mode uplinks at slow nodes from about every
2.5 minutes to about every 4.7 minutes. Less airtime also means less battery.

The version byte lets the format change later without guessing.

**What it costs.**
- Not self-describing: the gateway and firmware must agree on the layout, and
  a mismatch silently rejects every reading (see the codebase guide, section 5).
- Fixed precision and ranges, chosen by us.
- Pressure costs 2 bytes and is not used by detection. It is kept for
  diagnosis (weather fronts) and possible later use.

**What would change our minds.** The firmware team choosing CayenneLPP or
another standard so that ChirpStack and other tools can decode it; adding
sensors often enough that a self-describing format pays for its extra bytes.

---

## 4. EWMA rolling baselines instead of fixed thresholds

**Decision.** Per node, per channel, an exponentially weighted moving average
(EWMA) tracks what "normal" is: temperature, humidity, and gas resistance in
log space. Each reading is judged by how far it is from that node's baseline,
never against an absolute number. The weight of each new reading is computed
from the real time since the last one: `alpha = 1 - 0.5 ** (dt / half_life)`.

**Who decided.**
- Spec: EWMA per node and per channel, configurable half-life (default 2 h),
  "NEVER on absolute thresholds. The sensor drifts over weeks."
- Claude Code, with measurements:
  - **Temperature and humidity half-lives of 20 min, gas kept at 2 h.** With
    2 h on every channel, the temperature baseline lags the evening cool-down
    by about 5 °C, which hides a fire's temperature rise: 79 of 120 evening
    and night fires reached ALARM, against 119 of 120 with 20 min. Tried and
    rejected: 10 min (misses only went from 9 to 7 in the rain-front case).
  - **Gas in log space.** Gas resistance varies multiplicatively (a sensor at
    50 kΩ and one at 500 kΩ both fall by a percentage), so a percentage drop
    is the meaningful measure.
  - **Time-based weight**, because uplinks arrive irregularly (heartbeats,
    alarm mode, lost packets).
  - **Baseline frozen during WATCH and ALARM**, so it cannot learn the smoke
    and cancel its own alarm, with a safety valve that resumes updates after
    6 hours.

**Alternatives.**
- Fixed absolute thresholds (for example gas below 50 kΩ, or above 50 °C).
- A rolling mean over a fixed window.
- A z-score: deviation divided by each node's own measured noise.
- CUSUM or other change-point detectors.
- Machine learning trained on data.

**Why this one.**
- Every BME688 has a different gas resistance in clean air, often by a
  factor of several, and it drifts over weeks. One absolute threshold would
  be too sensitive for some nodes and blind for others.
- An EWMA needs constant memory per node (three numbers), handles irregular
  timing naturally, and is easy to explain and test.
- ML needs labelled fire data, which does not exist yet.

**What it costs.**
- **A fire that builds more slowly than the baseline adapts** (over hours) is
  partly absorbed and may never cross a threshold.
- A 5-hour warm-up per new node (see 7).
- Freezing the baseline has its own risk: a permanent sensor shift that
  starts during a WATCH holds the node in WATCH for up to 6 hours.

**What would change our minds.** Field data showing that sensor noise differs
a lot between nodes (then use z-scores); recorded fires (then consider
trained models); slow smouldering fires being a real threat at these sites.

**Weak spot.** The thresholds are percentages and degrees that are the same
for every node, not scaled to each node's noise. A noisy sensor will cross
them more often than a quiet one. A reviewer who knows statistical process
control would ask why we do not use deviation in units of each node's own
standard deviation. It is a fair challenge; the honest answer is that a
fixed-percentage rule was simpler to build and tune with no field data.

---

## 5. Requiring multiple channels to agree before alarming

**Decision.** A reading counts at WATCH or ALARM level only if **gas resistance
has dropped** by the threshold **and** (temperature has risen **or**
humidity has dropped) by its threshold.

| | Gas drop | Temperature rise | Humidity drop |
|---|---|---|---|
| WATCH | 25 % | 3 °C | 8 %RH points |
| ALARM | 45 % | 4 °C | 12 %RH points |

**Who decided.** The rule is from the Spec. The threshold values were first
guesses by Claude Code, then measured; the change of the ALARM temperature
rise from 5 °C to 4 °C was Claude Code's proposal, which you approved ("Yes,
apply 4.0").

**Alternatives.**
- Gas alone.
- Any one channel.
- A weighted score across all channels.
- All three channels must agree.

**Why this one.** False alarms are the failure mode this detector is designed
around: a detector that cries wolf gets switched off. Each channel alone has
common innocent causes:

- Gas resistance falls in rain and fog (humidity), and with other VOCs.
- Temperature rises and humidity falls every sunny morning.

A fire does both at once: smoke lowers gas resistance **and** the heat raises
temperature and dries the air. Requiring both kills the two most common false
alarms. Rain is the clearest example: gas drops, but humidity rises and
temperature falls, so the rule fails, correctly.

Measured: 0 false WATCH or ALARM in 336 simulated node-days, including harsh
weather; 239 of 240 smoke events reached ALARM (median 8 min).

**What it costs.**
- Smoke that arrives **without heat or dryness at the sensor**, for example
  smoke drifting from a fire some distance away, cooled on the way, does not
  count at all.
- Fire starting at the onset of rain or fog can stop at WATCH (1 of 240
  simulated events), because rain masks the temperature and humidity signs.

**What would change our minds.** Field data showing that early smoke at these
sites usually arrives cool; a controlled burn showing a different signature.

**Weak spot.** Because WATCH also needs agreement, a gas-only drop never
raises anything, not even a low-priority note. Early warning from distant
smoke is arguably the most valuable warning, and a reviewer would be right
to ask why a strong gas-only drop does not at least raise WATCH. Reasonable
answer: it was a deliberate trade for fewer false alarms, untested against
real smoke, and a gas-only WATCH level is the first thing to try with field
data.

---

## 6. Hysteresis and the alarm cooldown

**Decision.**
- States go WARMUP → NORMAL ⇄ WATCH ⇄ ALARM, **one level at a time**.
- Going up needs **3 consecutive readings** at the higher level (NORMAL →
  WATCH, then WATCH → ALARM). Going down one level needs **5**.
- **Notification cooldown, 30 minutes per node:** after an ALARM message, no
  new ALARM message for that node for 30 min; after a WATCH or ALARM message,
  no new WATCH message. Every state change is still recorded. "All clear" is
  sent only if a WATCH or ALARM message went out for that episode.

**Who decided.** Hysteresis and a cooldown: Spec ("state must hold for N
consecutive readings" and "a cooldown so one event doesn't spam alarms").
The numbers: Claude Code. WATCH confirmation went from 2 to 3 because, with 2,
back-to-back gas glitches occasionally caused a false WATCH.

**Alternatives.**
- Alarm on the first reading over the threshold.
- Time-based confirmation ("above for 5 minutes") instead of counts.
- Separate on and off thresholds (a lower threshold to leave ALARM).
- No cooldown; or a global cooldown instead of per node.

**Why this one.**
- Single-reading spikes happen (the simulator models rare gas glitches; real
  sensors have them too). Requiring three in a row removes them.
- One level at a time means ALARM is always preceded by a WATCH alert, so
  operators get an early warning.
- Needing more readings to step down than to step up keeps a node from
  flickering in and out of ALARM at a threshold.
- Per node, not global: a fire spreading to a second node still notifies.

**What it costs.**
- **Delay.** At least 6 readings from NORMAL to ALARM. Measured from smoke
  start: a median of 8 min on fast nodes (an uplink every minute), 13 min at
  SF11 (every 2 min), and 18 min at SF12 (every 3 min).
- **Slow all-clear.** After a node returns to heartbeats, 5 readings take up
  to ~50 minutes.
- The cooldown could suppress a genuine second ALARM on the same node within
  30 min (for example a flare-up). It is still recorded and still shown on
  the console; only the message is held back.
- Counting readings means the delay depends on the uplink rate, which is set
  by the firmware and the radio, not by us.

**What would change our minds.** Real glitch rates (if sensors are cleaner
than modelled, 2 readings may be enough); operators reporting that the
cooldown hid something they needed.

---

## 7. The warm-up period and its length

**Decision.** A new node is not evaluated until its baseline has **30
readings**: about 5 hours at a 10-minute heartbeat. A restart of the
detection service does not repeat it, because the service rebuilds every
baseline from the last 12 hours of stored readings.

**Who decided.**
- That a warm-up exists: Spec.
- 30 readings: Claude Code's initial value (when nodes reported every minute,
  30 minutes).
- **Keeping 30 when the heartbeat became 10 minutes: your call.** Claude Code
  proposed cutting it to 12 (2 hours) to make warm-up shorter. You refused:
  "Warm-up length is what makes the baseline trustworthy... false alarms are
  the failure mode this whole detector is designed around. 5 hours of warm-up
  on a node that will sit in a forest for months is not a real cost. Use the
  speed-up multiplier for demos instead." You asked for numbers if 30 turned
  out wrong.
- The 12-hour restart replay: Claude Code.

**Alternatives.** 12 readings; time-based warm-up ("2 hours"); starting
from a fleet-wide default baseline; no warm-up.

**Why this one.**
- Your argument above: a node lives for months; 5 blind hours once is cheap,
  and a noisy baseline raises the false-alarm rate for its whole life.
- 30 readings at 10 min span 5 hours, more than two half-lives of the 2-hour
  gas baseline, so the gas baseline has mostly settled.
- Measured: no run produced a false WATCH right after warm-up.
- Demos use `DEMO_SPEEDUP`, which shortens the time, not the count.

**What it costs.**
- A newly installed node, or one silent for more than 12 hours, gives no
  fire detection for about 5 hours.
- The console must say so honestly: the WARMING UP pill exists so it never
  shows ALL CLEAR before anything is being monitored.

**What would change our minds.** Measured false-alarm rates for shorter
warm-ups (this was never run); field installation practice where 5 blind
hours after a battery swap matters.

**Weak spot, two of them.**
- **The evidence is one-sided.** We measured that 30 works. We never measured
  12, so "12 is noisier" is a sound argument but not a number. If a reviewer
  asks "how do you know 30 and not 15?", the honest answer is that 30 is
  deliberately conservative. Running the 336 node-day test at 12 and 20
  would settle it.
- **30 samples do not cover sensor burn-in.** A new BME688 needs roughly 24–48
  hours before its gas readings stabilise. The warm-up does not cover that, so
  a newly installed node could drift through its first day. This argues for a
  *longer* first warm-up, not a shorter one.

---

## 8. Heartbeat plus alarm-mode intervals, against duty cycle limits

**Decision.**
- **Heartbeat:** every node sends one uplink every 10 minutes.
- **Alarm mode:** the node reads its sensor every 60 s (`sample_seconds`, the
  rate in the team's power study) and keeps its own slow gas baseline. When
  gas is 15 % below it on 2 reads in a row, the node transmits at once, then
  every minute (every read) while the trigger holds
  (`interval_while_triggered_seconds`) and until 10 minutes after it last
  held (`interval_after_trigger_minutes`).
- **Duty cycle:** the node never transmits sooner than EU868's 1 % duty cycle
  allows at its spreading factor (6 s at SF7, 82 s at SF11, 148 s at SF12).
- **fPort says which mode:** heartbeats on fPort 2, alarm-mode uplinks on
  fPort 3. The console's RAPID UPDATES badge reads the port, not the timing.
- **Missing:** a node is MISSING / OFFLINE after 3 missed heartbeats (30
  min), one value used by both the detector and the console.

**Who decided.**
- A 10-minute heartbeat plus event-driven transmission on a local
  threshold, and "missing after N missed heartbeats": Spec.
- A sustained alarm-mode interval rather than one immediate uplink, the
  duty-cycle enforcement per spreading factor, deriving missing from the
  heartbeat once: Claude Code's proposals, which you approved.
- The local trigger values (15 %, 2 reads): Claude Code, explicitly a
  **placeholder** for the firmware team.
- **The 60 s sensor read: the team's power study** ("Power Consumption
  Analysis & Architecture", VIP402), adopted by your call on 8 Nov. The
  study wakes the node every 60 s for 5 s and gets about 3.1 years from the
  battery. Until then the simulator read every 30 s, a Claude Code
  placeholder; by the study's own figures that would be about 1.6 years.
- fPort signalling: Claude Code, after the "TX FAST" bug, which you asked to
  have fixed at the root ("Do all three").
- History of the alarm-mode rate. On 4 Nov you chose 30 s while triggered
  (**your call**). **That rate was not designed; it was found.** Until then it was an undocumented side effect of the code:
  every read where the trigger held forced an uplink, so the configured
  "1 minute" only applied after the trigger cleared, and the README, this
  document and `config.yaml` all said "about every minute". Every
  time-to-ALARM figure we had quoted for fast nodes was measured with the
  30 s behaviour. Claude Code found it while measuring per-node timings; you
  chose to keep the faster rate and make it deliberate: it is now its own
  setting, `simulator.firmware.interval_while_triggered_seconds`, next to
  `interval_after_trigger_minutes` for the slower tail. On 8 Nov the power
  study set the sensor read to 60 s, and a node can only send on a read, so
  both alarm-mode rates are now 60 s (your call, over keeping 30 s reads at
  half the battery life). The setting stays, so a faster rate can be
  restored if the firmware ever reads faster while triggered.

**Alternatives.**
- Fixed fast reporting (every minute): what the first prototype did. At SF11
  and SF12 it breaks the duty-cycle rules, and it costs ten times the battery.
- Heartbeat only, slower or faster.
- One immediate uplink on a breach, then back to heartbeats (the literal
  reading of the spec).
- Detecting fire on the node itself and sending only alarms.
- Confirmed uplinks (the network acknowledges each one).
- Guessing the mode from the time between uplinks (what the console first did).

**Why this one.**
- **Heartbeat alone cannot detect fire.** Measured: with 10-minute heartbeats
  and no event-driven mode, 0 of 240 smoke events reached ALARM. A 25-minute
  smoke event gives only 2–3 readings, fewer than the 6 that WATCH plus ALARM
  confirmation need.
- **One immediate uplink is not enough either**, for the same reason: it
  would be followed by heartbeats, and ALARM would come about an hour late.
  Hence a sustained fast rate.
- **60 s reads, as the power study sets.** The trade, measured over 60
  injections per node (8 Nov):

  | Sensor read | Smoke to ALARM, fast nodes | SF11 | SF12 | Battery (study's figures) |
  |---|---|---|---|---|
  | 60 s (chosen) | median **8 min** | 13 min | 18 min | about 3.1 years |
  | 30 s (before) | median **5.5 min** | 11 min | 17.5 min | about 1.6 years |

  Almost all of the node's power goes into the 5 s it is awake for each
  read, so halving the read interval nearly halves battery life. 2.5
  minutes of detection was judged not worth half the battery.
  - Every minute is **legal at SF7–SF10**, whose shortest duty-cycle gaps are
    6–37 s. At SF11 and SF12 the node falls back to its shortest legal gap,
    rounded up to the next sensor read: 2 min and 3 min.
  - A possible middle way, not built or measured: read every 60 s normally
    and every 30 s only while the trigger holds. Alarm mode fires 0.05–0.6
    times a day in normal weather, so the battery cost would be small, and
    time to ALARM would land somewhere between 5.5 and 8 min. It needs
    firmware support and a change to the simulated firmware.
- **The node's trigger is more sensitive than the gateway's WATCH (15 % vs
  25 %)**, so the node speeds up before the gateway needs the data. The node
  only decides *how often to talk*; the gateway decides *whether there is a
  fire*. Keeping detection in one place, with full history and configuration,
  is easier to tune than firmware in a forest.
- **Duty cycle is law, not a preference** (EU868 sub-band 868.0–868.6 MHz, 1 %).
  A simulator that ignored it would promise alarm times real nodes cannot reach.
- **The port is a fact the node states;** timing is a guess that broke as soon
  as two processes ran with different `DEMO_SPEEDUP` values.
- Battery: about 144 uplinks per node per day (146 in harsh weather; alarm
  mode fires 0.05–0.6 times a day in normal weather). **The power study does
  not include these uplinks yet** (its only case is "without a single LoRa
  transmission"). A rough estimate from its own figures, 87 mA for the time
  on air plus about 2 s awake per uplink, takes 3.1 years to about 3.0 at
  SF7 and about 2.6 at SF12. The study should add that case.

**What it costs.**
- **Detection depends on firmware that does not exist yet.** If the real
  firmware does not implement the local trigger and the fPorts, detection does
  not work, and nothing in the gateway can compensate.
- Slow nodes are slow: SF12 median 18 minutes to ALARM.
- 2.5 minutes slower than with 30 s reads, on every node near a gateway.
- A node that burns before its trigger fires is only noticed as OFFLINE, 30
  minutes later.

**What would change our minds.** The firmware team's real constraints (battery
budget, sensor read time; a BME688 gas reading needs its heater, which costs
power on every read); measured trigger rates in the field; spreading factors at
the actual sites (the fix for slow nodes is gateway placement, not thresholds).

**Weak spot.** Every timing number here is a placeholder agreed with no one
on the firmware side. A reviewer would be right to say that the gateway's
alarm performance is promised on behalf of firmware that has not been
designed. This needs a written interface agreement (payload, ports, trigger
behaviour) with the firmware team.

---

## 9. Treating a gas resistance DROP as the VOC signal

**Decision.** Smoke is detected as gas resistance **falling** below baseline.
A rise is never treated as smoke. The console calls a drop "VOC up".

**Who decided.** Spec: "BME688 gas resistance FALLS when VOC concentration
RISES. Smoke makes resistance drop, not climb. Get this sign right."

**Alternatives.** The BME688's own gas index from Bosch's BSEC library (an
"IAQ" or VOC-equivalent estimate); a gas-scanning profile with several heater
temperatures (the BME688's AI-Studio approach, which can tell gas types apart).

**Why this one.**
- It is how the sensor works: the BME688 has a heated metal-oxide layer whose
  resistance falls when it reacts with reducing gases, which smoke contains.
- Raw resistance needs no proprietary library on the node and can be
  compared with a per-node baseline on the gateway.
- `tests/test_detection.py` checks the sign both ways, so it cannot be flipped
  by accident.

**What it costs.** Gas resistance falls for many reasons that are not fire:

- **humidity** (handled by the agreement rule: humidity rises in rain);
- **other VOCs**: vehicle exhaust, solvents, and **terpenes that vegetation
  emits on hot days**;
- heater temperature and supply voltage changes on the node;
- sensor ageing (absorbed by the baseline).

**What would change our minds.** Bosch's gas-scanning profiles proving able to
separate wood smoke from terpenes; field data.

**Weak spot.** The hot-day terpene case is the one a reviewer who knows forests
will raise: on a hot, dry afternoon, temperature is up, humidity is down, and
pine terpenes lower gas resistance. That is all three signs of a fire with no
fire. The 20-minute temperature and humidity baselines absorb the slow daily
rise, which helps, but **the simulator does not model terpenes at all**, so
the 0 false alarms in 336 node-days says nothing about this case. Say so
plainly if asked; it is listed in the README's known limitations.

---

## 10. Zoomed out: only gateways and the centre, but nodes in ALARM always shown

**Decision.**
- Below zoom 17 (`console.map.node_detail_zoom`), which includes the default
  view (zoom 15), the map shows the gateways, the civil defence centre, and
  **every node in ALARM as its own red marker**. Nothing else.
- From zoom 17 in, every node is shown, coloured by state, with nearby
  markers grouped into a count badge that takes the worst state inside it.
- The SENSOR NETWORK panel always counts every node in the database,
  whatever the map shows.

**Who decided.** Spec, refined by your own change requests. The first version
(per-block aggregate markers when zoomed out) was your request of 2 Nov; you
then asked for the clean view: "Zoomed out, the map shows exactly two things:
gateway markers, the civil defence centre marker... THE ONE EXCEPTION: if any
node is in ALARM, that node appears as a red marker at EVERY zoom level."
Implementation details (alarm markers never clustered, kept on screen when an
alarmed node goes silent, cluster badges taking the worst state) were Claude
Code's.

**Alternatives.**
- All node markers at every zoom (fine for 4 nodes, unreadable for hundreds).
- Clusters at every zoom.
- Per-block aggregates (shaded block areas with counts and worst state): the
  version built first, then removed at your request.
- A heat map.

**Why this one.**
- The real deployment is hundreds of nodes; hundreds of markers is noise, and
  noise hides the one thing that matters.
- The map's job zoomed out is orientation and "is anything burning". An
  almost empty map makes a red marker impossible to miss.
- An alarm must never be hidden by the zoom level, so ALARM markers are a
  separate layer that is never clustered or hidden.
- Status that matters is not only on the map: the pill, the sensor network
  table and the event log show every state at all times.

**What it costs.**
- **Nodes on WATCH are invisible on the zoomed-out map.** The pill turns amber
  and the table lists them, but the map gives no location until you zoom in.
- **MISSING nodes are invisible zoomed out too.** A node destroyed by fire
  before it reached ALARM goes silent and shows nothing on the default view.
- No overview of network health on the map (where coverage is thin, which
  block has gaps).

**What would change our minds.** Operators in testing missing a WATCH because
they did not look away from the map.

**Weak spot.** A reviewer could reasonably argue that WATCH is exactly the
early warning an operator should see on the map, and that a cluster of nodes
going MISSING in one area is itself a fire sign. Showing WATCH as an amber
marker zoomed out would cost little clutter, because WATCH is rare. This was
your explicit choice; be ready to defend "clean map, amber is in the pill and
the table" or to concede it.

---

## 11. Thermal camera, drone and wind station marked NOT CONNECTED

**Decision.** The three panels are drawn with their real layout, greyed, with
"NOT CONNECTED - PLANNED PHASE 2" and no numbers. Each has a feature flag in
`config.yaml` (`console.features`); turning one on changes the label to
"ENABLED - NO DATA SOURCE CONFIGURED", because no integration code exists yet.

**Who decided.** Spec: "They do not exist yet. Do NOT fake live data for them."

**Alternatives.** Simulated live numbers; leaving the panels out until
phase 2; a static mock-up image.

**Why this one.**
- An operator console must never show data that is not real. A plausible fake
  wind speed in a fire console is worse than none: someone may act on it, or
  later stop trusting the real numbers.
- A design review of a prototype should see what is built and what is not.
  Faked panels make a demo look more finished than the system is.
- Keeping the layout reserves the screen space, so phase 2 does not need a
  redesign, and reviewers can see the intended whole.

**What it costs.** A third of the console is grey in demos. Phase 2 layouts
are guesses until the hardware is chosen.

**What would change our minds.** Nothing short of real hardware. If a demo
needs to show the phase 2 idea, use a clearly labelled mock-up slide, not the
operational console.

This one is easy to defend.

---

## 12. Smaller decisions you may be asked about

| Decision | Who | Why | Cost / weak spot |
|---|---|---|---|
| **NO DATA pill** when no node has reported within the missing threshold | Claude Code | A green ALL CLEAR while the gateway, broker or service is down would be the worst possible failure. | None worth noting. |
| **WARMING UP pill** while every reporting node is still in warm-up | Claude Code (found during the browser check) | ALL CLEAR would claim monitoring that is not happening yet. | None. |
| **Replay the last 12 h on restart** | Claude Code | A restart should not mean 5 more blind hours. Replayed transitions are not re-alerted. | An outage over 12 h still means a fresh warm-up. |
| **OFFLINE alert after 3 missed heartbeats**, threshold derived once from the heartbeat | Claude Code (OFFLINE), Spec (missed heartbeats) | A burning node goes quiet; silence must alert. One derived value means the detector and console cannot disagree. | 30 minutes is slow for a node that burned before reaching ALARM. |
| **Alarm zone drawn as "sensor coverage"**, a hull of 150 m circles around nodes in ALARM | Claude Code, you approved the labelling | Sensors are points; drawing a "fire area" would claim knowledge we do not have. | The 150 m radius is a guess. |
| **No suppression-request button** | Claude Code recommended, your call | It would have needed the web app to write, breaking the read-only rule, and it looks operational when it is not. | None in a prototype. |
| **Postgres, with SQLite as a no-Docker fallback** | Spec (Postgres), Claude Code (fallback) | Docker was not installed on the first night. | Tests run on SQLite by default; Postgres-only behaviour needs `TEST_DATABASE_URL`. |
| **Polling every 5 s** instead of WebSockets | Spec ("Auto-refresh by polling") | Simple, robust, no build step. | Up to 5 s display delay; load grows with open consoles. |
| **Libraries saved in the repo** (Leaflet, markercluster) | Claude Code (Spec for the clustering plugin) | Works without a CDN at a remote site. Checked against published hashes. | Updates are manual. Map imagery still needs internet. |
| **Site labels instead of devEUIs** in the operator UI | Spec | Operators think in places, not hex. | Placeholder positions must be replaced by a site survey before any deployment. |

---

## The challenge that applies to everything

Every threshold, half-life, confirmation count and the 0-false-alarm result
was tuned and measured **against a simulator built in the same project**. The
smoke model (gas falling 40–70 %, temperature up a few degrees, humidity
down) is an assumption, not a measurement. So is the weather model.

A reviewer who says "your detector has only been shown to detect your own
model of a fire" is right. The defensible position is:

1. The **structure** (relative baselines, channel agreement, hysteresis, a
   node-side trigger with gateway-side detection) is sound on general grounds
   and does not depend on the exact numbers.
2. The **numbers** are starting points, all in `config.yaml`, each with a
   comment saying where it came from.
3. What would validate them, in order of value: a controlled burn near one or
   two real nodes; weeks of real data from nodes in the forest with no fire
   (the false-alarm side, including hot days and terpenes); recorded real
   ChirpStack uplinks.

Until then, the honest claim is "a working, testable pipeline with
plausible, documented defaults", not "a validated fire detector".
