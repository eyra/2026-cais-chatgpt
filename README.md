# Feldspar

Feldspar is an integration mechanism for building data donation applications that can be hosted on the [Next](https://next.eyra.co/) platform. It enables researchers to create custom data extraction and donation flows using Python and React.

## Digital Trace Data Donation (Port)

More information about the Port program can be found [here](https://eyra.notion.site/Port-Program-4bbf0bbc466547af95f05c609405c4b2?pvs=4).

Feldspar enables researchers to:

- Extract only the data of interest through local processing (on the participant's device) using Python (Pyodide)
- Prompt participants for questions about the data
- Enable participants to inspect the extracted data before donation
- Enable participants to delete table rows before donation
- Consent or decline to donate the extracted data

## Getting Started

### Prerequisites

- Fork or clone this repo
- Install [Node.js](https://nodejs.org/en)
- Install [pnpm](https://pnpm.io/installation) (Fast, disk space efficient package manager)
- Install [Python](https://www.python.org/) (Version 3.11 or higher)
- Install [Poetry](https://python-poetry.org/)
- Install [Earthly CLI](https://earthly.dev/get-earthly)

### Installation

1. Install dependencies:

   ```sh
   pnpm install
   ```

2. Run the project locally with hot reloading (builds Python package and starts the development server):

   ```sh
   pnpm run start
   ```

3. Access the application at [http://localhost:3000](http://localhost:3000)

## Customizing the Python Code

The core of Feldspar's functionality is in the Python script at `packages/python/port/script.py`. This script defines the flow of the data donation process.

### CAIS extraction and reference provenance

The five donated fields (`conversation title`, `role`, `message`, `model`,
`time`) and local-time `YYYY-MM-DD HH:MM:SS` formatting follow
[trbKnl/port-chatgpt-uu at commit
`d80e9d834095034c9e80c85986bdab61fb8f152a`](https://github.com/trbKnl/port-chatgpt-uu/tree/d80e9d834095034c9e80c85986bdab61fb8f152a)
(AGPL-3.0). Donated values mirror the reference: on four real exports
(August 2026), every message produced the same row as the unmodified reference
extractor. The reference's fuzzy key matching is replaced by explicit parsing
that accepts only the observed format and reports anything else ("fail soon"):

- `packages/python/port/chatgpt.py` reads only `title`, `message.author.role`,
  `message.metadata.model_slug`, `message.create_time`, the hidden flag and
  `message.content.parts`. Similarly named keys elsewhere can no longer override
  a field or be appended to message text. Message text is retained verbatim,
  including markdown and ChatGPT's inline citation markers.
- As in the reference: all mapping branches (edited prompts, regenerated
  answers) are donated; non-text parts are flattened into their scalar values
  (e.g. image pointer and dimensions, `None` for nulls); reasoning messages
  (`thoughts`, `reasoning_recap`) are rows with an empty message.
- Only observed encodings are accepted: timestamps as numeric Unix seconds, the
  hidden flag as a boolean (normally absent), titles as strings. Absent model
  or metadata (user messages) is an empty model.
- The table shows messages newest first. The optional time window is
  `TIME_WINDOW_MONTHS` in `port/script.py`, currently set to **`None`**: no month
  cutoff. Set it to a positive number to keep only the last N calendar months
  (UTC) before upload. The window used is recorded in the tracking donation.
  The donation byte budget applies independently of the month window.
  A test release with another window can change only this value (CI builds a
  release per branch). The reference had no window and kept export order
  (conversations in file order, messages in mapping order).
- `packages/python/port/script.py` validates the archive boundary. Current
  exports list their files in `export_manifest.json`; large exports may split
  `conversations.json` into several files, which are read in manifest order,
  one at a time, alongside the retained messages bounded by the donation budget.
  Exports without such a manifest must contain exactly one `conversations.json`.
  Each file must be at most 256 MiB of readable UTF-8 JSON without duplicate
  keys. A missing listed file or unreadable JSON rejects the export with a retry.

Two kinds of failure are handled differently:

- **Format change** (fail soon): a field present on every conversation or
  message has an unknown shape, e.g. a timestamp that is no longer a number or
  a missing `mapping` or `author.role`. Every message would be affected, so
  extraction stops at the first occurrence and the export is rejected with a
  retry. The log names the reason code and position, so the script can be fixed
  and participants can be asked to donate again.
- **Unusable single message**: an unknown content type without `parts`, a
  timestamp in the future (device clock), or text that cannot be serialized.
  Only that message is skipped; extraction continues. If skipped messages leave
  nothing to donate, the export is rejected with a retry.

Both are logged as warnings without titles, text or export IDs. Skipped
messages are logged as one summary line with counts per reason, e.g.
`Processing issues: unknown_content_type:canvas message_excluded=3`. Every log
line reaches two channels:

- the host log (`CommandSystemLog`, forwarded by Next to AppSignal), so Eyra
  sees format changes early;
- the tracking donation `<session>-tracking`, as in the Utrecht reference
  (`donate_logs`): the flow log is donated at every step, whatever the consent
  decision, so researchers can account for skipped messages in their analysis.

The consent screen shows only the conversation tables; its donated JSON is
unchanged. Messages outside the time window are not issues. An export with no
messages in the window still reaches the consent step: a notice explains the
empty table and the participant donates it, so researchers can tell "no recent
ChatGPT use" apart from drop-out.

Coverage is in `packages/python/tests/` and `tests/donation.spec.ts`, using
synthetic fixtures shaped like the real exports. No split export has been
observed yet; that support follows the manifest format and is covered by
synthetic tests only.

### Basic Structure

1. Fork the repository to create your own version
2. Navigate to `packages/python/port/script.py`
3. Modify the `process(data)` function to customize your data donation flow

A basic donation flow typically includes:

1. Prompt the participant to select a file
2. Extract relevant data from the file
3. Present the extracted data in a consent form
4. Process the participant's consent decision

### Example: Modifying the File Selection Screen

```python
def prompt_file(extensions):
    description = props.Translatable({
        "en": "Please select your data export file.",
        "de": "Bitte wählen Sie Ihre Datenexportdatei aus.",
        "it": "Seleziona il tuo file di esportazione dati.",
        "es": "Por favor, seleccione su archivo de exportación de datos.",
        "nl": "Selecteer uw data-exportbestand."
    })
    return props.PropsUIPromptFileInput(description, extensions)
```

### Working with Assets

Add any static assets your script needs to `packages/python/port/assets/`. Access them in your script:

```python
from port.api.assets import *

def process(sessionId):
    # Path to an asset
    path = asset_path("my_file.txt")

    # Open an asset directly
    file = open_asset("my_file.txt")

    # Read asset contents
    content = read_asset("my_file.txt")
```

### Donation size and worker memory

CAIS sets `MAX_DONATION_BYTES` in `port/script.py` to **200,000,000 bytes**.
This bounds the final UTF-8 donation JSON, including all five message fields,
JSON escaping, separators, table identifiers and deletion metadata. It leaves
approximately 10 MB for multipart overhead below Next's 210,000,000-byte
whole-request limit; ZIP size and raw message text size are not the budget.

Extraction retains the globally newest whole-message prefix that fits.
Older messages beyond the cutoff are excluded, even if a smaller older message
would fit the remaining space. Equal timestamps retain export encounter order.
Selection is bounded while reading the export, rather than accumulating every
message and truncating afterward. If the newest eligible message alone exceeds
the budget, consent still opens with an empty table and explains that the newest
message is too large to include.

The table description shows the original number of messages available for review,
with localized number formatting. It does not change when participants remove rows.
When the size limit excludes messages, the description also shows that count and
explains that the newest messages were kept. These counts are not a total of every
record in the export: hidden, unusable, and out-of-window messages are separate.
Size-limit exclusions remain in the host monitoring and tracking-donation logs.

CAIS uses one paginated table with `data_frame_max_size=None`; there is no
JavaScript row cutoff. More than 100,000 short messages can therefore be retained.
The Python consent-table API still defaults to 10,000 rows for other callers;
explicit numeric limits retain their existing behavior.

Serialized command strings cross the worker boundary as transferable UTF-8
buffers and are decoded before UI handling. Responses return only their payload,
not the original command, and transferred Python command proxies are released.
Encoding leaves script-owned props unchanged. Deploy the Python wheel, worker
and framework together; public script dictionaries and host donation JSON remain
unchanged.

Fatal worker errors are forwarded to monitoring before Feldspar sends one
`CommandSystemExit` with code `1` and stops the worker. The exit explanation uses
the participant's locale, without exposing error details. `MemoryError`, the
observed pandas `OverflowError` messages (`Could not reserve memory block` and
`Maximum recursion level reached`), and WASM `memory access out of bounds` failures
use: “Data processing failed. The data may be too large to process on this device.”
Other fatal errors use: “Data processing failed.” Both messages are translated
into the seven supported languages.

Late worker events and pending UI responses cannot resume processing after failure.
Error-level script logs do not themselves end the flow; handled errors can still
use script-defined retries. The host owns recovery; Feldspar does not restart the
failed runtime or add a retry UI.

The budget is not a universal browser-memory guarantee: parsing an individual
export file, dataframe objects and serialization still consume additional memory.
The 256 MiB per-file safeguard remains; tab-crash recovery is a separate Next concern.

The inherited `tests/generate_memory_zip.py` and `tests/memory-benchmark.cjs`
exercise the upstream Feldspar demo, not CAIS's ChatGPT extraction. Run them in
an [upstream Feldspar checkout](https://github.com/eyra/feldspar#reproducing-large-donation-memory-use).

### Local extraction debugging (CLI)

Run the extraction locally against a real export, with any time window — no
browser or Pyodide needed:

```bash
cd packages/python
poetry run python -m port.script path/to/export.zip                     # time window
poetry run python -m port.script path/to/export.zip --window-months 24
poetry run python -m port.script path/to/export.zip --window-months none
```

It prints the tracking log and counts (messages, conversations, date range),
never message content. To try another window in the browser, change
`TIME_WINDOW_MONTHS`; `pnpm run start` rebuilds the wheel on save.

### Adding Dependencies

If you need additional Python packages, add them to `packages/python/pyproject.toml` in the `tool.poetry.dependencies` section.

## Adding New Visual Components (Advanced)

Feldspar allows you to add custom UI components that can be used in your Python script. This is a more advanced feature that requires understanding both Python and React.

### Step 1: Define Component Types

Create a new folder in `packages/data-collector/src/components/my_component/` and add a `types.ts` file:

```typescript
export interface PropsUIPromptMyComponent {
  __type__: "PropsUIPromptMyComponent";
  title: string;
  // Add any other properties your component needs
}
```

### Step 2: Create React Component

Add a `component.tsx` file to implement your component:

```typescript
import React from "react";
import { PropsUIPromptMyComponent } from "./types";
import { ReactFactoryContext } from "@eyra/feldspar";

type Props = PropsUIPromptMyComponent & ReactFactoryContext;

export const MyComponent: React.FC<Props> = ({ title, resolve }) => {
  return (
    <div>
      <h1>{title}</h1>
      <button
        onClick={() => resolve?.({ __type__: "PayloadTrue", value: true })}
      >
        Continue
      </button>
    </div>
  );
};
```

### Step 3: Create Component Factory

Add a new file at `packages/data-collector/src/factories/my_component.tsx`:

```typescript
import { PromptFactory, ReactFactoryContext } from "@eyra/feldspar";
import React from "react";
import { MyComponent } from "../components/my_component/component";
import { PropsUIPromptMyComponent } from "../components/my_component/types";

export class MyComponentFactory implements PromptFactory {
  create(body: unknown, context: ReactFactoryContext) {
    if (this.isMyComponent(body)) {
      return <MyComponent {...body} {...context} />;
    }
    return null;
  }

  private isMyComponent(body: unknown): body is PropsUIPromptMyComponent {
    return (
      (body as PropsUIPromptMyComponent).__type__ === "PropsUIPromptMyComponent"
    );
  }
}
```

### Step 4: Register Your Component

Update `packages/data-collector/src/App.tsx` to include your new factory:

```typescript
import { DataSubmissionPageFactory, ScriptHostComponent } from "@eyra/feldspar";
import { HelloWorldFactory } from "./factories/hello_world";
import { MyComponentFactory } from "./factories/my_component";

function App() {
  return (
    <div className="App">
      <ScriptHostComponent
        workerUrl="./py_worker.js"
        standalone={process.env.NODE_ENV !== "production"}
        factories={[
          new DataSubmissionPageFactory({
            promptFactories: [
              new HelloWorldFactory(),
              new MyComponentFactory(), // Add your new factory here
            ],
          }),
        ]}
      />
    </div>
  );
}

export default App;
```

### Step 5: Use Your Component in Python

Add a class to your `script.py` to create your component:

```python
from dataclasses import dataclass

@dataclass
class PropsUIPromptMyComponent:
    title: str

    def toDict(self):
        dict = {}
        dict["__type__"] = "PropsUIPromptMyComponent"
        dict["title"] = self.title
        return dict

def process(sessionId):
    result = yield render_data_submission_page(
        PropsUIPromptMyComponent("My Custom Component")
    )
    # Handle the result...
```

## Creating a Release

When your data donation application is ready for deployment:

1. Create a release package:

   ```sh
   ./release.sh
   ```

2. Find the generated ZIP file in the `releases/` directory, named with the current date and sequential number (e.g., `feldspar_2023-07-15_1.zip`)

3. This ZIP file can be deployed to:
   - The Next platform
   - A self-hosted environment
   - Any server that can host static files and store the donated data

To use the release in the Next platform, add a "Donate task" and select the generated ZIP file as the "Flow application".

## Important Disclaimer

Please review the [disclaimer](./DISCLAIMER.md) in this repository for important information about technical limitations, logging behavior, and data handling considerations.

## Funding

Feldspar is part of the Port program for data donation and has been funded by the UU, PDI-SSH ([D3i project](https://datadonation.eu/)), and [Eyra](https://www.eyra.co/).

## Contributing

We welcome contributions to make Feldspar better. Please read our [contributing guidelines](https://github.com/eyra/feldspar/blob/master/CONTRIBUTING.md) for details on how to submit issues, feature requests, and pull requests.

<!-- Original detailed API examples and technical specifications can be included here -->
