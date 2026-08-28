import json
import time

import pytest

from app.db import Database
from app.dedupe import groups

GB = 1024**3


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "dedupe.db")


def add_title(db, name="Show", kind="show", external_id="tvdb:1", year=2019):
    db.execute(
        "INSERT INTO title (kind, source, external_id, name, year) VALUES (?,?,?,?,?)",
        (kind, "plex", external_id, name, year),
    )
    return db.scalar("SELECT last_insert_rowid()")


def add_file(
    db, title_id, path, *, size=GB, width=1920, height=1080, codec="h264",
    hdr="sdr", depth=8, duration=2700.0, container="matroska,webm", audio=None,
    season=1, episode=1,
):
    now = time.time()
    db.execute(
        "INSERT INTO media_file "
        "(path, library_root, size_bytes, mtime, probe_version, probed_at, container, "
        " duration_s, v_codec, v_bit_depth, v_width, v_height, hdr_type, audio_json, "
        " first_seen, last_seen, missing) "
        "VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,0)",
        (path, "/media/tv", size, now, now, container, duration, codec, depth,
         width, height, hdr, json.dumps(audio or [{"codec": "eac3", "channels": 6}]),
         now, now),
    )
    file_id = db.scalar("SELECT last_insert_rowid()")
    db.execute(
        "INSERT INTO file_title (file_id, title_id, season, episode, resolved_by, confidence) "
        "VALUES (?,?,?,?, 'plex', 1.0)",
        (file_id, title_id, season, episode),
    )
    return file_id


class TestKeeperChoice:
    def test_higher_resolution_wins_even_though_it_is_bigger(self, db):
        title = add_title(db)
        add_file(db, title, "/media/tv/a.720p.mkv", width=1280, height=720, size=1 * GB)
        best = add_file(db, title, "/media/tv/a.1080p.mkv", size=4 * GB)

        groups.build(db)
        row = db.one("SELECT * FROM duplicate_group")
        assert row["keeper_file_id"] == best

    def test_reclaimable_excludes_the_keeper(self, db):
        title = add_title(db)
        add_file(db, title, "/media/tv/a.720p.mkv", width=1280, height=720, size=1 * GB)
        add_file(db, title, "/media/tv/a.1080p.mkv", size=4 * GB)

        groups.build(db)
        row = db.one("SELECT * FROM duplicate_group")
        assert row["reclaimable_bytes"] == 1 * GB

    def test_lossless_audio_breaks_a_tie(self, db):
        title = add_title(db)
        plain = add_file(db, title, "/media/tv/a.mkv",
                         audio=[{"codec": "eac3", "channels": 6}])
        rich = add_file(db, title, "/media/tv/b.mkv",
                        audio=[{"codec": "truehd", "channels": 8}])

        groups.build(db)
        row = db.one("SELECT * FROM duplicate_group")
        assert row["keeper_file_id"] == rich
        assert plain != rich

    def test_members_are_ranked_and_stored(self, db):
        title = add_title(db)
        add_file(db, title, "/media/tv/a.sd.mkv", width=720, height=480)
        add_file(db, title, "/media/tv/a.1080p.mkv")

        groups.build(db)
        ranks = db.query(
            "SELECT file_id, rank, score_json FROM duplicate_member ORDER BY rank"
        )
        assert [r["rank"] for r in ranks] == [0, 1]
        assert json.loads(ranks[0]["score_json"])["tier"] == "1080p"


