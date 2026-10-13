"""Write MQTT-style discovery and state records as JSON Lines."""

from __future__ import annotations

import json
import sys
from typing import TextIO


class StdoutPublisher:
    """Emit each topic and payload as one JSON object on standard output."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream

    def publish(self, topic: str, payload: str, retain: bool = True) -> None:
        output_payload: object = payload
        if payload.lstrip().startswith("{"):
            output_payload = json.loads(payload)
        record = {"topic": topic, "payload": output_payload, "retain": retain}
        stream = self._stream if self._stream is not None else sys.stdout
        print(json.dumps(record), file=stream, flush=True)
