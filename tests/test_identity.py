import sqlite3
import time

import pytest

from app.config import Config, PlexConfig
from app.db import Database
from app.identity import plex, resolve

GB = 1024**3


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "identity.db")


def add_media_file(db, path, size=GB):
    now = time.time()
    db.execute(
        "INSERT INTO media_file (path, library_root, size_bytes, mtime, probe_version, "
        "first_seen, last_seen, missing) VALUES (?,?,?,?,1,?,?,0)",
        (path, "/media/tv", size, now, now, now),
    )
    return db.scalar("SELECT last_insert_rowid()")


def make_plex_db(directory, rows):
    """Minimal stand-in for the Plex library database.

    Only the three tables and the columns we actually read.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / plex.PLEX_DB_NAME
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE metadata_items (
            id INTEGER PRIMARY KEY, library_section_id INTEGER, metadata_type INTEGER,
            guid TEXT, title TEXT, year INTEGER, "index" INTEGER, parent_id INTEGER
        );
        CREATE TABLE media_items (id INTEGER PRIMARY KEY, metadata_item_id INTEGER);
        CREATE TABLE media_parts (
            id INTEGER PRIMARY KEY, media_item_id INTEGER, file TEXT, size INTEGER
        );
        """
    )
    for row in rows:
        conn.execute(
            'INSERT INTO metadata_items (id, metadata_type, guid, title, year, "index", parent_id)'
            " VALUES (?,?,?,?,?,?,?)",
            row["item"],
        )
    conn.commit()
    return path


def seed_show(conn_path, *, show_id=10, season_id=11, episode_id=12,
              show_title="The Show", guid="plex://show/abc", season=2, episode=5,
              file="/volume1/data/media/tv/The Show/Season 02/ep.mkv"):
    conn = sqlite3.connect(conn_path)
    conn.execute(
        'INSERT INTO metadata_items (id, metadata_type, guid, title, year, "index", parent_id)'
        " VALUES (?,?,?,?,?,?,?)",
        (show_id, plex.TYPE_SHOW, guid, show_title, 2019, None, None),
    )
    conn.execute(
        'INSERT INTO metadata_items (id, metadata_type, guid, title, year, "index", parent_id)'
        " VALUES (?,?,?,?,?,?,?)",
        (season_id, plex.TYPE_SEASON, "plex://season/x", "Season 2", None, season, show_id),
    )
    conn.execute(
        'INSERT INTO metadata_items (id, metadata_type, guid, title, year, "index", parent_id)'
        " VALUES (?,?,?,?,?,?,?)",
        (episode_id, plex.TYPE_EPISODE, "plex://episode/y", "An Episode", None,
         episode, season_id),
    )
    conn.execute("INSERT INTO media_items (id, metadata_item_id) VALUES (?,?)",
                 (episode_id * 10, episode_id))
    conn.execute("INSERT INTO media_parts (id, media_item_id, file, size) VALUES (?,?,?,?)",
                 (episode_id * 100, episode_id * 10, file, GB))
    conn.commit()
    conn.close()


def seed_movie(conn_path, *, movie_id=20, title="A Movie", year=2016,
               guid="plex://movie/def",
               file="/volume1/data/media/movies/A Movie (2016)/movie.mkv"):
    conn = sqlite3.connect(conn_path)
    conn.execute(
        'INSERT INTO metadata_items (id, metadata_type, guid, title, year, "index", parent_id)'
        " VALUES (?,?,?,?,?,?,?)",
        (movie_id, plex.TYPE_MOVIE, guid, title, year, None, None),
    )
    conn.execute("INSERT INTO media_items (id, metadata_item_id) VALUES (?,?)",
                 (movie_id * 10, movie_id))
    conn.execute("INSERT INTO media_parts (id, media_item_id, file, size) VALUES (?,?,?,?)",
                 (movie_id * 100, movie_id * 10, file, GB))
    conn.commit()
    conn.close()


