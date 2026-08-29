"""Quality-ladder interpolation tests.

The ladder decides how hard every file in the library gets compressed, so the
interpolation needs to pick the *coarsest* setting that still clears the target
-- picking the finest would waste most of the available savings, and picking one
past the target would ship visibly bad encodes.
"""

from bench import ladder
from bench.ladder import _interpolate, build_ladder, project_timeline
from bench.runner import Measurement

# VMAF falls as QP rises.
SWEEP = [(20, 97.0), (23, 95.0), (26, 92.0), (29, 88.0)]


def test_exact_hit_returns_that_setting():
    quality, extrapolated = _interpolate(SWEEP, 95.0)
    assert quality == 23
    assert extrapolated is False


def test_interpolates_between_bracketing_points():
    quality, extrapolated = _interpolate(SWEEP, 93.5)
    assert quality == 24.5
    assert extrapolated is False


def test_picks_the_coarsest_setting_that_still_passes():
    # Target 92 is met at QP 26; QP 20 and 23 also pass but waste savings.
    quality, _ = _interpolate(SWEEP, 92.0)
    assert quality == 26


def test_all_settings_pass_is_flagged_as_extrapolated():
    quality, extrapolated = _interpolate(SWEEP, 80.0)
    assert quality == 29  # coarsest we tried
    assert extrapolated is True


def test_no_setting_passes_falls_back_to_finest_and_flags_it():
    quality, extrapolated = _interpolate(SWEEP, 99.0)
    assert quality == 20  # finest we tried
    assert extrapolated is True


def test_non_monotonic_sweep_stays_inside_the_bracket():
    # Real measurements wobble. The dip at QP 23 must not make us jump past the
    # highest QP that actually passed (26) toward the one that failed (29).
    noisy = [(20, 97.0), (23, 94.0), (26, 94.5), (29, 88.0)]
    quality, _ = _interpolate(noisy, 94.2)
    assert 26 <= quality < 29


def _m(encoder="hevc_vaapi", quality=23, vmaf=95.0, height=1080, fps=60.0, ratio=0.5):
    return Measurement(
        clip="c", encoder=encoder, quality=quality,
        src_width=1920, src_height=height, out_width=1920, out_height=height,
        frames=750, wall_seconds=12.5, cpu_seconds=3.0, fps=fps,
        in_bytes=100_000_000, out_bytes=int(100_000_000 * ratio), size_ratio=ratio,
        vmaf_mean=vmaf, vmaf_min=vmaf - 4, vmaf_p1=vmaf,
    )


def test_build_ladder_emits_an_entry_per_content_class():
    measurements = [_m(quality=q, vmaf=v, ratio=0.7 - i * 0.08)
                    for i, (q, v) in enumerate(SWEEP)]
    entries = build_ladder(measurements, {"movie": 95.0, "tv": 92.0})

    by_class = {e.content_class: e for e in entries}
    assert set(by_class) == {"movie", "tv"}
    # The stricter movie target must give a finer setting than the TV target.
    assert by_class["movie"].quality < by_class["tv"].quality
    assert by_class["movie"].quality_flag == "qp"


def test_build_ladder_ignores_failed_measurements():
    good = [_m(quality=q, vmaf=v) for q, v in SWEEP]
    bad = _m(quality=35, vmaf=60.0)
    bad.ok = False
    entries = build_ladder(good + [bad], {"tv": 92.0})
    assert entries and all(e.samples == len(good) for e in entries)


def test_build_ladder_needs_at_least_two_points():
    assert build_ladder([_m()], {"tv": 92.0}) == []


def test_projection_reports_calendar_time_not_just_seconds():
    entries = build_ladder([_m(quality=q, vmaf=v) for q, v in SWEEP], {"tv": 92.0})
    projection = project_timeline(
        entries, total_files=6000, average_minutes_per_file=45.0, hours_per_day=8.0
    )
    assert projection["total_files"] == 6000
    assert projection["calendar_days_at_night_only"] > projection["calendar_days_24x7"]
    # 6000 files at 45 min each is a big job however fast the encoder is.
    assert projection["total_cpu_hours"] > 100


