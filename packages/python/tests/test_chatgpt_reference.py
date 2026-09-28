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


def extract(*conversations, now=NOW, window_months=None):
    return chatgpt.extract_conversations(list(conversations), now=now, window_months=window_months)


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
def test_conversation_format_change_rejects_export_at_first_occurrence(bad, reason):
    consumed = []

    def export():
        for item in (conversation(), bad, conversation()):
            consumed.append(item)
            yield item

    with pytest.raises(chatgpt.UnsupportedExportFormat) as raised:
        chatgpt.extract_conversations(export(), now=NOW)
    assert (raised.value.reason, raised.value.conversation, raised.value.message) == (reason, 2, None)
    # Fail soon: nothing after the format change is parsed.
    assert len(consumed) == 2


@pytest.mark.parametrize("bad, reason", [
    (None, "invalid_message"), ({}, "invalid_message"),
    ({"message": None, "parent": "parent-id"}, "invalid_message"),
    ({"message": []}, "invalid_message"),
    (node(metadata="private metadata"), "invalid_metadata"),
    # Only booleans have been observed; other encodings are not guessed.
    (node(metadata={"is_visually_hidden_from_conversation": "true"}), "invalid_hidden_flag"),
    (node(metadata={"is_visually_hidden_from_conversation": 1}), "invalid_hidden_flag"),
    (node(author=None), "invalid_role"),
    (node(author={"role": None}), "invalid_role"),
    (node(author={"role": 7}), "invalid_role"),
    (node(metadata={"model_slug": []}), "invalid_model"),
    (node(content=None), "invalid_content"),
    (node(content={"parts": "not a list"}), "invalid_content"),
    (node(content={"parts": ["valid prefix", float("nan")]}), "invalid_content"),
    (node(create_time="2026-01-15T12:00:00Z"), "invalid_timestamp"),
])
def test_message_format_change_rejects_export_with_position_only(bad, reason):
    with pytest.raises(chatgpt.UnsupportedExportFormat) as raised:
        extract(conversation({"valid": node(), "bad": bad}))
    assert (raised.value.reason, raised.value.conversation, raised.value.message) == (reason, 1, 2)
    assert "private" not in str(raised.value)


@pytest.mark.parametrize("content, reason", [
    ({"content_type": "canvas", "text": "private"}, "unknown_content_type:canvas"),
    ({"text": "private"}, "unknown_content_type:other"),
    ({"content_type": "private free text!", "text": "x"}, "unknown_content_type:other"),
    ({"parts": ["private\ud800value"]}, "invalid_text"),
])
def test_unusable_message_is_skipped_and_reported_without_losing_siblings(content, reason):
    result = extract(conversation({"bad": node(content=content), "valid": node()}))
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
def test_unobserved_timestamp_encodings_are_format_changes(timestamp):
    with pytest.raises(chatgpt.UnsupportedExportFormat) as raised:
        extract(conversation({"bad": node(create_time=timestamp)}))
    assert raised.value.reason == "invalid_timestamp"


@pytest.mark.parametrize("now", [NOW, NOW.replace(tzinfo=None), NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))])
def test_utc_calendar_year_boundaries_are_inclusive_and_future_is_reported(now):
    cutoff = NOW.replace(year=NOW.year - 1)
    values = [
        ("old", cutoff - timedelta(microseconds=1)),
        ("cutoff", cutoff), ("now", NOW), ("future", NOW + timedelta(microseconds=1)),
    ]
    result = extract(conversation({name: node(create_time=value.timestamp(), content={"parts": [name]}) for name, value in values}), now=now, window_months=12)
    assert result.messages["message"].tolist() == ["now", "cutoff"]
    assert result.issues.to_dict("records") == [{"conversation": "1", "message": "4", "reason": "future_timestamp", "action": "message_excluded"}]