class TestPathMapping:
    def test_longest_prefix_wins(self):
        mapping = {
            "/volume1/data/media": "/media",
            "/volume1/data/media/tv": "/media/tv-special",
        }
        assert plex.map_path("/volume1/data/media/tv/x.mkv", mapping) == "/media/tv-special/x.mkv"

    def test_unmapped_path_is_unchanged(self):
        assert plex.map_path("/elsewhere/x.mkv", {"/volume1": "/media"}) == "/elsewhere/x.mkv"

    def test_case_matters(self):
        # /volume1/Media is a real decoy on this NAS; a case-insensitive match
        # here would silently point the whole run at the wrong share.
        mapping = {"/volume1/data/media": "/media"}
        assert plex.map_path("/volume1/Data/Media/x.mkv", mapping) == "/volume1/Data/Media/x.mkv"


class TestPlexRead:
    def test_episode_identity_belongs_to_the_show(self, tmp_path):
        path = make_plex_db(tmp_path / "plex", [])
        seed_show(path)
        matches = list(plex.read_matches(path, {"/volume1/data/media": "/media"}))

        assert len(matches) == 1
        match = matches[0]
        assert match.kind == "episode"
        assert match.title_kind == "show"
        assert match.external_id == "plex://show/abc"
        assert (match.name, match.season, match.episode) == ("The Show", 2, 5)
        assert match.path == "/media/tv/The Show/Season 02/ep.mkv"

    def test_movie_identity(self, tmp_path):
        path = make_plex_db(tmp_path / "plex", [])
        seed_movie(path)
        matches = list(plex.read_matches(path, {"/volume1/data/media": "/media"}))

        assert matches[0].title_kind == "movie"
        assert (matches[0].name, matches[0].year) == ("A Movie", 2016)

    def test_orphan_episode_without_a_show_is_skipped(self, tmp_path):
        path = make_plex_db(tmp_path / "plex", [])
        conn = sqlite3.connect(path)
        conn.execute(
            'INSERT INTO metadata_items (id, metadata_type, guid, title, "index", parent_id)'
            " VALUES (?,?,?,?,?,?)",
            (5, plex.TYPE_EPISODE, "plex://episode/z", "Orphan", 1, None),
        )
        conn.execute("INSERT INTO media_items (id, metadata_item_id) VALUES (50, 5)")
        conn.execute(
            "INSERT INTO media_parts (id, media_item_id, file, size) VALUES (500, 50, ?, 1)",
            ("/volume1/data/media/tv/orphan.mkv",),
        )
        conn.commit()
        conn.close()

        assert list(plex.read_matches(path, {})) == []

    def test_live_database_is_copied_not_opened(self, tmp_path):
        source_dir = tmp_path / "plex"
        make_plex_db(source_dir, [])
        scratch = tmp_path / "scratch"
        copy = plex.copy_database(source_dir, scratch)

        assert copy.parent == scratch
        assert copy.exists()
        assert (source_dir / plex.PLEX_DB_NAME).exists()

    def test_missing_database_explains_the_appstore_trap(self, tmp_path):
        with pytest.raises(plex.PlexUnavailable) as exc:
            plex.copy_database(tmp_path / "nope", tmp_path / "scratch")
        assert "@appstore" in str(exc.value)


