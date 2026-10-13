from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest
from dotenv import load_dotenv

from reolink_monitor.config import load_config
from reolink_monitor.monitor import BatteryMonitor

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


class RecordingPublisher:
    def __init__(self) -> None:
        self.messages: dict[str, str] = {}

    def publish(self, topic: str, payload: str, retain: bool = True) -> None:
        del retain
        self.messages[topic] = payload


@pytest.mark.asyncio
async def test_first_configured_real_camera_poll(caplog):
    if not CONFIG_PATH.is_file():
        pytest.skip("config.yaml is not present; skipping real-camera integration test")

    load_dotenv()
    config = load_config(CONFIG_PATH, require_mqtt=False)
    camera = config.cameras[0]
    publisher = RecordingPublisher()
    monitor = BatteryMonitor(config, publisher)
    caplog.set_level(logging.DEBUG, logger="reolink_monitor.monitor")

    await monitor.poll_camera(camera)

    identity = re.sub(r"[^a-z0-9]+", "_", camera.name.lower()).strip("_")
    state_root = f"{config.state_prefix}/{identity}_ch{camera.channel}"
    assert publisher.messages[f"{state_root}/availability"] == "online"
    assert f"{state_root}/battery" in publisher.messages
    assert f"{state_root}/battery_status" in publisher.messages
    assert f"{state_root}/recent_recordings" in publisher.messages
    assert f"{state_root}/recent_recordings/attributes" in publisher.messages
    assert "searching recordings" in caplog.text
    assert "recording search returned" in caplog.text or "Could not list recent recordings" in caplog.text

    recordings = json.loads(publisher.messages[f"{state_root}/recent_recordings/attributes"])
    assert isinstance(recordings["recordings"], list)
    assert 0 < len(recordings["recordings"]) <= 10, (
        f"{camera.name} returned no recordings in the last 24 hours; "
        f"monitor logs: {caplog.text}"
    )
