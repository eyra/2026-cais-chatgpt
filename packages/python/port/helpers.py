"""Extraction helpers from trbKnl/port-chatgpt-uu (AGPL-3.0).

Source: src/framework/processing/py/port/helpers.py at
https://github.com/trbKnl/port-chatgpt-uu/tree/d80e9d834095034c9e80c85986bdab61fb8f152a

The four extraction functions retain the reference implementation's behaviour,
including substring matching, insertion-order ties and local-time formatting.
The unused split_dataframe helper is omitted: CAIS partitions by conversation.
"""

import math
import re
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def convert_unix_timestamp(timestamp: str) -> str:
    out = timestamp
    try:
        out = datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        print(e)

    return out


def dict_denester(
    inp: dict[Any, Any] | list[Any],
    new: dict[Any, Any] | None = None,
    name: str = "",
    run_first: bool = True,
) -> dict[Any, Any]:
    """Denest a dict or list, returning a new denested dict."""
    if run_first:
        new = {}

    if isinstance(inp, dict):
        for k, v in inp.items():
            if isinstance(v, (dict, list)):
                dict_denester(v, new, f"{name}-{str(k)}", run_first=False)
            else:
                newname = f"{name}-{k}"
                new.update({newname[1:]: v})  # type: ignore

    elif isinstance(inp, list):
        for i, item in enumerate(inp):
            dict_denester(item, new, f"{name}-{i}", run_first=False)

    else:
        new.update({name[1:]: inp})  # type: ignore

    return new  # type: ignore


def find_item(d: dict[Any, Any], key_to_match: str) -> str:
    """Select the least-nested matching key; keep the first insertion-order tie."""
    out = ""
    pattern = r"{}".format(f"^.*{key_to_match}.*$")
    depth = math.inf

    try:
        for k, v in d.items():
            if re.match(pattern, k):
                depth_current_match = k.count("-")
                if depth_current_match < depth:
                    depth = depth_current_match
                    out = str(v)
    except Exception as e:
        logger.error("bork bork: %s", e)

    return out


def find_items(d: dict[Any, Any], key_to_match: str) -> list:
    """Select all substring-matching values in traversal order, as strings."""
    out = []
    pattern = r"{}".format(f"^.*{key_to_match}.*$")

    try:
        for k, v in d.items():
            if re.match(pattern, k):
                out.append(str(v))
    except Exception as e:
        logger.error("bork bork: %s", e)

    return out
