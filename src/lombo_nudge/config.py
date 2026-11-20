"""Settings, read once from environment variables (see .env.example).

Every setting has a LOMBO_NUDGE_ prefix so it never collides with another container's variables.
A bad value stops the app at start-up with a message that names the variable.
"""

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
    "JELLYFIN_URL",
    "JELLYFIN_API_KEY",
    "WEBHOOK_URL",
    "DRY_RUN",
    "INTERVAL_SECONDS",
    "SEARCH_AFTER_HOURS",
    "PARTLY_AFTER_HOURS",
    "STALL_AFTER_HOURS",
    "REALERT_DAYS",
    "RETRY_COOLDOWN_MINUTES",
    "STATE_FILE",
)


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


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
        raise ConfigError(f"{PREFIX}{app}_URL is not set. It is the address of {app.title()} as this app sees it.")
    _check_url(f"{app}_URL", url)
    if not key or key == "changeme":
        raise ConfigError(f"{PREFIX}{app}_API_KEY is not set. Find it in {app.title()}: Settings > General.")
    return Service(url, key)


def _number(get: Callable[..., str], name: str, default: str, minimum: int) -> int:
    try:
        n = int(get(name, default))
    except ValueError:
        raise ConfigError(f"{PREFIX}{name} must be a whole number") from None
    if n < minimum:
        raise ConfigError(f"{PREFIX}{name} must be at least {minimum}")
    return n


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
    # Safe by default: nothing is sent or changed until the person sets DRY_RUN=false.
    return Config(
        seerr=seerr,
        sonarr=sonarr,
        radarr=radarr,
        jellyfin=jellyfin,
        webhook_url=webhook,
        dry_run=_truthy(get("DRY_RUN", "true")),
        interval_seconds=_number(get, "INTERVAL_SECONDS", "3600", 60),
        rules=rules,
        realert_days=_number(get, "REALERT_DAYS", "7", 1),
        retry_cooldown_minutes=_number(get, "RETRY_COOLDOWN_MINUTES", "30", 1),
        state_file=state,
    )
