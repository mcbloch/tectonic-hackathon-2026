#!/usr/bin/env python3
"""Teams Copilot demo backend: serves teams/ and answers questions with a real LLM.

Stdlib only. The whole mock-data pack is loaded once at startup and shipped as the
system context of every request, so the model behaves like a Copilot that already
has the workspace open. The browser only sends the conversation turns.

Config (first match wins, read from the process env, teams/.env, then the root .env):
  COPILOT_BASE_URL | OPENAI_BASE_URL   default https://opencode.ai/zen/go/v1
  COPILOT_API_KEY  | OPENAI_API_KEY | API_KEY   (root .env API_KEY is the opencode key)
  COPILOT_MODEL                        default deepseek-v4-flash

Run:  python3 teams/server.py            -> http://127.0.0.1:8000/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

TEAMS_DIR = Path(__file__).resolve().parent
ROOT = TEAMS_DIR.parent
MOCK = ROOT / "mock-data"

DEFAULT_BASE = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL = "deepseek-v4-flash"
# The opencode gateway refuses unroutable requests without one; harmless elsewhere.
SESSION_ID = uuid.uuid4().hex

MAX_BODY = 256 * 1024
MAX_TURNS = 12
UPSTREAM_TIMEOUT = 180

PERSONA = """\
You are Copilot, the assistant built into Microsoft Teams for the SD Worx account team.
The team serves one client: Proximus SA Belgium (client id PROX-BE-001), Belgian monthly payroll.
A colleague who just took over the account is asking you questions in the Teams Copilot pane.

Answer only from the workspace context below. Rules:
- Be specific: dates, cut-off times, owners, document and message ids.
- Cite the sources you used inline, e.g. [SP-PROX-002] or [outlook-msg-0003].
- If the context does not answer the question, say so and name what is missing. Never invent
  a deadline, an amount, a name or a policy.
- When sources disagree, say so explicitly, give both claims with their sources, and state
  which one is the standing rule and which is an exception.
