"""LOMBO_NUDGE_FALLBACK_TAG: when a title is tagged and searched, when it is left alone, and when the tag goes."""

import time

from lombo_nudge import cli, state
from lombo_nudge.clients import Arr
from lombo_nudge.config import from_env
from lombo_nudge.http import ApiError

MIN = 60


def setup(world, env, **over):
    for arr in (world.sonarr, world.radarr):
        arr.routes["/api/v3/tag"] = [{"id": 5, "label": "private"}]
    over = {"LOMBO_NUDGE_FALLBACK_TAG": "private", "LOMBO_NUDGE_DRY_RUN": "false", **over}
    return from_env({**env, **over})


def edits(arr):
    return [c["body"] for c in arr.changes() if c["path"].endswith("/editor")]


def searches(arr):
    return [c["body"] for c in arr.changes() if c["path"] == "/api/v3/command"]


def test_tags_and_searches_once_after_the_wait(world, env):
    cfg, now = setup(world, env), time.time()
    cli.fallback_once(cfg, now)
    cli.fallback_once(cfg, now + 29 * MIN)
    assert world.radarr.changes() == []
    cli.fallback_once(cfg, now + 30 * MIN)
    cli.fallback_once(cfg, now + 35 * MIN)
    assert edits(world.radarr) == [{"movieIds": [10], "tags": [5], "applyTags": "add"}]
    assert searches(world.radarr) == [{"name": "MoviesSearch", "movieIds": [10]}]
    assert world.sonarr.changes() == []  # the show has something in the queue


def test_the_tag_comes_off_when_nothing_is_missing(world, env):
    cfg, now = setup(world, env), time.time()
    cli.fallback_once(cfg, now)
    cli.fallback_once(cfg, now + 30 * MIN)
    world.radarr.routes["/api/v3/movie"] = [{"id": 10, "tmdbId": 100, "hasFile": True, "tags": [5]}]
    cli.fallback_once(cfg, now + 40 * MIN)
    assert edits(world.radarr)[-1] == {"movieIds": [10], "tags": [5], "applyTags": "remove"}
    assert state.load(cfg.state_file)["tagged"] == {}


def test_a_hand_added_tag_is_left_alone(world, env):
    world.radarr.routes["/api/v3/movie"] = [{"id": 10, "tmdbId": 100, "hasFile": False, "tags": [5]}]
    cfg, now = setup(world, env), time.time()
    cli.fallback_once(cfg, now)
    cli.fallback_once(cfg, now + 30 * MIN)
    assert world.radarr.changes() == []
    assert state.load(cfg.state_file)["tagged"] == {}


def test_nothing_changes_in_a_dry_run_or_while_searching_is_broken(world, env, capsys):
    now = time.time()
    dry = setup(world, env, LOMBO_NUDGE_DRY_RUN="true")
    cli.fallback_once(dry, now)
    cli.fallback_once(dry, now + 30 * MIN)
    assert "would tag" in capsys.readouterr().out
    world.radarr.routes["/api/v3/health"] = [{"source": "IndexerStatusCheck", "type": "error", "message": "down"}]
    cli.fallback_once(setup(world, env), now + 31 * MIN)
    assert world.radarr.changes() == []
    assert world.sonarr.changes() == []


def test_off_without_a_tag(world, env):
    cfg = from_env({**env, "LOMBO_NUDGE_DRY_RUN": "false"})
    cli.fallback_once(cfg, time.time() + 90 * MIN)
    assert world.radarr.calls == []


def test_a_title_deleted_meanwhile_does_not_block_the_rest(world, env, monkeypatch):
    cfg, now = setup(world, env), time.time()
    cli.fallback_once(cfg, now)
    cli.fallback_once(cfg, now + 30 * MIN)
    world.radarr.routes["/api/v3/movie"] = []

    def gone(self, item_id, tag_id, add):
        raise ApiError("Radarr /api/v3/movie/editor: HTTP 500")

    monkeypatch.setattr(Arr, "set_tag", gone)
    cli.fallback_once(cfg, now + 40 * MIN)
    assert state.load(cfg.state_file)["tagged"] == {}


def test_doctor_checks_the_tag_exists(world, env, capsys):
    cfg = setup(world, env)
    assert cli.doctor(cfg) == 0
    assert "tag 'private' exists" in capsys.readouterr().out
    world.radarr.routes["/api/v3/tag"] = []
    assert cli.doctor(cfg) == 1
    assert "no tag 'private'" in capsys.readouterr().out
