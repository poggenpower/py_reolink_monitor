from __future__ import annotations

import json
import logging
import sys
import textwrap

import pytest

from reolink_monitor import main


def test_debug_flag_enables_debug_for_app_and_reolink_aio(monkeypatch):
    async def fake_run(_config_path, _mode, *, mqtt_insecure=False):
        return None

    monkeypatch.setattr(main, "_run", fake_run)
    monkeypatch.setattr(sys, "argv", ["reolink-monitor", "--debug"])
    monkeypatch.setattr(main.asyncio, "run", lambda coroutine: coroutine.close())

    main.main()

    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("reolink_monitor").getEffectiveLevel() == logging.DEBUG
    assert logging.getLogger("reolink_aio").getEffectiveLevel() == logging.DEBUG
    assert logging.getLogger("reolink_aio.api").getEffectiveLevel() == logging.DEBUG


def test_mqtt_insecure_flag_is_passed_to_run(monkeypatch):
    received = {}

    async def fake_run(_config_path, _mode, *, mqtt_insecure=False):
        received["mqtt_insecure"] = mqtt_insecure

    monkeypatch.setattr(main, "_run", fake_run)
    monkeypatch.setattr(sys, "argv", ["reolink-monitor", "--mqtt-insecure"])

    main.main()

    assert received["mqtt_insecure"] is True


@pytest.mark.asyncio
async def test_stdout_mode_runs_without_mqtt_config_or_connection(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            cameras:
              - name: Front Door
                host: 192.0.2.10
                username: admin
                password: secret
            """
        ),
        encoding="utf-8",
    )

    class FakeMonitor:
        def __init__(self, config, publisher):
            assert config.mqtt is None
            self.publisher = publisher

        async def run(self):
            self.publisher.publish("reolink_monitor/front_door_ch0/battery", "50")

    monkeypatch.setattr(main, "BatteryMonitor", FakeMonitor)

    await main._run(config_path, "stdout")

    output = json.loads(capsys.readouterr().out)
    assert output["topic"] == "reolink_monitor/front_door_ch0/battery"
    assert output["payload"] == "50"
