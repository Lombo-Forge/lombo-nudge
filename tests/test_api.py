import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from lombo_nudge import api, cli, findings, state
from lombo_nudge.cli import main
from lombo_nudge.config import from_env, parse_bind, token_problem

TOKEN = "t" * 12 + "OPEN-sesame-24chars"
LIVE = {"LOMBO_NUDGE_DRY_RUN": "false"}


class Api:
    def __init__(self, env, log=None):
        self.cfg = from_env({**env, "LOMBO_NUDGE_API_TOKEN": TOKEN})
        self.lines: list[str] = log if log is not None else []
        self.server = api.make_server(
            self.cfg,
            lambda: cli.findings_doc(self.cfg, time.time()),
            lambda rid, apply: cli.retry_once(self.cfg, rid, time.time(), apply),
            bind=("127.0.0.1", 0),
            log=self.lines.append,
        )
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def call(self, method, path, body=None, headers=None, token=TOKEN, raw=None):
        h = {"Authorization": f"Bearer {token}"} if token else {}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        text = r.read().decode()
        c.close()
        return r.status, json.loads(text) if text else None, r


@pytest.fixture
def srv(env):
    s = Api(env)
    yield s
    s.server.shutdown()
    s.server.server_close()


@pytest.fixture
def live(world, tmp_path):
    s = Api(world.env(tmp_path, **LIVE))
    yield s
    s.server.shutdown()
    s.server.server_close()


def test_findings_route_is_fresh_and_shaped(srv, world):
    status, doc, r = srv.call("GET", "/v1/findings")
    assert status == 200
    assert r.getheader("Cache-Control") == "no-store"
    assert r.getheader("Access-Control-Allow-Origin") is None
    assert doc["version"] == 1
    assert doc["complete"] is True
    assert [f["request_id"] for f in doc["findings"]] == [1, 2, 3]
    assert doc["findings"][0] == {
        "request_id": 1,
        "title": "Test Movie",
        "requester": "Sam",
        "kind": "movie",
        "state": "searching",
        "reason": "not_found",
        "summary": findings.SUMMARIES["not_found"],
        "days": 3,
        "can_retry": True,
        "alerted": False,
    }


def test_retry_preview_changes_nothing(live, world):
    status, doc, _ = live.call("GET", "/v1/retry/2")
    assert status == 200
    assert doc["ok"] is True
    assert doc["request_id"] == 2
    assert doc["would"][0].startswith("remove:")
    assert "  - Show.S01" in doc["would"]
    assert world.sonarr.changes() == []


def test_post_retry_dry_run_changes_nothing(srv, world):
    status, doc, _ = srv.call("POST", "/v1/retry", {"request_id": 2})
    assert status == 200
    assert doc["ok"] is True
    assert doc["message"] == "dry run: nothing was changed"
    assert doc["would"][0].startswith("remove:")
    assert world.sonarr.changes() == []
    assert state.load(srv.cfg.state_file)["retry"] == {}


def test_post_retry_live_then_cooldown_422(live, world):
    status, doc, _ = live.call("POST", "/v1/retry", {"request_id": 2})
    assert (status, doc["ok"], doc["message"]) == (200, True, "Done.")
    assert world.sonarr.changes()[0]["body"] == {"ids": [77]}
    status, doc, _ = live.call("POST", "/v1/retry", {"request_id": 2})
    assert status == 422
    assert doc["ok"] is False
    assert "minutes before trying again" in doc["message"]
    assert len(world.sonarr.changes()) == 1


def test_post_retry_that_does_nothing_says_so(live, world):
    world.sonarr.routes["/api/v3/queue"] = {"records": [{"id": 77, "seriesId": 20, "sizeleft": 9}], "totalRecords": 1}
    status, doc, _ = live.call("POST", "/v1/retry", {"request_id": 2})
    assert (status, doc["message"]) == (200, "Nothing needed doing.")


def test_two_retries_at_once_act_once(live, world):
    results = []

    def go():
        results.append(live.call("POST", "/v1/retry", {"request_id": 2})[0])

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 422]
    assert len(world.sonarr.changes()) == 1


