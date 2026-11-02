"""Console notifier: prints a loud block to the detection service's window."""

from __future__ import annotations

import sys

from gateway.detect import ALARM, Event
from gateway.notify import Notifier, format_event


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, stream=None):
        self.stream = stream

    def send(self, event: Event) -> None:
        stream = self.stream or sys.stdout
        bar = ("!" if event.to_state == ALARM else "=") * 72
        stream.write(f"\n{bar}\n{format_event(event)}\n{bar}\n")
        if event.to_state == ALARM:
            stream.write("\a")  # terminal bell
        stream.flush()
