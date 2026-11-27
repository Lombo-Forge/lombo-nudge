"""4K movie requests go to the second Radarr, with their own Seerr status."""

import time

from conftest import Fake, paged, request
from lombo_nudge import cli
from lombo_nudge.config import from_env


def test_a_4k_request_is_checked_and_retried_on_the_4k_radarr(world, env):
    four_k = Fake()
    req = request(9, "movie", 100, is4k=True)
    req["media"] |= {"status": 5, "status4k": 3}  # the HD copy is there, the 4K one is not
    world.seerr.routes["/api/v1/request"] = paged([req])
    world.seerr.routes["/api/v1/request/9"] = req
    four_k.routes["/api/v3/movie"] = [{"id": 30, "tmdbId": 100, "hasFile": False, "isAvailable": True}]
    four_k.routes["/api/v3/queue"] = {"records": [], "totalRecords": 0}
    four_k.routes["/api/v3/health"] = []
    try:
        off = cli.findings_doc(from_env(env), time.time())
        assert off["findings"] == []  # no 4K Radarr set: skipped, as before
        cfg = from_env(
            env
            | {
                "LOMBO_NUDGE_RADARR_4K_URL": four_k.url,
                "LOMBO_NUDGE_RADARR_4K_API_KEY": "good-key",
                "LOMBO_NUDGE_DRY_RUN": "false",
            }
        )
        assert [f["reason"] for f in cli.findings_doc(cfg, time.time())["findings"]] == ["not_found"]
        assert cli.retry_once(cfg, 9, time.time()).changed
        assert [c["body"] for c in four_k.changes()] == [{"name": "MoviesSearch", "movieIds": [30]}]
        assert world.radarr.changes() == []
    finally:
        four_k.server.shutdown()
