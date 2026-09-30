"""Load individual document files and track which ones are new or changed.

The database directory holds one JSON file per document (outlook mail or Teams
message, see the *_schema_example.json files). Files are validated lightly and
turned into Document records. Freshness is tracked with a state file that maps
absolute path -> {fingerprint, mtime_ns}; a document is evaluated when it is
new, its content changed, or its mtime moved (the pipeline is timestamp aware).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

REQUIRED_FIELDS = ("source", "type", "sent_at", "client_tags")
SOURCE_FIELDS = {
    "outlook": ("subject", "from", "to"),
    "microsoft_teams": ("team", "sender"),
}
STATE_VERSION = 1


def canonical_tag(tag: str) -> str:
    """Case/whitespace insensitive form used everywhere in the graph."""
    return " ".join(tag.split()).lower()


def file_fingerprint(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass(frozen=True)
class Document:
    path: Path
    doc_id: str
    source: str
    doc_type: str
    sent_at: str
    tags: tuple[str, ...]          # canonical tags
    display_tags: tuple[str, ...]  # original strings, for reports
    excerpt: str
    fingerprint: str
    mtime_ns: int


@dataclass(frozen=True)
class BatchItem:
    document: Document
    status: str  # "new" | "edited" | "touched" | "forced" | "since"


def _excerpt(data: dict) -> str:
    if data.get("subject"):
        return " ".join(str(data["subject"]).split())[:160]
    return " ".join(str(data.get("body", "")).split())[:160]


def _validate(data: object, path: Path) -> None:
    if not isinstance(data, dict):
        raise ValueError("document is not a JSON object")
    missing = [field for field in REQUIRED_FIELDS if field not in data]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")
    tags = data["client_tags"]
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError("client_tags must be a list of strings")
    if not any(t.strip() for t in tags):
        raise ValueError("client_tags is empty")
    for field in SOURCE_FIELDS.get(str(data["source"]), ()):
        if field not in data:
            raise ValueError(f"missing {data['source']} field: {field}")


def load_documents(directories: Sequence[Path]) -> tuple[list[Document], list[str]]:
    documents: list[Document] = []
    errors: list[str] = []
    for directory in directories:
        root = Path(directory)
        if not root.exists():
            errors.append(f"{root}: directory does not exist")
            continue
        for path in sorted(root.rglob("*.json")):
            try:
                raw = path.read_bytes()
                data = json.loads(raw)
                _validate(data, path)
                seen: set[str] = set()
                tags, display = [], []
                for value in data["client_tags"]:
                    canonical = canonical_tag(value)
                    if canonical and canonical not in seen:
                        seen.add(canonical)
                        tags.append(canonical)
                        display.append(" ".join(value.split()))
                documents.append(
                    Document(
                        path=path.resolve(),
                        doc_id=str(data.get("message_id") or path.stem),
                        source=str(data.get("source", "unknown")),
                        doc_type=str(data.get("type", "unknown")),
                        sent_at=str(data.get("sent_at", "")),
                        tags=tuple(tags),
                        display_tags=tuple(display),
                        excerpt=_excerpt(data),
                        fingerprint=file_fingerprint(raw),
                        mtime_ns=path.stat().st_mtime_ns,
                    )
                )
            except (ValueError, json.JSONDecodeError, OSError) as exc:
                errors.append(f"{path}: {exc}")
    return documents, errors


def load_state(state_path: Path) -> dict:
    try:
        state = json.loads(Path(state_path).read_bytes())
        if isinstance(state, dict) and state.get("version") == STATE_VERSION:
            return state
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": STATE_VERSION, "files": {}}


def save_state(state_path: Path, documents: Iterable[Document]) -> None:
    files = {
        str(doc.path): {"fingerprint": doc.fingerprint, "mtime_ns": doc.mtime_ns}
        for doc in documents
    }
    payload = {"version": STATE_VERSION, "files": files}
    target = Path(state_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True))


def select_batch(documents: Iterable[Document], state: dict, force_all: bool = False) -> list[BatchItem]:
    """Return the documents that were created or changed since the last run."""
    known = state.get("files", {}) if isinstance(state, dict) else {}
    batch: list[BatchItem] = []
    for doc in documents:
        record = known.get(str(doc.path))
        if force_all:
            batch.append(BatchItem(doc, "forced"))
        elif record is None:
            batch.append(BatchItem(doc, "new"))
        elif record.get("fingerprint") != doc.fingerprint:
            batch.append(BatchItem(doc, "edited"))
        elif int(record.get("mtime_ns", -1)) != doc.mtime_ns:
            batch.append(BatchItem(doc, "touched"))
    return batch
