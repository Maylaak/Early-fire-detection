"""Small wrapper around paho-mqtt 2.x shared by the simulator and ingestion."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

import paho.mqtt.client as mqtt

from gateway.config import MqttConfig

log = logging.getLogger(__name__)

MessageHandler = Callable[[mqtt.MQTTMessage], None]


class BrokerUnavailable(RuntimeError):
    pass


def connect(
    cfg: MqttConfig,
    client_name: str,
    subscriptions: list[str] | None = None,
    on_message: MessageHandler | None = None,
    persistent: bool = False,
) -> mqtt.Client:
    """Connect, start the network thread, and (re)subscribe on every connect.

    paho reconnects automatically after the first successful connect; topics
    are subscribed in on_connect so they survive a broker restart.

    persistent=True (the detection service): `client_name` is used as the
    client id as it is, with a persistent session (clean_session=False), so the
    broker queues QoS 1 messages for us while we are disconnected and delivers
    them on reconnect. Otherwise the id gets the process id appended and the
    session is thrown away on disconnect.
    """
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_name if persistent else f"{client_name}-{os.getpid()}",
        clean_session=not persistent,
    )

    def _on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connect refused: %s", reason_code)
            return
        resumed = ""
        if persistent:
            resumed = (" (resumed session: queued messages follow)" if flags.session_present
                       else " (new session: the broker queues for us from now on)")
        log.info("MQTT connected to %s:%s%s", cfg.host, cfg.port, resumed)
        for topic in subscriptions or []:
            client.subscribe(topic, qos=cfg.qos)

    def _on_disconnect(client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            log.warning("MQTT disconnected (%s), reconnecting...", reason_code)

    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    if on_message is not None:
        client.on_message = lambda c, u, msg: on_message(msg)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    try:
        client.connect(cfg.host, cfg.port, keepalive=cfg.keepalive_seconds)
    except OSError as e:
        raise BrokerUnavailable(
            f"cannot reach MQTT broker at {cfg.host}:{cfg.port} ({e}). "
            "Is Mosquitto running? (docker compose up -d)"
        ) from e
    client.loop_start()
    return client
