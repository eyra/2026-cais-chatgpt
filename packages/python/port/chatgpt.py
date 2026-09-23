"""Utrecht ChatGPT extraction, separated from CAIS filtering and presentation.

Adapted from trbKnl/port-chatgpt-uu (AGPL-3.0),
src/framework/processing/py/port/chatgpt.py at
https://github.com/trbKnl/port-chatgpt-uu/tree/d80e9d834095034c9e80c85986bdab61fb8f152a

The extraction loop and five donated fields follow the reference. Differences:
- Input is already parsed and validated by the current Feldspar upload flow.
- Two private columns retain conversation identity and the selected raw timestamp
  for CAIS partitioning/filtering. They are never donated.
- Empty exports retain column names; extraction errors propagate, not an empty
  success result as in the reference's broad exception handler.
"""

from typing import Any

import pandas as pd

import port.helpers as helpers

MESSAGE_COLUMNS = ["conversation title", "role", "message", "model", "time"]


def conversations_to_df(conversations: list[dict[str, Any]]) -> pd.DataFrame:
    datapoints = []

    for conversation_index, conversation in enumerate(conversations):
        title = conversation["title"]
        for _, turn in conversation["mapping"].items():
            denested_d = helpers.dict_denester(turn)
            is_hidden = helpers.find_item(denested_d, "is_visually_hidden_from_conversation")
            if is_hidden != "True":
                role = helpers.find_item(denested_d, "role")
                message = "".join(helpers.find_items(denested_d, "part"))
                model = helpers.find_item(denested_d, "-model_slug")
                create_time = helpers.find_item(denested_d, "create_time")
                time = helpers.convert_unix_timestamp(create_time)

                datapoint = {
                    "conversation title": title,
                    "role": role,
                    "message": message,
                    "model": model,
                    "time": time,
                }
                if role != "":
                    datapoint["_conversation"] = conversation_index
                    datapoint["_create_time"] = create_time
                    datapoints.append(datapoint)

    return pd.DataFrame(
        datapoints, columns=[*MESSAGE_COLUMNS, "_conversation", "_create_time"]
    )
