"""CAIS ChatGPT data-donation flow.

Explicit ChatGPT parsing lives in port.chatgpt. This module handles bounded,
part-by-part archive loading, lossless review-table partitioning and the
Feldspar consent flow. As in the Utrecht reference, the flow log (including
records that could not be processed) is donated as "<session>-tracking" and
forwarded to the host as CommandSystemLog.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import datetime
from pathlib import PurePosixPath
import io
import json
import logging
from typing import Any
import zipfile
import zlib

import pandas as pd

import port.api.props as props
from port import chatgpt
from port.api.commands import CommandSystemDonate, CommandUIRender
from port.chatgpt import MESSAGE_COLUMNS

LOG_STREAM = io.StringIO()

# Reference tracking setup (port-chatgpt-uu script.py): every log record is
# kept in LOG_STREAM and donated by donate_logs().
logging.basicConfig(
    stream=LOG_STREAM,
    level=logging.INFO,
    format="%(asctime)s --- %(name)s --- %(levelname)s --- %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)

logger = logging.getLogger(__name__)

CHATGPT_EXPORT_FILE = "conversations.json"
EXPORT_MANIFEST_FILE = "export_manifest.json"
MAX_EXPORT_JSON_BYTES = 256 * 1024 * 1024
MIN_TABLE_ROW_LIMIT = 10_000
MAX_TABLE_ROW_LIMIT = 50_000
TABLE_ROW_LIMIT = 10_000


class InvalidChatGPTExport(ValueError):
    """Raised when an upload is not a structurally valid ChatGPT export."""


def process(data: dict[str, str]) -> Generator:
    session_id = data.get("sessionId", "")
    tracking_key = f"{session_id}-tracking"
    logger.info("Starting the donation flow")
    yield donate_logs(tracking_key)

    while True:
        logger.info("Prompt for file")
        yield donate_logs(tracking_key)
        file_result = yield render_data_submission_page([prompt_file()])
        if file_result.__type__ != "PayloadFile":
            logger.info("Skipped at file selection ending flow")
            yield donate_logs(tracking_key)
            return

        extraction, no_usable_messages = extract_export(file_result.value)
        yield donate_logs(tracking_key)
        if extraction is not None:
            break

        retry_result = yield render_data_submission_page(
            [retry_confirmation(no_usable_messages)]
        )
        if retry_result.__type__ != "PayloadTrue":
            logger.info("Skipped during retry flow")
            yield donate_logs(tracking_key)
            return

    logger.info("Prompt consent")
    yield donate_logs(tracking_key)
    result = yield render_data_submission_page(prompt_consent(extraction))
    if result.__type__ == "PayloadJSON":
        logger.info("Data donated")
        yield donate(f"{session_id}-chatgpt-conversations", result.value)
    elif result.__type__ == "PayloadFalse":
        logger.info("Data submission declined")
        yield donate(
            f"{session_id}-chatgpt-conversations",
            json.dumps({"status": "data_submission declined"}),
        )
    yield donate_logs(tracking_key)


def extract_export(
    path: Any, now: datetime | None = None
) -> tuple[chatgpt.ExtractionResult | None, bool]:
    """Return the extraction, or None and whether processing failures left no messages.

    Rejections, format changes and skipped messages are logged as warnings, so
    they reach both the tracking donation (researchers) and the host log
    (AppSignal). Skipped messages are logged as one summary line with counts
    per reason and result.
    """
    try:
        extraction = chatgpt.extract_conversations(iter_conversations(path), now=now)
    except InvalidChatGPTExport as error:
        logger.warning("Rejected ChatGPT export: %s", error)
        return None, False
    except chatgpt.UnsupportedExportFormat as error:
        logger.warning("Rejected ChatGPT export: unsupported format, %s", error)
        return None, False

    if not extraction.issues.empty:
        counts = extraction.issues.value_counts(["reason", "action"], sort=False)
        logger.warning(
            "Processing issues: %s",
            ", ".join(f"{reason} {action}={count}" for (reason, action), count in counts.items()),
        )
    logger.info(
        "Extracted %s ChatGPT messages with %s processing issue(s)",
        len(extraction.messages),
        len(extraction.issues),
    )
    if extraction.messages.empty and not extraction.issues.empty:
        logger.warning("Rejected ChatGPT export: processing failures left no messages in the study window")
        return None, True
    return extraction, False


def iter_conversations(path: Any) -> Iterator[Any]:
    """Yield the export's conversations, one conversations file at a time.

    Large exports may split conversations.json into several files. Each file
    is parsed only when the previous one is exhausted and released, so peak
    memory is one file rather than the whole export. The ZIP itself is read
    lazily from the upload. A file that cannot be read rejects the export.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            for member in conversation_members(archive):
                conversations = read_json_member(archive, member)
                if not isinstance(conversations, list):
                    raise InvalidChatGPTExport("conversations JSON must contain a list")
                yield from conversations
                del conversations
    except InvalidChatGPTExport:
        raise
    except (
        OSError, UnicodeDecodeError, ValueError, zipfile.BadZipFile,
        RuntimeError, NotImplementedError, EOFError, RecursionError, zlib.error,
    ) as error:
        raise InvalidChatGPTExport("archive or conversations JSON cannot be read") from error


