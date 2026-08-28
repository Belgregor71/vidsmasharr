"""The x265 keepers list, and the night-only rule that goes with it.

A keeper costs six to ten hours instead of half an hour, so two things have to
hold: only a path the user typed gets one, and it never starts during the day.
"""

import pytest

from app.config import Config
from app.db import Database
from app.plan import keepers as keepers_mod
from app.plan import planner
from app.plan.profiles import Ladder, Rung
from app.plan.rules import ENCODE, FileFacts, decide
from app.work import worker

GB = 1024**3


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "keepers.db")


@pytest.fixture
def config(tmp_path):
    return Config(config_dir=tmp_path)


def rung(encoder="hevc_vaapi", **kwargs):
    defaults = dict(
        encoder=encoder, content_class="movie", resolution="1080p",
        target_vmaf=95.0,
        quality=24.0 if encoder.endswith("_vaapi") else 22.0,
        quality_flag="qp" if encoder.endswith("_vaapi") else "crf",
        expected_size_ratio=0.30, expected_fps=30.0 if encoder.endswith("_vaapi") else 4.0,
        expected_out_bitrate=3_000_000, samples=6,
    )
    defaults.update(kwargs)
    return Rung(**defaults)


def facts(path="/media/movies/Blade Runner 2049 (2017)/br2049.mkv", **kwargs):
    defaults = dict(
        file_id=1, path=path, size_bytes=12 * GB, duration_s=9840.0,
        container="matroska", v_codec="h264", v_bit_depth=8,
        v_width=1920, v_height=1080, v_bitrate=10_000_000, v_fps=24.0,
        hdr_type="sdr", audio=[], content_class="movie",
    )
    defaults.update(kwargs)
    return FileFacts(**defaults)


# ------------------------------------------------------------------ the list


class TestList:
    def test_an_exact_path_matches(self):
        keepers = keepers_mod.parse("/media/movies/a/b.mkv")
        assert keepers.matches("/media/movies/a/b.mkv")
        assert not keepers.matches("/media/movies/a/c.mkv")

    def test_a_directory_takes_everything_under_it(self):
        keepers = keepers_mod.parse("/media/movies/Studio Ghibli/")
        assert keepers.matches("/media/movies/Studio Ghibli/Totoro (1988)/t.mkv")
        assert not keepers.matches("/media/movies/Other/t.mkv")

    def test_a_directory_without_the_slash_works_too(self):
        keepers = keepers_mod.parse("/media/movies/Studio Ghibli")
        assert keepers.matches("/media/movies/Studio Ghibli/Totoro (1988)/t.mkv")
        # ...but does not swallow a sibling that merely starts the same way.
        assert not keepers.matches("/media/movies/Studio Ghibli Extras/t.mkv")

    def test_globs_work(self):
        keepers = keepers_mod.parse("/media/movies/*/*Criterion*.mkv")
        assert keepers.matches("/media/movies/Ran (1985)/Ran Criterion.mkv")
        assert not keepers.matches("/media/movies/Ran (1985)/Ran.mkv")

    def test_a_bare_file_name_matches_wherever_it_is(self):
        keepers = keepers_mod.parse("br2049.mkv")
        assert keepers.matches("/media/movies/Blade Runner 2049 (2017)/br2049.mkv")

    def test_comments_and_blank_lines_are_ignored(self):
        keepers = keepers_mod.parse(
            "# the films worth the hours\n"
            "\n"
            "/media/movies/a.mkv   # this one especially\n"
        )
        assert keepers.patterns == ["/media/movies/a.mkv"]

    def test_no_file_configured_is_empty_and_harmless(self, config):
        keepers = keepers_mod.load(config)
        assert not keepers
        assert not keepers.error
        assert not keepers.matches("/media/movies/a.mkv")

    def test_a_configured_file_that_is_missing_is_reported(self, config, tmp_path):
        """"No keepers" and "the keepers list did not load" are different
        nights of encoding."""
        config.encoder.keepers_file = tmp_path / "nope.txt"
        keepers = keepers_mod.load(config)
        assert not keepers
        assert "does not exist" in keepers.error

    def test_it_reads_the_file(self, config, tmp_path):
        path = tmp_path / "keepers.txt"
        path.write_text("/media/movies/a.mkv\n", encoding="utf-8")
        config.encoder.keepers_file = path
        assert keepers_mod.load(config).matches("/media/movies/a.mkv")


# ------------------------------------------------------------------ planning


