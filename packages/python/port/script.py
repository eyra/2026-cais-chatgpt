"""CAIS ChatGPT data-donation flow.

The Utrecht extraction lives in port.chatgpt and port.helpers. This module
adds the CAIS upload validation, twelve-month filter, sorting and lossless
table partitioning using the current Feldspar file and consent APIs.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import PurePosixPath
import json
import logging
from typing import Any
import zipfile

import pandas as pd

import port.api.props as props
from port import chatgpt
from port.api.commands import CommandSystemDonate, CommandUIRender, FlushLogs
from port.chatgpt import MESSAGE_COLUMNS

logger = logging.getLogger(__name__)

CHATGPT_EXPORT_FILE = "conversations.json"
MIN_TABLE_ROW_LIMIT = 10_000
MAX_TABLE_ROW_LIMIT = 50_000
TABLE_ROW_LIMIT = 10_000


class InvalidChatGPTExport(ValueError):
    """Raised when an upload is not a structurally valid ChatGPT export."""


def process(data: dict[str, str]) -> Generator:
    session_id = data.get("sessionId", "")
    tables: list[pd.DataFrame] | None = None

    while True:
        file_result = yield render_data_submission_page([prompt_file()])
        if file_result.__type__ != "PayloadFile":
            return

        tables, retry = yield from extract_tables(file_result.value)
        if retry:
            continue
        break

    if tables is None:
        return

    result = yield render_data_submission_page(prompt_consent(tables))
    if result.__type__ == "PayloadJSON":
        yield donate(f"{session_id}-chatgpt-conversations", result.value)
    elif result.__type__ == "PayloadFalse":
        yield donate(
            f"{session_id}-chatgpt-conversations",
            json.dumps({"status": "data_submission declined"}),
        )


def extract_tables(
    path: Any, now: datetime | None = None
) -> Generator[object, None, tuple[list[pd.DataFrame] | None, bool]]:
    try:
        conversations = load_conversations(path)
        frame = conversations_to_dataframe(conversations, now=now)
        tables = partition_dataframe(frame)
        logger.info(
            "Extracted %s ChatGPT messages into %s review table(s)",
            len(frame),
            len(tables),
        )
        yield FlushLogs
        return tables, False
    except InvalidChatGPTExport as error:
        logger.info("Rejected ChatGPT export: %s", error)
        retry_result = yield render_data_submission_page([retry_confirmation()])
        return None, retry_result.__type__ == "PayloadTrue"


def load_conversations(path: Any) -> list[dict[str, Any]]:
    """Read and structurally validate the archive's conversations payload."""
    try:
        with zipfile.ZipFile(path) as archive:
            candidates = [
                info
                for info in archive.infolist()
                if not info.is_dir()
                and PurePosixPath(info.filename).name == CHATGPT_EXPORT_FILE
            ]
            if len(candidates) != 1:
                raise InvalidChatGPTExport(
                    f"expected one {CHATGPT_EXPORT_FILE}, found {len(candidates)}"
                )

            with archive.open(candidates[0]) as source:
                conversations = json.load(source)
    except InvalidChatGPTExport:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        raise InvalidChatGPTExport("archive or conversations JSON cannot be read") from error

    if not isinstance(conversations, list):
        raise InvalidChatGPTExport("conversations JSON must contain a list")
    if not all(
        isinstance(conversation, dict)
        and "title" in conversation
        and isinstance(conversation.get("mapping"), dict)
        for conversation in conversations
    ):
        raise InvalidChatGPTExport("every conversation must have a title and mapping")

    return conversations


def conversations_to_dataframe(
    conversations: list[dict[str, Any]], now: datetime | None = None
) -> pd.DataFrame:
    """Apply CAIS time selection/order without changing the Utrecht field values.

    The cutoff uses absolute UTC time, not the reference's local display string.
    Undated/unparseable messages cannot be assigned to the study window and are
    excluded here, not by the reference extractor.
    """
    current_time = normalise_utc(now or datetime.now(timezone.utc))
    cutoff = twelve_month_cutoff(current_time).timestamp()
    frame = chatgpt.conversations_to_df(conversations)
    timestamps = pd.to_numeric(frame["_create_time"], errors="coerce")
    in_window = timestamps.ge(cutoff) & timestamps.lt(float("inf"))
    ordered_index = timestamps[in_window].sort_values(
        ascending=False, kind="stable"
    ).index
    return frame.loc[ordered_index, [*MESSAGE_COLUMNS, "_conversation"]].reset_index(drop=True)


