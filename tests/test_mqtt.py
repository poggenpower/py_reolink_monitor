from __future__ import annotations

import ssl

from reolink_monitor import mqtt
from reolink_monitor.config import MqttConfig


class FakeClient:
    def __init__(self, *_args, **_kwargs):
        self.ssl_context = None
        self.will = None
        self.published = []
        self.disconnected = False
        self.loop_stopped = False

    def tls_set_context(self, context):
        self.ssl_context = context

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return FakePublishInfo()

    def disconnect(self):
        self.disconnected = True

    def loop_stop(self):
        self.loop_stopped = True


class FakePublishInfo:
    rc = mqtt.mqtt.MQTT_ERR_SUCCESS

    def wait_for_publish(self, timeout=None):
        return None

    def is_published(self):
        return True


class SuccessfulReasonCode:
    is_failure = False

    def __str__(self):
        return "Success"


def test_mqtt_tls_uses_trusted_server_certificate_and_validates_hostname(monkeypatch, tmp_path):
    cert_path = str(tmp_path / "broker.pem")
    contexts = []

    def create_context(*, cafile):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        contexts.append((cafile, context))
        return context

    monkeypatch.setattr(mqtt.mqtt, "Client", FakeClient)
    monkeypatch.setattr(mqtt.ssl, "create_default_context", create_context)

    publisher = mqtt.MqttPublisher(
        MqttConfig(host="mqtt.local", tls=True, server_cert=cert_path),
        availability_topic="custom/availability",
    )

    ca_file, context = contexts[0]
    assert ca_file == cert_path
    assert publisher._client.ssl_context is context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN


def test_mqtt_insecure_enables_tls_without_certificate_validation(monkeypatch):
    contexts = []

    def create_context(*, cafile):
        assert cafile is None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        contexts.append(context)
        return context

    monkeypatch.setattr(mqtt.mqtt, "Client", FakeClient)
    monkeypatch.setattr(mqtt.ssl, "create_default_context", create_context)

    publisher = mqtt.MqttPublisher(
        MqttConfig(host="mqtt.local", ca_cert="/unused/when/insecure.pem"),
        availability_topic="custom/availability",
        insecure=True,
    )

    assert publisher._client.ssl_context is contexts[0]
    assert contexts[0].verify_mode == ssl.CERT_NONE
    assert contexts[0].check_hostname is False


def test_mqtt_tls_is_not_enabled_by_default(monkeypatch):
    monkeypatch.setattr(mqtt.mqtt, "Client", FakeClient)

    publisher = mqtt.MqttPublisher(
        MqttConfig(host="mqtt.local"),
        availability_topic="custom/availability",
    )

    assert publisher._client.ssl_context is None


def test_mqtt_publishes_retained_lwt_and_availability(monkeypatch):
    monkeypatch.setattr(mqtt.mqtt, "Client", FakeClient)
    publisher = mqtt.MqttPublisher(
        MqttConfig(host="mqtt.local"),
        availability_topic="custom/monitor/availability",
    )

    publisher._on_connect(None, None, None, SuccessfulReasonCode())
    publisher.close()

    assert publisher._client.will == (
        "custom/monitor/availability",
        "offline",
        1,
        True,
    )
    assert publisher._client.published == [
        ("custom/monitor/availability", "online", 1, True),
        ("custom/monitor/availability", "offline", 1, True),
    ]
    assert publisher._client.disconnected
    assert publisher._client.loop_stopped
