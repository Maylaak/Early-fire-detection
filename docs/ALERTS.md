# Alerts: e-mail, desktop notifications, Telegram, SMS

Every alert is sent by the detection service (window 1), from a background
thread, with three retries per channel. A channel that fails never delays
detection or the other channels. The console's red screen and sound do not
depend on any of these.

| Channel | Where it appears | Needs internet | Status |
|---|---|---|---|
| Console window | window 1 of the demo | no | always on |
| Desktop notifications | Windows notification from the open console tab | no | ready |
| E-mail | inboxes in `ALERT_EMAIL_TO` | yes | ready |
| Telegram | a chat or group | yes | ready |
| SMS | phones | depends on the transport | requirements below, not built |

## E-mail

1. Get an account that can send mail by SMTP. For Gmail, turn on 2-step
   verification, then create an **app password** (Google Account → Security →
   App passwords). The normal Gmail password does not work.
2. Server settings are in `config.yaml` under `notifiers.email`. The defaults
   are Gmail's (`smtp.gmail.com`, port 587, `starttls`). For Outlook / Office 365,
   use `smtp.office365.com`, 587, `starttls`.
3. Add `email` to `notifiers.enabled`, for example `enabled: [console, email]`.
4. In window 1, before `python -m gateway.service`:

```bat
set ALERT_EMAIL_USER=your.address@gmail.com
set ALERT_EMAIL_PASSWORD=the-16-letter-app-password
set ALERT_EMAIL_TO=first@example.org,second@example.org
```

`ALERT_EMAIL_FROM` is optional (default: the login). The password and the
addresses live only in environment variables, never in `config.yaml` or Git.

- **What a message looks like:** subject `[FIRE ALARM] F-07-03 - cluster F-07`.
  The body gives the time, the reason, the node, its cluster and gateway, its GPS
  position and a Google Maps link. OFFLINE (missing node) and back-online events
  are sent the same way.
- **If a setting is missing**, the detection service refuses to start and
  names the missing variable, so nobody believes e-mail alerts work when they don't.
- **If sending fails** (no internet, wrong password), window 1 prints a warning
  and the failure is recorded in the event log in the database (the console no
  longer shows that log); the alarm itself is unaffected.

## Desktop (web) notifications

Nothing to configure. On the console, click **Desktop alerts** (next to
Sound), then **Allow** when the browser asks. From then on:

- every **new fire alarm** pops up a Windows notification that stays until
  dismissed, naming the node, cluster, gateway and GPS position;
- every node that **goes missing** pops up a notification with its days missing;
- clicking a notification brings the console forward on that cluster.

Limits:
- The console tab must stay open (it may be in the background or minimised).
- If the button says **BLOCKED**, the browser has refused notifications for the
  page: click the padlock icon in the address bar, allow notifications, reload.
- Windows *Focus assist* / *Do not disturb* hides them.
- It only works at `http://127.0.0.1:8000` or `http://localhost:8000`, or over
  HTTPS. Opening the console from another computer by its IP address over
  plain HTTP will show "BLOCKED".

## SMS: requirements (not built yet)

Enabling `sms` today stops the service from starting, on purpose. To build it:

1. **Transport (team decision).**
   - **GSM modem at the gateway** (recommended): a USB 4G modem with a local
     SIM, driven by AT commands. Works with no internet, only mobile coverage.
     Needs the modem, a prepaid SIM with credit, and a free USB port.
   - **SMS gateway service** (e.g. Twilio): needs internet, an account and a
     paid sender number, and is charged per message.
2. **Recipients**: the phone numbers of civil defence and municipality contacts,
   kept in an environment variable (like e-mail), in international format (+961…).
3. **Message**: plain ASCII, at most 160 characters, for example
   `FIRE ALARM F-07-03 cl F-07 GW-01 33.55330,35.56636 12:31`.
4. **Rate limit**: one SMS per alarm, using the same per-node cooldown as the
   other channels (`detection.notify_cooldown_minutes`).
5. **Failure handling**: like the other channels, retry three times, then record
   the channel as failed.
6. **Tests** with a fake modem or fake API, like the e-mail tests: no real SMS
   is sent by the test suite.
