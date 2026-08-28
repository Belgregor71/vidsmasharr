"""Phase 0, step 2: measure what this box really achieves.

Everything the planner does downstream -- priority ranking, "how many months
will this take", the quality ladder -- depends on numbers that are specific to
this CPU, this ffmpeg build and this library's content. This module produces
them by encoding real clips from the real library.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from app.db import Database
from app.plan.rules import EFFICIENT_CODECS, MIN_ENCODE_BITRATE
from app.scan.probe import MediaInfo, probe
from app.work import vmaf
from app.work.ffmpeg_cmd import QUALITY_FLAG, VideoSpec, build_encode_command


@dataclass
class Clip:
    path: Path
    source_path: str
    label: str
    content_class: str      # movie | tv
    info: MediaInfo


@dataclass
class Measurement:
    clip: str
    encoder: str
    quality: float
    src_width: int | None
    src_height: int | None
    out_width: int | None
    out_height: int | None
    frames: int | None
    wall_seconds: float
    cpu_seconds: float
    fps: float | None
    in_bytes: int
    out_bytes: int
    size_ratio: float | None
    # Source frame rate, so the ladder can turn out_bytes into an output
    # bitrate -- which is what the planner actually estimates with.
    src_fps: float | None = None
    vmaf_mean: float | None = None
    vmaf_min: float | None = None
    vmaf_p1: float | None = None
    ok: bool = True
    error: str | None = None


def _child_cpu_seconds() -> float:
    """Cumulative CPU consumed by child processes. Linux-accurate; 0 on Windows."""
    times = os.times()
    return times.children_user + times.children_system


def _run_timed(cmd: list[str], timeout: int) -> tuple[subprocess.CompletedProcess, float, float]:
    cpu_before = _child_cpu_seconds()
    wall_before = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    wall = time.monotonic() - wall_before
    cpu = _child_cpu_seconds() - cpu_before
    return result, wall, cpu


# Rejection thresholds come from production policy (app/plan/rules.py) rather
# than being restated here: the benchmark must reject exactly what the planner
# rejects, or the ladder gets calibrated on jobs we will never run. Observed on
# the first real run -- a 720p x265 clip came out at 299% of its original size.
MIN_CANDIDATE_BITRATE = MIN_ENCODE_BITRATE


def calibration_reject_reason(info: MediaInfo) -> str | None:
    """Why this file is a bad thing to calibrate on, or None if it is fine.

    The ladder must be built from files the policy would actually re-encode.
    Anything else measures a job we will never run.
    """
    if info.is_protected_hdr:
        return f"{info.hdr_type} is protected content and never re-encoded"
    if not info.v_codec:
        return "no readable video stream"
    if info.v_codec in EFFICIENT_CODECS:
        return (
            f"already {info.v_codec} -- re-encoding this makes it bigger, "
            "and policy would skip it"
        )
    floor = MIN_CANDIDATE_BITRATE.get(info.resolution_tier, 1_000_000)
    if info.v_bitrate and info.v_bitrate < floor:
        return (
            f"{info.v_bitrate / 1e6:.1f}Mbps is already below the "
            f"{floor / 1e6:.1f}Mbps floor for {info.resolution_tier}"
        )
    return None


# ------------------------------------------------------------------ clips


def extract_clips(
    sources: list[Path],
    out_dir: Path,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    per_source: int = 2,
    seconds: int = 30,
    content_class: str = "tv",
    max_sources: int | None = None,
) -> list[Clip]:
    """Cut representative clips out of real library files.

    Stream-copied, so extraction is fast and the clip is bit-identical to the
    source -- which makes it a valid VMAF reference. Sample points avoid the
    first and last 10% so we don't benchmark on credits and black frames.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    clips: list[Clip] = []
    accepted = 0

    for source in sources:
        if max_sources is not None and accepted >= max_sources:
            break
        try:
            info = probe(source, ffprobe)
        except Exception as exc:  # noqa: BLE001 - a bad file must not stop the run
            print(f"  ! skipping {source.name}: {exc}")
            continue

        if info.duration_s < seconds * 3:
            print(f"  ! skipping {source.name}: too short to sample")
            continue
        reject = calibration_reject_reason(info)
        if reject:
            print(f"  ! skipping {source.name}: {reject}")
            continue

        usable_start = info.duration_s * 0.10
        usable_span = info.duration_s * 0.80
        for index in range(per_source):
            offset = usable_start + usable_span * (index + 0.5) / per_source
            label = f"{source.stem[:40]}_{index}".replace(" ", "_")
            dest = out_dir / f"{label}.mkv"
            cmd = [
                ffmpeg, "-hide_banner", "-nostdin", "-y",
                "-ss", f"{offset:.2f}",
                "-i", str(source),
                "-t", str(seconds),
                "-map", "0:v:0", "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                str(dest),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
                print(f"  ! clip extraction failed for {source.name} @ {offset:.0f}s")
                continue
            try:
                clip_info = probe(dest, ffprobe)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! could not probe extracted clip {dest.name}: {exc}")
                continue
            clips.append(
                Clip(path=dest, source_path=str(source), label=label,
                     content_class=content_class, info=clip_info)
            )
            print(f"  + {label}: {clip_info.resolution_tier} {clip_info.v_codec} "
                  f"{dest.stat().st_size / 1e6:.0f}MB")
        accepted += 1
    return clips


# ------------------------------------------------------------------ vmaf


def score_vmaf(
    distorted: Path, reference: Path, *, ffmpeg: str, work_dir: Path,
    reference_height: int | None = None, threads: int = 2, timeout: int = 3600,
) -> tuple[float | None, float | None, float | None, str | None]:
    """Whole-file score, as a plain tuple. See app/work/vmaf.py for the work.

    Production verification and the benchmark must ask this question in exactly
    the same way, so the implementation lives with the worker and this is a
    thin adapter.
    """
    result = vmaf.score(
        distorted, reference, ffmpeg=ffmpeg, work_dir=work_dir,
        reference_height=reference_height, threads=threads, timeout=timeout,
    )
    return result.mean, result.min, result.p1, result.error


def measure(
    clip: Clip, encoder: str, quality: float, *, ffmpeg: str, work_dir: Path,
    vaapi_device: str, target_height: int | None = None, preset: str = "slow",
    threads: int | None = None, run_vmaf: bool = True, timeout: int = 7200,
    hw_decode: bool = False, ffmpeg_vmaf: str | None = None,
) -> Measurement:
    spec = VideoSpec(
        encoder=encoder, quality=quality, target_height=target_height,
        preset=preset, hw_decode=hw_decode,
    )
    decode_tag = "hwdec" if hw_decode else "swdec"
    dest = work_dir / f"{clip.label}_{encoder}_{int(quality)}_{decode_tag}.mkv"
    cmd = build_encode_command(
        ffmpeg=ffmpeg, source=clip.path, dest=dest, spec=spec,
        vaapi_device=vaapi_device, threads=threads,
    )

    in_bytes = clip.path.stat().st_size
    measurement = Measurement(
        clip=clip.label, encoder=encoder, quality=quality,
        src_width=clip.info.v_width, src_height=clip.info.v_height,
        out_width=None, out_height=target_height or clip.info.v_height,
        frames=None, wall_seconds=0.0, cpu_seconds=0.0, fps=None,
        in_bytes=in_bytes, out_bytes=0, size_ratio=None,
        src_fps=clip.info.v_fps,
    )

    try:
        result, wall, cpu = _run_timed(cmd, timeout)
    except subprocess.SubprocessError as exc:
        measurement.ok = False
        measurement.error = f"encode failed: {exc}"
        return measurement

    measurement.wall_seconds = wall
    measurement.cpu_seconds = cpu

    if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        measurement.ok = False
        tail = " | ".join((result.stderr or "").strip().splitlines()[-3:])
        measurement.error = f"encode failed ({result.returncode}): {tail[:300]}"
        dest.unlink(missing_ok=True)
        return measurement

    measurement.out_bytes = dest.stat().st_size
    measurement.size_ratio = measurement.out_bytes / in_bytes if in_bytes else None

    frames = _frame_count(result.stderr or "")
    measurement.frames = frames
    if frames and wall > 0:
        measurement.fps = frames / wall

    if run_vmaf:
        mean, minimum, p1, error = score_vmaf(
            dest, clip.path, ffmpeg=ffmpeg_vmaf or ffmpeg, work_dir=work_dir,
            reference_height=clip.info.v_height if target_height else None,
        )
        measurement.vmaf_mean, measurement.vmaf_min, measurement.vmaf_p1 = mean, minimum, p1
        if error:
            measurement.error = error

    dest.unlink(missing_ok=True)
    return measurement


def _frame_count(stderr: str) -> int | None:
    """Pull the final frame count out of ffmpeg's progress line."""
    last = None
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped.startswith("frame="):
            last = stripped
    if not last:
        return None
    try:
        return int(last.split("frame=")[1].split()[0])
    except (IndexError, ValueError):
        return None


def persist(db: Database, run_id: str, measurements: list[Measurement]) -> None:
    now = time.time()
    for m in measurements:
        db.execute(
            """
            INSERT INTO bench_result (
                run_id, clip, encoder, quality_key, quality_value,
                src_width, src_height, out_width, out_height, frames,
                wall_seconds, cpu_seconds, fps, in_bytes, out_bytes, size_ratio,
                vmaf_mean, vmaf_min, vmaf_p1, ok, error, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id, m.clip, m.encoder, QUALITY_FLAG.get(m.encoder, "q"), m.quality,
                m.src_width, m.src_height, m.out_width, m.out_height, m.frames,
                m.wall_seconds, m.cpu_seconds, m.fps, m.in_bytes, m.out_bytes,
                m.size_ratio, m.vmaf_mean, m.vmaf_min, m.vmaf_p1,
                1 if m.ok else 0, m.error, now,
            ),
        )


def choose_decode_mode(
    clip: Clip, encoder: str, *, ffmpeg: str, work_dir: Path, vaapi_device: str,
    quality: float = 26, target_height: int | None = None,
) -> tuple[bool, str]:
    """Decide whether to decode in hardware for this encoder.

    A full hardware pipeline keeps frames on the GPU and avoids a round trip
    through system RAM, which is worth a lot on a memory-starved Celeron. But it
    fails outright on codecs the fixed-function decoder does not handle, and it
    is not always faster once scaling is involved. So we measure both once,
    rather than assuming, and run the rest of the sweep with the winner.

    Returns (use_hw_decode, human-readable reason).
    """
    if not encoder.endswith(("_vaapi", "_qsv")):
        return False, "software encoder: hardware decode not applicable"

    results: dict[bool, Measurement] = {}
    for mode in (True, False):
        results[mode] = measure(
            clip, encoder, quality, ffmpeg=ffmpeg, work_dir=work_dir,
            vaapi_device=vaapi_device, target_height=target_height,
            run_vmaf=False, hw_decode=mode, timeout=900,
        )

    hw, sw = results[True], results[False]

    if not hw.ok and not sw.ok:
        return False, "both decode paths failed; falling back to software decode"
    if not hw.ok:
        return False, f"hardware decode failed ({(hw.error or '')[:80]}); using software decode"
    if not sw.ok:
        return True, "software decode failed; using hardware decode"
    if not hw.fps or not sw.fps:
        return True, "hardware decode works; no reliable fps comparison"

    gain = (hw.fps - sw.fps) / sw.fps * 100
    if hw.fps >= sw.fps:
        return True, f"hardware decode {gain:+.0f}% faster ({hw.fps:.1f} vs {sw.fps:.1f} fps)"
    return False, f"software decode {-gain:+.0f}% faster ({sw.fps:.1f} vs {hw.fps:.1f} fps)"
