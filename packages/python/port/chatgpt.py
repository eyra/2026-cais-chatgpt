"""Extract reviewable ChatGPT messages and privacy-safe processing issues.

The five donated fields and their values follow trbKnl/port-chatgpt-uu
(AGPL-3.0), commit d80e9d834095034c9e80c85986bdab61fb8f152a, verified per
message against real exports. Fields are read from their known locations only;
value encodings that were never observed are reported, not guessed.
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
# Observed reasoning content without parts; the reference donates these as
# rows with an empty message. Other content types without parts are unknown.
EMPTY_TEXT_CONTENT_TYPES = frozenset({"thoughts", "reasoning_recap"})


@dataclass(frozen=True)
class ExtractionResult:
    messages: pd.DataFrame
    issues: pd.DataFrame


def _valid_text(value: Any) -> bool:
    """Pandas' JSON encoder cannot serialize isolated Unicode surrogates."""
    return isinstance(value, str) and _SURROGATE.search(value) is None


def _hidden_flag(value: Any) -> bool | None:
    """None means invalid. The flag is normally absent; only booleans are known."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return None


def _timestamp(value: Any) -> datetime | None:
    """Exports use Unix seconds as numbers; any other encoding is unknown."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _content_text(content: Any) -> str | None:
    """Flatten parts like the reference: ordered scalar leaves, str()-converted.

    Nulls become "None" and booleans "True"/"False", as in the reference. The
    parts list has depth zero; descendants may be at most 64 edges deep.
    Iteration bounds deep input without relying on Python recursion. None
    signals malformed content, not a partially recovered message.
    """
    if not isinstance(content, dict):
        return None
    if "parts" not in content:
        return "" if content.get("content_type") in EMPTY_TEXT_CONTENT_TYPES else None
    parts = content["parts"]
    if not isinstance(parts, list):
        return None
    leaves = []
    pending = [(parts, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 64:
            return None
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in reversed(value.values()))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in reversed(value))
        elif isinstance(value, str):
            if not _valid_text(value):
                return None
            leaves.append(value)
        elif value is None:
            leaves.append("None")
        elif isinstance(value, (bool, int)):
            leaves.append(str(value))
        elif isinstance(value, float) and math.isfinite(value):
            leaves.append(str(value))
        else:
            return None
    return "".join(leaves)


def extract_conversations(
    conversations: Iterable[Any], now: datetime | None = None
) -> ExtractionResult:
    """Keep the last UTC calendar year, reporting each excluded malformed record.

    Locations are 1-based positions across the whole export (all parts), never
    export IDs or participant text. Conversations are consumed once, in order,
    so a lazy iterable keeps only the current export part in memory.
    The first invalid field determines the issue for an excluded record. Hidden
    messages and ordinary root placeholders are intentionally skipped. Naive
    clocks are interpreted as UTC; aware clocks are normalized to UTC.
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
            issue(conversation_position, None, "invalid_conversation")
            continue
        title = conversation.get("title")
        if not _valid_text(title):
            issue(conversation_position, None, "invalid_title")
            continue
        mapping = conversation.get("mapping")
        if not isinstance(mapping, dict):
            issue(conversation_position, None, "invalid_mapping")
            continue
        for message_position, node in enumerate(mapping.values(), start=1):
            if not isinstance(node, dict) or "message" not in node:
                issue(conversation_position, message_position, "invalid_message")
                continue
            message = node["message"]
            if message is None and node.get("parent") is None:
                continue
            if not isinstance(message, dict):
                issue(conversation_position, message_position, "invalid_message")
                continue
            metadata = message.get("metadata")
            if metadata is None:
                metadata = {}
            if not isinstance(metadata, dict):
                issue(conversation_position, message_position, "invalid_metadata")
                continue
            hidden = _hidden_flag(metadata.get("is_visually_hidden_from_conversation"))
            if hidden is None:
                issue(conversation_position, message_position, "invalid_hidden_flag")
                continue
            if hidden:
                continue
            created = _timestamp(message.get("create_time"))
            if created is None:
                issue(conversation_position, message_position, "invalid_timestamp")
                continue
            if created > current:
                issue(conversation_position, message_position, "future_timestamp")
                continue
            if created < cutoff:
                continue
            author = message.get("author")
            role = author.get("role") if isinstance(author, dict) else None
            if not _valid_text(role) or not role.strip():
                issue(conversation_position, message_position, "invalid_role")
                continue
            model = metadata.get("model_slug")
            if model is None:
                model = ""
            if not _valid_text(model):
                issue(conversation_position, message_position, "invalid_model")
                continue
            text = _content_text(message.get("content"))
            if text is None:
                issue(conversation_position, message_position, "invalid_content")
                continue
            try:
                local_time = created.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OverflowError, OSError):
                issue(conversation_position, message_position, "invalid_timestamp")
                continue
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
