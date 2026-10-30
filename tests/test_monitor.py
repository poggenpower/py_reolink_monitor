from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from xml.etree import ElementTree as XML

import pytest

from reolink_monitor import monitor as monitor_module
from reolink_monitor.config import AppConfig, CameraConfig, MqttConfig
from reolink_monitor.monitor import (
    _FILE_INFO_PAGE_SIZE,
    BatteryMonitor,
    _pin_udp_port,
)


class FakePublisher:
    def __init__(self):
        self.messages: list[tuple[str, str, bool]] = []

    def publish(self, topic: str, payload: str, retain: bool = True) -> None:
        self.messages.append((topic, payload, retain))


class FakeHost:
    def __init__(
        self,
        *,
        percentage: int | None = 76,
        status: int = 1,
        fail: bool = False,
        pages: list[str] | None = None,
        error: Exception | None = None,
    ):
        self.percentage = percentage
        self.status = status
        self.fail = fail
        self.logged_out = False
        self.baichuan = FakeBaichuan(pages=pages, error=error)

    async def get_host_data(self):
        if self.fail:
            raise ConnectionError("camera unreachable")

    async def get_states(self):
        return None

    def battery_percentage(self, channel):
        assert channel == 0
        return self.percentage

    def battery_status(self, channel):
        assert channel == 0
        return self.status

    def timezone(self):
        return UTC

    def camera_uid(self, channel):
        assert channel == 0
        return "CAMERA_UID_0"

    async def logout(self):
        self.logged_out = True


class FakeBaichuan:
    def __init__(self, pages=None, error=None):
        self.pages = list(pages or [])
        self.error = error
        self.commands: list[tuple[int, str]] = []
        self._connection = None

    async def send(self, cmd_id, body=""):
        self.commands.append((cmd_id, body))
        if self.error:
            raise self.error
        if cmd_id == 14:
            return "<body><FileInfoList><FileInfo><handle>search-1</handle></FileInfo></FileInfoList></body>"
        if cmd_id == 15:
            if self.pages:
                return self.pages.pop(0)
            return "<body><FileInfoList /></body>"
        return "<body />"


class FakeUdpConnection:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def file_info_page(recordings):
    root = XML.Element("body")
    file_info_list = XML.SubElement(root, "FileInfoList", version="1.1")
    for recording in recordings:
        file_info = XML.SubElement(file_info_list, "FileInfo")
        XML.SubElement(file_info, "Id").text = recording.file_name
        XML.SubElement(file_info, "recordType").text = recording.trigger
        for tag, value in (("startTime", recording.start_time), ("endTime", recording.end_time)):
            time_element = XML.SubElement(file_info, tag)
            for field, field_value in (
                ("year", value.year),
                ("month", value.month),
                ("day", value.day),
                ("hour", value.hour),
                ("minute", value.minute),
                ("second", value.second),
            ):
                XML.SubElement(time_element, field).text = str(field_value)
    return XML.tostring(root, encoding="unicode")


def configured_monitor(host: FakeHost) -> tuple[BatteryMonitor, FakePublisher]:
    config = AppConfig(
        mqtt=MqttConfig(host="localhost"),
        cameras=(
            CameraConfig(
                name="Garden",
                host="192.0.2.10",
                username="admin",
                password="x",
                retry_attempts=1,
            ),
        ),
    )
    publisher = FakePublisher()
    fake_host = host

    def host_factory(
        host: str,
        username: str,
        password: str,
        *,
        port: int,
    ) -> FakeHost:
        assert (host, username, password, port) == (
            "192.0.2.10",
            "admin",
            "x",
            9000,
        )
        return fake_host

    monitor = BatteryMonitor(
        config,
        publisher,
        host_factory=host_factory,
    )
    return monitor, publisher