- Lead with the answer, then the evidence. Keep it short: a few sentences or a short list.
"""


def load_env_files() -> None:
    """Merge .env values into the environment; the process env always wins."""
    merged: dict[str, str] = {}
    for path in (ROOT / ".env", TEAMS_DIR / ".env"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            merged[key.strip()] = value.strip().strip("'\"")
    for key, value in merged.items():
        os.environ.setdefault(key, value)


def _json_file(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _who(person) -> str:
    if not isinstance(person, dict):
        return str(person or "?")
    name = person.get("name") or person.get("email") or "?"
    org = person.get("org")
    return f"{name} ({org})" if org else name


def _people(people) -> str:
    return ", ".join(_who(person) for person in people or []) or "-"


def _attachments(attachments, referenced) -> str:
    names = []
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        doc_id = attachment.get("sharepoint_document_id")
        names.append(f"{attachment.get('name')} [{doc_id}]" if doc_id else str(attachment.get("name")))
    for doc_id in referenced or []:
        if doc_id not in " ".join(names):
            names.append(f"[{doc_id}]")
    return "; ".join(names)


def _teams_section(messages) -> str:
    lines = []
    for message in messages or []:
        where = message.get("channel") or message.get("chat_name") or message.get("meeting_title") or "-"
        header = (
            f"[{message.get('message_id')}] {message.get('sent_at')} | "
            f"{message.get('team') or '-'} / {where} | {message.get('sender')} "
            f"({message.get('sender_org') or '?'})"
        )
        if message.get("reply_to_id"):
            header += f" | reply to {message['reply_to_id']}"
        block = [header, f"  {message.get('body') or ''}"]
        attachments = _attachments(message.get("attachments"), message.get("referenced_documents"))
        if attachments:
            block.append(f"  attachments: {attachments}")
        lines.append("\n".join(block))
    return "\n".join(lines)


def _outlook_section(messages) -> str:
    lines = []
    for message in messages or []:
        attachments = _attachments(message.get("attachments"), message.get("referenced_documents"))
        block = [
            f"[{message.get('message_id')}] {message.get('sent_at')} | {message.get('folder')} | "
            f"from {_who(message.get('from'))} to {_people(message.get('to'))} | cc {_people(message.get('cc'))}",
            f"  subject: {message.get('subject')}",
            f"  {message.get('body') or ''}",
        ]
        if attachments:
            block.append(f"  attachments: {attachments}")
        lines.append("\n".join(block))
    return "\n".join(lines)


def build_context() -> str:
    sections: list[str] = []

    memory = _json_file(MOCK / "client-memory" / "proximus-belgium-client-memory.json")
    if memory:
        sections.append(
            "# Client memory (distilled and internally reviewed, highest trust)\n"
            + json.dumps(memory, indent=1, ensure_ascii=False)
        )

    docs = _json_file(MOCK / "sharepoint-onedrive" / "document-index.json")
    if docs:
        entries = docs.get("documents", docs) if isinstance(docs, dict) else docs
        lines = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            lines.append(
                f"[{entry.get('document_id')}] {entry.get('title') or entry.get('file_name')} "
                f"({entry.get('file_format')}) | owner {entry.get('owner')} | status {entry.get('status')} | "
                f"path {entry.get('relative_path')}\n  topics: {', '.join(entry.get('topics') or [])}"
            )
        sections.append("# SharePoint / OneDrive documents (metadata only, file bodies not loaded)\n" + "\n".join(lines))

    teams = _json_file(MOCK / "teams" / "teams_export.json")
    if teams:
        messages = teams.get("messages", teams) if isinstance(teams, dict) else teams
        sections.append(
            f"# Microsoft Teams messages ({len(messages)} messages, team 'SD Worx - Proximus Account')\n"
            + _teams_section(messages)
        )

    outlook = _json_file(MOCK / "outlook" / "outlook_export.json")
    if outlook:
        messages = outlook.get("messages", outlook) if isinstance(outlook, dict) else outlook
        sections.append(f"# Outlook email ({len(messages)} messages, shared mailbox)\n" + _outlook_section(messages))

    conflicts = _json_file(MOCK / "derived" / "seeded_conflicts.json")
    if conflicts:
        sections.append("# Seeded conflicts (known contradictions to watch for)\n" + json.dumps(conflicts, indent=1, ensure_ascii=False))

    questions = _json_file(MOCK / "derived" / "continuity_questions.json")
    if questions:
        sections.append("# Continuity questions this demo is expected to answer\n" + json.dumps(questions, indent=1, ensure_ascii=False))

    return "\n\n".join(sections)


def chat(messages: list[dict], context: str) -> str:
    base = (os.environ.get("COPILOT_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE).rstrip("/")
    key = os.environ.get("COPILOT_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("API_KEY")
    model = os.environ.get("COPILOT_MODEL") or DEFAULT_MODEL
    if not key:
        raise RuntimeError("no API key configured (set API_KEY in .env or COPILOT_API_KEY in teams/.env)")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": f"{PERSONA}\n\n# Workspace context\n{context}"},
            *messages,
        ],
        "temperature": 0.2,
        "max_tokens": 700,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "User-Agent": "teams-copilot-demo/1.0",
    }
    if "opencode.ai" in base:
        headers["x-opencode-session"] = os.environ.get("COPILOT_SESSION_ID") or SESSION_ID
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        raise RuntimeError(f"LLM call failed: {exc.code} {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"LLM unreachable: {exc}") from exc

    choices = body.get("choices") or []
    content = (choices[0].get("message", {}).get("content") if choices else None) or ""
    if not content.strip():
        raise RuntimeError("LLM returned an empty answer")
    return content.strip()


def normalise(payload: dict) -> list[dict]:
    turns = []
    for entry in payload.get("messages") or []:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        text = str(entry.get("content") or "").strip()
        if role in {"user", "assistant"} and text:
            turns.append({"role": role, "content": text})
    while turns and turns[0]["role"] == "assistant":
        turns.pop(0)
    return turns[-MAX_TURNS:]


def make_handler(context: str):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(TEAMS_DIR), **kwargs)

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            if urlsplit(self.path).path != "/api/chat":
                self._json(404, {"error": "unknown endpoint"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                self._json(413, {"error": "bad request size"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
                messages = normalise(payload)
            except (json.JSONDecodeError, AttributeError):
                self._json(400, {"error": "invalid JSON body"})
                return
            if not messages:
                self._json(400, {"error": "no question provided"})
                return
            try:
                reply = chat(messages, context)
            except RuntimeError as exc:
                self._json(502, {"error": str(exc)})
                return
            self._json(200, {"reply": reply})

    return Handler


def main() -> None:
    load_env_files()
    parser = argparse.ArgumentParser(description="Teams Copilot demo server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    context = build_context()
    if not context:
        sys.exit(f"no mock data found under {MOCK}")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(context))
    base = os.environ.get("COPILOT_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE
    model = os.environ.get("COPILOT_MODEL") or DEFAULT_MODEL
    print(f"Teams Copilot on http://{args.host}:{args.port}/  model={model}  endpoint={base}  context={len(context) // 1024} KB")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
