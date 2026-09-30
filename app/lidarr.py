"""Lidarr v1 API client plus a short-lived snapshot of the library for status badges."""

import asyncio
import time

import httpx

from . import config
from .matching import norm_artist, norm_title


class LidarrError(Exception):
    pass


class Lidarr:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=config.LIDARR_URL + "/api/v1",
            headers={"X-Api-Key": config.LIDARR_API_KEY},
            timeout=60,
        )
        self._snapshot: dict | None = None
        self._snapshot_at = 0.0
        self._snapshot_lock = asyncio.Lock()
        self._profiles: dict | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _req(self, method: str, path: str, **kw):
        try:
            r = await self._client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise LidarrError(f"Can't reach Lidarr: {e}") from e
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body[0].get("errorMessage") if isinstance(body, list) else body.get("message")
            except Exception:
                msg = r.text[:200]
            raise LidarrError(msg or f"Lidarr returned {r.status_code}")
        return r.json() if r.content else None

    # --- setup ----------------------------------------------------------------

    async def profiles(self) -> dict:
        """Resolve the configured profile names to ids once."""
        if self._profiles is None:
            quality = await self._req("GET", "/qualityprofile")
            metadata = await self._req("GET", "/metadataprofile")

            def pick(items, name):
                for p in items:
                    if p["name"].lower() == name.lower():
                        return p["id"]
                return items[0]["id"]

            self._profiles = {
                "quality": pick(quality, config.LIDARR_QUALITY_PROFILE),
                "metadata": pick(metadata, config.LIDARR_METADATA_PROFILE),
            }
        return self._profiles

    async def status(self) -> dict:
        return await self._req("GET", "/system/status")

    # --- library snapshot -----------------------------------------------------

    def invalidate(self) -> None:
        self._snapshot_at = 0

    async def snapshot(self) -> dict:
        async with self._snapshot_lock:
            if self._snapshot and time.monotonic() - self._snapshot_at < 60:
                return self._snapshot
            artists, albums, queue = await asyncio.gather(
                self._req("GET", "/artist"),
                self._req("GET", "/album"),
                self._req("GET", "/queue", params={"pageSize": 500}),
            )
            by_id = {a["id"]: a for a in artists}
            queued = {r["albumId"] for r in queue.get("records", []) if r.get("albumId")}
            album_index: dict[tuple[str, str], dict] = {}
            for al in albums:
                artist = by_id.get(al["artistId"])
                if not artist:
                    continue
                al["_queued"] = al["id"] in queued
                album_index.setdefault((norm_artist(artist["artistName"]), norm_title(al["title"])), al)
            self._snapshot = {
                "artists": artists,
                "artist_names": {norm_artist(a["artistName"]): a for a in artists},
                "albums": albums,
                "album_index": album_index,
                "album_by_foreign": {al["foreignAlbumId"]: al for al in albums},
                "queued": queued,
            }
            self._snapshot_at = time.monotonic()
            return self._snapshot

    @staticmethod
    def album_state(al: dict | None) -> str:
        """library | downloading | wanted | known (in Lidarr, not monitored) | none"""
        if not al:
            return "none"
        stats = al.get("statistics") or {}
        if stats.get("trackCount") and stats.get("percentOfTracks", 0) >= 100:
            return "library"
        if al.get("_queued"):
            return "downloading"
        if al.get("monitored"):
            return "wanted"
        return "known"

    # --- lookups & actions ----------------------------------------------------

    async def album_lookup(self, term: str) -> list[dict]:
        return await self._req("GET", "/album/lookup", params={"term": term}) or []

    async def artist_lookup(self, term: str) -> list[dict]:
        return await self._req("GET", "/artist/lookup", params={"term": term}) or []

    async def add_album(self, lookup_album: dict) -> dict:
        """Add a looked-up album. Adds its artist too if needed, monitoring nothing else."""
        p = await self.profiles()
        album = dict(lookup_album)
        artist = dict(album["artist"])
        if not artist.get("id"):
            artist.update(
                monitored=True,
                monitorNewItems="none",
                qualityProfileId=p["quality"],
                metadataProfileId=p["metadata"],
                rootFolderPath=config.LIDARR_ROOT_FOLDER,
                addOptions={"monitor": "none", "searchForMissingAlbums": False},
                tags=[],
            )
        album["artist"] = artist
        album["monitored"] = True
        # Adding a new artist triggers a refresh that applies monitor=none to every album,
        # this one included. The caller monitors and searches once that refresh is done.
        album["addOptions"] = {"searchForNewAlbum": False}
        result = await self._req("POST", "/album", json=album)
        self.invalidate()
        return result

    async def refreshing(self) -> bool:
        """True while Lidarr still has an artist refresh queued or running."""
        cmds = await self._req("GET", "/command") or []
        return any(c.get("name") in ("RefreshArtist", "RefreshAlbum")
                   and c.get("status") in ("queued", "started") for c in cmds)

    async def monitor_and_search(self, album_id: int) -> None:
        await self._req("PUT", "/album/monitor", json={"albumIds": [album_id], "monitored": True})
        await self._req("POST", "/command", json={"name": "AlbumSearch", "albumIds": [album_id]})
        self.invalidate()

    async def ensure_profile_allows(self, artist: dict) -> bool:
        """Switch an existing artist to the TuneFinder metadata profile and refresh it.

        Returns True if a refresh was started (the album may appear shortly after).
        """
        p = await self.profiles()
        if artist.get("metadataProfileId") == p["metadata"]:
            return False
        full = await self._req("GET", f"/artist/{artist['id']}")
        full["metadataProfileId"] = p["metadata"]
        await self._req("PUT", f"/artist/{artist['id']}", json=full)
        await self._req("POST", "/command", json={"name": "RefreshArtist", "artistId": artist["id"]})
        self.invalidate()
        return True

    async def add_artist(self, lookup_artist: dict) -> dict:
        """Add an artist with every album monitored and search for all of them."""
        p = await self.profiles()
        artist = dict(lookup_artist)
        artist.update(
            monitored=True,
            monitorNewItems="all",
            qualityProfileId=p["quality"],
            metadataProfileId=p["metadata"],
            rootFolderPath=config.LIDARR_ROOT_FOLDER,
            addOptions={"monitor": "all", "searchForMissingAlbums": True},
            tags=[],
        )
        result = await self._req("POST", "/artist", json=artist)
        self.invalidate()
        return result

    async def monitor_artist_all(self, artist_id: int) -> None:
        full = await self._req("GET", f"/artist/{artist_id}")
        full["monitored"] = True
        full["monitorNewItems"] = "all"
        await self._req("PUT", f"/artist/{artist_id}", json=full)
        albums = await self._req("GET", "/album", params={"artistId": artist_id})
        ids = [a["id"] for a in albums]
        if ids:
            await self._req("PUT", "/album/monitor", json={"albumIds": ids, "monitored": True})
        await self._req("POST", "/command", json={"name": "ArtistSearch", "artistId": artist_id})
        self.invalidate()

    async def album(self, album_id: int) -> dict | None:
        try:
            return await self._req("GET", f"/album/{album_id}")
        except LidarrError:
            return None
