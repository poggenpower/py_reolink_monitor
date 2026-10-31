from __future__ import annotations

import asyncio
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
    assert messages["battery"] == "76"
    assert messages["battery_status"] == "charging"
    assert messages["recent_recordings"] == "0"
    assert messages["attributes"] == '{"recordings": []}'
    scraped_at = datetime.fromisoformat(messages["scraped_at_iso"])
    assert scraped_at.tzinfo is not None
    assert scraped_at.utcoffset() == datetime.now().astimezone().utcoffset()
    assert int(scraped_at.timestamp()) == int(messages["scraped_at_unix"])
    assert all(not retained for _, _, retained in publisher.messages)
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
async def test_one_time_lookback_is_used_for_recording_search(monkeypatch):
    monitor, _publisher = configured_monitor(FakeHost())
    camera = monitor._config.cameras[0]
    search_windows = []

    async def search(_host, _camera, start, end):
        search_windows.append(end - start)
        return []

    monkeypatch.setattr(monitor_module, "_search_file_info_recordings", search)
    monitor._one_time_lookback = 37
    await monitor._publish_recent_recordings(
        FakeHost(),
        camera,
        "reolink_monitor/garden_ch0",
        monitor._one_time_lookback,
    )

    assert search_windows == [timedelta(minutes=37)]


def test_one_time_lookback_requires_positive_integer():
    monitor, publisher = configured_monitor(FakeHost())
    monitor._one_time_lookback = 1440

    assert monitor._set_one_time_lookback("60") == 60
    assert monitor._one_time_lookback == 60
    assert publisher.messages[0] == (
        "reolink_monitor/bridge/onetime_lookback_minutes/state",
        "60",
        True,
    )
    assert monitor._set_one_time_lookback("1.5") is None
    assert monitor._one_time_lookback == 60
    assert monitor._set_one_time_lookback("0") is None
    assert monitor._set_one_time_lookback("525601") is None


@pytest.mark.asyncio
async def test_scrape_publishes_final_status_and_event(monkeypatch):
    monitor, publisher = configured_monitor(FakeHost())
    run = monitor_module.ScrapeRun(
        run_id="run-1",
        trigger="both",
        lookback_minutes=60,
        started_at=datetime.now().astimezone(),
    )

    async def poll_all(lookback_minutes):
        assert lookback_minutes == 60
        return ("Garden",)

    monkeypatch.setattr(monitor, "poll_all", poll_all)
    await monitor._run_scrape(run)

    messages = {topic: (payload, retained) for topic, payload, retained in publisher.messages}
    assert messages["reolink_monitor/bridge/scrape/state"] == ("failed", True)
    event, retained = messages["reolink_monitor/bridge/scrape/finished"]
    assert not retained
    result = json.loads(event)
    assert result["event_type"] == "failed"
    assert result["status"] == "failed"
    assert result["trigger"] == "both"
    assert result["run_id"] == "run-1"
    assert result["lookback_minutes"] == 60
    assert result["failed_cameras"] == ["Garden"]


@pytest.mark.asyncio
async def test_button_during_interval_marks_current_scrape_both_without_overlap(monkeypatch):
    monitor, publisher = configured_monitor(FakeHost())
    command_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    scrape_started = asyncio.Event()
    trigger_updated = asyncio.Event()
    scrape_finished = asyncio.Event()
    release_scrape = asyncio.Event()
    poll_count = 0
    original_publish = monitor._publish_scrape_status
    original_publisher_publish = publisher.publish

    def publish_status(status, attributes):
        original_publish(status, attributes)
        if status == "running" and attributes.get("trigger") == "both":
            trigger_updated.set()

    def publish(topic, payload, retain=True):
        original_publisher_publish(topic, payload, retain)
        if topic == "reolink_monitor/bridge/scrape/finished":
            scrape_finished.set()

    async def poll_all(_lookback_minutes):
        nonlocal poll_count
        poll_count += 1
        scrape_started.set()
        await release_scrape.wait()
        return ()

    monkeypatch.setattr(monitor, "_publish_scrape_status", publish_status)
    monkeypatch.setattr(publisher, "publish", publish)
    monkeypatch.setattr(monitor, "poll_all", poll_all)
    run_task = asyncio.create_task(monitor.run(command_queue))
    try:
        await asyncio.wait_for(scrape_started.wait(), timeout=1)
        command_queue.put_nowait(("reolink_monitor/bridge/scrape/trigger", "PRESS"))
        await asyncio.wait_for(trigger_updated.wait(), timeout=1)
        release_scrape.set()
        await asyncio.wait_for(scrape_finished.wait(), timeout=1)
    finally:
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

    event = next(
        json.loads(payload)
        for topic, payload, _retained in publisher.messages
        if topic == "reolink_monitor/bridge/scrape/finished"
    )
    assert poll_count == 1
    assert event["trigger"] == "both"


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

    assert publisher.messages == []


