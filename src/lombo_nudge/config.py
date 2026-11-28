"""Settings, read once from environment variables (see .env.example).

Every setting has a LOMBO_NUDGE_ prefix so it never collides with another container's variables.
A bad value stops the app at start-up with a message that names the variable.
"""

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from .core import Rules

PREFIX = "LOMBO_NUDGE_"

# Every setting, so a test can keep .env.example in sync.
SETTINGS = (
    "SEERR_URL",
    "SEERR_API_KEY",
    "SONARR_URL",
    "SONARR_API_KEY",
    "RADARR_URL",
    "RADARR_API_KEY",
    "RADARR_4K_URL",
    "RADARR_4K_API_KEY",
    "JELLYFIN_URL",
    "JELLYFIN_API_KEY",
    "WEBHOOK_URL",
    "DRY_RUN",
    "INTERVAL_SECONDS",
    "SEARCH_AFTER_HOURS",
    "PARTLY_AFTER_HOURS",
    "STALL_AFTER_HOURS",
    "REALERT_DAYS",
    "DECLINE_AFTER_DAYS",
    "RETRY_COOLDOWN_MINUTES",
    "FALLBACK_TAG",
    "FALLBACK_AFTER_MINUTES",
    "STATE_FILE",
    "API_TOKEN",
    "API_BIND",
)

TOKEN_MIN = 24
DEFAULT_BIND = "127.0.0.1:8095"


class ConfigError(Exception):
    """A setting is missing or invalid. The message says which one and what to do."""


@dataclass(frozen=True)
class Service:
    url: str
    key: str


@dataclass(frozen=True)
class Config:
    seerr: Service
    sonarr: Service | None
    radarr: Service | None
    jellyfin: Service | None
    webhook_url: str | None
    dry_run: bool
    interval_seconds: int
    rules: Rules
    realert_days: int
    retry_cooldown_minutes: int
    state_file: str
    api_token: str = ""  # `serve` needs it (see token_problem); empty means the API is off
    api_bind: str = DEFAULT_BIND  # host:port `serve` listens on
    decline_after_days: int = 0  # 0 = never decline anything
    radarr_4k: Service | None = None  # a second Radarr that gets the 4K movie requests
    fallback_tag: str = ""  # tag added to a title nothing is found for; empty = off
    fallback_after_minutes: int = 30


def _truthy(value: str, name: str) -> bool:
    """Strict on/off: a typo such as DRY_RUN=treu is an error, never a silent 'off'."""
    word = value.strip().lower()
    if word in {"1", "true", "yes", "on"}:
        return True
    if word in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{PREFIX}{name} must be true or false (got {value!r})")


def _check_url(name: str, url: str) -> None:
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or not p.netloc:
        raise ConfigError(f"{PREFIX}{name} must be a full URL such as http://seerr:5055 (got {url!r})")


def _service(get: Callable[..., str], app: str, required: bool = False) -> Service | None:
    url = get(f"{app}_URL").rstrip("/")
    key = get(f"{app}_API_KEY")
    if not url and (not key or key == "changeme") and not required:
        return None
    if not url:
        raise ConfigError(
            f"{PREFIX}{app}_URL is not set. It is the address of {app.replace('_', ' ').title()} as this app sees it."
        )
    _check_url(f"{app}_URL", url)
    if not key or key == "changeme":
        raise ConfigError(
            f"{PREFIX}{app}_API_KEY is not set. Find it in {app.replace('_', ' ').title()}: Settings > General."
        )
    return Service(url, key)


def _number(get: Callable[..., str], name: str, default: str, minimum: int) -> int:
    try:
        n = int(get(name, default))
    except ValueError:
        raise ConfigError(f"{PREFIX}{name} must be a whole number") from None
    if n < minimum:
        raise ConfigError(f"{PREFIX}{name} must be at least {minimum}")
    return n


def parse_bind(value: str) -> tuple[str, int]:
    """'ip:port' (or '[::1]:port') as (host, port). The host must be an IP address. Raises ConfigError."""
    host, sep, port_text = value.rpartition(":")
    host = host.strip("[]")
    try:
        ipaddress.ip_address(host)
        ok = bool(sep) and port_text.isdigit() and 1 <= int(port_text) <= 65535
    except ValueError:
        ok = False
    if not ok:
        raise ConfigError(f"{PREFIX}API_BIND must look like 127.0.0.1:8095 (an IP address, a colon, a port 1 to 65535)")
    return host, int(port_text)


def is_loopback(host: str) -> bool:
    return ipaddress.ip_address(host).is_loopback


def token_problem(token: str) -> str | None:
    """Why this token cannot be used, or None when it can."""
    if not token:
        return f"{PREFIX}API_TOKEN is not set. Make a long random one (for example `openssl rand -hex 32`)."
    if len(token) < TOKEN_MIN:
        return f"{PREFIX}API_TOKEN is too short: use at least {TOKEN_MIN} characters (`openssl rand -hex 32`)."
    if not all(33 <= ord(c) <= 126 for c in token):
        return f"{PREFIX}API_TOKEN must be printable ASCII with no spaces."
    return None


def from_env(env: dict[str, str]) -> Config:
    def get(name: str, default: str = "") -> str:
        return (env.get(PREFIX + name) or default).strip()

    seerr = _service(get, "SEERR", required=True)
    sonarr = _service(get, "SONARR")
    radarr = _service(get, "RADARR")
    jellyfin = _service(get, "JELLYFIN")
    assert seerr is not None  # noqa: S101 (required=True never returns None; this narrows the type)
    if not sonarr and not radarr:
        raise ConfigError(f"Set {PREFIX}SONARR_URL and {PREFIX}SONARR_API_KEY, or the RADARR pair, or both.")

    webhook = get("WEBHOOK_URL") or None
    if webhook and urlparse(webhook).scheme not in {"http", "https"}:
        raise ConfigError(f"{PREFIX}WEBHOOK_URL must start with http:// or https:// (or be empty)")

    rules = Rules(
        search_after_h=_number(get, "SEARCH_AFTER_HOURS", "24", 1),
        partly_after_h=_number(get, "PARTLY_AFTER_HOURS", "72", 1),
        stall_after_h=_number(get, "STALL_AFTER_HOURS", "12", 1),
    )
    state = get("STATE_FILE", "/data/state.json")
    bind = get("API_BIND", DEFAULT_BIND)
    parse_bind(bind)  # a bad address stops the app at start-up like any other setting
    # Safe by default: nothing is sent or changed until the person sets DRY_RUN=false.
    return Config(
        seerr=seerr,
        sonarr=sonarr,
        radarr=radarr,
        jellyfin=jellyfin,
        webhook_url=webhook,
        dry_run=_truthy(get("DRY_RUN", "true"), "DRY_RUN"),
        interval_seconds=_number(get, "INTERVAL_SECONDS", "3600", 60),
        rules=rules,
        realert_days=_number(get, "REALERT_DAYS", "7", 1),
        retry_cooldown_minutes=_number(get, "RETRY_COOLDOWN_MINUTES", "30", 1),
        state_file=state,
        api_token=get("API_TOKEN"),
        api_bind=bind,
        radarr_4k=_service(get, "RADARR_4K"),
        decline_after_days=_number(get, "DECLINE_AFTER_DAYS", "0", 0),
        fallback_tag=get("FALLBACK_TAG"),
        fallback_after_minutes=_number(get, "FALLBACK_AFTER_MINUTES", "30", 5),
    )
