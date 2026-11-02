import asyncio
import functools
import json
import sqlite3
import time

import httpx
import pytest

from app import auth, config, store


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "JELLYFIN_URL", "http://jellyfin:8096")
    return tmp_path


def fake_jellyfin(monkeypatch, handler):
    calls = []

    def recording(request):
        calls.append(request)
        return handler(request)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        functools.partial(real, transport=httpx.MockTransport(recording)))
    return calls


def jellyfin_user(admin=False, disabled=False):
    return {"AccessToken": "tok", "User": {
        "Id": "abc123", "Name": "sam",
        "Policy": {"IsAdministrator": admin, "IsDisabled": disabled}}}


def test_jellyfin_login_ok_and_ends_jellyfin_session(monkeypatch):
    calls = fake_jellyfin(monkeypatch, lambda r: httpx.Response(
        200, json=jellyfin_user(admin=True)) if r.url.path == "/Users/AuthenticateByName"
        else httpx.Response(204))
    user = asyncio.run(auth.jellyfin_login("sam", "pw"))
    assert user == {"provider": "jellyfin", "user_id": "abc123", "username": "sam",
                    "thumb": None, "owner": True}
    assert json.loads(calls[0].content) == {"Username": "sam", "Pw": "pw"}
    assert calls[1].url.path == "/Sessions/Logout"
    assert 'Token="tok"' in calls[1].headers["Authorization"]


def test_jellyfin_login_wrong_password(monkeypatch):
    fake_jellyfin(monkeypatch, lambda r: httpx.Response(401))
    with pytest.raises(auth.AuthError, match="Wrong"):
        asyncio.run(auth.jellyfin_login("sam", "nope"))


def test_jellyfin_login_disabled_user(monkeypatch):
    fake_jellyfin(monkeypatch, lambda r: httpx.Response(200, json=jellyfin_user(disabled=True))
                  if r.url.path == "/Users/AuthenticateByName" else httpx.Response(204))
    with pytest.raises(auth.AuthError, match="disabled"):
        asyncio.run(auth.jellyfin_login("sam", "pw"))


def test_old_plex_sessions_survive_migration(data_dir):
    conn = sqlite3.connect(data_dir / "hearr.db")
    conn.executescript("""
        create table sessions (token text primary key, plex_id integer not null,
            username text not null, thumb text, owner integer not null default 0,
            expires_at real not null);
    """)
    conn.execute("insert into sessions values ('t1', 42, 'alex', null, 1, ?)", (time.time() + 60,))
    conn.commit()
    conn.close()

    store.init()
    s = store.get_session("t1")
    assert (s["provider"], s["user_id"], s["username"], s["owner"]) == ("plex", "42", "alex", 1)

    store.add_session("t2", {"provider": "jellyfin", "user_id": "abc123", "username": "sam",
                             "owner": False}, time.time() + 60)
    assert store.get_session("t2")["provider"] == "jellyfin"


@pytest.fixture
def local_admin(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "s3cret")
    monkeypatch.setattr(auth, "_failures", auth.defaultdict(auth.deque))


def test_local_admin_login_skips_jellyfin(monkeypatch, local_admin):
    calls = fake_jellyfin(monkeypatch, lambda r: httpx.Response(500))
    user = asyncio.run(auth.password_login("admin", "s3cret", "1.2.3.4"))
    assert (user["provider"], user["username"], user["owner"]) == ("local", "admin", True)
    assert calls == []


def test_wrong_local_password_falls_through_to_jellyfin(monkeypatch, local_admin):
    fake_jellyfin(monkeypatch, lambda r: httpx.Response(200, json=jellyfin_user())
                  if r.url.path == "/Users/AuthenticateByName" else httpx.Response(204))
    user = asyncio.run(auth.password_login("admin", "nope", "1.2.3.4"))
    assert user["provider"] == "jellyfin"


def test_local_only_rejects_wrong_password(monkeypatch, local_admin):
    monkeypatch.setattr(config, "JELLYFIN_URL", "")
    with pytest.raises(auth.BadCredentials):
        asyncio.run(auth.password_login("admin", "nope", "1.2.3.4"))


def test_rate_limit_per_ip(monkeypatch, local_admin):
    fake_jellyfin(monkeypatch, lambda r: httpx.Response(401))
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.BadCredentials):
            asyncio.run(auth.password_login("sam", "nope", "1.2.3.4"))
    # Even the right password is refused from that IP now...
    with pytest.raises(auth.TooManyAttempts):
        asyncio.run(auth.password_login("admin", "s3cret", "1.2.3.4"))
    # ...but other IPs are unaffected, and the block lifts after the window.
    assert asyncio.run(auth.password_login("admin", "s3cret", "5.6.7.8"))["provider"] == "local"
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + auth.FAILURE_WINDOW + 1)
    assert asyncio.run(auth.password_login("admin", "s3cret", "1.2.3.4"))["provider"] == "local"


def test_unreachable_jellyfin_doesnt_count(monkeypatch, local_admin):
    def down(_r):
        raise httpx.ConnectError("down")
    fake_jellyfin(monkeypatch, down)
    for _ in range(auth.MAX_FAILURES + 1):
        with pytest.raises(auth.AuthError, match="Couldn't reach"):
            asyncio.run(auth.password_login("sam", "pw", "1.2.3.4"))
