import time
from datetime import datetime
from pathlib import Path

import pytest

from app.config import Config
from app.db import Database
from app.scan.probe import AudioTrack, MediaInfo, SubtitleTrack
from app.work import schedule, streams, swap, verify, vmaf, worker

GB = 1024**3
MB = 1024**2


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "work.db")


@pytest.fixture
def config(tmp_path):
    config = Config(config_dir=tmp_path, scratch_dir=tmp_path / "scratch")
    config.safety.min_free_bytes = 0
    return config


def audio(index=1, codec="eac3", channels=6, language="eng", bitrate=640_000,
          forced=False, commentary=False):
    return AudioTrack(
        index=index, codec=codec, profile=None, channels=channels,
        language=language, title=None, bitrate=bitrate, is_default=index == 1,
        is_forced=forced, is_commentary=commentary,
    )


def subtitle(index=5, codec="subrip", language="eng", forced=False):
    return SubtitleTrack(
        index=index, codec=codec, language=language, title=None,
        is_default=False, is_forced=forced, is_sdh=False,
    )


def fake_info(**kwargs) -> MediaInfo:
    defaults = dict(
        path="/media/tv/a.mkv", size_bytes=3 * GB, container="matroska,webm",
        duration_s=2700.0, bitrate=9_000_000, v_codec="h264", v_profile="High",
        v_bit_depth=8, v_width=1920, v_height=1080, v_bitrate=8_000_000,
        v_fps=24.0, pix_fmt="yuv420p", color_transfer="bt709",
        color_primaries="bt709", hdr_type="sdr",
        audio=[audio()], subs=[],
    )
    defaults.update(kwargs)
    return MediaInfo(**defaults)


# ------------------------------------------------------------------- schedule


class TestWindow:
    def test_night_window_wraps_past_midnight(self, config):
        cfg = config.schedule  # 23:00 -> 07:00
        assert schedule.is_night(datetime(2026, 8, 28, 23, 30), cfg)
        assert schedule.is_night(datetime(2026, 8, 28, 2, 0), cfg)
        assert schedule.is_night(datetime(2026, 8, 28, 6, 59), cfg)
        assert not schedule.is_night(datetime(2026, 8, 28, 7, 0), cfg)
        assert not schedule.is_night(datetime(2026, 8, 28, 19, 0), cfg)

    def test_a_same_day_window_does_not_wrap(self, config):
        config.schedule.night_start = "01:00"
        config.schedule.night_end = "05:00"
        assert schedule.is_night(datetime(2026, 8, 28, 3, 0), config.schedule)
        assert not schedule.is_night(datetime(2026, 8, 28, 23, 0), config.schedule)

    def test_daytime_is_throttled_not_stopped(self, config):
        window = schedule.window_for(datetime(2026, 8, 28, 14, 0), config.schedule)
        assert window.working
        assert window.threads == config.schedule.day_threads
        assert window.nice == config.schedule.day_nice

    def test_daytime_can_be_turned_off_entirely(self, config):
        config.schedule.day_enabled = False
        window = schedule.window_for(datetime(2026, 8, 28, 14, 0), config.schedule)
        assert not window.working

    def test_night_runs_at_full_width(self, config):
        window = schedule.window_for(datetime(2026, 8, 28, 1, 0), config.schedule)
        assert window.threads == config.schedule.night_threads
        assert window.nice == 0


