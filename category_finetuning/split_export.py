"""Split a big mock export into individual document files in the database.

Works for both export styles (outlook_export.json, teams_export.json): the
file must contain a ``messages`` list (or be a list itself). Each message is
written as its own JSON file. When an export uses ``source_system`` /
``source_type`` instead of the project schema's ``source`` / ``type``, those
fields are filled in, everything else is preserved untouched.

Usage:
    python3 split_export.py /path/to/outlook_export.json
    python3 split_export.py /path/to/outlook_export.json /path/to/teams_export.json --database database
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
BASE_DIR = Path(__file__).resolve().parent


def default_type(source: str) -> str:
    return "email" if "outlook" in source.lower() else "channel_message"


def split_export(path: Path) -> tuple[list[dict], str, int]:
    data = json.loads(path.read_bytes())
    messages = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(messages, list):
        raise SystemExit(f"{path}: no 'messages' list found")
    fallback_source = path.parent.name
    prepared: list[dict] = []
    skipped = 0
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            skipped += 1
            continue
        document = dict(message)
        source = str(document.get("source") or document.get("source_system") or fallback_source)
        document.setdefault("source", source)
        document.setdefault("type", str(document.get("type") or document.get("source_type") or default_type(source)))
        if not document.get("sent_at") or not document.get("client_tags"):
            print(f"  skip {path.name} #{index}: missing sent_at or client_tags")
            skipped += 1
            continue
        document.setdefault("message_id", f"{source}-{index:04d}")
        prepared.append(document)
    return prepared, fallback_source, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Split an export file into one JSON document per message.")
    parser.add_argument("exports", nargs="+", type=Path, help="export file(s) to split")
    parser.add_argument("--database", type=Path, default=BASE_DIR / "database",
                        help="output directory (default: database/)")
    args = parser.parse_args(argv)

    args.database.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped = 0
    counts: dict[str, int] = {}
    for export in args.exports:
        documents, _, export_skipped = split_export(export)
        skipped += export_skipped
        for document in documents:
            identifier = str(document["message_id"])
            filename = SAFE_FILENAME.sub("-", identifier).strip("-") + ".json"
            (args.database / filename).write_text(json.dumps(document, indent=2) + "\n")
            written += 1
            source = str(document.get("source", "unknown"))
            counts[source] = counts.get(source, 0) + 1

    print(f"wrote {written} files to {args.database}")
    for source, count in sorted(counts.items()):
        print(f"  {source}: {count}")
    if skipped:
        print(f"skipped {skipped} message(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