def test_projection_without_hardware_measurements_is_an_error_not_a_guess():
    software_only = [_m(encoder="libx265", quality=q, vmaf=v) for q, v in SWEEP]
    entries = build_ladder(software_only, {"tv": 92.0})
    assert "error" in project_timeline(
        entries, total_files=100, average_minutes_per_file=45.0
    )


def test_hardware_qp_is_floored_to_an_integer():
    # -qp takes an int. Flooring (not rounding) means noise can only move the
    # setting toward higher quality, never past the target.
    fractional = [(20, 97.0), (23, 95.0), (26, 92.0), (29, 88.0)]
    measurements = [_m(quality=q, vmaf=v) for q, v in fractional]
    entry = build_ladder(measurements, {"tv": 93.5})[0]
    assert entry.quality == 24.0        # interpolated 24.5, floored to 24
    assert entry.quality == int(entry.quality)


def test_software_crf_keeps_fractional_precision():
    # libx265 accepts fractional CRF, so we do not throw the precision away.
    measurements = [_m(encoder="libx265", quality=q, vmaf=v)
                    for q, v in [(19, 97.0), (22, 95.0), (25, 92.0)]]
    entry = build_ladder(measurements, {"tv": 93.5})[0]
    assert entry.quality == 23.5
    assert entry.quality_flag == "crf"


def test_ladder_refuses_settings_that_grow_the_file():
    # Regression from the first real NAS run: an already-x265 720p source came
    # back at 299% size at qp=20, and the ladder happily emitted it as a rung.
    bloated = [
        _m(encoder="hevc_vaapi", quality=q, vmaf=v, ratio=r)
        for q, v, r in [(20, 89.4, 2.99), (23, 87.6, 1.98),
                        (26, 85.2, 1.34), (29, 81.5, 1.05)]
    ]
    entries = build_ladder(bloated, {"movie": 95.0, "tv": 92.0})

    assert len(entries) == 1
    assert entries[0].content_class == "unusable"
    assert "NO USABLE SETTING" in entries[0].note
    # Crucially, no movie/tv rung was emitted for production to act on.
    assert not [e for e in entries if e.content_class in ("movie", "tv")]


def test_ladder_still_works_when_only_some_settings_shrink():
    mixed = [
        _m(encoder="hevc_vaapi", quality=q, vmaf=v, ratio=r)
        for q, v, r in [(20, 97.0, 1.20), (23, 95.0, 0.88),
                        (26, 92.0, 0.61), (29, 88.0, 0.44)]
    ]
    entries = build_ladder(mixed, {"tv": 92.0})
    assert [e.content_class for e in entries] == ["tv"]
    assert entries[0].expected_size_ratio < 1.0


def test_ladder_calibrates_on_the_mean_not_the_worst_frame():
    # A single corrupt frame tanks p1 but must not move the chosen setting.
    good = [_m(quality=q, vmaf=v, ratio=0.6) for q, v in SWEEP]
    for m in good:
        m.vmaf_p1 = 4.3          # the artifact seen on the first real run
    entry = build_ladder(good, {"tv": 92.0})[0]
    assert entry.quality == 26   # from the means, exactly as if p1 were sane


