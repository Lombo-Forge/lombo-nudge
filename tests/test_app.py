import re
from pathlib import Path

import pytest

from lombo_nudge import core, notify, state
from lombo_nudge.clients import Arr, Seerr
from lombo_nudge.config import PREFIX, SETTINGS, ConfigError, from_env
from lombo_nudge.http import ApiError, Client
from lombo_nudge.jellyfin import Jellyfin

ROOT = Path(__file__).resolve().parent.parent


def test_config_defaults_are_safe(env):
    cfg = from_env(env)
    assert cfg.dry_run is True
    assert cfg.interval_seconds == 3600
    assert cfg.rules == core.Rules(24, 72, 12)
    assert cfg.jellyfin is None
    assert cfg.realert_days == 7


def test_config_one_arr_is_enough_and_jellyfin_is_optional(env):
    only_radarr = {k: v for k, v in env.items() if "SONARR" not in k}
    cfg = from_env({**only_radarr, "LOMBO_NUDGE_JELLYFIN_URL": "http://jf:8096", "LOMBO_NUDGE_JELLYFIN_API_KEY": "k"})
    assert cfg.sonarr is None
    assert cfg.radarr is not None
    assert cfg.jellyfin is not None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LOMBO_NUDGE_SEERR_URL", "seerr:5055"),
        ("LOMBO_NUDGE_SEERR_URL", ""),
        ("LOMBO_NUDGE_SEERR_API_KEY", "changeme"),
        ("LOMBO_NUDGE_SONARR_API_KEY", ""),
        ("LOMBO_NUDGE_RADARR_URL", "nope"),
        ("LOMBO_NUDGE_WEBHOOK_URL", "ftp://x"),
        ("LOMBO_NUDGE_INTERVAL_SECONDS", "5"),
        ("LOMBO_NUDGE_INTERVAL_SECONDS", "soon"),
        ("LOMBO_NUDGE_SEARCH_AFTER_HOURS", "0"),
        ("LOMBO_NUDGE_REALERT_DAYS", "x"),
    ],
)
def test_config_rejects_bad_values_and_names_the_setting(env, name, value):
    with pytest.raises(ConfigError, match=name):
        from_env({**env, name: value})


def test_config_needs_sonarr_or_radarr(env):
    bare = {k: v for k, v in env.items() if "SONARR" not in k and "RADARR" not in k}
    with pytest.raises(ConfigError, match="SONARR"):
        from_env(bare)


def test_env_example_matches_config():
    text = (ROOT / ".env.example").read_text()
    listed = set(re.findall(rf"^#?\s*{PREFIX}([A-Z_]+)=", text, re.M))
    assert listed == set(SETTINGS)
    source = (ROOT / "src/lombo_nudge/config.py").read_text()
    used = set(re.findall(r'get\("([A-Z_]+)"', source)) | set(re.findall(r'_number\(get, "([A-Z_]+)"', source))
    for app in re.findall(r'_service\(get, "([A-Z]+)"', source):
        used |= {app + "_URL", app + "_API_KEY"}
    assert used == set(SETTINGS)


def test_every_reason_is_documented():
    doc = (ROOT / "docs/how-it-works.md").read_text()
    for code in core.REASONS:
        assert f"`{code}`" in doc, code


def test_http_client_reports_failures(world):
    seerr = Seerr(world.seerr.url, "good-key")
    assert seerr.status()["version"] == "1.2.3"
    with pytest.raises(ApiError, match="HTTP 401"):
        Seerr(world.seerr.url, "wrong").me()
    with pytest.raises(ApiError, match="HTTP 404"):
        seerr.get("/nope")
    world.seerr.routes["/bad"] = b"<html>"
    with pytest.raises(ApiError, match="not JSON"):
        seerr.get("/bad")
    with pytest.raises(ApiError):
        Client("X", "http://127.0.0.1:1", timeout=1).get("/x")


