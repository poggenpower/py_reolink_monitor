"""Poll Reolink cameras and publish battery data and discovery to MQTT."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone, tzinfo
from typing import Protocol
from uuid import uuid4
from xml.etree import ElementTree as XML

from reolink_aio.api import Host
from reolink_aio.const import UNKNOWN
from reolink_aio.enums import ConnectionEnum
from reolink_aio.exceptions import ReolinkError, UnexpectedDataError

from .config import RECENT_RECORDING_TYPES, AppConfig, CameraConfig

_LOGGER = logging.getLogger(__name__)
_BATTERY_STATUS = {0: "discharging", 1: "charging", 2: "charged"}
_UDP_LOCAL_PORT: ContextVar[int | None] = ContextVar("baichuan_udp_local_port", default=None)
_UDP_CONNECTION_PATCHED = False
_RECENT_RECORDING_LIMIT = 10
_RECORDING_STREAM = "main"
_BRIDGE_IDENTIFIER = "reolink_monitor_bridge"
_CMD_GET_SYSTEM_GENERAL = 104
_FILE_INFO_PAGE_SIZE = 40
_FILE_INFO_MAX_PAGES = 50
_FILE_RECORD_TYPES = (
    "manual",
    "sched",
    "io",
    "md",
    "people",
    "face",
    "vehicle",
    "dog_cat",
    "visitor",
    "other",
    "package",
    "crossline",
    "intrusion",
    "loitering",
    "legacy",
    "loss",
)
_FILE_RECORD_OPTIONS = (
    *_FILE_RECORD_TYPES,
    "answer",
    "nonmotorveh",
    "IPCInputOutput",
)
_RECORDING_TYPE_TRIGGERS = {
    "person": {"person", "people", "face"},
    "other": {"other"},
    "vehicle": {"vehicle", "nonmotorveh"},
    "animal": {"animal", "dog_cat", "dog", "cat"},
}


class Publisher(Protocol):
    def publish(self, topic: str, payload: str, retain: bool = True) -> None: ...


class BaichuanConnection(Protocol):
    async def close(self) -> None: ...


class BaichuanApi(Protocol):
    _connection: BaichuanConnection | None

    async def send(self, cmd_id: int, body: str = "") -> str: ...


class CameraHost(Protocol):
    baichuan: BaichuanApi

    async def get_host_data(self) -> None: ...

    async def get_states(self) -> None: ...

    def timezone(self) -> tzinfo | None: ...

    def camera_uid(self, channel: int) -> str: ...

    def camera_model(self, channel: int | None) -> str: ...

    def battery_percentage(self, channel: int) -> int | None: ...

    def battery_status(self, channel: int) -> int: ...

    async def logout(self) -> None: ...


class RecentRecording:
    def __init__(
        self, file_name: str, start_time: datetime, end_time: datetime, trigger: str
    ) -> None:
        self.file_name = file_name
        self.start_time = start_time
        self.end_time = end_time
        self.trigger = trigger

    @property
    def duration(self) -> timedelta:
        return self.end_time - self.start_time


@dataclass(frozen=True)
class CameraClock:
    device_time: datetime
    server_time_utc: datetime
    timezone_setting: str | None
    utc_offset_seconds: int
    timezone: tzinfo


@dataclass
class ScrapeRun:
    run_id: str
    trigger: str
    lookback_minutes: int
    started_at: datetime
    recording_types: tuple[str, ...] = ()


def _recording_search_xml(
    channel: int,
    uid: str,
    start: datetime | None = None,
    end: datetime | None = None,
    handle: str | None = None,
) -> str:
    root = XML.Element("body")
    file_info_list = XML.SubElement(root, "FileInfoList", version="1.1")
    file_info = XML.SubElement(file_info_list, "FileInfo")

    if handle is not None:
        XML.SubElement(file_info, "channelId").text = str(channel)
        XML.SubElement(file_info, "uid").text = uid
        XML.SubElement(file_info, "searchAITrack").text = "1"
        XML.SubElement(file_info, "handle").text = handle
    else:
        if start is None or end is None:
            raise ValueError("start and end are required to open a recording search")
        XML.SubElement(file_info, "uid").text = uid
        XML.SubElement(file_info, "searchAITrack").text = "1"
        XML.SubElement(file_info, "channelId").text = str(channel)
        XML.SubElement(file_info, "logicChnBitmap").text = "255"
        XML.SubElement(file_info, "streamType").text = "mainStream"
        XML.SubElement(file_info, "recordType").text = ", ".join(_FILE_RECORD_TYPES)
        file_record_type = XML.SubElement(file_info, "fileRecordType")
        for record_type in _FILE_RECORD_OPTIONS:
            XML.SubElement(file_record_type, "i").text = record_type
        for tag, value in (("startTime", start), ("endTime", end)):
            assert value is not None
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

    return XML.tostring(root, encoding="unicode", xml_declaration=True)


def _parse_recording_time(element: XML.Element | None, timezone: tzinfo | None) -> datetime:
    if element is None:
        raise UnexpectedDataError("Baichuan FileInfo response is missing a recording time")
    try:
        value = datetime(
            year=int(element.findtext("year", "")),
            month=int(element.findtext("month", "")),
            day=int(element.findtext("day", "")),
            hour=int(element.findtext("hour", "")),
            minute=int(element.findtext("minute", "")),
            second=int(element.findtext("second", "")),
            tzinfo=timezone,
        )
    except ValueError as err:
        raise UnexpectedDataError("Baichuan FileInfo response contains an invalid recording time") from err
    return value


def _parse_recording_page(response: str, timezone: tzinfo | None) -> list[RecentRecording]:
    try:
        root = XML.fromstring(response)
    except XML.ParseError as err:
        raise UnexpectedDataError("Baichuan returned malformed FileInfo XML") from err

    recordings = []
    for file_info in root.findall(".//FileInfo"):
        file_name = file_info.findtext("Id") or file_info.findtext("name")
        if not file_name:
            raise UnexpectedDataError("Baichuan FileInfo response is missing a recording identifier")
        start_time = _parse_recording_time(file_info.find("startTime"), timezone)
        end_time = _parse_recording_time(file_info.find("endTime"), timezone)
        if end_time < start_time:
            raise UnexpectedDataError("Baichuan FileInfo response has an end time before its start time")
        recordings.append(
            RecentRecording(
                file_name=file_name,
                start_time=start_time,
                end_time=end_time,
                trigger=file_info.findtext("recordType", "unknown").strip() or "unknown",
            )
        )
    return recordings


def _parse_camera_clock(response: str, server_time_utc: datetime) -> CameraClock:
    try:
        root = XML.fromstring(response)
    except XML.ParseError as err:
        raise UnexpectedDataError("Baichuan returned malformed SystemGeneral XML") from err

    system_general = root.find(".//SystemGeneral")
    if system_general is None:
        raise UnexpectedDataError("Baichuan SystemGeneral response is missing its settings")
    try:
        device_time = datetime(
            year=int(system_general.findtext("year", "")),
            month=int(system_general.findtext("month", "")),
            day=int(system_general.findtext("day", "")),
            hour=int(system_general.findtext("hour", "")),
            minute=int(system_general.findtext("minute", "")),
            second=int(system_general.findtext("second", "")),
        )
    except ValueError as err:
        raise UnexpectedDataError(
            "Baichuan SystemGeneral response contains an invalid camera time"
        ) from err

    offset_seconds = round(
        (device_time - server_time_utc.replace(tzinfo=None)).total_seconds() / 60
    ) * 60
    try:
        device_timezone = timezone(timedelta(seconds=offset_seconds))
    except ValueError as err:
        raise UnexpectedDataError(
            "Baichuan camera time differs from server time by an invalid timezone offset"
        ) from err

    return CameraClock(
        device_time=device_time,
        server_time_utc=server_time_utc,
        timezone_setting=system_general.findtext("timeZone"),
        utc_offset_seconds=offset_seconds,
        timezone=device_timezone,
    )


async def _search_file_info_recordings(
    host: CameraHost, camera: CameraConfig, start: datetime, end: datetime
) -> list[RecentRecording]:
    uid = host.camera_uid(camera.channel).split("_")[0]
    if not uid or uid == "Unknown":
        raise UnexpectedDataError(f"Could not determine Baichuan UID for {camera.name}")

    recordings_by_name: dict[str, RecentRecording] = {}
    day = start.date()
    while day <= end.date():
        day_start = datetime.combine(day, time.min, tzinfo=start.tzinfo)
        day_end = datetime.combine(day, time(23, 59, 59), tzinfo=start.tzinfo)
        open_xml = _recording_search_xml(camera.channel, uid, day_start, day_end)
        response = await host.baichuan.send(cmd_id=14, body=open_xml)
        handle = None
        page_xml = None
        try:
            try:
                root = XML.fromstring(response)
            except XML.ParseError as err:
                raise UnexpectedDataError("Baichuan returned malformed FileInfo search XML") from err
            handle = root.findtext(".//handle")
            if not handle:
                raise UnexpectedDataError("Baichuan FileInfo search response is missing its handle")

            page_xml = _recording_search_xml(camera.channel, uid, handle=handle)
            page_count = 0
            for page_count in range(1, _FILE_INFO_MAX_PAGES + 1):
                page = _parse_recording_page(
                    await host.baichuan.send(cmd_id=15, body=page_xml), start.tzinfo
                )
                for recording in page:
                    if recording.end_time < start or recording.start_time > end:
                        continue
                    recordings_by_name.setdefault(recording.file_name, recording)
                _LOGGER.debug(
                    "%s FileInfo page %d returned %d recordings",
                    camera.name,
                    page_count,
                    len(page),
                )
                if len(page) < _FILE_INFO_PAGE_SIZE:
                    break
            else:
                raise UnexpectedDataError(
                    f"Baichuan FileInfo search exceeded {_FILE_INFO_MAX_PAGES} pages"
                )
        finally:
            if handle is not None and page_xml is not None:
                try:
                    await host.baichuan.send(cmd_id=16, body=page_xml)
                except (ReolinkError, OSError, TimeoutError):
                    _LOGGER.warning(
                        "%s could not close Baichuan FileInfo search handle %s",
                        camera.name,
                        handle,
                        exc_info=True,
                    )
        day += timedelta(days=1)

    return list(recordings_by_name.values())


class HostFactory(Protocol):
    def __call__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int,
        **kwargs: object,
    ) -> CameraHost: ...


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _pin_udp_port() -> None:
    global _UDP_CONNECTION_PATCHED
    if _UDP_CONNECTION_PATCHED:
        return
    import reolink_aio.baichuan.baichuan as bc_module

    base = bc_module.BaichuanUdpConnection

    class FixedPortUdpConnection(base):  # type: ignore[misc, valid-type]
        def __init__(self, host, port=0, *args, **kwargs):
            local_port = _UDP_LOCAL_PORT.get()
            super().__init__(
                host, port if local_port is None else local_port, *args, **kwargs
            )

    bc_module.BaichuanUdpConnection = FixedPortUdpConnection
    _UDP_CONNECTION_PATCHED = True


class BatteryMonitor:
    def __init__(
        self,
        config: AppConfig,
        publisher: Publisher,
        host_factory: HostFactory = Host,
    ) -> None:
        self._config = config
        self._publisher = publisher
        self._host_factory = host_factory
        self._one_time_recording_types = config.recent_recordings_types
        self._camera_clocks: dict[str, CameraClock | None] = {}
        self._camera_models: dict[str, str | None] = {}

    def _identity(self, camera: CameraConfig) -> str:
        return f"{_slug(camera.name)}_ch{camera.channel}"

    def _camera_state_root(self, camera: CameraConfig) -> str:
        return (
            f"{self._config.state_prefix}/camera/"
            f"{self._identity(camera)}/state"
        )

    def publish_discovery(self) -> None:
        bridge_identifier = _BRIDGE_IDENTIFIER
        bridge_root = f"{self._config.state_prefix}/bridge"
        availability_topic = f"{bridge_root}/availability"
        _LOGGER.info(
            "Publishing Home Assistant MQTT discovery for Reolink Bridge and %d camera devices "
            "(%d entities total)",
            len(self._config.cameras),
            len(self._config.cameras) * 7 + 8,
        )
        bridge_topic = (
            f"{self._config.discovery_prefix}/binary_sensor/"
            "reolink_monitor/bridge_status/config"
        )
        bridge_device = {
            "identifiers": [bridge_identifier],
            "manufacturer": "Reolink",
            "model": "Reolink Monitor",
            "name": "Reolink Bridge",
        }
        bridge_payload = {
            "name": "Status",
            "unique_id": "reolink_monitor_bridge_status",
            "state_topic": availability_topic,
            "payload_on": "online",
            "payload_off": "offline",
            "device_class": "connectivity",
            "entity_category": "diagnostic",
            "device": bridge_device,
        }
        self._publisher.publish(bridge_topic, json.dumps(bridge_payload), retain=True)

        for camera in self._config.cameras:
            self._publish_camera_discovery(camera)

        controls_device = bridge_device
        metadata_root = f"{bridge_root}/metadata"
        for key, label, unit in (
            ("poll_interval_seconds", "Poll Interval", "s"),
            (
                "recent_recordings_lookback_minutes",
                "Recent Recordings Lookback",
                "min",
            ),
        ):
            self._publisher.publish(
                f"{self._config.discovery_prefix}/sensor/"
                f"reolink_monitor/{key}/config",
                json.dumps(
                    {
                        "name": label,
                        "unique_id": f"reolink_monitor_{key}",
                        "state_topic": f"{metadata_root}/{key}",
                        "unit_of_measurement": unit,
                        "entity_category": "diagnostic",
                        "device": controls_device,
                    }
                ),
                retain=True,
            )

        recording_types_root = f"{bridge_root}/onetime_recording_types"
        self._publisher.publish(
            f"{self._config.discovery_prefix}/text/"
            "reolink_monitor/onetime_recording_types/config",
            json.dumps(
                {
                    "name": "One-time Recording Types",
                    "unique_id": "reolink_monitor_onetime_recording_types",
                    "state_topic": f"{recording_types_root}/state",
                    "command_topic": f"{recording_types_root}/set",
                    "retain": False,
                    "min": 1,
                    "max": 40,
                    "mode": "text",
                    "availability_topic": availability_topic,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "device": controls_device,
                }
            ),
            retain=True,
        )
        lookback_root = f"{bridge_root}/onetime_lookback_minutes"
        self._publisher.publish(
            f"{self._config.discovery_prefix}/number/reolink_monitor/onetime_lookback/config",
            json.dumps(
                {
                    "name": "One-time Lookback",
                    "unique_id": "reolink_monitor_onetime_lookback",
                    "state_topic": f"{lookback_root}/state",
                    "command_topic": f"{lookback_root}/set",
                    "retain": False,
                    "min": 1,
                    "max": max(525600, self._config.recent_recordings_lookback_minutes),
                    "step": 1,
                    "mode": "box",
                    "unit_of_measurement": "min",
                    "availability_topic": availability_topic,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "device": controls_device,
                }
            ),
            retain=True,
        )
        self._publisher.publish(
            f"{self._config.discovery_prefix}/button/reolink_monitor/scrape_now/config",
            json.dumps(
                {
                    "name": "Scrape Now",
                    "unique_id": "reolink_monitor_scrape_now",
                    "command_topic": f"{bridge_root}/scrape/trigger",
                    "payload_press": "PRESS",
                    "retain": False,
                    "availability_topic": availability_topic,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "device": controls_device,
                }
            ),
            retain=True,
        )
        scrape_root = f"{bridge_root}/scrape"
        self._publisher.publish(
            f"{self._config.discovery_prefix}/sensor/reolink_monitor/scrape_status/config",
            json.dumps(
                {
                    "name": "Scrape Status",
                    "unique_id": "reolink_monitor_scrape_status",
                    "state_topic": f"{scrape_root}/state",
                    "json_attributes_topic": f"{scrape_root}/attributes",
                    "availability_topic": availability_topic,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "device": controls_device,
                    "entity_category": "diagnostic",
                }
            ),
            retain=True,
        )
        self._publisher.publish(
            f"{self._config.discovery_prefix}/event/reolink_monitor/scrape_finished/config",
            json.dumps(
                {
                    "name": "Scrape Finished",
                    "unique_id": "reolink_monitor_scrape_finished",
                    "state_topic": f"{scrape_root}/finished",
                    "event_types": ["completed", "partial", "failed", "cancelled"],
                    "availability_topic": availability_topic,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "entity_category": "diagnostic",
                    "device": controls_device,
                }
            ),
            retain=True,
        )

    def _publish_camera_discovery(self, camera: CameraConfig) -> None:
        identity = self._identity(camera)
        state_root = self._camera_state_root(camera)
        availability_topic = f"{self._config.state_prefix}/bridge/availability"
        device = {
            "identifiers": [f"reolink_monitor_{identity}"],
            "manufacturer": "Reolink",
            "name": camera.name,
            "via_device": _BRIDGE_IDENTIFIER,
        }
        model = self._camera_models.get(identity)
        if model is not None:
            device["model"] = model
        entities = (
            (
                "battery",
                "Battery",
                {
                    "device_class": "battery",
                    "state_class": "measurement",
                    "unit_of_measurement": "%",
                },
            ),
            (
                "battery_status",
                "Battery Status",
                {"entity_category": "diagnostic", "icon": "mdi:battery-charging"},
            ),
            (
                "recent_recordings",
                "Recent Recordings",
                {
                    "entity_category": "diagnostic",
                    "icon": "mdi:video-box",
                    "json_attributes_topic": f"{state_root}/recent_recordings/attributes",
                },
            ),
            (
                "scraped_at_iso",
                "Last Scraped",
                {
                    "device_class": "timestamp",
                    "entity_category": "diagnostic",
                },
            ),
            (
                "scraped_at_unix",
                "Last Scraped (Unix)",
                {
                    "unit_of_measurement": "s",
                    "entity_category": "diagnostic",
                },
            ),
            (
                "camera_model",
                "Model",
                {"entity_category": "diagnostic", "icon": "mdi:cctv"},
            ),
        )
        for key, label, attributes in entities:
            topic = (
                f"{self._config.discovery_prefix}/sensor/"
                f"reolink_monitor/{identity}_{key}/config"
            )
            payload = {
                "name": f"{camera.name} {label}",
                "unique_id": f"reolink_monitor_{identity}_{key}",
                "state_topic": f"{state_root}/{key}",
                "availability_topic": availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device,
                "expire_after": self._config.poll_interval_seconds * 3,
                **attributes,
            }
            self._publisher.publish(topic, json.dumps(payload), retain=True)

        clock_topic = f"{state_root}/clock_offset"
        self._publisher.publish(
            f"{self._config.discovery_prefix}/sensor/"
            f"reolink_monitor/{identity}_camera_clock_offset/config",
            json.dumps(
                {
                    "name": f"{camera.name} Camera Clock Offset",
                    "unique_id": f"reolink_monitor_{identity}_camera_clock_offset",
                    "state_topic": clock_topic,
                    "json_attributes_topic": f"{clock_topic}/attributes",
                    "unit_of_measurement": "s",
                    "entity_category": "diagnostic",
                    "availability_topic": availability_topic,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "device": device,
                    "expire_after": self._config.poll_interval_seconds * 3,
                }
            ),
            retain=True,
        )

    def publish_metadata(self) -> None:
        metadata_root = f"{self._config.state_prefix}/bridge/metadata"
        self._publisher.publish(
            f"{metadata_root}/recent_recordings_lookback_minutes",
            str(self._config.recent_recordings_lookback_minutes),
            retain=True,
        )
        self._publisher.publish(
            f"{metadata_root}/poll_interval_seconds",
            str(self._config.poll_interval_seconds),
            retain=True,
        )

    async def _safe_logout(self, host: CameraHost) -> None:
        try:
            await host.logout()
        except (AttributeError, OSError, ReolinkError, TimeoutError, TypeError) as err:
            _LOGGER.debug("logout failed: %s", err)
        finally:
            connection = host.baichuan._connection
            if connection is not None:
                try:
                    await connection.close()
                except (OSError, ReolinkError, TimeoutError) as err:
                    _LOGGER.warning("Baichuan connection cleanup failed: %s", err)

    def _publish_camera_clock(
        self, camera: CameraConfig, clock: CameraClock | None
    ) -> None:
        topic = f"{self._camera_state_root(camera)}/clock_offset"
        if clock is None:
            state = "unknown"
            attributes = {"status": "unavailable"}
        else:
            state = str(clock.utc_offset_seconds)
            attributes = {
                "status": "available",
                "camera_time": clock.device_time.replace(tzinfo=clock.timezone).isoformat(),
                "server_time_utc": clock.server_time_utc.isoformat(),
                "timezone_setting": clock.timezone_setting,
            }
        self._publisher.publish(topic, state, retain=False)
        self._publisher.publish(
            f"{topic}/attributes", json.dumps(attributes), retain=False
        )

    async def _ensure_camera_clock(
        self, host: CameraHost, camera: CameraConfig
    ) -> CameraClock | None:
        identity = self._identity(camera)
        if identity in self._camera_clocks:
            return self._camera_clocks[identity]

        before = datetime.now(UTC)
        try:
            response = await host.baichuan.send(cmd_id=_CMD_GET_SYSTEM_GENERAL)
            after = datetime.now(UTC)
            clock = _parse_camera_clock(response, before + (after - before) / 2)
        except (ReolinkError, OSError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Could not retrieve camera clock for %s: %s", camera.name, err)
            self._publish_camera_clock(camera, None)
            return None

        self._camera_clocks[identity] = clock
        self._publish_camera_clock(camera, clock)
        _LOGGER.info(
            "%s camera clock offset from UTC is %d seconds (camera timezone setting: %s)",
            camera.name,
            clock.utc_offset_seconds,
            clock.timezone_setting or "unknown",
        )
        return clock

    def _update_camera_model(self, camera: CameraConfig, reported_model: str) -> str | None:
        identity = self._identity(camera)
        model = None if reported_model in ("", UNKNOWN) else reported_model
        if self._camera_models.get(identity) != model:
            self._camera_models[identity] = model
            self._publish_camera_discovery(camera)
            if model is not None:
                _LOGGER.info("%s camera model is %s", camera.name, model)
        return model

    async def _connect_camera(self, camera: CameraConfig) -> CameraHost:
        kwargs: dict[str, object] = {"timeout": camera.connection_timeout_seconds}
        if camera.uid:
            kwargs["uid"] = camera.uid
        if camera.use_udp:
            kwargs["bc_connection"] = ConnectionEnum.udp
            kwargs["bc_only"] = True
            _pin_udp_port()
        elif camera.uid:
            kwargs["bc_only"] = True

        factory_kwargs = {}
        signature = inspect.signature(self._host_factory)
        accepts_var_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        for key, value in kwargs.items():
            if accepts_var_kwargs or key in signature.parameters:
                factory_kwargs[key] = value
        host = self._host_factory(
            camera.host,
            camera.username,
            camera.password,
            port=camera.port,
            **factory_kwargs,
        )

        last_error: Exception | None = None
        for attempt in range(1, camera.retry_attempts + 1):
            try:
                _LOGGER.info(
                    "Connecting to %s attempt %d/%d",
                    camera.name,
                    attempt,
                    camera.retry_attempts,
                )
                token = (
                    _UDP_LOCAL_PORT.set(camera.udp_local_port) if camera.use_udp else None
                )
                try:
                    await asyncio.wait_for(
                        host.get_host_data(), timeout=camera.connection_timeout_seconds
                    )
                finally:
                    if token is not None:
                        _UDP_LOCAL_PORT.reset(token)
                return host
            except (ReolinkError, TimeoutError, OSError) as err:
                last_error = err
                _LOGGER.warning("%s attempt %d failed: %s", camera.name, attempt, err)
                await self._safe_logout(host)
                if attempt < camera.retry_attempts:
                    wait = camera.retry_wait_seconds * attempt
                    _LOGGER.info("Waiting %.0f s before retry for %s", wait, camera.name)
                    await asyncio.sleep(wait)
        error_detail = (
            f"{type(last_error).__name__}: {last_error}"
            if last_error is not None
            else "unknown error"
        )
        raise ConnectionError(
            f"Camera {camera.name} not reachable after {camera.retry_attempts} attempts: "
            f"{error_detail}"
        )

    async def poll_camera(
        self,
        camera: CameraConfig,
        lookback_minutes: float | None = None,
        recording_types: tuple[str, ...] | None = None,
    ) -> bool:
        host = await self._connect_camera(camera)
        try:
            await self._ensure_camera_clock(host, camera)
            await host.get_states()
            percentage = host.battery_percentage(camera.channel)
            status_code = host.battery_status(camera.channel)
            state_root = self._camera_state_root(camera)
            model = self._update_camera_model(camera, host.camera_model(camera.channel))
            self._publisher.publish(f"{state_root}/model", model or "unknown", retain=False)
            if percentage is None:
                battery = "unknown"
                status = "unknown"
            else:
                battery = str(percentage)
                status = _BATTERY_STATUS.get(status_code, "unknown")
            self._publisher.publish(f"{state_root}/battery", battery, retain=False)
            self._publisher.publish(
                f"{state_root}/battery_status", status, retain=False
            )
            recordings_ok = await self._publish_recent_recordings(
                host, camera, state_root, lookback_minutes, recording_types
            )
            scraped_at = datetime.now().astimezone()
            self._publisher.publish(
                f"{state_root}/scraped_at_iso",
                scraped_at.isoformat(),
                retain=False,
            )
            self._publisher.publish(
                f"{state_root}/scraped_at_unix",
                str(int(scraped_at.timestamp())),
                retain=False,
            )
            _LOGGER.info("%s battery=%s%% status=%s", camera.name, battery, status)
            return recordings_ok
        finally:
            await self._safe_logout(host)

    async def _publish_recent_recordings(
        self,
        host: CameraHost,
        camera: CameraConfig,
        state_root: str,
        lookback_minutes: float | None = None,
        recording_types: tuple[str, ...] | None = None,
    ) -> bool:
        lookback_minutes = (
            self._config.recent_recordings_lookback_minutes
            if lookback_minutes is None
            else lookback_minutes
        )
        clock = self._camera_clocks.get(self._identity(camera))
        if clock is None:
            end = datetime.now(host.timezone()) + timedelta(minutes=5)
        else:
            end = datetime.now(UTC).astimezone(clock.timezone) + timedelta(minutes=5)
        start = end - timedelta(minutes=lookback_minutes)
        recording_types = (
            self._config.recent_recordings_types
            if recording_types is None
            else recording_types
        )
        _LOGGER.debug(
            "%s searching recordings for channel %d stream %s from %s to %s",
            camera.name,
            camera.channel,
            _RECORDING_STREAM,
            start.isoformat(),
            end.isoformat(),
        )
        try:
            recordings = await _search_file_info_recordings(host, camera, start, end)
        except (ReolinkError, OSError, TimeoutError) as err:
            _LOGGER.warning("Could not list recent recordings for %s: %s", camera.name, err)
            self._publisher.publish(
                f"{state_root}/recent_recordings", "unknown", retain=False
            )
            self._publisher.publish(
                f"{state_root}/recent_recordings/attributes",
                json.dumps({"recordings": [], "error": str(err)}),
                retain=False,
            )
            return False

        if recording_types:
            allowed_triggers = set().union(
                *(_RECORDING_TYPE_TRIGGERS[recording_type] for recording_type in recording_types)
            )
            recordings = [
                recording
                for recording in recordings
                if {
                    trigger.strip().lower()
                    for trigger in recording.trigger.split(",")
                }
                & allowed_triggers
            ]
        total_count = len(recordings)
        recent = sorted(
            recordings, key=lambda recording: recording.start_time, reverse=True
        )[:_RECENT_RECORDING_LIMIT]
        _LOGGER.info(
            "%s recording search returned %d unique clips within the last %d minutes; "
            "publishing details for %d",
            camera.name,
            total_count,
            lookback_minutes,
            len(recent),
        )
        recordings = [
            {
                "file_name": recording.file_name,
                "start_time": recording.start_time.isoformat(),
                "duration_seconds": int(recording.duration.total_seconds()),
                "trigger": recording.trigger,
            }
            for recording in recent
        ]
        _LOGGER.debug("%s recent recordings: %s", camera.name, recordings)
        self._publisher.publish(
            f"{state_root}/recent_recordings",
            str(total_count),
            retain=False,
        )
        self._publisher.publish(
            f"{state_root}/recent_recordings/attributes",
            json.dumps({"recordings": recordings}),
            retain=False,
        )
        return True

    async def poll_all(
        self,
        lookback_minutes: float | None = None,
        recording_types: tuple[str, ...] | None = None,
    ) -> tuple[str, ...]:
        failed_cameras = []
        for camera in self._config.cameras:
            try:
                if not await self.poll_camera(camera, lookback_minutes, recording_types):
                    failed_cameras.append(camera.name)
            except Exception:
                _LOGGER.exception("Failed to poll Reolink camera %s", camera.name)
                failed_cameras.append(camera.name)
        return tuple(failed_cameras)

    def _publish_scrape_status(
        self,
        status: str,
        attributes: dict[str, object],
    ) -> None:
        root = f"{self._config.state_prefix}/bridge/scrape"
        self._publisher.publish(f"{root}/state", status, retain=True)
        self._publisher.publish(
            f"{root}/attributes",
            json.dumps(attributes),
            retain=True,
        )

    def _publish_one_time_lookback(self) -> None:
        self._publisher.publish(
            f"{self._config.state_prefix}/bridge/onetime_lookback_minutes/state",
            str(self._one_time_lookback),
            retain=True,
        )

    def _publish_one_time_recording_types(self) -> None:
        value = ",".join(self._one_time_recording_types) or "all"
        self._publisher.publish(
            f"{self._config.state_prefix}/bridge/onetime_recording_types/state",
            value,
            retain=True,
        )

    def _set_one_time_recording_types(self, payload: str) -> tuple[str, ...] | None:
        normalized = payload.strip().lower()
        if normalized == "all":
            value = ()
        else:
            requested_types = [
                recording_type.strip() for recording_type in normalized.split(",")
            ]
            if (
                not normalized
                or any(not recording_type for recording_type in requested_types)
                or any(
                    recording_type not in RECENT_RECORDING_TYPES
                    for recording_type in requested_types
                )
            ):
                _LOGGER.warning(
                    "Ignoring invalid set_onetime_recording_types payload: %s", payload
                )
                self._publish_one_time_recording_types()
                return None
            value = tuple(dict.fromkeys(requested_types))
        self._one_time_recording_types = value
        self._publish_one_time_recording_types()
        return value

    def _set_one_time_lookback(self, payload: str) -> int | None:
        normalized = payload.strip()
        if not normalized.isdecimal():
            _LOGGER.warning("Ignoring invalid set_onetime_lookback payload: %s", payload)
            self._publish_one_time_lookback()
            return None
        try:
            value = int(normalized)
        except ValueError:
            _LOGGER.warning("Ignoring invalid set_onetime_lookback payload: %s", payload)
            self._publish_one_time_lookback()
            return None
        if value < 1:
            _LOGGER.warning("Ignoring invalid set_onetime_lookback payload: %s", payload)
            self._publish_one_time_lookback()
            return None
        if value > max(525600, self._config.recent_recordings_lookback_minutes):
            _LOGGER.warning("Ignoring out-of-range one-time lookback: %s", payload)
            self._publish_one_time_lookback()
            return None
        self._one_time_lookback = value
        self._publish_one_time_lookback()
        return value

    async def _run_scrape(self, run: ScrapeRun) -> None:
        root = f"{self._config.state_prefix}/bridge/scrape"
        attributes: dict[str, object] = {
            "run_id": run.run_id,
            "trigger": run.trigger,
            "lookback_minutes": run.lookback_minutes,
            "recording_types": run.recording_types or ["all"],
            "started_at": run.started_at.isoformat(),
        }
        self._publish_scrape_status("running", attributes)
        try:
            failed_cameras = await self.poll_all(
                run.lookback_minutes,
                run.recording_types,
            )
        except asyncio.CancelledError:
            finished_at = datetime.now().astimezone()
            attributes.update(
                {
                    "trigger": run.trigger,
                    "finished_at": finished_at.isoformat(),
                    "camera_count": len(self._config.cameras),
                    "failed_count": 0,
                    "failed_cameras": [],
                }
            )
            self._publish_scrape_status("cancelled", attributes)
            self._publisher.publish(
                f"{root}/finished",
                json.dumps(
                    {"event_type": "cancelled", "status": "cancelled", **attributes}
                ),
                retain=False,
            )
            raise
        finished_at = datetime.now().astimezone()
        if failed_cameras:
            status = "failed" if len(failed_cameras) == len(self._config.cameras) else "partial"
        else:
            status = "completed"
        attributes.update(
            {
                "trigger": run.trigger,
                "finished_at": finished_at.isoformat(),
                "camera_count": len(self._config.cameras),
                "failed_count": len(failed_cameras),
                "failed_cameras": failed_cameras,
            }
        )
        self._publish_scrape_status(status, attributes)
        self._publisher.publish(
            f"{root}/finished",
            json.dumps({"event_type": status, "status": status, **attributes}),
            retain=False,
        )
        _LOGGER.info(
            "Scrape %s finished with status %s (trigger=%s, failed=%d/%d)",
            run.run_id,
            status,
            run.trigger,
            len(failed_cameras),
            len(self._config.cameras),
        )

    async def run(
        self, command_queue: asyncio.Queue[tuple[str, str]] | None = None
    ) -> None:
        self.publish_discovery()
        self.publish_metadata()
        self._one_time_lookback = self._config.recent_recordings_lookback_minutes
        self._one_time_recording_types = self._config.recent_recordings_types
        self._publish_one_time_lookback()
        self._publish_one_time_recording_types()
        self._publish_scrape_status("idle", {})
        loop = asyncio.get_running_loop()
        next_interval = loop.time()
        active_run: ScrapeRun | None = None
        scrape_task: asyncio.Task[None] | None = None
        command_task: asyncio.Task[tuple[str, str]] | None = None
        try:
            while True:
                if scrape_task is None and loop.time() >= next_interval:
                    active_run = ScrapeRun(
                        run_id=str(uuid4()),
                        trigger="interval",
                        lookback_minutes=self._config.recent_recordings_lookback_minutes,
                        started_at=datetime.now().astimezone(),
                        recording_types=self._config.recent_recordings_types,
                    )
                    scrape_task = asyncio.create_task(self._run_scrape(active_run))
                    next_interval = loop.time() + self._config.poll_interval_seconds

                if command_queue is None:
                    if scrape_task is None:
                        await asyncio.sleep(max(0, next_interval - loop.time()))
                    else:
                        await scrape_task
                        scrape_task = None
                        active_run = None
                        next_interval = loop.time() + self._config.poll_interval_seconds
                    continue

                if command_task is None:
                    command_task = asyncio.create_task(command_queue.get())
                wait_tasks = [command_task]
                if scrape_task is not None:
                    wait_tasks.append(scrape_task)
                timeout = (
                    None
                    if scrape_task is not None
                    else max(0, next_interval - loop.time())
                )
                done, _pending = await asyncio.wait(
                    wait_tasks,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if scrape_task is not None and scrape_task in done:
                    await scrape_task
                    scrape_task = None
                    active_run = None
                    next_interval = loop.time() + self._config.poll_interval_seconds
                if command_task in done:
                    topic, payload = command_task.result()
                    command_task = None
                    expected_prefix = f"{self._config.state_prefix}/bridge/"
                    if not topic.startswith(expected_prefix):
                        _LOGGER.warning("Ignoring unsupported MQTT command topic: %s", topic)
                        continue
                    command = topic.removeprefix(expected_prefix)
                    if command == "onetime_lookback_minutes/set":
                        self._set_one_time_lookback(payload)
                    elif command == "onetime_recording_types/set":
                        self._set_one_time_recording_types(payload)
                    elif command == "scrape/trigger":
                        if payload.strip() != "PRESS":
                            _LOGGER.warning("Ignoring invalid scrape_now payload: %s", payload)
                            continue
                        if scrape_task is not None and active_run is not None:
                            if active_run.trigger == "interval":
                                active_run.trigger = "both"
                                self._publish_scrape_status(
                                    "running",
                                    {
                                        "run_id": active_run.run_id,
                                        "trigger": active_run.trigger,
                                        "lookback_minutes": active_run.lookback_minutes,
                                        "recording_types": (
                                            active_run.recording_types or ["all"]
                                        ),
                                        "started_at": active_run.started_at.isoformat(),
                                    },
                                )
                        else:
                            active_run = ScrapeRun(
                                run_id=str(uuid4()),
                                trigger="button",
                                lookback_minutes=self._one_time_lookback,
                                started_at=datetime.now().astimezone(),
                                recording_types=self._one_time_recording_types,
                            )
                            self._one_time_lookback = (
                                self._config.recent_recordings_lookback_minutes
                            )
                            self._publish_one_time_lookback()
                            self._one_time_recording_types = (
                                self._config.recent_recordings_types
                            )
                            self._publish_one_time_recording_types()
                            scrape_task = asyncio.create_task(self._run_scrape(active_run))
                    else:
                        _LOGGER.warning("Ignoring unsupported MQTT command: %s", command)
        finally:
            if command_task is not None and not command_task.done():
                command_task.cancel()
                await asyncio.gather(command_task, return_exceptions=True)
            if scrape_task is not None:
                if not scrape_task.done():
                    scrape_task.cancel()
                    try:
                        await scrape_task
                    except asyncio.CancelledError:
                        pass
                else:
                    await scrape_task
