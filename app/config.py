import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


LIDARR_URL = _env("LIDARR_URL", "http://lidarr:8686").rstrip("/")
LIDARR_API_KEY = _env("LIDARR_API_KEY")
LIDARR_ROOT_FOLDER = _env("LIDARR_ROOT_FOLDER", "/data/Music")
LIDARR_QUALITY_PROFILE = _env("LIDARR_QUALITY_PROFILE", "Standard")
LIDARR_METADATA_PROFILE = _env("LIDARR_METADATA_PROFILE", "Hearr")
# Link shown in the UI for opening Lidarr in the browser.
LIDARR_PUBLIC_URL = _env("LIDARR_PUBLIC_URL", LIDARR_URL).rstrip("/")

DATA_DIR = Path(_env("DATA_DIR", "/data"))

# Deezer publishes its charts as "Top <country>" playlists (by "Deezer Charts").
KNOWN_CHARTS = {
    "global": ("🌍 Global", 3155776842),
    "us": ("🇺🇸 USA", 1313621735),
    "uk": ("🇬🇧 UK", 1111142221),
    "fr": ("🇫🇷 France", 1109890291),
    "de": ("🇩🇪 Germany", 1111143121),
    "es": ("🇪🇸 Spain", 1116190041),
    "it": ("🇮🇹 Italy", 1116187241),
    "br": ("🇧🇷 Brazil", 1111141961),
    "co": ("🇨🇴 Colombia", 1116188451),
    "tr": ("🇹🇷 Turkey", 1116189071),
}


def _parse_charts(spec: str) -> dict:
    """CHARTS="tr,global" or with custom playlists: "global,My list=1234567"."""
    charts = {}
    for part in (p.strip() for p in spec.split(",")):
        if not part:
            continue
        if "=" in part:
            label, pid = part.rsplit("=", 1)
            if pid.strip().isdigit():
                charts[f"p{pid.strip()}"] = {"name": label.strip(), "playlist_id": int(pid)}
        elif part.lower() in KNOWN_CHARTS:
            name, pid = KNOWN_CHARTS[part.lower()]
            charts[part.lower()] = {"name": name, "playlist_id": pid}
    return charts or {"global": {"name": KNOWN_CHARTS["global"][0], "playlist_id": KNOWN_CHARTS["global"][1]}}


TOP_PLAYLISTS = _parse_charts(_env("CHARTS", "global,us,uk"))

# How far back "new releases from your artists" looks.
NEW_RELEASE_DAYS = int(_env("NEW_RELEASE_DAYS", "120"))

# Sign in with Plex: users must have access to this Plex server (its machine identifier).
PLEX_SERVER_ID = _env("PLEX_SERVER_ID")

# Optional: lets "Remove" also delete the seeding torrent (only torrents in this category).
QBITTORRENT_URL = _env("QBITTORRENT_URL").rstrip("/")
QBITTORRENT_USER = _env("QBITTORRENT_USER", "admin")
QBITTORRENT_PASSWORD = _env("QBITTORRENT_PASSWORD")
QBITTORRENT_CATEGORY = _env("QBITTORRENT_CATEGORY", "music")

# Set when Hearr is served over HTTPS (e.g. behind a reverse proxy) so the session cookie is Secure.
COOKIE_SECURE = _env("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