class TestStreamingPause:
    def test_an_active_stream_pauses_work(self, config, monkeypatch):
        monkeypatch.setattr(schedule, "tautulli_streams", lambda c: 2)
        paused, reason = schedule.someone_is_watching(config)
        assert paused
        assert "2 active stream" in reason

    def test_nobody_watching_lets_work_continue(self, config, monkeypatch):
        monkeypatch.setattr(schedule, "tautulli_streams", lambda c: 0)
        paused, _ = schedule.someone_is_watching(config)
        assert not paused

    def test_plex_is_the_fallback_when_tautulli_cannot_answer(self, config, monkeypatch):
        monkeypatch.setattr(schedule, "tautulli_streams", lambda c: None)
        monkeypatch.setattr(schedule, "plex_streams", lambda c: 1)
        paused, reason = schedule.someone_is_watching(config)
        assert paused
        assert "Plex" in reason

    def test_being_unable_to_ask_pauses_rather_than_assumes(self, config, monkeypatch):
        """One video engine. Guessing wrong makes someone's film stutter."""
        config.plex.enabled = True
        config.plex.token = "x"
        monkeypatch.setattr(schedule, "tautulli_streams", lambda c: None)
        monkeypatch.setattr(schedule, "plex_streams", lambda c: None)
        paused, reason = schedule.someone_is_watching(config)
        assert paused
        assert "pausing to be safe" in reason

    def test_nothing_configured_does_not_block_work_forever(self, config, monkeypatch):
        monkeypatch.setattr(schedule, "tautulli_streams", lambda c: None)
        monkeypatch.setattr(schedule, "plex_streams", lambda c: None)
        paused, _ = schedule.someone_is_watching(config)
        assert not paused

    def test_pausing_can_be_disabled(self, config, monkeypatch):
        config.schedule.pause_when_streaming = False
        monkeypatch.setattr(schedule, "tautulli_streams", lambda c: 5)
        paused, _ = schedule.someone_is_watching(config)
        assert not paused


# ---------------------------------------------------------------------- swap


class TestInstall:
    def test_nothing_is_installed_while_deletion_is_off(self, tmp_path):
        original = tmp_path / "show.mkv"
        original.write_bytes(b"original")
        output = tmp_path / "scratch" / "show.mkv"
        output.parent.mkdir()
        output.write_bytes(b"new")

        result = swap.install(output, original, delete_original=False)

        assert result.ok
        assert not result.original_deleted
        assert original.read_bytes() == b"original"
        assert output.exists()

    def test_same_extension_is_replaced_in_place(self, tmp_path):
        original = tmp_path / "show.mkv"
        original.write_bytes(b"original bytes")
        output = tmp_path / "show-new.mkv"
        output.write_bytes(b"new")

        result = swap.install(output, original, delete_original=True)

        assert result.ok
        assert result.original_deleted
        assert result.final_path == original
        assert original.read_bytes() == b"new"
        assert not output.exists()

    def test_a_different_extension_removes_the_original(self, tmp_path):
        original = tmp_path / "show.avi"
        original.write_bytes(b"old avi")
        output = tmp_path / "show-new.mkv"
        output.write_bytes(b"new mkv")

        result = swap.install(output, original, delete_original=True)

        assert result.ok
        assert result.final_path == tmp_path / "show.mkv"
        assert result.final_path.read_bytes() == b"new mkv"
        assert not original.exists()

    def test_a_short_copy_never_replaces_the_original(self, tmp_path, monkeypatch):
        original = tmp_path / "show.mkv"
        original.write_bytes(b"the original, still needed")
        output = tmp_path / "out.mkv"
        output.write_bytes(b"new content")

        # Simulate the disk filling mid-copy.
        real_copy = swap.shutil.copyfileobj

        def truncated(src, dst, length=0):
            dst.write(src.read(3))

        monkeypatch.setattr(swap.shutil, "copyfileobj", truncated)
        result = swap.install(output, original, delete_original=True)
        monkeypatch.setattr(swap.shutil, "copyfileobj", real_copy)

        assert not result.ok
        assert "short file" in (result.error or "")
        assert original.read_bytes() == b"the original, still needed"
        assert not (tmp_path / ("show.mkv" + swap.TEMP_SUFFIX)).exists()

    def test_no_room_beside_the_original_is_refused(self, tmp_path, monkeypatch):
        original = tmp_path / "show.mkv"
        original.write_bytes(b"original")
        output = tmp_path / "out.mkv"
        output.write_bytes(b"new")
        monkeypatch.setattr(swap, "free_bytes", lambda p: 0)

        result = swap.install(output, original, delete_original=True)

        assert not result.ok
        assert original.exists()

    def test_dry_run_touches_nothing(self, tmp_path):
        original = tmp_path / "show.avi"
        original.write_bytes(b"original")
        output = tmp_path / "out.mkv"
        output.write_bytes(b"new")

        result = swap.install(output, original, delete_original=True, dry_run=True)

        assert result.ok
        assert not result.original_deleted
        assert original.exists()
        assert not (tmp_path / "show.mkv").exists()


