---
name: adapt
description: Change LomboNudge's rules, reason codes, thresholds, settings or alerts without breaking it. Use when the user wants different behaviour, a new reason, a new setting, a new Seerr/Sonarr/Radarr/Jellyfin call or a different alert target.
---

# Adapt LomboNudge

1. **Change a rule or add a reason:** edit `classify()` and `REASONS` in `core.py`. Write the test first in
   `tests/test_core.py` with plain data, watch it fail, then change the code. Add a row to the table in
   `docs/how-it-works.md` (a test fails until every code is documented). Keep `core.py` free of network, disk and
   clock; pass what it needs in through `Facts`.
2. **Add a setting:** read and validate it in `config.py` (the error must name the variable), add the name to
   `SETTINGS`, add a commented line to `.env.example` (a test keeps them in sync), and add a bad-value case in
   `tests/test_app.py`.
3. **Add a service call:** add a method to the client in `clients.py` or `jellyfin.py`, a route to the fake in
   `tests/conftest.py`, and a test. Confirm the call against a throwaway instance before relying on it. Calls that
   change things belong in `retry.py` only and run only when `DRY_RUN=false`.
4. **Change alerts:** `notify.py` posts JSON (`content` and `text`) to a webhook. For a service needing a different
   body, put a small bridge in front, or add a second function and keep `send()` as is.
5. **Run:** `pytest -q --cov`, `ruff check .`, `ruff format --check .`, `mypy`, `sh scripts/leak-check.sh`. All must pass.
6. **Stay safe:** `DRY_RUN` must still default to true, `plan` must stay read-only, and a failed read of Seerr,
   Sonarr or Radarr must still skip the whole round (never alert on partial data).
