"""The findings document: what `plan --json`, `findings.json` and `GET /v1/findings` all say (docs/contracts.md).

Pure except `write()`. Only stuck requests are listed. `summary` is one plain sentence for a person; `reason` is
the app's own code for tools that want to map it themselves.
"""

import contextlib
import json
import os
from pathlib import Path
from typing import Any

from .scan import Finding

VERSION = 1
FILE_NAME = "findings.json"
MAX_TEXT = 200  # characters kept of a title or a name

# reason code -> one plain sentence (160 characters at most, no app names). A test fails when a code has none.
SUMMARIES: dict[str, str] = {
    "not_approved": "The request is still waiting for approval, or it was declined.",
    "available": "This is already available to watch.",
    "ignored": "This title was blocked or removed on purpose.",
    "not_checked": "This request cannot be checked automatically.",
    "too_new": "This was approved recently and is still being looked for.",
    "downloading": "A download is in progress and moving along.",
    "waiting_release": "This has not been released yet, so there is nothing to download.",
    "no_episode_list": "No episode list exists for this show yet, so nothing can be searched.",
    "not_found": "No copy has been found yet. It may need a different source or a manual search.",
    "missing_in_arr": "The request was approved, but the download side has no record of this title.",
    "download_failed": "A download for this failed. Trying again looks for another copy.",
    "download_stalled": "A download for this has stopped moving. Trying again looks for another copy.",
    "quality_blocked": "A copy was downloaded but refused because it is not better than what is already there.",
    "import_stuck": "A copy finished downloading but could not be added to the library. It needs a manual look.",
    "partly_here": "Some episodes are here, but the rest have not arrived for several days.",
    "not_in_jellyfin": "The files are ready, but the library does not show them yet. A library scan may fix it.",
    "seerr_out_of_sync": "It is in the library, but the request page still shows it as waiting.",
    "seerr_not_updated": "The files are ready, but the request page still shows it as waiting.",
}

# reason code -> a coarse group a page can colour or sort by (only stuck codes appear in the document)
STATES: dict[str, str] = {
    "not_found": "searching",
    "missing_in_arr": "missing",
    "download_failed": "stalled",
    "download_stalled": "stalled",
    "quality_blocked": "blocked",
    "import_stuck": "blocked",
    "partly_here": "partly",
    "not_in_jellyfin": "sync",
    "seerr_out_of_sync": "sync",
    "seerr_not_updated": "sync",
}

# A retry can do something for these (look again, or remove a bad download); for the rest it only explains.
RETRYABLE = {"not_found", "download_failed", "download_stalled"}


def clean(text: str, limit: int = MAX_TEXT) -> str:
    """Control characters (and other unprintable ones) become '?', and the text is cut to `limit`."""
    return "".join(c if c.isprintable() else "?" for c in text)[:limit]


def build(findings: list[Finding], complete: bool, alerts: dict[str, Any], now: float) -> dict[str, Any]:
    """The document. `alerts` is the state file's "alerts" (request id -> [code, time])."""
    rows: list[dict[str, Any]] = []
    for f in findings:
        if not f.verdict.stuck:
            continue
        sent = alerts.get(str(f.request_id))
        rows.append(
            {
                "request_id": f.request_id,
                "title": clean(f.title),
                "requester": clean(f.requester),
                "kind": f.kind,
                "state": STATES[f.verdict.code],
                "reason": f.verdict.code,
                "summary": SUMMARIES[f.verdict.code],
                "days": f.days,
                "can_retry": f.verdict.code in RETRYABLE,
                "alerted": isinstance(sent, list) and sent[:1] == [f.verdict.code],
            }
        )
    rows.sort(key=lambda r: (-r["days"], r["request_id"]))
    return {"version": VERSION, "updated": int(now), "complete": complete, "findings": rows}


def path_for(state_file: str) -> str:
    return str(Path(state_file).parent / FILE_NAME)


def write(path: str, doc: dict[str, Any]) -> None:
    """Atomic: a temporary file, then a rename, so a reader sees the old file or the new one. Mode 0644: it holds
    titles only and other programs read it. Raises OSError."""
    tmp = path + ".tmp"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o644)
            f.write(json.dumps(doc))
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
