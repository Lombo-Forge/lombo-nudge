# How it works

## Contents

- [The loop](#the-loop)
- [Reason codes](#reason-codes)
- [Which one wins](#which-one-wins)
- [Alerts](#alerts)
- [Retry](#retry)
- [What it remembers](#what-it-remembers)

## The loop

Each round (`plan`, `run`, or every `LOMBO_NUDGE_INTERVAL_SECONDS` with `run --loop`):

1. Ask Seerr for every approved request (pages of 100, at most 5000).
2. Ask Sonarr and Radarr for their titles and download queues.
3. For each request, gather plain facts: how old the approval is, how many episode files exist against
   how many are wanted, whether the movie is released, which queue items belong to it, and how long the
   queue item and the file count have been unchanged.
4. `core.classify()` turns the facts into one reason code.
5. Codes marked "stuck" are listed by `plan` and announced by `run`.

If Seerr, Sonarr or Radarr cannot be read, the whole round is skipped with an error message. An outage
never becomes a pile of false alarms, and nothing is forgotten because of a partial read.

## Reason codes

Codes marked **stuck** are problems. The others are normal states, shown only with `plan --all`.

| Code | Stuck | Meaning and what to do |
|---|---|---|
| `not_approved` | no | The request is pending or declined. Approve it in Seerr. |
| `available` | no | Seerr says it is available. Nothing to do. |
| `ignored` | no | Seerr has it blocklisted or deleted. |
| `not_checked` | no | The Sonarr or Radarr this request needs is not configured, or it is a 4K request (see the README), or Seerr gave no TVDB id for a show so it cannot be matched in Sonarr. |
| `too_new` | no | Approved less than `LOMBO_NUDGE_SEARCH_AFTER_HOURS` ago, or a show with some episodes that is not yet old enough to worry about. |
| `downloading` | no | A download is in the queue and moving (or finished and waiting for its normal import). |
| `waiting_release` | no | The movie is not released yet (Radarr `isAvailable` is false). Nothing can be found before then. |
| `no_episode_list` | no | Sonarr knows no episodes for the request yet. Common for new anime; refresh the series in Sonarr. |
| `not_found` | **yes** | Approved longer ago than the search window, nothing queued, no files: the indexers found nothing. Check indexers and the quality profile, or try `retry`. |
| `missing_in_arr` | **yes** | Seerr approved it but Sonarr or Radarr does not have the title. Seerr's link to the app may be broken; check Seerr's settings for that app. |
| `download_failed` | **yes** | A queued download failed, or Sonarr/Radarr marks it with a warning or error. `retry` removes it, blocklists it and searches again. |
| `download_stalled` | **yes** | A queued download has not made progress for `LOMBO_NUDGE_STALL_AFTER_HOURS`. Needs at least two rounds (it measures from the first time it saw the same remaining size). |
| `quality_blocked` | **yes** | A finished download was refused for quality reasons (the app's own message mentions quality or "not an upgrade"). Raise the profile cutoff or import by hand. |
| `import_stuck` | **yes** | A download finished but was never imported (blocked or failed import). Use Manual Import in Sonarr or Radarr; `retry` never removes it. |
| `partly_here` | **yes** | Some requested episodes exist, the rest are missing, and the count has not changed for `LOMBO_NUDGE_PARTLY_AFTER_HOURS`. |
| `not_in_jellyfin` | **yes** | All files are imported, but Jellyfin (asked by provider id) does not list the title. Scan the library; check the library folder and metadata. Only with Jellyfin set. |
| `seerr_out_of_sync` | **yes** | The files are in Jellyfin but Seerr still shows the request as not available. Run the Jellyfin sync job in Seerr. Only with Jellyfin set. |
| `seerr_not_updated` | **yes** | The files are imported and Seerr says not available, with no Jellyfin to confirm either way. |

"Quality blocked" is detected only from the text of the queue message. A release that was never grabbed
because of the quality profile shows up as `not_found`; the indexer search results are not inspected.

## Which one wins

The first match in this order is the answer:

1. Request not approved, then available, then blocklisted or deleted.
2. Title not in Sonarr/Radarr: `missing_in_arr` (or `too_new`).
3. Queue items: a finished and bad item gives `quality_blocked` or `import_stuck`; a bad unfinished item
   gives `download_failed`; no progress gives `download_stalled`; otherwise `downloading`.
4. Movie not released: `waiting_release`.
5. No episodes known: `no_episode_list`.
6. Some but not all files: `partly_here` (or `too_new`).
7. No files: `not_found` (or `too_new`).
8. All files present: `not_in_jellyfin`, `seerr_out_of_sync` or `seerr_not_updated` (or `too_new`).

## Alerts

`run` sends one webhook message per round that lists the requests with a new reason. The same request
with the same reason is announced again only after `LOMBO_NUDGE_REALERT_DAYS`. A request that is fixed
is forgotten, so a later problem is announced again. With `DRY_RUN=true` (the default) nothing is sent
and nothing is marked as sent.

## Retry

`lombo-nudge retry ID` looks at one stuck request and shows what it would do. With `DRY_RUN=false` it
does it. The possible actions:

| Action | Meaning |
|---|---|
| `remove` | Failed or stalled downloads with data left: remove from the download client, blocklist, search for another copy. |
| `search` | Nothing queued: ask Sonarr (the requested seasons, at most 6) or Radarr to search again. |
| `manual` | A finished download that did not import: never removed, use Manual Import. |
| `moving` | Downloading normally: nothing to do. |
| `missing` | The app no longer has the title. |
| `client_down` | Sonarr/Radarr report the download client as unreachable: nothing is changed, so good downloads are not blocklisted. |
| `cooldown` | Retried within `LOMBO_NUDGE_RETRY_COOLDOWN_MINUTES`. |

Retry only works on requests that are approved and not yet available. For a show, only queue entries for the
requested seasons are touched when Sonarr says which season an entry is for; an entry with no season number
(often a single episode) cannot be told apart and is included, which is one more reason to read the dry run first.
Request age comes from Seerr's `updatedAt` (it has no separate approval time), so editing a request restarts its wait.

## What it remembers

One JSON file (`LOMBO_NUDGE_STATE_FILE`): when each queue item last changed size, when each request's
file count last changed, which alert was sent, and the last retry time. Delete it any time; it only
means stalls are measured from the next round and alerts may repeat once. `plan` reads it but never
writes it.