def test_discovery_announces_battery_entity_with_ha_device_metadata(caplog):
    monitor, publisher = configured_monitor(FakeHost())

    with caplog.at_level(logging.INFO, logger="reolink_monitor.monitor"):
        monitor.publish_discovery()

    assert (
        "Home Assistant MQTT discovery for Reolink Bridge and 1 camera devices (12 entities total)"
        in caplog.text
    )
    bridge_topic, bridge_payload, retained = publisher.messages[0]
    bridge = json.loads(bridge_payload)
    assert bridge_topic == ("homeassistant/binary_sensor/reolink_monitor/bridge_status/config")
    assert retained
    assert bridge["name"] == "Status"
    assert bridge["unique_id"] == "reolink_monitor_bridge_status"
    assert bridge["state_topic"] == "reolink_monitor/bridge/availability"
    assert bridge["payload_on"] == "online"
    assert bridge["payload_off"] == "offline"
    assert bridge["device_class"] == "connectivity"
    assert bridge["entity_category"] == "diagnostic"
    assert bridge["device"] == {
        "identifiers": ["reolink_monitor_bridge"],
        "manufacturer": "Reolink",
        "model": "Reolink Monitor",
        "name": "Reolink Bridge",
    }

    battery_topic, battery_payload, retained = publisher.messages[1]
    discovery = json.loads(battery_payload)
    assert battery_topic == ("homeassistant/sensor/reolink_monitor/garden_ch0_battery/config")
    assert retained
    assert discovery["device_class"] == "battery"
    assert discovery["unit_of_measurement"] == "%"
    assert discovery["unique_id"] == "reolink_monitor_garden_ch0_battery"
    assert discovery["state_topic"] == ("reolink_monitor/camera/garden_ch0/state/battery")
    assert discovery["device"]["manufacturer"] == "Reolink"
    assert discovery["device"]["via_device"] == "reolink_monitor_bridge"
    assert discovery["availability_topic"] == "reolink_monitor/bridge/availability"
    assert publisher.messages[2][0].endswith("garden_ch0_battery_status/config")
    battery_status_discovery = json.loads(publisher.messages[2][1])
    assert battery_status_discovery["device"]["via_device"] == "reolink_monitor_bridge"
    recordings_discovery = json.loads(publisher.messages[3][1])
    assert recordings_discovery["name"] == "Garden Recent Recordings"
    assert recordings_discovery["device"]["via_device"] == "reolink_monitor_bridge"
    camera_state_root = "reolink_monitor/camera/garden_ch0/state"
    assert recordings_discovery["state_topic"] == (f"{camera_state_root}/recent_recordings")
    assert recordings_discovery["json_attributes_topic"] == (
        f"{camera_state_root}/recent_recordings/attributes"
    )
    discovery = {topic: json.loads(payload) for topic, payload, _ in publisher.messages}
    scraped_at = discovery["homeassistant/sensor/reolink_monitor/garden_ch0_scraped_at_iso/config"]
    assert scraped_at["state_topic"] == f"{camera_state_root}/scraped_at_iso"
    assert scraped_at["device_class"] == "timestamp"
    scraped_at_unix = discovery[
        "homeassistant/sensor/reolink_monitor/garden_ch0_scraped_at_unix/config"
    ]
    assert scraped_at_unix["state_topic"] == f"{camera_state_root}/scraped_at_unix"
    assert scraped_at_unix["unit_of_measurement"] == "s"

    metadata = discovery["homeassistant/sensor/reolink_monitor/poll_interval_seconds/config"]
    assert metadata["state_topic"] == ("reolink_monitor/bridge/metadata/poll_interval_seconds")
    assert (
        "homeassistant/sensor/reolink_monitor/recent_recordings_lookback_minutes/config"
    ) in discovery

    lookback = discovery["homeassistant/number/reolink_monitor/onetime_lookback/config"]
    assert lookback["state_topic"] == ("reolink_monitor/bridge/onetime_lookback_minutes/state")
    assert lookback["command_topic"] == ("reolink_monitor/bridge/onetime_lookback_minutes/set")
    assert lookback["retain"] is False
    assert lookback["min"] == 1
    assert lookback["max"] == 525600
    button = discovery["homeassistant/button/reolink_monitor/scrape_now/config"]
    assert button["command_topic"] == "reolink_monitor/bridge/scrape/trigger"
    assert button["retain"] is False
    assert button["payload_press"] == "PRESS"
    status = discovery["homeassistant/sensor/reolink_monitor/scrape_status/config"]
    assert status["state_topic"] == "reolink_monitor/bridge/scrape/state"
    assert status["json_attributes_topic"] == ("reolink_monitor/bridge/scrape/attributes")
    scrape_event = discovery["homeassistant/event/reolink_monitor/scrape_finished/config"]
    assert scrape_event["state_topic"] == ("reolink_monitor/bridge/scrape/finished")
    assert scrape_event["event_types"] == [
        "completed",
        "partial",
        "failed",
        "cancelled",
    ]
    assert publisher.messages[0][2]
    assert all(retained for _topic, _payload, retained in publisher.messages)


