from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceEvent:
    schema_version: str
    event_id: str
    source: str
    external_id: str
    revision: int
    event_time: str
    ingested_at: str
    title: str
    text: str
    source_url: str | None
    participants: list[str]
    tags: list[str]
    content_sha256: str
    sensitivity: str
    raw_artifact_id: str | None
    redaction_version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AnalysisRequest:
    snapshot_id: str
    model_input: str
    events: list[dict[str, Any]]
    prompt_version: str
    prompt: str
    allowed_owners: list[str] = field(default_factory=lambda: ["Liu", "LU", "unassigned"])


@dataclass
class AdapterResult:
    provider: str
    model: str
    output: dict[str, Any] | None
    duration_ms: int
    exit_code: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    stderr: str = ""
    raw_output: str = ""
