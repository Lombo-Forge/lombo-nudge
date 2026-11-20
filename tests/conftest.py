"""Fake Seerr, Sonarr, Radarr, Jellyfin and a webhook on localhost, so tests need no real server."""

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

Route = Any  # a JSON-able value, or a callable(query) returning one; an int means "reply with that status"


class Fake:
    """One fake HTTP service. `routes` maps a GET path to its answer; other methods are recorded and answered 200."""

    def __init__(self, header: str = "X-Api-Key", key: str | None = "good-key", open_paths: tuple[str, ...] = ()):
        self.calls: list[dict[str, Any]] = []
        self.routes: dict[str, Route] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _reply(self, code: int, payload: Any = None, raw: bytes | None = None) -> None:
                data = raw if raw is not None else json.dumps(payload if payload is not None else {}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self) -> None:
                u = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(u.query).items()}
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else None
                outer.calls.append({"method": self.command, "path": u.path, "query": query, "body": body})
                got = self.headers.get(header)
                expected = f'MediaBrowser Token="{key}"' if header == "Authorization" else key
                if key is not None and u.path not in open_paths and got != expected:
                    return self._reply(401)
                if self.command != "GET":
                    return self._reply(200, {})
                if u.path not in outer.routes:
                    return self._reply(404)
                route = outer.routes[u.path]
                value = route(query) if callable(route) else route
                if isinstance(value, bytes):
                    return self._reply(200, raw=value)
                if isinstance(value, int):
                    return self._reply(value)
                return self._reply(200, value)

            do_GET = do_POST = do_DELETE = _handle

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def changes(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] != "GET"]


def iso(hours_ago: float) -> str:
    return datetime.fromtimestamp(time.time() - hours_ago * 3600, UTC).isoformat()


def request(
    rid: int, kind: str, tmdb: int, tvdb: int | None = None, hours_ago: float = 72, **extra: Any
) -> dict[str, Any]:
    media: dict[str, Any] = {"mediaType": kind, "tmdbId": tmdb, "status": 3}
    if tvdb:
        media["tvdbId"] = tvdb
    return {
        "id": rid,
        "status": 2,
        "type": kind,
        "is4k": False,
        "createdAt": iso(hours_ago),
        "updatedAt": iso(hours_ago),
        "requestedBy": {"displayName": "Sam"},
        "media": media,
        **extra,
    }


def paged(items: list[dict[str, Any]]) -> Callable[[dict[str, str]], dict[str, Any]]:
    def route(q: dict[str, str]) -> dict[str, Any]:
        skip, take = int(q.get("skip", 0)), int(q.get("take", 100))
        return {"results": items[skip : skip + take]}

    return route


class World:
    def __init__(self) -> None:
        self.seerr = Fake(open_paths=("/api/v1/status",))
        self.sonarr = Fake()
        self.radarr = Fake()
        self.jf = Fake(header="Authorization", open_paths=("/System/Info/Public",))
        self.hook = Fake(key=None)  # a webhook needs no key
        self.all = [self.seerr, self.sonarr, self.radarr, self.jf, self.hook]
        self.reset()

    def reset(self) -> None:
        s = self.seerr.routes
        s["/api/v1/status"] = {"version": "1.2.3"}
        s["/api/v1/auth/me"] = {"displayName": "Admin"}
        s["/api/v1/movie/100"] = {"title": "Test Movie"}
        s["/api/v1/movie/101"] = {"title": "Missing Movie"}
        s["/api/v1/tv/200"] = {"name": "Test Show"}
        s["/api/v1/request"] = paged(
            [
                request(1, "movie", 100),  # in Radarr, no file, nothing queued -> not_found
                request(2, "tv", 200, 2000),  # in Sonarr, a failed download -> download_failed
                request(3, "movie", 101),  # not in Radarr -> missing_in_arr
                request(4, "movie", 102) | {"media": {"mediaType": "movie", "tmdbId": 102, "status": 5}},  # available
            ]
        )
        s["/api/v1/request/1"] = request(1, "movie", 100)
        s["/api/v1/request/2"] = request(2, "tv", 200, 2000, seasons=[{"seasonNumber": 1}, {"seasonNumber": 2}])
        s["/api/v1/request/4"] = request(4, "movie", 102) | {
            "media": {"mediaType": "movie", "tmdbId": 102, "status": 5}
        }
        self.radarr.routes["/api/v3/system/status"] = {"version": "5.0"}
        self.radarr.routes["/api/v3/health"] = []
        self.radarr.routes["/api/v3/movie"] = lambda q: (
            [{"id": 10, "tmdbId": 100, "hasFile": False, "isAvailable": True}]
            if q.get("tmdbId") in (None, "100")
            else []
        )
        self.radarr.routes["/api/v3/queue"] = {"records": [], "totalRecords": 0}
        self.sonarr.routes["/api/v3/system/status"] = {"version": "4.0"}
        self.sonarr.routes["/api/v3/health"] = []
        self.sonarr.routes["/api/v3/series"] = [
            {
                "id": 20,
                "tvdbId": 2000,
                "seasons": [
                    {"seasonNumber": 1, "statistics": {"episodeFileCount": 0, "episodeCount": 8}},
                    {"seasonNumber": 2, "statistics": {"episodeFileCount": 0, "episodeCount": 8}},
                ],
            }
        ]
        self.sonarr.routes["/api/v3/queue"] = {
            "records": [
                {"id": 77, "seriesId": 20, "title": "Show.S01", "sizeleft": 500, "trackedDownloadStatus": "warning"}
            ],
            "totalRecords": 1,
        }
        self.jf.routes["/System/Info/Public"] = {"ServerName": "Fake", "Version": "10.9"}
        self.jf.routes["/System/Info"] = {"ServerName": "Fake"}
        self.jf.routes["/Items"] = {"Items": [], "TotalRecordCount": 0}

    def env(self, tmp: Any, **extra: str) -> dict[str, str]:
        return {
            "LOMBO_NUDGE_SEERR_URL": self.seerr.url,
            "LOMBO_NUDGE_SEERR_API_KEY": "good-key",
            "LOMBO_NUDGE_SONARR_URL": self.sonarr.url,
            "LOMBO_NUDGE_SONARR_API_KEY": "good-key",
            "LOMBO_NUDGE_RADARR_URL": self.radarr.url,
            "LOMBO_NUDGE_RADARR_API_KEY": "good-key",
            "LOMBO_NUDGE_WEBHOOK_URL": self.hook.url + "/hook",
            "LOMBO_NUDGE_STATE_FILE": str(tmp / "state.json"),
            **extra,
        }


@pytest.fixture
def world():
    w = World()
    yield w
    for f in w.all:
        f.server.shutdown()


@pytest.fixture
def env(world, tmp_path):
    return world.env(tmp_path)
