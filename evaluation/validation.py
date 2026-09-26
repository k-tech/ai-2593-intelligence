from __future__ import annotations

import re
from typing import Any

from collectors.feedmob.normalizer import DATE_OR_PHONE_RE


CONTENT_FIELDS = {"executive_summary", "themes", "risks", "experiments", "followups"}
TOP_LEVEL = CONTENT_FIELDS | {"model_meta"}
THEME_FIELDS = {"key", "title", "summary", "why_it_matters", "evidence_event_ids",
                "confidence", "owner", "next_action"}
SENSITIVE_PATTERNS = [
    re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|password)\s*[:=]\s*[^\s,;]+"),
]
HAN_RE = re.compile(r"[\u3400-\u9fff]")


def validate_chinese_analysis(output: Any) -> list[str]:
    """Reject English-only reader-facing fields; preserve IDs, owners and proper names."""
    if not isinstance(output, dict):
        return []
    fields: list[tuple[str, Any]] = [("executive_summary", output.get("executive_summary"))]
    themes = output.get("themes")
    if isinstance(themes, list):
        for index, theme in enumerate(themes):
            if isinstance(theme, dict):
                fields.extend((f"themes[{index}].{key}", theme.get(key)) for key in
                              ("title", "summary", "why_it_matters", "next_action"))
    for key in ("risks", "experiments", "followups"):
        values = output.get(key)
        if isinstance(values, list):
            fields.extend((f"{key}[{index}]", value) for index, value in enumerate(values))
    return [f"{path} must be written in Chinese" for path, value in fields
            if isinstance(value, str) and value.strip() and len(HAN_RE.findall(value)) < 2]


def validate_analysis_content(output: Any, event_ids: set[str],
                              allowed_owners: set[str]) -> list[str]:
    errors: list[str] = []
    if not isinstance(output, dict):
        return ["output must be an object"]
    missing = CONTENT_FIELDS - output.keys()
    extra = output.keys() - CONTENT_FIELDS
    if missing:
        errors.append("missing top-level fields: " + ", ".join(sorted(missing)))
    if extra:
        errors.append("unexpected top-level fields: " + ", ".join(sorted(extra)))
    if not isinstance(output.get("executive_summary"), str) or not output.get("executive_summary", "").strip():
        errors.append("executive_summary must be a non-empty string")
    themes = output.get("themes")
    if not isinstance(themes, list):
        errors.append("themes must be an array")
        themes = []
    for index, theme in enumerate(themes):
        prefix = f"themes[{index}]"
        if not isinstance(theme, dict):
            errors.append(f"{prefix} must be an object")
            continue
        if THEME_FIELDS - theme.keys():
            errors.append(f"{prefix} missing fields: " + ", ".join(sorted(THEME_FIELDS - theme.keys())))
        if theme.keys() - THEME_FIELDS:
            errors.append(f"{prefix} has unexpected fields")
        evidence = theme.get("evidence_event_ids")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"{prefix} must include evidence")
        elif unknown := set(evidence) - event_ids:
            errors.append(f"{prefix} references unknown evidence: {', '.join(sorted(unknown))}")
        confidence = theme.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            errors.append(f"{prefix} confidence must be between 0 and 1")
        if theme.get("owner") not in allowed_owners:
            errors.append(f"{prefix} owner is not allowed")
    for field in ("risks", "experiments", "followups"):
        if not isinstance(output.get(field), list) or not all(isinstance(x, str) for x in output.get(field, [])):
            errors.append(f"{field} must be an array of strings")
    rendered = str(output)
    if any(pattern.search(rendered) for pattern in SENSITIVE_PATTERNS):
        errors.append("output contains potentially sensitive text")
    text_fields = [output.get("executive_summary", "")]
    for field in ("risks", "experiments", "followups"):
        if isinstance(output.get(field), list):
            text_fields.extend(item for item in output[field] if isinstance(item, str))
    for theme in themes:
        if isinstance(theme, dict):
            text_fields.extend(theme.get(field, "") for field in
                               ("title", "summary", "why_it_matters", "next_action"))
    if any(match.group("phone") is not None
           for value in text_fields if isinstance(value, str)
           for match in DATE_OR_PHONE_RE.finditer(value)):
        errors.append("output contains potentially sensitive phone-like text")
    return errors


def validate_analysis(output: Any, event_ids: set[str], allowed_owners: set[str]) -> list[str]:
    if not isinstance(output, dict):
        return ["output must be an object"]
    errors: list[str] = []
    missing = TOP_LEVEL - output.keys()
    extra = output.keys() - TOP_LEVEL
    if missing:
        errors.append("missing top-level fields: " + ", ".join(sorted(missing)))
    if extra:
        errors.append("unexpected top-level fields: " + ", ".join(sorted(extra)))
    content = {key: output[key] for key in CONTENT_FIELDS if key in output}
    errors.extend(validate_analysis_content(content, event_ids, allowed_owners))
    meta = output.get("model_meta")
    meta_fields = {"provider", "model", "prompt_version", "duration_ms", "input_tokens",
                   "output_tokens", "cost_usd"}
    if not isinstance(meta, dict):
        errors.append("model_meta must be an object")
        return errors
    if missing_meta := meta_fields - meta.keys():
        errors.append("model_meta missing fields: " + ", ".join(sorted(missing_meta)))
    if meta.keys() - meta_fields:
        errors.append("model_meta has unexpected fields")
    if meta.get("provider") not in {"anthropic", "openai"}:
        errors.append("model_meta provider is invalid")
    for field in ("model", "prompt_version"):
        if not isinstance(meta.get(field), str) or not meta.get(field, "").strip():
            errors.append(f"model_meta {field} must be a non-empty string")
    for field in ("duration_ms", "input_tokens", "output_tokens"):
        value = meta.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"model_meta {field} must be a non-negative integer")
    cost = meta.get("cost_usd")
    if cost is not None and (not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0):
        errors.append("model_meta cost_usd must be null or a non-negative number")
    return errors