class TestMultipleClipAggregation:
    """Each group holds one series per calibration clip, sharing quality values.

    Before these, duplicate quality values were fed straight to the
    interpolator, which silently followed whichever clip sorted first -- in
    practice the easiest one. The real 2026-08-28 run produced a 1080p TV rung
    promising a 14% size ratio at a setting where the visible clip measured 39%
    and scored VMAF 90.5 against a target of 92.
    """

    HARD = {20: (96.5, 0.72), 23: (94.0, 0.55), 26: (90.5, 0.39),
            29: (83.4, 0.25), 32: (75.3, 0.17)}
    EASY = {20: (99.0, 0.30), 23: (97.5, 0.22), 26: (95.0, 0.14),
            29: (92.5, 0.10), 32: (88.0, 0.07)}

    def _measurements(self):
        from bench.runner import Measurement

        out = []
        for clip, series in (("hard", self.HARD), ("easy", self.EASY)):
            for quality, (vmaf, ratio) in series.items():
                out.append(Measurement(
                    clip=clip, encoder="hevc_vaapi", quality=quality,
                    src_width=1920, src_height=1080, out_width=None,
                    out_height=1080, frames=720, wall_seconds=20.0,
                    cpu_seconds=20.0, fps=35.0, in_bytes=100_000_000,
                    out_bytes=int(100_000_000 * ratio), size_ratio=ratio,
                    src_fps=24.0, vmaf_mean=vmaf,
                ))
        return out

    def test_the_hardest_clip_governs_the_setting(self):
        entries = ladder.build_ladder(self._measurements(), {"tv": 92.0})
        rung = next(e for e in entries if e.content_class == "tv")

        # The easy clip alone would have justified qp 26 or coarser. The hard
        # clip scores 90.5 there, so the ladder must not go that far.
        assert rung.quality < 26

    def test_the_size_ratio_is_not_taken_from_the_easiest_clip(self):
        entries = ladder.build_ladder(self._measurements(), {"tv": 92.0})
        rung = next(e for e in entries if e.content_class == "tv")

        # Somewhere between the two clips, never at or below the easy one.
        easy_best = min(ratio for _, ratio in self.EASY.values())
        assert rung.expected_size_ratio > easy_best

    def test_disagreeing_clips_are_called_out(self):
        entries = ladder.build_ladder(self._measurements(), {"tv": 92.0})
        rung = next(e for e in entries if e.content_class == "tv")
        assert "disagreed" in rung.note

    def test_agreeing_clips_produce_no_warning(self):
        from bench.runner import Measurement

        out = []
        for clip in ("a", "b"):
            for quality, vmaf in ((20, 97.0), (23, 94.5), (26, 91.0), (29, 87.0)):
                out.append(Measurement(
                    clip=clip, encoder="hevc_vaapi", quality=quality,
                    src_width=1920, src_height=1080, out_width=None,
                    out_height=1080, frames=720, wall_seconds=20.0,
                    cpu_seconds=20.0, fps=35.0, in_bytes=100_000_000,
                    out_bytes=40_000_000, size_ratio=0.4, src_fps=24.0,
                    vmaf_mean=vmaf,
                ))
        entries = ladder.build_ladder(out, {"tv": 92.0})
        assert all("disagreed" not in e.note for e in entries)

    def test_aggregation_collapses_duplicates(self):
        points = [(20, 90.0), (20, 96.0), (23, 85.0), (23, 89.0)]
        assert ladder._aggregate(points, "min") == [(20, 90.0), (23, 85.0)]
        assert ladder._aggregate(points, "mean") == [(20, 93.0), (23, 87.0)]


class TestProjection:
    """SD encodes several times faster than 1080p and holds few of the bytes.

    Averaging every rung together produced a headline of 61 fps on the real
    run, a speed no 1080p file will ever see.
    """

    def _entry(self, resolution, fps):
        return ladder.LadderEntry(
            encoder="hevc_vaapi", content_class="tv", resolution=resolution,
            target_vmaf=92.0, quality=24.0, quality_flag="qp",
            expected_size_ratio=0.4, expected_out_bitrate=None,
            expected_fps=fps, extrapolated=False, samples=10,
        )

    def test_sd_speed_does_not_inflate_the_estimate(self):
        entries = [self._entry("1080p", 35.9), self._entry("sd", 157.5)]
        projection = ladder.project_timeline(
            entries, total_files=1000, average_minutes_per_file=45.0
        )
        assert projection["measured_encode_fps"] == 35.9
        assert projection["projected_from"] == "1080p"

    def test_no_measurements_at_that_resolution_is_an_error_not_a_guess(self):
        projection = ladder.project_timeline(
            [self._entry("sd", 157.5)], total_files=1000,
            average_minutes_per_file=45.0,
        )
        assert "error" in projection


