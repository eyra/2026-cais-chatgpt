"""CAIS ChatGPT data-donation flow.

Explicit ChatGPT parsing lives in port.chatgpt. This module handles bounded,
part-by-part archive loading, byte-budgeted message selection and the
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
# Bound actual donation JSON, leaving headroom below Next's 210 MB request limit.
MAX_DONATION_BYTES = 200_000_000
CONVERSATIONS_TABLE_ID = "chatgpt_conversations_1"
DONATION_ENVELOPE_BYTES = len(json.dumps(
    {CONVERSATIONS_TABLE_ID: {"data": [], "metadata": {"deletedRowCount": 0}}},
    ensure_ascii=False,
    separators=(",", ":"),
).encode("utf-8")) - 2  # The extractor counts the row array's brackets itself.
# Time window: messages from the last N calendar months (UTC) before the
# upload; None disables the month cutoff. A test release with another window is a
# branch that changes only this value. Locally: python -m port.script
# export.zip --window-months 24 (or none).
TIME_WINDOW_MONTHS: int | None = None


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

        extraction, problem = extract_export(file_result.value)
        yield donate_logs(tracking_key)
        if extraction is not None:
            break

        retry_result = yield render_data_submission_page([nothing_to_donate(problem)])
        if retry_result.__type__ != "PayloadTrue":
            logger.info("Skipped during retry flow")
            yield donate_logs(tracking_key)
            return

    logger.info("Prompt consent")
    yield donate_logs(tracking_key)
    pending_consent = [render_data_submission_page(prompt_consent(extraction))]
    del extraction, file_result
    # Pop transfers ownership to the wrapper: this suspended generator must not
    # retain either the extraction or the table frames after serialization.
    result = yield pending_consent.pop()
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
) -> tuple[chatgpt.ExtractionResult | None, str | None]:
    """Return the extraction, or None and why the export cannot be used.

    The reason is "unreadable", "unsupported_format", or "unusable" (processing
    failures left no messages). An export without messages in the time window is returned
    normally: the participant still completes the consent step with an empty
    table, which is informative for researchers too. Rejections, format changes
    and skipped messages are logged as warnings, so they reach both the
    tracking donation (researchers) and the host log (AppSignal). Skipped
    messages are logged as one summary line with counts per reason and result.
    """
    logger.info("Time window: %s", window_label())
    try:
        extraction = chatgpt.extract_conversations(
            iter_conversations(path),
            now=now,
            window_months=TIME_WINDOW_MONTHS,
            max_data_bytes=MAX_DONATION_BYTES - DONATION_ENVELOPE_BYTES,
        )
    except InvalidChatGPTExport as error:
        logger.warning("Rejected ChatGPT export: %s", error)
        return None, "unreadable"
    except chatgpt.UnsupportedExportFormat as error:
        logger.warning("Rejected ChatGPT export: unsupported format, %s", error)
        return None, "unsupported_format"

    if not extraction.issues.empty:
        counts = extraction.issues.value_counts(["reason", "action"], sort=False)
        logger.warning(
            "Processing issues: %s",
            ", ".join(f"{reason} {action}={count}" for (reason, action), count in counts.items()),
        )
    logger.info(
        "Extracted %s ChatGPT messages (%s outside the time window) with %s processing issue(s)",
        len(extraction.messages),
        extraction.outside_window,
        len(extraction.issues),
    )
    if extraction.outside_byte_limit:
        logger.info(
            "Excluded %s message(s) outside the %s-byte donation budget",
            extraction.outside_byte_limit, MAX_DONATION_BYTES,
        )
    if extraction.messages.empty:
        if extraction.outside_byte_limit:
            logger.info("No messages to donate: the newest message exceeds the donation byte budget")
        elif not extraction.issues.empty:
            logger.warning("Rejected ChatGPT export: processing failures left no messages in the time window")
            return None, "unusable"
        elif extraction.outside_window:
            logger.info("No messages to donate: all messages are outside the time window")
        else:
            logger.info("No messages to donate: the export contains no messages")
    return extraction, None


def window_label() -> str:
    if TIME_WINDOW_MONTHS is None:
        return "all messages"
    return f"last {TIME_WINDOW_MONTHS} months"


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


def window_phrase() -> dict[str, str]:
    """Per-locale " from the last N months", or "" without a window."""
    months = TIME_WINDOW_MONTHS
    if months is None:
        return {"en": "", "de": "", "nl": ""}
    return {
        "en": f" from the last {months} months",
        "de": f" aus den letzten {months} Monaten",
        "nl": f" uit de afgelopen {months} maanden",
    }


def nothing_to_donate(problem: str | None) -> props.PropsUIPromptConfirm:
    """Why the export cannot be used; ok retries with another file."""
    within = window_phrase()
    if problem == "unusable":
        text = {
            "en": f"Some records could not be processed, and no messages{within['en']} remain. Please select a different ZIP file.",
            "de": f"Einige Datensätze konnten nicht verarbeitet werden, und es verbleiben keine Nachrichten{within['de']}. Bitte wählen Sie eine andere ZIP-Datei.",
            "nl": f"Sommige gegevens konden niet worden verwerkt en er blijven geen berichten{within['nl']} over. Selecteer een ander ZIP-bestand.",
        }
    elif problem == "unsupported_format":
        text = {
            "en": "This ChatGPT export uses a format we do not support yet. Please email support@eyra.co and mention the study you are participating in. Do not attach your ChatGPT export or conversation contents.",
            "de": "Dieser ChatGPT-Export verwendet ein Format, das wir noch nicht unterstützen. Bitte schreiben Sie eine E-Mail an support@eyra.co und nennen Sie die Studie, an der Sie teilnehmen. Fügen Sie weder Ihren ChatGPT-Export noch Gesprächsinhalte bei.",
            "nl": "Deze ChatGPT-export gebruikt een formaat dat we nog niet ondersteunen. Stuur een e-mail naar support@eyra.co en vermeld aan welk onderzoek u deelneemt. Stuur uw ChatGPT-export of gespreksinhoud niet mee.",
        }
    else:
        text = {
            "en": "We could not read this ChatGPT export. Please select the unmodified ZIP file you received from ChatGPT.",
            "de": "Dieser ChatGPT-Export konnte nicht gelesen werden. Bitte wählen Sie die unveränderte ZIP-Datei, die Sie von ChatGPT erhalten haben.",
            "nl": "Deze ChatGPT-export kon niet worden gelezen. Selecteer het ongewijzigde ZIP-bestand dat u van ChatGPT hebt ontvangen.",
        }
    return props.PropsUIPromptConfirm(
        props.Translatable(text),
        props.Translatable({"en": "Try again", "de": "Erneut versuchen", "nl": "Probeer opnieuw"}),
    )


def no_messages_notice(extraction: chatgpt.ExtractionResult) -> props.PropsUIPromptText:
    """Why the table is empty; the participant still completes the step."""
    if extraction.outside_byte_limit:
        return props.PropsUIPromptText(props.Translatable({
            "en": "The newest message exceeds the data-size limit, so no messages could be included. Please click “Yes, donate” to complete this step.",
            "de": "Die neueste Nachricht überschreitet die zulässige Datengröße, daher konnten keine Nachrichten aufgenommen werden. Bitte klicken Sie auf „Ja, spenden“, um diesen Schritt abzuschließen.",
            "nl": "Het nieuwste bericht overschrijdt de limiet voor de gegevensgrootte, waardoor er geen berichten konden worden opgenomen. Klik op ‘Ja, doneer’ om deze stap af te ronden.",
        }))
    within = window_phrase() if extraction.outside_window else {"en": "", "de": "", "nl": ""}
    return props.PropsUIPromptText(props.Translatable({
        "en": f"No messages{within['en']} are available to donate, so the table below is empty. Please click “Yes, donate” to complete this step.",
        "de": f"Es sind keine Nachrichten{within['de']} für die Spende verfügbar, daher ist die Tabelle unten leer. Bitte klicken Sie auf „Ja, spenden“, um diesen Schritt abzuschließen.",
        "nl": f"Er zijn geen berichten{within['nl']} beschikbaar om te doneren, daarom is de tabel hieronder leeg. Klik op ‘Ja, doneer’ om deze stap af te ronden.",
    }))


def prompt_consent(extraction: chatgpt.ExtractionResult) -> list[Any]:
    table = extraction.messages[MESSAGE_COLUMNS].reset_index(drop=True)
    count = len(table)
    local_count = f"{count:,}".replace(",", ".")
    within = window_phrase()
    description = {
        "en": f"We found {count:,} {'message' if count == 1 else 'messages'}{within['en']} for you to review",
        "de": f"Wir haben {local_count} {'Nachricht' if count == 1 else 'Nachrichten'}{within['de']} für Sie zum Überprüfen gefunden",
        "nl": f"We hebben {local_count} {'bericht' if count == 1 else 'berichten'}{within['nl']} gevonden die u kunt bekijken",
    }
    if count:
        for locale, text in {
            "en": ", newest first. You can remove any you prefer not to share before donating.",
            "de": ", die neuesten zuerst. Sie können vor der Spende entfernen, was Sie lieber nicht teilen möchten.",
            "nl": ", de nieuwste eerst. U kunt vóór het doneren verwijderen wat u liever niet deelt.",
        }.items():
            description[locale] += text
    else:
        for locale in description:
            description[locale] += "."
    excluded = extraction.outside_byte_limit
    if excluded:
        local_excluded = f"{excluded:,}".replace(",", ".")
        for locale, text in {
            "en": f"{excluded:,} {'additional ' if count else ''}{'message was' if excluded == 1 else 'messages were'} excluded because of the data-size limit.",
            "de": f"{local_excluded} {'weitere ' if count else ''}{'Nachricht wurde' if excluded == 1 else 'Nachrichten wurden'} wegen der zulässigen Datengröße ausgeschlossen.",
            "nl": f"{local_excluded} {'extra ' if count else ''}{'bericht is' if excluded == 1 else 'berichten zijn'} uitgesloten vanwege de limiet voor de gegevensgrootte.",
        }.items():
            description[locale] += " " + text
        if count:
            for locale, text in {
                "en": "The most recent messages were kept.",
                "de": "Die neuesten Nachrichten wurden beibehalten.",
                "nl": "De nieuwste berichten zijn behouden.",
            }.items():
                description[locale] += " " + text
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
            id=CONVERSATIONS_TABLE_ID,
            number=1,
            title=props.Translatable(titles),
            description=props.Translatable(description),
            data_frame=table,
            data_frame_max_size=None,
            headers=headers,
            column_widths=column_widths,
        )
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
        *([no_messages_notice(extraction)] if extraction.messages.empty else []),
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
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m port.script",
        description="Run the extraction on a ChatGPT export locally. Prints the "
        "tracking log and counts only, never message content.",
    )
    parser.add_argument("export", help="path to the ChatGPT export ZIP")
    parser.add_argument(
        "--window-months",
        default=str(TIME_WINDOW_MONTHS),
        help='time window in months, or "none" for all messages (default: %(default)s)',
    )
    arguments = parser.parse_args()
    TIME_WINDOW_MONTHS = (
        None if arguments.window_months.lower() == "none" else int(arguments.window_months)
    )
    extraction, problem = extract_export(arguments.export)
    print(LOG_STREAM.getvalue(), end="")
    if extraction is None:
        print(f"Export rejected: {problem}")
    elif extraction.messages.empty:
        print("Would donate an empty table: no messages in the time window")
    else:
        messages = extraction.messages
        print(
            f"Would donate {len(messages)} messages from "
            f"{messages['_conversation'].nunique()} conversations, "
            f"{messages['time'].iloc[-1]} to {messages['time'].iloc[0]}"
        )
