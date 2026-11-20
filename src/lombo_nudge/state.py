"""The small JSON file that remembers what the program saw last time.

It holds three things: when each queue item last made progress (to spot stalls), when each request's
file count last changed (to spot "partly here"), and which alert was sent for which request.
A missing or damaged file just means "start fresh".
"""

import json
import os
from pathlib import Path
from typing import Any


class StateError(Exception):
    """The state file could not be written."""


def empty() -> dict[str, Any]:
    return {"queue": {}, "sig": {}, "alerts": {}, "retry": {}}


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
