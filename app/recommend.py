"""Pure helpers for Discover's album and song recommendations."""
from itertools import zip_longest

from .matching import clean_title, norm_artist, norm_title

ALBUM_TYPES = ("album", "ep")


def best_albums(raw: list[dict], limit: int) -> list[dict]:
    """The most popular studio albums of one artist, one entry per title.

    Deezer lists every edition separately (deluxe, remaster, ...), so titles are compared
    after clean_title(). Full albums win over EPs; singles and compilations are skipped.
    """
    seen: set[str] = set()
    picked: list[dict] = []
    for kind in ALBUM_TYPES:
        pool = [a for a in raw if a.get("record_type") == kind]
        for a in sorted(pool, key=lambda a: (a.get("fans") or 0, a.get("release_date") or ""), reverse=True):
            key = norm_title(clean_title(a.get("title", "")))
            if not key or key in seen:
                continue
            seen.add(key)
            picked.append(a)
        if len(picked) >= limit:
            break
    return picked[:limit]


def interleave(groups: list[list], limit: int) -> list:
    """Round-robin over groups (best artist first) so one artist can't fill the list."""
    out = []
    for batch in zip_longest(*groups):
        out.extend(x for x in batch if x is not None)
        if len(out) >= limit:
            break
    return out[:limit]


def dedupe_tracks(tracks: list[dict]) -> list[dict]:
    """Drop repeats of the same song by the same artist (Deezer returns live/remaster copies)."""
    seen: set[tuple[str, str]] = set()
    out = []
    for t in tracks:
        key = (norm_artist((t.get("artist") or {}).get("name", "")), norm_title(clean_title(t.get("title", ""))))
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out
