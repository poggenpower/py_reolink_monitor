from __future__ import annotations

import ssl

from reolink_monitor import mqtt
from reolink_monitor.config import MqttConfig


class FakeClient:
    def __init__(self, *_args, **_kwargs):
        self.ssl_context = None

    def tls_set_context(self, context):
        self.ssl_context = context


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
        MqttConfig(host="mqtt.local", tls=True, server_cert=cert_path)
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
        insecure=True,
    )

    assert publisher._client.ssl_context is contexts[0]
    assert contexts[0].verify_mode == ssl.CERT_NONE
    assert contexts[0].check_hostname is False


def test_mqtt_tls_is_not_enabled_by_default(monkeypatch):
    monkeypatch.setattr(mqtt.mqtt, "Client", FakeClient)

    publisher = mqtt.MqttPublisher(MqttConfig(host="mqtt.local"))

    assert publisher._client.ssl_context is None
