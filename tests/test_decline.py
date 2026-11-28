"""LOMBO_NUDGE_DECLINE_AFTER_DAYS: what gets declined, when, and what holds it back."""

import time

import pytest

from lombo_nudge import cli
from lombo_nudge.config import from_env

DAY = 86400


def declines(world):
    # a DELETE: real Seerr answers 409 to a decline of an approved request
    assert all(c["method"] == "DELETE" for c in world.seerr.changes())
    return [c["path"] for c in world.seerr.changes()]


def cfg_for(env, **over):
    return from_env({**env, "LOMBO_NUDGE_DECLINE_AFTER_DAYS": "7", "LOMBO_NUDGE_DRY_RUN": "false", **over})


def test_declines_only_after_the_wait_and_only_where_nothing_arrives(world, env):
    cfg, now = cfg_for(env), time.time()
    cli.run_once(cfg, now)
    cli.run_once(cfg, now + 6 * DAY)
    assert declines(world) == []  # old requests, but only just seen stuck
    cli.run_once(cfg, now + 8 * DAY)
    # 1 not_found and 3 missing_in_arr; 2 has a failed download in the queue
    assert sorted(declines(world)) == ["/api/v1/request/1", "/api/v1/request/3"]
    assert any("Removed the request for Test Movie (Sam)" in c["body"]["content"] for c in world.hook.calls)


def test_off_by_default_and_dry_run_declines_nothing(world, env, capsys):
    now = time.time()
    for cfg in (from_env({**env, "LOMBO_NUDGE_DRY_RUN": "false"}), cfg_for(env, LOMBO_NUDGE_DRY_RUN="true")):
        cli.run_once(cfg, now)
        cli.run_once(cfg, now + 30 * DAY)
    assert declines(world) == []
    assert "would decline #1" in capsys.readouterr().out
    cli.plan(cfg_for(env), False, now + 30 * DAY)
    assert "#1 would be declined in 0 day(s)" in capsys.readouterr().out


def test_nothing_is_declined_while_searching_is_broken(world, env):
    cfg, now = cfg_for(env), time.time()
    cli.run_once(cfg, now)
    world.radarr.routes["/api/v3/health"] = [{"source": "IndexerStatusCheck", "type": "error", "message": "down"}]
    cli.run_once(cfg, now + 8 * DAY)
    assert declines(world) == []
    world.radarr.routes["/api/v3/health"] = [{"source": "IndexerLongTermStatusCheck", "type": "warning"}]
    cli.run_once(cfg, now + 9 * DAY)
    assert len(declines(world)) == 2  # one dead indexer among several does not stop it


def test_at_most_three_a_day(world, env, monkeypatch):
    monkeypatch.setattr(cli.core, "DECLINES_PER_DAY", 1)
    cfg, now = cfg_for(env), time.time()
    cli.run_once(cfg, now)
    cli.run_once(cfg, now + 8 * DAY)
    cli.run_once(cfg, now + 8 * DAY + 3600)
    assert len(declines(world)) == 1
    cli.run_once(cfg, now + 9 * DAY + 60)
    assert len(declines(world)) == 2


def test_a_retry_starts_the_wait_again(world, env):
    cfg, now = cfg_for(env), time.time()
    cli.run_once(cfg, now)
    cli.retry_once(cfg, 1, now + 6 * DAY)
    cli.run_once(cfg, now + 6 * DAY + 1)
    cli.run_once(cfg, now + 8 * DAY)
    assert "/api/v1/request/1" not in declines(world)


def test_a_partly_arrived_show_is_never_declined(world, env):
    show = world.sonarr.routes["/api/v3/series"][0]
    show["seasons"][0]["statistics"]["episodeFileCount"] = 5
    world.sonarr.routes["/api/v3/queue"] = {"records": [], "totalRecords": 0}
    cfg, now = cfg_for(env), time.time()
    for day in (0, 4, 12, 20):
        cli.run_once(cfg, now + day * DAY)
    assert [f["reason"] for f in cli.findings_doc(cfg, now + 20 * DAY)["findings"] if f["request_id"] == 2] == [
        "partly_here"
    ]
    assert "/api/v1/request/2" not in declines(world)


@pytest.mark.parametrize(("status", "declined"), [("continuing", False), ("upcoming", False), ("ended", True)])
def test_a_show_still_releasing_is_never_declined(world, env, status, declined):
    world.sonarr.routes["/api/v3/series"][0]["status"] = status
    world.sonarr.routes["/api/v3/queue"] = {"records": [], "totalRecords": 0}  # nothing found for request 2
    cfg, now = cfg_for(env), time.time()
    cli.run_once(cfg, now)
    cli.run_once(cfg, now + 8 * DAY)
    assert ("/api/v1/request/2" in declines(world)) is declined
