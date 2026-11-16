"""Extract reviewable ChatGPT messages; never reject an export for its content.

The five donated fields and their values follow trbKnl/port-chatgpt-uu
(AGPL-3.0), commit d80e9d834095034c9e80c85986bdab61fb8f152a, verified per
message against real exports, with one exception: an object or list part of a
message (an image) is donated as NON_TEXT_PART instead of its flattened
fields. Fields are read from their known locations only; value encodings that
were never observed are not guessed.

A problem in a conversation or message never rejects the export (Flux
10507872361): what is readable is kept, nothing is invented, and nothing is
dropped without an issue. Issues carry a reason with the value's type, never
the value, and one of these actions:
- message_kept / conversation_kept: a field could not be read and is empty
  (title, role, model) or UNREADABLE_PART (content or a text part), or the
  time was unusable or in the future (see _message_fields). Reported only
  for messages that are donated.
- message_excluded / conversation_excluded: nothing readable remains (no
  message object, a conversation without a mapping), or the content type is
  unknown.
"""

import calendar
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import heapq
import json
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
# Donated in place of each object or list part of a message, such as an image
# asset pointer, so no file ids are donated.
NON_TEXT_PART = "[image]"
# Donated in place of a text part that cannot be read: a non-finite number, or
# text that donation JSON cannot carry.
UNREADABLE_PART = "[unreadable]"


@dataclass(frozen=True)
class ExtractionResult:
    messages: pd.DataFrame
    issues: pd.DataFrame
    # Visible messages dated before the window (not parsed further): tells
    # "nothing recent" apart from "no messages at all" when `messages` is empty.
    outside_window: int = 0
    outside_byte_limit: int = 0


class _OutsideWindow(Exception):
    pass


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


def _type_name(container: dict, key: str) -> str:
    """Type of a field's value for logs, never the value; "absent" if missing."""
    return type(container[key]).__name__ if key in container else "absent"


def _text_field(container: dict, key: str, field: str, kept: list[str]) -> str:
    """A string field, or "" (reported in `kept`) if it cannot be read."""
    value = container.get(key)
    if value is None and field == "model":
        return ""  # User messages have no model.
    if not isinstance(value, str):
        kept.append(f"invalid_{field}:{_type_name(container, key)}")
        return ""
    if not _valid_text(value):
        kept.append(f"invalid_text:{field}")
        return ""
    return value


