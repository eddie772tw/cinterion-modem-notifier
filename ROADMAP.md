# Cinterion Modem Notifier Roadmap

This roadmap prioritizes long-running reliability and bounded SMS notification latency over boot-time optimization. The current service is intentionally read-only with respect to modem control; operation-changing actions remain separate future work.

## Current baseline

- ModemManager 1.23.4 with the Cinterion plugin.
- User systemd service starts during boot because `loginctl` lingering is enabled.
- ModemManager signal watcher is retained, but live SMS signals have not been observed through the current `gdbus` monitor path.
- A 5-second SMS/call reconciliation pass is the reliable notification path.
- Known SMS paths are indexed so reconciliation does not re-read old SMS bodies.
- Recent measured SMS notification latency: about 4-7 seconds.
- Full scan cost at 36 stored SMS: about 7.35 seconds; incremental reconciliation: about 0.17-0.19 seconds.

## Priority order

1. Preserve notification correctness and recovery.
2. Keep steady-state latency bounded as SMS storage grows.
3. Improve observability without leaking SMS bodies, phone numbers, webhook URLs, or location.
4. Prove or replace the native D-Bus event path.
5. Add controlled low-risk modem capabilities behind explicit boundaries.
6. Only introduce PostgreSQL or a larger broker when deployment scale justifies it.

## Phase 1 — Reliability and measurement

### Goals

- Make notification lag measurable rather than inferred from logs.
- Detect modem reset, ModemManager restart, watcher exit, and webhook failure separately.
- Keep the service self-healing without changing modem state.

### Work

- Add structured local metrics: `sms_storage_count`, `reconciliation_seconds`, `notification_lag_seconds`, `webhook_failures`, `watcher_restarts`, `full_scan_seconds`. (Initial scan/reconciliation metrics and read-only health/metrics queries are implemented.)
- Store transport/observation timestamps separately from modem-provided SMS timestamps.
- Add bounded retry/backoff for Discord delivery without duplicate notifications.
- Add a health snapshot query for service, watcher, ModemManager, modem registration, and bearer state.
- Add retention limits for SQLite event history; keep SMS body retention explicit and private.

### Acceptance

- A test fixture proves lag calculation and does not treat modem timestamps as local arrival time.
- A webhook failure is visible locally but does not crash the modem monitor.
- Event history remains bounded after repeated SMS/call/status events.
- No secrets or full SMS bodies appear in normal operational logs.

## Phase 2 — Scalable SMS ingestion

### Goals

- Avoid making notification latency proportional to the total number of stored SMS.
- Preserve correctness when messages are deleted, paths are reused, or ModemManager restarts.

### Work

- Keep the path index bounded to currently listed objects; retain stable identity hashes for deduplication.
- Keep a read-only benchmark for path-list, incremental, and full-scan timings. (Implemented as `scripts/benchmark_sms.py`.)
- Add a periodic full consistency scan at a much lower frequency than reconciliation.
- Measure path-list latency at representative storage sizes before changing intervals.
- Add tests for path deletion, path reuse, storage migration, duplicate multipart messages, and malformed SMS objects.
- Do not use PostgreSQL to optimize the ModemManager list call; the bottleneck is upstream of the database.

### Acceptance

- Normal reconciliation performs no per-message `mmcli -s` call for known paths.
- A deleted and reused path cannot silently suppress a new SMS after the next consistency scan.
- The service remains within a declared latency budget at the tested SMS count.

## Phase 3 — Native event subscription investigation

### Goals

- Determine whether `Messaging.Added` can be consumed reliably under the current system-bus policy and Cinterion plugin.

### Work

- Test a native D-Bus subscription using an approved system binding or GLib/GIO interface.
- Capture signal object paths and members with a local, bounded diagnostic fixture.
- Check system-bus policy and ModemManager authorization without weakening global permissions.
- Keep reconciliation as a fallback until live signal delivery is proven across modem restart and SMS arrival.

### Acceptance

- A real incoming SMS produces an observed `Messaging.Added` or equivalent signal with a verified path.
- Signal-to-notification latency is measured across repeated tests.
- Signal loss, watcher restart, and ModemManager restart recover without manual intervention.
- If native subscription cannot be made reliable, document that conclusion and keep the hybrid design.

## Phase 4 — Read-only modem depth

### Goals

- Expose useful 4G diagnostics without modem side effects.

### Work

- Add structured signal metrics, serving-cell data where authorized, bearer/IP/route/DNS state, modem time, and location capability/status.
- Separate modem registration from host internet reachability.
- Add a local read-only broker or CLI contract shared by Hermes and Codex.
- Keep location values and SMS contents private by default.

### Acceptance

- Every field has a source, unit, authorization behavior, and unavailable representation.
- Read-only commands never enable GPS, change APN, scan networks, or alter modem state.
- Permission failures are returned explicitly rather than converted into fake values.

## Phase 5 — Controlled actions, only when requested

### Candidate actions

- Send SMS.
- Delete SMS.
- USSD.
- Bearer connect/disconnect.
- Network scan or operator registration.
- GNSS enable/disable.

Each action requires a separate command schema, explicit user confirmation, audit record, timeout, and a documented failure/recovery path. No arbitrary shell or arbitrary AT command interface is planned.

Voice call control and audio bridging remain a separate hardware-validation project. Do not accept calls or bridge audio until the actual SKU, audio path, ALSA/PCM route, and codec pipeline are verified.

## Storage and PostgreSQL decision

SQLite remains the default for one local notifier and local readers. Use WAL and bounded retention if concurrent local reads or history size require them. Consider PostgreSQL only when there are multiple modems, remote dashboards, multiple writers, long-term analytics, or cross-host consumers. PostgreSQL cannot reduce the cost of `mmcli --messaging-list-sms`; it would only replace the local persistence layer.

## Non-goals

- No production deployment or public HTTP API by default.
- No automatic SMS sending or deletion.
- No automatic APN/network-mode changes.
- No raw arbitrary AT command tool.
- No copying private SMS, location, or webhook data into external services without explicit authorization.

## Working rule

Each phase must leave the current notifier usable. New paths are introduced behind tests and verification; the reliable reconciliation path is not removed until a replacement has demonstrated equivalent correctness and recovery.