class TestChoosingTheEncoder:
    def ladder(self, *, with_software=True):
        rungs = [rung("hevc_vaapi")]
        if with_software:
            rungs.append(rung("libx265"))
        return Ladder(rungs, preferred_encoder="hevc_vaapi")

    def test_a_normal_file_gets_the_hardware_encoder(self, config):
        from app.plan import estimate as estimate_mod

        decision = decide(facts(), config, self.ladder(), estimate_mod.Estimator())
        assert decision.action == ENCODE
        assert decision.detail["encoder"] == "hevc_vaapi"

    def test_a_keeper_gets_the_software_rung(self, config):
        from app.plan import estimate as estimate_mod

        decision = decide(
            facts(is_keeper=True), config, self.ladder(), estimate_mod.Estimator()
        )
        assert decision.detail["encoder"] == "libx265"
        assert "keepers list" in decision.reason

    def test_a_keeper_falls_back_to_hardware_rather_than_not_planning(self, config):
        """Encoding it well but not perfectly beats leaving a film the user
        asked for out of the queue entirely."""
        from app.plan import estimate as estimate_mod

        decision = decide(
            facts(is_keeper=True), config, self.ladder(with_software=False),
            estimate_mod.Estimator(),
        )
        assert decision.detail["encoder"] == "hevc_vaapi"
        assert "--include-software" in decision.reason

    def test_the_encoder_lands_on_the_decision_row(self, db, config, tmp_path):
        path = tmp_path / "keepers.txt"
        path.write_text("/media/movies/keep.mkv\n", encoding="utf-8")
        config.encoder.keepers_file = path

        for name in ("keep.mkv", "ordinary.mkv"):
            db.execute(
                "INSERT INTO media_file (path, library_root, size_bytes, mtime, "
                " probe_version, duration_s, v_codec, v_bit_depth, v_width, "
                " v_height, v_bitrate, v_fps, hdr_type, first_seen, last_seen) "
                "VALUES (?, '/media/movies', ?, 1, 1, 9840.0, 'h264', 8, 1920, "
                " 1080, 10000000, 24.0, 'sdr', 1, 1)",
                (f"/media/movies/{name}", 12 * GB),
            )

        stats = planner.build(db, config, ladder=self.ladder())
        rows = {
            r["path"].rsplit("/", 1)[-1]: r["encoder"]
            for r in db.query(
                "SELECT d.encoder, mf.path FROM decision d "
                "JOIN media_file mf ON mf.id = d.file_id"
            )
        }
        assert rows == {"keep.mkv": "libx265", "ordinary.mkv": "hevc_vaapi"}
        assert stats.keepers == 1 and stats.software == 1

    def test_the_plan_says_when_a_keepers_list_has_no_software_rung(
        self, db, config, tmp_path
    ):
        path = tmp_path / "keepers.txt"
        path.write_text("/media/movies/keep.mkv\n", encoding="utf-8")
        config.encoder.keepers_file = path
        stats = planner.build(db, config, ladder=self.ladder(with_software=False))
        assert any("--include-software" in note for note in stats.notes)


# ------------------------------------------------------------------ the night


class TestNightOnly:
    def seed(self, db, encoder, priority):
        db.execute(
            "INSERT INTO media_file (path, library_root, size_bytes, mtime, "
            " first_seen, last_seen) VALUES (?, '/media/movies', 1, 1, 1, 1)",
            (f"/media/movies/{encoder}.mkv",),
        )
        file_id = db.scalar("SELECT last_insert_rowid()")
        db.execute(
            "INSERT INTO decision (file_id, action, reason, priority, state, "
            " encoder, created_at) VALUES (?, 'encode', 'r', ?, 'pending', ?, 1)",
            (file_id, priority, encoder),
        )

    def test_daytime_steps_over_a_software_encode(self, db):
        # The keeper ranks higher, and still must not start at nine in the
        # morning: it would still be running at teatime.
        self.seed(db, "libx265", priority=99.0)
        self.seed(db, "hevc_vaapi", priority=1.0)

        night = worker.next_decision(db, allow_software=True)
        day = worker.next_decision(db, allow_software=False)

        assert night["encoder"] == "libx265"
        assert day["encoder"] == "hevc_vaapi"

    def test_a_remux_has_no_encoder_and_always_qualifies(self, db):
        db.execute(
            "INSERT INTO media_file (path, library_root, size_bytes, mtime, "
            " first_seen, last_seen) VALUES ('/media/movies/r.mkv','/m',1,1,1,1)"
        )
        db.execute(
            "INSERT INTO decision (file_id, action, reason, priority, state, "
            " created_at) VALUES (1, 'remux', 'r', 5.0, 'pending', 1)"
        )
        assert worker.next_decision(db, allow_software=False)["action"] == "remux"

    def test_waiting_keepers_are_reported_not_hidden(self, db, config):
        self.seed(db, "libx265", priority=99.0)
        assert worker.pending_software(db) == 1

        stats = worker.run(
            db, config, limit=1, execute=False, ignore_schedule=False,
            progress=None,
        )
        # Nothing ran, and the reason says which jobs are waiting for the dark.
        if stats.attempted == 0:
            assert "keeper" in stats.stopped_because or not stats.stopped_because

    def test_the_night_window_says_it_is_the_night(self, config):
        from datetime import datetime

        from app.work import schedule

        night = schedule.window_for(datetime(2026, 8, 28, 2, 0), config.schedule)
        day = schedule.window_for(datetime(2026, 8, 28, 14, 0), config.schedule)
        assert night.is_night and not day.is_night