def test_retry_refusals_and_upstream_trouble(srv, world):
    status, doc, _ = srv.call("GET", "/v1/retry/4")
    assert status == 422
    assert "not an approved request" in doc["message"]
    status, doc, _ = srv.call("GET", "/v1/retry/99")
    assert (status, doc["message"]) == (422, "there is no such request")
    world.sonarr.routes["/api/v3/series"] = 500
    status, doc, _ = srv.call("GET", "/v1/findings")
    assert (status, doc["message"]) == (502, "could not complete the request; see the log")
    assert "HTTP 500" in " ".join(srv.lines)
    assert "HTTP 500" not in json.dumps(doc)
    world.seerr.routes["/api/v1/request/2"] = 500
    status, doc, _ = srv.call("GET", "/v1/retry/2")
    assert status == 502
    assert "Seerr" not in doc["message"]


def test_state_error_is_502(env, world, monkeypatch):
    s = Api(env)
    real = state.locked
    monkeypatch.setattr(cli.state, "locked", lambda p: real(p, wait=0.1))
    try:
        with state.locked(s.cfg.state_file):
            status, doc, _ = s.call("POST", "/v1/retry", {"request_id": 2})
        assert (status, doc["message"]) == (502, "could not complete the request; see the log")
        assert "still working" in " ".join(s.lines)
    finally:
        s.server.shutdown()
        s.server.server_close()


def test_unexpected_error_is_500_and_server_lives(srv, world):
    world.seerr.routes["/api/v1/request"] = {"results": [{"id": 5, "status": 2, "media": "oops"}]}
    status, doc, _ = srv.call("GET", "/v1/findings")
    assert (status, doc) == (500, {"ok": False, "message": "error"})
    assert srv.call("GET", "/v1/retry/4")[0] == 422


def test_auth_and_limiter(srv):
    assert srv.call("GET", "/v1/findings", token=None)[:2] == (401, {"ok": False, "message": "unauthorized"})
    status, _, r = srv.call("GET", "/v1/findings", token="wrong")
    assert (status, r.getheader("WWW-Authenticate")) == (401, "Bearer")
    for _ in range(8):
        srv.call("GET", "/v1/findings", token="wrong")
    status, _, r = srv.call("GET", "/v1/findings")  # 10 failures so far: now even the right token is refused
    assert (status, r.getheader("Retry-After")) == (429, "60")


def test_limiter_forgets_after_the_window():
    now = [0.0]
    lim = api.FailureLimiter(lambda: now[0])
    for _ in range(api.MAX_FAILURES):
        lim.record("a")
    assert lim.blocked("a")
    now[0] = api.WINDOW + 1
    assert not lim.blocked("a")
    for i in range(api.MAX_CLIENTS + 5):
        lim.record(f"c{i}")
    assert len(lim.failures) <= api.MAX_CLIENTS


def test_routes_methods_and_unknowns(srv):
    assert srv.call("GET", "/nope")[0] == 404
    assert srv.call("GET", "/v1/retry/abc")[0] == 404
    status, _, r = srv.call("POST", "/v1/findings", {"x": 1})
    assert (status, r.getheader("Allow")) == (405, "GET")
    status, _, r = srv.call("GET", "/v1/retry")
    assert (status, r.getheader("Allow")) == (405, "POST")
    status, _, r = srv.call("POST", "/v1/retry/2", {"request_id": 2})
    assert (status, r.getheader("Allow")) == (405, "GET")
    assert srv.call("OPTIONS", "/v1/findings")[0] == 405
    assert srv.call("GET", "/nope", token=None)[0] == 401  # a stranger learns nothing about the routes


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[1]",
        b"{}",
        b'{"request_id": "2"}',
        b'{"request_id": true}',
        b'{"request_id": 2.5}',
        b'{"request_id": 0}',
        b'{"request_id": 1000000001}',
        b'{"request_id": 2, "x": 1}',
        b'{"request_id": 2, "request_id": 3}',
        b'{"request_id": NaN}',
        b"\xff\xfe",
    ],
)
def test_body_validation(srv, world, raw):
    status, doc, _ = srv.call("POST", "/v1/retry", raw=raw)
    assert (status, doc["ok"]) == (400, False)
    assert world.sonarr.calls == [] or world.sonarr.changes() == []


