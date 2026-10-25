"""Poll Reolink cameras and publish battery data and discovery to MQTT."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from contextvars import ContextVar
from datetime import datetime, time, timedelta, tzinfo
from typing import Protocol
from xml.etree import ElementTree as XML

from reolink_aio.api import Host
from reolink_aio.enums import ConnectionEnum
from reolink_aio.exceptions import ReolinkError, UnexpectedDataError

from .config import AppConfig, CameraConfig

_LOGGER = logging.getLogger(__name__)
_BATTERY_STATUS = {0: "discharging", 1: "charging", 2: "charged"}
_UDP_LOCAL_PORT: ContextVar[int | None] = ContextVar("baichuan_udp_local_port", default=None)
_UDP_CONNECTION_PATCHED = False
_RECENT_RECORDING_LIMIT = 10
_RECORDING_STREAM = "main"
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


class Publisher(Protocol):
    def publish(self, topic: str, payload: str, retain: bool = True) -> None: ...


class BaichuanApi(Protocol):
    async def send(self, cmd_id: int, body: str = "") -> str: ...


class CameraHost(Protocol):
    baichuan: BaichuanApi

    async def get_host_data(self) -> None: ...

    async def get_states(self) -> None: ...

    def timezone(self) -> tzinfo: ...

    def camera_uid(self, channel: int) -> str: ...

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

    def _identity(self, camera: CameraConfig) -> str:
        return f"{_slug(camera.name)}_ch{camera.channel}"

    def publish_discovery(self) -> None:
        _LOGGER.info(
            "Publishing Home Assistant MQTT discovery for %d cameras (%d entities)",
            len(self._config.cameras),
            len(self._config.cameras) * 3,
        )
        for camera in self._config.cameras:
            identity = self._identity(camera)
            state_root = f"{self._config.state_prefix}/{identity}"
            availability_topic = f"{state_root}/availability"
            device = {
                "identifiers": [f"reolink_monitor_{identity}"],
                "manufacturer": "Reolink",
                "name": camera.name,
            }
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
                self._publisher.publish(topic, json.dumps(payload))

    async def _safe_logout(self, host: CameraHost) -> None:
        try:
            await host.logout()
        except (AttributeError, OSError, ReolinkError, TimeoutError, TypeError) as err:
            _LOGGER.debug("logout failed: %s", err)

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
        raise ConnectionError(
            f"Camera {camera.name} not reachable after {camera.retry_attempts} attempts: "
            f"{last_error or 'unknown error'}"
        )

    async def poll_camera(self, camera: CameraConfig) -> None:
        host = await self._connect_camera(camera)
        try:
            await host.get_states()
            percentage = host.battery_percentage(camera.channel)
            status_code = host.battery_status(camera.channel)
            identity = self._identity(camera)
            state_root = f"{self._config.state_prefix}/{identity}"
            self._publisher.publish(f"{state_root}/availability", "online")
            if percentage is None:
                battery = "unknown"
                status = "unknown"
            else:
                battery = str(percentage)
                status = _BATTERY_STATUS.get(status_code, "unknown")
            self._publisher.publish(f"{state_root}/battery", battery)
            self._publisher.publish(f"{state_root}/battery_status", status)
            await self._publish_recent_recordings(host, camera, state_root)
            _LOGGER.info("%s battery=%s%% status=%s", camera.name, battery, status)
        finally:
            await self._safe_logout(host)

    async def _publish_recent_recordings(
        self, host: CameraHost, camera: CameraConfig, state_root: str
    ) -> None:
        # The camera can timestamp clips a few minutes ahead of this host.
        end = datetime.now(host.timezone()) + timedelta(minutes=5)
        start = end - timedelta(minutes=self._config.recent_recordings_lookback_minutes)
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
            self._publisher.publish(f"{state_root}/recent_recordings", "unknown")
            self._publisher.publish(
                f"{state_root}/recent_recordings/attributes",
                json.dumps({"recordings": [], "error": str(err)}),
            )
            return

        total_count = len(recordings)
        recent = sorted(
            recordings, key=lambda recording: recording.start_time, reverse=True
        )[:_RECENT_RECORDING_LIMIT]
        _LOGGER.info(
            "%s recording search returned %d unique clips within the last %d minutes; "
            "publishing details for %d",
            camera.name,
            total_count,
            self._config.recent_recordings_lookback_minutes,
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
        )
        self._publisher.publish(
            f"{state_root}/recent_recordings/attributes",
            json.dumps({"recordings": recordings}),
        )

    async def poll_all(self) -> None:
        for camera in self._config.cameras:
            try:
                await self.poll_camera(camera)
            except Exception:
                identity = self._identity(camera)
                topic = f"{self._config.state_prefix}/{identity}/availability"
                try:
                    self._publisher.publish(topic, "offline")
                except Exception:
                    _LOGGER.exception("Could not publish offline status for %s", camera.name)
                _LOGGER.exception("Failed to poll Reolink camera %s", camera.name)

    async def run(self) -> None:
        self.publish_discovery()
        while True:
            await self.poll_all()
            await asyncio.sleep(self._config.poll_interval_seconds)
