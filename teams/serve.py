"""Local demo server for the Teams mock.

Serves the mock (index.html, app.js, styles.css) and backs two demo hooks:

- POST /api/outlook-message stores a fake "sent" mail as a new document in the
  category_finetuning database, in the shape of outlook_schema_example.json.
- GET /api/notification hands over the content of category_finetuning/notif
  once per write, so the mock can ask "This mail was categorised as X, is that
  correct?" while the pipeline runs.

Usage:
    python3 serve.py
    python3 serve.py --port 8080 --database ../category_finetuning/database
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = BASE_DIR.parent / "category_finetuning" / "database"

# Values filled in for every mail the mock sends; edit these to change what the
# generated document looks like. Capitalization follows the database entries.
MAILBOX = "proximus-account@sdworx.example"
SENDER = "marie.dubois@sdworx.example"
CLIENT_TAGS = ["Proximus", "Belgium", "electricity"]
CONFIDENCE_HINT = "sd_worx_response"

MESSAGE_FILE = re.compile(r"^outlook-msg-(\d+)\.json$", re.IGNORECASE)
SUBJECT_PREFIX = re.compile(r"^\s*((re|fwd|fw)\s*:\s*)+", re.IGNORECASE)
NOT_SLUG = re.compile(r"[^a-z0-9]+")

_database = DEFAULT_DATABASE
_write_lock = threading.Lock()

# Text file the rest of the pipeline writes to trigger a categorisation popup.
NOTIF_FILE = DEFAULT_DATABASE.parent / "notif"

_notif_file = NOTIF_FILE
_notif_lock = threading.Lock()
_notif_delivered = ""


def next_message_id(database: Path) -> str:
    highest = 0
    for path in database.glob("outlook-msg-*.json"):
        match = MESSAGE_FILE.match(path.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"outlook-msg-{highest + 1:04d}"


def conversation_id(subject: str) -> str:
    topic = SUBJECT_PREFIX.sub("", str(subject))
    slug = NOT_SLUG.sub("-", topic.lower()).strip("-")
    return f"outlook-conv-{slug or 'untitled'}"


def split_addresses(value: object) -> list[str]:
    return [part.strip() for part in re.split(r"[;,]", str(value or "")) if part.strip()]


def build_document(payload: dict, message_id: str) -> dict:
    """Build one database document (fields: see outlook_schema_example.json)."""
    subject = " ".join(str(payload.get("subject") or "").split()) or "(no subject)"
    return {
        "source": "outlook",
        "type": "email",
        "mailbox": MAILBOX,
        "conversation_id": conversation_id(subject),
        "message_id": message_id,
        "subject": subject,
        "from": SENDER,
        "to": split_addresses(payload.get("to")),
        "cc": [],
        "sent_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        "body": str(payload.get("body") or ""),
        "attachments": [],
        "client_tags": list(CLIENT_TAGS),
        "confidence_hint": CONFIDENCE_HINT,
    }


def read_notification() -> dict | None:
    """Hand over the notif file once per write; its content drives the popup.

    Returns None when the file is missing, empty, or already delivered. Rewriting
    the file changes its mtime, which makes it news again.
    """
    global _notif_delivered
    try:
        raw = _notif_file.read_text(encoding="utf-8")
        version = str(_notif_file.stat().st_mtime_ns)
    except OSError:
        return None
    category = " ".join(raw.split())
    if not category:
        return None
    with _notif_lock:
        if version == _notif_delivered:
            return None
        _notif_delivered = version
    return {"category": category, "version": version}


class MockHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server names its handlers this way
        if urlparse(self.path).path == "/api/notification":
            notification = read_notification()
            self._send_json({"category": notification["category"] if notification else None})
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - http.server names its handlers this way
        if urlparse(self.path).path != "/api/outlook-message":
            self.send_error(404, "unknown endpoint")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
        except (ValueError, json.JSONDecodeError):
            self.send_error(400, "invalid JSON body")
            return

        with _write_lock:
            message_id = next_message_id(_database)
            document = build_document(payload, message_id)
            target = _database / f"{message_id}.json"
            target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        print(f"  inserted {target}")

        self._send_json({"ok": True, "message_id": message_id, "path": str(target)})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE,
                        help="document database directory (default: category_finetuning/database)")
    parser.add_argument("--notif", type=Path, default=NOTIF_FILE,
                        help="text file watched for categorisation popups (default: category_finetuning/notif)")
    args = parser.parse_args()

    global _database, _notif_file
    _database = args.database.resolve()
    _notif_file = args.notif.resolve()
    _database.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    print(f"Teams mock: http://{args.host}:{args.port}/  (database: {_database})")
    print("Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("server stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
