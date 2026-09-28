"""Repair known WB text mojibake without changing non-corrupted values."""
from __future__ import annotations

import re
from typing import Any

_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_CJK = re.compile(r"[\u3400-\u9fff]")


def repair_wb_text(value: str) -> str:
    """Recover Russian text that was decoded as Big5 then re-encoded as UTF-8.

    The WB feedback payload observed in this account contains strings such as
    ``妍把我忍...``; ``big5 -> gbk`` recovers the original Russian. Only use
    the conversion when it produces Cyrillic and removes the CJK mojibake.
    """
    if not value or not _CJK.search(value):
        return value
    try:
        candidate = value.encode("big5").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    if _CYRILLIC.search(candidate) and not _CJK.search(candidate):
        return candidate
    return value


def repair_wb_payload(value: Any) -> Any:
    if isinstance(value, str):
        return repair_wb_text(value)
    if isinstance(value, list):
        return [repair_wb_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_wb_payload(item) for key, item in value.items()}
    return value
