import json
import time

import pytest

from app.config import Config
from app.db import Database
from app.plan import estimate as estimate_mod
from app.plan import planner
from app.plan.profiles import Ladder, Rung, load_ladder, provisional_ladder
from app.plan.rules import DOWNSCALE, ENCODE, REMUX, SKIP, FileFacts, decide

GB = 1024**3
MB = 1024**2
EPISODE = 2700.0  # 45 minutes


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "plan.db")


@pytest.fixture
def config(tmp_path):
    return Config(config_dir=tmp_path)


# Roughly what a real ladder looks like: output bitrate falls with resolution.
LADDER_BITRATE = {"2160p": 3_000_000, "1080p": 3_000_000,
                  "720p": 1_500_000, "sd": 800_000}


def rung(resolution="1080p", content_class="tv", **kwargs):
    defaults = dict(
        encoder="hevc_vaapi",
        content_class=content_class,
        resolution=resolution,
        target_vmaf=92.0,
        quality=24.0,
        quality_flag="qp",
        expected_size_ratio=0.45,
        expected_fps=30.0,
        expected_out_bitrate=LADDER_BITRATE.get(resolution, 3_000_000),
        samples=6,
    )
    defaults.update(kwargs)
    return Rung(**defaults)


def ladder_for_tests():
    """A plausible full ladder. Shared with the web tests."""
    return Ladder(
        [
            rung(resolution=res, content_class=cls)
            for res in ("2160p", "1080p", "720p", "sd")
            for cls in ("movie", "tv")
        ],
        preferred_encoder="hevc_vaapi",
    )


@pytest.fixture
def ladder():
    return ladder_for_tests()


def facts(**kwargs):
    defaults = dict(
        file_id=1,
        path="/media/tv/Show/S01E01.mkv",
        size_bytes=3 * GB,
        duration_s=EPISODE,
        container="matroska,webm",
        v_codec="h264",
        v_bit_depth=8,
        v_width=1920,
        v_height=1080,
        v_bitrate=8_000_000,
        v_fps=24.0,
        hdr_type="sdr",
        audio=[{"codec": "eac3", "channels": 6, "language": "eng", "bitrate": 640_000}],
    )
    defaults.update(kwargs)
    return FileFacts(**defaults)


def plan_one(f, config, ladder):
    return decide(f, config, ladder, estimate_mod.DEFAULT)


# --------------------------------------------------------------- safety first


class TestProtectedContent:
    """The one mistake that cannot be undone: 8-bit hardware over an HDR grade."""

    @pytest.mark.parametrize(
        "hdr_type", ["hdr10", "hdr10plus", "hlg", "dolbyvision", "unknown-10bit", "unknown"]
    )
    def test_never_planned_for_any_action(self, hdr_type, config, ladder):
        decision = plan_one(facts(hdr_type=hdr_type), config, ladder)
        assert decision.action == SKIP
        assert "protected" in decision.reason

    def test_protected_is_not_even_remuxed_for_a_big_audio_win(self, config, ladder):
        """A stream copy looks safe, but a Dolby Vision RPU can be lost in one.

        Protected content is never rewritten at all -- one invariant with no
        exceptions is easier to keep true than one with a carve-out.
        """
        heavy = facts(
            hdr_type="dolbyvision",
            size_bytes=40 * GB,
            duration_s=7200.0,
            audio=[
                {"codec": "truehd", "channels": 8, "language": "eng"},
                {"codec": "dts", "channels": 6, "language": "fra"},
                {"codec": "dts", "channels": 6, "language": "deu"},
            ],
        )
        assert plan_one(heavy, config, ladder).action == SKIP

    def test_a_protected_file_is_skipped_before_anything_else_is_considered(
        self, config, ladder
    ):
        # Also in an open duplicate group, also tiny: the protection reason is
        # the one that must be reported, because it is the one that never lifts.
        f = facts(hdr_type="hdr10", size_bytes=10 * MB, in_open_duplicate_group=True)
        assert "protected" in plan_one(f, config, ladder).reason


# --------------------------------------------------------------- what to do


