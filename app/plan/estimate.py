"""How big will the output be, and how long will it take?

Everything the queue ordering depends on comes from here, so the estimates are
deliberately conservative: where two models disagree we take the one that
promises *less* saving. Over-promising would push a file up a queue that is
measured in months, ahead of one that really would have paid better.

None of these numbers are exact and they are not meant to be. `plan/calibrate.py`
closes the loop by comparing them against the `outcome` table, which is why
every estimate records the basis it was computed from -- and why a corrected
estimate says so, rather than quietly becoming the new raw model.

A note on "cpu_seconds": for a hardware encode the work happens on the iGPU and
barely touches the CPU. The field means *elapsed encode seconds* -- the scarce
resource either way, since only one encode runs at a time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.plan.profiles import ASSUMED_FPS, Rung
from app.plan.rules import FileFacts

# Bitrate to assume for an audio track ffprobe reported no bitrate for -- which
# in mkv is nearly all of them, the container rarely stores it.
#
# These are not harmless. On a *remux* the dropped audio is the entire saving,
# so this table alone decides both the promised GB and where the job lands in a
# queue ranked by GB per CPU-hour. The first real remux came in at 51% of its
# estimate for exactly this reason; see AAC_BITRATE_PER_CHANNEL.
AUDIO_BITRATE_GUESS = {
    "truehd": 4_500_000,
    "mlp": 4_500_000,
    "pcm_s24le": 6_900_000,
    "pcm_s16le": 4_600_000,
    "flac": 1_000_000,
    "dts": 1_500_000,
    "eac3": 640_000,
    "ac3": 448_000,
    "opus": 192_000,
    "mp3": 192_000,
    "vorbis": 192_000,
}
DTS_HD_BITRATE = 3_500_000
DEFAULT_AUDIO_BITRATE = 384_000

# AAC is the one codec common enough, and variable enough, to be worth scaling
# by channel count rather than guessing flat. A flat 256_000 over-promised by
# exactly 2x on the first real remux -- twelve stereo tracks that actually ran
# near 128k -- while the same file's 5.1 tracks are genuinely fatter than any
# single number the table could hold.
AAC_BITRATE_PER_CHANNEL = 64_000

# Frame rate to assume when ffprobe could not report one.
DEFAULT_FPS = 24.0

# Muxing overhead: index, headers, subtitle tracks we do not model.
CONTAINER_OVERHEAD = 1.01

# An encode that does not beat this fraction of the source bitrate is not worth
# running, so never predict an output above it -- predicting one would put a
# pointless job in the queue with a plausible-looking saving.
MAX_USEFUL_BITRATE_RATIO = 0.95

# Remux throughput: read and write on the same spinning volume.
REMUX_BYTES_PER_SECOND = 150 * 1024**2
# Audio-only transcode runs far faster than real time even on a Celeron.
AUDIO_ENCODE_REALTIME = 40.0


@dataclass
class Estimate:
    out_bytes: int
    saved_bytes: int
    cpu_seconds: float
    basis: str
    detail: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ audio


def parse_bitrate(value: str | int) -> int:
    """"640k" -> 640000. Config accepts the ffmpeg spelling."""
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    multiplier = 1
    if text.endswith("k"):
        multiplier, text = 1_000, text[:-1]
    elif text.endswith("m"):
        multiplier, text = 1_000_000, text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return DEFAULT_AUDIO_BITRATE


def track_bitrate(track: dict) -> int:
    if track.get("bitrate"):
        return int(track["bitrate"])
    codec = (track.get("codec") or "").lower()
    if codec == "dts" and (track.get("profile") or "") not in ("", "DTS"):
        return DTS_HD_BITRATE
    if codec == "aac":
        # Unknown channel count falls back to stereo rather than to something
        # larger: under-stating a dropped track under-promises the saving, and
        # that is the direction this module is built to err in.
        channels = int(track.get("channels") or 2)
        return AAC_BITRATE_PER_CHANNEL * max(channels, 1)
    return AUDIO_BITRATE_GUESS.get(codec, DEFAULT_AUDIO_BITRATE)


def track_bytes(track: dict, duration_s: float) -> int:
    return int(track_bitrate(track) * max(duration_s, 0.0) / 8)


def _track_quality(track: dict) -> tuple:
    """Sort key for "best track": channels first, commentary last."""
    return (
        int(track.get("channels") or 0),
        0 if track.get("is_commentary") else 1,
        track_bitrate(track),
    )


def select_audio(facts: FileFacts, audio_cfg) -> dict[str, Any]:
    """Which audio track survives, and what happens to it.

    One stored file has to direct-play on both TVs, so the output carries a
    single best track in a codec everything decodes. The one hard exception:
    if *nothing* matches the keep-languages list we keep the file exactly as it
    is. An anime library is full of Japanese-only files, and a rule that strips
    a file down to no audio at all is worse than one that saves nothing.
    """
    tracks = facts.audio or []
    if not tracks:
        return {"keep": [], "drop": [], "transcode": False, "note": "no audio tracks"}

    wanted = {lang.lower() for lang in audio_cfg.keep_languages}
    matching = [t for t in tracks if (t.get("language") or "und").lower() in wanted]
    if not matching:
        return {
            "keep": list(tracks), "drop": [], "transcode": False,
            "note": (
                "no track in the keep-languages list; keeping every track rather "
                "than leaving the file silent"
            ),
        }

    best = max(matching, key=_track_quality)
    drop = [t for t in tracks if t is not best]

    bitrate = track_bitrate(best)
    codec = (best.get("codec") or "").lower()
    passthrough = (
        codec in {c.lower() for c in audio_cfg.passthrough_codecs}
        and bitrate <= audio_cfg.passthrough_max_bitrate
    )
    return {
        "keep": [best], "drop": drop, "transcode": not passthrough,
        "note": (
            f"keep {codec} {best.get('channels') or '?'}ch "
            f"{best.get('language') or 'und'}"
            + ("" if passthrough else f" -> {audio_cfg.target_codec} "
               f"{audio_cfg.target_bitrate}")
        ),
    }


def audio_bytes_after(facts: FileFacts, audio_cfg) -> tuple[int, int, dict]:
    """(bytes kept, bytes dropped, selection detail)."""
    selection = select_audio(facts, audio_cfg)
    duration = facts.duration_s

    if selection["transcode"]:
        target = parse_bitrate(audio_cfg.target_bitrate)
        kept = int(target * duration / 8)
    else:
        kept = sum(track_bytes(t, duration) for t in selection["keep"])

    original_kept = sum(track_bytes(t, duration) for t in selection["keep"])
    dropped = sum(track_bytes(t, duration) for t in selection["drop"])
    # Shrinking the kept track is a saving too.
    dropped += max(0, original_kept - kept)

    detail = {
        "audio_note": selection["note"],
        "dropped_tracks": len(selection["drop"]),
        "audio_transcode": selection["transcode"],
    }
    return kept, dropped, detail


# ------------------------------------------------------------------ video


def source_video_bytes(facts: FileFacts) -> int:
    """The share of the file that is video, with audio accounted for.

    Uses the real file size rather than the probed video bitrate, because the
    file size is a fact and the bitrate is often a container-level guess.
    """
    audio_total = sum(track_bytes(t, facts.duration_s) for t in facts.audio or [])
    return max(0, facts.size_bytes - audio_total)


def video_out_bitrate(
    facts: FileFacts, rung: Rung, target_height: int | None, policy
) -> tuple[int, str]:
    """Predicted output video bitrate, and where the number came from.

    Two models, and we take whichever predicts the *larger* output:

    - the ladder's measured output bitrate, or failing that the policy target
      for the output resolution. Both are source-independent, which is right
      for constant-quality encoding: QP output depends on how complex the
      picture is, not on how fat the source file was.
    - the ladder's measured size ratio applied to this file. This one tracks
      the source, so it over-predicts output for unusually fat sources -- and
      over-predicting output means under-promising savings, which is the safe
      direction here.
    """
    out_tier = "1080p" if target_height == 1080 else facts.tier
    models: list[tuple[int, str]] = []

    if rung.expected_out_bitrate:
        models.append((int(rung.expected_out_bitrate), "ladder-bitrate"))
    else:
        target = {
            "2160p": policy.target_bitrate_1080p,
            "1080p": policy.target_bitrate_1080p,
            "720p": policy.target_bitrate_720p,
            "sd": policy.target_bitrate_sd,
        }.get(out_tier, policy.target_bitrate_1080p)
        models.append((int(target), "policy-target"))

    if rung.expected_size_ratio and facts.v_bitrate:
        models.append(
            (int(facts.v_bitrate * rung.expected_size_ratio), "ladder-ratio")
        )

    bitrate, basis = max(models)

    # Never predict an output that fails to beat the source.
    if facts.v_bitrate:
        ceiling = int(facts.v_bitrate * MAX_USEFUL_BITRATE_RATIO)
        if bitrate > ceiling:
            bitrate, basis = ceiling, f"{basis}/source-capped"

    return bitrate, basis


def encode_seconds(facts: FileFacts, rung: Rung, target_height: int | None) -> float:
    out_tier = "1080p" if target_height == 1080 else facts.tier
    fps = rung.expected_fps or ASSUMED_FPS.get(out_tier, 25.0)
    frames = max(facts.duration_s, 0.0) * (facts.v_fps or DEFAULT_FPS)
    return frames / max(fps, 0.1)


# ------------------------------------------------------------------ estimator


class Estimator:
    """The default estimator. Injected into the rules so it can be stubbed.

    `calibration` is what Phase 4 learned from the jobs that have actually run
    (see plan/calibrate.py). None means the raw model, which is what every plan
    made before the first encode finished necessarily uses.
    """

    def __init__(self, calibration=None):
        self.calibration = calibration

    def _correct(
        self, out_bytes: int, cpu_seconds: float, basis: str, speed_key: str
    ) -> tuple[int, float, str]:
        """Apply the measured corrections, and say in the basis that we did.

        The suffix is not decoration. The next calibration groups by basis, so
        a corrected estimate is measured as its own model and its factor
        converges towards 1 instead of compounding on the one already applied.
        """
        if self.calibration is None:
            return out_bytes, cpu_seconds, basis
        size = self.calibration.size_factor(basis)
        speed = self.calibration.speed_factor(speed_key)
        return int(out_bytes * size), cpu_seconds * speed, f"{basis}+cal"

    def encode(
        self, facts: FileFacts, rung: Rung, target_height: int | None, config
    ) -> Estimate:
        bitrate, basis = video_out_bitrate(facts, rung, target_height, config.policy)
        video_out = int(bitrate * max(facts.duration_s, 0.0) / 8)
        audio_out, audio_dropped, detail = audio_bytes_after(facts, config.audio)

        out_bytes = int((video_out + audio_out) * CONTAINER_OVERHEAD)
        cpu_seconds = encode_seconds(facts, rung, target_height)
        out_tier = "1080p" if target_height == 1080 else facts.tier
        out_bytes, cpu_seconds, basis = self._correct(
            out_bytes, cpu_seconds, basis, f"{rung.encoder}:{out_tier}"
        )
        # An encode can genuinely grow a file; the rules use the saving to
        # reject that, so it must be allowed to come out at or below zero.
        saved = facts.size_bytes - out_bytes

        detail.update({
            "encoder": rung.encoder,
            "quality": rung.quality,
            "quality_flag": rung.quality_flag,
            "target_vmaf": rung.target_vmaf,
            "target_height": target_height,
            "out_bitrate": bitrate,
            "video_out_bytes": video_out,
            "audio_out_bytes": audio_out,
            "audio_saved_bytes": audio_dropped,
            "source_bitrate": facts.v_bitrate,
        })
        return Estimate(
            out_bytes=out_bytes,
            saved_bytes=max(saved, 0),
            cpu_seconds=cpu_seconds,
            basis=basis,
            detail=detail,
        )

    def remux(self, facts: FileFacts, config) -> Estimate:
        """Drop unwanted tracks; the video stream is copied untouched."""
        video = source_video_bytes(facts)
        audio_out, audio_dropped, detail = audio_bytes_after(facts, config.audio)

        out_bytes = int((video + audio_out) * CONTAINER_OVERHEAD)
        seconds = max(5.0, facts.size_bytes / REMUX_BYTES_PER_SECOND)
        if detail.get("audio_transcode"):
            seconds += facts.duration_s / AUDIO_ENCODE_REALTIME

        basis = "stream-copy"
        out_bytes, seconds, basis = self._correct(
            out_bytes, seconds, basis, f"remux:{facts.tier}"
        )

        detail.update({
            "video_out_bytes": video,
            "audio_out_bytes": audio_out,
            "audio_saved_bytes": audio_dropped,
        })
        return Estimate(
            out_bytes=out_bytes,
            saved_bytes=max(facts.size_bytes - out_bytes, 0),
            cpu_seconds=seconds,
            basis=basis,
            detail=detail,
        )


DEFAULT = Estimator()
