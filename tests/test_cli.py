import contextlib
import json
import time

from conftest import paged, request
from lombo_nudge import notify, state
from lombo_nudge.cli import MAX_ALERT_CHARS, main


def test_bad_settings_exit_2(capsys):
    assert main(["plan"], {}) == 2
    assert "LOMBO_NUDGE_SEERR_URL" in capsys.readouterr().err


def test_plan_lists_every_stuck_request_with_its_reason(env, world, capsys):
    assert main(["plan"], env) == 0
    out = capsys.readouterr().out
    assert "Test Movie" in out
    assert "[not_found]" in out
    assert "[download_failed]" in out
    assert "[missing_in_arr]" in out
    assert "#4" not in out  # available: hidden unless --all
    assert main(["check", "--all"], env) == 0
    assert "[available]" in capsys.readouterr().out


def test_plan_never_posts_or_writes(env, world, tmp_path):
    assert main(["plan"], {**env, "LOMBO_NUDGE_DRY_RUN": "false"}) == 0
    assert world.hook.calls == []
    assert not (tmp_path / "state.json").exists()
    for fake in (world.seerr, world.sonarr, world.radarr):
        assert fake.changes() == []


def test_run_is_quiet_while_dry_but_remembers_progress(env, world, tmp_path, capsys):
    assert main(["run"], env) == 0
    assert world.hook.calls == []
    assert "not sent: dry run" in capsys.readouterr().out
    saved = state.load(str(tmp_path / "state.json"))
    assert saved["alerts"] == {}
    assert "tv:77" in saved["queue"]
    assert set(saved["sig"]) == {"1", "2", "3", "4"}


def test_run_alerts_once_then_stays_quiet(env, world, tmp_path):
    live = {**env, "LOMBO_NUDGE_DRY_RUN": "false"}
    assert main(["run"], live) == 0
    assert len(world.hook.calls) == 1
    text = world.hook.calls[0]["body"]["content"]
    assert "Test Movie" in text
    assert "[download_failed]" in text
    assert main(["run"], live) == 0
    assert len(world.hook.calls) == 1  # same reasons: no repeat
    # a request that gets fixed drops its marker, and a new reason alerts again
    saved = state.load(str(tmp_path / "state.json"))
    assert set(saved["alerts"]) == {"1", "2", "3"}
    world.sonarr.routes["/api/v3/queue"] = {"records": [], "totalRecords": 0}
    assert main(["run"], live) == 0
    assert len(world.hook.calls) == 2
    assert "[not_found]" in world.hook.calls[1]["body"]["content"]


def test_run_without_webhook_keeps_alerts_pending(env, world, tmp_path, capsys):
    quiet = {k: v for k, v in env.items() if "WEBHOOK" not in k}
    assert main(["run"], {**quiet, "LOMBO_NUDGE_DRY_RUN": "false"}) == 0
    assert "no webhook set" in capsys.readouterr().out
    assert state.load(str(tmp_path / "state.json"))["alerts"] == {}


def test_realert_after_the_wait(env, world, tmp_path):
    live = {**env, "LOMBO_NUDGE_DRY_RUN": "false"}
    assert main(["run"], live) == 0
    path = str(tmp_path / "state.json")
    st = state.load(path)
    st["alerts"]["1"][1] = time.time() - 8 * 86400
    state.save(path, st)
    assert main(["run"], live) == 0
    assert len(world.hook.calls) == 2
    assert world.hook.calls[1]["body"]["content"].count("Stuck request") == 1


def test_stalled_download_is_noticed_from_remembered_progress(env, world, tmp_path, capsys):
    world.sonarr.routes["/api/v3/queue"] = {
        "records": [{"id": 77, "seriesId": 20, "title": "S", "sizeleft": 500}],
        "totalRecords": 1,
    }
    state.save(str(tmp_path / "state.json"), {**state.empty(), "queue": {"tv:77": [500, time.time() - 20 * 3600]}})
    main(["plan"], env)
    assert "[download_stalled]" in capsys.readouterr().out


def test_quality_and_import_problems(env, world, capsys):
    world.sonarr.routes["/api/v3/queue"] = {
        "records": [
            {
                "id": 77,
                "seriesId": 20,
                "sizeleft": 0,
                "trackedDownloadState": "importBlocked",
                "statusMessages": [{"title": "x", "messages": ["Not an upgrade for existing episode file(s)"]}],
            }
        ],
        "totalRecords": 1,
    }
    main(["plan"], env)
    assert "[quality_blocked]" in capsys.readouterr().out
    world.sonarr.routes["/api/v3/queue"]["records"][0]["statusMessages"] = []
    main(["plan"], env)
    assert "[import_stuck]" in capsys.readouterr().out


