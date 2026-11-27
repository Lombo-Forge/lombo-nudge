"""The small JSON file that remembers what the program saw last time.

It holds: when each queue item last made progress (to spot stalls), when each request's
file count last changed (to spot "partly here"), which alert was sent for which request, and since when a
request has had nothing arriving (and which were declined for it).
A missing or damaged file just means "start fresh".
"""

import fcntl
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class StateError(Exception):
    """The state file could not be written."""


def empty() -> dict[str, Any]:
    return {"queue": {}, "sig": {}, "alerts": {}, "retry": {}, "stuck": {}, "declined": {}}


def load(path: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return empty()
    if not isinstance(data, dict):
        return empty()
    return {**empty(), **{k: v for k, v in data.items() if isinstance(v, dict)}}


def save(path: str, state: dict[str, Any]) -> None:
    tmp = path + ".tmp"
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(tmp).write_text(json.dumps(state))
        os.replace(tmp, path)
    except OSError as e:
        raise StateError(
            f"cannot write {path}: {e}. Mount a writable folder at /data or change LOMBO_NUDGE_STATE_FILE."
        ) from e


@contextmanager
def locked(path: str, wait: float = 60.0) -> Iterator[None]:
    """Hold an advisory lock (a file next to the state file) so `run` and `retry` never rewrite the state at the
    same time. It goes away with the process, so a crash never leaves it stuck. Waits up to `wait` seconds."""
    lock = os.path.join(os.path.dirname(path) or ".", "nudge.lock")
    try:
        os.makedirs(os.path.dirname(lock) or ".", exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        raise StateError(f"cannot take the lock {lock}: {e}. Mount a writable folder at /data.") from e
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StateError("another run or retry is still working; try again in a minute") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)
