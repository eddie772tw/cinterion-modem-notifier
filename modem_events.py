"""Event-driven ModemManager signal source using the system gdbus CLI.

The runtime image does not ship a Python D-Bus binding.  gdbus is part of the
system GLib tooling, so we use it only as a signal transport and keep all data
reads in the existing Mmcli adapter.  A signal triggers a targeted snapshot;
we never parse modem state from human-readable signal text.
"""
from __future__ import annotations

import re
import selectors
import subprocess
from dataclasses import dataclass
from typing import IO


SIGNAL_HEADER = re.compile(
    r"object (?P<object_path>/[^:]+): signal "
    r"interface=(?P<interface>[^;]+); member=(?P<member>[^ ]+)"
)


@dataclass(frozen=True)
class ModemSignal:
    object_path: str
    interface: str
    member: str
    raw: str


class GdbusSignalSource:
    """Watch ModemManager signals without changing modem state."""

    def __init__(self, modem_path: str | None = None, command: str = "gdbus") -> None:
        self.modem_path = modem_path
        self.command = command
        self.process: subprocess.Popen[str] | None = None
        self.selector = selectors.DefaultSelector()

    def start(self) -> None:
        self.stop()
        # gdbus may use block buffering when stdout is a pipe; force line
        # buffering so a single SMS signal reaches the notifier immediately.
        command = ["stdbuf", "-oL", self.command, "monitor", "--system", "--dest", "org.freedesktop.ModemManager1"]
        if self.modem_path:
            command.extend(["--object-path", self.modem_path])
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if self.process.stdout is None:
            raise RuntimeError("gdbus monitor did not provide stdout")
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def stop(self) -> None:
        if self.process is not None:
            if self.process.stdout is not None:
                try:
                    self.selector.unregister(self.process.stdout)
                except (KeyError, ValueError):
                    pass
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def next_signal(self, timeout: float) -> ModemSignal | None:
        if not self.alive or self.process is None or self.process.stdout is None:
            return None
        ready = self.selector.select(timeout)
        if not ready:
            return None
        line = self.process.stdout.readline()
        if not line:
            return None
        match = SIGNAL_HEADER.search(line)
        if not match:
            return None
        return ModemSignal(match["object_path"], match["interface"], match["member"], line.strip())

    def __enter__(self) -> "GdbusSignalSource":
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop()
