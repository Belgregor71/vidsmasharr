"""Is this output good enough to replace the original with?

The answer has to be "no" by default. Every check below is written so that
missing information fails rather than passes: an unreadable output, a VMAF pass
that could not run, a duration we could not measure. The cost of a false "no"
is one wasted encode. The cost of a false "yes" is a file the user cannot get
back, because the next step deletes the original.

Two gates, in order:

1. **Structural.** Does the file open, is it the length it should be, does it
   still have video and audio, is it actually smaller. These are cheap and
   catch the failures that actually happen -- a truncated encode from a full
   disk, a dropped audio track from a bad map, an "encode" that grew.
2. **Perceptual.** Sampled VMAF against the source. Expensive, so it runs only
   after the structural checks pass, and not at all for a remux, where the
   video stream is copied bit for bit and there is nothing to score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.scan.probe import MediaInfo, ProbeError, probe
from app.work import vmaf

# A container rewrite shifts duration by a frame or two; a truncated encode
# shifts it by minutes. Allow the larger of half a percent and one second.
DURATION_TOLERANCE_PCT = 0.005
DURATION_TOLERANCE_MIN_S = 1.0

# An output at or above this fraction of the source did not earn its CPU.
MAX_ACCEPTABLE_SIZE_RATIO = 0.98


@dataclass
class Verification:
    ok: bool = False
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    vmaf_mean: float | None = None
    vmaf_min: float | None = None
    vmaf_p1: float | None = None
    samples: list[float] = field(default_factory=list)
    out_info: MediaInfo | None = None

    def fail(self, reason: str) -> "Verification":
        self.failures.append(reason)
        self.ok = False
        return self

    @property
    def summary(self) -> str:
        if self.ok:
            score = f", VMAF {self.vmaf_mean:.1f}" if self.vmaf_mean is not None else ""
            return f"verified{score}"
        return "; ".join(self.failures) or "failed"


def check_structure(
    source: MediaInfo,
    output: Path,
    *,
    ffprobe: str,
    expect_codec: str | None = "hevc",
    expect_height: int | None = None,
    expect_audio_tracks: int | None = None,
) -> Verification:
    result = Verification()

    if not output.exists():
        return result.fail("output file does not exist")
    if output.stat().st_size == 0:
        return result.fail("output file is empty")

    try:
        info = probe(output, ffprobe)
    except ProbeError as exc:
        return result.fail(f"output will not probe: {exc}")
    result.out_info = info

    if not info.v_codec:
        return result.fail("output has no video stream")
    if expect_codec and info.v_codec != expect_codec:
        result.fail(f"output video is {info.v_codec}, expected {expect_codec}")

    tolerance = max(DURATION_TOLERANCE_MIN_S, source.duration_s * DURATION_TOLERANCE_PCT)
    if source.duration_s > 0:
        if info.duration_s <= 0:
            result.fail("output duration could not be read")
        elif abs(info.duration_s - source.duration_s) > tolerance:
            result.fail(
                f"output is {info.duration_s:.1f}s against the source's "
                f"{source.duration_s:.1f}s -- truncated or wrongly muxed"
            )

    if expect_height and info.v_height and abs(info.v_height - expect_height) > 8:
        result.fail(f"output is {info.v_height} rows, expected {expect_height}")

    # A file that lost its audio plays silently and looks fine in a thumbnail.
    if source.audio and not info.audio:
        result.fail("output has no audio at all but the source did")
    elif expect_audio_tracks is not None and len(info.audio) != expect_audio_tracks:
        result.fail(
            f"output has {len(info.audio)} audio track(s), expected "
            f"{expect_audio_tracks}"
        )

    if source.size_bytes:
        ratio = info.size_bytes / source.size_bytes
        if ratio >= MAX_ACCEPTABLE_SIZE_RATIO:
            result.fail(
                f"output is {ratio * 100:.0f}% of the source; the encode did not "
                f"earn its time"
            )

    result.ok = not result.failures
    return result


def check_quality(
    source_path: Path,
    output: Path,
    *,
    source: MediaInfo,
    config,
    target_vmaf: float,
    work_dir: Path,
    downscaled: bool = False,
    threads: int = 2,
    progress=None,
) -> Verification:
    """Sampled VMAF. Every sample must clear the bar, not just the average.

    Scoring the mean of the means would let one badly handled scene hide behind
    two easy ones, and the badly handled scene is the whole reason to check.
    """
    result = Verification()
    floor = target_vmaf - config.quality.vmaf_fail_margin

    offsets = vmaf.sample_offsets(
        source.duration_s,
        config.quality.vmaf_sample_count,
        config.quality.vmaf_sample_seconds,
    )
    if not offsets:
        return result.fail("could not choose VMAF sample points (unknown duration)")

    means: list[float] = []
    for offset in offsets:
        if progress:
            progress(f"      VMAF sample at {offset / 60:.0f}m ...")
        score = vmaf.score(
            output, source_path,
            ffmpeg=config.vmaf_ffmpeg,
            work_dir=work_dir,
            reference_height=source.v_height if downscaled else None,
            threads=threads,
            start_s=offset,
            duration_s=float(config.quality.vmaf_sample_seconds),
        )
        if not score.ok:
            # Could not measure means could not verify. Never a pass.
            return result.fail(f"VMAF could not be measured: {score.error}")

        means.append(score.mean)
        result.samples.append(round(score.mean, 2))
        if result.vmaf_min is None or (score.min is not None and score.min < result.vmaf_min):
            result.vmaf_min = score.min
        if score.p1 is not None and (result.vmaf_p1 is None or score.p1 < result.vmaf_p1):
            result.vmaf_p1 = score.p1

        if score.mean < floor:
            result.fail(
                f"VMAF {score.mean:.1f} at {offset / 60:.0f}m is below the "
                f"{floor:.1f} floor (target {target_vmaf:.0f})"
            )

    result.vmaf_mean = sum(means) / len(means)
    result.ok = not result.failures
    return result


def merge(structure: Verification, quality: Verification | None) -> Verification:
    """One verdict from both gates."""
    if quality is None:
        return structure
    return Verification(
        ok=structure.ok and quality.ok,
        failures=structure.failures + quality.failures,
        warnings=structure.warnings + quality.warnings,
        vmaf_mean=quality.vmaf_mean,
        vmaf_min=quality.vmaf_min,
        vmaf_p1=quality.vmaf_p1,
        samples=quality.samples,
        out_info=structure.out_info,
    )
