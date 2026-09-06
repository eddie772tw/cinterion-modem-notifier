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
from pathlib import Path
from typing import Any


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


@dataclass(frozen=True)
class Event:
    kind: str
    title: str
    fields: dict[str, str]

    def fingerprint(self) -> str:
        data = json.dumps([self.kind, self.title, self.fields], sort_keys=True)
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
        self.data: dict[str, Any] = {"fingerprints": [], "seen_sms": [], "calls": {}}
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
    def __init__(self, mmcli: Mmcli, store: Store, discord: Discord, call_policy: str) -> None:
        self.mmcli, self.store, self.discord = mmcli, store, discord
        if call_policy not in {"hangup-incoming", "observe"}:
            raise ValueError("CALL_POLICY must be 'hangup-incoming' or 'observe'")
        self.call_policy = call_policy
        if discord.target_id and store.data.get("delivery_target") != discord.target_id:
            # The event history belongs to the previous target (or a legacy
            # blank-webhook run); deliver a fresh state snapshot to this URL.
            store.data["fingerprints"] = []
            store.data["delivery_target"] = discord.target_id

    def poll(self) -> None:
        events = self._status_events() + self._sms_events() + self._call_events()
        for event in events:
            if self.store.seen(event):
                continue
            if self.discord.deliver(event):
                self.store.record(event)
                if event.kind.startswith("sms:"):
                    self.store.data["seen_sms"] = (self.store.data["seen_sms"] + [event.kind[4:]])[-500:]
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

    def _sms_events(self) -> list[Event]:
        events: list[Event] = []
        seen: set[str] = set(self.store.data["seen_sms"])
        migrated = False
        for path in self.mmcli.paths("--messaging-list-sms"):
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
    )
    try:
        interval = max(5, int(setting("POLL_INTERVAL_SECONDS", "20")))
    except ValueError:
        LOG.warning("invalid POLL_INTERVAL_SECONDS; using 20")
        interval = 20
    while True:
        monitor.poll()
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
