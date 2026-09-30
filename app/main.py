import asyncio
import logging
import re
from collections import Counter
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, config, store
from .deezer import Deezer, DeezerError
from .lidarr import Lidarr, LidarrError
from .qbit import Qbit
from .matching import (AUTO_ACCEPT, SUGGEST_MIN, artist_matches, clean_title, norm_artist, norm_title,
                       score_candidate)

log = logging.getLogger("tunefinder")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC = Path(__file__).parent / "static"
dz = Deezer()
lidarr = Lidarr()
qbit = Qbit()
_background: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init()
    yield
    await dz.close()
    await lidarr.close()


app = FastAPI(title="TuneFinder", lifespan=lifespan)


PUBLIC_API = ("/api/auth/", "/api/health")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not path.startswith(PUBLIC_API):
        token = request.cookies.get(auth.SESSION_COOKIE)
        user = store.get_session(token) if token else None
        if not user:
            return JSONResponse(status_code=401, content={"detail": "Sign in with Plex first."})
        request.state.user = user
    return await call_next(request)


@app.exception_handler(auth.AuthError)
async def _auth_error(_req, exc):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(DeezerError)
async def _deezer_error(_req, exc):
    return JSONResponse(status_code=502, content={"detail": f"Deezer: {exc}"})


@app.exception_handler(LidarrError)
async def _lidarr_error(_req, exc):
    return JSONResponse(status_code=502, content={"detail": f"Lidarr: {exc}"})


# --- shaping Deezer objects ----------------------------------------------------

def _year(d: str | None) -> int | None:
    m = re.match(r"(\d{4})", d or "")
    return int(m.group(1)) if m else None


def album_out(a: dict, artist: dict | None = None) -> dict:
    ar = a.get("artist") or artist or {}
    return {
        "id": a["id"],
        "title": a.get("title", ""),
        "artist": ar.get("name", ""),
        "artist_id": ar.get("id"),
        "cover": a.get("cover_big") or a.get("cover_medium") or a.get("cover"),
        "year": _year(a.get("release_date")),
        "release_date": a.get("release_date"),
        "type": a.get("record_type"),
        "explicit": a.get("explicit_lyrics", False),
    }


def artist_out(a: dict) -> dict:
    return {
        "id": a["id"],
        "name": a.get("name", ""),
        "picture": a.get("picture_big") or a.get("picture_medium"),
        "fans": a.get("nb_fan"),
    }


def track_out(t: dict) -> dict:
    al = t.get("album") or {}
    return {
        "id": t["id"],
        "title": t.get("title", ""),
        "artist": (t.get("artist") or {}).get("name", ""),
        "artist_id": (t.get("artist") or {}).get("id"),
        "duration": t.get("duration"),
        "preview": t.get("preview"),
        "album": album_out(al, t.get("artist")) if al else None,
    }


# --- library status ------------------------------------------------------------

async def _snapshot_or_none() -> dict | None:
    try:
        return await lidarr.snapshot()
    except LidarrError as e:
        log.warning("Lidarr unavailable: %s", e)
        return None


async def annotate(albums: list[dict] = (), artists: list[dict] = ()) -> None:
    """Add a `status` to albums and `in_library` to artists, in place."""
    snap = await _snapshot_or_none()
    requests = store.latest_album_requests()
    for a in albums:
        if a is None:
            continue
        if snap is None:
            a["status"] = "unknown"
            continue
        al = snap["album_index"].get((norm_artist(a["artist"]), norm_title(a["title"])))
        state = lidarr.album_state(al)
        req = requests.get(a["id"])
        if state in ("none", "known") and req and req["state"] in ("requested", "refreshing"):
            state = "requested"
        a["status"] = state
        if al:
            a["lidarr_album_id"] = al["id"]
    for ar in artists:
        ar["in_library"] = bool(snap and norm_artist(ar["name"]) in snap["artist_names"])