def test_leap_day_cutoff_falls_back_to_february_28_with_same_utc_time():
    now = datetime(2024, 2, 29, 9, tzinfo=timezone.utc)
    result = extract(conversation({
        "old": node(create_time=datetime(2023, 2, 28, 8, 59, 59, tzinfo=timezone.utc).timestamp()),
        "boundary": node(create_time=datetime(2023, 2, 28, 9, tzinfo=timezone.utc).timestamp(), content={"parts": ["boundary"]}),
    }), now=now, window_months=12)
    assert result.messages["message"].tolist() == ["boundary"]
    assert result.issues.empty


def test_depth_limit_is_a_format_change_not_partial_text():
    leaf = "at boundary"
    for _ in range(63):
        leaf = {"child": leaf}
    assert extract(conversation({"valid": node(content={"parts": [leaf]})})).messages["message"].tolist() == ["at boundary"]
    with pytest.raises(chatgpt.UnsupportedExportFormat):
        extract(conversation({"invalid": node(content={"parts": ["prefix", [leaf]]})}))


def test_issue_locations_are_input_positions_and_never_contain_private_data(capsys, caplog):
    private = "private person title id message content"
    source = conversation({
        private: {"message": None},
        "future " + private: node(create_time=NOW.timestamp() + 60, content={"parts": [private]}),
        "canvas " + private: node(content={"content_type": "canvas", "text": private}),
        "valid": node(),
    }, title=private)
    result = extract(source, conversation(title="private\ud800title"))
    assert result.messages["message"].tolist() == ["Hello world"]
    assert result.issues.to_dict("records") == [
        {"conversation": "1", "message": "2", "reason": "future_timestamp", "action": "message_excluded"},
        {"conversation": "1", "message": "3", "reason": "unknown_content_type:canvas", "action": "message_excluded"},
        {"conversation": "2", "message": "", "reason": "invalid_text", "action": "conversation_excluded"},
    ]
    assert "private" not in result.issues.to_json()
    captured = capsys.readouterr()
    assert "private" not in captured.out + captured.err + caplog.text


def test_extraction_does_not_mutate_the_export():
    source = conversation()
    original = deepcopy(source)
    extract(source)
    assert source == original


@pytest.mark.parametrize("field", ["title", "role", "model", "parts"])
def test_invalid_unicode_is_reported_before_it_can_break_donation_json(field):
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
    assert result.issues["reason"].tolist() == ["invalid_text"]
    donated = json.loads(result.messages[chatgpt.MESSAGE_COLUMNS].to_json(orient="records"))
    assert [row["message"] for row in donated] == ["Exact text 😀 citeturn1"]


def test_out_of_window_messages_do_not_need_content_parsing():
    result = extract(conversation({
        "old": node(create_time=datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp(), content={"unknown_format": "old"}),
        "current": node(),
    }), window_months=12)
    assert result.messages["message"].tolist() == ["Hello world"]
    assert result.issues.empty
    assert result.outside_window == 1


@pytest.mark.parametrize("months, first_kept", [
    # 31 March minus one month clamps to the end of February.
    (1, datetime(2026, 2, 28, 12, tzinfo=timezone.utc)),
    (24, datetime(2024, 3, 31, 12, tzinfo=timezone.utc)),
])
def test_window_counts_back_calendar_months(months, first_kept):
    result = extract(conversation({
        "before": node(create_time=(first_kept - timedelta(seconds=1)).timestamp(), content={"parts": ["before"]}),
        "first": node(create_time=first_kept.timestamp(), content={"parts": ["first"]}),
    }), now=datetime(2026, 3, 31, 12, tzinfo=timezone.utc), window_months=months)
    assert result.messages["message"].tolist() == ["first"]
    assert result.outside_window == 1


def test_without_window_all_messages_are_kept():
    result = extract(conversation({
        "old": node(create_time=datetime(2010, 1, 1, tzinfo=timezone.utc).timestamp()),
        "new": node(),
    }))
    assert len(result.messages) == 2
    assert result.outside_window == 0


@pytest.mark.parametrize("months", [0, -1, True])
def test_window_must_be_a_positive_number_of_months(months):
    with pytest.raises(ValueError):
        extract(conversation(), window_months=months)


def donated_rows(result):
    return result.messages[chatgpt.MESSAGE_COLUMNS].to_dict("records")