def test_header_validation(srv):
    ok = json.dumps({"request_id": 2}).encode()
    assert srv.call("POST", "/v1/retry", raw=ok, headers={"Content-Type": "text/plain"})[0] == 415
    assert srv.call("POST", "/v1/retry", raw=ok, headers={"Content-Type": "application/json; charset=utf-8"})[0] == 200
    big = b'{"request_id": 2, "pad": "' + b"x" * 5000 + b'"}'
    assert srv.call("POST", "/v1/retry", raw=big)[0] == 413
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    c.putrequest("POST", "/v1/retry")
    c.putheader("Authorization", f"Bearer {TOKEN}")
    c.putheader("Content-Type", "application/json")
    c.endheaders()
    assert c.getresponse().status == 411
    c.close()


def test_nothing_secret_is_logged_or_answered(live, world):
    live.call("GET", "/v1/findings")
    live.call("POST", "/v1/retry", {"request_id": 2})
    live.call("GET", "/v1/retry/2?token=abc", headers={"X-Secret": "hush"})
    live.call("GET", "/v1/findings", token="wrong-guess")
    live.call("POST", "/v1/retry", raw=b'{"request_id": "private body"}')
    everything = "\n".join(live.lines)
    for secret in (TOKEN, "wrong-guess", "private body", "hush", "abc", "Test Movie", "Show.S01", "Sam"):
        assert secret not in everything
    assert "GET /v1/retry/{id} -> 200" in everything
    assert "[api] 127.0.0.1 GET /v1/findings -> 200" in everything


def test_serve_refuses_without_a_good_token(env, capsys):
    assert main(["serve"], env) == 1
    assert "LOMBO_NUDGE_API_TOKEN is not set" in capsys.readouterr().err
    assert main(["serve"], {**env, "LOMBO_NUDGE_API_TOKEN": "short"}) == 1
    assert main(["serve"], {**env, "LOMBO_NUDGE_API_TOKEN": "x" * 20 + " " + "y" * 20}) == 1
    assert "no spaces" in capsys.readouterr().err


