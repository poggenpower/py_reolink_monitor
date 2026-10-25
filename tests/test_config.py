from __future__ import annotations

import textwrap

import pytest

from reolink_monitor.config import load_config


def test_load_config_resolves_environment_and_parses_camera(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKER", "mqtt.local")
    monkeypatch.setenv("MQTT_PORT", "2883")
    monkeypatch.setenv("CAMERA_HOST", "192.0.2.12")
    monkeypatch.setenv("CAMERA_PASSWORD", "secret")
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            """
            mqtt:
              host: "${BROKER}"
              port: "${MQTT_PORT}"
            cameras:
              - name: Side Gate
                host: "${CAMERA_HOST}"
                username: admin
                password: "${CAMERA_PASSWORD}"
            """
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.mqtt is not None
    assert config.mqtt.host == "mqtt.local"
    assert config.mqtt.port == 2883
    assert config.cameras[0].host == "192.0.2.12"
    assert config.cameras[0].channel == 0
    assert config.poll_interval_seconds == 300
    assert config.recent_recordings_lookback_minutes == 1440
    assert config.mqtt.client_id == "reolink_monitor"
    assert config.state_prefix == "reolink_monitor"
    assert "secret" not in repr(config)


def test_load_config_parses_mqtt_tls_certificate_paths(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            """
            mqtt:
              host: mqtt.local
              tls: true
              ca_cert: certificates/ca.pem
            cameras:
              - name: Side Gate
                host: 192.0.2.12
                username: admin
                password: secret
            """
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.mqtt is not None
    assert config.mqtt.tls is True
    assert config.mqtt.ca_cert == str(tmp_path / "certificates/ca.pem")
    assert config.mqtt.server_cert is None


def test_load_config_rejects_both_mqtt_certificate_options(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            """
            mqtt:
              host: mqtt.local
              server_cert: broker.pem
              ca_cert: ca.pem
            cameras:
              - name: Side Gate
                host: 192.0.2.12
                username: admin
                password: secret
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cannot both be set"):
        load_config(path)


def test_missing_environment_variable_is_reported(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "mqtt:\n  host: '${MISSING_BROKER}'\ncameras: []\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="MISSING_BROKER"):
        load_config(path)


def test_config_rejects_empty_camera_list(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("mqtt:\n  host: localhost\ncameras: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="non-empty list"):
        load_config(path)


def test_load_config_parses_recent_recordings_lookback_minutes(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            """
            recent_recordings_lookback_minutes: 90
            mqtt:
              host: mqtt.local
            cameras:
              - name: Side Gate
                host: 192.0.2.12
                username: admin
                password: secret
            """
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.recent_recordings_lookback_minutes == 90


@pytest.mark.parametrize("lookback", [0, -1, "invalid"])
def test_config_rejects_invalid_recent_recordings_lookback_minutes(tmp_path, lookback):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            f"""
            recent_recordings_lookback_minutes: {lookback}
            mqtt:
              host: mqtt.local
            cameras:
              - name: Side Gate
                host: 192.0.2.12
                username: admin
                password: secret
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="recent_recordings_lookback_minutes"):
        load_config(path)


def test_load_config_parses_udp_recovery_settings(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            """
            mqtt:
              host: localhost
            cameras:
              - name: Garden
                host: 192.0.2.20
                username: admin
                password: secret
                uid: "${CAM_UID}"
                udp: true
                local_port: 44231
                retry_attempts: 5
                retry_wait_seconds: 15
                connection_timeout_seconds: 45
            """
        ),
        encoding="utf-8",
    )

    import os
    os.environ["CAM_UID"] = "ABCD1234"
    config = load_config(path)

    camera = config.cameras[0]
    assert camera.uid == "ABCD1234"
    assert camera.use_udp is True
    assert camera.udp_local_port == 44231
    assert camera.retry_attempts == 5
    assert camera.retry_wait_seconds == 15.0
    assert camera.connection_timeout_seconds == 45.0


def test_config_rejects_ephemeral_udp_local_port(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            """
            mqtt:
              host: localhost
            cameras:
              - name: Garden
                host: 192.0.2.20
                username: admin
                password: secret
                udp: true
                udp_local_port: 0
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="udp_local_port"):
        load_config(path)


def test_stdout_mode_config_does_not_require_mqtt_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMERA_HOST", "192.0.2.15")
    monkeypatch.setenv("CAMERA_PASSWORD", "secret")
    monkeypatch.delenv("MQTT_HOST", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            """
            mqtt:
              host: "${MQTT_HOST}"
            cameras:
              - name: Front Door
                host: "${CAMERA_HOST}"
                username: admin
                password: "${CAMERA_PASSWORD}"
            """
        ),
        encoding="utf-8",
    )

    config = load_config(path, require_mqtt=False)

    assert config.mqtt is None
    assert config.cameras[0].host == "192.0.2.15"
