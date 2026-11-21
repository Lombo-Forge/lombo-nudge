"""The decisions. Pure functions and plain data: no network, disk or clock.

`classify()` gives one approved request a reason code. `plan_retry()` says what a retry would do.
Everything it needs (ages, queue state, file counts) is passed in by scan.py, which does the I/O.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# Seerr request status 2 = approved. Seerr media status 5 = available, 6 = blocklisted, 7 = deleted.
REQUEST_APPROVED = 2
MEDIA_AVAILABLE = 5
MEDIA_IGNORED = (6, 7)

# code -> (stuck, plain-language meaning). docs/how-it-works.md explains each one; a test keeps them in sync.
REASONS: dict[str, tuple[bool, str]] = {
    "not_approved": (False, "The request is pending or declined, so there is nothing to deliver yet."),
    "available": (False, "Seerr says it is available."),
    "ignored": (False, "Seerr has it blocklisted or deleted."),
    "not_checked": (False, "The Sonarr or Radarr this request needs is not configured, or it is a 4K request."),
    "too_new": (False, "Approved recently; it gets a day or more before anyone is told."),
    "downloading": (False, "A download is in the queue and moving."),
    "waiting_release": (False, "The movie has not been released yet, so nothing can be found."),
    "no_episode_list": (False, "Sonarr has no episode list for it yet (common with new anime)."),
    "not_found": (True, "Approved a while ago, nothing queued and no files: the indexers found nothing."),
    "missing_in_arr": (True, "Approved, but Sonarr or Radarr does not have the title at all."),
    "download_failed": (True, "A queued download failed or is flagged with a warning or error."),
    "download_stalled": (True, "A queued download has made no progress for hours."),
    "quality_blocked": (True, "A finished download was refused because of quality (for example not an upgrade)."),
    "import_stuck": (True, "A download finished but was never imported."),
    "partly_here": (True, "Some episodes are here, the rest are missing and nothing has changed for days."),
    "not_in_jellyfin": (True, "Imported by Sonarr or Radarr, but Jellyfin does not list it."),
    "seerr_out_of_sync": (
        True,
        "The files are in Jellyfin, but Seerr does not show the request as available yet "
        "(it may say processing or partly available).",
    ),
    "seerr_not_updated": (
        True,
        "The files are imported but Seerr does not show it as available yet "
        "(it may say processing or partly available).",
    ),
}


@dataclass(frozen=True)
class Rules:
    """Thresholds in hours. Config builds this from the LOMBO_NUDGE_* settings."""

    search_after_h: float = 24
    partly_after_h: float = 72
    stall_after_h: float = 12


@dataclass(frozen=True)
class Queued:
    """One Sonarr/Radarr queue entry that belongs to the request's title."""

    id: int
    title: str = ""
    bad: bool = False  # failed, warning, error or a blocked/failed import
    finished: bool = False  # nothing left to download: waiting to be imported
    quality_blocked: bool = False
    stalled_h: float = 0.0


@dataclass(frozen=True)
class Facts:
    request_status: int | None
    media_status: int | None
    kind: str  # "movie" or "tv"
    age_h: float
    idle_h: float  # hours the file count has been unchanged
    in_arr: bool
    released: bool = True
    files: int = 0
    wanted: int = 1
    queue: tuple[Queued, ...] = ()
    in_jellyfin: bool | None = None  # None = not checked


@dataclass(frozen=True)
class Verdict:
    code: str
    message: str

    @property
    def stuck(self) -> bool:
        return REASONS[self.code][0]


def _v(code: str, message: str | None = None) -> Verdict:
    return Verdict(code, message or REASONS[code][1])