class TestActionChoice:
    def test_fat_h264_episode_is_encoded(self, config, ladder):
        decision = plan_one(facts(), config, ladder)
        assert decision.action == ENCODE
        assert decision.est_saved_bytes > 0
        assert decision.detail["encoder"] == "hevc_vaapi"

    def test_sdr_4k_is_downscaled_to_1080p(self, config, ladder):
        f = facts(v_width=3840, v_height=2160, v_bitrate=25_000_000, size_bytes=30 * GB,
                  duration_s=7200.0)
        decision = plan_one(f, config, ladder)
        assert decision.action == DOWNSCALE
        assert decision.detail["target_height"] == 1080

    def test_hdr_4k_is_not_downscaled(self, config, ladder):
        f = facts(v_width=3840, v_height=2160, hdr_type="hdr10", size_bytes=30 * GB)
        assert plan_one(f, config, ladder).action == SKIP

    def test_hevc_source_is_not_re_encoded(self, config, ladder):
        decision = plan_one(facts(v_codec="hevc"), config, ladder)
        assert decision.action != ENCODE

    def test_hevc_source_with_foreign_audio_is_still_worth_a_remux(self, config, ladder):
        f = facts(
            v_codec="hevc",
            size_bytes=8 * GB,
            duration_s=7200.0,
            audio=[
                {"codec": "eac3", "channels": 6, "language": "eng", "bitrate": 640_000},
                {"codec": "dts", "channels": 6, "language": "fra", "bitrate": 1_500_000},
                {"codec": "dts", "channels": 6, "language": "deu", "bitrate": 1_500_000},
            ],
        )
        decision = plan_one(f, config, ladder)
        assert decision.action == REMUX
        assert decision.est_saved_bytes >= config.policy.audio_remux_min_saving_bytes
        # Minutes of disk I/O, not hours of encoding.
        assert decision.est_cpu_seconds < 300

    def test_the_remux_reason_names_the_work_that_frees_the_space(
        self, config, ladder
    ):
        """Dropping a track is only one of the ways a remux gets smaller.

        A file with a single fat audio stream has nothing to drop -- the saving
        comes from re-encoding that stream down to the target bitrate. Saying
        "dropping 0 audio track(s) frees 0.3 GB" described it as a bug for two
        sessions running.
        """
        f = facts(
            v_codec="hevc",
            size_bytes=11 * GB,
            duration_s=7200.0,
            audio=[
                {"codec": "eac3", "channels": 8, "language": "eng",
                 "bitrate": 1_500_000},
            ],
        )
        decision = plan_one(f, config, ladder)
        assert decision.action == REMUX
        assert decision.detail["dropped_tracks"] == 0
        assert "re-encoding the audio frees" in decision.reason
        assert "dropping 0 audio track(s)" not in decision.reason

    def test_the_remux_reason_still_counts_dropped_tracks(self, config, ladder):
        f = facts(
            v_codec="hevc",
            size_bytes=8 * GB,
            duration_s=7200.0,
            audio=[
                {"codec": "eac3", "channels": 6, "language": "eng", "bitrate": 640_000},
                {"codec": "dts", "channels": 6, "language": "fra",
                 "bitrate": 1_500_000},
                {"codec": "dts", "channels": 6, "language": "deu",
                 "bitrate": 1_500_000},
            ],
        )
        decision = plan_one(f, config, ladder)
        assert decision.action == REMUX
        assert "dropping 2 audio track(s) frees" in decision.reason

    def test_already_lean_source_is_left_alone(self, config, ladder):
        decision = plan_one(facts(v_bitrate=2_000_000, size_bytes=1 * GB), config, ladder)
        assert decision.action == SKIP
        assert "below the" in decision.reason

    def test_small_files_are_not_worth_the_per_file_cost(self, config, ladder):
        f = facts(size_bytes=400 * MB, v_width=720, v_height=576, v_bitrate=1_200_000)
        decision = plan_one(f, config, ladder)
        assert decision.action == SKIP
        assert "minimum" in decision.reason

    def test_min_source_bytes_can_be_disabled(self, config, ladder):
        # A 22-minute 720p episode: below the default floor, but fat enough to
        # be worth encoding once the floor is lifted.
        f = facts(size_bytes=600 * MB, v_width=1280, v_height=720,
                  v_bitrate=3_000_000, duration_s=1350.0)
        assert plan_one(f, config, ladder).action == SKIP

        config.policy.min_source_bytes = 0
        assert plan_one(f, config, ladder).action == ENCODE

    def test_a_saving_below_the_floor_is_not_worth_the_hours(self, config, ladder):
        config.quality.savings_floor_pct = 90.0
        assert plan_one(facts(), config, ladder).action == SKIP

    def test_no_rung_for_the_resolution_is_reported_not_guessed(self, config):
        empty = Ladder([rung(resolution="720p", content_class="tv")],
                       preferred_encoder="hevc_vaapi")
        decision = plan_one(facts(), config, empty)
        assert decision.action == SKIP
        assert "no calibrated setting" in decision.reason

    def test_a_resolution_the_benchmark_called_unusable_is_honoured(self, config):
        unusable = Ladder(
            [
                rung(resolution="1080p", content_class="unusable",
                     note="NO USABLE SETTING: the smallest output was 130% of source"),
                rung(resolution="1080p", content_class="tv"),
            ],
            preferred_encoder="hevc_vaapi",
        )
        decision = plan_one(facts(), config, unusable)
        assert decision.action == SKIP
        assert "NO USABLE SETTING" in decision.reason


