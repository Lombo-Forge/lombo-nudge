# Setup

## Contents

- [What you need](#what-you-need)
- [Steps](#steps)
- [Choosing when it speaks](#choosing-when-it-speaks)
- [Webhooks](#webhooks)
- [Running without Docker](#running-without-docker)
- [For a page or another tool (optional)](#for-a-page-or-another-tool-optional)

## What you need

- Seerr with at least one approved request, and its API key (Settings > General).
- Sonarr and/or Radarr, with their API keys (Settings > General).
- Optional: Jellyfin and an API key (Dashboard > API Keys), to tell "imported but missing from the library" from "Seerr not updated".
- Optional: a webhook URL (Discord, Slack, or a bridge such as Apprise for ntfy, Gotify or email).

LomboNudge only needs to reach those services over HTTP. Put it on the same Docker network (see `compose.example.yaml`).

## Steps

1. Copy `.env.example` to `.env` and fill in the URLs and keys for the services you use.
2. Make the data folder writable by the container user: `mkdir data && chown 999 data`.
3. `docker compose -f compose.example.yaml up -d`.
4. `docker compose -f compose.example.yaml exec lombo-nudge lombo-nudge doctor` and fix every `[FAIL]`.
5. `... exec lombo-nudge lombo-nudge plan` to see what it finds. Nothing is sent or changed.
6. Set `LOMBO_NUDGE_DRY_RUN=false` and restart to start sending alerts.

## Choosing when it speaks

The defaults wait a day before reporting "nothing found" and three days before "partly here". Raise
`LOMBO_NUDGE_SEARCH_AFTER_HOURS` if releases often take longer to appear. A stalled download needs two
rounds to be seen, so keep `LOMBO_NUDGE_INTERVAL_SECONDS` well under `LOMBO_NUDGE_STALL_AFTER_HOURS`.

## Webhooks

The message is posted as JSON with the text under both `content` (Discord) and `text` (Slack and
compatible). Send to ntfy, Gotify or email through a bridge. No service is built in.

## Running without Docker

```sh
pip install .
export LOMBO_NUDGE_SEERR_URL=http://localhost:5055 ...   # see .env.example
lombo-nudge doctor
lombo-nudge run --loop
```

Set `LOMBO_NUDGE_STATE_FILE` to a path you can write.

## For a page or another tool (optional)

Set `LOMBO_NUDGE_API_TOKEN` (`openssl rand -hex 32`) and run `lombo-nudge serve` next to `run --loop`. It listens on
`LOMBO_NUDGE_API_BIND`. The findings are also written to `findings.json` beside the state file after every round,
with no token needed to read it. The shape of both and the retry call are in [contracts.md](contracts.md); read
[security.md](security.md) before exposing it to anything beyond the same machine.
