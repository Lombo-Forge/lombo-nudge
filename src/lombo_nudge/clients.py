"""Seerr, Sonarr and Radarr clients. Read-only except Arr.delete_queue() (retry), Arr.command() (retry and
fallback), Arr.set_tag() (fallback) and Seerr.decline() (DECLINE_AFTER_DAYS), all only when DRY_RUN=false."""

from typing import Any, cast

from .http import ApiError, Client

PAGE = 100
MAX_REQUESTS = 5000  # hard safety cap on the request listing
MAX_QUEUE_PAGES = 50


class Seerr(Client):
    def __init__(self, base_url: str, api_key: str, timeout: float = 15):
        super().__init__("Seerr", base_url, {"X-Api-Key": api_key}, timeout)
        self._titles: dict[tuple[str, int], str] = {}

    def me(self) -> dict[str, Any]:
        """Needs a valid key."""
        return cast(dict[str, Any], self.get("/api/v1/auth/me"))

    def status(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.get("/api/v1/status", auth=False))

    def request_by_id(self, request_id: int) -> dict[str, Any]:
        return cast(dict[str, Any], self.get(f"/api/v1/request/{request_id}") or {})

    def approved_requests(self) -> tuple[list[dict[str, Any]], bool]:
        """Every approved request, paged until a short page. (requests, complete): complete is False when a
        later page failed or the safety cap cut the listing, so callers do not forget anything on a partial read."""
        out: list[dict[str, Any]] = []
        for page in range(MAX_REQUESTS // PAGE):
            try:
                data = self.get("/api/v1/request", filter="approved", take=PAGE, skip=page * PAGE, sort="added")
            except ApiError:
                if page == 0:
                    raise  # Seerr down: skip the whole pass
                return out, False
            got = (data or {}).get("results", [])
            out += got
            if len(got) < PAGE:
                return out, True
        return out, False

    def decline(self, request_id: int) -> None:
        """Deletes the request: Seerr only declines pending ones (409), and these are approved."""
        self.request("DELETE", f"/api/v1/request/{request_id}")

    def title(self, kind: str, tmdb_id: int | None) -> str:
        if tmdb_id is None:
            return f"a {kind}"
        key = (kind, tmdb_id)
        if key not in self._titles:
            try:
                d = self.get(f"/api/v1/{'tv' if kind == 'tv' else 'movie'}/{tmdb_id}") or {}
                self._titles[key] = str((d.get("name") if kind == "tv" else d.get("title")) or f"{kind} {tmdb_id}")
            except ApiError:
                self._titles[key] = f"{kind} {tmdb_id}"
        return self._titles[key]


class Arr(Client):
    """Sonarr (kind "tv") or Radarr (kind "movie"): both speak API v3."""

    def __init__(self, kind: str, base_url: str, api_key: str, timeout: float = 20):
        super().__init__("Sonarr" if kind == "tv" else "Radarr", base_url, {"X-Api-Key": api_key}, timeout)
        self.kind = kind

    def system_status(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.get("/api/v3/system/status"))

    def health(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self.get("/api/v3/health") or [])

    def items(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self.get("/api/v3/series" if self.kind == "tv" else "/api/v3/movie") or [])

    def lookup(self, tvdb_id: int | None, tmdb_id: int | None) -> list[dict[str, Any]]:
        if self.kind == "tv":
            return cast(list[dict[str, Any]], self.get("/api/v3/series", tvdbId=tvdb_id) or [])
        return cast(list[dict[str, Any]], self.get("/api/v3/movie", tmdbId=tmdb_id) or [])

    def queue(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        include = {"includeSeries": "true"} if self.kind == "tv" else {"includeMovie": "true"}
        for page in range(1, MAX_QUEUE_PAGES + 1):
            data = self.request("GET", "/api/v3/queue", {"page": page, "pageSize": 200, **include}) or {}
            recs = data.get("records", [])
            out += recs
            if not recs or len(out) >= int(data.get("totalRecords", 0)):
                break
        return out

    def delete_queue(self, ids: list[int]) -> None:
        self.request(
            "DELETE",
            "/api/v3/queue/bulk",
            {"removeFromClient": "true", "blocklist": "true", "skipRedownload": "false"},
            {"ids": ids},
        )

    def tag_id(self, label: str) -> int | None:
        tags = self.get("/api/v3/tag") or []
        return next((int(t["id"]) for t in tags if str(t.get("label")).lower() == label.lower()), None)

    def set_tag(self, item_id: int, tag_id: int, add: bool) -> None:
        noun, ids = ("series", "seriesIds") if self.kind == "tv" else ("movie", "movieIds")
        body = {ids: [item_id], "tags": [tag_id], "applyTags": "add" if add else "remove"}
        self.request("PUT", f"/api/v3/{noun}/editor", body=body)

    def command(self, body: dict[str, Any]) -> None:
        self.request("POST", "/api/v3/command", body=body)
