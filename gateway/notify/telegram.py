"""Telegram notifier (Bot API sendMessage).

Secrets come from environment variables, never from config.yaml. The
variable NAMES are in config.yaml (notifiers.telegram), defaults:

    set TELEGRAM_BOT_TOKEN=123456:ABC...    (from @BotFather)
    set TELEGRAM_CHAT_ID=-1001234567890     (user, group or channel id)

Then add "telegram" to notifiers.enabled. See README, "Telegram alerts".
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from gateway.config import TelegramConfig
from gateway.detect import Event
from gateway.notify import Notifier, NotifierConfigError, NotifierError, format_event

API_BASE = "https://api.telegram.org"


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, timeout_seconds: float,
                 client: httpx.Client | None = None):
        self._url = f"{API_BASE}/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.client = client or httpx.Client(timeout=timeout_seconds)

    @classmethod
    def from_config(cls, cfg: TelegramConfig, env: Mapping[str, str]) -> TelegramNotifier:
        token, chat = env.get(cfg.bot_token_env), env.get(cfg.chat_id_env)
        missing = [name for name, v in ((cfg.bot_token_env, token), (cfg.chat_id_env, chat)) if not v]
        if missing:
            raise NotifierConfigError(
                f"telegram notifier is enabled but {' and '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} not set")
        return cls(token, chat, cfg.timeout_seconds)

    def send(self, event: Event) -> None:
        try:
            r = self.client.post(self._url, json={
                "chat_id": self.chat_id,
                "text": format_event(event),
                "disable_web_page_preview": True,
            })
        except httpx.HTTPError as e:
            # The exception text can contain the URL, and the URL contains the token.
            raise NotifierError(f"cannot reach Telegram ({type(e).__name__})") from None
        if r.status_code != 200:
            try:
                desc = r.json().get("description", "")
            except ValueError:
                desc = r.text[:200]
            raise NotifierError(f"Telegram API HTTP {r.status_code}: {desc}")
