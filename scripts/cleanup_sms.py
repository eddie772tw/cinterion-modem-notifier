#!/usr/bin/env python3
"""Manually inspect or delete already-archived modem SMS.

Default mode is dry-run. Destructive mode requires root, --apply, --confirm,
and at least one narrow filter.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from modem_notifier import Mmcli, as_mapping, sms_identity  # noqa: E402
from sms_inbox import SmsInbox  # noqa: E402


def state_directory() -> Path:
    configured = os.environ.get("MODEM_STATE_DIR", "").strip()
    if configured:
        return Path(configured)
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        home = Path(pwd.getpwnam(sudo_user).pw_dir)
    else:
        home = Path.home()
    return Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")) / "cinterion-modem-notifier"


def sms_details(mmcli: Mmcli, path: str) -> dict[str, object] | None:
    raw = mmcli.json("-s", path) or {}
    sms_data = as_mapping(raw.get("sms", {}))
    details = dict(as_mapping(sms_data.get("properties", {})))
    details.update(as_mapping(sms_data.get("content", {})))
    details.update(as_mapping(sms_data.get("generic", {})))
    if details.get("state") != "received":
        return None
    return details


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or delete archived modem SMS")
    parser.add_argument("--modem-id", default=os.environ.get("MODEM_ID", "0"))
    parser.add_argument("--sender", help="restrict candidates to this exact sender")
    parser.add_argument("--text-prefix", help="restrict candidates to this text prefix")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--show-text", action="store_true", help="show SMS text in local output")
    parser.add_argument("--apply", action="store_true", help="perform deletion; root is required")
    parser.add_argument("--confirm", action="store_true", help="required together with --apply")
    args = parser.parse_args()

    if args.apply and os.geteuid() != 0:
        parser.error("--apply must be run with sudo/root")
    if args.apply and not args.confirm:
        parser.error("--apply requires --confirm")
    if args.apply and not (args.sender or args.text_prefix):
        parser.error("--apply requires --sender and/or --text-prefix")

    state = state_directory()
    inbox = SmsInbox(state / "events.db")
    mmcli = Mmcli(str(args.modem_id))
    current_paths = set(mmcli.paths("--messaging-list-sms"))
    candidates = inbox.cleanup_candidates(current_paths, args.limit)
    candidates = [
        row for row in candidates
        if (not args.sender or row["from"] == args.sender)
        and (not args.text_prefix or row["text"].startswith(args.text_prefix))
    ]

    print(json.dumps({
        "dry_run": not args.apply,
        "apply": args.apply,
        "modem_id": str(args.modem_id),
        "candidate_count": len(candidates),
        "candidates": [
            {
                "identity": row["identity"],
                "modem_path": row["modem_path"],
                "from": row["from"],
                "timestamp": row["timestamp"],
                "storage": row["storage"],
                **({"text": row["text"]} if args.show_text else {}),
            }
            for row in candidates
        ],
    }, ensure_ascii=False, indent=2))

    if not args.apply:
        return 0

    failures = []
    deleted = []
    for row in candidates:
        path = str(row["modem_path"])
        details = sms_details(mmcli, path)
        if details is None or sms_identity(details) != row["identity"]:
            failures.append({"path": path, "reason": "identity/state verification failed"})
            continue
        result = subprocess.run(
            ["mmcli", "-m", str(args.modem_id), f"--messaging-delete-sms={path}"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            failures.append({"path": path, "reason": (result.stderr or result.stdout).strip()})
            continue
        if path in set(mmcli.paths("--messaging-list-sms")):
            failures.append({"path": path, "reason": "delete reported success but path remains"})
            continue
        deleted.append(path)

    print(json.dumps({"deleted": deleted, "failed": failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
