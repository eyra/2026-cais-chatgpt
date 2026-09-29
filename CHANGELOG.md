# Changelog

## Types of Changes and How to Note Them

* Added - For any new features that have been added since the last version was released
* Changed - To note any changes to the software's existing functionality
* Deprecated - To note any features that were once stable but are no longer and have thus been removed
* Fixed - Any bugs or errors that have been fixed should be so noted
* Removed - This notes any features that have been deleted and removed from the software
* Security - This acts as an invitation to users who want to upgrade and avoid any software vulnerabilities

## Unreleased — CAIS ChatGPT

* Changed donation waiting text to add reassurance after 30 seconds: sending may take 10 minutes or longer, without attributing the wait to the participant's connection. Reuses the existing message area in all seven framework languages, without upload-progress, transport, or host changes.
* Changed the consent description to show the original review-message count in English, German and Dutch, with localized number formatting and plain-language review guidance. Show size-limit exclusions separately without treating them as the complete export total; explain when the newest message exceeds the budget and leaves an empty table. Participant deletions do not change the original review count.
* Fixed unsupported-format guidance: participants are now asked to email support@eyra.co with their study name, without attaching their export or conversation contents. Added English, German and Dutch wording; invalid/unreadable exports retain the original-ZIP guidance.
* Changed CAIS consent to one paginated table containing the newest whole-message prefix within 200,000,000 bytes of final UTF-8 donation JSON, including all donated fields and envelope metadata. Selection is bounded during extraction; older messages beyond the cutoff are excluded. Removed CAIS's row limit and the framework's JavaScript row cutoff; the Python table API accepts `None` for no row limit while retaining its numeric defaults.
* Fixed retention of extracted Python dataframes while participants review consent: ownership now ends after consent serialization, and the worker explicitly releases transferred Python command proxies. Review adjustments and donated data are unchanged.
* Removed full-donation console serialization from both consent-page variants, avoiding an extra JSON allocation when donating.
* Changed extraction from the Utrecht reference's recursive substring matching to explicit field parsing. Donated keys, values and local-time formatting are unchanged: verified message-by-message against the unmodified reference on four real exports, including branches, image parts (flattened like the reference) and reasoning messages (rows with an empty message). Similarly named metadata can no longer override roles/models or be appended to message text.
* Changed parsing to accept only the observed export format: numeric Unix-second timestamps, boolean hidden flags, string titles and known content types.
* Added fail-soon handling of export format changes: an unknown shape of a field present on every conversation or message rejects the export at its first occurrence, logging the reason code and position. Single unusable messages (unknown content type, future timestamp, unserializable text) are skipped and logged as one summary line with counts per reason; if skipped messages leave nothing to donate, the export is rejected.
* Added a configurable time window, `TIME_WINDOW_MONTHS`, now set to `None` (no month cutoff). Positive values enable a calendar-month window. Participant texts and the tracking log follow the setting; `python -m port.script export.zip --window-months 24` (or `--window-months none`) runs it locally and prints counts only.
* Changed exports without messages in the time window to still reach the consent step, with a notice explaining the empty table, so the participant donates it instead of ending without a donation.
* Added the Utrecht reference's tracking donation: the flow log, including rejections and skipped-message summaries, is donated as `<session>-tracking` at every step, whatever the consent decision, and forwarded as `CommandSystemLog` to the host (AppSignal). Logs contain positions and reason codes only, no participant text.
* Added support for split exports: `conversations.json` files listed in `export_manifest.json` are read in order, one at a time alongside the byte-bounded retained messages.
* Added archive safeguards: 256 MiB per JSON file, rejection of missing listed files, duplicate JSON keys, unreadable or excessively nested JSON, and text that cannot be serialized.
* Fixed future-dated timestamps passing the twelve-month filter.
* Removed `port/helpers.py` (reference fuzzy-matching helpers).
* Changed the single CAIS table title to “Your conversations with ChatGPT” (with German and Dutch equivalents), without a part-number suffix.
* Changed the CAIS table to show capitalized column labels (German and Dutch equivalents) and relative column widths: message widest, then conversation title. Uses the upstream display-only `headers` and `column_widths`; donated keys are unchanged.
* Changed the consent-page introduction to the Utrecht reference's wording (English and Dutch verbatim from `port-chatgpt-uu`). The German text is a provisional Eyra translation; replace it with the approved CAIS wording when available.

## Unreleased

