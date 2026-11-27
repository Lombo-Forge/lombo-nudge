"""Reads Seerr, Sonarr, Radarr (and Jellyfin if set) and turns every approved request into a Finding.

Read-only. If Seerr, Sonarr or Radarr cannot be read, the ApiError propagates and the whole pass is
skipped, so an outage never turns into a pile of false alarms.
"""

from dataclasses import dataclass, replace
from typing import Any

from . import core
from .clients import Arr, Seerr
from .jellyfin import Jellyfin

BAD_STATES = {"importblocked", "importfailed", "failed", "failedpending"}


@dataclass(frozen=True)
class Finding:
    request_id: int
    title: str
    requester: str
    kind: str
    verdict: core.Verdict
    days: int


@dataclass(frozen=True)
class Scan:
    findings: list[Finding]
    queue: dict[str, list[Any]]  # progress memory to store: "tv:12" -> [sizeleft, since]
    sig: dict[str, list[Any]]  # file-count memory to store: "request id" -> ["files/wanted", since]
    complete: bool  # False when the request listing was cut short


def request_kind(req: dict[str, Any]) -> str:
    return "tv" if ((req.get("media") or {}).get("mediaType") or req.get("type")) == "tv" else "movie"


def arr_key(kind: str, req: dict[str, Any]) -> str:
    """Which app a request goes to: "tv", "movie" or "movie4k". 4K shows ("tv4k") have none."""
    return f"{kind}4k" if req.get("is4k") else kind


def media_status(req: dict[str, Any]) -> Any:
    """Seerr keeps a separate status for the 4K copy of a title."""
    return (req.get("media") or {}).get("status4k" if req.get("is4k") else "status")


def is_bad(rec: dict[str, Any]) -> bool:
    return (
        str(rec.get("trackedDownloadStatus") or "").lower() in ("warning", "error")
        or str(rec.get("trackedDownloadState") or "").lower() in BAD_STATES
        or str(rec.get("status") or "").lower() in ("failed", "warning")
    )


def is_quality_blocked(rec: dict[str, Any]) -> bool:
    text = " ".join(
        " ".join([str(m.get("title") or ""), *map(str, m.get("messages") or [])])
        for m in rec.get("statusMessages") or []
    ).lower()
    return "upgrade" in text or "quality" in text


def _queue_for(
    app: str, item_id: Any, records: list[dict[str, Any]], seen: dict[str, list[Any]], now: float
) -> tuple[core.Queued, ...]:
    idkey = "seriesId" if app == "tv" else "movieId"
    out = []
    for rec in records:
        if rec.get(idkey) != item_id:
            continue
        left = rec.get("sizeleft") or 0
        since = seen.get(f"{app}:{rec.get('id')}", [left, now])[1]
        out.append(
            core.Queued(
                id=int(rec.get("id") or 0),
                title=str(rec.get("title") or ""),
                bad=is_bad(rec),
                finished=not left,
                quality_blocked=is_quality_blocked(rec),
                stalled_h=(now - since) / 3600 if left else 0.0,
            )
        )
    return tuple(out)


def _counts(kind: str, item: dict[str, Any], req: dict[str, Any]) -> tuple[int, int, bool]:
    """(files, wanted, released) for the title in Sonarr/Radarr."""
    if kind == "tv":
        wanted_seasons = {s.get("seasonNumber") for s in req.get("seasons") or []}
        parts = [
            s.get("statistics") or {}
            for s in item.get("seasons") or []
            if not wanted_seasons or s.get("seasonNumber") in wanted_seasons
        ]
        return (
            sum(p.get("episodeFileCount") or 0 for p in parts),
            sum(p.get("episodeCount") or 0 for p in parts),
            True,
        )
    return (1 if item.get("hasFile") else 0), 1, bool(item.get("isAvailable", True))


def scan(
    seerr: Seerr,
    arrs: dict[str, Arr],
    jellyfin: Jellyfin | None,
    state: dict[str, Any],
    now: float,
    rules: core.Rules,
) -> Scan:
    requests, complete = seerr.approved_requests()
    items: dict[str, dict[Any, dict[str, Any]]] = {}
    queues: dict[str, list[dict[str, Any]]] = {}
    for app, arr in arrs.items():
        key = "tvdbId" if app == "tv" else "tmdbId"
        items[app] = {i[key]: i for i in arr.items() if i.get(key)}
        queues[app] = arr.queue()

    seen = core.track(
        state["queue"],
        {f"{k}:{r.get('id')}": r.get("sizeleft") or 0 for k, recs in queues.items() for r in recs},
        now,
    )
    sigs: dict[str, list[Any]] = {}
    findings: list[Finding] = []
    for req in requests:
        rid = req.get("id")
        if rid is None:
            continue
        kind = request_kind(req)
        media = req.get("media") or {}
        # Seerr has no separate "approved at" time. updatedAt is when the request was last changed, which is
        # normally the approval; a later edit restarts the wait (documented in docs/how-it-works.md).
        approved = core.parse_ts(req.get("updatedAt")) or core.parse_ts(req.get("createdAt")) or now
        age_h = (now - approved) / 3600
        who = str((req.get("requestedBy") or {}).get("displayName") or "Someone")
        app = arr_key(kind, req)
        if app not in arrs:
            skipped = core.Verdict("not_checked", core.REASONS["not_checked"][1])
            findings.append(Finding(rid, f"{kind} #{rid}", who, kind, skipped, int(age_h // 24)))
            continue
        if kind == "tv" and not media.get("tvdbId"):
            # Without a TVDB id the match is a guess: say so instead of reporting "missing in Sonarr".
            msg = "Seerr has no TVDB id for this show, so it cannot be matched in Sonarr."
            findings.append(
                Finding(rid, f"{kind} #{rid}", who, kind, core.Verdict("not_checked", msg), int(age_h // 24))
            )
            continue
        item = items[app].get(media.get("tvdbId") if kind == "tv" else media.get("tmdbId"))
        files, wanted, released = _counts(kind, item, req) if item else (0, 1, True)
        queue = _queue_for(app, item.get("id"), queues[app], seen, now) if item else ()
        sig = {str(rid): f"{files}/{wanted}"}
        sigs.update(core.track({str(rid): state["sig"].get(str(rid), [])}, sig, now))
        facts = core.Facts(
            request_status=req.get("status"),
            media_status=media_status(req),
            kind=kind,
            age_h=age_h,
            idle_h=(now - sigs[str(rid)][1]) / 3600,
            in_arr=item is not None,
            released=released,
            files=files,
            wanted=wanted,
            queue=queue,
        )
        verdict = core.classify(facts, rules)
        if verdict.code == "seerr_not_updated" and jellyfin and not req.get("is4k"):
            found = jellyfin.has_title(kind, media.get("tmdbId"), media.get("tvdbId"))
            facts = replace(facts, in_jellyfin=found)
            verdict = core.classify(facts, rules)
        title = seerr.title(kind, media.get("tmdbId")) if verdict.stuck else f"{kind} #{rid}"
        findings.append(Finding(rid, title, who, kind, verdict, int(age_h // 24)))
    return Scan(findings, seen, sigs, complete)
