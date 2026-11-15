# Repository instructions for Codex

## Scope and discovery

This directory is the project root: Python gateway, plain JavaScript console, PostgreSQL/Mosquitto infrastructure. Read `PROJECT_CONTEXT.md`, `AI_HANDOFF.md`, `CLAUDE.md` and relevant implementation/tests before coding. Confirm current files rather than assuming the context snapshot remains current. The inspected source copy lacked Git metadata; do not initialize Git or add remotes to compensate.

## Change discipline

- Inspect and reuse established functions, services, configuration models and UI helpers before adding anything. Avoid duplicate logic.
- Preserve existing behavior unless the task explicitly changes it. Keep changes focused; avoid broad refactoring and unnecessary dependencies.
- Preserve teammates' uncommitted work. In a Git clone inspect status/diffs before editing; never revert unrelated changes.
- Never commit, push, merge or rewrite history unless explicitly instructed. Also respect `CLAUDE.md`: no pushing, remote edits or gh CLI use. Do not fetch, pull or switch branches as part of documentation/discovery work.
- Never introduce secrets into source control, logs or generated documentation. Use environment variables; do not reproduce connection credentials or Telegram token-bearing URLs.

## Architecture rules

- Keep `gateway/detect.py` pure: readings/config/timestamps in, evaluation/events out. MQTT, persistence and notification coordination belong in service/Pipeline and their adapters.
- Keep the web process read-only. Schema initialization and writes belong to the detection service. Preserve PostgreSQL role permissions and SQLite read-only behavior.
- Reuse `Store` transactions, `Writer` jobs and `Dispatcher` instead of performing blocking I/O in detection. Preserve queue ordering, replay and notified bookkeeping.
- Preserve devEUI/frame-counter/time deduplication, UTC-aware timestamps, persisted schemas and existing API contracts. Plan migrations explicitly; nullable-column additions are the current limited migration mechanism. Do not erase databases as an upgrade shortcut.
- Maintain one detection service per persistent MQTT client ID. Do not append random IDs to conceal duplicate-instance faults.
- Add config fields through strict models in `gateway/config.py` and corresponding YAML/tests. Keep derived missing thresholds centralized and speed-up consistent; never scale sample counts accidentally.
- Follow Python type hints, snake_case functions/modules, PascalCase records/classes and existing exception/logging patterns.
- Frontend work uses existing HTML/CSS/JavaScript, `common.js` helpers and SVG charts. Match existing styling and naming; do not introduce a framework without a task requirement.
- Preserve HTML escaping, operator site labels, alarm visibility at every zoom, truthful missing/warm-up states, and fPort-based rapid-update indication. Coverage circles are not fire extent.
- Do not present SMS or phase-2 panels as operational integrations. Extend adapters and tests when implementing them.

## Verification and reporting

Use appropriate tests under `tests/` for changed behavior. Pure logic tests must not require real services. Notification tests should mock transport. Web changes should verify API contracts and relevant UI states. Documentation-only changes need reference/content checks rather than unrelated runtime launches.

Before tests inspect integration environment settings without exposing their values. TEST_DATABASE_URL points to a database whose tables tests drop; require a disposable target. TEST_MQTT_HOST/TEST_MQTT_PORT are for a scratch broker. Never run destructive integration checks on the working demo database.

Use the existing virtual environment where viable; do not upgrade dependencies or change configuration without task need. Report changed files, verification performed and results, checks not run, and unresolved limitations. Never claim runtime success from static inspection alone.