* Fixed - Forward fatal worker errors to monitoring, then emit a single `CommandSystemExit` with code `1` and terminate the worker. Ignore late worker events and pending responses after termination; ordinary error-level script logs remain non-terminal. Localize the safe exit explanation in all seven supported languages, with a possible-data-size hint only for recognized memory failures. Keep diagnostics in monitoring and recovery with the host.
* Fixed - Transfer serialized command strings and string response payloads as owned UTF-8 buffers instead of expanding Python strings and copying full original commands across the worker boundary. Release Python command proxies after transfer and avoid logging full render/donation data. Deploy the Python wheel, worker and framework together; public script dictionaries and host donation JSON are unchanged.
* Changed - Allow `data_frame_max_size=None` for explicitly unlimited consent tables and remove the hidden JavaScript 50,000-row cutoff. Existing Python numeric limits and the 10,000-row default remain; scripts are responsible for choosing suitable data and upload limits.
* Added - An opt-in synthetic ZIP generator and process-memory benchmark for the unchanged demo's actual JSON-summary upload, review, delete/undo and donation flow.
* Fixed - Hide framework numbering for a single consent table; number multiple tables in display order without changing script-authored titles or donated data.
* Fixed - Bound long consent-table and mobile-card text to three-line previews with an explicit full-text reader only when lines are hidden, preserving complete search and donation values.
* Changed - Left-align desktop table search when pagination is not needed, without showing a redundant single-page indicator.
* Changed - Show translated field names in full-text dialog headings using Title6, with a baseline-aligned Caption label in grey2 that wraps on narrow screens; empty labels retain the "Full text" fallback.
* Added - Optional `column_widths` on `PropsUIPromptConsentFormTable`: relative desktop column widths per column name (unlisted columns default to 1; non-positive values are rejected). Mobile cards are unchanged.
* Changed - Consent-table `headers` are display-only: desktop headers, mobile card labels and full-text dialogs show the label, while donated rows keep the data frame's column names in every locale. Migration: scripts that relied on `headers` to rename donated keys must rename the data frame columns instead.
* Fixed - Long-text previews collapse line breaks and blank lines into spaces so they fill three lines of content; the full-text dialog, search and donated values keep the original line breaks.
* Changed - The full-text dialog uses a darker `shadow-dialog` drop shadow instead of a dimmed backdrop, so it stands out from the table without showing a gray block when Feldspar is embedded in a host modal such as Next.
* Fixed - Consent-table descriptions are shown under the table title again (lost in release 4). `PropsUIPromptConsentFormTable.description` is now optional: omit it (passing `data_frame=` by keyword) or pass `None` to show no description; existing positional calls keep working. Scripts that pass a description should check its text, since participants will now see it. Donated data is unchanged.
* Fixed - Pin CI to pnpm 12.5.1 to avoid the E2E shutdown regression introduced in pnpm 12.6, and print per-test progress in CI logs.

## \#8 2026-07-03

* Added SafeData for crash-resistant JSON access in donation scripts
* Added locale routing from JavaScript to Python via `port.start(context)` so Python can localize DataFrame content the React i18n layer can't reach
* Added `python -m port` CLI extraction runner for local script development
* Added `workerLog` forwarding and `FlushLogs` sentinel so long-running Python extractions stream log progress to the client in real time
* Changed `PropsUIPromptConfirm.cancel` to be optional; removed the default cancel affordance from demo confirm prompts
* Changed demo `script.py`: refactored into step functions using `yield from` for clearer control flow
* Changed dependency stack: Vite 8, TypeScript 6, Node.js 24.18, Python 3.14.6, Tailwind 4.3, Playwright 1.61, plus a large batch of Renovate/Dependabot bumps
* Fixed `sys.modules` pollution in `test_script_wrapper`
* Fixed release workflow to pin the release tag to the workflow commit

## \#7 2026-03-05

* Added status text during data submission to inform users to keep the window open
* Added CommandSystemLog for forwarding logs from JavaScript and Python to the hosting application
* Changed Tailwind CSS to v4
* Changed CI workflows: added dependency update testing, feature branch releases
* Removed unused _build_release.yml workflow

## \#6 2026-02-25

* Added maximum data frame sizes to both the API and UI
* Added GitHub Actions release workflow with automated testing
* Added unit tests for dataframe truncation (Python and JavaScript)
* Added Lithuanian (LT) and Romanian (RO) translations
* Added Git LFS for test fixtures
* Fixed case-insensitive search in consent table
* Removed redundant Playwright workflow (consolidated into release workflow)

## \#5 2025-09-10

* Switched to pnpm for package management
* Switched to Vite for the frontend build system
* Added Spanish language
* Changed: split script.py into a default basic version in script.py and an advanced version script_custom_ui.py
* Added renovate

## \#4 2025-05-02

* Fixed - Explicit loaded event is sent to ensure proper initialization (channel setup)
* Changed: Feldspar is now split into React component and app
* Changed: Allow multiple block-types to interleave on a submission page
* Added: end to end tests using Playwright

## \#3 2025-04-08

* Changed: layout to support mobile screens (enables mobile friendly data donation)
* Added: support for mobile variant of a table using cards (used for data donation consent screen)

## \#2 2024-06-13

* Added: Support for progress prompt
* Added: German translations
* Added: Support for assets available in Python

## \#1 2024-03-15

Initial version