def classify(f: Facts, rules: Rules = Rules()) -> Verdict:
    if f.request_status != REQUEST_APPROVED:
        return _v("not_approved")
    if f.media_status == MEDIA_AVAILABLE:
        return _v("available")
    if f.media_status in MEDIA_IGNORED:
        return _v("ignored")
    old = f.age_h > rules.search_after_h
    if not f.in_arr:
        return _v("missing_in_arr", "Sonarr/Radarr does not have this title.") if old else _v("too_new")

    finished_bad = [q for q in f.queue if q.bad and q.finished]
    if any(q.quality_blocked for q in finished_bad):
        return _v("quality_blocked")
    if finished_bad:
        return _v("import_stuck")
    if any(q.bad for q in f.queue):
        return _v("download_failed")
    if any(q.stalled_h > rules.stall_after_h for q in f.queue):
        return _v("download_stalled")
    if f.queue:
        return _v("downloading")
    if not f.released:
        return _v("waiting_release")

    wanted = 1 if f.kind == "movie" else f.wanted
    if wanted <= 0:
        return _v("no_episode_list")
    if 0 < f.files < wanted:
        if f.age_h > rules.partly_after_h and f.idle_h > rules.partly_after_h:
            return _v("partly_here", f"Partly here: {f.files} of {wanted} episodes, unchanged for days.")
        return _v("too_new")
    if f.files == 0:
        return _v("not_found") if old else _v("too_new")
    # All wanted files are in Sonarr/Radarr but Seerr does not say available.
    if not old:
        return _v("too_new")
    if f.in_jellyfin is False:
        return _v("not_in_jellyfin")
    if f.in_jellyfin is True:
        return _v("seerr_out_of_sync")
    return _v("seerr_not_updated")


def parse_ts(value: object) -> float | None:
    """ISO 8601 text to a Unix timestamp; naive times count as UTC. None when it cannot be read."""
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).timestamp()


def track(prev: dict[str, list[Any]], values: dict[str, Any], now: float) -> dict[str, list[Any]]:
    """Remember when each value last changed. prev/result: key -> [value, since]. Keys that vanished are dropped."""
    out: dict[str, list[Any]] = {}
    for key, value in values.items():
        old = prev.get(key)
        out[key] = [value, old[1] if old and old[0] == value else now]
    return out


def should_alert(prev: list[object] | None, code: str, now: float, realert_s: float) -> bool:
    """One alert per change of reason, and again after realert_s while still stuck. prev = [code, time]."""
    if not REASONS[code][0]:
        return False
    if not prev or prev[0] != code:
        return True
    at = prev[1]
    return isinstance(at, int | float) and now - at > realert_s


def alert_text(title: str, requester: str, verdict: Verdict, days: int) -> str:
    plural = "" if days == 1 else "s"
    return (
        f"Stuck request: {title} for {requester}. {verdict.message} "
        f"[{verdict.code}] Waiting {days} day{plural}. Look at it in Sonarr/Radarr."
    )


# ---------------------------------------------------------------- retry
@dataclass(frozen=True)
class RetryItem:
    id: int
    title: str = ""
    sizeleft: float = 0
    bad: bool = False
    stalled: bool = False


@dataclass(frozen=True)
class RetryPlan:
    """action: remove | search | manual | moving | missing | client_down | cooldown."""

    action: str
    message: str
    ids: tuple[int, ...] = ()
    releases: tuple[str, ...] = ()


def plan_retry(
    *,
    app: str,
    in_arr: bool,
    cooldown_left_s: float,
    client_down: bool,
    queue: list[RetryItem],
    seasons: list[int],
    max_seasons: int = 6,
) -> RetryPlan:
    if cooldown_left_s > 0:
        mins = max(1, int(cooldown_left_s // 60) + 1)
        return RetryPlan("cooldown", f"Retried a moment ago. Give it about {mins} minutes before trying again.")
    if client_down:
        return RetryPlan(
            "client_down",
            f"{app} says the download client is not answering. Try again once it is back: "
            "changing things now could throw away downloads that are fine.",
        )
    if not in_arr:
        return RetryPlan("missing", f"{app} does not have this title any more.")
    remove = [q for q in queue if q.sizeleft > 0 and (q.bad or q.stalled)]
    if remove:
        n = len(remove)
        return RetryPlan(
            "remove",
            f"Remove {n} stalled download{'s' if n != 1 else ''}, blocklist "
            f"{'them' if n != 1 else 'it'} and search for another copy.",
            tuple(q.id for q in remove),
            tuple(sorted({q.title or "?" for q in remove})),
        )
    if any(q.sizeleft <= 0 for q in queue):
        return RetryPlan(
            "manual", f"The download finished but did not import. Open it in {app}: Activity, Queue, Manual Import."
        )
    if queue:
        return RetryPlan("moving", "It is downloading and moving; nothing to retry yet.")
    picked = sorted(set(seasons))[:max_seasons]
    where = f" (season{'s' if len(picked) != 1 else ''} {', '.join(map(str, picked))})" if picked else ""
    return RetryPlan("search", f"Nothing is downloading. Ask {app} to search again{where}.")