class TestDuplicateInteraction:
    def test_a_file_in_an_open_group_is_not_encoded(self, config, ladder):
        decision = plan_one(facts(in_open_duplicate_group=True), config, ladder)
        assert decision.action == SKIP
        assert "duplicate" in decision.reason


# --------------------------------------------------------------- estimates


class TestAudioPolicy:
    def test_keeps_the_best_english_track_and_drops_the_rest(self, config):
        f = facts(audio=[
            {"codec": "eac3", "channels": 2, "language": "eng"},
            {"codec": "eac3", "channels": 6, "language": "eng"},
            {"codec": "eac3", "channels": 6, "language": "fra"},
        ])
        selection = estimate_mod.select_audio(f, config.audio)
        assert len(selection["keep"]) == 1
        assert selection["keep"][0]["channels"] == 6
        assert selection["keep"][0]["language"] == "eng"
        assert len(selection["drop"]) == 2

    def test_commentary_never_wins_over_a_feature_track(self, config):
        f = facts(audio=[
            {"codec": "eac3", "channels": 6, "language": "eng", "is_commentary": True},
            {"codec": "eac3", "channels": 6, "language": "eng"},
        ])
        selection = estimate_mod.select_audio(f, config.audio)
        assert not selection["keep"][0].get("is_commentary")

    def test_a_japanese_only_file_keeps_its_audio(self, config):
        """Half the Anime library has no English track at all.

        Stripping to the keep-languages list would leave those files silent,
        which is a far worse outcome than saving nothing.
        """
        f = facts(audio=[{"codec": "aac", "channels": 2, "language": "jpn"}])
        selection = estimate_mod.select_audio(f, config.audio)
        assert len(selection["keep"]) == 1
        assert not selection["drop"]

    def test_lossless_audio_is_transcoded_and_counted_as_a_saving(self, config):
        f = facts(audio=[{"codec": "truehd", "channels": 8, "language": "eng"}])
        kept, dropped, detail = estimate_mod.audio_bytes_after(f, config.audio)
        assert detail["audio_transcode"] is True
        assert dropped > 0
        assert kept < estimate_mod.track_bytes(f.audio[0], f.duration_s)


class TestEstimates:
    def test_output_is_never_predicted_above_the_source_bitrate(self, config, ladder):
        lean = facts(v_bitrate=2_600_000)  # just over the 1080p floor
        estimate = estimate_mod.DEFAULT.encode(lean, rung(), None, config)
        assert estimate.detail["out_bitrate"] <= lean.v_bitrate

    def test_the_more_conservative_of_the_two_models_wins(self, config):
        """Ratio and bitrate models disagree; we take the smaller saving."""
        fat = facts(v_bitrate=20_000_000)
        estimate = estimate_mod.DEFAULT.encode(fat, rung(), None, config)
        # ratio model: 20 Mbps * 0.45 = 9 Mbps, well above the 3 Mbps ladder figure
        assert estimate.detail["out_bitrate"] == pytest.approx(9_000_000, rel=0.01)
        assert estimate.basis == "ladder-ratio"

    def test_encode_time_scales_with_frames_not_bytes(self, config):
        short = facts(duration_s=1350.0)
        long = facts(duration_s=2700.0)
        a = estimate_mod.DEFAULT.encode(short, rung(), None, config)
        b = estimate_mod.DEFAULT.encode(long, rung(), None, config)
        assert b.cpu_seconds == pytest.approx(2 * a.cpu_seconds, rel=0.01)

    def test_the_aac_guess_scales_with_channels(self):
        """The miss that made the first real remux promise twice what it paid.

        mkv stores no per-track bitrate, so a dropped track is worth whatever
        this guess says -- and on a remux that guess *is* the whole estimate.
        A flat 256k read every stereo track as twice its size.
        """
        stereo = {"codec": "aac", "channels": 2, "bitrate": None}
        surround = {"codec": "aac", "channels": 6, "bitrate": None}

        assert estimate_mod.track_bitrate(stereo) == 128_000
        assert estimate_mod.track_bitrate(surround) == 384_000

    def test_a_declared_bitrate_always_beats_the_guess(self):
        real = {"codec": "aac", "channels": 2, "bitrate": 96_000}
        assert estimate_mod.track_bitrate(real) == 96_000

    def test_an_aac_track_of_unknown_width_is_assumed_stereo(self):
        """Under-stating a dropped track under-promises the saving, which is
        the direction this module is built to err in."""
        assert estimate_mod.track_bitrate({"codec": "aac"}) == 128_000

    def test_parse_bitrate_accepts_the_ffmpeg_spelling(self):
        assert estimate_mod.parse_bitrate("640k") == 640_000
        assert estimate_mod.parse_bitrate("1.5m") == 1_500_000
        assert estimate_mod.parse_bitrate(320_000) == 320_000


