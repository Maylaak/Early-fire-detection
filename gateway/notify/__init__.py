"""Pluggable alert notifiers.

A notifier is anything with `name` and `send(event)`. To add one (e-mail,
siren relay, ...): subclass Notifier, implement send(), and register it in
build_notifiers(). send() raises NotifierError on failure.

The Dispatcher sends from a background thread, so a slow or unreachable
service (e.g. Telegram with no internet) never delays detection. An event is
marked `notified` in the database once at least one notifier delivered it.
Sending does not depend on the database: alerts go out while it is down.
An optional on_result callback hears each channel's final outcome (sent, or
given up after retries), for the operator console's event log.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence

from gateway.config import NotifiersConfig
from gateway.detect import ALARM, KIND_OFFLINE, KIND_ONLINE, NORMAL, WATCH, Event

log = logging.getLogger(__name__)

RETRY_DELAYS_SECONDS = (2.0, 5.0, 15.0)


class NotifierError(RuntimeError):
    """Sending failed (network, API error...). The dispatcher retries."""


class NotifierConfigError(ValueError):
    """Notifier is enabled but cannot work (missing secret, stub...). Fails at startup."""


class Notifier(ABC):
    name: str = "notifier"

    @abstractmethod
    def send(self, event: Event) -> None: ...


def headline(event: Event) -> str:
    if event.kind == KIND_OFFLINE:
        return "NODE OFFLINE"
    if event.kind == KIND_ONLINE:
        return "Node back online"
    return {ALARM: "FIRE ALARM", WATCH: "WATCH - possible smoke", NORMAL: "All clear"}.get(
        event.to_state or "", event.kind)


def format_event(event: Event) -> str:
    """Plain-text alert body shared by all notifiers (ASCII only: Windows consoles, SMS)."""
    return (f"[{headline(event)}] {event.node_name}\n"
            f"{event.time:%Y-%m-%d %H:%M:%S} UTC\n"
            f"{event.message}")


def build_notifiers(cfg: NotifiersConfig, env: Mapping[str, str] | None = None) -> list[Notifier]:
    """Create the enabled notifiers. Raises NotifierConfigError if one cannot work,
    so a misconfigured alarm channel is found at startup, not during a fire."""
    from gateway.notify.console import ConsoleNotifier
    from gateway.notify.sms import SmsNotifier
    from gateway.notify.telegram import TelegramNotifier

    env = os.environ if env is None else env
    out: list[Notifier] = []
    for name in dict.fromkeys(cfg.enabled):  # de-duplicate, keep order
        if name == "console":
            out.append(ConsoleNotifier())
        elif name == "telegram":
            out.append(TelegramNotifier.from_config(cfg.telegram, env))
        elif name == "sms":
            out.append(SmsNotifier())
    if not out:
        log.warning("no notifiers enabled: alarms will only be recorded in the database")
    return out


class Dispatcher:
    """Delivers events to every notifier from a background thread, with retries."""

    def __init__(
        self,
        notifiers: Sequence[Notifier],
        on_delivered: Callable[[object], None],
        retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS,
        on_result: Callable[[Event, object, str, bool, str], None] | None = None,
    ):
        self.notifiers = list(notifiers)
        self.on_delivered = on_delivered
        # (event, event_id, notifier name, delivered?, last error) per channel.
        # event_id is whatever the caller passed to submit(): a database id, or a
        # reference to an event not written yet (the database may be down).
        self.on_result = on_result
        self.retry_delays = retry_delays
        self._queue: queue.Queue[tuple[Event, object] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="notifier", daemon=True)

    def start(self) -> Dispatcher:
        self._thread.start()
        return self

    def submit(self, event: Event, event_id: object) -> None:
        self._queue.put((event, event_id))

    def stop(self, timeout: float = 10.0) -> None:
        """Deliver what is queued (up to `timeout`), then stop."""
        self._queue.put(None)
        self._thread.join(timeout)

    def deliver(self, event: Event, event_id: object) -> bool:
        """Send to all notifiers; retry only the ones that failed, so a broken
        channel never holds up the working ones. True if at least one delivered."""
        delivered = False
        pending = list(self.notifiers)
        errors: dict[str, str] = {}
        for attempt in range(len(self.retry_delays) + 1):
            failed = []
            for n in pending:
                try:
                    n.send(event)
                except Exception as e:  # any failure: retry this channel later
                    failed.append(n)
                    errors[n.name] = str(e) or type(e).__name__
                    log.warning("%s notifier failed for event %s: %s", n.name, event_id, e)
                else:
                    if not delivered:  # mark as soon as the first channel succeeds
                        delivered = True
                        self.on_delivered(event_id)
                    self._report(event, event_id, n.name, True, "")
            pending = failed
            if not pending:
                break
            if attempt < len(self.retry_delays):
                time.sleep(self.retry_delays[attempt])
        for n in pending:
            log.error("%s notifier gave up on event %s", n.name, event_id)
            self._report(event, event_id, n.name, False, errors.get(n.name, ""))
        return delivered

    def _report(self, event: Event, event_id: object, channel: str, ok: bool, detail: str) -> None:
        if self.on_result is None:
            return
        try:
            self.on_result(event, event_id, channel, ok, detail)
        except Exception:  # the log is secondary: never let it stop delivery
            log.exception("recording the %s result for event %s failed", channel, event_id)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self.deliver(*item)
            except Exception:
                log.exception("notification dispatch failed")
