"""Behavioural coverage for the CAIS ChatGPT donation extractor."""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import textwrap
import zipfile

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from port import chatgpt, script

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
    canvas = message("user", timestamp(2026, 9, 20), [])
    canvas["message"]["content"] = {"content_type": "canvas", "text": "private"}
    path = write_split_export(
        tmp_path,
        {
            "c-0.json": [conversation("Valid", [message("user", timestamp(2026, 9, 20), ["Kept"])])],
            "c-1.json": [conversation("Canvas", [canvas])],
        },
        ["c-0.json", "c-1.json"],
    )
    extraction = chatgpt.extract_conversations(script.iter_conversations(path), now=NOW)
    assert extraction.messages["message"].tolist() == ["Kept"]
    assert extraction.issues[["conversation", "reason"]].values.tolist() == [["2", "unknown_content_type:canvas"]]


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
        window_months=12,
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


def canvas_message(created_at: float) -> dict:
    unusable = message("user", created_at, [])
    unusable["message"]["content"] = {"content_type": "canvas", "text": "PRIVATE_TEXT"}
    return unusable


def test_partial_export_keeps_valid_messages_and_logs_one_summary(tmp_path, caplog):
    payload = [
        conversation("Valid", [message("user", timestamp(2026, 9, 20), ["Kept"])]),
        conversation("PRIVATE_TITLE", [canvas_message(timestamp(2026, 9, 19)), canvas_message(timestamp(2026, 9, 18))]),
        conversation("PRIVATE_TITLE", [message("user", timestamp(2026, 9, 22), ["PRIVATE_TEXT"])]),
    ]
    with caplog.at_level("WARNING", logger="port.script"):
        extraction, problem = script.extract_export(write_export(tmp_path, payload), now=NOW)
    assert problem is None
    assert extraction.messages["message"].tolist() == ["Kept"]
    warnings = [record.getMessage() for record in caplog.records if record.name == "port.script"]
    assert warnings == [
        "Processing issues: unknown_content_type:canvas message_excluded=2, future_timestamp message_excluded=1"
    ]


def test_format_change_rejects_export_and_logs_its_position(tmp_path, caplog):
    payload = [
        conversation("Valid", [message("user", timestamp(2026, 9, 20), ["Kept"])]),
        conversation("PRIVATE_TITLE", [message("user", "2026-09-20T12:00:00Z", ["PRIVATE_TEXT"])]),
    ]
    with caplog.at_level("WARNING", logger="port.script"):
        assert script.extract_export(write_export(tmp_path, payload), now=NOW) == (None, "unreadable")
    assert caplog.records[-1].getMessage() == (
        "Rejected ChatGPT export: unsupported format, reason=invalid_timestamp conversation=2 message=1"
    )
    assert "PRIVATE" not in caplog.text


def test_export_with_only_skipped_messages_is_rejected(tmp_path):
    payload = [conversation("Failed", [canvas_message(timestamp(2026, 9, 20))])]
    assert script.extract_export(write_export(tmp_path, payload), now=NOW) == (None, "unusable")


@pytest.mark.parametrize("payload, outside_window", [
    ([conversation("Old", [message("user", timestamp(2020, 1, 1), ["Old"])])], 1),
    ([conversation("Empty", [])], 0),
])
def test_nothing_in_the_window_still_reaches_consent_not_failures(tmp_path, payload, outside_window):
    extraction, problem = script.extract_export(write_export(tmp_path, payload), now=NOW)
    assert problem is None
    assert extraction.messages.empty
    assert extraction.issues.empty
    assert extraction.outside_window == outside_window


@pytest.mark.parametrize("window, kept", [(12, []), (24, ["20 months old"]), (None, ["20 months old", "2020"])])
def test_time_window_setting_selects_messages(tmp_path, monkeypatch, window, kept):
    monkeypatch.setattr(script, "TIME_WINDOW_MONTHS", window)
    payload = [conversation("Mixed", [
        message("user", timestamp(2025, 1, 21), ["20 months old"]),
        message("user", timestamp(2020, 1, 1), ["2020"]),
    ])]
    extraction, _ = script.extract_export(write_export(tmp_path, payload), now=NOW)
    assert extraction.messages["message"].tolist() == kept


FLOW = textwrap.dedent("""
    import json, sys
    from types import SimpleNamespace
    from port import script
    from port.api.commands import CommandSystemDonate

    answers = {
        "PropsUIPromptFileInput": SimpleNamespace(__type__="PayloadFile", value=sys.argv[1]),
        "PropsUIPromptConfirm": SimpleNamespace(__type__="PayloadFalse", value=None),
        "PropsUIDataSubmissionButtons": SimpleNamespace(__type__=sys.argv[2], value="{}"),
    }
    flow = script.process({"sessionId": "s"})
    donations = []
    try:
        command = next(flow)
        while True:
            if isinstance(command, CommandSystemDonate):
                donations.append((command.key, command.json_string))
                command = flow.send(None)
            else:
                body = command.toDict()["page"]["body"]
                command = flow.send(answers[body[-1]["__type__"]])
    except StopIteration:
        pass
    print(json.dumps(donations))
""")


def run_flow(path: Path, consent: str = "PayloadJSON") -> list[tuple[str, object]]:
    """Drive process() in a fresh interpreter, as in Pyodide: no host logging setup."""
    completed = subprocess.run(
        [sys.executable, "-c", FLOW, str(path), consent],
        cwd=Path(__file__).parent.parent, capture_output=True, text=True, check=True,
    )
    return [(key, json.loads(value)) for key, value in json.loads(completed.stdout)]


@pytest.mark.parametrize("consent", ["PayloadJSON", "PayloadFalse"])
def test_tracking_is_donated_whatever_the_consent_decision(tmp_path, consent):
    payload = [
        conversation("Valid", [message("user", datetime.now(timezone.utc).timestamp() - 60, ["Kept"])]),
        conversation("PRIVATE_TITLE", [canvas_message(datetime.now(timezone.utc).timestamp() - 60)]),
    ]
    donations = run_flow(write_export(tmp_path, payload), consent)
    assert [key for key, _ in donations if key != "s-tracking"] == ["s-chatgpt-conversations"]
    assert donations[-1][0] == "s-tracking"
    tracking = "\n".join(donations[-1][1])
    assert "Processing issues: unknown_content_type:canvas message_excluded=1" in tracking
    assert ("Data donated" if consent == "PayloadJSON" else "Data submission declined") in tracking
    assert "PRIVATE" not in tracking


def test_tracking_records_a_format_change_rejection(tmp_path):
    payload = [conversation("PRIVATE_TITLE", [message("user", "2026-09-20T12:00:00Z", ["PRIVATE_TEXT"])])]
    donations = run_flow(write_export(tmp_path, payload))
    assert {key for key, _ in donations} == {"s-tracking"}
    tracking = "\n".join(donations[-1][1])
    assert "unsupported format, reason=invalid_timestamp conversation=1 message=1" in tracking
    assert "Skipped during retry flow" in tracking
    assert "PRIVATE" not in tracking


def test_empty_window_still_donates_and_tracks_why(tmp_path):
    payload = [conversation("PRIVATE_TITLE", [message("user", timestamp(2020, 1, 1), ["PRIVATE_TEXT"])])]
    donations = dict(run_flow(write_export(tmp_path, payload)))
    assert set(donations) == {"s-tracking", "s-chatgpt-conversations"}
    tracking = "\n".join(donations["s-tracking"])
    assert "Extracted 0 ChatGPT messages (1 outside the time window)" in tracking
    assert "Data donated" in tracking
    assert "PRIVATE" not in tracking
