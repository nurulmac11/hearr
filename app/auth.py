"""Sign in with Plex, Jellyfin or a local admin account.

Plex uses its PIN flow: the server creates a PIN, the browser signs in on app.plex.tv and comes
back, then the server swaps the PIN for the user's token. A user is allowed in only if their
Plex account can see the configured Plex server (the owner, or someone it's shared with).

Jellyfin takes a username and password, which Hearr checks against the configured Jellyfin
server. Any enabled Jellyfin user gets in; administrators count as owners.

The local admin account (ADMIN_USERNAME / ADMIN_PASSWORD) is an owner.

Password sign-ins are rate limited per IP: too many wrong passwords and that IP has to wait.
"""

import secrets
import time
import uuid
from collections import defaultdict, deque
from urllib.parse import urlencode

import httpx

from . import config, store

PLEX_TV = "https://plex.tv/api/v2"
SESSION_COOKIE = "hearr_session"
SESSION_DAYS = 30


class AuthError(Exception):
    pass


class BadCredentials(AuthError):
    """Wrong username or password; counts towards the per-IP limit."""


class TooManyAttempts(AuthError):
    pass


MAX_FAILURES = 10
FAILURE_WINDOW = 15 * 60
_failures: dict[str, deque] = defaultdict(deque)


def _recent_failures(ip: str) -> deque:
    q = _failures[ip]
    while q and q[0] < time.monotonic() - FAILURE_WINDOW:
        q.popleft()
    if not q:
        del _failures[ip]
    return q


def check_rate_limit(ip: str) -> None:
    q = _recent_failures(ip)
    if len(q) >= MAX_FAILURES:
        wait = int(q[0] + FAILURE_WINDOW - time.monotonic()) // 60 + 1
        raise TooManyAttempts(f"Too many failed sign-ins. Try again in {wait} min.")


def record_failure(ip: str) -> None:
    if len(_failures) > 10_000:  # drop IPs whose failures have all expired
        for old in list(_failures):
            _recent_failures(old)
    _failures[ip].append(time.monotonic())


def client_id() -> str:
    """Stable per-install identifier that Plex shows under authorized devices."""
    path = config.DATA_DIR / "plex_client_id"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(str(uuid.uuid4()))
    return path.read_text().strip()


def _headers(token: str | None = None) -> dict:
    h = {
        "Accept": "application/json",
        "X-Plex-Product": "Hearr",
        "X-Plex-Client-Identifier": client_id(),
    }
    if token:
        h["X-Plex-Token"] = token
    return h


async def start_pin(forward_url: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{PLEX_TV}/pins", params={"strong": "true"}, headers=_headers())
        r.raise_for_status()
        pin = r.json()
    query = urlencode({
        "clientID": client_id(),
        "code": pin["code"],
        "forwardUrl": forward_url,
        "context[device][product]": "Hearr",
    })
    return {"pin_id": pin["id"], "auth_url": f"https://app.plex.tv/auth#?{query}"}


async def check_pin(pin_id: int) -> str | None:
    """Return the user's Plex token once they've signed in, else None."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{PLEX_TV}/pins/{pin_id}", headers=_headers())
        if r.status_code == 404:
            raise AuthError("This sign-in link expired. Try again.")
        r.raise_for_status()
        return r.json().get("authToken")


async def authorize(token: str) -> dict:
    """Look up the Plex user and check they can access our Plex server."""
    async with httpx.AsyncClient(timeout=20) as c:
        ru = await c.get(f"{PLEX_TV}/user", headers=_headers(token))
        if ru.status_code == 401:
            raise AuthError("Plex rejected the sign-in.")
        ru.raise_for_status()
        user = ru.json()
        rr = await c.get(f"{PLEX_TV}/resources", headers=_headers(token))
        rr.raise_for_status()
        resources = rr.json()

    server = next((r for r in resources
                   if r.get("clientIdentifier") == config.PLEX_SERVER_ID), None)
    if not server:
        raise AuthError(f"{user.get('username') or 'This account'} doesn't have access to this Plex server.")
    return {
        "provider": "plex",
        "user_id": str(user["id"]),
        "username": user.get("username") or user.get("title") or user.get("email"),
        "thumb": user.get("thumb"),
        "owner": bool(server.get("owned")),
    }


def _jellyfin_headers(token: str | None = None) -> dict:
    auth = f'MediaBrowser Client="Hearr", Device="Hearr", DeviceId="{client_id()}", Version="1.0"'
    if token:
        auth += f', Token="{token}"'
    return {"Accept": "application/json", "Authorization": auth}


async def jellyfin_login(username: str, password: str) -> dict:
    """Check the credentials with Jellyfin and return the user."""
    async with httpx.AsyncClient(base_url=config.JELLYFIN_URL, timeout=20) as c:
        try:
            r = await c.post("/Users/AuthenticateByName", headers=_jellyfin_headers(),
                             json={"Username": username, "Pw": password})
        except httpx.HTTPError as e:
            raise AuthError(f"Couldn't reach Jellyfin: {e}") from e
        if r.status_code in (400, 401, 403):
            raise BadCredentials("Wrong username or password.")
        r.raise_for_status()
        data = r.json()
        # Hearr keeps its own session, so end the one Jellyfin just created rather than leave
        # a "Hearr" device behind in the user's Jellyfin dashboard on every sign-in.
        try:
            await c.post("/Sessions/Logout", headers=_jellyfin_headers(data["AccessToken"]))
        except httpx.HTTPError:
            pass

    user = data["User"]
    policy = user.get("Policy") or {}
    if policy.get("IsDisabled"):
        raise AuthError(f"{user['Name']}'s Jellyfin account is disabled.")
    return {
        "provider": "jellyfin",
        "user_id": user["Id"],
        "username": user["Name"],
        "thumb": None,
        "owner": bool(policy.get("IsAdministrator")),
    }


def local_login(username: str, password: str) -> dict | None:
    """The local admin if the credentials match it, else None."""
    if not config.ADMIN_PASSWORD:
        return None
    ok_user = secrets.compare_digest(username.encode(), config.ADMIN_USERNAME.encode())
    ok_pass = secrets.compare_digest(password.encode(), config.ADMIN_PASSWORD.encode())
    if not (ok_user and ok_pass):
        return None
    return {"provider": "local", "user_id": config.ADMIN_USERNAME,
            "username": config.ADMIN_USERNAME, "thumb": None, "owner": True}


async def password_login(username: str, password: str, ip: str) -> dict:
    """Sign in as the local admin or, failing that, a Jellyfin user."""
    check_rate_limit(ip)
    try:
        user = local_login(username, password)
        if user:
            return user
        if config.JELLYFIN_URL:
            return await jellyfin_login(username, password)
        raise BadCredentials("Wrong username or password.")
    except BadCredentials:
        record_failure(ip)
        raise


def create_session(user: dict) -> str:
    token = secrets.token_urlsafe(32)
    store.add_session(token, user, time.time() + SESSION_DAYS * 86400)
    return token
