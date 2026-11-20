"""Optional Jellyfin client: used only to confirm that a finished title really shows up in the library.

Authentication uses an API key (Jellyfin: Dashboard > API Keys) in the Authorization header.
"""

from typing import Any, cast

from .http import ApiError, Client

JellyfinError = ApiError


class Jellyfin(Client):
    def __init__(self, base_url: str, api_key: str, timeout: float = 10):
        super().__init__("Jellyfin", base_url, {"Authorization": f'MediaBrowser Token="{api_key}"'}, timeout)

    def public_info(self) -> dict[str, Any]:
        """Server name and version. Needs no key, so it tells 'unreachable' apart from 'bad key'."""
        return cast(dict[str, Any], self.get("/System/Info/Public", auth=False))

    def info(self) -> dict[str, Any]:
        """Needs a valid key."""
        return cast(dict[str, Any], self.get("/System/Info"))

    def has_title(self, kind: str, tmdb_id: int | None, tvdb_id: int | None) -> bool | None:
        """True/False if Jellyfin lists the title (matched by provider id); None when it cannot be asked."""
        provider = f"tvdb.{tvdb_id}" if kind == "tv" else f"tmdb.{tmdb_id}"
        if (tvdb_id if kind == "tv" else tmdb_id) is None:
            return None
        found = self.get(
            "/Items",
            Recursive="true",
            IncludeItemTypes="Series" if kind == "tv" else "Movie",
            AnyProviderIdEquals=provider,
            Limit=1,
        )
        return bool((found or {}).get("Items"))