class TestQuarantine:
    def test_a_failed_output_is_kept_with_its_reason(self, tmp_path):
        output = tmp_path / "bad.mkv"
        output.write_bytes(b"x")
        held = swap.quarantine(output, tmp_path / "scratch", "VMAF 61.2 below floor")

        assert held is not None
        assert held.exists()
        assert not output.exists()
        assert "VMAF 61.2" in held.with_suffix(held.suffix + ".txt").read_text()

    def test_a_second_failure_does_not_overwrite_the_first(self, tmp_path):
        for _ in range(2):
            output = tmp_path / "bad.mkv"
            output.write_bytes(b"x")
            swap.quarantine(output, tmp_path / "scratch", "reason")

        held = list((tmp_path / "scratch" / "quarantine").glob("bad*.mkv"))
        assert len(held) == 2


# -------------------------------------------------------------------- verify


class TestStructuralChecks:
    def _output(self, tmp_path, size=1 * GB):
        path = tmp_path / "out.mkv"
        path.write_bytes(b"x")
        return path

    def test_a_missing_output_fails(self, tmp_path):
        result = verify.check_structure(
            fake_info(), tmp_path / "nope.mkv", ffprobe="ffprobe"
        )
        assert not result.ok

    def test_an_empty_output_fails(self, tmp_path):
        path = tmp_path / "out.mkv"
        path.write_bytes(b"")
        result = verify.check_structure(fake_info(), path, ffprobe="ffprobe")
        assert not result.ok
        assert "empty" in result.failures[0]

    def test_a_truncated_encode_is_caught_by_duration(self, tmp_path, monkeypatch):
        out = self._output(tmp_path)
        monkeypatch.setattr(
            verify, "probe",
            lambda p, f: fake_info(v_codec="hevc", duration_s=800.0, size_bytes=1 * GB),
        )
        result = verify.check_structure(fake_info(), out, ffprobe="ffprobe")
        assert not result.ok
        assert "truncated" in result.summary

    def test_losing_the_audio_is_caught(self, tmp_path, monkeypatch):
        out = self._output(tmp_path)
        monkeypatch.setattr(
            verify, "probe",
            lambda p, f: fake_info(v_codec="hevc", size_bytes=1 * GB, audio=[]),
        )
        result = verify.check_structure(fake_info(), out, ffprobe="ffprobe")
        assert not result.ok
        assert "no audio" in result.summary

    def test_an_output_that_grew_is_rejected(self, tmp_path, monkeypatch):
        out = self._output(tmp_path)
        monkeypatch.setattr(
            verify, "probe",
            lambda p, f: fake_info(v_codec="hevc", size_bytes=4 * GB),
        )
        result = verify.check_structure(fake_info(), out, ffprobe="ffprobe")
        assert not result.ok
        assert "did not earn its time" in result.summary

    def test_the_wrong_codec_is_rejected(self, tmp_path, monkeypatch):
        out = self._output(tmp_path)
        monkeypatch.setattr(
            verify, "probe", lambda p, f: fake_info(size_bytes=1 * GB)
        )
        result = verify.check_structure(fake_info(), out, ffprobe="ffprobe")
        assert not result.ok
        assert "expected hevc" in result.summary

    def test_a_good_output_passes(self, tmp_path, monkeypatch):
        out = self._output(tmp_path)
        monkeypatch.setattr(
            verify, "probe",
            lambda p, f: fake_info(v_codec="hevc", size_bytes=1 * GB),
        )
        result = verify.check_structure(fake_info(), out, ffprobe="ffprobe")
        assert result.ok, result.summary

    def test_a_downscale_must_land_on_the_target_height(self, tmp_path, monkeypatch):
        out = self._output(tmp_path)
        monkeypatch.setattr(
            verify, "probe",
            lambda p, f: fake_info(v_codec="hevc", size_bytes=1 * GB, v_height=1080),
        )
        source = fake_info(v_width=3840, v_height=2160, size_bytes=20 * GB)
        assert verify.check_structure(
            source, out, ffprobe="ffprobe", expect_height=1080
        ).ok
        assert not verify.check_structure(
            source, out, ffprobe="ffprobe", expect_height=720
        ).ok


