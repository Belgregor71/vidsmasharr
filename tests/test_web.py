import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.dedupe import groups
from app.web.app import create_app, human_bytes, human_duration

GB = 1024**3


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "web.db")


@pytest.fixture
def client(db, tmp_path):
    config = Config(config_dir=tmp_path)
    return TestClient(create_app(config=config, db=db))


def seed_duplicate(db, *, name="The Show"):
    db.execute(
        "INSERT INTO title (kind, source, external_id, name, year) "
        "VALUES ('show','plex','plex://show/a',?,2019)",
        (name,),
    )
    title_id = db.scalar("SELECT last_insert_rowid()")
    ids = []
    for path, width, height, size in (
        ("/media/tv/show.1080p.mkv", 1920, 1080, 4 * GB),
        ("/media/tv/show.720p.mkv", 1280, 720, 1 * GB),
    ):
        now = time.time()
        db.execute(
            "INSERT INTO media_file (path, library_root, size_bytes, mtime, probe_version, "
            "probed_at, container, duration_s, v_codec, v_bit_depth, v_width, v_height, "
            "hdr_type, audio_json, first_seen, last_seen, missing) "
            "VALUES (?,?,?,?,1,?,'matroska,webm',2700.0,'h264',8,?,?,'sdr',?,?,?,0)",
            (path, "/media/tv", size, now, now, width, height,
             json.dumps([{"codec": "eac3", "channels": 6}]), now, now),
        )
        file_id = db.scalar("SELECT last_insert_rowid()")
        ids.append(file_id)
        db.execute(
            "INSERT INTO file_title (file_id, title_id, season, episode, resolved_by, confidence) "
            "VALUES (?,?,1,1,'plex',1.0)",
            (file_id, title_id),
        )
    groups.build(db)
    return ids


class TestFilters:
    def test_bytes(self):
        assert human_bytes(0) == "0 B"
        assert human_bytes(1536) == "2 KB"
        assert human_bytes(4 * GB) == "4.00 GB"

    def test_duration(self):
        assert human_duration(None) == "-"
        assert human_duration(2700) == "45m"
        assert human_duration(7200) == "2h 00m"


