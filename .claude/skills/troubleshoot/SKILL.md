---
name: troubleshoot
description: Diagnose LomboNudge problems from doctor output and plan output. Use when the app fails to start, doctor shows FAIL, alerts do not arrive, nothing is reported, or a report looks wrong.
---

# Troubleshoot LomboNudge

Start with `lombo-nudge doctor`; each line says what failed. The full table of lines and fixes is in
`docs/troubleshooting.md`; read it rather than guessing. Then `lombo-nudge plan --all` shows every request with its
reason code; `docs/how-it-works.md` explains each code.

Rules of thumb:

- A reason code is the program doing its job. Explain it; do not lower a threshold to hide a report.
- A round that prints `[FAIL]` on Seerr, Sonarr or Radarr reports nothing on purpose (outage safety). Fix that service.
- Stalls are only seen after two rounds; the state file (`LOMBO_NUDGE_STATE_FILE`) holds the clocks. Deleting it is safe.
- `DRY_RUN=true` means no alerts and no retries. Check that first when "nothing arrives".
- `retry` can remove and blocklist downloads: show the dry-run plan and get a yes before applying it.
- When the user reports a bug, ask for `doctor` output with hostnames, addresses and keys removed.