class TestQualityGate:
    def _score(self, value):
        return lambda *a, **k: vmaf.VmafScore(mean=value, min=value - 2, p1=value - 4)

    def test_every_sample_must_clear_the_bar(self, tmp_path, config, monkeypatch):
        scores = iter([96.0, 88.0, 97.0])
        monkeypatch.setattr(
            vmaf, "score",
            lambda *a, **k: vmaf.VmafScore(mean=next(scores), min=80.0, p1=70.0),
        )
        result = verify.check_quality(
            tmp_path / "src.mkv", tmp_path / "out.mkv", source=fake_info(),
            config=config, target_vmaf=95.0, work_dir=tmp_path,
        )
        assert not result.ok
        assert "below the" in result.summary

    def test_a_pass_records_the_samples(self, tmp_path, config, monkeypatch):
        monkeypatch.setattr(vmaf, "score", self._score(96.0))
        result = verify.check_quality(
            tmp_path / "src.mkv", tmp_path / "out.mkv", source=fake_info(),
            config=config, target_vmaf=95.0, work_dir=tmp_path,
        )
        assert result.ok
        assert len(result.samples) == config.quality.vmaf_sample_count

    def test_an_unmeasurable_score_is_never_a_pass(self, tmp_path, config, monkeypatch):
        """No libvmaf means no verification, and no verification means no delete."""
        monkeypatch.setattr(
            vmaf, "score", lambda *a, **k: vmaf.VmafScore(error="no libvmaf here")
        )
        result = verify.check_quality(
            tmp_path / "src.mkv", tmp_path / "out.mkv", source=fake_info(),
            config=config, target_vmaf=95.0, work_dir=tmp_path,
        )
        assert not result.ok
        assert "could not be measured" in result.summary

    def test_the_margin_allows_a_small_shortfall(self, tmp_path, config, monkeypatch):
        config.quality.vmaf_fail_margin = 3.0
        monkeypatch.setattr(vmaf, "score", self._score(92.5))
        result = verify.check_quality(
            tmp_path / "src.mkv", tmp_path / "out.mkv", source=fake_info(),
            config=config, target_vmaf=95.0, work_dir=tmp_path,
        )
        assert result.ok


class TestSampleOffsets:
    def test_samples_avoid_the_credits_at_both_ends(self):
        offsets = vmaf.sample_offsets(3600.0, 3, 20.0)
        assert len(offsets) == 3
        assert all(360.0 <= o <= 3240.0 for o in offsets)

    def test_a_very_short_file_still_gets_one_sample(self):
        assert vmaf.sample_offsets(10.0, 3, 20.0) == [0.0]

    def test_no_duration_means_no_samples(self):
        assert vmaf.sample_offsets(0.0, 3, 20.0) == []


# ------------------------------------------------------------------- streams


