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

    async def quality_profiles(self) -> list[dict]:
        return await self._req("GET", "/qualityprofile")

    async def set_artist_quality(self, artist_id: int, quality_id: int) -> bool:
        """Change an existing artist's quality profile. Returns True if it changed."""
        full = await self._req("GET", f"/artist/{artist_id}")
        if full.get("qualityProfileId") == quality_id:
            return False
        full["qualityProfileId"] = quality_id
        await self._req("PUT", f"/artist/{artist_id}", json=full)
        self.invalidate()
        return True

    async def status(self) -> dict:
        return await self._req("GET", "/system/status")

    # --- library snapshot -----------------------------------------------------

    def invalidate(self) -> None:
        self._snapshot_at = 0

    async def snapshot(self) -> dict:
        async with self._snapshot_lock:
            if self._snapshot and time.monotonic() - self._snapshot_at < 60:
                return self._snapshot
            artists, albums, queue, commands = await asyncio.gather(
                self._req("GET", "/artist"),
                self._req("GET", "/album"),
                self._req("GET", "/queue", params={"pageSize": 500}),
                self._req("GET", "/command"),
            )
            by_id = {a["id"]: a for a in artists}
            queued = {r["albumId"] for r in queue.get("records", []) if r.get("albumId")}
            searching_albums, searching_artists = set(), set()
            for cmd in commands or []:
                if cmd.get("status") not in ("queued", "started"):
                    continue
                body = cmd.get("body") or {}
                if cmd.get("name") == "AlbumSearch":
                    searching_albums |= set(body.get("albumIds") or [])
                elif cmd.get("name") == "ArtistSearch" and body.get("artistId"):
                    searching_artists.add(body["artistId"])
            album_index: dict[tuple[str, str], dict] = {}
            for al in albums:
                artist = by_id.get(al["artistId"])
                if not artist:
                    continue
                al["_queued"] = al["id"] in queued
                al["_searching"] = al["id"] in searching_albums or al["artistId"] in searching_artists
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
        """library | downloading | searching | wanted | known (in Lidarr, not monitored) | none

        "wanted" means monitored but missing with no search running; Lidarr only searches
        when told to (or when new releases show up in its periodic RSS check).
        """
        if not al:
            return "none"
        stats = al.get("statistics") or {}
        if stats.get("trackCount") and stats.get("percentOfTracks", 0) >= 100:
            return "library"
        if al.get("_queued"):
            return "downloading"
        if al.get("monitored") and al.get("_searching"):
            return "searching"
        if al.get("monitored"):
            return "wanted"
        return "known"

    # --- lookups & actions ----------------------------------------------------

    async def album_lookup(self, term: str) -> list[dict]:
        return await self._req("GET", "/album/lookup", params={"term": term}) or []

    async def artist_lookup(self, term: str) -> list[dict]:
        return await self._req("GET", "/artist/lookup", params={"term": term}) or []

    async def add_album(self, lookup_album: dict, quality_id: int | None = None) -> dict:
        """Add a looked-up album. Adds its artist too if needed, monitoring nothing else."""
        p = await self.profiles()
        album = dict(lookup_album)
        artist = dict(album["artist"])
        if not artist.get("id"):
            artist.update(
                monitored=True,
                monitorNewItems="none",
                qualityProfileId=quality_id or p["quality"],
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

    async def add_artist(self, lookup_artist: dict, quality_id: int | None = None) -> dict:
        """Add an artist with every album monitored and search for all of them."""
        p = await self.profiles()
        artist = dict(lookup_artist)
        artist.update(
            monitored=True,
            monitorNewItems="all",
            qualityProfileId=quality_id or p["quality"],
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

    async def search_album(self, album_id: int) -> None:
        await self._req("POST", "/command", json={"name": "AlbumSearch", "albumIds": [album_id]})
        self.invalidate()

    async def download_ids(self, album_id: int | None = None, artist_id: int | None = None) -> set[str]:
        """Torrent hashes Lidarr grabbed for an album or a whole artist."""
        if album_id:
            records = (await self._req("GET", "/history", params={
                "pageSize": 500, "eventType": 1, "albumId": album_id})).get("records", [])  # 1 = grabbed
        elif artist_id:
            # /history ignores artistId; /history/artist filters properly.
            records = await self._req("GET", "/history/artist",
                                      params={"artistId": artist_id, "eventType": 1}) or []
        else:
            return set()
        return {r["downloadId"] for r in records if r.get("downloadId")}

    async def albums_for_download(self, download_id: str) -> set[int]:
        records = (await self._req("GET", "/history", params={
            "pageSize": 200, "downloadId": download_id})).get("records", [])
        return {r["albumId"] for r in records if r.get("albumId")}

    async def _cancel_queue(self, predicate) -> int:
        queue = await self._req("GET", "/queue", params={"pageSize": 500})
        n = 0
        for r in queue.get("records", []):
            if predicate(r):
                await self._req("DELETE", f"/queue/{r['id']}",
                                params={"removeFromClient": "true", "blocklist": "false"})
                n += 1
        return n

    async def remove_album(self, album_id: int) -> dict:
        """Stop wanting an album, cancel its downloads and delete its files."""
        await self._req("PUT", "/album/monitor", json={"albumIds": [album_id], "monitored": False})
        cancelled = await self._cancel_queue(lambda r: r.get("albumId") == album_id)
        files = await self._req("GET", "/trackfile", params={"albumId": album_id}) or []
        if files:
            await self._req("DELETE", "/trackfile/bulk", json={"trackFileIds": [f["id"] for f in files]})
        self.invalidate()
        return {"files": len(files), "cancelled": cancelled}

    async def artist_is_empty(self, artist_id: int) -> bool:
        """No files left and nothing monitored."""
        albums = await self._req("GET", "/album", params={"artistId": artist_id}) or []
        return all(not a["monitored"] and not (a.get("statistics") or {}).get("trackFileCount")
                   for a in albums)

    async def remove_artist(self, artist_id: int) -> dict:
        """Delete an artist from Lidarr together with all of their files."""
        albums = await self._req("GET", "/album", params={"artistId": artist_id}) or []
        files = sum((a.get("statistics") or {}).get("trackFileCount", 0) for a in albums)
        cancelled = await self._cancel_queue(lambda r: r.get("artistId") == artist_id)
        await self._req("DELETE", f"/artist/{artist_id}", params={"deleteFiles": "true"})
        self.invalidate()
        return {"files": files, "cancelled": cancelled}

    async def album(self, album_id: int) -> dict | None:
        try:
            return await self._req("GET", f"/album/{album_id}")
        except LidarrError:
            return None