def test_udp_connection_uses_each_camera_fixed_local_port(monkeypatch):
    import reolink_aio.baichuan.baichuan as bc_module

    class FakeUdpConnection:
        def __init__(self, host, port=0, *args, **kwargs):
            self.host = host
            self.port = port

    monkeypatch.setattr(bc_module, "BaichuanUdpConnection", FakeUdpConnection)
    monkeypatch.setattr(monitor_module, "_UDP_CONNECTION_PATCHED", False)

    _pin_udp_port()
    patched_class = bc_module.BaichuanUdpConnection
    _pin_udp_port()
    assert bc_module.BaichuanUdpConnection is patched_class

    ports = []
    for configured_port in (44230, 44231):
        token = monitor_module._UDP_LOCAL_PORT.set(configured_port)
        try:
            ports.append(bc_module.BaichuanUdpConnection("camera", 0).port)
        finally:
            monitor_module._UDP_LOCAL_PORT.reset(token)

    assert ports == [44230, 44231]
    assert bc_module.BaichuanUdpConnection("camera", 12345).port == 12345


@pytest.mark.asyncio
async def test_failed_connection_closes_udp_socket_before_retry():
    host = FakeHost()
    connection = FakeUdpConnection()
    host.baichuan._connection = connection
    calls = 0

    async def get_host_data():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError()
        assert connection.closed

    host.get_host_data = get_host_data
    monitor, _ = configured_monitor(host)
    camera = monitor._config.cameras[0]
    camera = CameraConfig(
        name=camera.name,
        host=camera.host,
        username=camera.username,
        password=camera.password,
        port=camera.port,
        channel=camera.channel,
        retry_attempts=2,
        retry_wait_seconds=0,
    )

    connected_host = await monitor._connect_camera(camera)

    assert connected_host is host
    assert calls == 2
    assert connection.closed


@pytest.mark.asyncio
async def test_poll_publishes_battery_and_charging_status_and_logs_out():
    host = FakeHost()
    monitor, publisher = configured_monitor(host)

    await monitor.poll_camera(monitor._config.cameras[0])

    messages = {topic.rsplit("/", 1)[-1]: payload for topic, payload, _ in publisher.messages}
    assert messages == {
        "availability": "online",
        "battery": "76",
        "battery_status": "charging",
        "recent_recordings": "0",
        "attributes": '{"recordings": []}',
    }
    commands = host.baichuan.commands
    assert [cmd_id for cmd_id, _ in commands[:3]] == [14, 15, 16]
    assert [cmd_id for cmd_id, _ in commands[-3:]] == [14, 15, 16]
    open_query = XML.fromstring(host.baichuan.commands[0][1]).find(".//FileInfo")
    assert open_query is not None
    assert open_query.findtext("uid") == "CAMERA"
    assert open_query.findtext("channelId") == "0"
    assert open_query.findtext("streamType") == "mainStream"
    assert len(open_query.findall("./fileRecordType/i")) > 10
    assert XML.fromstring(host.baichuan.commands[1][1]).findtext(".//handle") == "search-1"
    assert XML.fromstring(host.baichuan.commands[2][1]).findtext(".//handle") == "search-1"
    assert host.logged_out


@pytest.mark.asyncio
async def test_missing_battery_reading_publishes_unknown():
    host = FakeHost(percentage=None)
    monitor, publisher = configured_monitor(host)

    await monitor.poll_camera(monitor._config.cameras[0])

    states = {
        topic.rsplit("/", 1)[-1]: payload
        for topic, payload, _ in publisher.messages
        if not topic.endswith("/attributes")
    }
    assert states["battery"] == "unknown"
    assert states["battery_status"] == "unknown"


@pytest.mark.asyncio
async def test_failed_camera_poll_marks_camera_offline():
    monitor, publisher = configured_monitor(FakeHost(fail=True))

    await monitor.poll_all()

    assert publisher.messages[-1][1] == "offline"


