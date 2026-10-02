"""Name normalisation and scoring used to match Deezer items to Lidarr's metadata."""

import re
import unicodedata
from difflib import SequenceMatcher

# Bracketed or dashed suffixes that describe an edition rather than the release itself.
_EDITION_WORDS = (
    "deluxe", "remaster", "remastered", "edition", "expanded", "anniversary", "bonus",
    "explicit", "clean", "version", "reissue", "special", "collector", "super",
    "single", "ep", "radio edit", "extended", "mono", "stereo",
)
_FEAT = re.compile(r"\s*[\(\[]?\s*(feat\.?|ft\.?|featuring|with)\s+[^\)\]]*[\)\]]?", re.I)
_BRACKETS = re.compile(r"\s*[\(\[]([^\)\]]*)[\)\]]")
_DASH_SUFFIX = re.compile(r"\s+[-–—]\s+([^-–—]+)$")
_TURKISH = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
                          "ç": "c", "Ç": "c", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u"})


def _is_edition(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in _EDITION_WORDS)


def fold(text: str) -> str:
    """Lowercase, strip accents (Turkish letters included) and punctuation."""
    text = (text or "").translate(_TURKISH)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_title(title: str) -> str:
    """Drop 'feat.' credits and edition markers, keeping the core release title."""
    t = _FEAT.sub("", title or "")
    t = _BRACKETS.sub(lambda m: "" if _is_edition(m.group(1)) else m.group(0), t)
    m = _DASH_SUFFIX.search(t)
    if m and _is_edition(m.group(1)):
        t = t[: m.start()]
    return t.strip()


def norm_title(title: str) -> str:
    return fold(clean_title(title))


def norm_artist(name: str) -> str:
    n = fold(name)
    return n[4:] if n.startswith("the ") else n


_FORMER_NAME = re.compile(r"\b(?:formerly|previously|aka|a k a|also known as)\s+(?:the\s+)?(.+)")


def artist_matches(a: str, b: str, b_disambiguation: str | None = None) -> bool:
    """True if a and b name the same artist. MusicBrainz keeps renamed artists under the new
    name with a disambiguation like "formerly Kanye West", so that counts as a match too."""
    na, nb = norm_artist(a), norm_artist(b)
    if not na or not nb:
        return False
    if na == nb or SequenceMatcher(None, na, nb).ratio() >= 0.92:
        return True
    m = _FORMER_NAME.search(fold(b_disambiguation))
    return bool(m) and norm_artist(m.group(1)) == na


def title_similarity(a: str, b: str) -> float:
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


DEEZER_TO_LIDARR_TYPE = {"album": "Album", "ep": "EP", "single": "Single", "compile": "Album"}


def score_candidate(dz_artist: str, dz_title: str, dz_year: int | None, dz_type: str | None,
                    cand_artist: str, cand_title: str, cand_year: int | None,
                    cand_type: str | None, cand_secondary: list[str],
                    cand_disambiguation: str | None = None) -> float:
    """0..1 confidence that a Lidarr lookup result is the Deezer release."""
    if not artist_matches(dz_artist, cand_artist, cand_disambiguation):
        return 0.0
    score = title_similarity(dz_title, cand_title)
    if dz_year and cand_year:
        diff = abs(dz_year - cand_year)
        score += 0.05 if diff <= 1 else (-0.05 if diff > 3 else 0)
    if dz_type and cand_type and DEEZER_TO_LIDARR_TYPE.get(dz_type) == cand_type:
        score += 0.03
    # Live, remix, demo and compilation variants rank below the studio release.
    if cand_secondary:
        score -= 0.08
    return max(0.0, min(score, 1.0))


AUTO_ACCEPT = 0.9
# Below this a candidate is almost certainly a different release, so don't offer it.
SUGGEST_MIN = 0.6
