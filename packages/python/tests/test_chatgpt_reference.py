"""Public extraction contracts. Donated values mirror the Utrecht reference,
verified message-by-message against real exports; only observed encodings are
accepted."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from port import chatgpt

NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
CREATED = datetime(2026, 1, 15, 12, tzinfo=timezone.utc).timestamp()


def node(**overrides):
    message = {
        "author": {"role": "user"},
        "content": {"parts": ["Hello", " world"]},
        "metadata": {"model_slug": "gpt-4"},
        "create_time": CREATED,
    }
    message.update(overrides)
    return {"message": message}


def conversation(mapping=None, **overrides):
    value = {"title": "Same title", "mapping": {"turn": node()} if mapping is None else mapping}
    value.update(overrides)
    return value


def extract(*conversations, now=NOW):
    return chatgpt.extract_conversations(list(conversations), now=now)


@pytest.fixture
def local_zone():
    if not hasattr(time, "tzset"):
        pytest.skip("requires runtime timezone selection")
    previous = os.environ.get("TZ")

    def select(zone):
        os.environ["TZ"] = zone
        time.tzset()

    try:
        yield select
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.mark.parametrize("zone, clock", [("UTC", "12"), ("Europe/Amsterdam", "13")])
def test_ordinary_donation_json_retains_original_values(local_zone, zone, clock):
    local_zone(zone)
    source = conversation({
        "root": {"message": None},
        "question": node(),
        "answer": node(author={"role": "assistant"}, content={"parts": ["**Yes**\n", "[source](https://example.org)"]}),
    })
    result = extract(source)
    expected = [
        {"conversation title": "Same title", "role": "user", "message": "Hello world", "model": "gpt-4", "time": f"2026-01-15 {clock}:00:00"},
        {"conversation title": "Same title", "role": "assistant", "message": "**Yes**\n[source](https://example.org)", "model": "gpt-4", "time": f"2026-01-15 {clock}:00:00"},
    ]
    assert json.loads(result.messages[chatgpt.MESSAGE_COLUMNS].to_json(orient="records")) == expected
    assert result.issues.empty


def test_similar_keys_cannot_override_explicit_fields_or_inject_text(local_zone):
    local_zone("UTC")
    turn = node()
    turn.update({
        "speaker_role": "tool", "create_time": NOW.timestamp(),
        "first": {"model_slug": "wrong-model"}, "parts_extra": ["private node data"],
        "is_visually_hidden_from_conversation": True,
    })
    turn["message"]["author"]["other_role"] = "wrong-role"
    turn["message"]["metadata"].update({"parts": ["private metadata"], "other_model_slug": "wrong-model"})
    turn["message"]["content"]["parts_extra"] = ["private content metadata"]
    result = extract(conversation({"node": turn}))
    assert result.messages[["role", "message", "model"]].values.tolist() == [["user", "Hello world", "gpt-4"]]
    assert result.messages["time"].tolist() == ["2026-01-15 12:00:00"]
    assert result.issues.empty


def test_ordered_content_leaves_preserve_literal_text_and_only_parts():
    literal = "  <script>not markup</script>\n**bold** [link](https://example.org) citeturn1 \\n"
    content = {
        "parts": [literal, {"content_type": "image", "asset_pointer": "asset://one", "width": 3}, [True, None, -2, 1.5], {"nested": {"citation": "[1]"}}, {}, []],
        "text": "ignored fallback", "part_metadata": "ignored metadata",
    }
    result = extract(conversation({"node": node(content=content)}))
    assert result.messages["message"].tolist() == [literal + "imageasset://one3TrueNone-21.5[1]"]
    assert result.issues.empty


def test_real_export_image_part_is_flattened_like_the_reference():
    # Shape of a multimodal_text message in current exports (values synthetic).
    image = {
        "content_type": "image_asset_pointer", "asset_pointer": "sediment://file_x",
        "size_bytes": 10, "width": 2, "height": 3, "fovea": None,
        "metadata": {"dalle": None, "sanitized": True},
    }
    turn = node(content={"content_type": "multimodal_text", "parts": [image, "Describe"]})
    turn["message"]["author"]["name"] = None
    result = extract(conversation({"node": turn}))
    assert result.messages["message"].tolist() == ["image_asset_pointersediment://file_x1023NoneNoneTrueDescribe"]
    assert result.issues.empty


@pytest.mark.parametrize("content", [
    {"content_type": "thoughts", "thoughts": [{"summary": "s", "content": "private reasoning"}], "source_analysis_msg_id": "x"},
    {"content_type": "reasoning_recap", "content": "Thought for 5s"},
])
def test_reasoning_messages_are_rows_with_empty_message_like_the_reference(content):
    result = extract(conversation({"node": node(author={"role": "assistant"}, content=content)}))
    assert result.messages[["role", "message", "model"]].values.tolist() == [["assistant", "", "gpt-4"]]
    assert result.issues.empty


def test_all_branches_and_duplicate_titles_keep_stable_independent_identity():
    source = conversation({
        "root": {"message": None, "parent": None},
        "left": node(content={"parts": ["left"]}),
        "right": node(content={"parts": ["right"]}),
        "newer": node(content={"parts": ["newer"]}, create_time=CREATED + 1),
    }, current_node="left")
    result = extract(source, conversation({"same-id": node(content={"parts": ["other conversation"]})}))
    assert result.messages["message"].tolist() == ["newer", "left", "right", "other conversation"]
    assert result.messages["_conversation"].tolist() == [0, 0, 0, 1]
    assert result.issues.empty


@pytest.mark.parametrize("metadata", [None, {}, {"model_slug": None}])
def test_optional_model_metadata_and_unlisted_role_are_supported(metadata):
    result = extract(conversation({"node": node(author={"role": "custom-tool"}, metadata=metadata)}, title=""))
    assert result.messages[["conversation title", "role", "model"]].values.tolist() == [["", "custom-tool", ""]]
    assert result.issues.empty


def test_absent_metadata_is_an_empty_model():
    source = conversation()
    del source["mapping"]["turn"]["message"]["metadata"]
    result = extract(source)
    assert result.messages[["message", "model"]].values.tolist() == [["Hello world", ""]]
    assert result.issues.empty


def test_hidden_messages_skip_invalid_remaining_fields():
    result = extract(conversation({"hidden": {"message": {"metadata": {"is_visually_hidden_from_conversation": True, "model_slug": []}}}}))
    assert result.messages.empty
    assert result.issues.empty


@pytest.mark.parametrize("flag", [None, False])
def test_false_hidden_flags_keep_messages(flag):
    result = extract(conversation({"visible": node(metadata={"is_visually_hidden_from_conversation": flag})}))
    assert result.messages["message"].tolist() == ["Hello world"]
    assert result.issues.empty


@pytest.mark.parametrize("bad, reason", [
    (None, "invalid_conversation"),
    ({"title": 3, "mapping": {}}, "invalid_title"),
    ({"title": None, "mapping": {}}, "invalid_title"),
    ({"mapping": {}}, "invalid_title"),
    ({"title": "t", "mapping": None}, "invalid_mapping"),
    ({"title": "t"}, "invalid_mapping"),
])
def test_bad_conversation_cannot_discard_later_valid_conversation(bad, reason):
    result = extract(bad, conversation())
    assert result.messages["message"].tolist() == ["Hello world"]
    assert result.messages["_conversation"].tolist() == [1]
    assert result.issues.to_dict("records") == [{"conversation": "1", "message": "", "reason": reason, "action": "conversation_excluded"}]


@pytest.mark.parametrize("bad, reason", [
    (None, "invalid_message"), ({}, "invalid_message"),
    ({"message": None, "parent": "parent-id"}, "invalid_message"),
    ({"message": []}, "invalid_message"),
    (node(metadata="private metadata"), "invalid_metadata"),
    # Only booleans have been observed; other encodings are not guessed.
    (node(metadata={"is_visually_hidden_from_conversation": "true"}), "invalid_hidden_flag"),
    (node(metadata={"is_visually_hidden_from_conversation": 1}), "invalid_hidden_flag"),
    (node(metadata={"is_visually_hidden_from_conversation": []}), "invalid_hidden_flag"),
    (node(author=None), "invalid_role"),
    (node(author={"role": None}), "invalid_role"),
    (node(author={"role": ""}), "invalid_role"), (node(author={"role": 7}), "invalid_role"),
    (node(metadata={"model_slug": []}), "invalid_model"),
    (node(content=None), "invalid_content"),
    (node(content={}), "invalid_content"), (node(content={"parts": None, "text": "not a fallback"}), "invalid_content"),
    (node(content={"parts": "not a list"}), "invalid_content"),
    (node(content={"text": 7}), "invalid_content"),
    # Unknown content types without parts are reported, not donated empty.
    (node(content={"content_type": "code", "text": "print(1)"}), "invalid_content"),
    (node(content={"parts": ["valid prefix", float("nan")]}), "invalid_content"),
])
def test_invalid_message_is_reported_without_partial_text_or_losing_sibling(bad, reason):
    result = extract(conversation({"bad": bad, "valid": node()}))
    assert result.messages["message"].tolist() == ["Hello world"]
    assert result.issues.to_dict("records") == [{"conversation": "1", "message": "1", "reason": reason, "action": "message_excluded"}]


@pytest.mark.parametrize("timestamp", [CREATED, int(CREATED)])
def test_numeric_unix_seconds_are_the_supported_timestamp(timestamp, local_zone):
    local_zone("UTC")
    result = extract(conversation({"node": node(create_time=timestamp)}))
    assert result.messages["time"].tolist() == ["2026-01-15 12:00:00"]
    assert result.issues.empty


@pytest.mark.parametrize("timestamp", [
    None, True, {}, "", str(CREATED), CREATED * 1000, "2026-01-15T12:00:00Z",
    float("nan"), float("inf"), float("-inf"), 1e30, -(10**400),
])
def test_invalid_timestamps_are_issues_not_silently_filtered(timestamp):
    result = extract(conversation({"bad": node(create_time=timestamp), "valid": node()}))
    assert result.messages["message"].tolist() == ["Hello world"]
    assert result.issues["reason"].tolist() == ["invalid_timestamp"]


@pytest.mark.parametrize("now", [NOW, NOW.replace(tzinfo=None), NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))])
def test_utc_calendar_year_boundaries_are_inclusive_and_future_is_reported(now):
    cutoff = NOW.replace(year=NOW.year - 1)
    values = [
        ("old", cutoff - timedelta(microseconds=1)),
        ("cutoff", cutoff), ("now", NOW), ("future", NOW + timedelta(microseconds=1)),
    ]
    result = extract(conversation({name: node(create_time=value.timestamp(), content={"parts": [name]}) for name, value in values}), now=now)
    assert result.messages["message"].tolist() == ["now", "cutoff"]
    assert result.issues.to_dict("records") == [{"conversation": "1", "message": "4", "reason": "future_timestamp", "action": "message_excluded"}]


def test_leap_day_cutoff_falls_back_to_february_28_with_same_utc_time():
    now = datetime(2024, 2, 29, 9, tzinfo=timezone.utc)
    result = extract(conversation({
        "old": node(create_time=datetime(2023, 2, 28, 8, 59, 59, tzinfo=timezone.utc).timestamp()),
        "boundary": node(create_time=datetime(2023, 2, 28, 9, tzinfo=timezone.utc).timestamp(), content={"parts": ["boundary"]}),
    }), now=now)
    assert result.messages["message"].tolist() == ["boundary"]
    assert result.issues.empty


def test_depth_limit_rejects_whole_content_not_only_the_deep_leaf():
    leaf = "at boundary"
    for _ in range(63):
        leaf = {"child": leaf}
    valid = node(content={"parts": [leaf]})
    invalid = node(content={"parts": ["must not retain prefix", [leaf]]})
    result = extract(conversation({"valid": valid, "invalid": invalid}))
    assert result.messages["message"].tolist() == ["at boundary"]
    assert result.issues.to_dict("records") == [{"conversation": "1", "message": "2", "reason": "invalid_content", "action": "message_excluded"}]


def test_issue_locations_are_input_positions_and_never_contain_private_data(capsys, caplog):
    private = "private person title id message content"
    source = conversation({
        private: {"message": None},
        "bad timestamp " + private: node(create_time=private, content={"parts": [private]}),
        "bad content " + private: node(content={"parts": [private, float("inf")]}),
        "valid": node(),
    }, title=private)
    result = extract(source, {"title": private, "mapping": private})
    assert result.messages["message"].tolist() == ["Hello world"]
    assert result.issues.to_dict("records") == [
        {"conversation": "1", "message": "2", "reason": "invalid_timestamp", "action": "message_excluded"},
        {"conversation": "1", "message": "3", "reason": "invalid_content", "action": "message_excluded"},
        {"conversation": "2", "message": "", "reason": "invalid_mapping", "action": "conversation_excluded"},
    ]
    assert private not in result.issues.to_json()
    captured = capsys.readouterr()
    assert private not in captured.out + captured.err + caplog.text


def test_extraction_does_not_mutate_the_export():
    source = conversation()
    original = deepcopy(source)
    extract(source)
    assert source == original


@pytest.mark.parametrize("field, reason", [
    ("title", "invalid_title"), ("role", "invalid_role"),
    ("model", "invalid_model"), ("parts", "invalid_content"),
])
def test_invalid_unicode_is_reported_before_it_can_break_donation_json(field, reason):
    bad = conversation()
    message = bad["mapping"]["turn"]["message"]
    invalid = "private\ud800value"
    if field == "title":
        bad["title"] = invalid
    elif field == "role":
        message["author"]["role"] = invalid
    elif field == "model":
        message["metadata"]["model_slug"] = invalid
    else:
        message["content"] = {"parts": [invalid]}
    good = conversation({"turn": node(content={"parts": ["Exact text 😀 citeturn1"]})})
    result = extract(bad, good)
    assert result.issues["reason"].tolist() == [reason]
    donated = json.loads(result.messages[chatgpt.MESSAGE_COLUMNS].to_json(orient="records"))
    assert [row["message"] for row in donated] == ["Exact text 😀 citeturn1"]


def test_out_of_window_messages_do_not_need_content_parsing():
    result = extract(conversation({
        "old": node(create_time=datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp(), content={"unknown_format": "old"}),
        "current": node(),
    }))
    assert result.messages["message"].tolist() == ["Hello world"]
    assert result.issues.empty
