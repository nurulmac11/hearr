import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


LIDARR_URL = _env("LIDARR_URL", "http://lidarr:8686").rstrip("/")
LIDARR_API_KEY = _env("LIDARR_API_KEY")
LIDARR_ROOT_FOLDER = _env("LIDARR_ROOT_FOLDER", "/data/Music")
LIDARR_QUALITY_PROFILE = _env("LIDARR_QUALITY_PROFILE", "Standard")
LIDARR_METADATA_PROFILE = _env("LIDARR_METADATA_PROFILE", "TuneFinder")
# Link shown in the UI for opening Lidarr in the browser.
LIDARR_PUBLIC_URL = _env("LIDARR_PUBLIC_URL", LIDARR_URL).rstrip("/")

DATA_DIR = Path(_env("DATA_DIR", "/data"))

# Deezer charts are playlists curated by Deezer.
TOP_PLAYLISTS = {
    "tr": {"name": "Turkey", "playlist_id": 1116189071},
    "global": {"name": "Global", "playlist_id": 3155776842},
}

# How far back "new releases from your artists" looks.
NEW_RELEASE_DAYS = int(_env("NEW_RELEASE_DAYS", "120"))

# Sign in with Plex: users must have access to this Plex server (its machine identifier).
PLEX_SERVER_ID = _env("PLEX_SERVER_ID")

# Optional: lets "Remove" also delete the seeding torrent (only torrents in this category).
QBITTORRENT_URL = _env("QBITTORRENT_URL").rstrip("/")
QBITTORRENT_USER = _env("QBITTORRENT_USER", "admin")
QBITTORRENT_PASSWORD = _env("QBITTORRENT_PASSWORD")
QBITTORRENT_CATEGORY = _env("QBITTORRENT_CATEGORY", "music")
