"""Behavioural coverage for the CAIS ChatGPT donation extractor."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import zipfile

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from port import script

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def timestamp(year: int, month: int, day: int, hour: int = 12) -> float:
    return datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp()


def message(
    role: str,
    created_at: float,
    parts: list[object],
    *,
    model: str | None = None,
    hidden: bool = False,
) -> dict:
    metadata = {"is_visually_hidden_from_conversation": hidden}
    if model is not None:
        metadata["model_slug"] = model
    return {
        "message": {
            "author": {"role": role},
            "create_time": created_at,
            "content": {"parts": parts},
            "metadata": metadata,
        }
    }


def conversation(title: str, messages: list[dict]) -> dict:
    return {
        "title": title,
        "mapping": {f"node-{index}": entry for index, entry in enumerate(messages)},
    }


def write_export(tmp_path: Path, payload: object, member: str = "conversations.json") -> Path:
    path = tmp_path / "chatgpt-export.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, json.dumps(payload))
    return path


def make_frame(conversations: list[int]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "conversation title": [f"Conversation {identifier}" for identifier in conversations],
            "role": ["user"] * len(conversations),
            "message": [str(index) for index in range(len(conversations))],
            "model": [""] * len(conversations),
            "time": ["2026-09-21 12:00:00"] * len(conversations),
            "_conversation": conversations,
        }
    )


def test_load_conversations_accepts_nested_export_member(tmp_path):
    source = [conversation("One", [])]

    loaded = script.load_conversations(
        write_export(tmp_path, source, "chatgpt-export/conversations.json")
    )

    assert loaded == source


@pytest.mark.parametrize(
    "payload, member",
    [
        ([], "other.json"),
        ({}, "conversations.json"),
        ([{"title": "Missing mapping"}], "conversations.json"),
        ([conversation("Valid", []), {"title": "Missing mapping"}], "conversations.json"),
    ],
)
def test_load_conversations_rejects_invalid_export_structure(tmp_path, payload, member):
    with pytest.raises(script.InvalidChatGPTExport):
        script.load_conversations(write_export(tmp_path, payload, member))


def test_load_conversations_rejects_invalid_zip(tmp_path):
    invalid_zip = tmp_path / "not-a-zip.zip"
    invalid_zip.write_text("not a zip")

    with pytest.raises(script.InvalidChatGPTExport):
        script.load_conversations(invalid_zip)


def test_extraction_keeps_visible_recent_messages_in_descending_time_order():
    frame = script.conversations_to_dataframe(
        [
            conversation(
                "Older conversation",
                [
                    message("user", timestamp(2025, 9, 21), ["Cutoff message"], model="gpt-4"),
                    message("assistant", timestamp(2025, 9, 20), ["Too old"], model="gpt-4"),
                    message("assistant", timestamp(2026, 8, 1), ["Hidden"], hidden=True),
                ],
            ),
            conversation(
                "Newest conversation",
                [
                    message("assistant", timestamp(2026, 9, 20), ["Newest ", "message"], model="gpt-5"),
                    message("user", timestamp(2026, 9, 1), ["Question", 42]),
                    message("", timestamp(2026, 9, 2), ["Missing role"]),
                ],
            ),
        ],
        now=NOW,
    )

    assert list(frame.columns) == [*script.MESSAGE_COLUMNS, "_conversation"]
    assert frame[script.MESSAGE_COLUMNS].to_dict("records") == [
        {
            "conversation title": "Newest conversation",
            "role": "assistant",
            "message": "Newest message",
            "model": "gpt-5",
            "time": datetime.fromtimestamp(timestamp(2026, 9, 20)).strftime("%Y-%m-%d %H:%M:%S"),
        },
        {
            "conversation title": "Newest conversation",
            "role": "user",
            "message": "Question42",
            "model": "",
            "time": datetime.fromtimestamp(timestamp(2026, 9, 1)).strftime("%Y-%m-%d %H:%M:%S"),
        },
        {
            "conversation title": "Older conversation",
            "role": "user",
            "message": "Cutoff message",
            "model": "gpt-4",
            "time": datetime.fromtimestamp(timestamp(2025, 9, 21)).strftime("%Y-%m-%d %H:%M:%S"),
        },
    ]


def test_partition_moves_a_complete_next_conversation_to_the_next_table():
    frame = make_frame([0] * 9_999 + [1] * 2)

    tables = script.partition_dataframe(frame)

    assert [len(table) for table in tables] == [9_999, 2]
    assert tables[0]["conversation title"].iloc[-1] == "Conversation 0"
    assert tables[1]["conversation title"].iloc[0] == "Conversation 1"


def test_partition_splits_a_conversation_larger_than_the_limit_without_losing_rows():
    frame = make_frame([0] * 10_001)

    tables = script.partition_dataframe(frame)

    assert [len(table) for table in tables] == [10_000, 1]
    assert pd.concat(tables, ignore_index=True)["message"].to_list() == frame["message"].to_list()


def test_partition_preserves_an_empty_result_table():
    table = script.partition_dataframe(pd.DataFrame(columns=[*script.MESSAGE_COLUMNS, "_conversation"]))

    assert len(table) == 1
    assert list(table[0].columns) == script.MESSAGE_COLUMNS
    assert table[0].empty


@pytest.mark.parametrize("row_limit", [9_999, 50_001])
def test_table_row_limit_is_constrained_to_the_framework_range(row_limit):
    with pytest.raises(ValueError, match="between 10000 and 50000"):
        script.validate_table_row_limit(row_limit)
