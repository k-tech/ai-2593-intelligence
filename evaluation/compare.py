from __future__ import annotations

from typing import Any


def compare_outputs(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_keys = {str(item.get("key")) for item in left.get("themes", [])}
    right_keys = {str(item.get("key")) for item in right.get("themes", [])}
    union = left_keys | right_keys
    return {
        "theme_overlap": len(left_keys & right_keys) / len(union) if union else 1.0,
        "shared_theme_keys": sorted(left_keys & right_keys),
        "left_only_theme_keys": sorted(left_keys - right_keys),
        "right_only_theme_keys": sorted(right_keys - left_keys),
    }
