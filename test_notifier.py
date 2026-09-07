import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from modem_notifier import (
    COLOR_BLUE,
    COLOR_GRAY,
    COLOR_GREEN,
    COLOR_RED,
    COLOR_YELLOW,
    Discord,
    Event,
    Mmcli,
    Monitor,
    OBJECT_PATH,
    Store,
    embed_color,
    masked_number,
    sms_identity,
)
from modem_events import SIGNAL_HEADER
from modem_history import EventHistory
from sms_inbox import SmsInbox


class NotifierTests(unittest.TestCase):
    def test_event_history_upserts_observations_and_returns_recent_events(self):
        with tempfile.TemporaryDirectory() as directory:
            history = EventHistory(Path(directory) / "events.db")
            event = Event("status", "Status", {"state": "registered"})
            history.observe(event)
            history.observe(event)
            rows = history.recent()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["observations"], 2)
            self.assertEqual(rows[0]["fields"]["state"], "registered")

    def test_event_history_records_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            history = EventHistory(Path(directory) / "events.db")
            history.record_metric("reconciliation_seconds", 0.25, "seconds", {"sms_count": 3})
            metrics = history.recent_metrics(name="reconciliation_seconds")
            self.assertEqual(len(metrics), 1)
            self.assertEqual(metrics[0]["unit"], "seconds")
            self.assertEqual(metrics[0]["dimensions"]["sms_count"], 3)

    def test_sms_inbox_archives_and_lists_cleanup_candidates_without_deleting(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = SmsInbox(Path(directory) / "events.db")
            inbox.archive("identity", "/SMS/1", {
                "from": "0900", "timestamp": "now", "storage": "me", "text": "hello",
            })
            self.assertEqual(inbox.cleanup_candidates({"/SMS/1"}), [])
            inbox.mark_notified("identity")
            candidates = inbox.cleanup_candidates({"/SMS/1"})
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["identity"], "identity")

    def test_modem_signal_header_extracts_interface_and_member(self):
        line = "object /org/freedesktop/ModemManager1/Modem/0: signal interface=org.freedesktop.DBus.Properties; member=PropertiesChanged"
        match = SIGNAL_HEADER.search(line)
        self.assertIsNotNone(match)
        self.assertEqual(match["object_path"], "/org/freedesktop/ModemManager1/Modem/0")

    def test_embed_colors_classify_modem_and_sms_events(self):
        connected = Event("status", "status", {
            "state": "registered", "network registration": "home",
            "packet service": "attached", "signal quality": "100", "failure": "--",
        })
        weak = Event("status", "status", {
            "state": "registered", "network registration": "home",
            "packet service": "attached", "signal quality": "20", "failure": "--",
        })
        disconnected = Event("status", "status", {
            "state": "failed", "network registration": "denied",
            "packet service": "detached", "signal quality": "--", "failure": "sim-missing",
        })
        self.assertEqual(embed_color(connected), COLOR_GREEN)
        self.assertEqual(embed_color(weak), COLOR_YELLOW)
        self.assertEqual(embed_color(disconnected), COLOR_RED)
        self.assertEqual(embed_color(Event("status", "status", {
            "state": "registered", "network registration": "home",
            "packet service": "detached", "signal quality": "100", "failure": "--",
        })), COLOR_RED)
        self.assertEqual(embed_color(Event("sms:abc", "SMS", {})), COLOR_BLUE)
        self.assertEqual(embed_color(Event("call:abc", "Call", {})), COLOR_GRAY)

    def test_discord_payload_contains_embed_color(self):
        class Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        event = Event("sms:abc", "Incoming SMS", {"text": "hello"})
        with patch("modem_notifier.urllib.request.urlopen", return_value=Response()) as open_url:
            self.assertTrue(Discord("https://example.invalid/webhook").deliver(event))
        request = open_url.call_args.args[0]
        self.assertEqual(json.loads(request.data)["embeds"][0]["color"], COLOR_BLUE)

    def test_object_paths_extract_numeric_sms_and_call_ids(self):
        output = (
            "/org/freedesktop/ModemManager1/SMS/30 (received)\n"
            "/org/freedesktop/ModemManager1/Call/7 (ringing-in)"
        )
        self.assertEqual(
            OBJECT_PATH.findall(output),
            [
                "/org/freedesktop/ModemManager1/SMS/30",
                "/org/freedesktop/ModemManager1/Call/7",
            ],
        )

    def test_phone_number_is_redacted(self):
        self.assertEqual(masked_number("+886912345678"), "…5678")

    def test_event_fingerprint_is_stable(self):
        event = Event("status", "status", {"state": "failed"})
        self.assertEqual(event.fingerprint(), event.fingerprint())

    def test_event_fingerprint_changes_with_observable_state(self):
        self.assertNotEqual(
            Event("status", "status", {"state": "failed"}).fingerprint(),
            Event("status", "status", {"state": "registered"}).fingerprint(),
        )

    def test_status_fingerprint_includes_presentation_color(self):
        event = Event("status", "status", {
            "state": "registered", "network registration": "home",
            "packet service": "attached", "signal quality": "100", "failure": "--",
        })
        self.assertEqual(event.fingerprint(), event.fingerprint())

    def test_signal_quality_fingerprint_uses_notification_thresholds(self):
        def status(quality):
            return Event("status", "status", {
                "state": "registered", "network registration": "home",
                "packet service": "attached", "signal quality": str(quality), "failure": "--",
            })

        self.assertEqual(status(89).fingerprint(), status(80).fingerprint())
        self.assertNotEqual(status(80).fingerprint(), status(74).fingerprint())
        self.assertEqual(status(74).fingerprint(), status(60).fingerprint())
        self.assertNotEqual(status(60).fingerprint(), status(49).fingerprint())
        self.assertNotEqual(status(49).fingerprint(), status(24).fingerprint())

    def test_new_webhook_target_resets_legacy_delivery_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.json")
            store.data["fingerprints"] = ["legacy"]
            Monitor(Mmcli("0"), store, Discord("https://example.invalid/webhook"), "observe")
            self.assertEqual(store.data["fingerprints"], [])
            self.assertIn("delivery_target", store.data)

    def test_status_includes_sim_lock_and_data_bearer(self):
        bearer_path = "/org/freedesktop/ModemManager1/Bearer/0"

        class FakeMmcli:
            modem_id = "0"

            def json(self, *args):
                if args == ("-m", "0"):
                    return {
                        "modem": {
                            "generic": {
                                "state": "registered",
                                "sim": "/org/freedesktop/ModemManager1/SIM/0",
                                "sim-slots": ["/org/freedesktop/ModemManager1/SIM/0", "/"],
                                "unlock-required": "sim-pin2",
                                "access-technologies": ["lte"],
                                "signal-quality": {"value": "100"},
                            },
                            "3gpp": {
                                "registration-state": "home",
                                "operator-name": "TW Mobile",
                                "packet-service-state": "attached",
                                "eps": {"initial-bearer": {"dbus-path": bearer_path}},
                            },
                        }
                    }
                if args == ("-b", bearer_path):
                    return {"bearer": {"status": {"connected": "yes", "interface": "--"}}}
                return None

            def paths(self, _action):
                return []

        with tempfile.TemporaryDirectory() as directory:
            monitor = Monitor(FakeMmcli(), Store(Path(directory) / "state.json"), Discord(""), "observe")
            fields = monitor._status_events()[0].fields
            self.assertEqual(fields["SIM lock"], "sim-pin2")
            self.assertEqual(fields["data bearer"], "yes")
            self.assertEqual(fields["data interface"], "--")

    def test_sms_identity_is_stable_and_does_not_expose_content(self):
        details = {"storage": "sm", "timestamp": "now", "number": "+886912345678", "text": "hello"}
        identity = sms_identity(details)
        self.assertEqual(identity, sms_identity(dict(details)))
        self.assertNotIn("hello", identity)

    def test_store_normalises_invalid_collections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"fingerprints": null, "seen_sms": {}, "calls": []}')
            store = Store(path)
            self.assertEqual(store.data["fingerprints"], [])
            self.assertEqual(store.data["seen_sms"], [])
            self.assertEqual(store.data["sms_paths"], {})
            self.assertEqual(store.data["calls"], {})

    def test_rejects_unknown_call_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                Monitor(Mmcli("0"), Store(Path(directory) / "state.json"), Discord(""), "accept")

    def test_incoming_call_is_hung_up_when_policy_requires_it(self):
        class FakeMmcli:
            def __init__(self):
                self.hung_up = []

            def paths(self, action):
                return ["/org/freedesktop/ModemManager1/Call/7"] if action == "--voice-list-calls" else []

            def json(self, *_args):
                return {"call": {"generic": {"direction": "incoming", "state": "ringing-in", "number": "+886912345678"}}}

            def hangup(self, path):
                self.hung_up.append(path)
                return True

        with tempfile.TemporaryDirectory() as directory:
            fake = FakeMmcli()
            monitor = Monitor(fake, Store(Path(directory) / "state.json"), Discord(""), "hangup-incoming")
            events = monitor._call_events()
            self.assertEqual(fake.hung_up, ["/org/freedesktop/ModemManager1/Call/7"])
            self.assertEqual(events[0].title, "Call state update")

    def test_sms_event_includes_full_number_and_text(self):
        class FakeMmcli:
            def paths(self, _action):
                return ["/org/freedesktop/ModemManager1/SMS/3"]

            def json(self, *_args):
                return {"sms": {"generic": {"state": "received", "number": "+886912345678", "timestamp": "now", "storage": "sm", "text": "hello"}}}

        with tempfile.TemporaryDirectory() as directory:
            monitor = Monitor(FakeMmcli(), Store(Path(directory) / "state.json"), Discord(""), "observe")
            event = monitor._sms_events()[0]
            self.assertEqual(event.fields["from"], "+886912345678")
            self.assertEqual(event.fields["text"], "hello")

    def test_sms_event_supports_content_and_properties_schema(self):
        class FakeMmcli:
            def paths(self, _action):
                return ["/org/freedesktop/ModemManager1/SMS/30"]

            def json(self, *_args):
                return {"sms": {
                    "content": {"number": "+886912345678", "text": "hello"},
                    "properties": {"state": "received", "storage": "sm", "timestamp": "now"},
                }}

        with tempfile.TemporaryDirectory() as directory:
            monitor = Monitor(FakeMmcli(), Store(Path(directory) / "state.json"), Discord(""), "observe")
            event = monitor._sms_events()[0]
            self.assertEqual(event.fields["from"], "+886912345678")
            self.assertEqual(event.fields["text"], "hello")

    def test_sms_event_handles_binary_data_and_malformed_optional_sections(self):
        class FakeMmcli:
            def paths(self, _action):
                return ["/org/freedesktop/ModemManager1/SMS/31"]

            def json(self, *_args):
                return {"sms": {
                    "content": {"number": "+886912345678", "text": "", "data": "0102"},
                    "properties": {"state": "received", "storage": "sm", "timestamp": "now"},
                    "generic": None,
                }}

        with tempfile.TemporaryDirectory() as directory:
            monitor = Monitor(FakeMmcli(), Store(Path(directory) / "state.json"), Discord(""), "observe")
            event = monitor._sms_events()[0]
            self.assertIn("binary SMS data present", event.fields["text"])


if __name__ == "__main__":
    unittest.main()
