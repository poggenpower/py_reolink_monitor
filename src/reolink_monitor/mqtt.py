"""Small synchronous MQTT publisher used by the async monitor."""

from __future__ import annotations

import logging
import ssl
import threading
from collections.abc import Callable, Sequence
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from .config import MqttConfig

_LOGGER = logging.getLogger(__name__)


class MqttPublisher:
    def __init__(
        self,
        config: MqttConfig,
        *,
        availability_topic: str,
        insecure: bool = False,
    ) -> None:
        self._config = config
        self._availability_topic = availability_topic
        self._connected = threading.Event()
        self._connection_error: str | None = None
        self._subscription_error: str | None = None
        self._command_topics: tuple[str, ...] = ()
        self._command_callback: Callable[[str, str], None] | None = None
        self._client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=config.client_id,
        )
        self._client.will_set(
            availability_topic,
            payload="offline",
            qos=1,
            retain=True,
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
        self._client.on_message = self._on_message

    def _on_connect(self, *_args: Any) -> None:
        reason_code = _args[3]
        if reason_code.is_failure:
            self._connection_error = str(reason_code)
        else:
            self._connection_error = None
            info = self._client.publish(
                self._availability_topic,
                "online",
                qos=1,
                retain=True,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                _LOGGER.error(
                    "Could not publish MQTT availability: %s",
                    mqtt.error_string(info.rc),
                )
            if self._command_topics:
                try:
                    self._subscribe_commands()
                except ConnectionError as err:
                    self._subscription_error = str(err)
                    _LOGGER.error("%s", err)
        self._connected.set()

    def _subscribe_commands(self) -> None:
        for topic in self._command_topics:
            result, _message_id = self._client.subscribe(topic, qos=1)
            if result != mqtt.MQTT_ERR_SUCCESS:
                error = ConnectionError(
                    f"Could not subscribe to MQTT command topic '{topic}': "
                    f"{mqtt.error_string(result)}"
                )
                self._subscription_error = str(error)
                raise error
        self._subscription_error = None

    def subscribe_commands(
        self, topics: str | Sequence[str], callback: Callable[[str, str], None]
    ) -> None:
        self._command_topics = (topics,) if isinstance(topics, str) else tuple(topics)
        self._command_callback = callback
        if self._connected.is_set() and not self._connection_error:
            self._subscribe_commands()

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        if self._command_callback is None:
            return
        if message.retain:
            _LOGGER.warning("Ignoring retained MQTT command on topic %s", message.topic)
            return
        try:
            payload = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            _LOGGER.warning("Ignoring non-UTF-8 MQTT command on topic %s", message.topic)
            return
        self._command_callback(message.topic, payload)

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
        if self._subscription_error:
            self.close()
            raise ConnectionError(self._subscription_error)

    def publish(self, topic: str, payload: str, retain: bool = True) -> None:
        info = self._client.publish(topic, payload, qos=1, retain=retain)
        info.wait_for_publish(timeout=10)
        if not info.is_published():
            raise TimeoutError(f"MQTT publish did not complete for topic '{topic}'")

    def close(self) -> None:
        try:
            if self._connected.is_set() and not self._connection_error:
                self.publish(self._availability_topic, "offline")
        finally:
            self._client.disconnect()
            self._client.loop_stop()
