# Changelog

## Types of Changes and How to Note Them

* Added - For any new features that have been added since the last version was released
* Changed - To note any changes to the software's existing functionality
* Deprecated - To note any features that were once stable but are no longer and have thus been removed
* Fixed - Any bugs or errors that have been fixed should be so noted
* Removed - This notes any features that have been deleted and removed from the software
* Security - This acts as an invitation to users who want to upgrade and avoid any software vulnerabilities

## Unreleased — CAIS ChatGPT

* Changed extraction from the Utrecht reference's recursive substring matching to explicit field parsing. Donated keys, values and local-time formatting are unchanged: verified message-by-message against the unmodified reference on four real exports, including branches, image parts (flattened like the reference) and reasoning messages (rows with an empty message). Similarly named metadata can no longer override roles/models or be appended to message text.
* Changed parsing to accept only the observed export format: numeric Unix-second timestamps, boolean hidden flags, string titles and known content types. Anything else is reported as a processing issue rather than guessed.
* Added per-record failure handling: a malformed message or conversation is excluded without discarding the rest of the export, and reported in a donated `chatgpt_processing_issues_N` table (positions and fixed reason codes only, no participant text). Exports without issues donate unchanged JSON; exports where failures leave no usable messages offer a retry.
* Added support for split exports: `conversations.json` files listed in `export_manifest.json` are read in order, one at a time, keeping peak memory to one file.
* Added archive safeguards: 256 MiB per JSON file, rejection of missing listed files, duplicate JSON keys, unreadable or excessively nested JSON, and text that cannot be serialized.
* Fixed future-dated timestamps passing the twelve-month filter.
* Removed `port/helpers.py` (reference fuzzy-matching helpers).
* Changed study table titles to “Your conversations with ChatGPT” (with German and Dutch equivalents); omitted the title suffix for a single table while retaining part numbers for multiple tables.
* Changed the CAIS table to show capitalized column labels (German and Dutch equivalents) and relative column widths: message widest, then conversation title. Uses the upstream display-only `headers` and `column_widths`; donated keys are unchanged.
* Changed the consent-page introduction to the Utrecht study's wording (English and Dutch verbatim from `port-chatgpt-uu`). The German text is a provisional Eyra translation; replace it with the approved CAIS wording when available.

## Unreleased

* Fixed - Hide framework numbering for a single consent table; number multiple tables in display order without changing script-authored titles or donated data.
* Fixed - Bound long consent-table and mobile-card text to three-line previews with an explicit full-text reader only when lines are hidden, preserving complete search and donation values.
* Changed - Left-align desktop table search when pagination is not needed, without showing a redundant single-page indicator.
* Changed - Show translated field names in full-text dialog headings using Title6, with a baseline-aligned Caption label in grey2 that wraps on narrow screens; empty labels retain the "Full text" fallback.
* Added - Optional `column_widths` on `PropsUIPromptConsentFormTable`: relative desktop column widths per column name (unlisted columns default to 1; non-positive values are rejected). Mobile cards are unchanged.
* Changed - Consent-table `headers` are display-only: desktop headers, mobile card labels and full-text dialogs show the label, while donated rows keep the data frame's column names in every locale. Migration: scripts that relied on `headers` to rename donated keys must rename the data frame columns instead.
* Fixed - Long-text previews collapse line breaks and blank lines into spaces so they fill three lines of content; the full-text dialog, search and donated values keep the original line breaks.
* Changed - The full-text dialog uses a darker `shadow-dialog` drop shadow instead of a dimmed backdrop, so it stands out from the table without showing a gray block when Feldspar is embedded in a host modal such as Next.

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