def _local_time(created: datetime) -> str:
    return created.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _timestamp(value: Any) -> datetime | None:
    """Exports use Unix seconds as numbers; anything else is unusable (None).

    So is a number that UTC or local time cannot represent (NaN, ±inf,
    milliseconds, ...).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        created = datetime.fromtimestamp(value, timezone.utc)
        _local_time(created)
    except (ValueError, OverflowError, OSError):
        return None
    return created


def _ancestor_time(mapping: dict, node: dict, times: dict[str, datetime | None]) -> datetime | None:
    """Time of the nearest dated ancestor via parent links, at most 64 steps up.

    Mapping order is node-id order, not time order: a parent may come after
    its child, so the previous node in the file is no substitute. `times`
    caches parsed node times for the conversation.
    """
    for _ in range(64):
        parent_id = node.get("parent")
        parent = mapping.get(parent_id) if isinstance(parent_id, str) else None
        if not isinstance(parent, dict):
            return None
        if parent_id not in times:
            message = parent.get("message")
            times[parent_id] = _timestamp(message.get("create_time")) if isinstance(message, dict) else None
        if times[parent_id] is not None:
            return times[parent_id]
        node = parent
    return None


def _inferred_time(
    mapping: dict, node: dict, times: dict[str, datetime | None],
    updated: datetime | None, created: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    """(inferred time, window time) for a message without a usable time.

    The inferred time is the nearest dated ancestor's, else the conversation's
    update_time, then its create_time. The window time is the later of the
    inferred time and the conversation's update_time, or None (inside) if
    update_time is unusable: in doubt, keep.
    """
    inferred = _ancestor_time(mapping, node, times)
    if inferred is None:
        inferred = updated if updated is not None else created
    return inferred, None if updated is None else max(inferred, updated)


def _content_text(message: dict, kept: list[str]) -> str:
    """Join the parts; each object or list part becomes NON_TEXT_PART.

    The reference flattens an object part (an image asset pointer) into its
    str()-converted scalar leaves, which donates file ids as text; CAIS donates
    a placeholder instead (Flux 10452034951), so such a part is not read
    further. Scalar parts are str()-converted as in the reference; one that
    cannot be read becomes UNREADABLE_PART, as does content that is not an
    object with a list of parts. Raises _Skip for an unknown content type.
    """
    content = message.get("content")
    if not isinstance(content, dict):
        kept.append(f"invalid_content:{_type_name(message, 'content')}")
        return UNREADABLE_PART
    if "parts" not in content:
        content_type = content.get("content_type")
        if content_type in EMPTY_TEXT_CONTENT_TYPES:
            return ""
        raise _Skip(f"unknown_content_type:{_label(content_type)}")
    parts = content["parts"]
    if not isinstance(parts, list):
        kept.append(f"invalid_content:{type(parts).__name__}")
        return UNREADABLE_PART
    texts = []
    for part in parts:
        if isinstance(part, (dict, list)):
            texts.append(NON_TEXT_PART)
        elif isinstance(part, str) and not _valid_text(part):
            kept.append("invalid_text:part")
            texts.append(UNREADABLE_PART)
        elif isinstance(part, float) and not math.isfinite(part):
            kept.append("invalid_content:float")
            texts.append(UNREADABLE_PART)
        else:
            # Scalars as in the reference: nulls "None", booleans "True"/"False".
            texts.append(part if isinstance(part, str) else str(part))
    return "".join(texts)


def _message_fields(
    message: dict,
    current: datetime,
    cutoff: datetime | None,
    infer: Callable[[], tuple[datetime | None, datetime | None]],
) -> tuple[datetime, str, str, str, str, list[str]] | None:
    """(sort time, role, text, model, local time, kept reasons), or None if hidden.

    Without a usable create_time, the window and sort order use the inferred
    time (at most the current time) and the local time is empty. A message
    whose time cannot be inferred at all, or that is dated in the future,
    counts as current for the window and the sort order; a future message
    keeps its own local time. Raises _OutsideWindow for a message older than
    the cutoff, _Skip if nothing readable remains.
    """
    kept: list[str] = []
    metadata = message.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        kept.append(f"invalid_metadata:{type(metadata).__name__}")
        metadata = {}
    hidden = metadata.get("is_visually_hidden_from_conversation")
    if hidden is True:
        return None
    if hidden is not None and hidden is not False:
        # Only booleans have been observed; in doubt, show the message.
        kept.append(f"invalid_hidden_flag:{type(hidden).__name__}")
    created = _timestamp(message.get("create_time"))
    local_time = ""
    if created is None:
        # Type only, never the value; "absent" for a missing key.
        value_type = _type_name(message, "create_time")
        created, window_time = infer()
        if created is None:
            created, window_time = current, None
            kept.append(f"timestamp_unknown_kept:{value_type}")
        else:
            created = min(created, current)
            kept.append(f"timestamp_inferred:{value_type}")
    elif created > current:
        # "Now" is the participant's clock, which may be wrong: donate as is.
        local_time = _local_time(created)
        created = window_time = current
        kept.append("timestamp_future_kept")
    else:
        local_time = _local_time(created)
        window_time = created
    if cutoff is not None and window_time is not None and window_time < cutoff:
        raise _OutsideWindow
    author = message.get("author")
    if not isinstance(author, dict):
        kept.append(f"invalid_role:{type(author).__name__}")
        role = ""
    elif author.get("role") == "":
        return None  # The reference drops rows with an empty role.
    else:
        role = _text_field(author, "role", "role", kept)
    model = _text_field(metadata, "model_slug", "model", kept)
    text = _content_text(message, kept)
    # One issue per reason and message.
    return created, role, text, model, local_time, list(dict.fromkeys(kept))


def months_before(value: datetime, months: int) -> datetime:
    """Same day and time `months` calendar months earlier, clamped to month end."""
    year, month = divmod(value.year * 12 + value.month - 1 - months, 12)
    day = min(value.day, calendar.monthrange(year, month + 1)[1])
    return value.replace(year=year, month=month + 1, day=day)


def extract_conversations(
    conversations: Iterable[Any],
    now: datetime | None = None,
    window_months: int | None = None,
    max_data_bytes: int | None = None,
) -> ExtractionResult:
    """Keep messages from the last `window_months` calendar months (UTC), newest first.

    window_months=None disables the date cutoff. max_data_bytes optionally bounds
    the compact UTF-8 JSON row array, retaining a newest-first prefix of whole
    messages. Skipped conversations and messages, and kept ones with an
    unreadable field or time, are returned as issues. Locations are
    1-based positions across the whole export (all parts), never export IDs or
    participant text. Conversations are consumed once, in order, so a lazy
    iterable keeps only the current export part in memory. Hidden messages and
    ordinary root placeholders are intentionally skipped. Naive clocks are
    interpreted as UTC; aware clocks are normalized to UTC.
    """
    if window_months is not None and (isinstance(window_months, bool) or window_months < 1):
        raise ValueError(f"window_months must be a positive number of months or None, got {window_months!r}")
    if max_data_bytes is not None and (
        isinstance(max_data_bytes, bool) or not isinstance(max_data_bytes, int) or max_data_bytes < 2
    ):
        raise ValueError("max_data_bytes must fit at least an empty JSON array")
    if now is None:
        now = datetime.now(timezone.utc)
    current = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    cutoff = None if window_months is None else months_before(current, window_months)
    rows = []
    issues = []
    outside_window = 0
    outside_byte_limit = 0
    row_bytes = 0
    sequence = 0
    discarded_through = None
    title_kept: dict[int, list[str]] = {}

    def issue(conversation: int, message: int | None, reason: str, action: str | None = None) -> None:
        if action is None:
            action = "conversation_excluded" if message is None else "message_excluded"
        issues.append({
            "conversation": str(conversation),
            "message": "" if message is None else str(message),
            "reason": reason,
            "action": action,
        })

    for conversation_position, conversation in enumerate(conversations, start=1):
        if not isinstance(conversation, dict):
            issue(conversation_position, None, f"invalid_conversation:{type(conversation).__name__}")
            continue
        mapping = conversation.get("mapping")
        if not isinstance(mapping, dict):
            issue(conversation_position, None, f"invalid_mapping:{_type_name(conversation, 'mapping')}")
            continue
        title_issues: list[str] = []
        title = _text_field(conversation, "title", "title", title_issues)
        if title_issues:
            # Reported once a message of the conversation is donated.
            title_kept[conversation_position] = title_issues
        conversation_updated = _timestamp(conversation.get("update_time"))
        conversation_created = _timestamp(conversation.get("create_time"))
        node_times: dict[str, datetime | None] = {}
        for message_position, node in enumerate(mapping.values(), start=1):
            if not isinstance(node, dict):
                issue(conversation_position, message_position, f"invalid_message:{type(node).__name__}")
                continue
            message = node.get("message")
            if message is None and node.get("parent") is None:
                continue  # A root placeholder.
            if not isinstance(message, dict):
                issue(conversation_position, message_position, f"invalid_message:{_type_name(node, 'message')}")
                continue
            try:
                fields = _message_fields(
                    message, current, cutoff,
                    lambda: _inferred_time(mapping, node, node_times, conversation_updated, conversation_created),
                )
            except _Skip as error:
                issue(conversation_position, message_position, error.reason)
                continue
            except _OutsideWindow:
                outside_window += 1
                continue
            if fields is None:
                continue
            sort_time, role, text, model, local_time, kept = fields
            # The encounter index preserves stable ordering for equal timestamps.
            key = (sort_time, -sequence)
            sequence += 1
            if discarded_through is not None and key <= discarded_through:
                outside_byte_limit += 1
                continue
            row = {
                "conversation title": title,
                "role": role,
                "message": text,
                "model": model,
                "time": local_time,
            }
            size = 0
            if max_data_bytes is not None:
                # Count a comma per row; the complete nonempty array then adds
                # one byte (two brackets minus the first row's absent comma).
                size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
            row["_conversation"] = conversation_position - 1
            # Reported after selection, so the byte budget can still exclude it.
            row["_kept"] = [(conversation_position, message_position, reason) for reason in kept]
            entry = (*key, size, row)
            if max_data_bytes is None:
                rows.append(entry)
            else:
                heapq.heappush(rows, entry)
                row_bytes += size
                while rows and row_bytes + 1 > max_data_bytes:
                    oldest = heapq.heappop(rows)
                    row_bytes -= oldest[2]
                    discarded_through = oldest[:2]
                    outside_byte_limit += 1
                # Keep the cutoff even if the heap becomes empty. Otherwise a
                # later, older small message could bypass an oversized newer one.

    rows.sort(reverse=True)
    for *_, row in rows:
        for conversation, message, reason in row["_kept"]:
            issue(conversation, message, reason, "message_kept")
    for conversation in {row["_conversation"] + 1 for *_, row in rows} & title_kept.keys():
        for reason in title_kept[conversation]:
            issue(conversation, None, reason, "conversation_kept")
    # In export order: conversation issues before their messages' issues.
    issues.sort(key=lambda item: (int(item["conversation"]), int(item["message"] or 0)))
    return ExtractionResult(
        messages=pd.DataFrame([row[3] for row in rows], columns=[*MESSAGE_COLUMNS, "_conversation"]),
        issues=pd.DataFrame(issues, columns=ISSUE_COLUMNS, dtype=str),
        outside_window=outside_window,
        outside_byte_limit=outside_byte_limit,
    )
