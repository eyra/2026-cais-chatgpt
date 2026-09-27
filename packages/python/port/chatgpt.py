"""Extract reviewable ChatGPT messages; fail soon on export format changes.

The five donated fields and their values follow trbKnl/port-chatgpt-uu
(AGPL-3.0), commit d80e9d834095034c9e80c85986bdab61fb8f152a, verified per
message against real exports. Fields are read from their known locations only;
value encodings that were never observed are not guessed.

Two kinds of failure are distinguished:
- A field present on every conversation or message has an unknown shape: the
  export format changed. Extraction stops at the first occurrence by raising
  UnsupportedExportFormat, so the change is noticed immediately.
- A single message cannot be used (unknown content type, future timestamp,
  unserializable text): it is skipped and reported as an issue; extraction
  continues.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from typing import Any

import pandas as pd

MESSAGE_COLUMNS = ["conversation title", "role", "message", "model", "time"]
ISSUE_COLUMNS = ["conversation", "message", "reason", "action"]
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_LABEL = re.compile(r"[a-z0-9_]{1,40}")
# Observed reasoning content without parts; the reference donates these as
# rows with an empty message. Other content types without parts are unknown.
EMPTY_TEXT_CONTENT_TYPES = frozenset({"thoughts", "reasoning_recap"})


class UnsupportedExportFormat(ValueError):
    """A field present on every conversation or message has an unknown shape.

    Carries the reason code and 1-based export positions; never export IDs or
    participant text.
    """

    def __init__(self, reason: str, conversation: int, message: int | None = None):
        location = f"conversation={conversation}"
        if message is not None:
            location += f" message={message}"
        super().__init__(f"reason={reason} {location}")
        self.reason = reason
        self.conversation = conversation
        self.message = message


@dataclass(frozen=True)
class ExtractionResult:
    messages: pd.DataFrame
    issues: pd.DataFrame


class _FormatChange(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Skip(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _valid_text(value: str) -> bool:
    """Pandas' JSON encoder cannot serialize isolated Unicode surrogates."""
    return _SURROGATE.search(value) is None


def _label(value: Any) -> str:
    """Export metadata name safe for logs; anything unexpected becomes "other"."""
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else "other"


def _hidden_flag(value: Any) -> bool:
    """The flag is normally absent; only booleans have been observed."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise _FormatChange("invalid_hidden_flag")


def _timestamp(value: Any) -> datetime:
    """Exports use Unix seconds as numbers; any other encoding is a format change."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _FormatChange("invalid_timestamp")
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise _FormatChange("invalid_timestamp") from None


def _content_text(content: Any) -> str:
    """Flatten parts like the reference: ordered scalar leaves, str()-converted.

    Nulls become "None" and booleans "True"/"False", as in the reference. The
    parts list has depth zero; descendants may be at most 64 edges deep.
    Iteration bounds deep input without relying on Python recursion.
    """
    if not isinstance(content, dict):
        raise _FormatChange("invalid_content")
    if "parts" not in content:
        content_type = content.get("content_type")
        if content_type in EMPTY_TEXT_CONTENT_TYPES:
            return ""
        raise _Skip(f"unknown_content_type:{_label(content_type)}")
    parts = content["parts"]
    if not isinstance(parts, list):
        raise _FormatChange("invalid_content")
    leaves = []
    pending = [(parts, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 64:
            raise _FormatChange("invalid_content")
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in reversed(value.values()))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in reversed(value))
        elif isinstance(value, str):
            if not _valid_text(value):
                raise _Skip("invalid_text")
            leaves.append(value)
        elif value is None:
            leaves.append("None")
        elif isinstance(value, (bool, int)):
            leaves.append(str(value))
        elif isinstance(value, float) and math.isfinite(value):
            leaves.append(str(value))
        else:
            raise _FormatChange("invalid_content")
    return "".join(leaves)