def test_imported_but_not_in_jellyfin_or_not_synced(env, world, capsys):
    done = request(1, "movie", 100)
    world.seerr.routes["/api/v1/request"] = lambda q: {"results": [done] if q["skip"] == "0" else []}
    world.radarr.routes["/api/v3/movie"] = [{"id": 10, "tmdbId": 100, "hasFile": True}]
    main(["plan"], env)
    assert "[seerr_not_updated]" in capsys.readouterr().out
    with_jf = {**env, "LOMBO_NUDGE_JELLYFIN_URL": world.jf.url, "LOMBO_NUDGE_JELLYFIN_API_KEY": "good-key"}
    main(["plan"], with_jf)
    assert "[not_in_jellyfin]" in capsys.readouterr().out
    world.jf.routes["/Items"] = {"Items": [{"Id": "a"}]}
    main(["plan"], with_jf)
    assert "[seerr_out_of_sync]" in capsys.readouterr().out


def test_waiting_release_and_4k_and_unconfigured_are_not_flagged(env, world, capsys):
    world.radarr.routes["/api/v3/movie"] = [{"id": 10, "tmdbId": 100, "hasFile": False, "isAvailable": False}]
    only_radarr = {k: v for k, v in env.items() if "SONARR" not in k}
    main(["plan", "--all"], only_radarr)
    out = capsys.readouterr().out
    assert "[waiting_release]" in out
    assert "[not_checked]" in out  # the tv request, no Sonarr
    assert "[download_failed]" not in out


def test_outage_skips_the_pass_instead_of_alerting(env, world, capsys):
    world.sonarr.routes["/api/v3/series"] = 500
    assert main(["run"], {**env, "LOMBO_NUDGE_DRY_RUN": "false"}) == 1
    assert "Sonarr /api/v3/series: HTTP 500" in capsys.readouterr().err
    assert world.hook.calls == []


def test_loop_survives_one_bad_round(env, world, monkeypatch, capsys):
    world.seerr.routes["/api/v1/request"] = 500
    calls = []
    clock = [time.time()]

    def fake_sleep(seconds):
        calls.append(1)
        clock[0] += seconds  # the loop decides from the clock when the next round is due
        if len(calls) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", fake_sleep)
    monkeypatch.setattr(time, "time", lambda: clock[0])
    with contextlib.suppress(KeyboardInterrupt):
        main(["run", "--loop"], env)
    assert capsys.readouterr().err.count("[FAIL]") == 2


def test_doctor_passes_and_fails(env, world, capsys):
    assert main(["doctor"], env) == 0
    out = capsys.readouterr().out
    assert "[FAIL]" not in out
    assert "Seerr API key accepted" in out
    assert "Jellyfin: not set" in out
    bad = {**env, "LOMBO_NUDGE_SEERR_API_KEY": "wrong"}
    assert main(["doctor"], bad) == 1
    assert "Seerr API key accepted: Seerr /api/v1/auth/me: HTTP 401" in capsys.readouterr().out


def test_doctor_covers_jellyfin_arr_errors_and_state(env, world, tmp_path, capsys):
    world.radarr.routes["/api/v3/health"] = [{"type": "error", "message": "Indexers unavailable"}]
    full = {
        **env,
        "LOMBO_NUDGE_JELLYFIN_URL": world.jf.url,
        "LOMBO_NUDGE_JELLYFIN_API_KEY": "good-key",
        "LOMBO_NUDGE_STATE_FILE": str(tmp_path / "f" / "s.json"),
    }
    assert main(["doctor"], full) == 1
    out = capsys.readouterr().out
    assert "Jellyfin reachable: Fake 10.9" in out
    assert "Radarr health: Radarr reports 1 error(s): Indexers unavailable" in out
    (tmp_path / "blocker").write_text("x")  # a file where the folder should be
    blocked = {**full, "LOMBO_NUDGE_STATE_FILE": str(tmp_path / "blocker" / "s.json")}
    assert main(["doctor"], blocked) == 1
    assert "[FAIL] state file" in capsys.readouterr().out


def test_retry_dry_run_shows_the_plan_and_changes_nothing(env, world, capsys):
    assert main(["retry", "2"], env) == 0
    out = capsys.readouterr().out
    assert "remove:" in out
    assert "Show.S01" in out
    assert "nothing was changed" in out
    assert world.sonarr.changes() == []


def test_retry_live_removes_then_cools_down(env, world, tmp_path, capsys):
    live = {**env, "LOMBO_NUDGE_DRY_RUN": "false"}
    assert main(["retry", "2"], live) == 0
    delete = world.sonarr.changes()[0]
    assert (delete["method"], delete["path"], delete["body"]) == ("DELETE", "/api/v3/queue/bulk", {"ids": [77]})
    assert delete["query"]["blocklist"] == "true"
    assert str(2) in state.load(str(tmp_path / "state.json"))["retry"]
    assert main(["retry", "2"], live) == 0
    assert "cooldown:" in capsys.readouterr().out
    assert len(world.sonarr.changes()) == 1


def test_retry_search_for_show_and_movie(env, world):
    live = {**env, "LOMBO_NUDGE_DRY_RUN": "false"}
    world.sonarr.routes["/api/v3/queue"] = {"records": [], "totalRecords": 0}
    assert main(["retry", "2"], live) == 0
    bodies = [c["body"] for c in world.sonarr.changes()]
    assert bodies == [
        {"name": "SeasonSearch", "seriesId": 20, "seasonNumber": 1},
        {"name": "SeasonSearch", "seriesId": 20, "seasonNumber": 2},
    ]
    assert main(["retry", "1"], live) == 0
    assert world.radarr.changes()[0]["body"] == {"name": "MoviesSearch", "movieIds": [10]}