class TestPriority:
    def test_a_fat_file_outranks_a_lean_one_of_the_same_length(self, config, ladder):
        fat = plan_one(facts(size_bytes=6 * GB, v_bitrate=18_000_000), config, ladder)
        lean = plan_one(facts(size_bytes=3 * GB, v_bitrate=8_000_000), config, ladder)
        fat.priority = planner._priority(fat.est_saved_bytes, fat.est_cpu_seconds)
        lean.priority = planner._priority(lean.est_saved_bytes, lean.est_cpu_seconds)
        assert fat.priority > lean.priority

    def test_a_free_remux_outranks_any_encode(self, config, ladder):
        remux = plan_one(
            facts(
                v_codec="hevc", size_bytes=8 * GB, duration_s=7200.0,
                audio=[
                    {"codec": "eac3", "channels": 6, "language": "eng", "bitrate": 640_000},
                    {"codec": "dts", "channels": 6, "language": "fra", "bitrate": 1_500_000},
                    {"codec": "dts", "channels": 6, "language": "deu", "bitrate": 1_500_000},
                ],
            ),
            config, ladder,
        )
        encode = plan_one(facts(), config, ladder)
        assert planner._priority(remux.est_saved_bytes, remux.est_cpu_seconds) > \
            planner._priority(encode.est_saved_bytes, encode.est_cpu_seconds)


# --------------------------------------------------------------- the database


def add_title(db, name="Show", kind="show", external_id="tvdb:1"):
    db.execute(
        "INSERT INTO title (kind, source, external_id, name, year) VALUES (?,?,?,?,?)",
        (kind, "plex", external_id, name, 2019),
    )
    return db.scalar("SELECT last_insert_rowid()")


def add_file(db, path, *, title_id=None, size=3 * GB, codec="h264", hdr="sdr",
             width=1920, height=1080, bitrate=8_000_000, duration=EPISODE,
             audio=None, season=1, episode=1, root="/media/tv"):
    now = time.time()
    db.execute(
        "INSERT INTO media_file "
        "(path, library_root, size_bytes, mtime, probe_version, probed_at, container, "
        " duration_s, v_codec, v_bit_depth, v_width, v_height, v_bitrate, v_fps, "
        " hdr_type, audio_json, first_seen, last_seen, missing) "
        "VALUES (?,?,?,?,1,?,'matroska,webm',?,?,8,?,?,?,24.0,?,?,?,?,0)",
        (path, root, size, now, now, duration, codec, width, height, bitrate, hdr,
         json.dumps(audio or [{"codec": "eac3", "channels": 6, "language": "eng",
                               "bitrate": 640_000}]),
         now, now),
    )
    file_id = db.scalar("SELECT last_insert_rowid()")
    if title_id:
        db.execute(
            "INSERT INTO file_title (file_id, title_id, season, episode, resolved_by, "
            "confidence) VALUES (?,?,?,?, 'plex', 1.0)",
            (file_id, title_id, season, episode),
        )
    return file_id


