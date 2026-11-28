# Security notes

## Contents

- [What `serve` is](#what-serve-is)
- [What it trusts](#what-it-trusts)
- [What it refuses](#what-it-refuses)
- [What it never logs](#what-it-never-logs)
- [Reaching it from a web page](#reaching-it-from-a-web-page)
- [Limits of this design](#limits-of-this-design)
- [What it changes](#what-it-changes)
- [The files it writes](#the-files-it-writes)

This is the author's own threat model for the optional HTTP API (`lombo-nudge serve`). There has been no
independent review, and it was never run against a real reverse proxy or a real browser.

## What `serve` is

A small server built on Python's `http.server`, started only when you run `lombo-nudge serve`. It listens on
`LOMBO_NUDGE_API_BIND` (default `127.0.0.1:8095`), speaks plain HTTP (no TLS: put it behind a proxy that does TLS) and
has three routes: `GET /v1/findings`, `GET /v1/retry/{request_id}` and `POST /v1/retry`. The only thing it can change
is a retry: the same removal of a failed download (with a blocklist entry) or search that `lombo-nudge retry` does,
and only when `LOMBO_NUDGE_DRY_RUN=false`. It cannot delete requests or library files.

## What it trusts

- **The token.** Whoever holds `LOMBO_NUDGE_API_TOKEN` can ask for a retry of any stuck request. The token proves *which program* is calling, not *which person* is behind it.
- **The network position.** A loopback bind trusts only programs on the same machine (or in the same container). Any other bind trusts everything that can reach that network.
- **The reverse proxy**, if there is one: it must set the `Authorization` header itself and overwrite any value the visitor sent.
- **Seerr, Sonarr, Radarr and the state folder**, exactly as the rest of the app does.

## What it refuses

- Starting without a token, or with one shorter than 24 characters, or with spaces or non-ASCII characters. A bind that is not an IP address and a port is a start-up error.
- Every request without the right `Authorization: Bearer` header (`401`, no detail). The token is compared in constant time.
- More than 10 failed tries a minute from one address (`429`); the limit is checked before the token is, so it slows a guesser who happens to be right too. The table of addresses is capped at 1024.
- Any path other than the routes (`404`) and any other method (`405`, with `Allow`), after the token is checked, so a stranger learns nothing about the routes.
- A body over 4096 bytes, a missing `Content-Length` (no chunked bodies), a content type other than `application/json`, anything but strict JSON of exactly the shape `{"request_id": <whole number>}`: duplicate keys, `NaN`, extra keys, a string, a boolean, a fraction or a number outside 1 to 1000000000.
- Requests that stall: a connection that does not finish its request in 10 seconds is dropped.
- More than one retry at a time: retries wait for each other, and for a running `run` (a lock file, `nudge.lock`, next to the state file). The retry cool-down is read after the wait, so two calls at once cannot both act.
- To change anything in dry run: a retry says what it would do and changes nothing.
- CORS: no `Access-Control-*` header is ever sent, and a browser's preflight request (`OPTIONS`) is not answered, so a web page on another site cannot call it directly.
- Detail about failures: an answer to a caller for an upstream or file problem is a generic sentence (`502`, or `500` for a bug). The detail is only in the log.

## What it never logs

It prints one line per request to standard error: the client address, the method, the route shape (the request
number is replaced by `{id}`, any query string is dropped, cleaned and 30 characters at most) and the status. Never the
`Authorization` header or any other header, never a request body, never the token, never a title or a requester's name.
An upstream error is logged by its message (which names the failing call, such as `Sonarr /api/v3/series: HTTP 500`,
not a key); an unexpected error only by its type.

## Reaching it from a web page

Never put the token in browser code: anyone who opens the page can read it. Instead:

1. Keep `serve` on a private network, not published on the host.
2. Let your own backend or reverse proxy expose only the routes you need under a path of its own, and have **it** add the `Authorization` header.
3. The proxy replaces any `Authorization` header the visitor sends, so a visitor cannot try tokens.

If the API is not behind a proxy that overwrites the header, a visitor can send wrong tokens, and the failure limit then locks out that address (and, behind a proxy that hides addresses, everybody).

## Limits of this design

- **The token authenticates the proxy, not the viewer.** Once the proxy adds the token for a path, anyone who can open that path can trigger retries. The cool-down limits it to one retry per request per `LOMBO_NUDGE_RETRY_COOLDOWN_MINUTES`, but a retry blocklists real downloads. Put the path behind a login, or a network only you can reach.
- **No TLS, no sessions, no per-person identity.** A retry is not attributed to anyone.
- **A non-loopback bind.** Inside a container you must bind `0.0.0.0` for another container to reach it; `doctor` and `serve` then print a warning. That is acceptable only on a private container network with no published port.
- **IPv6** binds (`[::1]:8095`) are supported by the code but were not tried.
- **The failure limit is per address**, so it cannot stop a distributed guess. A 32-character random token (`openssl rand -hex 32`) is what protects you.
- **Changing the token** means restarting `serve` and updating whatever adds it.
- Python's `http.server` is not built for the open internet. Never publish this port.
- The lock file is an advisory `flock`: it works on a local disk or a normal Docker volume, and may not on some network file systems.

## What it changes

Only with `DRY_RUN=false`: `retry` removes and blocklists stalled downloads and starts searches; `DECLINE_AFTER_DAYS` deletes requests in Seerr; `FALLBACK_TAG` adds and removes one tag on titles in Sonarr and Radarr and starts searches. Nothing else.

## The files it writes

`state.json` (progress, sent alerts, retry times) is written with the process default mode and `nudge.lock` with mode 0600; `findings.json` is written with mode 0644 on purpose, because it holds titles only and other programs read it (see [contracts.md](contracts.md)). Neither holds the token or any service key. `state.json` and `findings.json` are replaced atomically (temporary name, rename).
