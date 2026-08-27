"""FFmpeg command construction, shared by the benchmark and the real worker.

Kept separate from the worker so Phase 0 can exercise exactly the command shapes
that Phase 3 will run in production -- a benchmark that measures a different
command than the one we ship is worse than no benchmark.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Quality knob per encoder. Lower value = higher quality for all three.
QUALITY_FLAG = {
    "hevc_vaapi": "qp",
    "h264_vaapi": "qp",
    "hevc_qsv": "global_quality",
    "libx265": "crf",
    "libx264": "crf",
}


@dataclass
class VideoSpec:
    encoder: str                    # hevc_vaapi | hevc_qsv | libx265
    quality: float                  # qp / global_quality / crf
    target_height: int | None = None  # set to downscale (e.g. 1080)
    preset: str = "slow"            # software encoders only
    bit_depth: int = 8              # software encoders only
    hw_decode: bool = True          # full hardware pipeline where possible


def _is_vaapi(encoder: str) -> bool:
    return encoder.endswith("_vaapi")


def _is_qsv(encoder: str) -> bool:
    return encoder.endswith("_qsv")


def is_hardware(encoder: str) -> bool:
    return _is_vaapi(encoder) or _is_qsv(encoder)


def _scale_expr(kind: str, target_height: int) -> str:
    """Even-width preserving scale. -2 keeps the aspect and forces divisibility
    by 2, which every HEVC encoder requires."""
    if kind == "vaapi":
        # scale_vaapi wants explicit values; -2 is supported for w.
        return f"scale_vaapi=w=-2:h={target_height}"
    if kind == "qsv":
        return f"scale_qsv=w=-2:h={target_height}"
    return f"scale=-2:{target_height}:flags=lanczos"


def build_video_args(spec: VideoSpec, vaapi_device: Path | str) -> tuple[list[str], list[str]]:
    """Return (input_prefix_args, output_args) for a video encode.

    Split in two because hardware init flags must appear before -i while the
    codec flags must appear after it.
    """
    device = str(vaapi_device)

    if _is_vaapi(spec.encoder):
        pre = ["-init_hw_device", f"vaapi=va:{device}", "-filter_hw_device", "va"]
        if spec.hw_decode:
            # Decode straight into VAAPI surfaces: no round trip through system
            # RAM, which matters a lot on a 4GB box with slow memory.
            pre += ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"]
            chain = []
            if spec.target_height:
                chain.append(_scale_expr("vaapi", spec.target_height))
            vf = ",".join(chain) if chain else None
        else:
            chain = ["format=nv12", "hwupload"]
            if spec.target_height:
                chain.append(_scale_expr("vaapi", spec.target_height))
            vf = ",".join(chain)

        out = ["-c:v", spec.encoder, "-rc_mode", "CQP", "-qp", str(int(spec.quality))]
        if vf:
            out = ["-vf", vf] + out
        return pre, out

    if _is_qsv(spec.encoder):
        pre = ["-init_hw_device", f"qsv=hw:{device}", "-filter_hw_device", "hw"]
        if spec.hw_decode:
            pre += ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
            chain = []
            if spec.target_height:
                chain.append(_scale_expr("qsv", spec.target_height))
            vf = ",".join(chain) if chain else None
        else:
            chain = ["hwupload=extra_hw_frames=64", "format=qsv"]
            if spec.target_height:
                chain.append(_scale_expr("qsv", spec.target_height))
            vf = ",".join(chain)

        out = [
            "-c:v", spec.encoder,
            "-global_quality", str(int(spec.quality)),
            # Lookahead needs encoder memory we do not have to spare, and
            # Apollo Lake gains little from it.
            "-look_ahead", "0",
        ]
        if vf:
            out = ["-vf", vf] + out
        return pre, out

    # Software.
    pix_fmt = "yuv420p10le" if spec.bit_depth > 8 else "yuv420p"
    out = [
        "-c:v", spec.encoder,
        "-preset", spec.preset,
        "-crf", str(spec.quality),
        "-pix_fmt", pix_fmt,
    ]
    if spec.target_height:
        out = ["-vf", _scale_expr("sw", spec.target_height)] + out
    return [], out


def build_encode_command(
    *,
    ffmpeg: str,
    source: Path | str,
    dest: Path | str,
    spec: VideoSpec,
    vaapi_device: Path | str,
    audio_args: list[str] | None = None,
    extra_input_args: list[str] | None = None,
    threads: int | None = None,
) -> list[str]:
    pre, video_out = build_video_args(spec, vaapi_device)

    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y"]
    cmd += pre
    cmd += extra_input_args or []
    cmd += ["-i", str(source)]
    cmd += ["-map", "0:v:0"]
    cmd += video_out

    if audio_args is None:
        cmd += ["-an", "-sn"]
    else:
        cmd += audio_args

    if threads is not None and not is_hardware(spec.encoder):
        cmd += ["-threads", str(threads)]

    # Preserve chapters and file-level metadata across the re-encode.
    cmd += ["-map_metadata", "0", "-map_chapters", "0"]
    cmd += [str(dest)]
    return cmd


def build_vmaf_command(
    *,
    ffmpeg: str,
    distorted: Path | str,
    reference: Path | str,
    log_path: Path | str,
    threads: int = 2,
    reference_height: int | None = None,
) -> list[str]:
    """Score `distorted` against `reference`.

    When the encode downscaled, the distorted stream is scaled back up to the
    reference resolution before comparison. That is the meaningful question for
    us: how does the smaller file look on a TV that will upscale it anyway.
    """
    scale = ""
    if reference_height:
        scale = f"scale=-2:{reference_height}:flags=bicubic,"

    # log_path goes through two levels of parsing (filtergraph, then filter
    # args), so any directory separator or drive colon in it is a minefield.
    # Pass a bare filename and run ffmpeg with cwd set to the log's directory
    # instead -- that works identically on Linux and Windows.
    graph = (
        f"[0:v]{scale}setpts=PTS-STARTPTS,format=yuv420p[dist];"
        f"[1:v]setpts=PTS-STARTPTS,format=yuv420p[ref];"
        f"[dist][ref]libvmaf=log_fmt=json:log_path={_escape_filter_path(log_path)}"
        f":n_threads={threads}"
    )
    return [
        ffmpeg, "-hide_banner", "-nostdin",
        "-i", str(distorted),
        "-i", str(reference),
        "-lavfi", graph,
        "-f", "null", "-",
    ]


def _escape_filter_path(path: Path | str) -> str:
    """Filtergraph arguments treat : and \\ specially."""
    text = str(path).replace("\\", "/")
    return text.replace(":", "\\:")