class TestPlannerBuild:
    def test_writes_a_decision_for_every_probed_file(self, db, config, ladder):
        add_file(db, "/media/tv/a.mkv")
        add_file(db, "/media/tv/b.mkv", hdr="hdr10")

        stats = planner.build(db, config, ladder=ladder)
        assert stats.considered == 2
        assert db.scalar("SELECT COUNT(*) FROM decision") == 2
        assert stats.skipped == 1

    def test_a_provisional_ladder_writes_non_executable_decisions(self, db, config):
        add_file(db, "/media/tv/a.mkv")
        stats = planner.build(db, config, ladder=provisional_ladder(config.policy))

        assert stats.provisional
        assert db.scalar("SELECT state FROM decision") == "provisional"
        assert not db.query("SELECT 1 FROM decision WHERE state='pending'")

    def test_a_real_ladder_writes_pending_decisions(self, db, config, ladder):
        add_file(db, "/media/tv/a.mkv")
        planner.build(db, config, ladder=ladder)
        assert db.scalar("SELECT state FROM decision") == "pending"

    def test_open_duplicate_members_are_left_out_of_the_queue(self, db, config, ladder):
        title = add_title(db)
        first = add_file(db, "/media/tv/a.mkv", title_id=title)
        second = add_file(db, "/media/tv/b.mkv", title_id=title, size=4 * GB)
        db.execute(
            "INSERT INTO duplicate_group (title_id, season, episode, keeper_file_id, "
            "member_count, reclaimable_bytes, needs_human, reason, status, created_at) "
            "VALUES (?,1,1,?,2,?,0,'x','open',?)",
            (title, second, 3 * GB, time.time()),
        )
        group = db.scalar("SELECT last_insert_rowid()")
        for rank, file_id in enumerate((second, first)):
            db.execute(
                "INSERT INTO duplicate_member (group_id, file_id, rank) VALUES (?,?,?)",
                (group, file_id, rank),
            )

        planner.build(db, config, ladder=ladder)
        states = {r["state"] for r in db.query("SELECT state FROM decision")}
        assert states == {"skipped"}

    def test_one_title_cannot_monopolise_the_queue(self, db, config, ladder):
        config.policy.max_queued_per_title = 3
        title = add_title(db)
        for index in range(6):
            add_file(db, f"/media/tv/e{index}.mkv", title_id=title, episode=index)

        stats = planner.build(db, config, ladder=ladder)
        assert stats.queued == 3
        assert stats.deferred == 3
        assert db.scalar("SELECT COUNT(*) FROM decision WHERE state='deferred'") == 3

    def test_rebuilding_leaves_in_flight_decisions_alone(self, db, config, ladder):
        file_id = add_file(db, "/media/tv/a.mkv")
        planner.build(db, config, ladder=ladder)
        db.execute("UPDATE decision SET state='running' WHERE file_id=?", (file_id,))

        stats = planner.build(db, config, ladder=ladder)
        assert stats.preserved == 1
        assert stats.considered == 0
        assert db.scalar("SELECT state FROM decision WHERE file_id=?", (file_id,)) \
            == "running"

    def test_content_class_falls_back_to_the_library_root(self, db, config, ladder):
        add_file(db, "/media/movies/film.mkv", root="/media/movies", duration=7200.0,
                 size=8 * GB, bitrate=12_000_000)
        planner.build(db, config, ladder=ladder)
        profile = db.scalar("SELECT profile FROM decision")
        assert profile is not None

    def test_totals_report_what_the_plan_costs_and_saves(self, db, config, ladder):
        add_file(db, "/media/tv/a.mkv")
        planner.build(db, config, ladder=ladder)

        totals = planner.totals(db)
        assert totals["queued"] == 1
        assert totals["saved"] > 0
        assert totals["cpu_seconds"] > 0
        assert totals["provisional"] is False

    def test_top_jobs_come_back_best_first(self, db, config, ladder):
        add_file(db, "/media/tv/lean.mkv", size=3 * GB, bitrate=8_000_000)
        add_file(db, "/media/tv/fat.mkv", size=9 * GB, bitrate=26_000_000)

        planner.build(db, config, ladder=ladder)
        rows = planner.top_jobs(db, 10)
        assert [r["path"] for r in rows][0] == "/media/tv/fat.mkv"


class TestProfilesFile:
    def test_a_missing_profiles_file_reads_as_none(self, tmp_path):
        assert load_ladder(tmp_path / "nope.yaml") is None

    def test_round_trips_what_bench_writes(self, tmp_path):
        from bench.ladder import LadderEntry, to_profiles_yaml

        entry = LadderEntry(
            encoder="hevc_vaapi", content_class="tv", resolution="1080p",
            target_vmaf=92.0, quality=24.0, quality_flag="qp",
            expected_size_ratio=0.44, expected_out_bitrate=3_100_000,
            expected_fps=31.0, extrapolated=False, samples=6,
        )
        path = tmp_path / "profiles.yaml"
        path.write_text(to_profiles_yaml([entry], "hevc_vaapi"), encoding="utf-8")

        loaded = load_ladder(path)
        found = loaded.rung_for("tv", "1080p")
        assert found.quality == 24.0
        assert found.expected_out_bitrate == 3_100_000
        assert loaded.provisional is False

    def test_the_preferred_encoder_wins_over_software(self):
        both = Ladder(
            [rung(), rung(encoder="libx265", expected_fps=4.0)],
            preferred_encoder="hevc_vaapi",
        )
        assert both.rung_for("tv", "1080p").encoder == "hevc_vaapi"
