from app.recommend import best_albums, dedupe_tracks, interleave


def alb(title, fans, kind="album", date="2020-01-01"):
    return {"title": title, "fans": fans, "record_type": kind, "release_date": date}


def test_best_albums_prefers_popular_full_albums_and_collapses_editions():
    raw = [
        alb("Kid A", 900), alb("Kid A (Deluxe Edition)", 100), alb("OK Computer", 1500),
        alb("In Rainbows - Remastered", 700), alb("Creep", 5000, "single"), alb("Greatest Hits", 4000, "compile"),
        alb("Airbag EP", 300, "ep"),
    ]
    assert [a["title"] for a in best_albums(raw, 3)] == ["OK Computer", "Kid A", "In Rainbows - Remastered"]


def test_best_albums_falls_back_to_eps():
    raw = [alb("Only EP", 10, "ep"), alb("A Single", 99, "single")]
    assert [a["title"] for a in best_albums(raw, 2)] == ["Only EP"]


def test_interleave_is_round_robin_and_limited():
    assert interleave([[1, 2, 3], [4], [5, 6]], 10) == [1, 4, 5, 2, 6, 3]
    assert interleave([[1, 2, 3], [4], [5, 6]], 4) == [1, 4, 5, 2]
    assert interleave([], 5) == []


def test_dedupe_tracks_drops_repeat_songs_by_same_artist():
    t = lambda title, artist: {"title": title, "artist": {"name": artist}}
    out = dedupe_tracks([t("Creep", "Radiohead"), t("Creep - Remastered", "Radiohead"), t("Creep", "Other")])
    assert [(x["title"], x["artist"]["name"]) for x in out] == [("Creep", "Radiohead"), ("Creep", "Other")]
