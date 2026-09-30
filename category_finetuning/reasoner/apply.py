"""Apply tag corrections and keep an audit trail of the old tags.

Only confident corrections are written back: the document must have a
suggestion that brings it under epsilon (``fits``). The notification file
always holds just the most recently replaced old tag, one word:

    electricity

Drifts without a fitting replacement are left untouched (they still appear in
the report, but the notification file is not changed).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

from .drift import Evaluation
from .loader import canonical_tag


def _replace_tag(path: Path, old_canonical: str, new_tag: str) -> bool:
    data = json.loads(path.read_bytes())
    tags = data.get("client_tags")
    if not isinstance(tags, list):
        return False
    replaced = False
    updated = []
    for value in tags:
        if isinstance(value, str) and canonical_tag(value) == old_canonical:
            updated.append(new_tag)
            replaced = True
        else:
            updated.append(value)
    if not replaced:
        return False
    data["client_tags"] = updated
    path.write_text(json.dumps(data, indent=2) + "\n")
    return True


def _display_tag(evaluation: Evaluation) -> str:
    document = evaluation.document
    try:
        return document.display_tags[document.tags.index(evaluation.drifting_tag)]
    except (ValueError, IndexError):
        return evaluation.drifting_tag or ""


def _write_last(target: Path, line: str) -> None:
    """Keep only the most recent old tag in the notification file."""
    if target.is_dir():
        target = target / "tag_updates.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(line + "\n", encoding="utf-8")


def apply_updates(evaluations: Sequence[Evaluation], notif_path: Path) -> tuple[list[str], list[str]]:
    """Replace each drifting tag with the best fitting suggestion.

    Returns (applied file paths, errors) and writes the last replaced old tag
    to ``notif_path`` (the file holds only that single word).
    """
    applied: list[str] = []
    errors: list[str] = []
    lines: list[str] = []

    for evaluation in evaluations:
        if not evaluation.drifting_tag or not evaluation.suggestions:
            continue
        # Only act when a tag actually sticks out: a drift verdict or a
        # single-tag conflict. Merely ambiguous documents are left alone.
        if not (evaluation.drifting or evaluation.tag_conflict):
            continue
        replacement = next((item for item in evaluation.suggestions if item.fits), None)
        if replacement is None:
            continue  # left for review; nothing to notify
        try:
            if _replace_tag(evaluation.document.path, evaluation.drifting_tag, replacement.tag):
                applied.append(str(evaluation.document.path))
                lines.append(_display_tag(evaluation))
            else:
                errors.append(f"{evaluation.document.path}: tag not found in client_tags")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{evaluation.document.path}: {exc}")

    if lines:
        try:
            _write_last(Path(notif_path), lines[-1])
        except OSError as exc:
            errors.append(f"{notif_path}: {exc}")
    return applied, errors