def partition_dataframe(
    frame: pd.DataFrame, row_limit: int = TABLE_ROW_LIMIT
) -> list[pd.DataFrame]:
    """Split a sorted frame without truncation, respecting contiguous conversations."""
    validate_table_row_limit(row_limit)
    if frame.empty:
        return [pd.DataFrame(columns=MESSAGE_COLUMNS)]

    tables: list[pd.DataFrame] = []
    start = 0
    total_rows = len(frame)
    while start < total_rows:
        end = min(start + row_limit, total_rows)
        if end < total_rows and frame.iloc[end - 1]["_conversation"] == frame.iloc[end]["_conversation"]:
            boundary = end
            while boundary > start and frame.iloc[boundary - 1]["_conversation"] == frame.iloc[end]["_conversation"]:
                boundary -= 1
            if boundary > start:
                end = boundary

        tables.append(frame.iloc[start:end][MESSAGE_COLUMNS].reset_index(drop=True))
        start = end

    return tables


def validate_table_row_limit(row_limit: int) -> None:
    if not MIN_TABLE_ROW_LIMIT <= row_limit <= MAX_TABLE_ROW_LIMIT:
        raise ValueError(
            f"table row limit must be between {MIN_TABLE_ROW_LIMIT} and {MAX_TABLE_ROW_LIMIT}"
        )


def normalise_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def twelve_month_cutoff(value: datetime) -> datetime:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:  # 29 February to a non-leap year
        return value.replace(year=value.year - 1, day=28)




def render_data_submission_page(body: list[Any]) -> CommandUIRender:
    header = props.PropsUIHeader(
        props.Translatable(
            {
                "en": "Your ChatGPT data",
                "de": "Ihre ChatGPT-Daten",
                "nl": "Uw ChatGPT-gegevens",
            }
        )
    )
    return CommandUIRender(props.PropsUIPageDataSubmission("ChatGPT", header, body))


def prompt_file() -> props.PropsUIPromptFileInput:
    return props.PropsUIPromptFileInput(
        props.Translatable(
            {
                "en": "Please select the ZIP file from your ChatGPT data export.",
                "de": "Bitte wählen Sie die ZIP-Datei aus Ihrem ChatGPT-Datenexport.",
                "nl": "Selecteer het ZIP-bestand uit uw ChatGPT-data-export.",
            }
        ),
        "application/zip,.zip",
    )


def retry_confirmation() -> props.PropsUIPromptConfirm:
    return props.PropsUIPromptConfirm(
        props.Translatable(
            {
                "en": "We could not verify this as a ChatGPT data export. Please select a different ZIP file.",
                "de": "Diese Datei konnte nicht als ChatGPT-Datenexport bestätigt werden. Bitte wählen Sie eine andere ZIP-Datei.",
                "nl": "We konden dit bestand niet verifiëren als een ChatGPT-data-export. Selecteer een ander ZIP-bestand.",
            }
        ),
        props.Translatable({"en": "Try again", "de": "Erneut versuchen", "nl": "Probeer opnieuw"}),
    )


def prompt_consent(tables: list[pd.DataFrame]) -> list[Any]:
    table_count = len(tables)
    consent_tables = [
        props.PropsUIPromptConsentFormTable(
            id=f"chatgpt_conversations_{number}",
            number=number,
            title=props.Translatable(
                {
                    "en": f"Your ChatGPT conversations ({number}/{table_count})",
                    "de": f"Ihre ChatGPT-Unterhaltungen ({number}/{table_count})",
                    "nl": f"Uw ChatGPT-gesprekken ({number}/{table_count})",
                }
            ),
            description=props.Translatable(
                {
                    "en": "Messages from the last 12 months, newest first.",
                    "de": "Nachrichten der letzten 12 Monate, neueste zuerst.",
                    "nl": "Berichten van de afgelopen 12 maanden, nieuwste eerst.",
                }
            ),
            data_frame=table,
            data_frame_max_size=TABLE_ROW_LIMIT,
        )
        for number, table in enumerate(tables, start=1)
    ]
    return [
        props.PropsUIPromptText(
            props.Translatable(
                {
                    "en": "Please review the ChatGPT messages below. You can remove any information you prefer not to share.",
                    "de": "Bitte überprüfen Sie die untenstehenden ChatGPT-Nachrichten. Sie können alle Informationen entfernen, die Sie nicht teilen möchten.",
                    "nl": "Bekijk hieronder uw ChatGPT-berichten. U kunt informatie verwijderen die u liever niet deelt.",
                }
            )
        ),
        *consent_tables,
        props.PropsUIDataSubmissionButtons(
            donate_question=props.Translatable(
                {
                    "en": "Would you like to donate the above data?",
                    "de": "Möchten Sie die obenstehenden Daten spenden?",
                    "nl": "Wilt u de bovenstaande gegevens doneren?",
                }
            ),
            donate_button=props.Translatable(
                {"en": "Yes, donate", "de": "Ja, spenden", "nl": "Ja, doneer"}
            ),
        ),
    ]


def donate(key: str, json_string: str) -> CommandSystemDonate:
    return CommandSystemDonate(key, json_string)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m port.script path/to/chatgpt-export.zip")
        raise SystemExit(1)

    generator = extract_tables(sys.argv[1])
    try:
        while True:
            next(generator)
    except StopIteration as result:
        print(result.value)
