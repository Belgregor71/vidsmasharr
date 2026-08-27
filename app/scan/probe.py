"""ffprobe wrapper: raw JSON in, normalised stream facts out.

The HDR detection here is the single most safety-critical piece of the project.
Apollo Lake QuickSync can only *encode* HEVC in 8-bit, so pushing an HDR source
through the hardware path would silently produce an SDR file and destroy the HDR
grade permanently. There is no undo once the original is deleted.

So the rule is: this module errs toward declaring content protected. A false
positive costs us some savings on one file. A false negative destroys it.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# Bump when the parsing logic changes in a way that invalidates stored results,
# so the scanner knows to re-probe.
PROBE_VERSION = 1

# Transfer characteristics that mean HDR.
HDR_TRANSFERS = {"smpte2084", "smpte428", "arib-std-b67"}
# Wide-gamut primaries. Not proof of HDR on their own, but enough to protect.
WIDE_GAMUT_PRIMARIES = {"bt2020"}

LOSSLESS_AUDIO = {"truehd", "dts", "flac", "mlp", "pcm_s24le", "pcm_s16le", "alac"}
# DTS-HD MA / DTS:X appear as codec "dts" with a profile; treat by profile too.
LOSSLESS_AUDIO_PROFILES = {"DTS-HD MA", "DTS-HD Master Audio", "DTS-X", "DTS:X"}


class ProbeError(RuntimeError):
    pass


@dataclass
class AudioTrack:
    index: int
    codec: str
    profile: str | None
    channels: int
    language: str
    title: str | None
    bitrate: int | None
    is_default: bool
    is_forced: bool
    is_commentary: bool

    @property
    def is_lossless(self) -> bool:
        if self.codec in LOSSLESS_AUDIO:
            # Plain DTS core is lossy; only the HD profiles are lossless.
            if self.codec == "dts":
                return (self.profile or "") in LOSSLESS_AUDIO_PROFILES
            return True
        return False


@dataclass
class SubtitleTrack:
    index: int
    codec: str
    language: str
    title: str | None
    is_default: bool
    is_forced: bool
    is_sdh: bool


@dataclass
class MediaInfo:
    path: str
    size_bytes: int
    container: str
    duration_s: float
    bitrate: int | None
    v_codec: str | None
    v_profile: str | None
    v_bit_depth: int | None
    v_width: int | None
    v_height: int | None
    v_bitrate: int | None
    v_fps: float | None
    pix_fmt: str | None
    color_transfer: str | None
    color_primaries: str | None
    hdr_type: str
    audio: list[AudioTrack] = field(default_factory=list)
    subs: list[SubtitleTrack] = field(default_factory=list)

    @property
    def is_protected_hdr(self) -> bool:
        """True for anything the 8-bit hardware encoder must never touch."""
        return self.hdr_type not in ("sdr",)

    @property
    def resolution_tier(self) -> str:
        h = self.v_height or 0
        w = self.v_width or 0
        if w >= 3000 or h >= 1700:
            return "2160p"
        if w >= 1800 or h >= 1000:
            return "1080p"
        if w >= 1200 or h >= 700:
            return "720p"
        return "sd"

    def to_row(self) -> dict[str, Any]:
        return {
            "container": self.container,
            "duration_s": self.duration_s,
            "bitrate": self.bitrate,
            "v_codec": self.v_codec,
            "v_profile": self.v_profile,
            "v_bit_depth": self.v_bit_depth,
            "v_width": self.v_width,
            "v_height": self.v_height,
            "v_bitrate": self.v_bitrate,
            "v_fps": self.v_fps,
            "hdr_type": self.hdr_type,
            "audio_json": json.dumps([asdict(a) for a in self.audio]),
            "subs_json": json.dumps([asdict(s) for s in self.subs]),
        }


# ---------------------------------------------------------------- helpers


def _to_int(value: Any) -> int | None:
    try:
        result = int(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


def _parse_fps(rate: str | None) -> float | None:
    if not rate or "/" not in rate:
        return _to_float(rate)
    num, _, den = rate.partition("/")
    try:
        n, d = float(num), float(den)
        return n / d if d else None
    except ValueError:
        return None


def _bit_depth(stream: dict) -> int | None:
    """Highest bit depth any signal claims.

    bits_per_raw_sample and pix_fmt disagree surprisingly often -- remuxes in
    particular carry a stale "8" alongside a yuv420p10le pix_fmt. Taking the
    maximum rather than trusting one source keeps the disagreement on the safe
    side: we would rather protect an 8-bit file than encode a 10-bit one.
    """
    candidates = []

    tagged = _to_int(stream.get("bits_per_raw_sample"))
    if tagged:
        candidates.append(tagged)

    pix_fmt = stream.get("pix_fmt") or ""
    if pix_fmt:
        for marker, value in (("16", 16), ("12", 12), ("10", 10), ("9", 9)):
            if marker in pix_fmt:
                candidates.append(value)
                break
        else:
            candidates.append(8)

    return max(candidates) if candidates else None


def detect_hdr(stream: dict) -> str:
    """Classify a video stream's dynamic range, biased toward protection.

    Returns one of: sdr | hdr10 | hdr10plus | hlg | dolbyvision | unknown-10bit
    | unknown. Everything except "sdr" is treated as untouchable by the
    hardware encoder.
    """
    # 1. Dolby Vision announces itself in stream side data.
    for side in stream.get("side_data_list") or []:
        side_type = str(side.get("side_data_type", "")).lower()
        if "dovi" in side_type or "dolby vision" in side_type:
            return "dolbyvision"
        if "hdr dynamic metadata" in side_type or "hdr10+" in side_type:
            return "hdr10plus"

    transfer = (stream.get("color_transfer") or "").lower()
    primaries = (stream.get("color_primaries") or "").lower()
    depth = _bit_depth(stream)

    if transfer == "arib-std-b67":
        return "hlg"
    if transfer in HDR_TRANSFERS:
        return "hdr10"
    if primaries in WIDE_GAMUT_PRIMARIES:
        # BT.2020 primaries without a PQ/HLG transfer is unusual. Could be
        # mislabelled SDR, could be a stripped HDR file. Protect it.
        return "hdr10"
    if depth and depth > 8:
        # 10-bit without HDR signalling is often a high-quality SDR encode, but
        # we cannot re-encode it in hardware without a bit-depth drop anyway.
        return "unknown-10bit"
    if depth == 8:
        return "sdr"
    # Could not determine bit depth at all -- do not guess.
    return "unknown"


def _language(stream: dict) -> str:
    tags = stream.get("tags") or {}
    lang = (tags.get("language") or tags.get("LANGUAGE") or "und").lower()
    return lang[:3] if lang else "und"


def _title(stream: dict) -> str | None:
    tags = stream.get("tags") or {}
    return tags.get("title") or tags.get("TITLE")


def _looks_like_commentary(stream: dict) -> bool:
    title = (_title(stream) or "").lower()
    return any(word in title for word in ("comment", "director", "cast & crew"))


def _looks_like_sdh(stream: dict) -> bool:
    title = (_title(stream) or "").lower()
    return "sdh" in title or "hearing" in title or "cc" == title.strip()


# ---------------------------------------------------------------- entry point


def build_ffprobe_command(ffprobe: str, path: Path | str) -> list[str]:
    return [
        ffprobe,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        # side_data carries the Dolby Vision configuration record.
        "-show_entries", "stream_side_data",
        str(path),
    ]


def parse_probe_json(path: str, size_bytes: int, data: dict) -> MediaInfo:
    fmt = data.get("format") or {}
    streams = data.get("streams") or []

    video = next(
        (
            s for s in streams
            if s.get("codec_type") == "video"
            # Cover art shows up as a video stream; ignore it.
            and (s.get("disposition") or {}).get("attached_pic", 0) != 1
        ),
        None,
    )

    duration = _to_float(fmt.get("duration")) or 0.0
    total_bitrate = _to_int(fmt.get("bit_rate"))

    audio: list[AudioTrack] = []
    for s in streams:
        if s.get("codec_type") != "audio":
            continue
        disp = s.get("disposition") or {}
        audio.append(
            AudioTrack(
                index=int(s.get("index", 0)),
                codec=(s.get("codec_name") or "").lower(),
                profile=s.get("profile"),
                channels=int(s.get("channels") or 0),
                language=_language(s),
                title=_title(s),
                bitrate=_to_int(s.get("bit_rate")),
                is_default=bool(disp.get("default")),
                is_forced=bool(disp.get("forced")),
                is_commentary=bool(disp.get("comment")) or _looks_like_commentary(s),
            )
        )

    subs: list[SubtitleTrack] = []
    for s in streams:
        if s.get("codec_type") != "subtitle":
            continue
        disp = s.get("disposition") or {}
        subs.append(
            SubtitleTrack(
                index=int(s.get("index", 0)),
                codec=(s.get("codec_name") or "").lower(),
                language=_language(s),
                title=_title(s),
                is_default=bool(disp.get("default")),
                is_forced=bool(disp.get("forced")),
                is_sdh=bool(disp.get("hearing_impaired")) or _looks_like_sdh(s),
            )
        )

    if video is None:
        # No usable video stream. Mark as unknown so nothing will touch it.
        return MediaInfo(
            path=path, size_bytes=size_bytes,
            container=(fmt.get("format_name") or "").split(",")[0],
            duration_s=duration, bitrate=total_bitrate,
            v_codec=None, v_profile=None, v_bit_depth=None,
            v_width=None, v_height=None, v_bitrate=None, v_fps=None,
            pix_fmt=None, color_transfer=None, color_primaries=None,
            hdr_type="unknown", audio=audio, subs=subs,
        )

    v_bitrate = _to_int(video.get("bit_rate"))
    if v_bitrate is None and total_bitrate:
        # Container-level bitrate minus what we can account for in audio. Rough,
        # but good enough to rank candidates by.
        known_audio = sum(a.bitrate or 0 for a in audio)
        remainder = total_bitrate - known_audio
        if remainder > 0:
            v_bitrate = remainder
    if v_bitrate is None and duration > 0:
        v_bitrate = int(size_bytes * 8 / duration)

    return MediaInfo(
        path=path,
        size_bytes=size_bytes,
        container=(fmt.get("format_name") or "").split(",")[0],
        duration_s=duration,
        bitrate=total_bitrate,
        v_codec=(video.get("codec_name") or "").lower(),
        v_profile=video.get("profile"),
        v_bit_depth=_bit_depth(video),
        v_width=_to_int(video.get("width")),
        v_height=_to_int(video.get("height")),
        v_bitrate=v_bitrate,
        v_fps=_parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        pix_fmt=video.get("pix_fmt"),
        color_transfer=video.get("color_transfer"),
        color_primaries=video.get("color_primaries"),
        hdr_type=detect_hdr(video),
        audio=audio,
        subs=subs,
    )


def probe(path: Path | str, ffprobe: str = "ffprobe", timeout: int = 120) -> MediaInfo:
    path = Path(path)
    cmd = build_ffprobe_command(ffprobe, path)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out after {timeout}s: {path}") from exc
    except FileNotFoundError as exc:
        raise ProbeError(f"ffprobe not found at {ffprobe!r}") from exc

    if result.returncode != 0 or not result.stdout.strip():
        raise ProbeError(
            f"ffprobe failed ({result.returncode}) for {path}: "
            f"{(result.stderr or '').strip()[:400]}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned unparseable JSON for {path}") from exc

    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return parse_probe_json(str(path), size, data)
