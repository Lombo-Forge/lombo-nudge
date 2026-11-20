---
name: install-into-my-stack
description: Help a user install LomboNudge next to their own Seerr, Sonarr and Radarr and verify it with doctor. Use when someone asks to set it up, install it, or add it to their compose file or server.
---

# Install LomboNudge into the user's setup

Ask one question at a time and use the answers; never assume a layout.

1. **Which of Sonarr and Radarr do they run?** At least one is needed. Is Seerr connected to them already?
2. **How do the services reach each other?** Same compose project (container names work, for example
   `http://seerr:5055`), another project (join its network, see `compose.example.yaml`), or addresses on the LAN.
3. **Where are the API keys?** Seerr, Sonarr and Radarr: Settings > General. Optionally Jellyfin: Dashboard > API
   Keys (a new key just for this app). Never print or commit keys.
4. **Write `.env`** from `.env.example` (git-ignored). Keep `LOMBO_NUDGE_DRY_RUN=true`. Create the data folder and
   make it writable by uid 999.
5. **Run `lombo-nudge doctor`**, fix every `[FAIL]` (see `troubleshoot`), then `lombo-nudge plan --all` and walk
   through what it finds with them. Each reason code is explained in `docs/how-it-works.md`.
6. **Alerts:** ask for a webhook (Discord, Slack, or a bridge). Only after `plan` looks right, set `DRY_RUN=false`.
7. **Retry** (`lombo-nudge retry ID`) can remove and blocklist downloads. Show the dry-run plan first and only apply
   it for a request they pick.

Say plainly that this has not been tested against every Seerr/Sonarr/Radarr version; `doctor` checks the basics.
