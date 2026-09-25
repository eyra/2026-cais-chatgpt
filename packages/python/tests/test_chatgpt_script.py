"""Behavioural coverage for the CAIS ChatGPT donation extractor."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from port import chatgpt, script
from port.api.commands import FlushLogs
from port.api.props import PropsUIPromptConsentFormTable

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


def load(path: Path) -> list:
    return list(script.iter_conversations(path))


def write_split_export(tmp_path: Path, parts: dict[str, object], listed: list[str], prefix: str = "") -> Path:
    """Current export layout: a manifest listing the conversations files."""
    manifest = {
        "version": 1,
        "logical_files": {"conversations.json": {"files": listed, "sharded": len(listed) > 1}},
    }
    path = tmp_path / "split-export.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{prefix}export_manifest.json", json.dumps(manifest))
        archive.writestr(f"{prefix}sites/export_manifest.json", json.dumps({"omissions": []}))
        for name, payload in parts.items():
            archive.writestr(f"{prefix}{name}", json.dumps(payload))
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


def test_export_without_manifest_accepts_one_nested_conversations_file(tmp_path):
    source = [conversation("One", [])]
    assert load(write_export(tmp_path, source, "chatgpt-export/conversations.json")) == source


@pytest.mark.parametrize("prefix", ["", "rezipped-folder/"])
def test_split_export_yields_all_parts_in_manifest_order(tmp_path, prefix):
    first = [conversation("First", []), conversation("Second", [])]
    second = [conversation("Third", [])]
    path = write_split_export(
        tmp_path,
        {"conversations-001.json": second, "conversations-000.json": first},
        ["conversations-000.json", "conversations-001.json"],
        prefix,
    )
    assert load(path) == first + second


def test_split_export_issue_positions_continue_across_parts(tmp_path):
    path = write_split_export(
        tmp_path,
        {
            "c-0.json": [conversation("Valid", [message("user", timestamp(2026, 9, 20), ["Kept"])])],
            "c-1.json": [{"title": "Broken", "mapping": None}],
        },
        ["c-0.json", "c-1.json"],
    )
    extraction = chatgpt.extract_conversations(script.iter_conversations(path), now=NOW)
    assert extraction.messages["message"].tolist() == ["Kept"]
    assert extraction.issues[["conversation", "reason"]].values.tolist() == [["2", "invalid_mapping"]]


def test_split_export_parses_one_part_at_a_time(tmp_path, monkeypatch):
    path = write_split_export(
        tmp_path,
        {"a.json": [conversation("A", [])], "b.json": [conversation("B", [])]},
        ["a.json", "b.json"],
    )
    parsed = []
    read = script.read_json_member

    def record(archive, member):
        parsed.append(member.filename)
        return read(archive, member)

    monkeypatch.setattr(script, "read_json_member", record)
    conversations = script.iter_conversations(path)
    assert next(conversations)["title"] == "A"
    assert "b.json" not in parsed
    assert next(conversations)["title"] == "B"
    assert parsed[-1] == "b.json"


@pytest.mark.parametrize(
    "parts, listed",
    [
        ({"a.json": []}, ["a.json", "missing.json"]),
        ({"a.json": []}, []),
        ({"a.json": []}, ["a.json", "a.json"]),
        ({"a.json": {"not": "a list"}}, ["a.json"]),
    ],
)
def test_split_export_with_incomplete_or_invalid_manifest_is_rejected(tmp_path, parts, listed):
    with pytest.raises(script.InvalidChatGPTExport):
        load(write_split_export(tmp_path, parts, listed))


@pytest.mark.parametrize(
    "payload, member",
    [
        ([], "other.json"),
        ({}, "conversations.json"),
    ],
)
def test_load_conversations_rejects_invalid_export_structure(tmp_path, payload, member):
    with pytest.raises(script.InvalidChatGPTExport):
        load(write_export(tmp_path, payload, member))


def test_load_conversations_rejects_invalid_zip(tmp_path):
    invalid_zip = tmp_path / "not-a-zip.zip"
    invalid_zip.write_text("not a zip")

    with pytest.raises(script.InvalidChatGPTExport):
        load(invalid_zip)


def test_extraction_keeps_visible_recent_messages_in_descending_time_order():
    extraction = chatgpt.extract_conversations(
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
    frame = extraction.messages

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


@pytest.mark.parametrize(
    "raw",
    [
        b'[{"title":"one","title":"two","mapping":{}}]',
        b'{"not": "a conversation list"}',
        b"[",
        b"\xff",
    ],
)
def test_unreadable_or_ambiguous_json_is_rejected(tmp_path, raw):
    path = tmp_path / "bad-json.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("conversations.json", raw)
    with pytest.raises(script.InvalidChatGPTExport):
        load(path)


def test_duplicate_conversations_members_are_rejected(tmp_path):
    path = tmp_path / "ambiguous.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("one/conversations.json", "[]")
        archive.writestr("two/conversations.json", "[]")
    with pytest.raises(script.InvalidChatGPTExport):
        load(path)


def test_compressed_json_size_is_bounded_before_parsing(tmp_path, monkeypatch):
    monkeypatch.setattr(script, "MAX_EXPORT_JSON_BYTES", 128)
    path = tmp_path / "oversized.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("conversations.json", json.dumps([{"title": "x" * 129}]))
    with pytest.raises(script.InvalidChatGPTExport, match="size limit"):
        load(path)


def test_partial_export_retains_valid_messages_and_reports_failures(tmp_path, caplog):
    payload = [
        conversation("Valid", [message("user", timestamp(2026, 9, 20), ["Kept"])]),
        {"title": "PRIVATE_TITLE", "mapping": None},
        conversation("PRIVATE_TITLE", [message("user", None, ["PRIVATE_TEXT"])]),
    ]
    generator = script.extract_export(write_export(tmp_path, payload), now=NOW)
    with caplog.at_level("WARNING"):
        assert next(generator) == FlushLogs
    with pytest.raises(StopIteration) as finished:
        next(generator)
    extraction, retry = finished.value.value
    assert retry is False
    assert extraction.messages["message"].tolist() == ["Kept"]
    assert extraction.issues.to_dict("records") == [
        {"conversation": "2", "message": "", "reason": "invalid_mapping", "action": "conversation_excluded"},
        {"conversation": "3", "message": "1", "reason": "invalid_timestamp", "action": "message_excluded"},
    ]
    assert "invalid_mapping" in caplog.text
    assert "invalid_timestamp" in caplog.text
    assert "PRIVATE" not in caplog.text


def test_all_failed_export_offers_retry_instead_of_empty_consent(tmp_path):
    payload = [conversation("Failed", [message("user", None, ["Private"])])]
    generator = script.extract_export(write_export(tmp_path, payload), now=NOW)
    assert next(generator) == FlushLogs
    retry_page = next(generator).toDict()
    assert retry_page["page"]["body"][0]["__type__"] == "PropsUIPromptConfirm"
    with pytest.raises(StopIteration) as finished:
        generator.send(SimpleNamespace(__type__="PayloadTrue"))
    assert finished.value.value == (None, True)


def test_old_messages_are_not_reported_as_parsing_failures(tmp_path):
    payload = [conversation("Old", [message("user", timestamp(2020, 1, 1), ["Old"])])]
    generator = script.extract_export(write_export(tmp_path, payload), now=NOW)
    assert next(generator) == FlushLogs
    with pytest.raises(StopIteration) as finished:
        next(generator)
    extraction, retry = finished.value.value
    assert retry is False
    assert extraction.messages.empty
    assert extraction.issues.empty


def test_review_serialization_preserves_all_issues_across_table_limit():
    issues = pd.DataFrame(
        [
            {"conversation": str(index), "message": "", "reason": "invalid_mapping", "action": "conversation_excluded"}
            for index in range(1, 10_002)
        ]
    )
    extraction = chatgpt.ExtractionResult(messages=make_frame([0]), issues=issues)
    tables = [
        item.toDict()
        for item in script.prompt_consent(extraction)
        if isinstance(item, PropsUIPromptConsentFormTable)
    ]
    assert [table["id"] for table in tables] == [
        "chatgpt_conversations_1", "chatgpt_processing_issues_1", "chatgpt_processing_issues_2"
    ]
    assert list(json.loads(tables[0]["data_frame"])) == script.MESSAGE_COLUMNS
    reported = [json.loads(table["data_frame"]) for table in tables[1:]]
    assert [len(table["conversation"]) for table in reported] == [10_000, 1]
    assert [
        value for table in reported for value in table["conversation"].values()
    ] == issues["conversation"].tolist()
