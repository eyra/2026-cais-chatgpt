#!/usr/bin/env python3
"""Generate a synthetic ChatGPT export for load tests of the CAIS extraction.

Conversations are shaped like real exports (root placeholder, hidden undated
system message, visible user/assistant messages with Unix-second timestamps)
and spread evenly over the last --months months. Files larger than --part-mib
are split and listed in export_manifest.json, as in large real exports. The
text is synthetic; no participant data is involved.

    python tests/generate_chatgpt_export.py heavy.zip --mib 600 --months 36
"""

import argparse
import json
import random
import time
import zipfile

WORDS = (
    "the of and to in is that for it as with was on be by this are or from at "
    "which an have not but all can more one were their has will also research "
    "data model answer question summary example python table message export"
).split()


def text(rng: random.Random, size: int) -> str:
    words = []
    length = 0
    while length < size:
        word = rng.choice(WORDS)
        words.append(word)
        length += len(word) + 1
    return " ".join(words)


def conversation(rng: random.Random, index: int, start: float, messages: int, average: int) -> dict:
    mapping = {"root": {"message": None, "parent": None}}
    mapping["system"] = {"message": {
        "author": {"role": "system"}, "create_time": None, "content": {"parts": [""]},
        "metadata": {"is_visually_hidden_from_conversation": True},
    }}
    for position in range(messages):
        role = "user" if position % 2 == 0 else "assistant"
        mapping[f"m{position}"] = {"message": {
            "author": {"role": role},
            "create_time": start + position * 60 + rng.random(),
            "content": {"content_type": "text", "parts": [text(rng, rng.randint(average // 4, average * 7 // 4))]},
            "metadata": {"model_slug": "gpt-5"} if role == "assistant" else {},
        }}
    return {"title": f"Synthetic conversation {index}", "mapping": mapping}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output")
    parser.add_argument("--mib", type=float, default=600, help="approximate message text in MiB")
    parser.add_argument("--months", type=int, default=36, help="months of history before now")
    parser.add_argument("--part-mib", type=float, default=200, help="maximum conversations file size in MiB")
    parser.add_argument("--messages", type=int, default=20, help="messages per conversation")
    parser.add_argument("--average", type=int, default=2000, help="average message length in characters")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    now = time.time()
    span = args.months * 30.44 * 86400
    conversations = max(1, int(args.mib * 1024 * 1024 / (args.messages * args.average)))
    part_limit = int(args.part_mib * 1024 * 1024)
    parts = []
    written = 0
    # Exclusive creation avoids accidentally replacing an existing export.
    with zipfile.ZipFile(args.output, "x", zipfile.ZIP_DEFLATED) as archive:
        index = 0
        while index < conversations:
            name = f"conversations-{len(parts):03d}.json"
            parts.append(name)
            with archive.open(name, "w") as part:
                size = 0
                separator = b"["
                while index < conversations:
                    start = now - 3600 - rng.random() * span
                    encoded = json.dumps(conversation(rng, index, start, args.messages, args.average)).encode()
                    if size and size + len(encoded) + 2 > part_limit:
                        break
                    part.write(separator + encoded)
                    size += len(encoded) + 1
                    separator = b","
                    index += 1
                part.write(b"[]" if separator == b"[" else b"]")
                written += size + 1
        manifest = {"version": 1, "logical_files": {"conversations.json": {"files": parts, "sharded": len(parts) > 1}}}
        archive.writestr("export_manifest.json", json.dumps(manifest))
    print(json.dumps({
        "path": args.output, "conversations": conversations,
        "visible_messages": conversations * args.messages, "parts": len(parts),
        "json_mib": round(written / 1024 / 1024, 1), "months": args.months,
    }))


if __name__ == "__main__":
    main()
