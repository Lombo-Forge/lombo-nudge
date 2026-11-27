"""Command line: doctor, plan (alias check), run, retry. Talks to the outside world; core.py decides.

lombo-nudge doctor          check the settings and that every service answers; exit 1 on any failure
lombo-nudge plan [--all|--json]  list stuck requests and why; changes nothing, ever (alias: check)
lombo-nudge run [--loop]    same, then send alerts (only when DRY_RUN=false); --loop repeats every INTERVAL_SECONDS
lombo-nudge retry ID        show what a retry of request ID would do; with DRY_RUN=false, do it
lombo-nudge serve           small HTTP API: GET /v1/findings, GET and POST /v1/retry (needs API_TOKEN)
"""

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from typing import Any

from . import __version__, api, core, findings, notify, retry, state
from .clients import Arr, Seerr
from .config import Config, ConfigError, from_env, is_loopback, parse_bind, token_problem
from .http import ApiError
from .jellyfin import Jellyfin
from .scan import Finding, Scan, scan

MAX_ALERT_CHARS = 1800


def _seerr(cfg: Config) -> Seerr:
    return Seerr(cfg.seerr.url, cfg.seerr.key)


def _arrs(cfg: Config) -> dict[str, Arr]:
    out = {}
    if cfg.sonarr:
        out["tv"] = Arr("tv", cfg.sonarr.url, cfg.sonarr.key)
    if cfg.radarr:
        out["movie"] = Arr("movie", cfg.radarr.url, cfg.radarr.key)
    if cfg.radarr_4k:
        out["movie4k"] = Arr("movie", cfg.radarr_4k.url, cfg.radarr_4k.key)
        out["movie4k"].name = "Radarr 4K"
    return out


def _jellyfin(cfg: Config) -> Jellyfin | None:
    return Jellyfin(cfg.jellyfin.url, cfg.jellyfin.key) if cfg.jellyfin else None


def _scan(cfg: Config, st: dict[str, Any], now: float) -> Scan:
    return scan(_seerr(cfg), _arrs(cfg), _jellyfin(cfg), st, now, cfg.rules)


def doctor(cfg: Config) -> int:
    ok = True
    seerr = _seerr(cfg)
    checks: list[tuple[str, Callable[[], str]]] = [
        ("Seerr reachable", lambda: f"version {seerr.status().get('version', '?')}"),
        ("Seerr API key accepted", lambda: f"user {seerr.me().get('displayName') or 'ok'}"),
    ]
    for arr in _arrs(cfg).values():
        checks.append((f"{arr.name} answers with this key", partial(_version, arr)))
        checks.append((f"{arr.name} health", partial(_health, arr)))
    jf = _jellyfin(cfg)
    if jf:
        checks.append(("Jellyfin reachable", partial(_jf_public, jf)))
        checks.append(("Jellyfin API key accepted", partial(_jf_key, jf)))
    for label, check in checks:
        try:
            print(f"[ ok ] {label}: {check()}")
        except ApiError as e:
            print(f"[FAIL] {label}: {e}")
            ok = False
    if not jf:
        print("[ ok ] Jellyfin: not set (the not_in_jellyfin check is skipped)")
    print(f"[ ok ] webhook: {'set' if cfg.webhook_url else 'not set (alerts are skipped)'}")
    try:
        state.save(cfg.state_file, state.load(cfg.state_file))
        print(f"[ ok ] state file: {cfg.state_file} is writable")
    except state.StateError as e:
        print(f"[FAIL] state file: {e}")
        ok = False
    problem = token_problem(cfg.api_token)
    if cfg.api_token and problem:
        print(f"[FAIL] API token: {problem}")
        ok = False
    elif problem:
        print("[ ok ] API: off (no LOMBO_NUDGE_API_TOKEN, so `serve` will not start)")
    else:
        print(f"[ ok ] API: `serve` would listen on {cfg.api_bind}")
        if not is_loopback(parse_bind(cfg.api_bind)[0]):
            print("[warn] API bind is not loopback: use it only on a private network, never published to the internet")
    if cfg.decline_after_days:
        print(f"[ ok ] declines: requests with nothing arriving for {cfg.decline_after_days} days")
    else:
        print("[ ok ] declines: off (LOMBO_NUDGE_DECLINE_AFTER_DAYS=0)")
    print(f"[ ok ] mode: {'dry run (nothing is sent or changed)' if cfg.dry_run else 'LIVE'}")
    return 0 if ok else 1


def _version(arr: Arr) -> str:
    return f"version {arr.system_status().get('version', '?')}"


def _jf_public(jf: Jellyfin) -> str:
    info = jf.public_info()
    return f"{info.get('ServerName')} {info.get('Version')}"


def _jf_key(jf: Jellyfin) -> str:
    jf.info()
    return "yes"


def _health(arr: Arr) -> str:
    bad = [h for h in arr.health() if h.get("type") in ("error",)]
    if bad:
        raise ApiError(f"{arr.name} reports {len(bad)} error(s): " + "; ".join(str(h.get("message")) for h in bad[:3]))
    return "no errors"


