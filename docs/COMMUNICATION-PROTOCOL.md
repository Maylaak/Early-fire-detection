# Explicit node communication protocol (version 2)

## Scope and wire format

Legacy version 1 remains the same 11-byte environmental payload; encode/decode are unchanged. New decode_message dispatches by version. Version 2 is exactly two bytes: `02 01` = HEARTBEAT, `02 02` = FIRE_ALERT. Other kinds, lengths and trailing bytes are rejected. No sensor values are implied or synthesized. This minimal version has no optional sensor metadata; event metadata comes from the envelope (node, time, frame counter, port and receipt time).

Hardware must send these bytes as the application payload in a ChirpStack v4 JSON uplink, with a matching topic/devEUI, valid application port, nonnegative frame counter and stable uplink timestamp across redelivery. The discriminator is the payload, not fPort or transmission frequency. A new frame counter with a new timestamp is a new report. The existing provisional version-1 payload and simulator remain supported. The new frame meanings must be agreed with the firmware team before physical deployment.

The intended hardware schedule is one heartbeat per day, local sensor monitoring, and prompt explicit alerts after local fire detection. This task implements reception, not that simulator schedule or the hardware detection algorithm. Normal samples are not needed for version-2 messages. Legacy server detection still needs samples for baseline warm-up, multichannel evidence, hysteresis and replay.

## State, ordering and notification policy

Heartbeat updates contact, online status and latest message type without creating readings or alarms. A heartbeat-only new node is NORMAL (connectivity known, no local alarm reported); this is not evidence of gateway environmental evaluation. Legacy samples still enter normal baseline warm-up.

A valid unique FIRE_ALERT immediately latches ALARM without waiting for a baseline. The first explicit alert uses the existing Dispatcher, notifier formatting, console log and block tracking. Subsequent unique alerts while latched are recorded but do not repeatedly notify. Exact duplicates are suppressed, including after normal restart. Late unique fire alerts are recorded and latch if necessary; they never regress last-contact time. Stale heartbeats do not advance contact. Legacy measurements cannot clear an explicit latch.

Detection stays independent of database/network I/O. Control messages go through a nondroppable Writer job. Receipt, node state and fire event are written in one transaction. EventRef links the dispatcher result to the persisted event, as with legacy alerts. The ingestion convenience method handle remains measurement-only and rejects control messages with an explanatory error; service integrations must use accept followed by Pipeline.on_uplink.

No FIRE_CLEAR or acknowledgement endpoint is added. A future protocol must distinguish operator acknowledgement from actual fire clearance, authorize the operation, identify the alarm episode, reject stale/replayed clears, persist an audit trail, and define restart behavior. Silence, heartbeat, elapsed time and normal legacy samples are not clears.

## Persistence and compatibility

Schema changes are additive: nullable nodes.last_message_type and nodes.explicit_fire_active, plus control_uplinks containing the unique node/counter/time key, event/receipt timestamps, type, counter, port and optional event ID. Existing readings/events are preserved. Start detection before web after upgrading so init_schema applies the additions; do not recreate volumes or delete history.

The receipt key uses the existing uplink_key convention. Control keys reload at startup independently of the environmental replay window; the alarm latch restores from nodes even if its alert predates that window. Receipt keys currently remain in storage and memory without retention pruning. A bounded cache plus durable indexed lookup is a future scaling consideration.

Existing queue/transport durability limits remain: MQTT acknowledgement precedes durable storage; process death during a database outage can lose queued work. This is not exactly-once notification delivery or a durable outbox. One detection service per broker remains required.

## Read-only API additions

Node status adds last_contact (alias of last_seen), last_message_type (MEASUREMENT for old rows), and explicit_fire_active. Existing online/missing and state fields remain. Console history reads latest environmental samples independently of the contact timestamp; heartbeats do not invent chart samples. Explicit fire events appear in /api/events even when emitted directly from WARMUP. The web app remains read-only.

In detection configuration, explicit_heartbeat_minutes defaults to 1440 and explicit_missed_heartbeats to 3. The derived explicit_missing_minutes drives version-2 node connectivity: three missed daily contacts, not the legacy short demo timeout. These fields are intentionally not demo-scaled yet. Legacy measurement timing and all simulator scheduling are unchanged. Gateway online status still uses the legacy gateway heuristic; adapting gateway liveness and full demo clock/scheduler behavior belongs with the next scheduling task.

## Verification

Deterministic tests in tests/test_control_messages.py use temporary SQLite and mock notification delivery. They cover malformed payloads, deduplication, immediate alerts without baseline, persistence, heartbeat-only nodes, stale messages, latched state across heartbeat/restart/legacy traffic, read-only API exposure and notified bookkeeping. Existing codec, detection, ingestion, service, notification and web regressions are also run with external integration settings disabled.

## Task 3 timing update
See EVENT-DRIVEN-SIMULATOR.md: explicit heartbeat/missing durations now scale with DEMO_SPEEDUP, event-driven scheduling is enabled, and gateway connectivity distinguishes message types. This supersedes the Task 2 timing and simulator deferrals above.