class TestRebuildFromStoredRuns:
    """Re-running the benchmark costs a night; rebuilding costs seconds."""

    def _db(self, tmp_path):
        from app.db import Database

        return Database(tmp_path / "bench.db")

    def _store(self, db, run_id, *, quality_values, vmaf=94.0, resolution=1080):
        import time

        for quality in quality_values:
            db.execute(
                "INSERT INTO bench_result (run_id, clip, encoder, quality_key, "
                "quality_value, src_width, src_height, out_height, frames, "
                "wall_seconds, cpu_seconds, fps, in_bytes, out_bytes, size_ratio, "
                "vmaf_mean, ok, created_at) "
                "VALUES (?,?,'hevc_vaapi','qp',?,1920,?,?,720,20,20,35,100,40,0.4,?,1,?)",
                (run_id, f"{run_id}_clip", quality, resolution, resolution, vmaf,
                 time.time()),
            )

    def test_a_targeted_re_run_can_add_to_the_ladder_not_replace_it(self, tmp_path):
        """A movies-only re-run must not drop last night's TV rungs."""
        db = self._db(tmp_path)
        self._store(db, "night_one", quality_values=[20, 23, 26])
        self._store(db, "movies_only", quality_values=[14, 17])

        combined = ladder.load_measurements(db, ["night_one", "movies_only"])
        assert len(combined) == 5
        assert {m.clip for m in combined} == {"night_one_clip", "movies_only_clip"}

    def test_the_default_is_the_most_recent_run(self, tmp_path):
        db = self._db(tmp_path)
        self._store(db, "older", quality_values=[20])
        self._store(db, "newer", quality_values=[20])
        assert ladder.latest_run_id(db) == "newer"

    def test_runs_come_back_oldest_first(self, tmp_path):
        db = self._db(tmp_path)
        self._store(db, "older", quality_values=[20])
        self._store(db, "newer", quality_values=[20])
        assert ladder.all_run_ids(db) == ["older", "newer"]

    def test_an_unreachable_target_says_how_to_fix_it(self):
        from bench.runner import Measurement

        # Nothing reaches VMAF 95, exactly like the 2026-08-28 run.
        out = []
        for quality, vmaf in ((20, 94.9), (23, 92.6), (26, 89.0)):
            out.append(Measurement(
                clip="hard", encoder="hevc_vaapi", quality=quality,
                src_width=1920, src_height=1080, out_width=None, out_height=1080,
                frames=720, wall_seconds=20.0, cpu_seconds=20.0, fps=35.0,
                in_bytes=100, out_bytes=40, size_ratio=0.4, src_fps=24.0,
                vmaf_mean=vmaf,
            ))
        entries = ladder.build_ladder(out, {"movie": 95.0})
        note = entries[0].note

        assert "--qp-sweep" in note
        # And it names values below the finest actually tried.
        assert "14" in note


# ------------------------------------------------ content class separation


def _c(clip, quality, vmaf, ratio, content_class="", encoder="hevc_vaapi",
       height=1080):
    """A measurement that knows which clip and which content class it is."""
    m = _m(encoder=encoder, quality=quality, vmaf=vmaf, height=height, ratio=ratio)
    m.clip = clip
    m.content_class = content_class
    return m


class TestContentClassSeparation:
    """A movie sweep and a TV sweep at one resolution are two populations.

    Pooling them corrupts both at once, and it did. On the 2026-08-28 combined
    run, two thin TV clips and two fat movie clips averaged to 81% of source at
    q=20, which slipped under the "re-encoding this makes it bigger" guard that
    should have rejected the movie half outright -- and the movie rung came out
    at qp 19 with a quoted size of 118%, a number no file will ever produce.
    """

    def clips(self, tagged: bool):
        out = []
        # A thin TV source: shrinks well.
        for quality, vmaf, ratio in ((20, 95.7, 0.40), (23, 94.3, 0.25),
                                     (26, 92.4, 0.16), (29, 89.4, 0.11)):
            out.append(_c("tv_clip", quality, vmaf, ratio,
                          "tv" if tagged else ""))
        # An already-efficient movie source: every setting inflates it.
        for quality, vmaf, ratio in ((14, 98.1, 2.92), (17, 97.5, 1.87),
                                     (20, 96.4, 1.17)):
            out.append(_c("movie_clip", quality, vmaf, ratio,
                          "movie" if tagged else ""))
        return out

    def test_pooling_invents_a_movie_rung_out_of_tv_measurements(self):
        """The contrast is the whole point. Pooled, the movie half borrows the
        TV half's thinness and a confident-looking rung appears for content
        that cannot be usefully encoded at all. Separated, the same numbers say
        so."""
        pooled = build_ladder(self.clips(tagged=False),
                              {"movie": 95.0, "tv": 92.0})
        separated = build_ladder(self.clips(tagged=True),
                                 {"movie": 95.0, "tv": 92.0})

        pooled_movie = next(e for e in pooled if e.content_class == "movie")
        assert pooled_movie.expected_size_ratio < 1.0      # looks encodable

        assert not [e for e in separated if e.content_class == "movie"]
        assert [e for e in separated if e.content_class == "unusable"]

    def test_separated_the_movie_half_is_called_unusable(self):
        entries = build_ladder(self.clips(tagged=True),
                               {"movie": 95.0, "tv": 92.0})
        oversized = [e.content_class for e in entries
                     if e.expected_size_ratio > 1.0]
        assert oversized == ["unusable"]
        unusable = next(e for e in entries if e.content_class == "unusable")
        assert "makes these files bigger" in unusable.note

    def test_the_tv_rung_survives_untouched(self):
        entries = build_ladder(self.clips(tagged=True),
                               {"movie": 95.0, "tv": 92.0})
        tv = next(e for e in entries if e.content_class == "tv")
        assert tv.expected_size_ratio < 0.30

    def test_a_group_answers_only_for_its_own_target(self):
        entries = build_ladder(
            [_c("tv_clip", q, v, r, "tv")
             for q, v, r in ((20, 95.0, 0.40), (26, 92.0, 0.16))],
            {"movie": 95.0, "tv": 92.0},
        )
        assert {e.content_class for e in entries} == {"tv"}

    def test_untagged_measurements_still_build_but_say_so(self):
        entries = build_ladder(self.clips(tagged=False),
                               {"movie": 95.0, "tv": 92.0})
        assert any("before the content class was recorded" in e.note
                   for e in entries)


