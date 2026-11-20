"""The pure rules. Rewritten from the cases of the original stuck-request checker with generic data."""

import pytest

from lombo_nudge import core
from lombo_nudge.core import Facts, Queued, RetryItem, Rules, classify


def facts(**kw):
    base = {"request_status": 2, "media_status": 3, "kind": "movie", "age_h": 48, "idle_h": 48, "in_arr": True}
    return Facts(**{**base, **kw})


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"request_status": 1}, "not_approved"),
        ({"request_status": 3}, "not_approved"),
        ({"media_status": 5}, "available"),
        ({"media_status": 6}, "ignored"),
        ({"media_status": 7}, "ignored"),
        ({"in_arr": False}, "missing_in_arr"),
        ({"in_arr": False, "age_h": 2}, "too_new"),
        ({}, "not_found"),
        ({"age_h": 23}, "too_new"),
        ({"released": False}, "waiting_release"),
        ({"queue": (Queued(1, stalled_h=1),)}, "downloading"),
        ({"queue": (Queued(1, stalled_h=13),)}, "download_stalled"),
        ({"queue": (Queued(1, bad=True),)}, "download_failed"),
        ({"queue": (Queued(1, bad=True, finished=True),)}, "import_stuck"),
        ({"queue": (Queued(1, bad=True, finished=True, quality_blocked=True),)}, "quality_blocked"),
        ({"queue": (Queued(1, finished=True),)}, "downloading"),
        ({"kind": "tv", "wanted": 0}, "no_episode_list"),
        ({"kind": "tv", "wanted": 10}, "not_found"),
        ({"kind": "tv", "wanted": 10, "files": 4}, "too_new"),
        ({"kind": "tv", "wanted": 10, "files": 4, "age_h": 100, "idle_h": 100}, "partly_here"),
        ({"kind": "tv", "wanted": 10, "files": 4, "age_h": 100, "idle_h": 10}, "too_new"),
        ({"files": 1}, "seerr_not_updated"),
        ({"files": 1, "age_h": 3}, "too_new"),
        ({"files": 1, "in_jellyfin": False}, "not_in_jellyfin"),
        ({"files": 1, "in_jellyfin": True}, "seerr_out_of_sync"),
        ({"kind": "tv", "wanted": 3, "files": 3, "in_jellyfin": False}, "not_in_jellyfin"),
    ],
)
def test_classify(kw, code):
    assert classify(facts(**kw)).code == code


def test_a_queued_download_beats_everything_below_it():
    assert classify(facts(released=False, queue=(Queued(1),))).code == "downloading"
    assert classify(facts(in_arr=False, queue=(Queued(1),))).code == "missing_in_arr"


def test_rules_are_adjustable():
    assert classify(facts(age_h=30), Rules(search_after_h=48)).code == "too_new"
    assert classify(facts(queue=(Queued(1, stalled_h=3),)), Rules(stall_after_h=2)).code == "download_stalled"


def test_every_reason_has_text_and_stuck_flag_matches():
    for code, (stuck, text) in core.REASONS.items():
        assert text
        assert core.Verdict(code, text).stuck is stuck


def test_parse_ts():
    assert core.parse_ts("2026-01-01T00:00:00Z") == core.parse_ts("2026-01-01T00:00:00+00:00")
    assert core.parse_ts("2026-01-01T00:00:00") == core.parse_ts("2026-01-01T00:00:00Z")
    assert core.parse_ts("nope") is None


def test_track_remembers_when_a_value_last_changed():
    first = core.track({}, {"a": 10, "b": 5}, now=100)
    second = core.track(first, {"a": 10, "b": 4}, now=200)
    assert second == {"a": [10, 100], "b": [4, 200]}
    assert core.track(second, {"a": 10}, now=300) == {"a": [10, 100]}


def test_should_alert_once_per_change_then_again_after_the_wait():
    assert core.should_alert(None, "not_found", 1000, 100)
    assert not core.should_alert(["not_found", 950], "not_found", 1000, 100)
    assert core.should_alert(["not_found", 800], "not_found", 1000, 100)
    assert core.should_alert(["not_found", 999], "download_failed", 1000, 100)
    assert not core.should_alert(None, "downloading", 1000, 100)


def test_alert_text_names_title_person_reason_and_days():
    text = core.alert_text("Test Movie", "Sam", core.Verdict("not_found", "Nothing found."), 3)
    assert "Test Movie" in text
    assert "Sam" in text
    assert "[not_found]" in text
    assert "3 days" in text
    assert "1 day." in core.alert_text("X", "Y", core.Verdict("not_found", "m"), 1)


def retry(**kw):
    base = {"app": "Radarr", "in_arr": True, "cooldown_left_s": 0, "client_down": False, "queue": [], "seasons": []}
    return core.plan_retry(**{**base, **kw})


def test_plan_retry_actions_in_order():
    assert retry(cooldown_left_s=300, client_down=True).action == "cooldown"
    assert retry(client_down=True).action == "client_down"
    assert retry(in_arr=False).action == "missing"
    stuck = retry(queue=[RetryItem(5, "A", sizeleft=10, bad=True), RetryItem(6, "B", sizeleft=10, stalled=True)])
    assert (stuck.action, stuck.ids, stuck.releases) == ("remove", (5, 6), ("A", "B"))
    assert retry(queue=[RetryItem(5, sizeleft=0, bad=True)]).action == "manual"
    assert retry(queue=[RetryItem(5, sizeleft=10)]).action == "moving"
    s = retry(seasons=[2, 1, 1])
    assert s.action == "search"
    assert "seasons 1, 2" in s.message
    assert "season 1" in retry(seasons=[1]).message
    assert "(" not in retry().message
