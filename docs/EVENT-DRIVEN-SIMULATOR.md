# Event-driven simulation

The shipped config selects simulator.reporting_mode: event_driven. Set it to legacy to retain the earlier measurement/rapid-reporting simulation. The binary version-1 backend path remains supported in either case. Event-driven mode rejects --interval; use DEMO_SPEEDUP consistently in all processes instead.

## Sensing and fire detection

Each node samples internally every simulator.firmware.sample_seconds (60 simulated seconds by default). EventNodeFirmware calls the existing pure NodeDetector with these readings. It preserves the 30-sample warm-up, relative multichannel thresholds, EWMA baselines, consecutive confirmations and state hysteresis. Normal and WATCH readings are local only. Entering ALARM queues a two-byte version-2 FIRE_ALERT. The first sample allowed by the modeled radio duty cycle transmits it. A pending alarm is not discarded if the physical signal subsequently subsides. Another alert episode is permitted only after the local detector returns fully to NORMAL and later reaches ALARM again. The backend explicit alarm remains latched; local recovery does not clear it.

This is a simulated firmware model, not embedded hardware firmware. Sensor-model weather/day-night behavior retains the existing wall-clock convention; it is not a fully accelerated physical weather simulation.

## Heartbeats, time and restart

First use of a node in a fresh simulator state database schedules an immediate HEARTBEAT. Subsequent deadlines advance in exact intervals of detection.explicit_heartbeat_minutes (1440 simulated minutes by default), independently of fire alerts. A delayed tick sends at most one overdue heartbeat and skips missed intervals; it never creates a catch-up burst. An alert due at the same time has priority; the heartbeat waits for the legal radio gap.

DEMO_SPEEDUP divides local sample intervals, daily heartbeat, offline interval, smoke duration and detector duration parameters; sample/confirmation counts and thresholds do not change. At x60: sensing every second, daily heartbeat every 24 wall-clock minutes, three-missed-heartbeat timeout after 72 minutes. Warm-up is approximately 30 seconds before injection. Duty-cycle timing uses the actual two-byte control payload and existing airtime model, scaled for demonstrations.

simulator.state_path defaults to data/simulator-state.sqlite. It is a simulator checkpoint database, separate from the gateway database and under the ignored data directory. Per-node state includes next deadline, frame counter, last sensing/transmission time, detector state and pending/episode flags. SQLite serializes checkpoint updates. Duplicate or backward sensor ticks are ignored. Changing the heartbeat duration/demo speed starts a future interval at the new rate, without an extra boot heartbeat. Preserve this file across restarts; deleting it deliberately resets simulated devices. Model noise/drift and ongoing smoke injection are not checkpointed; those physical-model inputs restart, while detector/scheduler state persists.

Transmissions are atomically saved to an outbox with the checkpoint. The live loop publishes with QoS 1 and removes an entry only after broker acknowledgement. It does not block sensing while waiting. The MQTT client handles in-process retransmission; after restart the same payload, timestamp and frame are resent, not allocated anew. The gateway's key-based deduplication handles this at-least-once delivery. Broker ACK is not a guarantee of gateway database durability. No periodic application-level repeated-fire stream is added. Run one simulator; SQLite serialization is defensive, not a multi-simulator deployment design.

Dry-run uses in-memory state and does not consume or modify the live simulator checkpoints. Frame counters advance for scheduled transmissions, not every local sample. These model counters do not implement all LoRaWAN session/security lifecycle rules.

## Connectivity

Version-2 node connectivity uses explicit_heartbeat_minutes times explicit_missed_heartbeats (default three daily intervals), now demo-scaled. A fresh heartbeat or fire alert proves contact. Never-seen registry entries remain unconfirmed and outside observed online counts. Missing/offline does not clear an alarm. Gateway observations now store the last message type so explicit-protocol gateways use the daily-derived timeout while legacy gateway traffic retains its original timeout. The schema change is one nullable gateway column; start detection before web to apply it. No historical data is deleted.

## Demo

Stop the old simulator/service/web processes before restarting once with the same DEMO_SPEEDUP=60. Existing start_all.bat sets this value. Allow at least 30 seconds of local sensing (longer on a slow machine), then in an activated project CMD run:

    set DEMO_SPEEDUP=60
    python -m gateway.sim inject node-3

The simulator logs FIRE_ALERT after local confirmation and duty-cycle clearance; the backend records and notifies without waiting for the daily heartbeat or server baseline. If a prior explicit alarm is already latched, new reports remain recorded but do not re-notify the same active alarm. There is intentionally no clear API yet. Normal environmental charts no longer receive routine samples in this mode.

Physical firmware still needs sensor acquisition/calibration, local state persistence, the agreed versioned payload, proper LoRaWAN counters/keys/session handling, duty-cycle enforcement, reliable delivery policy, and a separately specified authorized fire-clear/acknowledgement workflow.
