"""Configuration loading and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class CameraConfig:
    name: str
    host: str
    username: str
    password: str = field(repr=False)
    port: int = 9000
    channel: int = 0
    uid: str | None = None
    use_udp: bool = False
    udp_local_port: int = 44230
    retry_attempts: int = 3
    retry_wait_seconds: float = 20.0
    connection_timeout_seconds: float = 60.0


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    client_id: str = "reolink_monitor"
    tls: bool = False
    server_cert: str | None = None
    ca_cert: str | None = None


@dataclass(frozen=True)
class AppConfig:
    mqtt: MqttConfig | None
    cameras: tuple[CameraConfig, ...]
    poll_interval_seconds: int = 300
    discovery_prefix: str = "homeassistant"
    state_prefix: str = "reolink_monitor"
    recent_recordings_lookback_minutes: int = 1440


def _expand_environment(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            variable = match.group(1)
            if variable not in os.environ:
                raise ValueError(f"Required environment variable {variable} is not set")
            return os.environ[variable]

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_expand_environment(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_environment(item) for key, item in value.items()}
    return value


def _require_mapping(value: Any, section: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"Configuration section '{section}' must be a mapping")
    return value


def _require_nonempty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{field_name}' must be a non-empty string")
    return value.strip()


def _integer(value: Any, field_name: str, minimum: int) -> int:
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"'{field_name}' must be an integer greater than or equal to {minimum}")
    return value


def _float(value: Any, field_name: str, minimum: float) -> float:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError as exc:  # pragma: no cover - reached by invalid YAML likely
            raise ValueError(f"'{field_name}' must be a number greater than or equal to {minimum}") from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ValueError(f"'{field_name}' must be a number greater than or equal to {minimum}")
    return float(value)


def _boolean(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "on"}:
            return True
        if lowered in {"false", "no", "0", "off"}:
            return False
    raise ValueError(f"'{field_name}' must be a boolean")


def load_config(path: str | Path, *, require_mqtt: bool = True) -> AppConfig:
    """Load YAML configuration, resolving required environment variable references."""
    with Path(path).open(encoding="utf-8") as config_file:
        raw = yaml.safe_load(config_file)
    raw = _require_mapping(raw, "root")
    mqtt_data = raw.get("mqtt")
    if not require_mqtt:
        raw = {key: value for key, value in raw.items() if key != "mqtt"}
    raw = _expand_environment(raw)

    mqtt: MqttConfig | None = None
    if require_mqtt:
        mqtt_data = _require_mapping(_expand_environment(mqtt_data), "mqtt")
        server_cert = mqtt_data.get("server_cert")
        if server_cert is not None:
            server_cert = _require_nonempty_string(server_cert, "mqtt.server_cert")
            server_cert_path = Path(server_cert)
            if not server_cert_path.is_absolute():
                server_cert_path = Path(path).resolve().parent / server_cert_path
            server_cert = str(server_cert_path)
        ca_cert = mqtt_data.get("ca_cert")
        if ca_cert is not None:
            ca_cert = _require_nonempty_string(ca_cert, "mqtt.ca_cert")
            ca_cert_path = Path(ca_cert)
            if not ca_cert_path.is_absolute():
                ca_cert_path = Path(path).resolve().parent / ca_cert_path
            ca_cert = str(ca_cert_path)
        if server_cert and ca_cert:
            raise ValueError("'mqtt.server_cert' and 'mqtt.ca_cert' cannot both be set")
        mqtt = MqttConfig(
            host=_require_nonempty_string(mqtt_data.get("host"), "mqtt.host"),
            port=_integer(mqtt_data.get("port", 1883), "mqtt.port", 1),
            username=mqtt_data.get("username") or None,
            password=(mqtt_data.get("password") or None),
            client_id=_require_nonempty_string(
                mqtt_data.get("client_id", "reolink_monitor"), "mqtt.client_id"
            ),
            tls=_boolean(mqtt_data.get("tls", False), "mqtt.tls"),
            server_cert=server_cert,
            ca_cert=ca_cert,
        )
        if mqtt.port > 65535:
            raise ValueError("'mqtt.port' must not exceed 65535")

    camera_values = raw.get("cameras")
    if not isinstance(camera_values, list) or not camera_values:
        raise ValueError("'cameras' must be a non-empty list")
    cameras: list[CameraConfig] = []
    for index, value in enumerate(camera_values):
        camera = _require_mapping(value, f"cameras[{index}]")
        uid = camera.get("uid")
        if uid is not None:
            uid = _require_nonempty_string(uid, f"cameras[{index}].uid")
        use_udp = _boolean(camera.get("udp", False), f"cameras[{index}].udp")
        udp_local_port = camera.get("udp_local_port", camera.get("local_port", 44230))
        retry_attempts = camera.get("retry_attempts", 3)
        retry_wait_seconds = camera.get("retry_wait_seconds", 20.0)
        connection_timeout_seconds = camera.get("connection_timeout_seconds", 60.0)
        udp_local_port = _integer(
            udp_local_port, f"cameras[{index}].udp_local_port", 0
        )
        if use_udp and udp_local_port == 0:
            raise ValueError(
                f"'cameras[{index}].udp_local_port' must be a fixed port greater than 0"
            )
        cameras.append(
            CameraConfig(
                name=_require_nonempty_string(camera.get("name"), f"cameras[{index}].name"),
                host=_require_nonempty_string(camera.get("host"), f"cameras[{index}].host"),
                username=_require_nonempty_string(
                    camera.get("username"), f"cameras[{index}].username"
                ),
                password=_require_nonempty_string(
                    camera.get("password"), f"cameras[{index}].password"
                ),
                port=_integer(camera.get("port", 9000), f"cameras[{index}].port", 1),
                channel=_integer(camera.get("channel", 0), f"cameras[{index}].channel", 0),
                uid=uid,
                use_udp=use_udp,
                udp_local_port=udp_local_port,
                retry_attempts=_integer(retry_attempts, f"cameras[{index}].retry_attempts", 1),
                retry_wait_seconds=_float(
                    retry_wait_seconds, f"cameras[{index}].retry_wait_seconds", 0.0
                ),
                connection_timeout_seconds=_float(
                    connection_timeout_seconds,
                    f"cameras[{index}].connection_timeout_seconds",
                    0.0,
                ),
            )
        )
        if cameras[-1].port > 65535:
            raise ValueError(f"'cameras[{index}].port' must not exceed 65535")
        if cameras[-1].udp_local_port > 65535:
            raise ValueError(f"'cameras[{index}].udp_local_port' must not exceed 65535")

    poll_interval = _integer(
        raw.get("poll_interval_seconds", 300), "poll_interval_seconds", 1
    )
    recent_recordings_lookback_minutes = _integer(
        raw.get("recent_recordings_lookback_minutes", 1440),
        "recent_recordings_lookback_minutes",
        1,
    )
    discovery_prefix = _require_nonempty_string(
        raw.get("discovery_prefix", "homeassistant"), "discovery_prefix"
    )
    state_prefix = _require_nonempty_string(
        raw.get("state_prefix", "reolink_monitor"), "state_prefix"
    )
    return AppConfig(
        mqtt=mqtt,
        cameras=tuple(cameras),
        poll_interval_seconds=poll_interval,
        recent_recordings_lookback_minutes=recent_recordings_lookback_minutes,
        discovery_prefix=discovery_prefix.strip("/"),
        state_prefix=state_prefix.strip("/"),
    )