def _message_fields(
    message: Any, current: datetime, cutoff: datetime
) -> tuple[datetime, str, str, str, str] | None:
    """(created, role, text, model, local time), or None if hidden or out of window."""
    if not isinstance(message, dict):
        raise _FormatChange("invalid_message")
    metadata = message.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise _FormatChange("invalid_metadata")
    if _hidden_flag(metadata.get("is_visually_hidden_from_conversation")):
        return None
    created = _timestamp(message.get("create_time"))
    if created > current:
        raise _Skip("future_timestamp")
    if created < cutoff:
        return None
    author = message.get("author")
    role = author.get("role") if isinstance(author, dict) else None
    if not isinstance(role, str):
        raise _FormatChange("invalid_role")
    if role == "":
        return None  # The reference drops rows with an empty role.
    model = metadata.get("model_slug")
    if model is None:
        model = ""
    if not isinstance(model, str):
        raise _FormatChange("invalid_model")
    text = _content_text(message.get("content"))
    if not _valid_text(role) or not _valid_text(model):
        raise _Skip("invalid_text")
    try:
        local_time = created.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError, OSError):
        raise _FormatChange("invalid_timestamp") from None
    return created, role, text, model, local_time


def extract_conversations(
    conversations: Iterable[Any], now: datetime | None = None
) -> ExtractionResult:
    """Keep the last UTC calendar year, newest first.

    Raises UnsupportedExportFormat at the first format change. Skipped messages
    are returned as issues. Locations are 1-based positions across the whole
    export (all parts), never export IDs or participant text. Conversations are
    consumed once, in order, so a lazy iterable keeps only the current export
    part in memory. Hidden messages and ordinary root placeholders are
    intentionally skipped. Naive clocks are interpreted as UTC; aware clocks are
    normalized to UTC.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    current = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    try:
        cutoff = current.replace(year=current.year - 1)
    except ValueError:
        cutoff = current.replace(year=current.year - 1, day=28)
    rows = []
    issues = []

    def issue(conversation: int, message: int | None, reason: str) -> None:
        issues.append({
            "conversation": str(conversation),
            "message": "" if message is None else str(message),
            "reason": reason,
            "action": "conversation_excluded" if message is None else "message_excluded",
        })

    for conversation_position, conversation in enumerate(conversations, start=1):
        if not isinstance(conversation, dict):
            raise UnsupportedExportFormat("invalid_conversation", conversation_position)
        title = conversation.get("title")
        if not isinstance(title, str):
            raise UnsupportedExportFormat("invalid_title", conversation_position)
        mapping = conversation.get("mapping")
        if not isinstance(mapping, dict):
            raise UnsupportedExportFormat("invalid_mapping", conversation_position)
        if not _valid_text(title):
            issue(conversation_position, None, "invalid_text")
            continue
        for message_position, node in enumerate(mapping.values(), start=1):
            if not isinstance(node, dict) or "message" not in node:
                raise UnsupportedExportFormat("invalid_message", conversation_position, message_position)
            message = node["message"]
            if message is None and node.get("parent") is None:
                continue
            try:
                fields = _message_fields(message, current, cutoff)
            except _FormatChange as error:
                raise UnsupportedExportFormat(
                    error.reason, conversation_position, message_position
                ) from None
            except _Skip as error:
                issue(conversation_position, message_position, error.reason)
                continue
            if fields is None:
                continue
            created, role, text, model, local_time = fields
            rows.append((created, {
                "conversation title": title,
                "role": role,
                "message": text,
                "model": model,
                "time": local_time,
                "_conversation": conversation_position - 1,
            }))

    rows.sort(key=lambda row: row[0], reverse=True)
    return ExtractionResult(
        messages=pd.DataFrame([row for _, row in rows], columns=[*MESSAGE_COLUMNS, "_conversation"]),
        issues=pd.DataFrame(issues, columns=ISSUE_COLUMNS, dtype=str),
    )
