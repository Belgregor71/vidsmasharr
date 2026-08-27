"""Phase 0, step 3: turn measurements into the quality ladder.

VAAPI's QP knob does not map to a VMAF score the same way across encoders,
resolutions or content types -- grain-heavy film and flat animation behave
completely differently at the same QP. So rather than hard-coding "QP 24",
we sweep, measure, and interpolate the setting that actually lands on the
target VMAF for each combination. The result is written to profiles.yaml and
becomes what the encoder uses in production.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import yaml

from bench.runner import Measurement

# A single bad scene is more noticeable than a good average, so the ladder is
# built against the 1st-percentile score where we have it.
PRIMARY_METRIC = "vmaf_p1"
FALLBACK_METRIC = "vmaf_mean"


@dataclass
class LadderEntry:
    encoder: str
    content_class: str
    resolution: str
    target_vmaf: float
    quality: float
    quality_flag: str
    expected_size_ratio: float
    expected_fps: float | None
    extrapolated: bool
    samples: int
    note: str = ""


def _metric(m: Measurement) -> float | None:
    value = getattr(m, PRIMARY_METRIC, None)
    if value is None:
        value = getattr(m, FALLBACK_METRIC, None)
    return value


def _resolution_of(m: Measurement) -> str:
    height = m.out_height or m.src_height or 0
    width = m.src_width or 0
    if height >= 1700 or width >= 3000:
        return "2160p"
    if height >= 1000:
        return "1080p"
    if height >= 700:
        return "720p"
    return "sd"


def _interpolate(points: list[tuple[float, float]], target: float) -> tuple[float, bool]:
    """Find the quality value that lands on `target`.

    `points` is [(quality, vmaf)], and vmaf falls as quality (QP/CRF) rises. We
    want the *largest* quality value still meeting the target -- the smallest
    file that is good enough. Returns (quality, extrapolated).
    """
    points = sorted(points)
    passing = [q for q, v in points if v >= target]
    failing = [q for q, v in points if v < target]

    if not passing:
        # Even our highest-quality setting missed. Extrapolate downward and flag it.
        return points[0][0], True
    if not failing:
        # Everything passed; the coarsest setting we tried is the answer, but we
        # may be leaving savings on the table.
        return points[-1][0], True

    best_pass = max(passing)
    worst_fail = min(q for q in failing if q > best_pass) if any(
        q > best_pass for q in failing
    ) else None
    if worst_fail is None:
        return best_pass, False

    v_pass = next(v for q, v in points if q == best_pass)
    v_fail = next(v for q, v in points if q == worst_fail)
    if v_pass == v_fail:
        return best_pass, False

    # Linear interpolation between the bracketing measurements.
    fraction = (v_pass - target) / (v_pass - v_fail)
    quality = best_pass + fraction * (worst_fail - best_pass)
    return round(quality, 1), False


def _expected_at(points: list[tuple[float, float]], quality: float) -> float:
    """Interpolate a secondary series (size ratio, fps) at the chosen quality."""
    points = sorted(points)
    if quality <= points[0][0]:
        return points[0][1]
    if quality >= points[-1][0]:
        return points[-1][1]
    for (q0, v0), (q1, v1) in zip(points, points[1:]):
        if q0 <= quality <= q1:
            if q1 == q0:
                return v0
            fraction = (quality - q0) / (q1 - q0)
            return v0 + fraction * (v1 - v0)
    return points[-1][1]


def build_ladder(
    measurements: list[Measurement],
    targets: dict[str, float],
) -> list[LadderEntry]:
    """targets maps content_class -> target VMAF, e.g. {"movie": 95, "tv": 92}."""
    from app.work.ffmpeg_cmd import QUALITY_FLAG

    usable = [m for m in measurements if m.ok and _metric(m) is not None]
    entries: list[LadderEntry] = []

    groups: dict[tuple[str, str], list[Measurement]] = {}
    for m in usable:
        groups.setdefault((m.encoder, _resolution_of(m)), []).append(m)

    for (encoder, resolution), members in sorted(groups.items()):
        quality_vmaf = [(m.quality, _metric(m)) for m in members]  # type: ignore[misc]
        quality_size = [(m.quality, m.size_ratio) for m in members if m.size_ratio]
        quality_fps = [(m.quality, m.fps) for m in members if m.fps]

        if len(quality_vmaf) < 2:
            continue

        for content_class, target in targets.items():
            quality, extrapolated = _interpolate(quality_vmaf, target)
            # Hardware encoders take an integer QP. Floor rather than round, so
            # measurement noise can only ever move us toward higher quality --
            # this setting is about to be applied to thousands of files.
            if encoder.endswith(("_vaapi", "_qsv")):
                quality = float(int(quality))
            note = ""
            if extrapolated:
                achieved = max(v for _, v in quality_vmaf)
                if achieved < target:
                    note = (
                        f"no tested setting reached VMAF {target}; best was "
                        f"{achieved:.1f}. Using the highest-quality setting tried."
                    )
                else:
                    note = (
                        "every tested setting beat the target; the true optimum is "
                        "likely coarser. Re-run the sweep with higher QP values."
                    )
            entries.append(
                LadderEntry(
                    encoder=encoder,
                    content_class=content_class,
                    resolution=resolution,
                    target_vmaf=target,
                    quality=quality,
                    quality_flag=QUALITY_FLAG.get(encoder, "q"),
                    expected_size_ratio=round(
                        _expected_at(quality_size, quality), 4
                    ) if quality_size else 0.0,
                    expected_fps=round(
                        _expected_at(quality_fps, quality), 2
                    ) if quality_fps else None,
                    extrapolated=extrapolated,
                    samples=len(members),
                    note=note,
                )
            )
    return entries


def to_profiles_yaml(
    entries: list[LadderEntry],
    preferred_encoder: str | None,
    decode_modes: dict[str, bool] | None = None,
    run_id: str | None = None,
) -> str:
    doc = {
        "generated_by": "bench.ladder",
        "run_id": run_id,
        "preferred_encoder": preferred_encoder,
        # Which decode path the benchmark measured as faster/working per encoder.
        # Production must use the same one, or the measured fps is a fiction.
        "hw_decode": decode_modes or {},
        "profiles": [asdict(e) for e in entries],
    }
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def project_timeline(
    entries: list[LadderEntry],
    *,
    total_files: int,
    average_minutes_per_file: float,
    average_fps_of_source: float = 24.0,
    hours_per_day: float = 8.0,
) -> dict:
    """How long would a full first pass take, in real calendar terms?

    Deliberately reported in days rather than seconds: the point is to make the
    scale of the job obvious before anyone starts it.
    """
    hw = [e for e in entries if e.expected_fps and e.encoder.endswith(("_vaapi", "_qsv"))]
    if not hw:
        return {"error": "no hardware measurements to project from"}

    fps = sum(e.expected_fps for e in hw) / len(hw)  # type: ignore[misc]
    frames_per_file = average_minutes_per_file * 60 * average_fps_of_source
    seconds_per_file = frames_per_file / fps
    total_hours = total_files * seconds_per_file / 3600

    return {
        "measured_encode_fps": round(fps, 1),
        "minutes_per_file": round(seconds_per_file / 60, 1),
        "total_files": total_files,
        "total_cpu_hours": round(total_hours, 1),
        "calendar_days_at_night_only": round(total_hours / hours_per_day, 1),
        "calendar_days_24x7": round(total_hours / 24, 1),
    }


def render_text(entries: list[LadderEntry]) -> str:
    if not entries:
        return "No usable ladder entries -- every measurement failed or lacked a VMAF score."

    lines = ["=== quality ladder ===", ""]
    header = f"{'encoder':<12} {'content':<8} {'res':<7} {'target':>6} {'setting':>8} {'size':>7} {'fps':>7}"
    lines.append(header)
    lines.append("-" * len(header))
    for e in sorted(entries, key=lambda x: (x.encoder, x.resolution, x.content_class)):
        size = f"{e.expected_size_ratio * 100:.0f}%" if e.expected_size_ratio else "?"
        fps = f"{e.expected_fps:.1f}" if e.expected_fps else "?"
        flag = f"{e.quality_flag}={e.quality:g}"
        lines.append(
            f"{e.encoder:<12} {e.content_class:<8} {e.resolution:<7} "
            f"{e.target_vmaf:>6.0f} {flag:>8} {size:>7} {fps:>7}"
        )
    warnings = [e for e in entries if e.note]
    if warnings:
        lines.append("")
        lines.append("warnings:")
        seen = set()
        for e in warnings:
            key = (e.encoder, e.resolution, e.note)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  ! {e.encoder} {e.resolution}: {e.note}")
    return "\n".join(lines)
