"""Estimator calibration: measuring the estimator against what really happened.

The thing worth testing is not the arithmetic, it is the judgement around it --
that too few samples produce no correction, that one wild outcome does not move
the model, and that a corrected estimate is labelled so the next round does not
compound on it.
"""

import time

import pytest

from app.config import Config
from app.db import Database
from app.plan import calibrate
from app.plan import estimate as estimate_mod
from app.plan.profiles import Rung
from app.plan.rules import FileFacts

GB = 1024**3
MB = 1024**2


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "cal.db")


@pytest.fixture
def config(tmp_path):
    return Config(config_dir=tmp_path)


def record(db, *, basis="ladder-bitrate", est_out=1 * GB, after=1 * GB,
           est_cpu=3600.0, cpu=3600.0, encoder="hevc_vaapi", resolution="1080p",
           action="encode"):
    db.execute(
        "INSERT INTO outcome (job_id, file_path, action, before_bytes, after_bytes,"
        " saved_bytes, est_saved_bytes, cpu_seconds, est_cpu_seconds, completed_at,"
        " est_out_bytes, estimate_basis, encoder, resolution, content_class) "
        "VALUES (NULL,'/media/tv/a.mkv',?,?,?,?,?,?,?,?,?,?,?,?,'tv')",
        (action, 3 * GB, after, 3 * GB - after, 3 * GB - est_out, cpu, est_cpu,
         time.time(), est_out, basis, encoder, resolution),
    )


class TestMeasure:
    def test_no_outcomes_is_not_an_error(self, db):
        report = calibrate.measure(db)
        assert report.considered == 0
        assert report.calibration is None
        assert "needs `app work`" in report.summary()

    def test_a_consistent_bias_becomes_a_factor(self, db):
        # Every file came out 30% bigger than predicted.
        for _ in range(10):
            record(db, est_out=1 * GB, after=int(1.3 * GB))
        report = calibrate.measure(db)

        assert report.calibration is not None
        assert report.calibration.size_factor("ladder-bitrate") == pytest.approx(1.3, abs=0.01)

    def test_too_few_samples_produces_nothing(self, db):
        for _ in range(3):
            record(db, after=int(1.3 * GB))
        report = calibrate.measure(db)

        assert report.size[0].n == 3
        assert not report.size[0].usable
        assert report.calibration is None

    def test_one_wild_outcome_does_not_move_the_model(self, db):
        """The median is the whole reason a concert film cannot re-rank a
        months-long queue on its own."""
        for _ in range(9):
            record(db, est_out=1 * GB, after=1 * GB)
        record(db, est_out=1 * GB, after=20 * GB)

        report = calibrate.measure(db)
        assert report.calibration.size_factor("ladder-bitrate") == pytest.approx(1.0, abs=0.01)

    def test_models_are_measured_separately(self, db):
        for _ in range(10):
            record(db, basis="ladder-bitrate", est_out=1 * GB, after=int(1.2 * GB))
        for _ in range(10):
            record(db, basis="policy-target", est_out=1 * GB, after=int(0.6 * GB))

        factors = calibrate.measure(db).calibration.size_factors
        assert factors["ladder-bitrate"] == pytest.approx(1.2, abs=0.01)
        assert factors["policy-target"] == pytest.approx(0.6, abs=0.01)

    def test_speed_is_keyed_by_encoder_and_resolution(self, db):
        """SD runs four times faster than 1080p on this box. Pooling them is
        how the first benchmark projection came out 1.75x optimistic."""
        for _ in range(10):
            record(db, encoder="hevc_vaapi", resolution="1080p",
                   est_cpu=1000, cpu=1500)
        for _ in range(10):
            record(db, encoder="hevc_vaapi", resolution="sd", est_cpu=1000, cpu=500)

        calibration = calibrate.measure(db).calibration
        assert calibration.speed_factor("hevc_vaapi:1080p") == pytest.approx(1.5, abs=0.01)
        assert calibration.speed_factor("hevc_vaapi:sd") == pytest.approx(0.5, abs=0.01)

    def test_an_absurd_factor_is_clamped_and_flagged(self, db):
        for _ in range(10):
            record(db, est_out=1 * MB, after=1 * GB)
        report = calibrate.measure(db)

        assert report.size[0].factor == calibrate.MAX_FACTOR
        assert any("clamped" in w for w in report.warnings)

    def test_wide_disagreement_is_flagged(self, db):
        for ratio in (0.5, 0.6, 0.7, 2.5, 2.6, 2.7, 1.0, 1.1, 1.2, 1.3):
            record(db, est_out=1 * GB, after=int(ratio * GB))
        report = calibrate.measure(db)
        assert any("does not behave" in w for w in report.warnings)

    def test_installed_held_outputs_do_not_pollute_the_speed_model(self, db):
        """`install_held` records zero seconds -- the encode happened on an
        earlier run and its time is counted there."""
        for _ in range(10):
            record(db, est_cpu=1000, cpu=0)
        report = calibrate.measure(db)
        assert report.speed == []