class TestStreamPlan:
    def test_keeps_the_best_english_track_and_maps_by_absolute_index(self, config):
        info = fake_info(audio=[
            audio(index=1, channels=2, language="eng"),
            audio(index=3, channels=6, language="eng"),
            audio(index=4, channels=6, language="fra"),
        ])
        plan = streams.build_stream_plan(info, config)

        assert plan.kept_audio == [3]
        assert sorted(plan.dropped_audio) == [1, 4]
        assert "-map" in plan.args and "0:3" in plan.args
        assert "0:4" not in plan.args

    def test_passthrough_audio_is_copied_not_re_encoded(self, config):
        plan = streams.build_stream_plan(fake_info(), config)
        assert "-c:a" in plan.args
        assert plan.args[plan.args.index("-c:a") + 1] == "copy"
        assert not plan.audio_transcoded

    def test_lossless_audio_is_transcoded_without_upmixing(self, config):
        info = fake_info(audio=[
            audio(index=1, codec="truehd", channels=2, bitrate=None)
        ])
        plan = streams.build_stream_plan(info, config)

        assert plan.audio_transcoded
        assert plan.args[plan.args.index("-c:a") + 1] == config.audio.target_codec
        # Stereo source stays stereo even though the target is 5.1.
        assert plan.args[plan.args.index("-ac") + 1] == "2"

    def test_a_forced_subtitle_survives_whatever_its_language(self, config):
        """Forced subs carry the foreign dialogue inside an English film."""
        info = fake_info(subs=[
            subtitle(index=5, language="eng"),
            subtitle(index=6, language="spa", forced=True),
            subtitle(index=7, language="deu"),
        ])
        plan = streams.build_stream_plan(info, config)

        assert sorted(plan.kept_subs) == [5, 6]
        assert plan.dropped_subs == [7]

    def test_the_kept_track_is_made_default(self, config):
        plan = streams.build_stream_plan(fake_info(), config)
        assert "-disposition:a:0" in plan.args

    def test_a_japanese_only_file_keeps_its_audio(self, config):
        info = fake_info(audio=[audio(index=1, language="jpn", codec="aac")])
        plan = streams.build_stream_plan(info, config)
        assert plan.kept_audio == [1]
        assert plan.dropped_audio == []


# -------------------------------------------------------------------- worker


def seed_job(db, tmp_path, *, size=4096, hdr="sdr", action="encode"):
    """A real file on disk plus a matching pending decision."""
    source = tmp_path / "media" / "show.mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"0" * size)
    stat = source.stat()

    now = time.time()
    db.execute(
        "INSERT INTO media_file (path, library_root, size_bytes, mtime, probe_version, "
        "probed_at, container, duration_s, v_codec, v_bit_depth, v_width, v_height, "
        "v_bitrate, v_fps, hdr_type, audio_json, first_seen, last_seen, missing) "
        "VALUES (?,?,?,?,1,?,'matroska,webm',2700.0,'h264',8,1920,1080,8000000,24.0,?,"
        "'[]',?,?,0)",
        (str(source), str(source.parent), stat.st_size, stat.st_mtime, now, hdr,
         now, now),
    )
    file_id = db.scalar("SELECT last_insert_rowid()")
    db.execute(
        "INSERT INTO decision (file_id, action, profile, reason, est_out_bytes, "
        "est_saved_bytes, est_cpu_seconds, priority, state, detail_json, "
        "estimate_basis, encoder, created_at) "
        "VALUES (?,?,'hevc_vaapi qp24','because',1000,3000,600,5.0,'pending',?,"
        "'ladder-bitrate','hevc_vaapi',?)",
        (file_id, action, '{"encoder": "hevc_vaapi", "quality": 24}', now),
    )
    return source, file_id, db.scalar("SELECT last_insert_rowid()")


class TestPreflight:
    def test_a_healthy_file_passes(self, db, config, tmp_path, monkeypatch):
        seed_job(db, tmp_path)
        monkeypatch.setattr(worker, "probe", lambda p, f: fake_info())
        row = worker.next_decision(db)
        assert worker.preflight(db, config, row).ok

    def test_protection_is_re_checked_against_the_fresh_probe(
        self, db, config, tmp_path, monkeypatch
    ):
        """The database says SDR; the file on disk is HDR. Disk wins.

        This is the case where an *arr upgrade replaced the release between
        planning and encoding. Trusting the plan here would flatten an HDR
        grade to 8-bit SDR and then delete the original.
        """
        seed_job(db, tmp_path, hdr="sdr")
        monkeypatch.setattr(worker, "probe", lambda p, f: fake_info(hdr_type="hdr10"))

        result = worker.preflight(db, config, worker.next_decision(db))

        assert not result.ok
        assert "protected" in result.error
        assert result.permanent

    def test_a_file_that_changed_on_disk_is_not_encoded(
        self, db, config, tmp_path, monkeypatch
    ):
        source, _, _ = seed_job(db, tmp_path)
        source.write_bytes(b"1" * 9999)  # different size than the row records
        monkeypatch.setattr(worker, "probe", lambda p, f: fake_info())

        result = worker.preflight(db, config, worker.next_decision(db))

        assert not result.ok
        assert "changed on disk" in result.error

    def test_a_missing_file_is_not_encoded(self, db, config, tmp_path, monkeypatch):
        source, _, _ = seed_job(db, tmp_path)
        source.unlink()
        result = worker.preflight(db, config, worker.next_decision(db))
        assert not result.ok
        assert "no longer exists" in result.error

    def test_the_free_space_floor_stops_work(self, db, config, tmp_path, monkeypatch):
        seed_job(db, tmp_path)
        config.safety.min_free_bytes = 500 * GB
        monkeypatch.setattr(worker, "probe", lambda p, f: fake_info())
        monkeypatch.setattr(swap, "free_bytes", lambda p: 10 * GB)

        result = worker.preflight(db, config, worker.next_decision(db))

        assert not result.ok
        assert "below the" in result.error
        # Not permanent: deleting something fixes it, and re-planning should not
        # throw the decision away.
        assert not result.permanent


