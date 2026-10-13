from __future__ import annotations

import json

from reolink_monitor.stdout import StdoutPublisher


def test_stdout_publisher_writes_json_discovery_and_string_state(capsys):
    publisher = StdoutPublisher()

    publisher.publish("homeassistant/sensor/camera/config", '{"name":"Camera Battery"}')
    publisher.publish("reolink_monitor/camera/battery", "76")

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records == [
        {
            "topic": "homeassistant/sensor/camera/config",
            "payload": {"name": "Camera Battery"},
            "retain": True,
        },
        {
            "topic": "reolink_monitor/camera/battery",
            "payload": "76",
            "retain": True,
        },
    ]
