"""SMS notifier: STUB. Not implemented yet.

Enabling "sms" in notifiers.enabled makes the detection service refuse to
start, so nobody believes SMS alerts are working when they are not.

To implement, pick one transport and fill in send():

1. GSM modem on the gateway (recommended for remote sites: works with no
   internet, only mobile coverage). A USB 3G/4G modem driven with AT
   commands over a serial port, e.g. with pyserial:
       AT+CMGF=1                 text mode
       AT+CMGS="+961XXXXXXXX"    then the text, then Ctrl+Z (0x1A)
2. An SMS gateway API (needs internet), e.g. Twilio:
       POST https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json
       form fields To, From, Body; HTTP basic auth (sid, auth token)

Then:
- read secrets / phone numbers from environment variables (like telegram.py),
  with their names in config.yaml under notifiers.sms (add that section to
  NotifiersConfig in gateway/config.py),
- keep messages ASCII and under 160 characters (format_event() is close;
  trim event.message if needed),
- raise NotifierError on failure so the dispatcher retries,
- add tests next to tests/test_notify.py.
"""

from __future__ import annotations

from gateway.detect import Event
from gateway.notify import Notifier, NotifierConfigError


class SmsNotifier(Notifier):
    name = "sms"

    def __init__(self) -> None:
        raise NotifierConfigError(
            "the SMS notifier is a stub and cannot send anything yet; "
            "remove 'sms' from notifiers.enabled or implement gateway/notify/sms.py")

    def send(self, event: Event) -> None:  # pragma: no cover - never constructed
        raise NotImplementedError