class TestStorage:
    def test_round_trips(self, db):
        for _ in range(10):
            record(db, after=int(1.4 * GB))
        original = calibrate.measure(db).calibration
        calibrate.save(db, original)

        loaded = calibrate.load(db)
        assert loaded.size_factors == original.size_factors
        assert loaded.samples == original.samples

    def test_nothing_saved_means_no_correction(self, db):
        assert calibrate.load(db) is None

    def test_garbage_in_the_kv_table_is_not_fatal(self, db):
        db.set_kv(calibrate.KV_KEY, "{not json")
        assert calibrate.load(db) is None

    def test_reset_clears_it(self, db):
        for _ in range(10):
            record(db, after=int(1.4 * GB))
        calibrate.save(db, calibrate.measure(db).calibration)
        calibrate.clear(db)
        assert calibrate.load(db) is None


class TestCorrectedEstimator:
    def facts(self):
        return FileFacts(
            file_id=1, path="/media/tv/a.mkv", size_bytes=3 * GB,
            duration_s=2700.0, container="matroska", v_codec="h264",
            v_bit_depth=8, v_width=1920, v_height=1080, v_bitrate=8_000_000,
            v_fps=24.0, hdr_type="sdr", audio=[], content_class="tv",
        )

    def rung(self):
        return Rung(
            encoder="hevc_vaapi", content_class="tv", resolution="1080p",
            target_vmaf=92.0, quality=24.0, quality_flag="qp",
            # Low enough that the ladder's measured output bitrate is the
            # model that wins, which is the one these tests name.
            expected_size_ratio=0.30, expected_fps=30.0,
            expected_out_bitrate=3_000_000, samples=6,
        )

    def test_the_correction_is_applied(self, config):
        raw = estimate_mod.Estimator().encode(self.facts(), self.rung(), None, config)
        calibration = calibrate.Calibration(
            size_factors={"ladder-bitrate": 1.5},
            speed_factors={"hevc_vaapi:1080p": 2.0},
        )
        corrected = estimate_mod.Estimator(calibration).encode(
            self.facts(), self.rung(), None, config
        )

        assert corrected.out_bytes == pytest.approx(raw.out_bytes * 1.5, rel=0.01)
        assert corrected.cpu_seconds == pytest.approx(raw.cpu_seconds * 2.0, rel=0.01)
        assert corrected.saved_bytes < raw.saved_bytes

    def test_a_corrected_estimate_says_so(self, config):
        """So the next calibration measures the corrected model on its own and
        converges towards 1 instead of compounding."""
        calibration = calibrate.Calibration(size_factors={"ladder-bitrate": 1.5})
        corrected = estimate_mod.Estimator(calibration).encode(
            self.facts(), self.rung(), None, config
        )
        assert corrected.basis == "ladder-bitrate+cal"

    def test_an_unmeasured_model_falls_back_to_the_pooled_factor(self, config):
        calibration = calibrate.Calibration(size_factors={}, size_default=1.25)
        corrected = estimate_mod.Estimator(calibration).encode(
            self.facts(), self.rung(), None, config
        )
        raw = estimate_mod.Estimator().encode(self.facts(), self.rung(), None, config)
        assert corrected.out_bytes == pytest.approx(raw.out_bytes * 1.25, rel=0.01)

    def test_speed_falls_back_to_the_encoder_when_the_resolution_is_new(self):
        calibration = calibrate.Calibration(speed_factors={"hevc_vaapi": 1.4})
        assert calibration.speed_factor("hevc_vaapi:2160p") == 1.4

    def test_no_calibration_leaves_the_estimate_untouched(self, config):
        a = estimate_mod.Estimator().encode(self.facts(), self.rung(), None, config)
        b = estimate_mod.Estimator(None).encode(self.facts(), self.rung(), None, config)
        assert a.out_bytes == b.out_bytes
        assert a.basis == b.basis == "ladder-bitrate"


class TestOutcomeSurvival:
    def test_deleting_a_decision_keeps_the_outcome(self, db):
        """The worker deletes the decision the moment a job succeeds and the
        original is replaced. That used to cascade through job into outcome and
        erase the measurement Phase 4 exists to make."""
        db.execute(
            "INSERT INTO media_file (path, library_root, size_bytes, mtime, "
            "first_seen, last_seen) VALUES ('/media/tv/a.mkv','/media/tv',1,1,1,1)"
        )
        db.execute(
            "INSERT INTO decision (file_id, action, reason, created_at) "
            "VALUES (1,'encode','because',1)"
        )
        db.execute("INSERT INTO job (decision_id, state) VALUES (1,'done')")
        db.execute(
            "INSERT INTO outcome (job_id, file_path, action, before_bytes, "
            "after_bytes, saved_bytes, completed_at, est_out_bytes, estimate_basis) "
            "VALUES (1,'/media/tv/a.mkv','encode',10,5,5,1,6,'ladder-bitrate')"
        )

        db.execute("DELETE FROM decision WHERE id=1")

        assert db.scalar("SELECT COUNT(*) FROM outcome") == 1
        assert db.scalar("SELECT COUNT(*) FROM job") == 1
        assert db.scalar("SELECT decision_id FROM job") is None