def _print(result: Scan, show_all: bool) -> None:
    shown = [f for f in result.findings if show_all or f.verdict.stuck]
    for f in shown:
        print(f"#{f.request_id} {f.title} (for {f.requester}, {f.days} d) [{f.verdict.code}] {f.verdict.message}")
    if not shown:
        print("Nothing is stuck.")
    if not result.complete:
        print("note: the request list was cut short; the result may be incomplete.")


def findings_doc(cfg: Config, now: float) -> dict[str, Any]:
    """The findings document, made now from a fresh scan. Writes nothing."""
    st = state.load(cfg.state_file)
    result = _scan(cfg, st, now)
    return findings.build(result.findings, result.complete, st["alerts"], now)


def plan(cfg: Config, show_all: bool, now: float, as_json: bool = False) -> int:
    if as_json:
        print(json.dumps(findings_doc(cfg, now), indent=2))
        return 0
    st = state.load(cfg.state_file)
    result = _scan(cfg, st, now)
    _print(result, show_all)
    if cfg.decline_after_days:
        since = _stuck_since(st, result, now)
        for f in result.findings:
            if f.verdict.code in core.DECLINABLE:
                left = cfg.decline_after_days - (now - since[str(f.request_id)]) / 86400
                print(f"#{f.request_id} would be declined in {max(math.ceil(left), 0)} day(s) if nothing arrives")
    return 0


def _batches(findings: list[Finding]) -> list[list[Finding]]:
    """Group findings so each webhook message stays within MAX_ALERT_CHARS."""
    out: list[list[Finding]] = [[]]
    size = 0
    for f in findings:
        n = min(len(core.alert_text(f.title, f.requester, f.verdict, f.days)), MAX_ALERT_CHARS) + 1
        if out[-1] and size + n > MAX_ALERT_CHARS:
            out.append([])
            size = 0
        out[-1].append(f)
        size += n
    return out


def _stuck_since(st: dict[str, Any], result: Scan, now: float) -> dict[str, float]:
    """Since when each request has had nothing arriving. A cut-off request list forgets nothing."""
    since = {
        str(f.request_id): st["stuck"].get(str(f.request_id), now)
        for f in result.findings
        if f.verdict.code in core.DECLINABLE
    }
    return since if result.complete else {**st["stuck"], **since}


def _searching_is_broken(cfg: Config) -> str:
    """Why "nothing found" cannot be trusted right now, or ""."""
    for arr in _arrs(cfg).values():
        for h in arr.health():
            source = str(h.get("source"))
            if source in retry.CLIENT_DOWN or (source.startswith("Indexer") and h.get("type") == "error"):
                return f"{arr.name} reports: {h.get('message') or source}"
    return ""


def _decline(cfg: Config, st: dict[str, Any], result: Scan, since: dict[str, float], now: float) -> list[Finding]:
    """Decline requests that have had nothing arriving for DECLINE_AFTER_DAYS. Returns those declined and
    records them in `st["declined"]`."""
    wait = cfg.decline_after_days * 86400
    due = [
        f
        for f in result.findings
        if f.verdict.code in core.DECLINABLE and now - since.get(str(f.request_id), now) >= wait
    ]
    if not wait or not due:
        return []
    why = _searching_is_broken(cfg)
    if why:
        print(f"[warn] nothing declined while searching is broken. {why}")
        return []
    recent = {k: t for k, t in st["declined"].items() if now - t < 86400}
    due = sorted(due, key=lambda f: since[str(f.request_id)])[: max(core.DECLINES_PER_DAY - len(recent), 0)]
    done: list[Finding] = []
    for f in due:
        if cfg.dry_run:
            print(f"would decline #{f.request_id} {f.title} (nothing for {cfg.decline_after_days} days)")
            continue
        try:
            _seerr(cfg).decline(f.request_id)
        except ApiError as e:  # tried again next round
            print(f"[FAIL] could not decline #{f.request_id}: {e}", file=sys.stderr)
            continue
        print(f"declined #{f.request_id} {f.title} (for {f.requester}): {f.verdict.code}")
        recent[str(f.request_id)] = now
        done.append(f)
    st["declined"] = recent
    return done


def run_once(cfg: Config, now: float) -> int:
    with state.locked(cfg.state_file):  # not at the same time as a retry from the API
        return _round(cfg, now)


