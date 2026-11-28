"""Fallback tag: when a requested title has had nothing queued for a while, add a tag to it in Sonarr/Radarr and
search once. An indexer that carries the same tag is only searched for tagged titles, so it acts as a second
choice. The tag is taken off again when nothing is missing. Downloads are never removed."""

from typing import Any

from . import core
from .clients import Arr, Seerr
from .http import ApiError
from .retry import MAX_SEASON_SEARCHES
from .scan import _counts, arr_key, media_status, request_kind

PER_DAY = 10  # a broken first-choice indexer must not send the whole request list to the second choice


def _missing(seerr: Seerr, arrs: dict[str, Arr]) -> tuple[dict[str, dict[str, Any]], set[str], bool]:
    """(idle, missing, complete). missing: "app:item id" of every monitored title a request still waits for.
    idle: those with nothing in the queue, with what a search needs. complete: the request list was read whole."""
    requests, complete = seerr.approved_requests()
    items: dict[str, dict[Any, dict[str, Any]]] = {}
    queued: set[str] = set()
    for app, arr in arrs.items():
        key = "tvdbId" if app == "tv" else "tmdbId"
        items[app] = {i[key]: i for i in arr.items() if i.get(key)}
        idkey = "seriesId" if app == "tv" else "movieId"
        queued |= {f"{app}:{r.get(idkey)}" for r in arr.queue()}
    idle: dict[str, dict[str, Any]] = {}
    missing: set[str] = set()
    for req in requests:
        kind = request_kind(req)
        app = arr_key(kind, req)
        status = media_status(req)
        if app not in arrs or status == core.MEDIA_AVAILABLE or status in core.MEDIA_IGNORED:
            continue
        media = req.get("media") or {}
        item = items[app].get(media.get("tvdbId") if kind == "tv" else media.get("tmdbId"))
        if not item or not item.get("monitored", True):
            continue
        files, wanted, released = _counts(kind, item, req)
        if not released or files >= wanted:
            continue
        key = f"{app}:{item['id']}"
        missing.add(key)
        if key not in queued:
            entry = idle.setdefault(
                key, {"app": app, "id": item["id"], "title": item.get("title") or key, "tags": item.get("tags") or []}
            )
            seasons = {int(s["seasonNumber"]) for s in req.get("seasons") or [] if s.get("seasonNumber") is not None}
            entry["seasons"] = sorted(seasons | set(entry.get("seasons", ())))
    return idle, missing, complete


def _search(arr: Arr, item_id: int, seasons: list[int]) -> None:
    if arr.kind != "tv":
        arr.command({"name": "MoviesSearch", "movieIds": [item_id]})
    elif seasons:
        for n in seasons[:MAX_SEASON_SEARCHES]:
            arr.command({"name": "SeasonSearch", "seriesId": item_id, "seasonNumber": n})
    else:
        arr.command({"name": "SeriesSearch", "seriesId": item_id})


def run(
    seerr: Seerr,
    arrs: dict[str, Arr],
    state: dict[str, Any],
    now: float,
    tag: str,
    after_s: float,
    dry_run: bool,
    blocked: str,
) -> list[str]:
    """One pass. Updates state["fallback"] (idle since), state["tagged"] (tags this program added) and
    state["escalated"] (for the daily cap). `blocked`: why searching cannot be trusted now, or "".
    Returns what it did, or in a dry run would do, as lines."""
    idle, missing, complete = _missing(seerr, arrs)
    since = {k: state["fallback"].get(k, now) for k in idle}
    state["fallback"] = since if complete else {**state["fallback"], **since}
    recent = {k: t for k, t in state["escalated"].items() if now - t < 86400}
    lines: list[str] = []
    ids: dict[str, int | None] = {}

    def tag_id(app: str) -> int | None:
        if app not in ids:
            ids[app] = arrs[app].tag_id(tag)
            if ids[app] is None:
                lines.append(f"[warn] {arrs[app].name} has no tag '{tag}': no fallback there")
        return ids[app]

    # Take our tag off titles nothing waits for any more. A cut-off request list proves nothing, so keep them.
    for key in list(state["tagged"]):
        app, _, item = key.partition(":")
        if not complete or key in missing or app not in arrs:
            continue
        found = tag_id(app)
        if found is not None:
            lines.append(f"{'would take' if dry_run else 'took'} tag '{tag}' off {key}: nothing is missing")
            if not dry_run:
                try:
                    arrs[app].set_tag(int(item), found, add=False)
                except ApiError:
                    # Sonarr and Radarr answer 500 for a title that is gone. Then there is no tag left to remove.
                    if any(i.get("id") == int(item) for i in arrs[app].items()):
                        raise
                del state["tagged"][key]

    due = [k for k in idle if now - since[k] >= after_s and k not in state["tagged"]]
    if due and blocked:
        lines.append(f"[warn] no fallback while searching is broken. {blocked}")
        due = []
    for key in sorted(due, key=lambda k: since[k]):
        if len(recent) >= PER_DAY:
            break
        e = idle[key]
        found = tag_id(e["app"])
        if found is None or found in e["tags"]:  # tagged by hand: leave it, and never take it off
            continue
        lines.append(f"{'would tag' if dry_run else 'tagged'} {e['title']} '{tag}' and search")
        if not dry_run:
            arr = arrs[e["app"]]
            arr.set_tag(e["id"], found, add=True)
            state["tagged"][key] = now  # before the search: a failed search must not orphan the tag
            recent[key] = now
            _search(arr, e["id"], e["seasons"])
    state["escalated"] = recent
    return lines