class TestBrokenClips:
    def test_a_clip_that_inflates_and_scores_badly_is_excluded(self):
        """Seen for real: one of two clips from the same film scored 75-78 at
        every setting while its output ran to 1.9x the size of the source,
        beside a sibling scoring 93-96 on the same sweep. More bits cannot cost
        eighteen points of VMAF -- the comparison was misaligned. Because VMAF
        aggregates by minimum, that one clip decided the whole rung."""
        good = [_c("good", q, v, r, "movie")
                for q, v, r in ((14, 97.6, 2.31), (17, 96.8, 1.49),
                                (20, 95.5, 0.92))]
        broken = [_c("broken", q, v, r, "movie")
                  for q, v, r in ((14, 78.2, 1.90), (17, 77.6, 1.25),
                                  (20, 76.5, 0.77))]

        entries = build_ladder(good + broken, {"movie": 95.0})
        assert entries
        assert "excluded 1 clip" in entries[0].note
        assert "broken" in entries[0].note

    def test_genuinely_hard_content_is_kept(self):
        """Scoring low while genuinely shrinking the file is hard content, and
        the ladder is supposed to be governed by it."""
        hard = [_c("hard", q, v, r, "tv")
                for q, v, r in ((20, 84.0, 0.55), (26, 80.0, 0.35))]
        entries = build_ladder(hard, {"tv": 92.0})
        assert entries
        assert "excluded" not in entries[0].note


