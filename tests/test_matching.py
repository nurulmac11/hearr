from app.matching import (AUTO_ACCEPT, artist_matches, clean_title, norm_artist, norm_title,
                          score_candidate)


def test_clean_title_drops_edition_and_features():
    assert clean_title("Hybrid Theory (20th Anniversary Edition)") == "Hybrid Theory"
    assert clean_title("Dracula (with JENNIE)") == "Dracula"
    assert clean_title("Dai Dai (feat. Burna Boy)") == "Dai Dai"
    assert clean_title("OK Computer - Remastered") == "OK Computer"
    assert clean_title("Kid A Mnesia") == "Kid A Mnesia"
    # Brackets that are part of the name stay.
    assert clean_title("(What's the Story) Morning Glory?") == "(What's the Story) Morning Glory?"


def test_turkish_folding():
    assert norm_title("Gülümse") == norm_title("Gulumse")
    assert norm_artist("Sezen Aksu") == norm_artist("SEZEN AKSU")
    assert norm_title("Işık") == "isik"


def test_artist_matching():
    assert artist_matches("The Clash", "Clash")
    assert artist_matches("Beyoncé", "Beyonce")
    assert not artist_matches("Radiohead", "Alex Schaaf")


def test_scoring_prefers_studio_release():
    studio = score_candidate("Linkin Park", "From Zero", 2024, "album",
                             "Linkin Park", "From Zero", 2024, "Album", [])
    acapella = score_candidate("Linkin Park", "From Zero", 2024, "album",
                               "Linkin Park", "From Zero: A Cappellas", 2025, "Album", ["Remix"])
    assert studio >= AUTO_ACCEPT
    assert acapella < studio
    assert acapella < AUTO_ACCEPT


def test_deluxe_matches_standard():
    s = score_candidate("Radiohead", "OK Computer (Deluxe Edition)", 1997, "album",
                        "Radiohead", "OK Computer", 1997, "Album", [])
    assert s >= AUTO_ACCEPT


def test_wrong_artist_scores_zero():
    assert score_candidate("Radiohead", "Kid A", 2000, "album",
                           "Wooden Elephant", "Kid A", 2021, "Album", []) == 0


def test_quality_estimates():
    from app.main import _quality_kbps

    def profile(*names):
        return {"items": [{"allowed": True, "quality": {"name": n}, "items": []} for n in names]}

    standard = _quality_kbps(profile("MP3-192", "MP3-320", "AAC-256", "OGG Vorbis Q10"))
    assert standard["typical"] == 320 and standard["high"] == 320 and standard["low"] == 192
    lossless = _quality_kbps(profile("FLAC", "ALAC", "FLAC 24bit"))
    assert lossless["typical"] == 900 and lossless["high"] == 2800
    anyq = _quality_kbps(profile("MP3-128", "MP3-320", "FLAC", "FLAC 24bit"))
    assert anyq["typical"] == 900 and anyq["low"] == 128 and anyq["high"] == 2800
    # 56 minutes at Standard ≈ 134 MB (Deadbeat measured 129 MB on disk)
    assert round(standard["typical"] * 125 * 56 * 60 / 1e6) == 134


def test_chart_config():
    from app.config import _parse_charts

    charts = _parse_charts("tr, global, Chill=1234567, bogus")
    assert list(charts) == ["tr", "global", "p1234567"]
    assert charts["tr"]["playlist_id"] == 1116189071
    assert charts["p1234567"] == {"name": "Chill", "playlist_id": 1234567}
    assert list(_parse_charts("")) == ["global"]