def test_serve_starts_and_stops(env, monkeypatch, capsys):
    good = {**env, "LOMBO_NUDGE_API_TOKEN": TOKEN, "LOMBO_NUDGE_API_BIND": "0.0.0.0:8095"}

    def stop(self, *a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(api.ThreadingHTTPServer, "serve_forever", stop)
    assert main(["serve"], good) == 0
    cap = capsys.readouterr()
    assert "API on 0.0.0.0:8095" in cap.out
    assert "not loopback" in cap.err


def test_config_bind_and_token():
    assert parse_bind("127.0.0.1:8095") == ("127.0.0.1", 8095)
    assert parse_bind("[::1]:80") == ("::1", 80)
    for bad in ("localhost:80", "127.0.0.1", "127.0.0.1:0", "127.0.0.1:70000", ":80", "1.2.3.4:x"):
        with pytest.raises(Exception, match="LOMBO_NUDGE_API_BIND"):
            parse_bind(bad)
    assert token_problem(TOKEN) is None
    assert token_problem("é" * 30)


def test_doctor_reports_the_api(env, world, capsys):
    assert main(["doctor"], env) == 0
    assert "API: off" in capsys.readouterr().out
    assert main(["doctor"], {**env, "LOMBO_NUDGE_API_TOKEN": TOKEN}) == 0
    out = capsys.readouterr().out
    assert "listen on 127.0.0.1:8095" in out
    assert "[warn]" not in out
    wide = {**env, "LOMBO_NUDGE_API_TOKEN": TOKEN, "LOMBO_NUDGE_API_BIND": "0.0.0.0:8095"}
    assert main(["doctor"], wide) == 0
    assert "[warn] API bind is not loopback" in capsys.readouterr().out
    assert main(["doctor"], {**env, "LOMBO_NUDGE_API_TOKEN": "short"}) == 1
    assert "[FAIL] API token" in capsys.readouterr().out
    with pytest.raises(Exception, match="LOMBO_NUDGE_API_BIND"):
        from_env({**env, "LOMBO_NUDGE_API_BIND": "nope"})


# ---------------------------------------------------------------- findings document and file
def test_every_reason_has_a_plain_summary():
    from lombo_nudge.core import REASONS

    assert set(findings.SUMMARIES) == set(REASONS)
    assert {c for c, (stuck, _) in REASONS.items() if stuck} == set(findings.STATES)
    for code, text in findings.SUMMARIES.items():
        assert 0 < len(text) <= 160, code
        assert text.endswith("."), code
        for name in ("Seerr", "Sonarr", "Radarr", "Arr", "_"):
            assert name not in text, (code, name)


def test_document_order_and_cleaning():
    from lombo_nudge.core import Verdict
    from lombo_nudge.scan import Finding

    def f(rid, days, code="not_found", title="T"):
        return Finding(rid, title, "Sam\x00", "movie", Verdict(code, "m"), days)

    rows = [f(5, 2), f(3, 9), f(2, 2, title="Bad\x1b[31m\ntitle"), f(9, 9, "available"), f(1, 9, "partly_here")]
    doc = findings.build(rows, False, {"3": ["not_found", 1], "1": ["other", 1]}, 1800000000.9)
    assert doc["updated"] == 1800000000
    assert doc["complete"] is False
    assert [r["request_id"] for r in doc["findings"]] == [1, 3, 2, 5]
    by = {r["request_id"]: r for r in doc["findings"]}
    assert by[3]["alerted"] is True
    assert by[1]["alerted"] is False  # alerted for a different reason
    assert by[1]["can_retry"] is False
    assert by[2]["title"] == "Bad?[31m?title"
    assert by[2]["requester"] == "Sam?"
    assert len(findings.clean("x" * 500)) == 200


def test_run_writes_findings_file_atomically_even_in_dry_run(env, world, tmp_path):
    assert main(["run"], env) == 0
    path = tmp_path / "findings.json"
    doc = json.loads(path.read_text())
    assert [f["request_id"] for f in doc["findings"]] == [1, 2, 3]
    assert path.stat().st_mode & 0o777 == 0o644
    assert not (tmp_path / "findings.json.tmp").exists()
    assert world.hook.calls == []
    # alerted shows after a live round
    assert main(["run"], {**env, **LIVE}) == 0
    assert all(f["alerted"] for f in json.loads(path.read_text())["findings"])


def test_write_failure_does_not_fail_the_round(env, world, tmp_path, monkeypatch, capsys):
    def boom(path, doc):
        raise OSError("disk full")

    monkeypatch.setattr(findings, "write", boom)
    assert main(["run"], env) == 0
    assert "[warn] cannot write findings.json: disk full" in capsys.readouterr().err
    assert (tmp_path / "state.json").exists()


def test_write_is_atomic_and_cleans_up(tmp_path, monkeypatch):
    path = str(tmp_path / "findings.json")
    findings.write(path, {"a": 1})
    findings.write(path, {"a": 2})
    assert json.loads(Path(path).read_text()) == {"a": 2}

    def fail_replace(a, b):
        raise OSError("nope")

    monkeypatch.setattr("os.replace", fail_replace)
    with pytest.raises(OSError, match="nope"):
        findings.write(path, {"a": 3})
    assert json.loads(Path(path).read_text()) == {"a": 2}  # the old file is intact
    assert not Path(path + ".tmp").exists()


def test_plan_json_prints_the_document_and_changes_nothing(env, world, tmp_path, capsys):
    assert main(["plan", "--json"], {**env, **LIVE}) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["version"] == 1
    assert [f["reason"] for f in doc["findings"]] == ["not_found", "download_failed", "missing_in_arr"]
    assert sorted(p.name for p in tmp_path.iterdir()) == []
    assert world.hook.calls == []
    world.seerr.routes["/api/v1/request"] = 500
    assert main(["plan", "--json"], env) == 1
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------- the state lock
def test_state_lock_waits_then_gives_up(tmp_path):
    path = str(tmp_path / "state.json")
    with state.locked(path), pytest.raises(state.StateError, match="still working"), state.locked(path, wait=0.2):
        pass
    with state.locked(path, wait=0.2):  # free again once released
        pass
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(state.StateError, match="cannot take the lock"), state.locked(str(blocker / "d" / "s.json")):
        pass


def test_a_held_lock_makes_run_and_retry_fail_plainly(env, world, tmp_path, monkeypatch, capsys):
    real = state.locked
    monkeypatch.setattr(cli.state, "locked", lambda p: real(p, wait=0.1))
    with state.locked(str(tmp_path / "state.json")):
        assert main(["run"], env) == 1
        assert main(["retry", "2"], env) == 1
    assert capsys.readouterr().err.count("still working") == 2
