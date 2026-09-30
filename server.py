"""Root entry point for the Teams Copilot demo.

    python3 server.py            # http://127.0.0.1:8000/
    PORT=8080 python3 server.py

Serves the UI from teams/, exposes the repository mock-data/ folder as JSON for the
Files and Mail tabs, and reuses teams/server.py for POST /api/chat (mock-data context
+ Copilot model call).
"""

import importlib.util
import json
import os
from datetime import date, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
TEAMS_DIR = ROOT / "teams"
DATA_DIR = ROOT / "mock-data"
PORT = int(os.environ.get("PORT", "8000"))

FILES_INDEX = "sharepoint-onedrive/document-index.json"
MAIL_EXPORT = "outlook/outlook_export.json"
TEAMS_EXPORT = "teams/teams_export.json"
CLIENT_MEMORY = "client-memory/proximus-belgium-client-memory.json"


def load_teams_server():
    """teams/server.py owns the /api/chat path (mock-data context + model call)."""
    spec = importlib.util.spec_from_file_location("teams_demo_server", TEAMS_DIR / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_json(relative_path):
    with open(DATA_DIR / relative_path, encoding="utf-8") as handle:
        return json.load(handle)


def human_size(byte_count):
    if byte_count < 1024:
        return f"{byte_count} B"
    if byte_count < 1024 * 1024:
        return f"{round(byte_count / 1024)} KB"
    return f"{byte_count / (1024 * 1024):.1f} MB"


def human_time(iso_timestamp):
    try:
        moment = datetime.fromisoformat(iso_timestamp)
    except (TypeError, ValueError):
        return str(iso_timestamp or "")[:10]
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    today = date.today()
    if moment.date() == today:
        return f"Today, {moment:%H:%M}"
    if (today - moment.date()).days == 1:
        return "Yesterday"
    return f"{moment:%b %d}"


def one_line(text, limit=120):
    line = next((" ".join(row.split()) for row in str(text or "").splitlines() if row.strip()), "")
    return line[:limit] + ("…" if len(line) > limit else "")


def to_text(item):
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("rule", "detail", "description", "statement", "issue", "summary", "note", "deadline"):
            if isinstance(item.get(key), str):
                return item[key]
        return "; ".join(value for value in item.values() if isinstance(value, str))
    return ""


def load_files():
    files = []
    for document in read_json(FILES_INDEX):
        path = DATA_DIR / str(document.get("relative_path", ""))
        exists = path.is_file()
        files.append(
            {
                "name": document.get("file_name", ""),
                "type": str(document.get("file_format", "file")).upper(),
                "size": human_size(path.stat().st_size) if exists else "—",
                "modified": human_time(datetime.fromtimestamp(path.stat().st_mtime).isoformat()) if exists else "—",
                "owner": document.get("owner") or "Unassigned",
                "status": document.get("status", ""),
                "title": document.get("title", ""),
                "topics": document.get("topics", []),
            }
        )
    return files


def load_mail():
    mail = [
        {
            "sender": (message.get("from") or {}).get("name", ""),
            "subject": message.get("subject", ""),
            "preview": one_line(message.get("body")),
            "time": human_time(message.get("sent_at")),
            "body": " ".join(str(message.get("body", "")).split()),
            "folder": str(message.get("folder", "")).split("/")[-1],
            "sentAt": str(message.get("sent_at", "")),
        }
        for message in read_json(MAIL_EXPORT).get("messages", [])
    ]
    mail.sort(key=lambda item: item["sentAt"], reverse=True)
    return mail


def load_memory():
    memory = read_json(CLIENT_MEMORY)
    return {
        "clientId": memory.get("client_id", ""),
        "clientName": memory.get("client_name", ""),
        "payrollDeadlines": [
            f"{entry.get('deadline', '')} {entry.get('activity', '')}".strip()
            for entry in memory.get("payroll_deadlines", [])
            if isinstance(entry, dict)
        ],
        "standingRules": [to_text(rule) for rule in memory.get("standing_rules", [])],
        "openIssues": [to_text(issue) for issue in memory.get("open_issues", [])],
        "exceptions": [to_text(item) for item in memory.get("exceptions", [])],
    }


def load_teams():
    export = read_json(TEAMS_EXPORT)
    messages = export.get("messages", [])
    recent = sorted(messages, key=lambda message: str(message.get("sent_at", "")), reverse=True)[:12]
    return {
        "messageCount": export.get("message_count", len(messages)),
        "threadCount": export.get("thread_or_chat_count", 0),
        "clientId": export.get("client_id", ""),
        "recent": [
            {
                "sender": message.get("sender", ""),
                "channel": message.get("channel") or message.get("chat_name") or message.get("container_type", ""),
                "body": message.get("body", ""),
                "time": human_time(message.get("sent_at")),
            }
            for message in recent
        ],
    }


DATA_ROUTES = {
    "/api/files": lambda: {"files": load_files()},
    "/api/mail": lambda: {"messages": load_mail()},
    "/api/memory": load_memory,
    "/api/teams": load_teams,
    "/api/manifest": lambda: read_json("manifest.json"),
}


def main():
    teams_server = load_teams_server()
    teams_server.load_env_files()
    context = teams_server.build_context()
    if not context:
        raise SystemExit(f"no mock data found under {DATA_DIR}")

    class Handler(teams_server.make_handler(context)):
        def do_GET(self):  # noqa: N802 (http.server API)
            route = urlsplit(self.path).path
            if route in DATA_ROUTES:
                try:
                    self._json(200, DATA_ROUTES[route]())
                except Exception as error:
                    self._json(500, {"error": f"{type(error).__name__}: {error}"})
                return
            super().do_GET()

    files, mail = load_files(), load_mail()
    base = os.environ.get("COPILOT_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or teams_server.DEFAULT_BASE
    model = os.environ.get("COPILOT_MODEL") or teams_server.DEFAULT_MODEL
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(
        f"[server] http://127.0.0.1:{PORT}/  ui=teams/  data=mock-data/  "
        f"files={len(files)}  mail={len(mail)}  teams={load_teams()['messageCount']}  "
        f"client={load_memory()['clientName']}  context={len(context) // 1024}KB  model={model}  endpoint={base}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
