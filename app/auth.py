"""Sign in with Plex.

Uses Plex's PIN flow: the server creates a PIN, the browser signs in on app.plex.tv and comes
back, then the server swaps the PIN for the user's token. A user is allowed in only if their
Plex account can see the configured Plex server (the owner, or someone it's shared with).
"""

import secrets
import time
import uuid
from urllib.parse import urlencode

import httpx

from . import config, store

PLEX_TV = "https://plex.tv/api/v2"
SESSION_COOKIE = "tf_session"
SESSION_DAYS = 30


class AuthError(Exception):
    pass


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
        "X-Plex-Product": "TuneFinder",
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
        "context[device][product]": "TuneFinder",
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
        "plex_id": user["id"],
        "username": user.get("username") or user.get("title") or user.get("email"),
        "thumb": user.get("thumb"),
        "owner": bool(server.get("owned")),
    }


def create_session(user: dict) -> str:
    token = secrets.token_urlsafe(32)
    store.add_session(token, user, time.time() + SESSION_DAYS * 86400)
    return token