def test_discovery_announces_battery_entity_with_ha_device_metadata(caplog):
    monitor, publisher = configured_monitor(FakeHost())

    with caplog.at_level(logging.INFO, logger="reolink_monitor.monitor"):
        monitor.publish_discovery()

    assert "Home Assistant MQTT discovery for 1 cameras (3 entities)" in caplog.text
    battery_topic, battery_payload, retained = publisher.messages[0]
    discovery = json.loads(battery_payload)
    assert battery_topic == (
        "homeassistant/sensor/reolink_monitor/garden_ch0_battery/config"
    )
    assert retained
    assert discovery["device_class"] == "battery"
    assert discovery["unit_of_measurement"] == "%"
    assert discovery["unique_id"] == "reolink_monitor_garden_ch0_battery"
    assert discovery["state_topic"] == "reolink_monitor/garden_ch0/battery"
    assert discovery["device"]["manufacturer"] == "Reolink"
    assert discovery["availability_topic"] == "reolink_monitor/garden_ch0/availability"
    assert publisher.messages[1][0].endswith("garden_ch0_battery_status/config")
    recordings_discovery = json.loads(publisher.messages[2][1])
    assert recordings_discovery["name"] == "Garden Recent Recordings"
    assert recordings_discovery["state_topic"] == "reolink_monitor/garden_ch0/recent_recordings"
    assert recordings_discovery["json_attributes_topic"] == (
        "reolink_monitor/garden_ch0/recent_recordings/attributes"
    )


@pytest.mark.asyncio
async def test_poll_publishes_total_recording_count_and_latest_ten_details(caplog):
    now = datetime.now(UTC)
    recordings = [
        SimpleNamespace(
            file_name=f"clip_{index}.mp4",
            start_time=now - timedelta(minutes=52 - index),
            end_time=now - timedelta(minutes=52 - index) + timedelta(seconds=12 + index),
            trigger="people" if index % 2 else "other",
        )
        for index in range(_FILE_INFO_PAGE_SIZE + 12)
    ]
    # The camera repeats the current page handle and serves a maximum of 40 files per call.
    host = FakeHost(
        pages=[
            file_info_page(recordings[:_FILE_INFO_PAGE_SIZE]),
            file_info_page([*recordings[_FILE_INFO_PAGE_SIZE:], recordings[-1]]),
        ]
    )
    monitor, publisher = configured_monitor(host)

    with caplog.at_level(logging.DEBUG, logger="reolink_monitor.monitor"):
        await monitor.poll_camera(monitor._config.cameras[0])

    state = {topic.rsplit("/", 1)[-1]: payload for topic, payload, _ in publisher.messages}
    assert state["recent_recordings"] == str(len(recordings))
    assert "52 unique clips within the last 1440 minutes; publishing details for 10" in caplog.text
    assert "clip_51.mp4" in caplog.text
    assert "clip_41.mp4" not in caplog.text
    attributes = json.loads(state["attributes"])
    assert len(attributes["recordings"]) == 10
    assert attributes["recordings"][0]["file_name"] == "clip_51.mp4"
    assert attributes["recordings"][-1]["file_name"] == "clip_42.mp4"
    assert attributes["recordings"][0]["trigger"] == "people"
    command_ids = [cmd_id for cmd_id, _ in host.baichuan.commands]
    assert command_ids[:4] == [14, 15, 15, 16]
    assert command_ids[-3:] == [14, 15, 16]


@pytest.mark.asyncio
async def test_recording_search_uses_configured_lookback_window(monkeypatch):
    monitor, publisher = configured_monitor(FakeHost())
    monitor._config = AppConfig(
        mqtt=MqttConfig(host="localhost"),
        cameras=monitor._config.cameras,
        recent_recordings_lookback_minutes=37,
    )
    searched = {}

    async def search(_host, _camera, start, end):
        searched["start"] = start
        searched["end"] = end
        return []

    monkeypatch.setattr("reolink_monitor.monitor._search_file_info_recordings", search)
    await monitor._publish_recent_recordings(
        FakeHost(), monitor._config.cameras[0], "reolink_monitor/garden_ch0"
    )

    assert searched["end"] - searched["start"] == timedelta(minutes=37)
    assert publisher.messages[-2][1] == "0"


@pytest.mark.asyncio
async def test_recording_search_failure_does_not_hide_battery_readings():
    host = FakeHost(error=TimeoutError("recording search timed out"))
    monitor, publisher = configured_monitor(host)

    await monitor.poll_camera(monitor._config.cameras[0])

    states = {topic.rsplit("/", 1)[-1]: payload for topic, payload, _ in publisher.messages}
    assert states["availability"] == "online"
    assert states["battery"] == "76"
    assert states["recent_recordings"] == "unknown"
    assert json.loads(states["attributes"]) == {
        "recordings": [],
        "error": "recording search timed out",
    }