def test_discovery_publishes_each_camera_under_its_own_ha_device():
    monitor, publisher = configured_monitor(FakeHost())
    monitor._config = AppConfig(
        mqtt=monitor._config.mqtt,
        cameras=(
            *monitor._config.cameras,
            CameraConfig(
                name="Back Yard",
                host="192.0.2.11",
                username="admin",
                password="x",
                channel=1,
            ),
        ),
    )

    monitor.publish_discovery()

    discovery = {topic: json.loads(payload) for topic, payload, _ in publisher.messages}
    for key in (
        "battery",
        "battery_status",
        "recent_recordings",
        "scraped_at_iso",
        "scraped_at_unix",
    ):
        topic = f"homeassistant/sensor/reolink_monitor/back_yard_ch1_{key}/config"
        assert topic in discovery
        assert discovery[topic]["state_topic"].startswith(
            "reolink_monitor/camera/back_yard_ch1/state/"
        )
        assert discovery[topic]["device"]["via_device"] == "reolink_monitor_bridge"


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
    assert all(not retained for _, _, retained in publisher.messages)
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
    assert all(not retained for _, _, retained in publisher.messages)


@pytest.mark.asyncio
async def test_recording_search_failure_does_not_hide_battery_readings():
    host = FakeHost(error=TimeoutError("recording search timed out"))
    monitor, publisher = configured_monitor(host)

    await monitor.poll_camera(monitor._config.cameras[0])

    states = {topic.rsplit("/", 1)[-1]: payload for topic, payload, _ in publisher.messages}
    assert states["battery"] == "76"
    assert states["recent_recordings"] == "unknown"
    assert json.loads(states["attributes"]) == {
        "recordings": [],
        "error": "recording search timed out",
    }
    assert all(not retained for _, _, retained in publisher.messages)


def test_publish_metadata_retains_configured_intervals():
    monitor, publisher = configured_monitor(FakeHost())
    monitor._config = AppConfig(
        mqtt=MqttConfig(host="localhost"),
        cameras=monitor._config.cameras,
        poll_interval_seconds=45,
        recent_recordings_lookback_minutes=180,
    )

    monitor.publish_metadata()

    assert publisher.messages == [
        (
            "reolink_monitor/bridge/metadata/recent_recordings_lookback_minutes",
            "180",
            True,
        ),
        ("reolink_monitor/bridge/metadata/poll_interval_seconds", "45", True),
    ]
