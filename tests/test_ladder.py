"""Quality-ladder interpolation tests.

The ladder decides how hard every file in the library gets compressed, so the
interpolation needs to pick the *coarsest* setting that still clears the target
-- picking the finest would waste most of the available savings, and picking one
past the target would ship visibly bad encodes.
"""

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
