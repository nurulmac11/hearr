"""Minimal qBittorrent Web API client, used to delete seeding torrents when music is removed."""

import httpx

from . import config


class Qbit:
    def configured(self) -> bool:
        return bool(config.QBITTORRENT_URL and config.QBITTORRENT_PASSWORD)

    async def delete_torrents(self, hashes: list[str], category: str) -> list[str]:
        """Delete torrents (with their files) whose hash is given and whose category matches.

        Returns the names of the torrents that were deleted.
        """
        if not hashes or not self.configured():
            return []
        async with httpx.AsyncClient(base_url=config.QBITTORRENT_URL, timeout=20) as c:
            r = await c.post("/api/v2/auth/login", data={
                "username": config.QBITTORRENT_USER, "password": config.QBITTORRENT_PASSWORD})
            if r.status_code >= 400 or r.text.strip() == "Fails.":
                raise RuntimeError("qBittorrent login failed")
            wanted = {h.lower() for h in hashes}
            torrents = (await c.get("/api/v2/torrents/info")).json()
            targets = [t for t in torrents
                       if t["hash"].lower() in wanted and t.get("category") == category]
            if targets:
                await c.post("/api/v2/torrents/delete", data={
                    "hashes": "|".join(t["hash"] for t in targets), "deleteFiles": "true"})
            return [t["name"] for t in targets]
