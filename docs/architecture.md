# How it is built

## Contents

- [The shape](#the-shape)
- [Contracts](#contracts)
- [Why it is split this way](#why-it-is-split-this-way)
- [Safe by default](#safe-by-default)
- [Settings](#settings)
- [Adding a reason or a service call](#adding-a-reason-or-a-service-call)

## The shape

```
Seerr    --\
Sonarr   ---+-- clients.py / jellyfin.py (on http.py) --> scan.py --> core.classify()
Radarr   --/                                                |
Jellyfin (optional) ----------------------------------------+
                                                            v
                         cli.py  --> notify.py --> your webhook
                            |   \--> retry.py --> core.plan_retry() --> Sonarr/Radarr
                            \------> state.py (JSON file)
```

## Contracts

| Module | Gets | Returns | Touches |
|---|---|---|---|
| `core.py` | `Facts` (plain data), `Rules` | `Verdict(code, message)`; `RetryPlan`; alert text; `track()` and `should_alert()` helpers | nothing: no network, disk or clock |
| `http.py` | method, path, params, body | parsed JSON, or `ApiError("<service> <path>: <reason>")` | network |
| `clients.py` | settings | `Seerr`, `Arr` (Sonarr/Radarr API v3) | network; only `Arr.delete_queue()` and `Arr.command()` change anything |
| `jellyfin.py` | provider id | `True`/`False` for "Jellyfin lists it" | network, read-only |
| `scan.py` | clients, state, `now` | `Scan(findings, queue, sig, complete)` | network, read-only |
| `retry.py` | request id | `Prepared` plan; `apply()` carries it out | network; changes only when `cli.py` calls `apply()` |
| `state.py` | a path | the state dict | one JSON file |
| `notify.py` | url, text | nothing, or `NotifyError` | one webhook POST |
| `cli.py` | argv, environment | exit code | the only place that prints, reads the clock and decides to change things |

`scan` and `retry` never see the clock: `cli.py` passes `now`.

## Why it is split this way

Rules change most and break most quietly. `classify()` takes plain data and returns plain data, so a
test is two lines. The I/O layers are thin and are tested against fake services on localhost
(`tests/conftest.py`).

## Safe by default

- `DRY_RUN` is true until set to false. In dry run, no alert is sent and no retry changes anything.
- `plan` (alias `check`) never sends, changes or writes anything.
- `run` writes only its own state file, even in dry run (so stalls can be measured), but never marks an alert as sent unless it was.
- If Seerr, Sonarr or Radarr cannot be read, the round is skipped.
- A bad setting stops the app at start-up and names the variable.
- In `--loop` mode one failed round is printed and the next one still runs.

## Settings

Every setting is an environment variable with the `LOMBO_NUDGE_` prefix, validated in `config.py`.
To add one: read it in `config.py` with a clear error, add it to `SETTINGS`, add a line to
`.env.example` (a test keeps the three in sync), and add a test.

## Adding a reason or a service call

New reason: add it to `REASONS` in `core.py`, a branch in `classify()`, a row in
`docs/how-it-works.md` (a test fails until every code is documented) and a case in `tests/test_core.py`.
New call: add a method to the client class, a route to the fake in `tests/conftest.py`, and test it.
Check a new call against a throwaway instance of the service before relying on it.