class TestNeedsHuman:
    def test_different_runtimes_are_flagged_not_ranked_silently(self, db):
        title = add_title(db, kind="movie", external_id="tmdb:9")
        add_file(db, title, "/media/movies/theatrical.mkv", duration=7200.0,
                 season=None, episode=None)
        add_file(db, title, "/media/movies/extended.mkv", duration=9000.0,
                 season=None, episode=None)

        groups.build(db)
        row = db.one("SELECT * FROM duplicate_group")
        assert row["needs_human"] == 1
        assert "cuts" in row["reason"]

    def test_hdr_versus_resolution_tradeoff_is_flagged(self, db):
        title = add_title(db, kind="movie", external_id="tmdb:10")
        add_file(db, title, "/media/movies/4k.hdr.mkv", width=3840, height=2160,
                 hdr="hdr10", depth=10, size=40 * GB, season=None, episode=None)
        add_file(db, title, "/media/movies/1080p.sdr.mkv", size=8 * GB,
                 season=None, episode=None)

        groups.build(db)
        row = db.one("SELECT * FROM duplicate_group")
        assert row["needs_human"] == 1
        assert "HDR" in row["reason"]

    def test_near_identical_copies_are_flagged_as_unrankable(self, db):
        title = add_title(db)
        add_file(db, title, "/media/tv/a.mkv", size=3 * GB)
        add_file(db, title, "/media/tv/b.mkv", size=3 * GB)

        groups.build(db)
        row = db.one("SELECT * FROM duplicate_group")
        assert row["needs_human"] == 1

    def test_clear_win_is_not_flagged(self, db):
        title = add_title(db)
        add_file(db, title, "/media/tv/a.sd.mkv", width=720, height=480)
        add_file(db, title, "/media/tv/a.1080p.mkv")

        groups.build(db)
        row = db.one("SELECT * FROM duplicate_group")
        assert row["needs_human"] == 0


class TestGrouping:
    def test_different_episodes_are_not_duplicates(self, db):
        title = add_title(db)
        add_file(db, title, "/media/tv/s01e01.mkv", episode=1)
        add_file(db, title, "/media/tv/s01e02.mkv", episode=2)

        stats = groups.build(db)
        assert stats.groups == 0

    def test_movies_group_on_null_season_and_episode(self, db):
        title = add_title(db, kind="movie", external_id="tmdb:11")
        add_file(db, title, "/media/movies/a.mkv", season=None, episode=None)
        add_file(db, title, "/media/movies/b.mkv", season=None, episode=None,
                 width=1280, height=720)

        stats = groups.build(db)
        assert stats.groups == 1

    def test_unprobed_files_are_ignored(self, db):
        title = add_title(db)
        add_file(db, title, "/media/tv/a.mkv")
        second = add_file(db, title, "/media/tv/b.mkv")
        db.execute("UPDATE media_file SET probe_version=NULL WHERE id=?", (second,))

        stats = groups.build(db)
        assert stats.groups == 0

    def test_missing_files_are_ignored(self, db):
        title = add_title(db)
        add_file(db, title, "/media/tv/a.mkv")
        second = add_file(db, title, "/media/tv/b.mkv")
        db.execute("UPDATE media_file SET missing=1 WHERE id=?", (second,))

        stats = groups.build(db)
        assert stats.groups == 0


class TestRebuild:
    def test_rebuild_is_idempotent(self, db):
        title = add_title(db)
        add_file(db, title, "/media/tv/a.sd.mkv", width=720, height=480)
        add_file(db, title, "/media/tv/a.1080p.mkv")

        groups.build(db)
        groups.build(db)
        assert db.scalar("SELECT COUNT(*) FROM duplicate_group") == 1
        assert db.scalar("SELECT COUNT(*) FROM duplicate_member") == 2

    def test_a_decided_group_survives_a_rebuild(self, db):
        title = add_title(db)
        add_file(db, title, "/media/tv/a.sd.mkv", width=720, height=480)
        add_file(db, title, "/media/tv/a.1080p.mkv")
        groups.build(db)

        db.execute("UPDATE duplicate_group SET status='dismissed'")
        stats = groups.build(db)

        assert stats.preserved == 1
        row = db.one("SELECT status FROM duplicate_group")
        assert row["status"] == "dismissed"

    def test_nothing_is_ever_deleted_from_media_file(self, db):
        title = add_title(db)
        add_file(db, title, "/media/tv/a.sd.mkv", width=720, height=480)
        add_file(db, title, "/media/tv/a.1080p.mkv")

        groups.build(db)
        # The whole module is report-only; the files table is untouched.
        assert db.scalar("SELECT COUNT(*) FROM media_file") == 2
        assert db.scalar("SELECT COUNT(*) FROM decision") == 0