class TestFailureStates:
    def test_a_stale_plan_returns_to_the_regenerable_pool(self, db, tmp_path):
        seed_job(db, tmp_path)
        row = worker.next_decision(db)
        worker._fail(db, row, "file changed on disk", permanent=True)
        assert db.scalar("SELECT state FROM decision") == "skipped"

    def test_a_real_failure_sticks(self, db, tmp_path):
        seed_job(db, tmp_path)
        row = worker.next_decision(db)
        worker._fail(db, row, "encode failed (1): boom")
        assert db.scalar("SELECT state FROM decision") == "failed"

    def test_failed_decisions_can_be_put_back_deliberately(self, db, tmp_path):
        seed_job(db, tmp_path)
        worker._fail(db, worker.next_decision(db), "encode failed")

        assert worker.retry_failed(db) == 1
        assert db.scalar("SELECT state FROM decision") == "pending"


class TestRecovery:
    def test_a_killed_run_leaves_nothing_stuck(self, db, tmp_path):
        seed_job(db, tmp_path)
        decision_id = db.scalar("SELECT id FROM decision")
        db.execute("UPDATE decision SET state='running' WHERE id=?", (decision_id,))
        db.execute(
            "INSERT INTO job (decision_id, state, attempts, started_at) "
            "VALUES (?, 'running', 1, ?)",
            (decision_id, time.time()),
        )

        assert worker.reclaim_stale(db) == 1
        assert db.scalar("SELECT state FROM decision") == "pending"
        assert db.scalar("SELECT state FROM job") == "interrupted"


class TestDryRun:
    def test_a_dry_run_reports_a_problem_without_recording_it(
        self, db, config, tmp_path, monkeypatch
    ):
        source, _, _ = seed_job(db, tmp_path)
        source.unlink()

        stats = worker.run(db, config, ignore_schedule=True, progress=None)

        assert stats.failed == 1
        # Still pending: a dry run reports, it does not decide.
        assert db.scalar("SELECT state FROM decision") == "pending"

    def test_a_dry_run_changes_nothing_and_terminates(
        self, db, config, tmp_path, monkeypatch
    ):
        seed_job(db, tmp_path)
        monkeypatch.setattr(worker, "probe", lambda p, f: fake_info())
        assert config.safety.dry_run is True

        stats = worker.run(db, config, ignore_schedule=True, progress=None)

        assert stats.attempted == 1
        assert stats.succeeded == 1
        assert db.scalar("SELECT state FROM decision") == "pending"
        assert db.scalar("SELECT COUNT(*) FROM job") == 0
        assert db.scalar("SELECT COUNT(*) FROM outcome") == 0

    def test_the_command_it_would_run_is_a_real_encode_command(
        self, db, config, tmp_path, monkeypatch
    ):
        seed_job(db, tmp_path)
        monkeypatch.setattr(worker, "probe", lambda p, f: fake_info())
        row = worker.next_decision(db)
        cmd, plan, spec = worker.build_command(
            row, fake_info(), config, dest=tmp_path / "out.mkv", threads=4
        )
        assert "hevc_vaapi" in cmd
        assert "-qp" in cmd
        assert str(tmp_path / "out.mkv") == cmd[-1]

    def test_a_remux_copies_the_video_stream(self, db, config, tmp_path, monkeypatch):
        seed_job(db, tmp_path, action="remux")
        monkeypatch.setattr(worker, "probe", lambda p, f: fake_info())
        row = worker.next_decision(db)
        cmd, _, spec = worker.build_command(
            row, fake_info(), config, dest=tmp_path / "out.mkv", threads=4
        )
        assert spec is None
        assert cmd[cmd.index("-c:v") + 1] == "copy"

    def test_progress_reporting_is_asked_for_machine_readably(self):
        cmd = worker.with_progress(["ffmpeg", "-i", "a.mkv", "out.mkv"])
        assert cmd[:4] == ["ffmpeg", "-progress", "pipe:1", "-nostats"]