class TestRobustMode:
    """--robust: interpolate per clip, then discount the hardest of them.

    The 2026-08-29 combined run is the case this exists for. Ten TV clips, nine
    of which reached VMAF 92 somewhere between QP 22 and 26. The tenth (a dark,
    grain-heavy Willow episode) topped out at 90.8 and never reached it at all.
    Pooling the VMAF curves by minimum first meant the pooled curve never
    crossed 92 either, so the rung fell off the end of the sweep to QP 20 and
    56% of source -- roughly a third of the reclaim the other nine would give.
    """

    def _sweep(self, clip, offset=0.0, ratio_scale=1.0):
        """A clip that clears 92 near QP 26, shifted by `offset` VMAF points."""
        return [_c(clip, q, v + offset, r * ratio_scale, "tv")
                for q, v, r in ((20, 96.0, 0.40), (23, 94.0, 0.25),
                                (26, 92.0, 0.16), (29, 88.0, 0.11))]

    def test_default_is_unchanged(self):
        ms = self._sweep("a") + self._sweep("b", offset=-1.0)
        assert (build_ladder(ms, {"tv": 92.0})[0].quality
                == build_ladder(ms, {"tv": 92.0}, robust=False)[0].quality)

    def test_an_unreachable_clip_no_longer_decides_the_rung(self):
        reachable = (self._sweep("a") + self._sweep("b", offset=-0.5)
                     + self._sweep("c", offset=-1.0))
        # Tops out below the target at every setting, exactly like Willow_1.
        unreachable = [_c("hopeless", q, v, r, "tv")
                       for q, v, r in ((20, 90.8, 0.62), (23, 88.0, 0.30),
                                       (26, 85.0, 0.16), (29, 80.8, 0.10))]
        ms = reachable + unreachable

        strict = build_ladder(ms, {"tv": 92.0})[0]
        robust = build_ladder(ms, {"tv": 92.0}, robust=True)[0]

        # Strict pools the curves, never crosses the target, and pins to the
        # finest setting tried -- reporting a size ratio it never calibrated.
        assert strict.quality == 20
        assert strict.extrapolated is True
        # Robust sets the clip aside by name and calibrates on the rest.
        assert robust.quality > strict.quality
        assert robust.extrapolated is False
        assert "hopeless" in robust.note
        assert "unreachable" in robust.note
        assert robust.expected_size_ratio < strict.expected_size_ratio

    def test_below_the_minimum_clip_count_it_stays_on_the_hardest(self):
        """Trimming the harder of two clips is not robustness, it is picking
        the flattering number. Under ROBUST_MIN_CLIPS it must not trim."""
        ms = self._sweep("a") + self._sweep("b", offset=-2.0)
        entry = build_ladder(ms, {"tv": 92.0}, robust=True)[0]
        hardest = min(s.quality for s in ladder._per_clip_settings(ms, 92.0))
        assert entry.quality == float(int(hardest))
        assert "below the 3 needed" in entry.note

    def test_a_rank_beyond_the_clip_count_does_not_crash(self, monkeypatch):
        """ROBUST_RANK is a tuning constant and the clip count comes from
        whatever the benchmark sampled; the two can cross. Found by sweeping
        the rank to see how far the rung moves."""
        monkeypatch.setattr(ladder, "ROBUST_RANK", 99)
        ms = (self._sweep("a") + self._sweep("b", offset=-0.5)
              + self._sweep("c", offset=-1.0))
        entry = build_ladder(ms, {"tv": 92.0}, robust=True)[0]
        # Clamped to the easiest clip rather than raising IndexError.
        assert entry.quality > 0

    def test_a_clip_that_cannot_shrink_at_its_own_setting_is_set_aside(self):
        """Already-efficient content: it reaches the target, but only by
        spending as many bits as the source. That is a file not worth
        encoding, not evidence about the setting for files that are."""
        ok = (self._sweep("a") + self._sweep("b", offset=-0.5)
              + self._sweep("c", offset=-1.0))
        efficient = self._sweep("fat", offset=-1.5, ratio_scale=5.0)
        entry = build_ladder(ok + efficient, {"tv": 92.0}, robust=True)[0]
        assert "fat" in entry.note
        assert "not worth encoding" in entry.note


class TestSizeSpreadWarning:
    def test_clips_disagreeing_on_size_are_warned_about(self):
        """VMAF disagreement was already warned about; size disagreement was
        not. On the 2026-08-29 run two clips of the same show differed by 49
        points of size at the chosen setting, and the rung reported their
        average as if it described either of them."""
        thin = [_c("thin", q, v, r, "tv")
                for q, v, r in ((20, 95.2, 0.33), (23, 93.9, 0.13),
                                (26, 91.9, 0.07))]
        fat = [_c("fat", q, v, r, "tv")
               for q, v, r in ((20, 95.0, 1.03), (23, 93.6, 0.62),
                               (26, 91.5, 0.39))]
        entry = build_ladder(thin + fat, {"tv": 92.0})[0]
        assert "points of size" in entry.note

    def test_clips_that_agree_on_size_are_not_warned_about(self):
        a = [_c("a", q, v, r, "tv")
             for q, v, r in ((20, 95.2, 0.33), (23, 93.9, 0.20), (26, 91.9, 0.12))]
        b = [_c("b", q, v, r, "tv")
             for q, v, r in ((20, 95.0, 0.35), (23, 93.6, 0.22), (26, 91.5, 0.14))]
        entry = build_ladder(a + b, {"tv": 92.0})[0]
        assert "points of size" not in entry.note


