# Troubleshooting

## Contents

- [Start with doctor](#start-with-doctor)
- [Doctor lines](#doctor-lines)
- [Nothing is reported](#nothing-is-reported)
- [Too many or wrong reports](#too-many-or-wrong-reports)
- [Alerts do not arrive](#alerts-do-not-arrive)
- [Retry does nothing](#retry-does-nothing)

## Start with doctor

`lombo-nudge doctor` checks every configured service and prints one line each. Remove hostnames,
addresses and keys before sharing the output.

## Doctor lines

| Line | Meaning and fix |
|---|---|
| `[FAIL] settings: LOMBO_NUDGE_...` | A setting is missing or invalid; the message names it. |
| `Seerr reachable` fails | Wrong URL, or Seerr is down. From the container, `http://seerr:5055` needs a shared network. |
| `Seerr API key accepted: HTTP 401` | Wrong key. Copy it from Seerr: Settings > General. |
| `Sonarr/Radarr answers with this key: HTTP 401` | Wrong key for that app. |
| `Sonarr/Radarr health: reports N error(s)` | The app itself has a problem (indexers, download client). Fix that first; LomboNudge will otherwise report downstream effects. |
| `Jellyfin reachable` ok but `API key accepted` fails | The key was refused; create one in Dashboard > API Keys. |
| `state file: cannot write` | Mount a writable folder at `/data` (`chown 999`) or change `LOMBO_NUDGE_STATE_FILE`. |

## Nothing is reported

- Run `plan --all`. If every request is `too_new`, they are still inside the waiting window.
- `not_checked` means that request's app is not configured (a 4K movie needs `LOMBO_NUDGE_RADARR_4K_URL`; 4K shows are skipped).
- Stalls need two rounds: run `run` once, wait, run again.
- A pass with an error prints `[FAIL]` and reports nothing, on purpose. Fix the service it names.

## Too many or wrong reports

- `missing_in_arr` for everything: Seerr and Sonarr/Radarr disagree about ids (check the app link in Seerr).
- `seerr_not_updated` for everything: Seerr's library sync is not running. Add Jellyfin to see the difference.
- `not_in_jellyfin` although the title is there: Jellyfin could not match by provider id. The lookup is unverified against every server version; remove the Jellyfin settings to fall back to `seerr_not_updated`.
- `download_stalled` right after a restart: delete the state file to restart the clocks.

## Alerts do not arrive

Is `LOMBO_NUDGE_DRY_RUN` still `true`? Is `LOMBO_NUDGE_WEBHOOK_URL` set? `run` prints "alert(s) not sent" and why.
A webhook error shows as `[FAIL] webhook failed: ...`; the alert is retried next round because it was not marked as sent.

## Retry does nothing

`retry ID` prints the action first. `moving` and `manual` change nothing by design; `client_down` and `cooldown`
wait. Without `DRY_RUN=false` it only shows the plan.
