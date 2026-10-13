"""Small synchronous MQTT publisher used by the async monitor."""

from __future__ import annotations

import logging
import ssl
import threading
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from .config import MqttConfig

_LOGGER = logging.getLogger(__name__)


class MqttPublisher:
    def __init__(self, config: MqttConfig, *, insecure: bool = False) -> None:
        self._config = config
        self._connected = threading.Event()
        self._connection_error: str | None = None
        self._client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=config.client_id,
        )
        if config.tls or insecure:
            trusted_cert = None if insecure else config.server_cert or config.ca_cert
            context = ssl.create_default_context(cafile=trusted_cert)
            if config.server_cert and not insecure:
                context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
            if insecure:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            self._client.tls_set_context(context)
        if config.username:
            self._client.username_pw_set(config.username, config.password)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    def _on_connect(self, *_args: Any) -> None:
        reason_code = _args[3]
        if reason_code.is_failure:
            self._connection_error = str(reason_code)
        else:
            self._connection_error = None
        self._connected.set()

    def _on_disconnect(self, *_args: Any) -> None:
        self._connected.clear()
        reason_code = _args[3]
        if reason_code.is_failure:
            _LOGGER.warning("Disconnected from MQTT broker: %s", reason_code)

    def connect(self, timeout: float = 10.0) -> None:
        self._client.connect(self._config.host, self._config.port, keepalive=60)
        self._client.loop_start()
        if not self._connected.wait(timeout):
            self.close()
            raise TimeoutError("Timed out waiting for MQTT broker connection")
        if self._connection_error:
            self.close()
            raise ConnectionError(f"MQTT broker rejected connection: {self._connection_error}")

    def publish(self, topic: str, payload: str, retain: bool = True) -> None:
        info = self._client.publish(topic, payload, qos=1, retain=retain)
        info.wait_for_publish(timeout=10)
        if not info.is_published():
            raise TimeoutError(f"MQTT publish did not complete for topic '{topic}'")

    def close(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()
