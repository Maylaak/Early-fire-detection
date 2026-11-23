"""E-mail notifier (SMTP).

Works with any mail server that accepts SMTP: Gmail, Outlook/Office 365, or
the municipality's own server. Server settings are in config.yaml
(notifiers.email). Addresses and the password come from environment
variables, never from config.yaml. Default variable names:

    set ALERT_EMAIL_USER=alerts@example.org        (SMTP login)
    set ALERT_EMAIL_PASSWORD=app-password           (Gmail: an "app password")
    set ALERT_EMAIL_FROM=alerts@example.org         (optional; default: the login)
    set ALERT_EMAIL_TO=ops@example.org,chief@example.org   (comma-separated)

Then add "email" to notifiers.enabled. If a variable is missing the
detection service refuses to start, so nobody believes e-mail alerts work
when they do not. A message is one alert: subject "[FIRE ALARM] F-07-03 ...",
body with the time, cluster, gateway and GPS position of the node.
"""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable, Mapping
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from gateway.config import EmailConfig, SitesConfig
from gateway.detect import Event
from gateway.notify import Notifier, NotifierConfigError, NotifierError, format_event, headline


def location_lines(event: Event, sites: SitesConfig | None) -> list[str]:
    """Where the node is, in operator words: label, cluster, gateway, GPS."""
    site = sites.node(event.dev_eui) if sites else None
    if site is None:
        return [f"Node: {event.node_name} (not in the site registry)"]
    gw = sites.gateway(site.gateway_id)
    kind = "surveyed" if site.surveyed else "planned grid position"
    return [
        f"Node: {site.label}",
        f"Cluster: {site.block}",
        f"Gateway: {gw.label if gw else '-'}",
        f"GPS: {site.lat:.5f}, {site.lon:.5f} ({kind})",
        f"Map: https://www.google.com/maps?q={site.lat:.5f},{site.lon:.5f}",
    ]


class EmailNotifier(Notifier):
    name = "email"

    def __init__(self, cfg: EmailConfig, username: str, password: str, sender: str,
                 recipients: list[str], sites: SitesConfig | None = None,
                 smtp_factory: Callable[..., smtplib.SMTP] | None = None):
        self.cfg, self.username, self.password = cfg, username, password
        self.sender, self.recipients, self.sites = sender, recipients, sites
        self._factory = smtp_factory

    @classmethod
    def from_config(cls, cfg: EmailConfig, env: Mapping[str, str],
                    sites: SitesConfig | None = None) -> EmailNotifier:
        user, password = env.get(cfg.username_env), env.get(cfg.password_env)
        to = [a.strip() for a in (env.get(cfg.to_env) or "").split(",") if a.strip()]
        missing = [n for n, v in ((cfg.username_env, user), (cfg.password_env, password), (cfg.to_env, to)) if not v]
        if missing:
            raise NotifierConfigError(
                f"email notifier is enabled but {' and '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} not set")
        bad = [a for a in to if "@" not in a]
        if bad:
            raise NotifierConfigError(f"{cfg.to_env} has an entry that is not an e-mail address: {bad[0]!r}")
        return cls(cfg, user, password, env.get(cfg.from_env) or user, to, sites)

    def message(self, event: Event) -> EmailMessage:
        site = self.sites.node(event.dev_eui) if self.sites else None
        who = site.label if site else event.node_name
        msg = EmailMessage()
        msg["Subject"] = f"[{headline(event)}] {who}" + (f" - cluster {site.block}" if site else "")
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg["Date"] = formatdate(localtime=False)
        msg["Message-ID"] = make_msgid(domain="wildfire-gateway.local")
        msg.set_content("\n".join([format_event(event), "", *location_lines(event, self.sites), "",
                                   "Sent automatically by the wildfire detection gateway."]))
        return msg

    def _connect(self) -> smtplib.SMTP:
        c = self.cfg
        if self._factory is not None:
            return self._factory(c.smtp_host, c.smtp_port, timeout=c.timeout_seconds)
        if c.security == "ssl":
            return smtplib.SMTP_SSL(c.smtp_host, c.smtp_port, timeout=c.timeout_seconds,
                                    context=ssl.create_default_context())
        return smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=c.timeout_seconds)

    def send(self, event: Event) -> None:
        msg = self.message(event)
        try:
            with self._connect() as smtp:
                if self.cfg.security == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
                smtp.login(self.username, self.password)
                refused = smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError:
            raise NotifierError("e-mail login refused (check user and password)") from None
        except (smtplib.SMTPException, OSError) as e:
            # Never echo server text that could contain the password.
            raise NotifierError(f"cannot send e-mail ({type(e).__name__})") from None
        if refused and len(refused) == len(self.recipients):
            raise NotifierError("the mail server refused every recipient")
