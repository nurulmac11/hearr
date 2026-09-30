# TuneFinder

Music discovery for a Lidarr setup. Browse charts, genres, new releases and recommendations,
play 30-second previews, and send albums to Lidarr with one click.

Data comes from Deezer's public API (no key needed). Requests go to Lidarr's own API, which
uses Lidarr's metadata server, so MusicBrainz doesn't need to be reachable.

## Pages

- **Charts:** Deezer's Top Turkey and Top Worldwide playlists, as albums/singles and as a top-100 song list.
- **Genres:** top albums, artists and songs per Deezer genre.
- **New:** recent releases from artists already in Lidarr, plus Deezer's editorial picks.
- **Discover:** artists similar to your Lidarr library, ranked by how many of your artists they're related to.
- **Search, Artist, Album:** full discographies (albums, EPs, singles) with request buttons.
- **Requests:** everything requested from TuneFinder, with its live Lidarr status.

## How a request works

1. The Deezer album is matched to Lidarr's metadata (`app/matching.py`). Titles are compared after
   removing edition and "feat." markers and folding Turkish letters and accents.
   - Score ≥ 0.9: requested straight away.
   - Score 0.6–0.9: you pick the right release from a short list.
   - Below that: reported as not in Lidarr's database yet (common for brand-new singles).
2. If the album is already in Lidarr, it is monitored and searched.
3. Otherwise it is added with its artist. The artist is added with nothing else monitored.
   TuneFinder waits for Lidarr's artist refresh to finish, then monitors and searches just that album.

Every request first asks for a quality (the Lidarr quality profiles: Standard, Lossless, Any), with the
last choice preselected. Lidarr sets quality per artist, so for an artist already in Lidarr the picker
shows their current profile and warns that changing it applies to all their albums.

**Size estimates.** The picker shows an estimated size per quality: album length (from Deezer) × a
typical bitrate (320 kbps for MP3/AAC, ~900 kbps for CD-quality FLAC, ~2800 kbps for 24-bit hi-res),
plus free space on the music disk. Checked against real downloads: Deadbeat 134 MB estimated / 129 MB
actual (MP3 320), Discovery ~410 MB / 418 MB (FLAC). For a whole artist already in Lidarr, the
length is the exact sum of the albums not on disk yet. For a new artist it's a rough sum of their
Deezer albums, EPs and singles with duplicate editions counted once.

**Wanted vs searching.** Lidarr only searches when told to (or when new uploads appear in its periodic
RSS check), so an album can be monitored for a long time with no search running. TuneFinder shows
*Searching* only while Lidarr has an AlbumSearch/ArtistSearch command queued or running. Otherwise a
monitored, missing album shows **Search now**, which goes through the quality picker and starts a search.

**Remove** (server owner only) stops Lidarr wanting the album, cancels its downloads, deletes its
files, and deletes the seeding torrent in qBittorrent (`QBITTORRENT_*`, category `music` only),
unless another album that still has files came from the same torrent. If the artist is left with
nothing, they are removed from Lidarr as well. **Remove artist** deletes the artist with all files
and torrents.

"Add whole artist" adds the artist with every album monitored and searches for all of them.

The Lidarr metadata profile named in `LIDARR_METADATA_PROFILE` should allow Albums, EPs and Singles,
otherwise chart singles can't be requested.

## Sign in

Everything except `/api/health` and `/api/auth/*` needs a session. Users sign in with Plex (PIN flow
via app.plex.tv). An account is allowed only if Plex lists the server named in `PLEX_SERVER_ID`
among its resources, which means the owner and anyone the server is shared with. Sessions last
30 days, are stored in SQLite and use an HttpOnly cookie. Requests record who made them.

## Configuration

| Variable | Default | |
|---|---|---|
| `LIDARR_URL` | `http://lidarr:8686` | Lidarr as seen from the container |
| `LIDARR_API_KEY` | | Required |
| `LIDARR_PUBLIC_URL` | `LIDARR_URL` | Link to Lidarr shown in the UI |
| `LIDARR_ROOT_FOLDER` | `/data/Music` | Lidarr root folder for new artists |
| `LIDARR_QUALITY_PROFILE` | `Standard` | Quality profile name |
| `LIDARR_METADATA_PROFILE` | `TuneFinder` | Metadata profile name |
| `PLEX_SERVER_ID` | | Required. The Plex server's machine identifier (`/identity` on the server) |
| `QBITTORRENT_URL` | | Optional. Enables deleting seeding torrents on Remove |
| `QBITTORRENT_USER` / `QBITTORRENT_PASSWORD` | `admin` / | qBittorrent Web UI login |
| `QBITTORRENT_CATEGORY` | `music` | Only torrents in this category are ever deleted |
| `NEW_RELEASE_DAYS` | `120` | How far back "New" looks |
| `DATA_DIR` | `/data` | Where the request history (SQLite) is stored |

The app listens on port 5070. Don't use 5060: browsers refuse to open it (it's the SIP port).

## Running

It runs from the Home Assistant compose file (`~/projects/home-assistant/docker-compose.yml`,
service `tunefinder`), which builds this folder:

```sh
cd ~/projects/home-assistant && docker compose up -d --build tunefinder
```

Local development:

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests
LIDARR_URL=http://localhost:8686 LIDARR_API_KEY=... DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload --port 5070
```