def _unique_albums_from_tracks(tracks: list[dict]) -> list[dict]:
    seen, out = set(), []
    for t in tracks:
        al = t["album"]
        if al and al["id"] not in seen:
            seen.add(al["id"])
            out.append(al)
    return out


def _unique_artists_from_tracks(raw: list[dict]) -> list[dict]:
    seen, out = set(), []
    for t in raw:
        ar = t.get("artist") or {}
        if ar.get("id") and ar["id"] not in seen:
            seen.add(ar["id"])
            out.append(artist_out(ar))
    return out


# --- API: sign in ------------------------------------------------------------

class PinCheck(BaseModel):
    pin_id: int


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.post("/api/auth/start")
async def auth_start(request: Request):
    if not config.PLEX_SERVER_ID:
        raise HTTPException(500, "PLEX_SERVER_ID isn't configured.")
    base = str(request.base_url).rstrip("/")
    pin = await auth.start_pin(f"{base}/?plexauth=1")
    return pin


@app.post("/api/auth/check")
async def auth_check(body: PinCheck, response: Response):
    token = await auth.check_pin(body.pin_id)
    if not token:
        return {"done": False}
    user = await auth.authorize(token)
    session = auth.create_session(user)
    response.set_cookie(auth.SESSION_COOKIE, session, max_age=auth.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax")
    log.info("Signed in: %s (owner=%s)", user["username"], user["owner"])
    return {"done": True, "user": {"username": user["username"], "thumb": user["thumb"]}}


@app.get("/api/auth/me")
async def auth_me(request: Request):
    token = request.cookies.get(auth.SESSION_COOKIE)
    user = store.get_session(token) if token else None
    if not user:
        raise HTTPException(401, "Not signed in.")
    return {"username": user["username"], "thumb": user["thumb"], "owner": bool(user["owner"])}


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        store.delete_session(token)
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


# --- API: browsing ------------------------------------------------------------

@app.get("/api/status")
async def status():
    try:
        s = await lidarr.status()
        p = await lidarr.profiles()
        return {"lidarr": True, "version": s.get("version"), "profiles": p,
                "lidarr_url": config.LIDARR_PUBLIC_URL}
    except LidarrError as e:
        return {"lidarr": False, "error": str(e), "lidarr_url": config.LIDARR_PUBLIC_URL}


@app.get("/api/charts/{key}")
async def charts(key: str):
    chart = config.TOP_PLAYLISTS.get(key)
    if not chart:
        raise HTTPException(404, "Unknown chart")
    raw = await dz.playlist_tracks(chart["playlist_id"])
    tracks = [track_out(t) for t in raw]
    albums = _unique_albums_from_tracks(tracks)
    artists = _unique_artists_from_tracks(raw)
    # Chart track payloads carry a trimmed artist; fetch pictures for the top few.
    details = await asyncio.gather(*(dz.artist(a["id"]) for a in artists[:18]), return_exceptions=True)
    for a, d in zip(artists, details):
        if isinstance(d, dict):
            a.update(artist_out(d))
    await annotate(albums + [t["album"] for t in tracks], artists)
    return {"name": chart["name"], "tracks": tracks, "albums": albums, "artists": artists[:18]}


@app.get("/api/genres")
async def genres():
    return [{"id": g["id"], "name": g["name"], "picture": g.get("picture_medium")}
            for g in await dz.genres()]


@app.get("/api/genre/{genre_id}")
async def genre(genre_id: int):
    albums_raw, artists_raw, tracks_raw = await asyncio.gather(
        dz.genre_chart(genre_id, "albums", 50),
        dz.genre_chart(genre_id, "artists", 24),
        dz.genre_chart(genre_id, "tracks", 50),
    )
    albums = [album_out(a) for a in albums_raw]
    artists = [artist_out(a) for a in artists_raw]
    tracks = [track_out(t) for t in tracks_raw]
    await annotate(albums + [t["album"] for t in tracks], artists)
    return {"albums": albums, "artists": artists, "tracks": tracks}


async def _deezer_artist_for(name: str) -> dict | None:
    for a in await dz.search("artist", name, limit=5):
        if norm_artist(a["name"]) == norm_artist(name):
            return a
    return None


@app.get("/api/new")
async def new_releases():
    picks_raw = await dz.editorial_selection()
    picks = [album_out(a) for a in picks_raw]

    yours: list[dict] = []
    snap = await _snapshot_or_none()
    if snap:
        cutoff = (date.today() - timedelta(days=config.NEW_RELEASE_DAYS)).isoformat()

        async def recent(name: str):
            try:
                a = await _deezer_artist_for(name)
                if not a:
                    return []
                albums = await dz.artist_albums(a["id"])
                return [album_out(al, a) for al in albums if (al.get("release_date") or "") >= cutoff]
            except DeezerError:
                return []

        names = [a["artistName"] for a in snap["artists"]][:60]
        for group in await asyncio.gather(*(recent(n) for n in names)):
            yours.extend(group)
        yours.sort(key=lambda a: a.get("release_date") or "", reverse=True)

    await annotate(picks + yours)
    return {"picks": picks, "yours": yours, "days": config.NEW_RELEASE_DAYS}


@app.get("/api/search")
async def search(q: str):
    q = q.strip()
    if not q:
        return {"artists": [], "albums": [], "tracks": []}
    artists_raw, albums_raw, tracks_raw = await asyncio.gather(
        dz.search("artist", q, 12), dz.search("album", q, 30), dz.search("track", q, 15),
    )
    artists = [artist_out(a) for a in artists_raw]
    albums = [album_out(a) for a in albums_raw]
    tracks = [track_out(t) for t in tracks_raw]
    await annotate(albums + [t["album"] for t in tracks], artists)
    return {"artists": artists, "albums": albums, "tracks": tracks}


@app.get("/api/artist/{artist_id}")
async def artist(artist_id: int):
    a, top, albums_raw, related = await asyncio.gather(
        dz.artist(artist_id), dz.artist_top(artist_id, 10), dz.artist_albums(artist_id),
        dz.related(artist_id, 16),
    )
    info = artist_out(a)
    albums = [album_out(al, a) for al in albums_raw]
    tracks = [track_out(t) for t in top]
    rel = [artist_out(r) for r in related]
    await annotate(albums + [t["album"] for t in tracks], [info] + rel)
    return {"artist": info, "top": tracks, "albums": albums, "related": rel}


@app.get("/api/album/{album_id}")
async def album(album_id: int):
    a = await dz.album(album_id)
    info = album_out(a)
    info["label"] = a.get("label")
    info["genres"] = [g["name"] for g in (a.get("genres") or {}).get("data", [])]
    info["duration"] = a.get("duration")
    tracks = [{"id": t["id"], "title": t["title"], "duration": t.get("duration"),
               "preview": t.get("preview"), "artist": (t.get("artist") or {}).get("name")}
              for t in (a.get("tracks") or {}).get("data", [])]
    await annotate([info])
    return {"album": info, "tracks": tracks}


@app.get("/api/discover")
async def discover():
    snap = await _snapshot_or_none()
    if not snap or not snap["artists"]:
        return {"seeds": [], "recommended": [], "rows": []}

    names = [a["artistName"] for a in snap["artists"]][:30]
    found = await asyncio.gather(*(_deezer_artist_for(n) for n in names), return_exceptions=True)
    seeds = [s for s in found if isinstance(s, dict)]
    rel_lists = await asyncio.gather(*(dz.related(s["id"], 20) for s in seeds), return_exceptions=True)

    counts: Counter = Counter()
    because: dict[int, list[str]] = {}
    info: dict[int, dict] = {}
    rows = []
    for seed, rel in zip(seeds, rel_lists):
        if not isinstance(rel, list):
            continue
        fresh = [r for r in rel if norm_artist(r["name"]) not in snap["artist_names"]]
        rows.append({"seed": artist_out(seed), "artists": [artist_out(r) for r in fresh[:12]]})
        for rank, r in enumerate(fresh):
            counts[r["id"]] += 1 + (20 - rank) / 40  # agreement across seeds matters most
            because.setdefault(r["id"], []).append(seed["name"])
            info[r["id"]] = r

    recommended = []
    for rid, _ in counts.most_common(40):
        out = artist_out(info[rid])
        out["because"] = because[rid][:3]
        recommended.append(out)
    all_artists = recommended + [a for row in rows for a in row["artists"]]
    await annotate([], all_artists)
    return {"seeds": [artist_out(s) for s in seeds], "recommended": recommended, "rows": rows}


# --- API: requesting ----------------------------------------------------------

class AlbumRequest(BaseModel):
    deezer_id: int
    foreign_album_id: str | None = None
    quality_profile_id: int | None = None


class ArtistRequest(BaseModel):
    deezer_id: int
    quality_profile_id: int | None = None


LOSSLESS = {"FLAC", "ALAC", "APE", "WavPack", "FLAC 24bit", "ALAC 24bit"}


def _allowed_qualities(profile: dict) -> set[str]:
    names = set()
    for item in profile["items"]:
        if item.get("allowed"):
            names |= {q["quality"]["name"] for q in item.get("items", [])} or {item["quality"]["name"]}
    return names - {"Unknown"}


HIRES = {"FLAC 24bit", "ALAC 24bit"}
# Typical average bitrates (kbps). Lossless varies with the music; these are common averages.
LOSSY_KBPS = {
    "MP3-VBR-V2": 190, "MP3-VBR-V0": 245, "AAC-VBR": 256, "WMA": 192,
    "OGG Vorbis Q5": 160, "OGG Vorbis Q6": 192, "OGG Vorbis Q7": 224, "OGG Vorbis Q8": 256,
    "OGG Vorbis Q9": 320, "OGG Vorbis Q10": 500,
}
CD_LOSSLESS_KBPS = 900   # 16-bit/44.1 kHz FLAC averages ~700-1100 kbps
HIRES_KBPS = 2800        # 24-bit/96 kHz FLAC averages ~2500-3300 kbps


def _lossy_kbps(name: str) -> int | None:
    if name in LOSSY_KBPS:
        return LOSSY_KBPS[name]
    m = re.match(r"(?:MP3|AAC)-(\d+)$", name)
    return int(m.group(1)) if m else None


def _quality_kbps(profile: dict) -> dict:
    """Low / typical / high average bitrate for what this profile would download.

    Lidarr grabs the best quality it finds within the profile, so "typical" is the best
    commonly available format and "high" covers rarer hi-res releases.
    """
    allowed = _allowed_qualities(profile)
    lossy = [k for k in (_lossy_kbps(n) for n in allowed - LOSSLESS) if k and k >= 128]
    has_cd = bool(allowed & (LOSSLESS - HIRES))
    has_hires = bool(allowed & HIRES)
    if has_cd or has_hires:
        typical = CD_LOSSLESS_KBPS if has_cd else HIRES_KBPS
        low = min(lossy) if lossy else (CD_LOSSLESS_KBPS * 0.75 if has_cd else 1500)
    else:
        typical = min(max(lossy), 320) if lossy else 320
        low = min(lossy) if lossy else typical
    high = HIRES_KBPS if has_hires else typical
    return {"low": low, "typical": typical, "high": high}


def _quality_description(profile: dict) -> str:
    allowed = _allowed_qualities(profile)
    if allowed and allowed <= LOSSLESS:
        return "FLAC / ALAC. Best sound, about 3x the size of MP3. Fewer downloads available."
    if not allowed & LOSSLESS:
        return "MP3 / AAC up to 320 kbps. Sounds great, small files."
    return "Whatever turns up first. Lidarr prefers the best, so usually FLAC."


@app.get("/api/qualities")
async def qualities():
    profiles = await lidarr.quality_profiles()
    default = (await lidarr.profiles())["quality"]
    def rank(p):  # lossy first, then lossless, then mixed ("Any")
        allowed = _allowed_qualities(p)
        return 0 if not allowed & LOSSLESS else 1 if allowed <= LOSSLESS else 2
    return {
        "default": default,
        "profiles": [{"id": p["id"], "name": p["name"], "description": _quality_description(p),
                      "kbps": _quality_kbps(p)}
                     for p in sorted(profiles, key=rank)],
    }


async def _discography_seconds(artist_id: int) -> tuple[int, int]:
    """Rough length of what adding a new artist would download: albums, EPs and singles,
    with editions of the same release (deluxe, remaster…) counted once."""
    seen, picked = set(), []
    for al in await dz.artist_albums(artist_id):
        if al.get("record_type") not in ("album", "ep", "single"):
            continue
        key = norm_title(al["title"])
        if key not in seen:
            seen.add(key)
            picked.append(al["id"])
    details = await asyncio.gather(*(dz.album(i) for i in picked[:80]), return_exceptions=True)
    return sum(d.get("duration", 0) for d in details if isinstance(d, dict)), len(picked)


async def _free_bytes() -> int | None:
    try:
        folders = await lidarr._req("GET", "/rootfolder")
    except LidarrError:
        return None
    for f in folders or []:
        if f["path"].rstrip("/") == config.LIDARR_ROOT_FOLDER.rstrip("/"):
            return f.get("freeSpace")
    return (folders[0].get("freeSpace") if folders else None)


@app.get("/api/request/context")
async def request_context(album_id: int | None = None, artist_id: int | None = None):
    """What the quality picker needs: is the artist in Lidarr (at which quality), how much
    music the request covers, and how much disk space is free."""
    snap = await lidarr.snapshot()
    if album_id:
        a = await dz.album(album_id)
        name = album_out(a)["artist"]
        seconds, albums, rough = a.get("duration") or 0, 1, False
    elif artist_id:
        name = (await dz.artist(artist_id))["name"]
        existing = snap["artist_names"].get(norm_artist(name))
        if existing:
            # Lidarr knows exactly which albums it would fetch; count the ones not on disk yet.
            missing = [al for al in snap["albums"] if al["artistId"] == existing["id"]
                       and lidarr.album_state(al) != "library"]
            seconds = sum(al.get("duration") or 0 for al in missing) // 1000
            albums, rough = len(missing), False
        else:
            (seconds, albums), rough = await _discography_seconds(artist_id), True
    else:
        raise HTTPException(400, "album_id or artist_id is required")
    existing = snap["artist_names"].get(norm_artist(name))
    return {"artist": name, "in_library": bool(existing),
            "quality_profile_id": existing.get("qualityProfileId") if existing else None,
            "seconds": seconds, "albums": albums, "rough": rough,
            "free_bytes": await _free_bytes()}


async def _checked_quality(qid: int | None) -> int | None:
    if qid is None:
        return None
    if qid not in {p["id"] for p in await lidarr.quality_profiles()}:
        raise HTTPException(400, "Unknown quality profile.")
    return qid


def _candidate_out(c: dict, score: float) -> dict:
    return {
        "foreign_album_id": c["foreignAlbumId"],
        "title": c["title"],
        "artist": c["artist"]["artistName"],
        "type": c.get("albumType"),
        "secondary": [s["name"] if isinstance(s, dict) else s for s in c.get("secondaryTypes", [])],
        "year": _year(c.get("releaseDate")),
        "tracks": sum(len(m.get("tracks", [])) for m in c.get("media", [])) or None,
        "score": round(score, 2),
    }


async def _find_candidates(artist_name: str, title: str, year, rtype) -> list[tuple[float, dict]]:
    terms = [f"{artist_name} {clean_title(title)}", clean_title(title)]
    seen: dict[str, dict] = {}
    for term in terms:
        for c in await lidarr.album_lookup(term):
            seen.setdefault(c["foreignAlbumId"], c)
        if any(artist_matches(artist_name, c["artist"]["artistName"]) for c in seen.values()):
            break
    scored = []
    for c in seen.values():
        secondary = [s["name"] if isinstance(s, dict) else s for s in c.get("secondaryTypes", [])]
        s = score_candidate(artist_name, title, year, rtype, c["artist"]["artistName"], c["title"],
                            _year(c.get("releaseDate")), c.get("albumType"), secondary)
        if s > 0:
            scored.append((s, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _grab_when_ready(req_id: int, foreign_album_id: str) -> None:
    """Wait until Lidarr has finished refreshing the artist, then monitor and search the album."""
    for _ in range(36):
        await asyncio.sleep(5)
        try:
            if await lidarr.refreshing():
                continue
            lidarr.invalidate()
            snap = await lidarr.snapshot()
        except LidarrError as e:
            log.warning("Waiting for Lidarr: %s", e)
            continue
        al = snap["album_by_foreign"].get(foreign_album_id)
        if al:
            await lidarr.monitor_and_search(al["id"])
            store.update(req_id, state="requested", lidarr_album_id=al["id"],
                         lidarr_artist_id=al["artistId"], message=None)
            return
    store.update(req_id, state="failed", message="Lidarr didn't list the album after adding the artist.")


@app.post("/api/request/album")
async def request_album(body: AlbumRequest, request: Request):
    who = request.state.user["username"]
    quality = await _checked_quality(body.quality_profile_id)
    a = await dz.album(body.deezer_id)
    info = album_out(a)
    title, artist_name = info["title"], info["artist"]
    snap = await lidarr.snapshot()

    def record(state, **kw):
        return store.add("album", info["id"], title, artist_name, info["cover"], state,
                         requested_by=who, **kw)

    # Already in Lidarr under the same names: just monitor + search.
    existing = snap["album_index"].get((norm_artist(artist_name), norm_title(title)))
    if existing and not body.foreign_album_id:
        if lidarr.album_state(existing) == "library":
            return {"status": "library", "message": "Already in your library."}
        if quality:
            await lidarr.set_artist_quality(existing["artistId"], quality)
        await lidarr.monitor_and_search(existing["id"])
        record("requested", lidarr_album_id=existing["id"], lidarr_artist_id=existing["artistId"],
               foreign_id=existing["foreignAlbumId"])
        return {"status": "wanted", "message": "Lidarr is searching for it."}

    scored = await _find_candidates(artist_name, title, info["year"], info["type"])
    if body.foreign_album_id:
        chosen = next((c for _, c in scored if c["foreignAlbumId"] == body.foreign_album_id), None)
        if not chosen:
            found = await lidarr.album_lookup(f"lidarr:{body.foreign_album_id}")
            chosen = found[0] if found else None
        if not chosen:
            raise HTTPException(404, "That release is no longer in Lidarr's search results.")
    elif scored and scored[0][0] >= AUTO_ACCEPT:
        chosen = scored[0][1]
    elif scored and scored[0][0] >= SUGGEST_MIN:
        return {"status": "confirm",
                "message": "Not sure which release this is. Pick the right one.",
                "candidates": [_candidate_out(c, s) for s, c in scored[:6] if s >= SUGGEST_MIN]}
    else:
        return {"status": "not_found",
                "message": "Lidarr's database doesn't have this release yet. Brand-new singles can "
                           "take a while to appear. You can add the whole artist instead."}

    fid = chosen["foreignAlbumId"]
    in_lidarr = snap["album_by_foreign"].get(fid)
    if in_lidarr:
        if lidarr.album_state(in_lidarr) == "library":
            return {"status": "library", "message": "Already in your library."}
        if quality:
            await lidarr.set_artist_quality(in_lidarr["artistId"], quality)
        await lidarr.monitor_and_search(in_lidarr["id"])
        record("requested", lidarr_album_id=in_lidarr["id"], lidarr_artist_id=in_lidarr["artistId"],
               foreign_id=fid)
        return {"status": "wanted", "message": "Lidarr is searching for it."}

    existing_artist_id = (chosen.get("artist") or {}).get("id")
    if quality and existing_artist_id:
        await lidarr.set_artist_quality(existing_artist_id, quality)
    try:
        added = await lidarr.add_album(chosen, quality)
    except LidarrError as e:
        artist_id = (chosen.get("artist") or {}).get("id")
        if not artist_id:
            raise
        # Existing artist whose metadata profile hides this release type.
        log.info("add_album failed for existing artist (%s); switching profile", e)
        artist = next((x for x in snap["artists"] if x["id"] == artist_id), None)
        if artist and await lidarr.ensure_profile_allows(artist):
            req_id = record("refreshing", lidarr_artist_id=artist_id, foreign_id=fid,
                            message="Refreshing the artist in Lidarr first.")
            _spawn(_grab_when_ready(req_id, fid))
            return {"status": "requested",
                    "message": "Refreshing the artist in Lidarr. The album will be grabbed in a minute."}
        raise
    req_id = record("refreshing", lidarr_album_id=added.get("id"), lidarr_artist_id=added.get("artistId"),
                    foreign_id=fid, message="Waiting for Lidarr to finish adding the artist.")
    _spawn(_grab_when_ready(req_id, fid))
    return {"status": "requested",
            "message": f"Added “{chosen['title']}” to Lidarr. The search starts in about a minute."}


@app.post("/api/request/artist")
async def request_artist(body: ArtistRequest, request: Request):
    who = request.state.user["username"]
    quality = await _checked_quality(body.quality_profile_id)
    a = await dz.artist(body.deezer_id)
    name = a["name"]
    snap = await lidarr.snapshot()
    existing = snap["artist_names"].get(norm_artist(name))
    if existing:
        if quality:
            await lidarr.set_artist_quality(existing["id"], quality)
        await lidarr.monitor_artist_all(existing["id"])
        store.add("artist", a["id"], name, name, a.get("picture_big"), "requested",
                  lidarr_artist_id=existing["id"], foreign_id=existing["foreignArtistId"],
                  requested_by=who)
        return {"status": "wanted", "message": f"Monitoring all of {name} in Lidarr. Searching now."}

    matches = [c for c in await lidarr.artist_lookup(name) if artist_matches(name, c["artistName"])]
    if not matches:
        return {"status": "not_found", "message": f"Lidarr's database doesn't have {name}."}
    added = await lidarr.add_artist(matches[0], quality)
    store.add("artist", a["id"], name, name, a.get("picture_big"), "requested",
              lidarr_artist_id=added.get("id"), foreign_id=added.get("foreignArtistId"),
              requested_by=who)
    return {"status": "wanted", "message": f"Added {name} to Lidarr with all albums. Searching now."}


class DeezerRef(BaseModel):
    deezer_id: int


async def _lidarr_album_for(deezer_id: int) -> tuple[dict, dict | None]:
    info = album_out(await dz.album(deezer_id))
    snap = await lidarr.snapshot()
    return info, snap["album_index"].get((norm_artist(info["artist"]), norm_title(info["title"])))


def _require_owner(request: Request) -> None:
    if not request.state.user.get("owner"):
        raise HTTPException(403, "Only the Plex server owner can remove music.")


class SearchRequest(BaseModel):
    deezer_id: int
    quality_profile_id: int | None = None


@app.post("/api/search/album")
async def search_album(body: SearchRequest):
    quality = await _checked_quality(body.quality_profile_id)
    info, al = await _lidarr_album_for(body.deezer_id)
    if not al:
        raise HTTPException(404, "This album isn't in Lidarr yet. Request it first.")
    if quality:
        await lidarr.set_artist_quality(al["artistId"], quality)
    await lidarr.monitor_and_search(al["id"])
    return {"status": "searching", "message": f"Lidarr is searching for “{info['title']}”."}


async def _delete_torrents(hashes: set[str], keep_albums_with_files: set[int]) -> list[str]:
    """Delete seeding torrents unless another album that still has files came from the same one."""
    if not hashes or not qbit.configured():
        return []
    snap = await lidarr.snapshot()
    with_files = {a["id"] for a in snap["albums"]
                  if (a.get("statistics") or {}).get("trackFileCount")} - keep_albums_with_files
    safe = []
    for h in hashes:
        if not (await lidarr.albums_for_download(h)) & with_files:
            safe.append(h)
    try:
        return await qbit.delete_torrents(safe, config.QBITTORRENT_CATEGORY)
    except Exception as e:  # removal already happened; the torrent is a bonus
        log.warning("Couldn't delete torrents: %s", e)
        return []


def _removed_message(what: str, res: dict, torrents: list[str], artist_removed: str | None) -> str:
    parts = [f"deleted {res['files']} file{'s' if res['files'] != 1 else ''}"] if res["files"] else []
    if res.get("cancelled"):
        parts.append("cancelled the download")
    if torrents:
        parts.append("deleted the seeding torrent" + ("s" if len(torrents) > 1 else ""))
    if artist_removed:
        parts.append(f"removed {artist_removed} from Lidarr")
    return f"Removed {what}" + (": " + ", ".join(parts) if parts else "") + "."


@app.post("/api/remove/album")
async def remove_album(body: DeezerRef, request: Request):
    _require_owner(request)
    info, al = await _lidarr_album_for(body.deezer_id)
    if not al:
        raise HTTPException(404, "This album isn't in Lidarr.")
    hashes = await lidarr.download_ids(album_id=al["id"])
    res = await lidarr.remove_album(al["id"])
    lidarr.invalidate()
    torrents = await _delete_torrents(hashes, keep_albums_with_files={al["id"]})
    artist_removed = None
    if await lidarr.artist_is_empty(al["artistId"]):
        await lidarr.remove_artist(al["artistId"])
        artist_removed = info["artist"]
    log.info("%s removed album %s (%s)", request.state.user["username"], info["title"], res)
    return {"status": "none", "artist_removed": bool(artist_removed),
            "message": _removed_message(f"“{info['title']}”", res, torrents, artist_removed)}


@app.post("/api/remove/artist")
async def remove_artist(body: DeezerRef, request: Request):
    _require_owner(request)
    a = await dz.artist(body.deezer_id)
    snap = await lidarr.snapshot()
    existing = snap["artist_names"].get(norm_artist(a["name"]))
    if not existing:
        raise HTTPException(404, f"{a['name']} isn't in Lidarr.")
    hashes = await lidarr.download_ids(artist_id=existing["id"])
    artist_album_ids = {al["id"] for al in snap["albums"] if al["artistId"] == existing["id"]}
    res = await lidarr.remove_artist(existing["id"])
    lidarr.invalidate()
    torrents = await _delete_torrents(hashes, keep_albums_with_files=artist_album_ids)
    log.info("%s removed artist %s (%s)", request.state.user["username"], a["name"], res)
    return {"status": "none", "message": _removed_message(a["name"], res, torrents, None)}


@app.get("/api/requests")
async def requests():
    rows = store.all_requests()
    snap = await _snapshot_or_none()
    by_id = {al["id"]: al for al in snap["albums"]} if snap else {}
    artists = {ar["id"]: ar for ar in snap["artists"]} if snap else {}
    for r in rows:
        if r["kind"] == "album":
            al = by_id.get(r["lidarr_album_id"]) if r["lidarr_album_id"] else None
            r["status"] = r["state"] if r["state"] in ("refreshing", "failed") else (
                lidarr.album_state(al) if al else ("unknown" if snap is None else "missing"))
            if al:
                stats = al.get("statistics") or {}
                r["progress"] = stats.get("percentOfTracks", 0)
        else:
            ar = artists.get(r["lidarr_artist_id"])
            stats = (ar or {}).get("statistics") or {}
            r["status"] = "artist" if ar else "missing"
            r["progress"] = stats.get("percentOfTracks", 0)
    return {"requests": rows, "lidarr_url": config.LIDARR_PUBLIC_URL}


# --- frontend -----------------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")
