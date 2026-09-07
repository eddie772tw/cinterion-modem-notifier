#!/usr/bin/env python3
"""Local ModemManager monitor with optional Discord webhook delivery.

This process is deliberately read-only: every modem interaction is an mmcli
query.  It never enables a modem, creates a bearer, sends an SMS, or sends AT
commands.  Discord delivery is disabled until DISCORD_WEBHOOK_URL is set.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from modem_events import GdbusSignalSource
from modem_history import EventHistory


LOG = logging.getLogger("cinterion_modem_notifier")
# mmcli prints D-Bus object paths such as ``.../SMS/30``. Keep this narrow so
# unrelated paths in command output are ignored.
OBJECT_PATH = re.compile(r"/org/freedesktop/ModemManager1/(?:SMS|Call)/\d+")

COLOR_GREEN = 0x57F287
COLOR_RED = 0xED4245
COLOR_YELLOW = 0xFEE75C
COLOR_BLUE = 0x3498DB
COLOR_GRAY = 0x95A5A6
WEAK_SIGNAL_THRESHOLD = 30
# Evaluated from the lowest threshold upward; the resulting labels remain
# user-facing ``<90``, ``<75``, ``<50``, and ``<25``.
SIGNAL_NOTIFICATION_THRESHOLDS = (25, 50, 75, 90)


def setting(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def masked_number(number: str) -> str:
    """Avoid putting full phone numbers into a third-party webhook by default."""
    digits = re.sub(r"[^0-9+]", "", number)
    if len(digits) <= 4:
        return "redacted"
    return f"…{digits[-4:]}"


def sms_identity(details: dict[str, Any]) -> str:
    """Return a stable, non-reversible identity for one received SMS."""
    identity = {
        key: str(details.get(key, ""))
        for key in ("storage", "timestamp", "number", "text", "data")
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def as_mapping(value: Any) -> Mapping[str, Any]:
    """Treat malformed optional ModemManager JSON sections as empty."""
    return value if isinstance(value, Mapping) else {}


def signal_quality_band(value: str) -> str:
    """Return the notification band for a signal quality percentage."""
    try:
        quality = int(value)
    except (TypeError, ValueError):
        return "unknown"
    for threshold in SIGNAL_NOTIFICATION_THRESHOLDS:
        if quality < threshold:
            return f"<{threshold}"
    return ">=90"


def sms_age_seconds(timestamp: str) -> float | None:
    try:
        return max(0.0, time.time() - datetime.fromisoformat(timestamp).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class Event:
    kind: str
    title: str
    fields: dict[str, str]

    def fingerprint(self) -> str:
        fingerprint_fields = self.fields
        if self.kind in {"status", "unavailable"} and "signal quality" in self.fields:
            # Keep the exact value visible in the embed, but deduplicate status
            # events by threshold band so normal RSSI fluctuation is quiet.
            fingerprint_fields = dict(self.fields)
            fingerprint_fields["signal quality"] = signal_quality_band(
                self.fields["signal quality"]
            )
        data = json.dumps([self.kind, self.title, fingerprint_fields], sort_keys=True)
        # Presentation changes to modem status (for example the health color)
        # should produce one fresh notification without replaying SMS history.
        if self.kind in {"status", "unavailable"}:
            data = json.dumps([data, embed_color(self)], sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()


def embed_color(event: Event) -> int:
    """Choose a compact visual status for Discord embeds."""
    if event.kind.startswith("sms:"):
        return COLOR_BLUE
    if event.kind not in {"status", "unavailable"}:
        return COLOR_GRAY
    if event.kind == "unavailable":
        return COLOR_RED

    fields = {key.lower(): value.strip().lower() for key, value in event.fields.items()}
    state = fields.get("state", "")
    registration = fields.get("network registration", "")
    packet = fields.get("packet service", "")
    failure = fields.get("failure", "")
    if (
        state in {"failed", "disabled", "locked", "unknown", "offline", "power-off"}
        or registration in {"denied", "unknown", "searching", "unregistered"}
        or failure not in {"", "--", "none"}
        or packet == "detached"
    ):
        return COLOR_RED

    try:
        signal = int(fields.get("signal quality", ""))
    except ValueError:
        signal = None
    if signal is not None and signal < WEAK_SIGNAL_THRESHOLD:
        return COLOR_YELLOW

    if state in {"registered", "connected", "enabled"} or registration in {"home", "roaming"} or packet == "attached":
        return COLOR_GREEN
    return COLOR_GRAY


class Mmcli:
    def __init__(self, modem_id: str) -> None:
        self.modem_id = modem_id

    def json(self, *args: str) -> dict[str, Any] | None:
        result = self._run("-J", *args)
        if result.returncode:
            LOG.debug("mmcli query failed: %s", result.stderr.strip())
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            LOG.warning("mmcli returned invalid JSON")
            return None

    def paths(self, action: str) -> list[str]:
        result = self._run("-m", self.modem_id, action)
        if result.returncode:
            LOG.debug("mmcli %s unavailable: %s", action, result.stderr.strip())
            return []
        return OBJECT_PATH.findall(result.stdout)

    def hangup(self, call_path: str) -> bool:
        """Hang up a call through ModemManager, never via a raw AT port."""
        result = self._run("-c", call_path, "--hangup")
        if result.returncode:
            LOG.warning("could not hang up call %s: %s", call_path, result.stderr.strip())
            return False
        LOG.info("incoming call hung up: %s", call_path)
        return True

    @staticmethod
    def _run(*args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["mmcli", *args], text=True, capture_output=True, timeout=15, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(["mmcli", *args], 1, "", str(exc))


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"fingerprints": [], "seen_sms": [], "sms_paths": {}, "calls": {}}
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError):
            LOG.warning("state file is unreadable; starting with empty state")
        self._normalise()

    def _normalise(self) -> None:
        for key in ("fingerprints", "seen_sms"):
            if not isinstance(self.data.get(key), list):
                self.data[key] = []
        if not isinstance(self.data.get("calls"), dict):
            self.data["calls"] = {}
        if not isinstance(self.data.get("sms_paths"), dict):
            self.data["sms_paths"] = {}

    def seen(self, event: Event) -> bool:
        return event.fingerprint() in self.data["fingerprints"]

    def record(self, event: Event) -> None:
        self.data["fingerprints"] = (self.data["fingerprints"] + [event.fingerprint()])[-200:]

    def save(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, sort_keys=True))
        temporary.chmod(0o600)
        temporary.replace(self.path)


class Discord:
    def __init__(self, webhook: str) -> None:
        self.webhook = webhook

    @property
    def enabled(self) -> bool:
        return bool(self.webhook)

    @property
    def target_id(self) -> str | None:
        if not self.webhook:
            return None
        return hashlib.sha256(self.webhook.encode()).hexdigest()

    def deliver(self, event: Event) -> bool:
        if not self.webhook:
            return False
        fields = []
        for key, value in event.fields.items():
            text = value or "--"
            if len(text) > 1024:
                text = text[:1021] + "..."
            fields.append({"name": key[:256], "value": text, "inline": True})
        payload = json.dumps({"embeds": [{
            "title": event.title[:256],
            "color": embed_color(event),
            "fields": fields,
        }]}).encode()
        request = urllib.request.Request(
            self.webhook,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "cinterion-modem-notifier/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                successful = 200 <= response.status < 300
                if successful:
                    LOG.info("Discord delivery succeeded: %s", event.title)
                return successful
        except (urllib.error.URLError, TimeoutError) as exc:
            LOG.warning("Discord delivery failed: %s", exc)
            return False


class Monitor:
    def __init__(self, mmcli: Mmcli, store: Store, discord: Discord, call_policy: str, history: EventHistory | None = None) -> None:
        self.mmcli, self.store, self.discord, self.history = mmcli, store, discord, history
        if call_policy not in {"hangup-incoming", "observe"}:
            raise ValueError("CALL_POLICY must be 'hangup-incoming' or 'observe'")
        self.call_policy = call_policy
        if discord.target_id and store.data.get("delivery_target") != discord.target_id:
            # The event history belongs to the previous target (or a legacy
            # blank-webhook run); deliver a fresh state snapshot to this URL.
            store.data["fingerprints"] = []
            store.data["delivery_target"] = discord.target_id

    def poll(self) -> None:
        status_events = self._status_events()
        scan_started = time.monotonic()
        sms_events = self._sms_events()
        if self.history is not None:
            self.history.record_metric(
                "full_sms_scan_seconds",
                time.monotonic() - scan_started,
                "seconds",
                {"sms_count": len(self.store.data["sms_paths"])},
            )
        self._deliver_events(status_events + sms_events + self._call_events())

    def poll_status(self) -> None:
        """Process a modem status signal without scanning SMS objects."""
        self._deliver_events(self._status_events())

    def poll_reconciliation(self) -> None:
        """Reconcile object-list events without polling modem status."""
        scan_started = time.monotonic()
        sms_events = self._sms_events(only_new_paths=True)
        if self.history is not None:
            self.history.record_metric(
                "sms_reconciliation_seconds",
                time.monotonic() - scan_started,
                "seconds",
                {"sms_count": len(self.store.data["sms_paths"])},
            )
        self._deliver_events(sms_events + self._call_events())

    def _deliver_events(self, events: list[Event]) -> None:
        for event in events:
            if self.history is not None:
                self.history.observe(event)
            if self.store.seen(event):
                continue
            if self.discord.deliver(event):
                self.store.record(event)
                if event.kind.startswith("sms:"):
                    self.store.data["seen_sms"] = (self.store.data["seen_sms"] + [event.kind[4:]])[-500:]
                    if self.history is not None:
                        lag = sms_age_seconds(event.fields.get("timestamp", ""))
                        if lag is not None:
                            self.history.record_metric("sms_notification_lag_seconds", lag, "seconds")
        self.store.save()

    def _status_events(self) -> list[Event]:
        snapshot = self.mmcli.json("-m", self.mmcli.modem_id)
        if not snapshot:
            return [Event("unavailable", "Modem unavailable", {"modem": self.mmcli.modem_id})]
        modem = snapshot.get("modem", {})
        generic = modem.get("generic", {})
        cellular = modem.get("3gpp", {})
        sim_slots = generic.get("sim-slots", [])
        sim_state = "present" if generic.get("sim") not in (None, "--", "/") else "missing"
        fields = {
            "state": str(generic.get("state", "unknown")),
            "failure": str(generic.get("state-failed-reason", "--")),
            "SIM": sim_state,
            "SIM slots": ", ".join(map(str, sim_slots)) or "--",
            "network registration": str(cellular.get("registration-state", "--")),
            "operator": str(cellular.get("operator-name", "--")),
            "packet service": str(cellular.get("packet-service-state", "--")),
            "access technology": ", ".join(generic.get("access-technologies", [])) or "--",
            "signal quality": str(generic.get("signal-quality", {}).get("value", "--")),
        }

        # A SIM may be present and registered while an auxiliary lock (for
        # example SIM PIN2/fixed-dialing) remains enabled.
        fields["SIM lock"] = str(generic.get("unlock-required", "--"))

        # Registration/packet attachment does not necessarily mean that the
        # host has a usable data interface. Include the initial EPS bearer
        # state when ModemManager exposes it.
        initial_bearer = cellular.get("eps", {}).get("initial-bearer", {})
        bearer_path = initial_bearer.get("dbus-path")
        fields["data bearer"] = "--"
        fields["data interface"] = "--"
        if bearer_path:
            bearer = self.mmcli.json("-b", str(bearer_path)) or {}
            bearer_details = bearer.get("bearer", {})
            bearer_status = bearer_details.get("status", {})
            fields["data bearer"] = str(bearer_status.get("connected", "--"))
            fields["data interface"] = str(bearer_status.get("interface", "--"))

        return [Event("status", "Modem status update", fields)]

    def _sms_events(self, only_new_paths: bool = False) -> list[Event]:
        events: list[Event] = []
        seen: set[str] = set(self.store.data["seen_sms"])
        sms_paths: dict[str, str] = self.store.data["sms_paths"]
        migrated = False
        paths = self.mmcli.paths("--messaging-list-sms")
        current_paths = set(paths)
        for path in list(sms_paths):
            if path not in current_paths:
                del sms_paths[path]
        for path in paths:
            if only_new_paths and path in sms_paths and sms_paths[path] in seen:
                continue
            sms = self.mmcli.json("-s", path)
            if not sms:
                continue
            sms_data = as_mapping(sms.get("sms", {}))
            # ModemManager JSON has used both a flattened ``generic`` object
            # and separate ``content``/``properties`` objects across releases.
            details = dict(as_mapping(sms_data.get("properties", {})))
            details.update(as_mapping(sms_data.get("content", {})))
            details.update(as_mapping(sms_data.get("generic", {})))
            if details.get("state") != "received":
                continue
            identity = sms_identity(details)
            sms_paths[path] = identity
            if identity in seen:
                continue
            # Migrate paths written by versions before stable SMS identities.
            # The content is read before accepting the legacy path, so future
            # path reuse can be detected by its different identity.
            if path in seen:
                seen.remove(path)
                seen.add(identity)
                migrated = True
                continue
            text = details.get("text")
            data = details.get("data")
            if text in (None, "", "--") and data not in (None, "", "--"):
                text = f"[binary SMS data present; {len(str(data))} characters]"
            fields = {
                "from": str(details.get("number", "--")) or "--",
                "timestamp": str(details.get("timestamp", "--")),
                "storage": str(details.get("storage", "--")),
                "text": str(text) if text not in (None, "") else "--",
            }
            events.append(Event(f"sms:{identity}", "Incoming SMS", fields))
        if migrated:
            self.store.data["seen_sms"] = list(seen)[-500:]
        return events

    def _call_events(self) -> list[Event]:
        current: dict[str, dict[str, Any]] = {}
        events: list[Event] = []
        for path in self.mmcli.paths("--voice-list-calls"):
            call = self.mmcli.json("-c", path)
            call_data = (call or {}).get("call", {})
            details = dict(call_data.get("properties", {}))
            details.update(call_data.get("content", {}))
            details.update(call_data.get("generic", {}))
            current[path] = details
            direction, state = str(details.get("direction", "--")), str(details.get("state", "--"))
            events.append(Event(f"call:{path}:{state}", "Call state update", {
                "direction": direction, "state": state,
                "number": masked_number(str(details.get("number", ""))),
            }))
            if direction == "incoming" and self.call_policy == "hangup-incoming":
                self.mmcli.hangup(path)
        previous: dict[str, dict[str, Any]] = self.store.data.get("calls", {})
        for path, details in previous.items():
            if path not in current and details.get("direction") == "incoming" and details.get("state") in {"ringing-in", "unknown"}:
                events.append(Event(f"missed:{path}", "Possible missed call", {
                    "number": masked_number(str(details.get("number", ""))),
                    "note": "Incoming call disappeared before an active state was observed.",
                }))
        self.store.data["calls"] = current
        return events


def main() -> int:
    logging.basicConfig(level=setting("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    state_home = Path(setting("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    monitor = Monitor(
        Mmcli(setting("MODEM_ID", "0")),
        Store(state_home / "cinterion-modem-notifier/state.json"),
        Discord(setting("DISCORD_WEBHOOK_URL", "")),
        setting("CALL_POLICY", "hangup-incoming"),
        EventHistory(state_home / "cinterion-modem-notifier/events.db"),
    )
    try:
        interval = max(5, int(setting("POLL_INTERVAL_SECONDS", "20")))
    except ValueError:
        LOG.warning("invalid POLL_INTERVAL_SECONDS; using 20")
        interval = 20
    try:
        reconciliation_interval = max(1, int(setting("RECONCILIATION_INTERVAL_SECONDS", "5")))
    except ValueError:
        LOG.warning("invalid RECONCILIATION_INTERVAL_SECONDS; using 5")
        reconciliation_interval = 5
    modem_path = f"/org/freedesktop/ModemManager1/Modem/{monitor.mmcli.modem_id}"
    # Monitor all ModemManager objects so SMS/Call object signals are not
    # lost when they are emitted below the modem object path.
    watcher = GdbusSignalSource()
    try:
        watcher.start()
    except (OSError, RuntimeError) as exc:
        LOG.warning("event monitor unavailable; falling back to polling: %s", exc)
        watcher = None

    # Start listening before the potentially expensive initial SMS scan. Any
    # signals emitted during the scan remain buffered and are reconciled after
    # the initial snapshot, eliminating the startup blind window.
    monitor.poll()

    # ModemManager emits state/property/SMS/call signals on the modem object.
    # A signal triggers the existing deduplicated snapshot logic; the fallback
    # poll is deliberately slow and only protects against a dead signal source.
    last_fallback_poll = time.monotonic()
    last_reconciliation = last_fallback_poll
    try:
        while True:
            if watcher is not None:
                signal = watcher.next_signal(timeout=1.0)
                if signal is not None:
                    LOG.debug(
                        "ModemManager signal: path=%s interface=%s member=%s",
                        signal.object_path,
                        signal.interface,
                        signal.member,
                    )
                    if signal.object_path == modem_path:
                        monitor.poll_status()
                    elif signal.object_path.startswith("/org/freedesktop/ModemManager1/SMS/") or signal.object_path.startswith("/org/freedesktop/ModemManager1/Call/") or signal.member in {"Added", "Deleted"} or any(
                        name in signal.interface for name in ("Messaging", "Voice")
                    ):
                        monitor.poll_reconciliation()
                elif not watcher.alive:
                    LOG.warning("event monitor exited; falling back to polling")
                    watcher.stop()
                    watcher = None
            if watcher is None and time.monotonic() - last_fallback_poll >= interval:
                monitor.poll()
                last_fallback_poll = time.monotonic()
            elif watcher is not None and time.monotonic() - last_reconciliation >= reconciliation_interval:
                # Signals remain primary, but SMS/call object lists are cheap
                # to reconcile and protect against missed/buffered D-Bus lines.
                monitor.poll_reconciliation()
                last_reconciliation = time.monotonic()
    finally:
        if watcher is not None:
            watcher.stop()


if __name__ == "__main__":
    sys.exit(main())