class TestResolve:
    def _config(self, tmp_path, plex_dir):
        return Config(
            config_dir=tmp_path,
            scratch_dir=tmp_path / "scratch",
            plex=PlexConfig(
                enabled=True, db_dir=plex_dir,
                path_map={"/volume1/data/media": "/media"},
            ),
        )

    def test_plex_match_wins_and_is_authoritative(self, db, tmp_path):
        plex_dir = tmp_path / "plex"
        make_plex_db(plex_dir, [])
        seed_show(plex_dir / plex.PLEX_DB_NAME)
        file_id = add_media_file(db, "/media/tv/The Show/Season 02/ep.mkv")

        stats = resolve.resolve(db, self._config(tmp_path, plex_dir))

        assert stats.by_source == {"plex": 1}
        row = db.one("SELECT * FROM file_title WHERE file_id=?", (file_id,))
        assert row["resolved_by"] == "plex"
        assert row["confidence"] == 1.0
        assert (row["season"], row["episode"]) == (2, 5)

    def test_filename_fallback_for_files_plex_does_not_know(self, db, tmp_path):
        plex_dir = tmp_path / "plex"
        make_plex_db(plex_dir, [])
        add_media_file(db, "/media/tv/Other Show/Season 01/Other.Show.S01E03.mkv")

        stats = resolve.resolve(db, self._config(tmp_path, plex_dir))

        assert stats.by_source == {"filename": 1}
        row = db.one("SELECT * FROM file_title")
        assert row["resolved_by"] == "filename"
        assert row["confidence"] < 1.0

    def test_filename_file_joins_the_title_plex_already_created(self, db, tmp_path):
        """The title-splitting case: two copies of one episode, one known to
        Plex and one not. They must land in the same title or the duplicate is
        never found."""
        plex_dir = tmp_path / "plex"
        make_plex_db(plex_dir, [])
        seed_show(plex_dir / plex.PLEX_DB_NAME)
        add_media_file(db, "/media/tv/The Show/Season 02/ep.mkv")
        add_media_file(db, "/media/tv/The Show/Season 02/The.Show.S02E05.720p.mkv")

        stats = resolve.resolve(db, self._config(tmp_path, plex_dir))

        assert db.scalar("SELECT COUNT(*) FROM title") == 1
        assert stats.titles_reused == 1
        rows = db.query("SELECT season, episode FROM file_title")
        assert {(r["season"], r["episode"]) for r in rows} == {(2, 5)}

    def test_ambiguous_name_does_not_merge_two_shows(self, db, tmp_path):
        plex_dir = tmp_path / "plex"
        make_plex_db(plex_dir, [])
        db.execute(
            "INSERT INTO title (kind, source, external_id, name, year) "
            "VALUES ('show','plex','plex://show/us','The Office',2005)"
        )
        db.execute(
            "INSERT INTO title (kind, source, external_id, name, year) "
            "VALUES ('show','plex','plex://show/uk','The Office',2001)"
        )
        seeded = {r["id"] for r in db.query("SELECT id FROM title")}
        add_media_file(db, "/media/tv/The Office/Season 01/The.Office.S01E01.mkv")

        resolve.resolve(db, self._config(tmp_path, plex_dir))

        # Two plausible homes and no year to choose between them. Inventing a
        # third title splits the group, which is safe; picking the wrong one
        # would tell a user two unrelated files are duplicates.
        row = db.one("SELECT title_id FROM file_title")
        assert row["title_id"] not in seeded
        assert db.scalar("SELECT COUNT(*) FROM title") == 3

    def test_rerun_updates_rather_than_duplicates(self, db, tmp_path):
        plex_dir = tmp_path / "plex"
        make_plex_db(plex_dir, [])
        seed_show(plex_dir / plex.PLEX_DB_NAME)
        add_media_file(db, "/media/tv/The Show/Season 02/ep.mkv")

        resolve.resolve(db, self._config(tmp_path, plex_dir))
        resolve.resolve(db, self._config(tmp_path, plex_dir))

        assert db.scalar("SELECT COUNT(*) FROM file_title") == 1
        assert db.scalar("SELECT COUNT(*) FROM title") == 1

    def test_unavailable_plex_is_reported_not_fatal(self, db, tmp_path):
        config = self._config(tmp_path, tmp_path / "does-not-exist")
        add_media_file(db, "/media/tv/Show/Season 01/Show.S01E01.mkv")

        stats = resolve.resolve(db, config)

        assert stats.warnings and "plex unavailable" in stats.warnings[0]
        # The run still resolves what it can from filenames.
        assert stats.by_source == {"filename": 1}