def donated_bytes(rows):
    return len(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def test_byte_budget_counts_utf8_escaping_and_array_separators_exactly():
    source = conversation({
        "newest": node(
            create_time=CREATED + 2,
            author={"role": '工具"user'},
            metadata={"model_slug": "模型\\test"},
            content={"parts": ['\U0001d11e café 漢字\n"quoted"\\path']},
        ),
        "next": node(create_time=CREATED + 1, content={"parts": ["第二 \U0001d11e\tmessage"]}),
        "oldest": node(content={"parts": ["older"]}),
    }, title='会話 \U0001d11e\n"title"')
    expected = donated_rows(extract(source))[:2]
    budget = donated_bytes(expected)

    exact = chatgpt.extract_conversations([source], now=NOW, max_data_bytes=budget)
    below = chatgpt.extract_conversations([source], now=NOW, max_data_bytes=budget - 1)

    assert donated_rows(exact) == expected
    assert exact.outside_byte_limit == 1
    assert donated_bytes(donated_rows(exact)) == budget
    assert donated_rows(below) == expected[:1]
    assert below.outside_byte_limit == 2
    assert donated_bytes(donated_rows(below)) <= budget - 1


def test_byte_budget_sorts_single_pass_shards_and_keeps_encounter_order_for_ties():
    shards = [
        [conversation({
            "old": node(content={"parts": ["oldest"]}),
            "first-tie": node(create_time=CREATED + 1, content={"parts": ["tie-aa"]}),
        })],
        [conversation({
            "second-tie": node(create_time=CREATED + 1, content={"parts": ["tie-bb"]}),
            "new": node(create_time=CREATED + 2, content={"parts": ["newest"]}),
        })],
        [conversation({
            "third-tie": node(create_time=CREATED + 1, content={"parts": ["tie-cc"]}),
        })],
    ]
    all_rows = donated_rows(extract(*(item for shard in shards for item in shard)))
    rows_by_message = {row["message"]: row for row in all_rows}
    expected = [rows_by_message[text] for text in ["newest", "tie-aa", "tie-bb"]]
    budget = donated_bytes(expected)
    single_pass = (item for shard in shards for item in shard)

    result = chatgpt.extract_conversations(single_pass, now=NOW, max_data_bytes=budget)

    assert donated_rows(result) == expected
    assert result.outside_byte_limit == 2
    assert donated_bytes(donated_rows(result)) == budget


def test_oversized_newest_message_blocks_older_messages_that_would_fit():
    older = conversation({"old": node(content={"parts": ["small"]})})
    budget = donated_bytes(donated_rows(extract(older)))
    newest = conversation({
        "new": node(create_time=CREATED + 1, content={"parts": ["large" * 100]}),
    })

    result = chatgpt.extract_conversations(
        iter([newest, older]), now=NOW, max_data_bytes=budget,
    )

    assert donated_rows(result) == []
    assert result.outside_byte_limit == 2
    assert donated_bytes(donated_rows(result)) == 2


def test_late_newer_message_survives_eviction_without_reopening_older_cutoff():
    initial = conversation({
        "old-a": node(create_time=CREATED + 1, content={"parts": ["old-a"]}),
        "old-b": node(content={"parts": ["old-b"]}),
    })
    budget = donated_bytes(donated_rows(extract(initial)))
    oversized = conversation({
        "blocker": node(create_time=CREATED + 2, content={"parts": ["large" * 100]}),
    })
    late_newest = conversation({
        "new": node(create_time=CREATED + 3, content={"parts": ["newer"]}),
    })
    late_oldest = conversation({
        "old": node(create_time=CREATED - 1, content={"parts": ["older"]}),
    })
    expected = donated_rows(extract(late_newest))
    # The late oldest row fits the remaining space, but lies beyond the cutoff.
    assert donated_bytes(expected + donated_rows(extract(late_oldest))) == budget

    result = chatgpt.extract_conversations(
        iter([initial, oversized, late_newest, late_oldest]),
        now=NOW,
        max_data_bytes=budget,
    )

    assert donated_rows(result) == expected
    assert result.outside_byte_limit == 4
    assert donated_bytes(donated_rows(result)) < budget
