# Contracts other programs may rely on

## Contents

- [The findings document](#the-findings-document)
- [The retry calls](#the-retry-calls)
- [Other routes and status codes](#other-routes-and-status-codes)
- [What a version bump means](#what-a-version-bump-means)
- [How a page or panel uses this](#how-a-page-or-panel-uses-this)

Everything below is a promise to other programs (a page script, a dashboard, another Lombo-Forge app). If you change
it, change this file in the same pull request. Nothing here was tried against a real client.

## The findings document

One document, three ways to get it: `lombo-nudge plan --json` (printed, nothing written), the file `findings.json`, and
`GET /v1/findings`.

```json
{
  "version": 1,
  "updated": 1800000000,
  "complete": true,
  "findings": [
    {
      "request_id": 41,
      "title": "Example Movie",
      "requester": "Sam",
      "kind": "movie",
      "state": "searching",
      "reason": "not_found",
      "summary": "No copy has been found yet. It may need a different source or a manual search.",
      "days": 5,
      "can_retry": true,
      "alerted": false
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `version` | Whole number, `1` today. See [what a version bump means](#what-a-version-bump-means). |
| `updated` | Unix time in seconds when the document was made. |
| `complete` | `false` when the request list from Seerr was cut short, so the list may miss requests. |
| `findings` | Only requests that are stuck. Longest waiting first (`days`, high to low), then `request_id`. Empty when nothing is stuck. |
| `request_id` | The Seerr request number. Whole number. |
| `title`, `requester` | As Seerr and the apps name them, with control characters replaced by `?`, at most 200 characters. |
| `kind` | `movie` or `tv`. |
| `state` | A coarse group: `searching` (`not_found`), `missing` (`missing_in_arr`), `stalled` (`download_failed`, `download_stalled`), `blocked` (`quality_blocked`, `import_stuck`), `partly` (`partly_here`), `sync` (`not_in_jellyfin`, `seerr_out_of_sync`, `seerr_not_updated`). |
| `reason` | The app's own reason code, explained in [how-it-works.md](how-it-works.md). Only the stuck codes appear. |
| `summary` | One plain sentence for a non-technical reader, at most 160 characters, one fixed text per `reason`. Show it to people as it is. |
| `days` | Whole days since the request was approved or last changed. |
| `can_retry` | `true` when a retry can do something for this reason (`not_found`, `download_failed`, `download_stalled`). A retry may still be refused at that moment, for example during the cool-down. |
| `alerted` | `true` when an alert for this same reason was already sent and is remembered. Always `false` in dry run. |

**The file** is `findings.json`, in the same folder as `LOMBO_NUDGE_STATE_FILE` (`/data/findings.json` by default). It is
rewritten after every round of `run`, in dry run too: it only describes what exists and changes nothing. It is written
to a temporary name and renamed, so a reader sees the old file or the new one, never half of one. It is readable by
every user on the machine (mode 0644) because it holds titles only. If it cannot be written, the round still
succeeds and prints a warning. Treat a file older than a few rounds (`LOMBO_NUDGE_INTERVAL_SECONDS`) as stale, and a
missing file as "nothing known". A round that fails (a service is down) leaves the old file as it was.

**The API** returns the same document from `GET /v1/findings`, made by a fresh scan at the moment of the request. That
costs the same calls as `plan`, so poll it gently (the file is free).

## The retry calls

Both do what `lombo-nudge retry ID` does, with the same rules and the same cool-down (`LOMBO_NUDGE_RETRY_COOLDOWN_MINUTES`).

`GET /v1/retry/{request_id}` shows what a retry would do and changes nothing:

```json
{"ok": true, "request_id": 2, "would": ["remove: Remove 1 stalled download, blocklist it and search for another copy.", "  - Show.S01"]}
```

`would` is the text `retry ID` prints, one string per line.

`POST /v1/retry` with `Content-Type: application/json` and a body of exactly `{"request_id": 2}` (a whole number from 1 to 1000000000, no other keys):

| Situation | Status | Body |
|---|---|---|
| Dry run (`LOMBO_NUDGE_DRY_RUN=true`) | 200 | `{"ok": true, "message": "dry run: nothing was changed", "would": [...]}` |
| Live, something was done | 200 | `{"ok": true, "message": "Done.", "did": [...]}` |
| Live, nothing needed doing (for example the download is moving) | 200 | `{"ok": true, "message": "Nothing needed doing.", "did": [...]}` |
| Retried too recently | 422 | `{"ok": false, "message": "Retried a moment ago. Give it about 12 minutes before trying again."}` |
| Not retryable: no such request, not approved, already available, no Sonarr or Radarr for it | 422 | `{"ok": false, "message": "..."}` |
| Seerr, Sonarr, Radarr or the state file could not be used | 502 | Generic message; the log has the detail. Nothing was changed. |
| Wrong request: not strict JSON or wrong shape (400), no `Content-Length` (411), body over 4096 bytes (413), not `application/json` (415) | 4xx | `{"ok": false, "message": "..."}` |

`GET /v1/retry/{request_id}` answers 422 and 502 the same way. During the cool-down it answers 200 and `would` holds the
cool-down line, like `retry ID` prints it. In dry run `POST` also answers 422 during the cool-down, as a live call would.

## Other routes and status codes

- Every request needs `Authorization: Bearer <token>`. Missing or wrong is `401` with `{"ok": false, "message": "unauthorized"}`. After 10 bad tries in a minute from one address, `429` with `Retry-After`, even for the right token, until the minute is up.
- An unknown path is `404`, the wrong method on a known path is `405` with an `Allow` header. Both are only answered to a caller with a good token.
- Every answer has `Cache-Control: no-store`. There are no CORS headers and no cookies. How a browser page should reach this is in [security.md](security.md).

## What a version bump means

- **Same version:** a new, optional field may be added to a finding or to the top level, and a new `reason` or `state` value may appear. A reader must ignore fields it does not know.
- **`version` goes up:** a field was removed or renamed, its meaning or type changed, or the sort order changed. A reader that does not know the new number should treat the document as unreadable instead of guessing. The route prefix `/v1` and the document `version` move together; the file has only one shape at a time.

## How a page or panel uses this

1. **Show the list.** Read `findings.json` (free, no token) or call `GET /v1/findings` (fresh, needs the token). Check `version` is a number you know, and `updated` is recent.
2. **Show `summary` to people.** It is plain language. Use `state` to group or colour rows and `reason` if you want your own wording; map them as you like, and ignore values you do not know.
3. **Offer a retry** only where `can_retry` is `true`. Optionally call `GET /v1/retry/{request_id}` first and show `would`.
4. **Retry:** `POST /v1/retry` with `{"request_id": 41}`. Show `message` on 200 and 422 (it is written for people). On 502, say it could not be done right now.
5. **Never put the token in browser code.** Let a server-side part (your own backend or reverse proxy) add the `Authorization` header. See [security.md](security.md).
6. Call it from the same machine or private network; `serve` is not meant for the open internet.