class TestTheRecordItLeaves:
    """A finished job has to leave behind enough to calibrate the estimator.

    The worker deletes the decision the moment it replaces an original, and
    `outcome` is the only permanent record of what that job cost and returned.
    """

    def succeed(self, db, config, tmp_path, monkeypatch, *, delete=True):
        source, _, _ = seed_job(db, tmp_path)
        config.safety.dry_run = False
        config.safety.delete_original_on_success = delete

        out_info = fake_info(size_bytes=1500, path=str(source))
        monkeypatch.setattr(worker, "probe", lambda p, f: fake_info(path=str(source)))
        monkeypatch.setattr(
            worker, "_run_with_progress",
            lambda cmd, timeout, nice=0, on_progress=None: (0, "", 1234.0),
        )
        monkeypatch.setattr(
            verify, "check_structure",
            lambda *a, **k: verify.Verification(ok=True, out_info=out_info),
        )
        monkeypatch.setattr(
            verify, "check_quality",
            lambda *a, **k: verify.Verification(
                ok=True, vmaf_mean=94.5, vmaf_min=93.1, out_info=out_info
            ),
        )
        monkeypatch.setattr(
            swap, "install",
            lambda dest, src, delete_original: swap.InstallResult(
                ok=True, final_path=src, original_deleted=delete_original
            ),
        )
        monkeypatch.setattr(worker, "_adopt_output", lambda *a, **k: None)

        row = worker.next_decision(db)
        window = schedule.WorkWindow(True, 4, 0, "test", is_night=True)
        return worker.run_decision(
            db, config, row, window=window, dry_run=False, progress=None
        )

    def test_the_outcome_carries_what_calibration_needs(
        self, db, config, tmp_path, monkeypatch
    ):
        result = self.succeed(db, config, tmp_path, monkeypatch)
        assert result.ok, result.error

        row = db.one("SELECT * FROM outcome")
        assert row["est_out_bytes"] == 1000       # what the plan predicted
        assert row["after_bytes"] == 1500         # what it actually produced
        assert row["est_cpu_seconds"] == 600      # predicted
        assert row["cpu_seconds"] == 1234.0       # measured
        assert row["encoder"] == "hevc_vaapi"
        assert row["resolution"] == "1080p"
        assert row["content_class"] == "tv"

    def test_it_survives_the_decision_being_deleted(
        self, db, config, tmp_path, monkeypatch
    ):
        self.succeed(db, config, tmp_path, monkeypatch, delete=True)

        assert db.scalar("SELECT COUNT(*) FROM decision") == 0
        assert db.scalar("SELECT COUNT(*) FROM outcome") == 1
        assert db.scalar("SELECT COUNT(*) FROM job") == 1

    def test_calibration_can_read_it_back(self, db, config, tmp_path, monkeypatch):
        """End to end: a real job through the worker, measured by the module
        that corrects the estimator."""
        from app.plan import calibrate

        self.succeed(db, config, tmp_path, monkeypatch)
        report = calibrate.measure(db, min_samples=1)

        assert report.used == 1
        assert report.calibration.size_factor("ladder-bitrate") == pytest.approx(1.5)
        assert report.calibration.speed_factor("hevc_vaapi:1080p") == pytest.approx(
            1234.0 / 600
        )

    def test_installing_a_held_output_does_not_write_a_second_outcome(
        self, db, config, tmp_path, monkeypatch
    ):
        """One job, one row.

        Found by running the pipeline against real ffmpeg: three files produced
        six outcomes, because the encode records one and the later install
        recorded another. That doubles the reclaimed total on /activity and
        gives the estimator two votes for one file -- and the duplicate carries
        no encode time and no VMAF, because neither can be measured once the
        source has already been replaced.
        """
        self.succeed(db, config, tmp_path, monkeypatch, delete=False)
        assert db.scalar("SELECT COUNT(*) FROM outcome") == 1
        assert db.scalar("SELECT state FROM decision") == "held"

        config.safety.delete_original_on_success = True
        monkeypatch.setattr(
            verify, "check_structure",
            lambda *a, **k: verify.Verification(ok=True, out_info=fake_info(size_bytes=1500)),
        )
        scratch = db.scalar("SELECT scratch_path FROM job")
        Path(scratch).parent.mkdir(parents=True, exist_ok=True)
        Path(scratch).write_bytes(b"0" * 1500)

        stats = worker.install_held(db, config, progress=None)

        assert stats.succeeded == 1
        assert db.scalar("SELECT COUNT(*) FROM outcome") == 1
        row = db.one("SELECT * FROM outcome")
        assert row["original_deleted"] == 1
        # The encode's own measurements survive the install untouched.
        assert row["cpu_seconds"] == 1234.0
        assert row["vmaf_mean"] == 94.5

    def test_the_arr_is_told_only_once_the_original_is_really_gone(
        self, db, config, tmp_path, monkeypatch
    ):
        """While the output is held in scratch, the file in the library is
        still the one the *arr already knows about. Telling it to re-read an
        unchanged file would be noise at best, and at worst would rename
        something we have not touched."""
        from app.guard import arr_notify

        told = []
        monkeypatch.setattr(
            arr_notify, "notify_replaced",
            lambda cfg, path, progress=None: told.append(str(path)),
        )

        self.succeed(db, config, tmp_path, monkeypatch, delete=False)
        assert told == []

    def test_the_arr_is_told_when_the_original_is_replaced(
        self, db, config, tmp_path, monkeypatch
    ):
        from app.guard import arr_notify

        told = []
        monkeypatch.setattr(
            arr_notify, "notify_replaced",
            lambda cfg, path, progress=None: told.append(str(path)),
        )

        self.succeed(db, config, tmp_path, monkeypatch, delete=True)
        assert len(told) == 1

    def test_a_downscale_is_recorded_at_the_resolution_it_ran_at(
        self, db, config, tmp_path, monkeypatch
    ):
        """A 4K source downscaled to 1080p is a 1080p encode, and belongs with
        the other 1080p encodes when speed is calibrated."""
        seed_job(db, tmp_path)
        db.execute(
            "UPDATE decision SET detail_json=?",
            ('{"encoder": "hevc_vaapi", "quality": 24, "target_height": 1080}',),
        )
        db.execute("UPDATE media_file SET v_width=3840, v_height=2160")
        row = db.one("SELECT d.*, mf.library_root, NULL AS title_kind FROM decision d "
                     "JOIN media_file mf ON mf.id = d.file_id")
        info = fake_info(v_width=3840, v_height=2160)

        assert worker._outcome_shape(row, info, worker._detail(row)) == ("tv", "1080p")


class TestSafetyCeilings:
    def test_the_worker_stops_when_the_schedule_says_so(
        self, db, config, tmp_path, monkeypatch
    ):
        seed_job(db, tmp_path)
        monkeypatch.setattr(
            schedule, "may_work_now",
            lambda c: schedule.WorkWindow(False, 0, 0, "someone is watching"),
        )
        stats = worker.run(db, config, progress=None)

        assert stats.attempted == 0
        assert "watching" in stats.stopped_because

    def test_install_held_refuses_while_deletion_is_off(self, db, config):
        stats = worker.install_held(db, config, progress=None)
        assert stats.attempted == 0
        assert "delete_original_on_success is off" in stats.stopped_because
