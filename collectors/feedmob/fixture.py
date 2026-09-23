from __future__ import annotations

from pathlib import Path
import json
from typing import Any


class FixtureCollector:
    """Offline collector used for deterministic development and acceptance tests."""

    def __init__(self, path: Path):
        self.path = path

    def discover(self) -> list[dict[str, Any]]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("fixture root must be a JSON array")
        required = {"source", "external_id", "event_time", "text"}
        for index, item in enumerate(payload):
            if not isinstance(item, dict) or not required.issubset(item):
                raise ValueError(f"fixture item {index} is missing required fields")
            if item["source"] not in {"slack", "fireflies", "email"}:
                raise ValueError(f"fixture item {index} has unsupported source")
        return payload