def conversation_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Resolve the conversations files, in order, from the export manifest.

    Current exports list every logical file and its (possibly split) physical
    files in export_manifest.json, relative to the manifest. Exports without
    such a manifest must contain exactly one conversations.json.
    """
    files = {info.filename: info for info in archive.infolist() if not info.is_dir()}
    manifests = []
    for name, info in files.items():
        if PurePosixPath(name).name == EXPORT_MANIFEST_FILE:
            manifest = read_json_member(archive, info)
            # Exports also contain other manifests (e.g. sites/) without files.
            if isinstance(manifest, dict) and "logical_files" in manifest:
                manifests.append((PurePosixPath(name).parent, manifest))
    if len(manifests) > 1:
        raise InvalidChatGPTExport(f"expected at most one {EXPORT_MANIFEST_FILE} listing files")

    if not manifests:
        candidates = [
            info for name, info in files.items()
            if PurePosixPath(name).name == CHATGPT_EXPORT_FILE
        ]
        if len(candidates) != 1:
            raise InvalidChatGPTExport(
                f"expected one {CHATGPT_EXPORT_FILE}, found {len(candidates)}"
            )
        return candidates

    base, manifest = manifests[0]
    logical_files = manifest["logical_files"]
    entry = logical_files.get(CHATGPT_EXPORT_FILE) if isinstance(logical_files, dict) else None
    parts = entry.get("files") if isinstance(entry, dict) else None
    if (
        not isinstance(parts, list) or not parts
        or not all(isinstance(part, str) for part in parts)
        or len(set(parts)) != len(parts)
    ):
        raise InvalidChatGPTExport("export manifest does not list the conversations files")
    members = []
    for part in parts:
        name = str(base / part)
        if name not in files:
            raise InvalidChatGPTExport("a conversations file listed in the manifest is missing")
        members.append(files[name])
    return members


def read_json_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> Any:
    """Parse one archive member, refusing files over the per-file size limit."""
    if member.file_size > MAX_EXPORT_JSON_BYTES:
        raise InvalidChatGPTExport("export JSON file exceeds the size limit")
    with archive.open(member) as source:
        payload = source.read(MAX_EXPORT_JSON_BYTES + 1)
    if len(payload) > MAX_EXPORT_JSON_BYTES:
        raise InvalidChatGPTExport("export JSON file exceeds the size limit")
    return json.loads(payload, object_pairs_hook=unique_json_object)


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous duplicate fields instead of silently selecting the last."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidChatGPTExport("export JSON contains duplicate object keys")
        result[key] = value
    return result


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


def retry_confirmation(no_usable_messages: bool = False) -> props.PropsUIPromptConfirm:
    return props.PropsUIPromptConfirm(
        props.Translatable(
            {
                "en": (
                    "Some records could not be processed, and no messages remain within the last 12 months. Please select a different ZIP file."
                    if no_usable_messages else
                    "We could not read this ChatGPT export. Please select the unmodified ZIP file you received from ChatGPT."
                ),
                "de": (
                    "Einige Datensätze konnten nicht verarbeitet werden, und es verbleiben keine Nachrichten aus den letzten 12 Monaten. Bitte wählen Sie eine andere ZIP-Datei."
                    if no_usable_messages else
                    "Dieser ChatGPT-Export konnte nicht gelesen werden. Bitte wählen Sie die unveränderte ZIP-Datei, die Sie von ChatGPT erhalten haben."
                ),
                "nl": (
                    "Sommige gegevens konden niet worden verwerkt en er blijven geen berichten uit de afgelopen 12 maanden over. Selecteer een ander ZIP-bestand."
                    if no_usable_messages else
                    "Deze ChatGPT-export kon niet worden gelezen. Selecteer het ongewijzigde ZIP-bestand dat u van ChatGPT hebt ontvangen."
                ),
            }
        ),
        props.Translatable({"en": "Try again", "de": "Erneut versuchen", "nl": "Probeer opnieuw"}),
    )


def prompt_consent(extraction: chatgpt.ExtractionResult) -> list[Any]:
    tables = partition_dataframe(extraction.messages)
    table_count = len(tables)
    titles = {
        "en": "Your conversations with ChatGPT",
        "de": "Ihre Unterhaltungen mit ChatGPT",
        "nl": "Uw gesprekken met ChatGPT",
    }
    # Display-only labels; donated keys remain the reference column names.
    headers = {
        "conversation title": props.Translatable(
            {"en": "Conversation title", "de": "Titel der Unterhaltung", "nl": "Gesprekstitel"}
        ),
        "role": props.Translatable({"en": "Role", "de": "Rolle", "nl": "Rol"}),
        "message": props.Translatable({"en": "Message", "de": "Nachricht", "nl": "Bericht"}),
        "model": props.Translatable({"en": "Model", "de": "Modell", "nl": "Model"}),
        "time": props.Translatable({"en": "Time", "de": "Zeit", "nl": "Tijd"}),
    }
    column_widths = {"conversation title": 3, "role": 1, "message": 6, "model": 1.2, "time": 1.8}
    consent_tables = [
        props.PropsUIPromptConsentFormTable(
            id=f"chatgpt_conversations_{number}",
            number=number,
            title=props.Translatable(
                {
                    locale: title if table_count == 1 else f"{title} ({number}/{table_count})"
                    for locale, title in titles.items()
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
            headers=headers,
            column_widths=column_widths,
        )
        for number, table in enumerate(tables, start=1)
    ]
    return [
        props.PropsUIPromptText(
            props.Translatable(
                {
                    "en": "Determine whether you would like to donate the data below. Carefully check the data and adjust when required. With your donation you contribute to the previously described research. Thank you in advance.",
                    "de": "Entscheiden Sie, ob Sie die untenstehenden Daten spenden möchten. Prüfen Sie die Daten sorgfältig und passen Sie sie bei Bedarf an. Mit Ihrer Spende tragen Sie zu der zuvor beschriebenen Forschung bei. Vielen Dank im Voraus.",
                    "nl": "Bepaal of u de onderstaande gegevens wilt doneren. Bekijk de gegevens zorgvuldig en pas zo nodig aan. Met uw donatie draagt u bij aan het eerder beschreven onderzoek. Alvast hartelijk dank.",
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


def donate_logs(key: str) -> CommandSystemDonate:
    """Donate the flow log collected so far (reference: port-chatgpt-uu)."""
    log_string = LOG_STREAM.getvalue()
    log_data = log_string.split("\n") if log_string else ["no logs"]
    return donate(key, json.dumps(log_data))


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m port.script path/to/chatgpt-export.zip")
        raise SystemExit(1)

    print(extract_export(sys.argv[1]))