def test_http_empty_answer_is_none(world):
    world.seerr.routes["/empty"] = b""
    assert Client("X", world.seerr.url, {"X-Api-Key": "good-key"}).get("/empty") is None


def test_seerr_pages_and_keeps_going_flag(world):
    world.seerr.routes["/api/v1/request"] = lambda q: {
        "results": [{"id": i} for i in range(int(q["skip"]), int(q["skip"]) + 100)]
    }
    seerr = Seerr(world.seerr.url, "good-key")
    got, complete = seerr.approved_requests()
    assert len(got) == 5000
    assert complete is False  # the safety cap cut it

    def second_page_fails(q):
        return {"results": [{"id": i} for i in range(100)]} if q["skip"] == "0" else 500

    world.seerr.routes["/api/v1/request"] = second_page_fails
    got, complete = seerr.approved_requests()
    assert (len(got), complete) == (100, False)
    world.seerr.routes["/api/v1/request"] = 500
    with pytest.raises(ApiError):
        seerr.approved_requests()


def test_seerr_title_falls_back(world):
    seerr = Seerr(world.seerr.url, "good-key")
    assert seerr.title("movie", 100) == "Test Movie"
    assert seerr.title("tv", 200) == "Test Show"
    assert seerr.title("movie", 999) == "movie 999"
    assert seerr.title("tv", None) == "a tv"


def test_arr_queue_pages_and_commands(world):
    pages = {
        "1": {"records": [{"id": 1}, {"id": 2}], "totalRecords": 3},
        "2": {"records": [{"id": 3}], "totalRecords": 3},
    }
    world.sonarr.routes["/api/v3/queue"] = lambda q: pages.get(q["page"], {"records": []})
    arr = Arr("tv", world.sonarr.url, "good-key")
    assert [r["id"] for r in arr.queue()] == [1, 2, 3]
    arr.delete_queue([1, 2])
    arr.command({"name": "SeriesSearch", "seriesId": 1})
    first, second = world.sonarr.changes()
    assert first["query"]["blocklist"] == "true"
    assert first["body"] == {"ids": [1, 2]}
    assert second["body"]["name"] == "SeriesSearch"
    assert arr.name == "Sonarr"
    assert Arr("movie", world.radarr.url, "k").name == "Radarr"


def test_jellyfin_has_title(world):
    jf = Jellyfin(world.jf.url, "good-key")
    assert jf.public_info()["ServerName"] == "Fake"
    assert jf.has_title("movie", 100, None) is False
    assert world.jf.calls[-1]["query"]["AnyProviderIdEquals"] == "tmdb.100"
    world.jf.routes["/Items"] = {"Items": [{"Id": "x"}]}
    assert jf.has_title("tv", None, 2000) is True
    assert world.jf.calls[-1]["query"]["AnyProviderIdEquals"] == "tvdb.2000"
    assert jf.has_title("tv", 5, None) is None
    with pytest.raises(ApiError, match="HTTP 401"):
        Jellyfin(world.jf.url, "wrong").info()


def test_notify_posts_json(world):
    notify.send(world.hook.url + "/hook", "hello")
    assert world.hook.calls[0]["body"] == {"content": "hello", "text": "hello"}
    with pytest.raises(notify.NotifyError):
        notify.send("http://127.0.0.1:1/hook", "x", timeout=1)


def test_state_survives_damage_and_reports_unwritable(tmp_path):
    path = tmp_path / "s.json"
    assert state.load(str(path)) == state.empty()
    path.write_text("{broken")
    assert state.load(str(path)) == state.empty()
    path.write_text("[1]")
    assert state.load(str(path)) == state.empty()
    state.save(str(path), {"queue": {"a": [1, 2]}})
    assert state.load(str(path))["queue"] == {"a": [1, 2]}
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(state.StateError, match="LOMBO_NUDGE_STATE_FILE"):
        state.save(str(blocker / "sub" / "s.json"), {})
