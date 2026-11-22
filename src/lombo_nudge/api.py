"""The small HTTP API behind `lombo-nudge serve` (docs/contracts.md, docs/security.md).

Three routes: GET /v1/findings, GET /v1/retry/{request_id}, POST /v1/retry. Every request needs
`Authorization: Bearer <token>`. This module only speaks HTTP; the findings and the retry come in as two functions
from cli.py, so the rules stay in one place. No CORS headers on purpose.
"""

import hmac
import json
import re
import socket
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .config import Config, parse_bind
from .findings import clean
from .http import ApiError
from .retry import Outcome, RetryRefused
from .state import StateError

MAX_BODY = 4096  # bytes of request body
MAX_ID = 10**9
MAX_FAILURES = 10  # bad or missing tokens per client per window, then 429
WINDOW = 60.0  # seconds
MAX_CLIENTS = 1024  # failure records kept at once
TIMEOUT = 10.0  # seconds a client may stall on one request

RETRY_PATH = re.compile(r"/v1/retry/(\d{1,9})")

Reply = tuple[int, dict[str, Any], dict[str, str] | None]  # status, JSON body, extra headers
Findings = Callable[[], dict[str, Any]]  # the findings document, made now
Retry = Callable[[int, bool], Outcome]  # (request id, apply) -> what happened; raises RetryRefused/ApiError/StateError


def fail(code: int, message: str, extra: dict[str, str] | None = None) -> Reply:
    return code, {"ok": False, "message": message}, extra


class FailureLimiter:
    """Counts bad tries per client address. Asked before the token is compared, so guessing is slowed
    for everyone, a right guess included."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.failures: dict[str, deque[float]] = {}
        self.lock = threading.Lock()

    def _recent(self, client: str, now: float) -> deque[float]:
        times = self.failures.get(client)
        if times is None:
            return deque()
        while times and now - times[0] >= WINDOW:
            times.popleft()
        if not times:
            del self.failures[client]
        return times

    def blocked(self, client: str) -> bool:
        with self.lock:
            return len(self._recent(client, self.clock())) >= MAX_FAILURES

    def record(self, client: str) -> None:
        with self.lock:
            now = self.clock()
            times = self._recent(client, now)
            times.append(now)
            self.failures[client] = times
            if len(self.failures) > MAX_CLIENTS:  # the oldest client is forgotten; memory stays bounded
                del self.failures[next(iter(self.failures))]


def parse_retry_body(raw: bytes) -> tuple[int | None, str]:
    """(request_id, "") for a valid body, else (None, why). Strict: one key, an integer, no duplicates, no NaN."""

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out

    def refuse(name: str) -> Any:
        raise ValueError(name)

    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates, parse_constant=refuse)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return None, "the body must be strict JSON"
    value = data.get("request_id") if isinstance(data, dict) and set(data) == {"request_id"} else None
    if not isinstance(value, int) or isinstance(value, bool):
        return None, 'the body must be exactly {"request_id": <whole number>}'
    if not 1 <= value <= MAX_ID:
        return None, f"request_id must be between 1 and {MAX_ID}"
    return value, ""


def make_server(
    cfg: Config,
    findings: Findings,
    retry: Retry,
    bind: tuple[str, int] | None = None,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] | None = None,
) -> ThreadingHTTPServer:
    """Build the server (not started). `bind` overrides API_BIND (tests use port 0)."""
    token = cfg.api_token.encode()
    limiter = FailureLimiter(clock)
    one_retry = threading.Lock()  # one retry at a time, on top of the state lock shared with `run`

    def say(text: str) -> None:
        (log or (lambda t: print(t, file=sys.stderr, flush=True)))(text)

    class Handler(BaseHTTPRequestHandler):
        server_version = "lombo-nudge"
        sys_version = ""
        timeout = TIMEOUT

        def log_message(self, format: str, *args: Any) -> None:  # silence the default access log
            pass

        def _send(self, code: int, payload: dict[str, Any], extra: dict[str, str] | None = None) -> None:
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)
            # Method, the shape of the route (the request number is not logged) and the status. Never headers,
            # query, body or titles.
            path = urlsplit(self.path).path
            shape = "/v1/retry/{id}" if RETRY_PATH.fullmatch(path) else clean(path)[:30]
            say(f"[api] {self.client_address[0]} {self.command} {shape} -> {code}")

        def _authorised(self) -> bool:
            client = self.client_address[0]
            if limiter.blocked(client):
                self._send(*fail(429, "too many failed tries; wait a minute", {"Retry-After": str(int(WINDOW))}))
                return False
            scheme, _, given = (self.headers.get("Authorization") or "").partition(" ")
            if scheme.lower() == "bearer" and hmac.compare_digest(given.strip().encode(), token):
                return True
            limiter.record(client)
            self._send(*fail(401, "unauthorized", {"WWW-Authenticate": "Bearer"}))
            return False

        def _dispatch(self) -> None:
            if self._authorised():
                self._send(*self._handle(urlsplit(self.path).path))

        def _handle(self, route: str) -> Reply:
            looked = RETRY_PATH.fullmatch(route)
            allowed = {"/v1/findings": "GET", "/v1/retry": "POST"}.get(route, "GET" if looked else None)
            if allowed is None:
                return fail(404, "not found")
            if self.command != allowed:
                return fail(405, f"use {allowed} here", {"Allow": allowed})
            try:
                if route == "/v1/findings":
                    return 200, findings(), None
                if looked:
                    request_id = int(looked.group(1))
                    out = retry(request_id, False)
                    return 200, {"ok": True, "request_id": request_id, "would": list(out.lines)}, None
                return self._retry()
            except RetryRefused as e:
                cause = e.__cause__
                if isinstance(cause, ApiError) and not str(cause).endswith("HTTP 404"):
                    say(f"[FAIL] api {route}: {cause}")
                    return fail(502, "could not complete the request; see the log")
                return fail(422, "there is no such request" if cause else str(e))
            except (ApiError, StateError) as e:  # our own errors have readable messages
                say(f"[FAIL] api {route}: {e}")
                return fail(502, "could not complete the request; see the log")
            except Exception as e:  # noqa: BLE001  # one bad request must never stop the server
                say(f"[FAIL] api {route}: {type(e).__name__}")
                return fail(500, "error")

        def _retry(self) -> Reply:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            length_text = self.headers.get("Content-Length")
            if ctype != "application/json":
                return fail(415, "send Content-Type: application/json")
            if length_text is None or not length_text.isdigit():
                return fail(411, "send a Content-Length")
            if int(length_text) > MAX_BODY:
                return fail(413, f"the body may be at most {MAX_BODY} bytes")
            request_id, why = parse_retry_body(self.rfile.read(int(length_text)))
            if request_id is None:
                return fail(400, why)
            with one_retry:
                out = retry(request_id, True)
            if out.action == "cooldown":
                return fail(422, out.message)
            if cfg.dry_run:
                return 200, {"ok": True, "message": "dry run: nothing was changed", "would": list(out.lines)}, None
            message = "Done." if out.changed else "Nothing needed doing."
            return 200, {"ok": True, "message": message, "did": list(out.lines)}, None

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _dispatch

    host, port = bind or parse_bind(cfg.api_bind)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    return Server((host, port), Handler)