def _round(cfg: Config, now: float) -> int:
    st = state.load(cfg.state_file)
    result = _scan(cfg, st, now)
    _print(result, False)
    since = _stuck_since(st, result, now)
    declined = _decline(cfg, st, result, since, now)
    if declined:
        gone = {f.request_id for f in declined}
        result = replace(result, findings=[f for f in result.findings if f.request_id not in gone])
        since = {k: v for k, v in since.items() if int(k) not in gone}
        if cfg.webhook_url:
            text = "\n".join(
                f"Declined {f.title} for {f.requester}: nothing arrived for {cfg.decline_after_days} days. "
                "Request it again to retry."
                for f in declined
            )
            try:
                notify.send(cfg.webhook_url, text[:MAX_ALERT_CHARS])
            except notify.NotifyError as e:
                print(f"[FAIL] {e}", file=sys.stderr)
    realert = cfg.realert_days * 86400
    sent: list[Finding] = []
    for f in result.findings:
        if core.should_alert(st["alerts"].get(str(f.request_id)), f.verdict.code, now, realert):
            sent.append(f)
    delivered: list[Finding] = []
    if sent and not cfg.dry_run and cfg.webhook_url:
        # Several messages if needed; only findings in an accepted message count as alerted.
        for batch in _batches(sent):
            text = "\n".join(core.alert_text(f.title, f.requester, f.verdict, f.days)[:MAX_ALERT_CHARS] for f in batch)
            try:
                notify.send(cfg.webhook_url, text)
            except notify.NotifyError as e:  # keep the rest; they are tried again next round
                print(f"[FAIL] {e}", file=sys.stderr)
                break
            delivered += batch
    elif sent:
        print(f"({len(sent)} alert(s) not sent: {'dry run' if cfg.dry_run else 'no webhook set'})")
    # Bookkeeping: remember progress always; remember alerts only if they were really sent.
    alerts = dict(st["alerts"])
    for f in delivered:
        alerts[str(f.request_id)] = [f.verdict.code, now]
    if result.complete:
        stuck_ids = {str(f.request_id) for f in result.findings if f.verdict.stuck}
        alerts = {k: v for k, v in alerts.items() if k in stuck_ids}
    state.save(cfg.state_file, {**st, "queue": result.queue, "sig": result.sig, "alerts": alerts, "stuck": since})
    try:  # a description of what exists (also in dry run); failing to write it never fails the round
        findings.write(findings.path_for(cfg.state_file), findings.build(result.findings, result.complete, alerts, now))
    except OSError as e:
        print(f"[warn] cannot write {findings.FILE_NAME}: {e}", file=sys.stderr)
    return 0


def retry_once(cfg: Config, request_id: int, now: float, apply: bool = True) -> retry.Outcome:
    """Prepare a retry and, when `apply` and not a dry run, do it. Under the state lock, so two retries (or a
    retry and a round) never overwrite each other and the cool-down is read after the one before it."""
    with state.locked(cfg.state_file):
        st = state.load(cfg.state_file)
        p = retry.prepare(_seerr(cfg), _arrs(cfg), st, request_id, now, cfg.rules, cfg.retry_cooldown_minutes * 60)
        changed = False
        if apply and not cfg.dry_run and retry.apply(p):
            st["retry"][str(request_id)] = now
            st["stuck"].pop(str(request_id), None)  # someone cares: the decline clock starts again
            state.save(cfg.state_file, st)
            changed = True
        return retry.Outcome(p.plan.action, p.plan.message, retry.describe(p), changed)


def do_retry(cfg: Config, request_id: int, now: float) -> int:
    try:
        out = retry_once(cfg, request_id, now)
    except retry.RetryRefused as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1
    print("\n".join(out.lines))
    if cfg.dry_run:
        print("(dry run: nothing was changed. Set LOMBO_NUDGE_DRY_RUN=false to apply.)")
    elif out.changed:
        print("Done.")
    return 0


def serve(cfg: Config) -> int:
    """Run the HTTP API until stopped. Refuses to start without a good token."""
    problem = token_problem(cfg.api_token)
    if problem:
        print(f"[FAIL] serve: {problem}", file=sys.stderr)
        return 1
    if not is_loopback(parse_bind(cfg.api_bind)[0]):
        print("[warn] API bind is not loopback: use it only on a private network.", file=sys.stderr)
    server = api.make_server(
        cfg,
        lambda: findings_doc(cfg, time.time()),
        lambda request_id, apply: retry_once(cfg, request_id, time.time(), apply),
    )
    print(f"lombo-nudge API on {cfg.api_bind} ({'dry run' if cfg.dry_run else 'LIVE'})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lombo-nudge", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    for name in ("plan", "check"):
        sp = sub.add_parser(name)
        sp.add_argument("--all", action="store_true", help="also list requests that are fine")
        sp.add_argument("--json", action="store_true", help="print the findings document (docs/contracts.md)")
    sub.add_parser("serve")
    run = sub.add_parser("run")
    run.add_argument("--loop", action="store_true")
    sub.add_parser("retry").add_argument("request_id", type=int)
    args = p.parse_args(argv)

    try:
        cfg = from_env(dict(os.environ) if env is None else env)
    except ConfigError as e:
        print(f"[FAIL] settings: {e}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "doctor":
            return doctor(cfg)
        if args.cmd in ("plan", "check"):
            return plan(cfg, args.all, time.time(), args.json)
        if args.cmd == "serve":
            return serve(cfg)
        if args.cmd == "retry":
            return do_retry(cfg, args.request_id, time.time())
        while True:
            try:
                run_once(cfg, time.time())
            except (ApiError, notify.NotifyError, state.StateError) as e:
                print(f"[FAIL] {e}", file=sys.stderr)  # a loop survives one bad round
                if not args.loop:
                    return 1
            if not args.loop:
                return 0
            time.sleep(cfg.interval_seconds)
    except (ApiError, state.StateError) as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
