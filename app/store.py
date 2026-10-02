"""SQLite history of requests made from Hearr."""

import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
create table if not exists requests (
    id integer primary key autoincrement,
    kind text not null,               -- album | artist
    deezer_id integer not null,
    title text not null,
    artist text not null,
    cover text,
    lidarr_album_id integer,
    lidarr_artist_id integer,
    foreign_id text,
    state text not null,              -- requested | refreshing | failed
    message text,
    created_at real not null
);
create index if not exists requests_deezer on requests(kind, deezer_id);
create table if not exists sessions (
    token text primary key,
    plex_id integer not null,
    username text not null,
    thumb text,
    owner integer not null default 0,
    expires_at real not null
);
"""


def _path():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = config.DATA_DIR / "hearr.db"
    legacy = config.DATA_DIR / "tunefinder.db"  # name used before the rename
    if not path.exists() and legacy.exists():
        legacy.rename(path)
    return path


@contextmanager
def db():
    conn = sqlite3.connect(_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with db() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("pragma table_info(requests)")}
        if "requested_by" not in cols:
            c.execute("alter table requests add column requested_by text")
        c.execute("delete from sessions where expires_at < ?", (time.time(),))


def add(kind: str, deezer_id: int, title: str, artist: str, cover: str | None, state: str,
        lidarr_album_id: int | None = None, lidarr_artist_id: int | None = None,
        foreign_id: str | None = None, message: str | None = None,
        requested_by: str | None = None) -> int:
    with db() as c:
        cur = c.execute(
            "insert into requests(kind, deezer_id, title, artist, cover, lidarr_album_id,"
            " lidarr_artist_id, foreign_id, state, message, created_at, requested_by)"
            " values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, deezer_id, title, artist, cover, lidarr_album_id, lidarr_artist_id,
             foreign_id, state, message, time.time(), requested_by),
        )
        return cur.lastrowid


def update(req_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db() as c:
        c.execute(f"update requests set {cols} where id = ?", (*fields.values(), req_id))


def delete_requests(kind: str, deezer_id: int) -> int:
    with db() as c:
        return c.execute("delete from requests where kind = ? and deezer_id = ?",
                         (kind, deezer_id)).rowcount


def all_requests(limit: int = 200) -> list[dict]:
    with db() as c:
        rows = c.execute("select * from requests order by created_at desc limit ?", (limit,))
        return [dict(r) for r in rows]


def latest_album_requests() -> dict[int, dict]:
    """deezer album id -> most recent request row."""
    out: dict[int, dict] = {}
    with db() as c:
        for r in c.execute("select * from requests where kind = 'album' order by created_at"):
            out[r["deezer_id"]] = dict(r)
    return out


def add_session(token: str, user: dict, expires_at: float) -> None:
    with db() as c:
        c.execute(
            "insert into sessions(token, plex_id, username, thumb, owner, expires_at)"
            " values (?,?,?,?,?,?)",
            (token, user["plex_id"], user["username"], user.get("thumb"), int(user["owner"]),
             expires_at),
        )


def get_session(token: str) -> dict | None:
    with db() as c:
        row = c.execute("select * from sessions where token = ? and expires_at > ?",
                        (token, time.time())).fetchone()
        return dict(row) if row else None


def delete_session(token: str) -> None:
    with db() as c:
        c.execute("delete from sessions where token = ?", (token,))
