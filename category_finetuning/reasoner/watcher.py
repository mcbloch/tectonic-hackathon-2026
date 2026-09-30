"""Poll-based directory watcher for newly created or edited documents.

Keeps the process alive and runs the reasoning pipeline whenever a JSON file
appears, changes or disappears. Polling with the standard library is
deliberately chosen over platform-specific inotify bindings: no extra
dependency, same behaviour everywhere, and the processed-files checkpoint
already makes each run idempotent. After a change the watcher waits for a
quiet period ("debounce") so a document that is still being written is only
processed once it is complete.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Iterable

Snapshot = dict[str, tuple[int, int]]  # resolved path -> (mtime_ns, size)


def snapshot(directories: Iterable[Path]) -> Snapshot:
    """Map every JSON file below the directories to its mtime and size."""
    files: Snapshot = {}
    for directory in directories:
        root = Path(directory)
        if not root.exists():
            continue
        for path in root.rglob("*.json"):
            try:
                stat = path.stat()
            except OSError:
                continue
            files[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
    return files


def diff_snapshots(before: Snapshot, after: Snapshot) -> tuple[list[str], list[str], list[str]]:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(path for path in set(before) & set(after) if before[path] != after[path])
    return added, modified, removed


def watch(
    directories: Iterable[Path],
    runner: Callable[[], int],
    *,
    interval: float = 2.0,
    debounce: float = 1.0,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run `runner` once, then again whenever the watched directories change."""
    directories = [Path(directory) for directory in directories]
    out(
        f"watching {', '.join(str(directory) for directory in directories)} "
        f"(poll {interval}s, debounce {debounce}s; Ctrl+C to stop)"
    )
    try:
        runner()
        current = snapshot(directories)
        while True:
            sleep(interval)
            fresh = snapshot(directories)
            if fresh == current:
                continue
            # Wait until writers are done before evaluating.
            while True:
                sleep(debounce)
                settled = snapshot(directories)
                if settled == fresh:
                    break
                fresh = settled
            added, modified, removed = diff_snapshots(current, fresh)
            current = fresh
            out(
                f"[{time.strftime('%H:%M:%S')}] {len(added)} new, {len(modified)} modified, "
                f"{len(removed)} removed - running"
            )
            try:
                runner()
            except Exception as exc:  # one bad run must not stop the watcher
                out(f"run failed: {exc}")
            # Absorb any writes the run itself made (e.g. applied tag updates)
            # so the watcher does not react to its own changes.
            current = snapshot(directories)
    except KeyboardInterrupt:
        out("watcher stopped")
        return 0
