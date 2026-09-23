"""Golden outputs cross-checked against Utrecht commit d80e9d834095034c9e80c85986bdab61fb8f152a.

Fixtures deliberately exercise the reference's unusual matching rules. Do not
replace these expectations with cleaner direct-field extraction semantics.
"""

from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from port import chatgpt, script

CREATED = datetime(2026, 1, 15, 12, tzinfo=timezone.utc).timestamp()


def reference_conversations():
    """Also used by the throwaway differential check against upstream code."""
    plain = {
        "message": {
            "author": {"role": "user"},
            "content": {"parts": ["Hello", " world"]},
            "metadata": {"model_slug": "gpt-4"},
            "create_time": CREATED,
        }
    }
    nested = deepcopy(plain)
    nested["message"]["content"]["parts"] = [
        "A", {"content_type": "image", "asset_pointer": "asset://one", "width": 3},
        [True, None, "Z"], {}, [],
    ]
    # A shallower substring match beats the exact role and timestamp below it.
    precedence = deepcopy(plain)
    precedence["speaker_role"] = "tool"
    precedence["create_time"] = CREATED + 1
    precedence["first"] = {"model_slug": "first-model"}
    precedence["second"] = {"model_slug": "second-model"}
    precedence["parts_extra"] = ["suffix"]
    hidden = deepcopy(plain)
    hidden["message"]["metadata"]["is_visually_hidden_from_conversation"] = True
    hidden_string = deepcopy(hidden)
    hidden_string["message"]["metadata"]["is_visually_hidden_from_conversation"] = "True"
    lowercase = deepcopy(hidden)
    lowercase["message"]["metadata"]["is_visually_hidden_from_conversation"] = "true"
    override_hidden = deepcopy(hidden)
    override_hidden["is_visually_hidden_from_conversation"] = False
    undated = deepcopy(plain)
    undated["message"]["create_time"] = None
    invalid_date = deepcopy(plain)
    invalid_date["message"]["create_time"] = "unknown"
    empty_role = deepcopy(plain)
    empty_role["message"]["author"]["role"] = ""
    null_role = deepcopy(plain)
    null_role["message"]["author"]["role"] = None
    # Both alternate branches are extracted; current_node is not followed.
    return [
        {
            "title": "Same title", "current_node": "plain",
            "mapping": {
                "root": {"message": None}, "plain": plain, "alternative": nested,
                "precedence": precedence, "hidden": hidden, "hidden-string": hidden_string,
                "lowercase": lowercase, "override-hidden": override_hidden,
                "undated": undated, "invalid-date": invalid_date,
                "empty-role": empty_role, "null-role": null_role,
            },
        },
        {"title": "Same title", "mapping": {"other-conversation": deepcopy(plain)}},
    ]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="requires runtime timezone selection")
@pytest.mark.parametrize("zone, clock", [("UTC", "12"), ("Europe/Amsterdam", "13")])
def test_reference_five_column_values(zone, clock):
    previous = os.environ.get("TZ")
    try:
        os.environ["TZ"] = zone
        time.tzset()
        frame = chatgpt.conversations_to_df(reference_conversations())
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()

    local_time = f"2026-01-15 {clock}:00:00"
    assert frame[chatgpt.MESSAGE_COLUMNS].values.tolist() == [
        ["Same title", "user", "Hello world", "gpt-4", local_time],
        ["Same title", "user", "Aimageasset://one3TrueNoneZ", "gpt-4", local_time],
        ["Same title", "tool", "Hello worldsuffix", "first-model", f"2026-01-15 {clock}:00:01"],
        ["Same title", "user", "Hello world", "gpt-4", local_time],
        ["Same title", "user", "Hello world", "gpt-4", local_time],
        ["Same title", "user", "Hello world", "gpt-4", "None"],
        ["Same title", "user", "Hello world", "gpt-4", "unknown"],
        ["Same title", "None", "Hello world", "gpt-4", local_time],
        ["Same title", "user", "Hello world", "gpt-4", local_time],
    ]


def test_cais_selection_preserves_reference_values_and_removes_private_columns():
    source = reference_conversations()
    reference = chatgpt.conversations_to_df(source)
    # One second after the plain messages: only the shallower selected timestamp
    # is in-range. The time filter must not use message.create_time instead.
    now = datetime(2027, 1, 15, 12, 0, 1, tzinfo=timezone.utc)
    tables = script.partition_dataframe(script.conversations_to_dataframe(source, now))
    donated = pd.concat(tables, ignore_index=True)

    pd.testing.assert_frame_equal(
        donated, reference.iloc[[2]][chatgpt.MESSAGE_COLUMNS].reset_index(drop=True)
    )


def test_cais_sort_is_stable_and_preserves_both_conversations_with_the_same_title():
    source = reference_conversations()
    reference = chatgpt.conversations_to_df(source)
    selected = script.conversations_to_dataframe(
        source, datetime(2026, 9, 21, tzinfo=timezone.utc)
    )
    # Missing/unparseable times excluded by CAIS; otherwise only order changes.
    expected = reference.iloc[[2, 0, 1, 3, 4, 7, 8]].drop(columns="_create_time").reset_index(drop=True)
    pd.testing.assert_frame_equal(selected, expected)
    assert selected["_conversation"].tolist() == [0, 0, 0, 0, 0, 0, 1]
