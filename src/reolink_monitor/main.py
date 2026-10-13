"""Application entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

from .config import load_config
from .monitor import BatteryMonitor
from .stdout import StdoutPublisher


async def _run(config_path: Path, mode: str, *, mqtt_insecure: bool = False) -> None:
    config = load_config(config_path, require_mqtt=mode == "mqtt")
    if mode == "stdout":
        await BatteryMonitor(config, StdoutPublisher()).run()
        return

    if config.mqtt is None:
        raise ValueError("MQTT configuration is required in MQTT mode")
    from .mqtt import MqttPublisher

    publisher = MqttPublisher(config.mqtt, insecure=mqtt_insecure)
    publisher.connect()
    try:
        await BatteryMonitor(config, publisher).run()
    finally:
        publisher.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor Reolink cameras")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="YAML configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--mode",
        choices=("mqtt", "stdout"),
        default="mqtt",
        help="publish to MQTT (default) or write topic/payload records to stdout",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable debug logging for this app and reolink-aio",
    )
    parser.add_argument(
        "--mqtt-insecure",
        "--insecure",
        dest="mqtt_insecure",
        action="store_true",
        help="use MQTT TLS without validating the broker certificate (unsafe)",
    )
    args = parser.parse_args()
    load_dotenv()
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger().setLevel(log_level)
    if args.debug:
        logging.getLogger("reolink_aio").setLevel(logging.DEBUG)
        logging.getLogger("reolink_aio.api").setLevel(logging.DEBUG)
    try:
        asyncio.run(_run(args.config, args.mode, mqtt_insecure=args.mqtt_insecure))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Stopped")


if __name__ == "__main__":
    main()
