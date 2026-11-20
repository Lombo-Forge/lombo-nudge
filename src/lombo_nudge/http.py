"""One small JSON-over-HTTP client for every service. Standard library only.

Every failure becomes an ApiError with a short message that names the service and the path,
so callers print it instead of crashing.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ApiError(Exception):
    """A request failed. The message names the service, the path and the reason."""


class Client:
    def __init__(self, name: str, base_url: str, headers: dict[str, str] | None = None, timeout: float = 10):
        self.name = name
        self.base = base_url.rstrip("/")
        self.headers = headers or {}
        self.timeout = timeout

    def request(
        self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None, auth: bool = True
    ) -> Any:
        query = "?" + urllib.parse.urlencode(params) if params else ""
        data = json.dumps(body).encode() if body is not None else None
        # S310 is silenced here and below: config.py only accepts http(s) URLs.
        req = urllib.request.Request(self.base + path + query, data=data, method=method)  # noqa: S310
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth:
            for k, v in self.headers.items():
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise ApiError(f"{self.name} {path}: HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(f"{self.name} {path}: {e}") from e
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as e:
            raise ApiError(f"{self.name} {path}: answer is not JSON ({e})") from e

    def get(self, path: str, auth: bool = True, **params: Any) -> Any:
        return self.request("GET", path, params or None, auth=auth)
