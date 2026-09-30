"""Small Deezer public API client with an in-memory TTL cache and rate limiting.

Deezer allows 50 requests per 5 seconds without a key.
"""

import asyncio
import time
from collections import deque

import httpx

BASE = "https://api.deezer.com"


class DeezerError(Exception):
    pass


class Deezer:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE, timeout=15, headers={"Accept-Language": "en"}
        )
        self._cache: dict[str, tuple[float, object]] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(8)

    async def close(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] > 5:
                    self._calls.popleft()
                if len(self._calls) < 45:
                    self._calls.append(now)
                    return
                await asyncio.sleep(5 - (now - self._calls[0]) + 0.05)

    async def get(self, path: str, ttl: int = 3600, **params) -> dict:
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key])
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            data = await self._fetch(path, params)
            self._cache[key] = (time.monotonic() + ttl, data)
            fut.set_result(data)
            return data
        except Exception as e:
            fut.set_exception(e)
            fut.exception()  # mark retrieved so waiters-less failures don't warn
            raise
        finally:
            self._inflight.pop(key, None)

    async def _fetch(self, path: str, params: dict) -> dict:
        for attempt in range(3):
            async with self._sem:
                await self._throttle()
                r = await self._client.get(path, params=params)
            r.raise_for_status()
            data = r.json()
            err = data.get("error") if isinstance(data, dict) else None
            if not err:
                return data
            # code 4 = quota exceeded; back off and retry
            if err.get("code") == 4 and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise DeezerError(err.get("message", "Deezer error"))
        raise DeezerError("Deezer quota exceeded")

    # --- convenience wrappers -------------------------------------------------

    async def playlist_tracks(self, playlist_id: int) -> list[dict]:
        data = await self.get(f"/playlist/{playlist_id}/tracks", ttl=1800, limit=100)
        return data.get("data", [])

    async def genres(self) -> list[dict]:
        data = await self.get("/genre", ttl=86400)
        return [g for g in data.get("data", []) if g["id"] != 0]

    async def genre_chart(self, genre_id: int, kind: str, limit: int = 50) -> list[dict]:
        data = await self.get(f"/chart/{genre_id}/{kind}", ttl=1800, limit=limit)
        return data.get("data", [])

    async def editorial_selection(self) -> list[dict]:
        data = await self.get("/editorial/0/selection", ttl=3600)
        return data.get("data", [])

    async def search(self, kind: str, q: str, limit: int = 25) -> list[dict]:
        data = await self.get(f"/search/{kind}", ttl=600, q=q, limit=limit)
        return data.get("data", [])

    async def artist(self, artist_id: int) -> dict:
        return await self.get(f"/artist/{artist_id}", ttl=86400)

    async def artist_top(self, artist_id: int, limit: int = 10) -> list[dict]:
        data = await self.get(f"/artist/{artist_id}/top", ttl=21600, limit=limit)
        return data.get("data", [])

    async def artist_albums(self, artist_id: int) -> list[dict]:
        data = await self.get(f"/artist/{artist_id}/albums", ttl=21600, limit=100)
        return data.get("data", [])

    async def related(self, artist_id: int, limit: int = 20) -> list[dict]:
        data = await self.get(f"/artist/{artist_id}/related", ttl=86400, limit=limit)
        return data.get("data", [])

    async def album(self, album_id: int) -> dict:
        return await self.get(f"/album/{album_id}", ttl=86400)