class TestPages:
    def test_dashboard_empty(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "No files indexed yet" in response.text

    def test_dashboard_with_data(self, client, db):
        seed_duplicate(db)
        response = client.get("/")
        assert response.status_code == 200
        assert "h264" in response.text
        assert "reclaimable" in response.text

    def test_duplicates_page_lists_the_group(self, client, db):
        seed_duplicate(db)
        response = client.get("/duplicates")
        assert response.status_code == 200
        assert "The Show" in response.text
        assert "1.00 GB" in response.text  # reclaimable = the 720p copy

    def test_duplicates_filter_needs_human(self, client, db):
        seed_duplicate(db)
        assert client.get("/duplicates?only=needs_human").status_code == 200
        assert client.get("/duplicates?only=clear").status_code == 200

    def test_library_page_and_filters(self, client, db):
        seed_duplicate(db)
        assert "show.1080p.mkv" in client.get("/library").text
        assert client.get("/library?codec=h264&tier=1080p&sort=size").status_code == 200
        assert "show.720p.mkv" not in client.get("/library?tier=1080p").text

    def test_library_search(self, client, db):
        seed_duplicate(db)
        assert "show.720p.mkv" in client.get("/library?q=720p").text


class TestActionsAreReportOnly:
    def test_choosing_a_keeper_records_the_choice(self, client, db):
        ids = seed_duplicate(db)
        group_id = db.scalar("SELECT id FROM duplicate_group")

        response = client.post(
            f"/duplicates/{group_id}/keeper", data={"file_id": ids[1]},
            follow_redirects=False,
        )
        assert response.status_code == 303

        row = db.one("SELECT * FROM duplicate_group")
        assert row["keeper_file_id"] == ids[1]
        assert row["status"] == "chosen"
        # Reclaimable is recomputed against the new keeper.
        assert row["reclaimable_bytes"] == 4 * GB

    def test_dismiss_survives_a_rebuild(self, client, db):
        seed_duplicate(db)
        group_id = db.scalar("SELECT id FROM duplicate_group")
        client.post(f"/duplicates/{group_id}/dismiss", follow_redirects=False)

        groups.build(db)
        assert db.scalar("SELECT status FROM duplicate_group") == "dismissed"

    def test_no_action_touches_media_file_rows(self, client, db):
        ids = seed_duplicate(db)
        before = db.query("SELECT id, path, size_bytes FROM media_file ORDER BY id")
        group_id = db.scalar("SELECT id FROM duplicate_group")

        client.post(f"/duplicates/{group_id}/keeper", data={"file_id": ids[1]})
        client.post(f"/duplicates/{group_id}/dismiss")
        client.post(f"/duplicates/{group_id}/reopen")

        after = db.query("SELECT id, path, size_bytes FROM media_file ORDER BY id")
        assert [tuple(r) for r in before] == [tuple(r) for r in after]
        # And nothing was queued for encoding either.
        assert db.scalar("SELECT COUNT(*) FROM decision") == 0
        assert db.scalar("SELECT COUNT(*) FROM job") == 0

    def test_keeper_must_be_a_member_of_the_group(self, client, db):
        seed_duplicate(db)
        group_id = db.scalar("SELECT id FROM duplicate_group")
        original = db.scalar("SELECT keeper_file_id FROM duplicate_group")

        client.post(f"/duplicates/{group_id}/keeper", data={"file_id": 99999})

        assert db.scalar("SELECT keeper_file_id FROM duplicate_group") == original


class TestPlanPage:
    def test_empty_plan_explains_how_to_build_one(self, client):
        response = client.get("/plan")
        assert response.status_code == 200
        assert "app plan" in response.text

    def test_shows_the_queue_ranked_by_value(self, client, db, tmp_path):
        from app.config import Config
        from app.plan import planner
        from tests.test_plan import add_file, ladder_for_tests

        add_file(db, "/media/tv/lean.mkv", size=3 * GB, bitrate=8_000_000)
        add_file(db, "/media/tv/fat.mkv", size=9 * GB, bitrate=26_000_000)
        planner.build(db, Config(config_dir=tmp_path), ladder=ladder_for_tests())

        response = client.get("/plan")
        assert response.status_code == 200
        assert "fat.mkv" in response.text
        # Best value first: the fat file outranks the lean one on the page.
        assert response.text.index("fat.mkv") < response.text.index("lean.mkv")

    def test_a_provisional_plan_says_so_loudly(self, client, db, tmp_path):
        from app.config import Config
        from app.plan import planner
        from app.plan.profiles import provisional_ladder
        from tests.test_plan import add_file

        config = Config(config_dir=tmp_path)
        add_file(db, "/media/tv/a.mkv")
        planner.build(db, config, ladder=provisional_ladder(config.policy))

        response = client.get("/plan")
        assert "Provisional" in response.text


class TestActivityPage:
    def test_an_empty_activity_page_explains_how_to_start(self, client):
        response = client.get("/activity")
        assert response.status_code == 200
        assert "app work" in response.text

    def test_completed_work_shows_actual_against_predicted(self, client, db):
        now = time.time()
        db.execute(
            "INSERT INTO media_file (path, library_root, size_bytes, mtime, "
            "first_seen, last_seen) VALUES ('/media/tv/show.mkv','/media/tv',?,?,?,?)",
            (4 * GB, now, now, now),
        )
        file_id = db.scalar("SELECT last_insert_rowid()")
        db.execute(
            "INSERT INTO decision (file_id, action, reason, state, created_at) "
            "VALUES (?, 'encode', 'because', 'done', ?)",
            (file_id, now),
        )
        decision_id = db.scalar("SELECT last_insert_rowid()")
        db.execute(
            "INSERT INTO job (decision_id, state, attempts) VALUES (?,'done',1)",
            (decision_id,),
        )
        job_id = db.scalar("SELECT last_insert_rowid()")
        db.execute(
            "INSERT INTO outcome (job_id, file_path, action, before_bytes, "
            "after_bytes, saved_bytes, est_saved_bytes, cpu_seconds, vmaf_mean, "
            "original_deleted, completed_at) "
            "VALUES (?, '/media/tv/show.mkv', 'encode', ?, ?, ?, ?, 2400, 94.6, 1, ?)",
            (job_id, 4 * GB, 1 * GB, 3 * GB, 2 * GB, time.time()),
        )

        response = client.get("/activity")
        assert "show.mkv" in response.text
        assert "94.6" in response.text
        assert "deleted" in response.text

    def test_the_header_banner_tells_the_truth_about_deletion(self, db, tmp_path):
        from fastapi.testclient import TestClient
        from app.web.app import create_app

        config = Config(config_dir=tmp_path)
        config.safety.dry_run = False
        config.safety.delete_original_on_success = True
        client = TestClient(create_app(config=config, db=db))

        assert "originals are deleted" in client.get("/").text

    def test_the_banner_says_dry_run_by_default(self, client):
        assert "nothing is encoded or deleted" in client.get("/").text
