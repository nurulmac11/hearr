"""SQLite history of requests made from TuneFinder."""

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
"""


def _path():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_DIR / "tunefinder.db"


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


def add(kind: str, deezer_id: int, title: str, artist: str, cover: str | None, state: str,
        lidarr_album_id: int | None = None, lidarr_artist_id: int | None = None,
        foreign_id: str | None = None, message: str | None = None) -> int:
    with db() as c:
        cur = c.execute(
            "insert into requests(kind, deezer_id, title, artist, cover, lidarr_album_id,"
            " lidarr_artist_id, foreign_id, state, message, created_at)"
            " values (?,?,?,?,?,?,?,?,?,?,?)",
            (kind, deezer_id, title, artist, cover, lidarr_album_id, lidarr_artist_id,
             foreign_id, state, message, time.time()),
        )
        return cur.lastrowid


def update(req_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db() as c:
        c.execute(f"update requests set {cols} where id = ?", (*fields.values(), req_id))


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
