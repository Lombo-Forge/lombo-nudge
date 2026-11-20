"""Retry one stuck request: look at Sonarr/Radarr, ask core.plan_retry() what to do, and (only when
asked to apply) do it. Removing a download blocklists it, so this is the one place that changes things."""

from dataclasses import dataclass
from typing import Any

from . import core
from .clients import Arr, Seerr
from .http import ApiError
from .scan import is_bad, request_kind

CLIENT_DOWN = {"DownloadClientStatusCheck", "DownloadClientCheck"}
MAX_SEASON_SEARCHES = 6


class RetryRefused(Exception):
    """The request cannot be retried (not found, not stuck, or its app is not set up)."""


@dataclass(frozen=True)
class Prepared:
    plan: core.RetryPlan
    app: Arr
    item_id: int | None
    seasons: list[int]


def prepare(
    seerr: Seerr,
    arrs: dict[str, Arr],
    state: dict[str, Any],
    request_id: int,
    now: float,
    rules: core.Rules,
    cooldown_s: float,
) -> Prepared:
    try:
        req = seerr.request_by_id(request_id)
    except ApiError as e:
        raise RetryRefused(f"request {request_id}: {e}") from e
    media = req.get("media") or {}
    if not req or req.get("status") != core.REQUEST_APPROVED or media.get("status") == core.MEDIA_AVAILABLE:
        raise RetryRefused(f"request {request_id} is not an approved request that is still waiting")
    kind = request_kind(req)
    arr = arrs.get(kind)
    if arr is None or req.get("is4k"):
        raise RetryRefused(f"{'Sonarr' if kind == 'tv' else 'Radarr'} is not set up for request {request_id}")

    last = state["retry"].get(str(request_id), 0)
    left = cooldown_s - (now - last) if isinstance(last, int | float) else 0
    health = arr.health()
    found = arr.lookup(media.get("tvdbId"), media.get("tmdbId"))
    item = found[0] if found else None
    queue: list[core.RetryItem] = []
    if item:
        idkey = "seriesId" if kind == "tv" else "movieId"
        for rec in arr.queue():
            if rec.get(idkey) != item.get("id"):
                continue
            prev = state["queue"].get(f"{kind}:{rec.get('id')}")
            stalled = bool(prev and (now - prev[1]) / 3600 > rules.stall_after_h)
            queue.append(
                core.RetryItem(
                    int(rec["id"]), str(rec.get("title") or ""), rec.get("sizeleft") or 0, is_bad(rec), stalled
                )
            )
    seasons = [int(s["seasonNumber"]) for s in req.get("seasons") or [] if s.get("seasonNumber") is not None]
    plan = core.plan_retry(
        app=arr.name,
        in_arr=item is not None,
        cooldown_left_s=left,
        client_down=any(h.get("source") in CLIENT_DOWN for h in health),
        queue=queue,
        seasons=seasons,
        max_seasons=MAX_SEASON_SEARCHES,
    )
    return Prepared(plan, arr, item.get("id") if item else None, sorted(set(seasons))[:MAX_SEASON_SEARCHES])


def apply(p: Prepared) -> bool:
    """Carry the plan out. True if something was changed or asked for."""
    if p.plan.action == "remove":
        p.app.delete_queue(list(p.plan.ids))
        return True
    if p.plan.action == "search":
        cmds: list[dict[str, Any]]
        if p.app.kind == "tv":
            cmds = [{"name": "SeasonSearch", "seriesId": p.item_id, "seasonNumber": n} for n in p.seasons] or [
                {"name": "SeriesSearch", "seriesId": p.item_id}
            ]
        else:
            cmds = [{"name": "MoviesSearch", "movieIds": [p.item_id]}]
        for cmd in cmds:
            p.app.command(cmd)
        return True
    return False
