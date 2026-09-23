from __future__ import annotations

import re
from typing import Any

from apps.orchestrator.domain import SourceEvent, canonical_json, sha256_text, utc_now


REDACTION_VERSION = "1.1"
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
TOKEN_RE = re.compile(r"(?i)\b(?:api[_-]?key|token|password)\s*[:=]\s*[^\s,;]+")
# Match dates before phone-like numeric strings. The previous phone expression
# treated ISO dates such as 2026-09-17 as telephone numbers.
DATE_OR_PHONE_RE = re.compile(
    r"(?P<date>(?<!\d)(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4})(?!\d))"
    r"|(?P<phone>(?<!\d)\+?\d[\d ()-]{7,}\d(?!\d))"
)


def _redact_phone_preserving_dates(match: re.Match[str]) -> str:
    if match.group("date") is not None:
        return match.group(0)
    return "[REDACTED_PHONE]"


def redact(text: str) -> str:
    text = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = TOKEN_RE.sub("[REDACTED_SECRET]", text)
    return DATE_OR_PHONE_RE.sub(_redact_phone_preserving_dates, text)


def content_hash(raw: dict[str, Any]) -> str:
    relevant = {
        "title": str(raw.get("title", "")).strip(),
        "text": str(raw.get("text", "")).strip(),
        "participants": sorted(str(p) for p in raw.get("participants", [])),
    }
    return sha256_text(canonical_json(relevant))


def normalize(raw: dict[str, Any], revision: int, raw_artifact_id: str | None,
              ingested_at: str | None = None) -> SourceEvent:
    source = str(raw["source"])
    external_id = str(raw["external_id"])
    digest = content_hash(raw)
    stable = sha256_text(f"{source}:{external_id}")[:16]
    participants = [redact(str(item)) for item in raw.get("participants", [])]
    return SourceEvent(
        schema_version="1.0",
        event_id=f"evt_{stable}_{revision}",
        source=source,
        external_id=external_id,
        revision=revision,
        event_time=str(raw["event_time"]),
        ingested_at=ingested_at or utc_now(),
        title=redact(str(raw.get("title", "")).strip()),
        text=redact(str(raw.get("text", "")).strip()),
        source_url=raw.get("source_url"),
        participants=participants,
        tags=sorted(set(str(tag) for tag in raw.get("tags", []))),
        content_sha256=digest,
        sensitivity=str(raw.get("sensitivity", "internal")),
        raw_artifact_id=raw_artifact_id,
        redaction_version=REDACTION_VERSION,
    )