class TestClipCounts:
    """Rungs built from different clip populations are not comparable on size.

    On the 2026-08-29 combined run under --robust, hevc_qsv at 1080p set aside
    three of ten clips as unreachable and hevc_vaapi set aside one. qsv's 36%
    was then an average over an easier seven clips than vaapi's 39% over nine,
    and the table read as though qsv were the smaller encoder. It is not.
    """

    def test_the_rung_reports_how_many_clips_set_it(self):
        ms = ([_c("a", q, v, r, "tv")
               for q, v, r in ((20, 96.0, 0.40), (23, 94.0, 0.25), (26, 92.0, 0.16))]
              + [_c("b", q, v, r, "tv")
                 for q, v, r in ((20, 95.5, 0.42), (23, 93.5, 0.27), (26, 91.5, 0.18))])
        entry = build_ladder(ms, {"tv": 92.0})[0]
        assert entry.clips_used == 2
        assert entry.clips_set_aside == 0
        assert "2" in ladder.render_text([entry])

    def test_set_aside_clips_are_counted_separately(self):
        ok = []
        for i, clip in enumerate(("a", "b", "c")):
            ok += [_c(clip, q, v - i * 0.5, r, "tv")
                   for q, v, r in ((20, 96.0, 0.40), (23, 94.0, 0.25),
                                   (26, 92.0, 0.16))]
        unreachable = [_c("hopeless", q, v, r, "tv")
                       for q, v, r in ((20, 90.0, 0.62), (23, 88.0, 0.30),
                                       (26, 85.0, 0.16))]
        entry = build_ladder(ok + unreachable, {"tv": 92.0}, robust=True)[0]
        assert entry.clips_used == 3
        assert entry.clips_set_aside == 1
        assert "3-1" in ladder.render_text([entry])


class TestRunClassPersistence:
    """--run-class has to outlive the command that used it.

    Labelling in memory only means every later rebuild must remember the flag,
    and forgetting is silent: the run falls back to the "unknown" class, and an
    unknown group speaks for *every* target. Six TV clips would then produce
    movie rungs, which is the fabrication the content classes exist to stop.
    """

    def _db(self, tmp_path, rows):
        from app.db import Database
        db = Database(tmp_path / "t.db")
        db.migrate()
        for run, klass in rows:
            db.execute(
                "INSERT INTO bench_result (run_id, clip, encoder, quality_key, "
                "quality_value, content_class, ok, created_at) "
                "VALUES (?,?,?,?,?,?,1,0)",
                (run, "c", "hevc_vaapi", "qp", 23.0, klass),
            )
        return db

    def test_the_label_is_written_into_the_measurements(self, tmp_path):
        db = self._db(tmp_path, [("r1", ""), ("r1", "")])
        ladder.persist_run_classes(db, {"r1": "tv"})
        assert db.scalar(
            "SELECT COUNT(*) FROM bench_result WHERE content_class='tv'") == 2

    def test_dry_run_writes_nothing(self, tmp_path):
        db = self._db(tmp_path, [("r1", ""), ("r1", "")])
        out = ladder.persist_run_classes(db, {"r1": "tv"}, dry_run=True)
        assert db.scalar(
            "SELECT COUNT(*) FROM bench_result WHERE content_class='tv'") == 0
        assert any("would label" in line for line in out)

    def test_a_run_that_already_recorded_its_class_is_left_alone(self, tmp_path):
        """The flag recovers what a run asserted before the column existed. A
        run that already answered must not be reinterpreted by a typo."""
        db = self._db(tmp_path, [("r1", "movie"), ("r1", "movie")])
        out = ladder.persist_run_classes(db, {"r1": "tv"})
        assert db.scalar(
            "SELECT COUNT(*) FROM bench_result WHERE content_class='movie'") == 2
        assert db.scalar(
            "SELECT COUNT(*) FROM bench_result WHERE content_class='tv'") == 0
        assert any("left alone" in line for line in out)

    def test_a_partly_labelled_run_only_fills_the_blanks(self, tmp_path):
        db = self._db(tmp_path, [("r1", "tv"), ("r1", "")])
        ladder.persist_run_classes(db, {"r1": "tv"})
        assert db.scalar(
            "SELECT COUNT(*) FROM bench_result WHERE content_class='tv'") == 2