def test_retry_that_would_do_nothing_changes_nothing(env, world, capsys):
    world.sonarr.routes["/api/v3/queue"] = {
        "records": [{"id": 77, "seriesId": 20, "sizeleft": 100}],
        "totalRecords": 1,
    }
    assert main(["retry", "2"], {**env, "LOMBO_NUDGE_DRY_RUN": "false"}) == 0
    assert "moving:" in capsys.readouterr().out
    assert world.sonarr.changes() == []


def test_retry_refuses_what_is_not_stuck(env, world, capsys):
    assert main(["retry", "4"], env) == 1
    assert "not an approved request" in capsys.readouterr().err
    assert main(["retry", "99"], env) == 1
    assert "HTTP 404" in capsys.readouterr().err
    world.seerr.routes["/api/v1/request/2"] = request(2, "tv", 200, 2000, is4k=True)
    assert main(["retry", "2"], env) == 1
    assert "not set up" in capsys.readouterr().err


def test_retry_client_down_and_missing(env, world, capsys):
    world.sonarr.routes["/api/v3/health"] = [{"source": "DownloadClientStatusCheck"}]
    main(["retry", "2"], env)
    assert "client_down:" in capsys.readouterr().out
    world.sonarr.routes["/api/v3/health"] = []
    world.seerr.routes["/api/v1/request/3"] = request(3, "movie", 101)
    main(["retry", "3"], env)
    assert "missing:" in capsys.readouterr().out


def test_state_file_format(env, tmp_path):
    main(["run"], env)
    data = json.loads((tmp_path / "state.json").read_text())
    assert set(data) == {"queue", "sig", "alerts", "retry", "stuck", "declined", "fallback", "tagged", "escalated"}


def _backlog(world, n=40):
    reqs = [request(100 + i, "movie", 500 + i) for i in range(n)]
    world.seerr.routes["/api/v1/request"] = paged(reqs)
    return reqs


def test_long_alert_is_split_and_every_request_is_announced(env, world, tmp_path):
    _backlog(world)
    assert main(["run"], {**env, "LOMBO_NUDGE_DRY_RUN": "false"}) == 0
    assert len(world.hook.calls) > 1
    texts = [c["body"]["content"] for c in world.hook.calls]
    assert all(len(t) <= MAX_ALERT_CHARS for t in texts)
    assert sum(t.count("Stuck request") for t in texts) == 40
    assert len(state.load(str(tmp_path / "state.json"))["alerts"]) == 40


def test_unsent_part_of_a_long_alert_stays_for_next_round(env, world, tmp_path, monkeypatch, capsys):
    _backlog(world)
    sent = []

    def flaky(url, message, timeout=10):
        if sent:
            raise notify.NotifyError("webhook failed: down")
        sent.append(message)

    monkeypatch.setattr(notify, "send", flaky)
    assert main(["run"], {**env, "LOMBO_NUDGE_DRY_RUN": "false"}) == 0
    kept = state.load(str(tmp_path / "state.json"))["alerts"]
    assert len(kept) == sent[0].count("Stuck request") < 40
    monkeypatch.undo()
    assert main(["run"], {**env, "LOMBO_NUDGE_DRY_RUN": "false"}) == 0
    assert len(state.load(str(tmp_path / "state.json"))["alerts"]) == 40


def test_tv_request_without_tvdb_id_is_not_called_missing(env, world, capsys):
    world.seerr.routes["/api/v1/request"] = paged([request(7, "tv", 200)])
    world.seerr.routes["/api/v1/request/7"] = request(7, "tv", 200)
    assert main(["plan", "--all"], env) == 0
    out = capsys.readouterr().out
    assert "[not_checked]" in out
    assert "TVDB" in out
    assert "missing_in_arr" not in out
    assert main(["retry", "7"], env) == 1
    assert "TVDB" in capsys.readouterr().err
    assert not [c for c in world.sonarr.calls if c["query"].get("tvdbId") == "None"]


def test_retry_leaves_other_seasons_alone(env, world, capsys):
    world.sonarr.routes["/api/v3/queue"] = {
        "records": [
            {
                "id": 77,
                "seriesId": 20,
                "seasonNumber": 1,
                "title": "S01",
                "sizeleft": 5,
                "trackedDownloadStatus": "warning",
            },
            {
                "id": 78,
                "seriesId": 20,
                "seasonNumber": 3,
                "title": "S03",
                "sizeleft": 5,
                "trackedDownloadStatus": "warning",
            },
        ],
        "totalRecords": 2,
    }
    assert main(["retry", "2"], {**env, "LOMBO_NUDGE_DRY_RUN": "false"}) == 0
    assert world.sonarr.changes()[0]["body"] == {"ids": [77]}
