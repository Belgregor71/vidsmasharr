"""Running libvmaf and reading its answer back.

Lives in `app/` rather than `bench/` because production verification is the
important caller: the benchmark uses this to build a ladder, but the worker
uses it to decide whether an original may be deleted. `bench.runner.score_vmaf`
delegates here so both ask the question exactly the same way.

Scoring needs a different ffmpeg binary from encoding -- jellyfin-ffmpeg has
VAAPI but no libvmaf. See `Config.vmaf_ffmpeg`.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.work.ffmpeg_cmd import build_vmaf_command


@dataclass
class VmafScore:
    mean: float | None = None
    min: float | None = None
    p1: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.mean is not None


def score(
    distorted: Path,
    reference: Path,
    *,
    ffmpeg: str,
    work_dir: Path,
    reference_height: int | None = None,
    threads: int = 2,
    timeout: int = 3600,
    start_s: float | None = None,
    duration_s: float | None = None,
    fps: str | float | None = None,
) -> VmafScore:
    """Score one file, or one segment of it, against its reference."""
    work_dir.mkdir(parents=True, exist_ok=True)
    log_name = f"vmaf_{uuid.uuid4().hex[:8]}.json"
    log_path = work_dir / log_name

    # Bare filename in the filtergraph + cwd on the process: see the note in
    # build_vmaf_command. Absolute paths break filter parsing.
    cmd = build_vmaf_command(
        ffmpeg=ffmpeg, distorted=distorted.resolve(), reference=reference.resolve(),
        log_path=log_name, threads=threads, reference_height=reference_height,
        start_s=start_s, duration_s=duration_s, fps=fps,
    )

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
            cwd=work_dir,
        )
    except subprocess.TimeoutExpired:
        log_path.unlink(missing_ok=True)
        return VmafScore(error=f"vmaf timed out after {timeout}s")
    except (OSError, subprocess.SubprocessError) as exc:
        return VmafScore(error=f"vmaf run failed: {exc}")

    if not log_path.exists():
        tail = " | ".join((result.stderr or "").strip().splitlines()[-3:])
        return VmafScore(error=f"vmaf produced no log: {tail[:300]}")

    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return VmafScore(error=f"vmaf log unreadable: {exc}")
    finally:
        log_path.unlink(missing_ok=True)

    return parse_log(data)


def parse_log(data: dict) -> VmafScore:
    pooled = (data.get("pooled_metrics") or {}).get("vmaf") or {}
    mean = pooled.get("mean")
    minimum = pooled.get("min")

    # The 1st percentile is a better proxy for "will I notice" than the mean:
    # a single bad scene drags perception down more than a good average lifts
    # it. It is only meaningful over a few hundred frames -- on a short sample
    # it collapses toward the single worst frame, which is why the ladder
    # calibrates on the mean and treats this as a warning signal.
    p1 = None
    frames = data.get("frames") or []
    if frames:
        scores = sorted(f.get("metrics", {}).get("vmaf", 0.0) for f in frames)
        index = min(len(scores) - 1, max(0, round(len(scores) * 0.01)))
        p1 = scores[index]
        if mean is None:
            mean = sum(scores) / len(scores)
        if minimum is None:
            minimum = scores[0]

    if mean is None:
        return VmafScore(error="vmaf log had no pooled score and no frames")
    return VmafScore(mean=mean, min=minimum, p1=p1)


def sample_offsets(duration_s: float, count: int, segment_s: float) -> list[float]:
    """Where to take samples from, avoiding credits and black frames.

    Spread evenly across the middle 80% of the runtime. The first and last
    tenth of a file are titles, recaps and credits: flat, easy to encode, and
    not representative of the scenes anyone will complain about.
    """
    if duration_s <= 0 or count <= 0:
        return []

    usable_start = duration_s * 0.10
    usable_span = max(duration_s * 0.80 - segment_s, 0.0)
    if usable_span <= 0:
        # Very short file: one sample from the start is all it can support.
        return [0.0]
    return [
        round(usable_start + usable_span * (index + 0.5) / count, 3)
        for index in range(count)
    ]
