import io
import json
import logging
import threading
from datetime import datetime, timezone

import httpx
import pytest

from gateway.config import DEFAULT_CONFIG_PATH, load_config
from gateway.detect import ALARM, KIND_OFFLINE, KIND_STATE, WATCH, Event
from gateway.notify import (
    Dispatcher,
    Notifier,
    NotifierConfigError,
    NotifierError,
    build_notifiers,
    format_event,
)
from gateway.notify.console import ConsoleNotifier
from gateway.notify.telegram import TelegramNotifier

CFG = load_config(DEFAULT_CONFIG_PATH, env={})
T = datetime(2026, 7, 1, 12, 30, tzinfo=timezone.utc)
ALARM_EVENT = Event("70b3d57ed0000002", "node-2", T, KIND_STATE,
                    "node-2: FIRE ALARM (WATCH -> ALARM); gas -57% vs baseline", True,
                    from_state=WATCH, to_state=ALARM)
OFFLINE_EVENT = Event("70b3d57ed0000004", "node-4", T, KIND_OFFLINE,
                      "node-4 OFFLINE: no uplink for 11 min", True)
TELEGRAM_ENV = {"TELEGRAM_BOT_TOKEN": "123:SECRET", "TELEGRAM_CHAT_ID": "-10042"}


def notifiers_cfg(*enabled):
    return CFG.notifiers.model_copy(update={"enabled": list(enabled)})


# --- formatting and console --------------------------------------------------

def test_format_event():
    text = format_event(ALARM_EVENT)
    assert text.startswith("[FIRE ALARM] node-2")
    assert "2026-07-01 12:30:00 UTC" in text
    assert "gas -57%" in text
    assert text.isascii()
    assert format_event(OFFLINE_EVENT).startswith("[NODE OFFLINE] node-4")


def test_console_notifier_prints_loudly():
    out = io.StringIO()
    ConsoleNotifier(out).send(ALARM_EVENT)
    assert "FIRE ALARM" in out.getvalue() and "!!!!!" in out.getvalue()


# --- telegram ---------------------------------------------------------------

def telegram(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TelegramNotifier("123:SECRET", "-10042", 5, client=client)


def test_telegram_sends_message():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    telegram(handler).send(ALARM_EVENT)
    assert seen["url"] == "https://api.telegram.org/bot123:SECRET/sendMessage"
    assert seen["body"]["chat_id"] == "-10042"
    assert "FIRE ALARM" in seen["body"]["text"]


def test_telegram_api_error_raises():
    def handler(request):
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    with pytest.raises(NotifierError, match="chat not found"):
        telegram(handler).send(ALARM_EVENT)


def test_telegram_network_error_does_not_leak_token():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(NotifierError) as exc:
        telegram(handler).send(ALARM_EVENT)
    assert "SECRET" not in str(exc.value)


def test_telegram_from_env():
    [n] = build_notifiers(notifiers_cfg("telegram"), env=TELEGRAM_ENV)
    assert isinstance(n, TelegramNotifier) and n.chat_id == "-10042"


def test_telegram_enabled_without_env_fails_at_startup():
    with pytest.raises(NotifierConfigError, match="TELEGRAM_BOT_TOKEN"):
        build_notifiers(notifiers_cfg("telegram"), env={"TELEGRAM_CHAT_ID": "1"})


# --- sms stub ---------------------------------------------------------------

def test_sms_stub_refuses_to_pretend():
    with pytest.raises(NotifierConfigError, match="stub"):
        build_notifiers(notifiers_cfg("console", "sms"), env={})


def test_build_default_is_console_only():
    [n] = build_notifiers(CFG.notifiers, env={})
    assert isinstance(n, ConsoleNotifier)


# --- dispatcher -------------------------------------------------------------

class Flaky(Notifier):
    def __init__(self, name, failures):
        self.name, self.failures, self.calls = name, failures, 0

    def send(self, event):
        self.calls += 1
        if self.calls <= self.failures:
            raise NotifierError(f"{self.name} down")


def test_one_working_channel_is_enough_and_is_not_delayed():
    delivered, order = [], []
    good, bad = Flaky("good", 0), Flaky("bad", 99)
    good.send = lambda e: order.append("good")  # record timing relative to retries
    d = Dispatcher([bad, good], on_delivered=delivered.append, retry_delays=(0, 0))
    assert d.deliver(ALARM_EVENT, 7) is True
    assert delivered == [7]  # marked once
    assert order == ["good"]  # sent in the first round, not after bad's retries
    assert bad.calls == 3  # first try + 2 retries


def test_retry_until_success():
    delivered = []
    n = Flaky("flaky", 2)
    assert Dispatcher([n], delivered.append, retry_delays=(0, 0, 0)).deliver(ALARM_EVENT, 1)
    assert n.calls == 3 and delivered == [1]


def test_all_channels_failing_is_not_marked_notified(caplog):
    delivered = []
    with caplog.at_level(logging.ERROR):
        ok = Dispatcher([Flaky("a", 99)], delivered.append, retry_delays=(0,)).deliver(ALARM_EVENT, 3)
    assert ok is False and delivered == []
    assert "gave up" in caplog.text


def test_background_thread_delivers_and_stops():
    done = threading.Event()
    delivered = []

    def on_delivered(event_id):
        delivered.append(event_id)
        done.set()

    out = io.StringIO()
    d = Dispatcher([ConsoleNotifier(out)], on_delivered).start()
    d.submit(OFFLINE_EVENT, 11)
    assert done.wait(5)
    d.stop()
    assert delivered == [11] and "NODE OFFLINE" in out.getvalue()


def test_each_channel_reports_its_final_outcome():
    results = []
    d = Dispatcher([Flaky("bad", 99), Flaky("good", 0)], on_delivered=lambda _id: None,
                   retry_delays=(0, 0), on_result=lambda e, i, ch, ok, detail: results.append((ch, ok, i)))
    d.deliver(ALARM_EVENT, 9)
    assert sorted(results) == [("bad", False, 9), ("good", True, 9)]  # one entry per channel


def test_failing_result_callback_does_not_stop_delivery():
    delivered = []

    def broken(*_):
        raise RuntimeError("log database down")

    d = Dispatcher([Flaky("good", 0)], delivered.append, retry_delays=(0,), on_result=broken)
    assert d.deliver(ALARM_EVENT, 4)
    assert delivered == [4]