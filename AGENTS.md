# AGENTS.md

Notes for an AI coding agent (or a person) working in this repo. `CLAUDE.md` pulls this file in.

## Contents

- [What this is](#what-this-is)
- [Layout](#layout)
- [Rules](#rules)
- [Test](#test)
- [Working efficiently](#working-efficiently)
- [Skills](#skills-claudeskills)

## What this is

LomboNudge is a small Python 3.12 program that runs beside Seerr, Sonarr and Radarr (and optionally Jellyfin). It
finds requests that were approved but never arrived, says why with a reason code, and sends webhook alerts.
`retry` can remove and blocklist stalled downloads, so **never alert or change on partial data**.
Standard library only (add a dependency only when it clearly earns its place).

## Layout

| Path | What |
|---|---|
| `src/lombo_nudge/core.py` | The decisions. Pure: no network, disk or clock. `classify()`, `REASONS`, `plan_retry()`, `track()`, `should_alert()`. |
| `src/lombo_nudge/http.py` | The one JSON HTTP client and `ApiError`. |
| `src/lombo_nudge/clients.py` | `Seerr` and `Arr` (Sonarr/Radarr API v3). |
| `src/lombo_nudge/jellyfin.py` | Optional: does Jellyfin list the title. |
| `src/lombo_nudge/scan.py` | Read-only: gathers facts and calls `classify()`. |
| `src/lombo_nudge/retry.py` | Prepares and applies a retry (the only code that changes Sonarr/Radarr). |
| `src/lombo_nudge/state.py` | The JSON state file. |
| `src/lombo_nudge/config.py` | Settings from `LOMBO_NUDGE_*` environment variables. |
| `src/lombo_nudge/notify.py` | Webhook alerts. |
| `src/lombo_nudge/cli.py` | `doctor`, `plan`/`check`, `run`, `retry`. The only place that prints or reads the clock. |
| `tests/` | pytest, with fake Seerr, Sonarr, Radarr, Jellyfin and webhook servers in `conftest.py`. |
| `scripts/leak-check.sh` | Fails when private or personal strings appear. CI runs it. |
| `docs/` | One topic per file, each with a `## Contents` list. |

## Rules

- **Keep `core.py` pure.** If a rule needs data, pass it in through `Facts`.
- **Safe by default.** `DRY_RUN` defaults to true. `plan` never sends, changes or writes. `retry` changes things only with `DRY_RUN=false`.
- **An unreadable Seerr, Sonarr or Radarr skips the whole round**; a cut-off request list never clears stored alerts.
- **A new reason** goes in `REASONS`, `classify()`, `docs/how-it-works.md` (a test enforces it) and `tests/test_core.py`.
- **A new setting** goes in `config.py` (validated, message names the variable), `SETTINGS`, `.env.example` (a test keeps them in sync), and `compose.example.yaml` if it needs passing through.
- **Never put private details in the repo:** real hostnames, IPs, webhook URLs, keys, local paths. Use `seerr.example.com`, `/mnt/media`, `changeme`. `scripts/leak-check.sh` enforces it; patterns live in `scripts/leak-patterns.txt`.
- Plain language in docs and messages. Never imply the Jellyfin, Seerr, Sonarr or Radarr projects made this.
- The Lombo look (every icon has the Lombo L) is in `docs/branding.md`.

## Test

```sh
pip install -e '.[dev]'
pre-commit install            # once: runs the checks below before every commit
pre-commit run --all-files    # ruff lint + format, strict mypy, file hygiene, leak check
pytest -q --cov               # tests with coverage (the gate is `fail_under` in pyproject.toml)
```

These are the same checks CI runs. Do not loosen a rule to make a change pass; fix the code, or say why in a
comment next to a narrow `noqa`.

Without Python installed: `docker run --rm -v "$PWD:/w" -w /w python:3.12-slim sh -c "pip install -q -e '.[dev]' && pytest -q"`.
Tests use fake servers; check a new API call against a throwaway instance of the real service, never someone's real one.

## Working efficiently

- Optional tools that live in your own profile, not in this repo. Skip any you do not have.
- Graphify: build the code-only graph with `graphify update .` (never `/graphify .`, which costs tokens) and run
  `graphify query "<question>"` before grepping. `graphify-out/` is generated and gitignored; INFERRED edges are guesses.
- Ponytail: the smallest change that fully solves the task. Keep diffs small and focused.

## Skills (`.claude/skills/`)

- `install-into-my-stack`: connect it to Seerr, Sonarr and Radarr and run `doctor`.
- `troubleshoot`: read `doctor` and `plan` output.
- `adapt`: change reasons, thresholds or settings safely.
- `ship-a-change`, `release`: get a change into `main`; cut a release.
