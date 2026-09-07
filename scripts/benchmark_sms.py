#!/usr/bin/env python3
"""Read-only benchmark for the local SMS ingestion paths."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from modem_notifier import Discord, Mmcli, Monitor, Store  # noqa: E402


def samples(function, count: int) -> tuple[list[float], object]:
    durations = []
    result = None
    for _ in range(count):
        started = time.monotonic()
        result = function()
        durations.append(time.monotonic() - started)
    return durations, result


def summary(values: list[float]) -> dict[str, float]:
    return {
        "min_seconds": round(min(values), 4),
        "max_seconds": round(max(values), 4),
        "avg_seconds": round(sum(values) / len(values), 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark read-only SMS ingestion paths")
    parser.add_argument("--modem-id", default="0")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--skip-full", action="store_true", help="skip the O(N) full SMS scan")
    args = parser.parse_args()
    if args.samples < 1 or args.samples > 20:
        parser.error("--samples must be between 1 and 20")

    state_path = Path.home() / ".local/state/cinterion-modem-notifier/state.json"
    store = Store(state_path)
    monitor = Monitor(Mmcli(str(args.modem_id)), store, Discord(""), "observe")

    list_times, paths = samples(lambda: monitor.mmcli.paths("--messaging-list-sms"), args.samples)
    incremental_times, incremental_events = samples(
        lambda: monitor._sms_events(only_new_paths=True), args.samples
    )
    output = {
        "modem_id": str(args.modem_id),
        "sms_path_count": len(paths),
        "indexed_path_count": len(store.data["sms_paths"]),
        "seen_sms_count": len(store.data["seen_sms"]),
        "samples": args.samples,
        "list_paths": summary(list_times),
        "incremental_reconciliation": summary(incremental_times),
        "incremental_events_last_sample": len(incremental_events),
    }
    if not args.skip_full:
        full_times, full_events = samples(lambda: monitor._sms_events(only_new_paths=False), 1)
        output["full_scan"] = {
            **summary(full_times),
            "events_last_sample": len(full_events),
        }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
