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

# Calibrate on the mean. The 1st percentile is the better *perceptual* signal in
# principle, but on a 30-second clip it degenerates into "worst single frame",
# where one misaligned or corrupt frame drags it to near zero and takes the
# whole ladder with it. Observed on the first real run: mean 89, p1 4.3.
# So: mean drives the ladder, and the low-end scores are kept as a warning
# signal that an encode had bad frames worth investigating.
PRIMARY_METRIC = "vmaf_mean"
FALLBACK_METRIC = "vmaf_p1"

# An encode that makes the file bigger is never a valid ladder rung. Sources
# that are already efficient (a good x265 encode) will do exactly this at every
# quality setting, and the honest answer there is "do not re-encode this", not
# "pick the least-bad setting".
MAX_USEFUL_SIZE_RATIO = 0.95

# Clips disagreeing by more than this at the chosen setting means the sample was
# not representative of the library, and the ladder is only as good as it. Set to
# match quality.vmaf_fail_margin: once the clips differ by more than the margin
# verification allows, no single setting can satisfy both of them.
WIDE_SPREAD_VMAF = 3.0


@dataclass
class LadderEntry:
    encoder: str
    content_class: str
    resolution: str
    target_vmaf: float
    quality: float
    quality_flag: str
    expected_size_ratio: float
    expected_out_bitrate: int | None
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


def _aggregate(
    points: list[tuple[float, float]], how: str = "mean"
) -> list[tuple[float, float]]:
    """Collapse several clips' measurements into one value per quality setting.

    Every group holds one series per calibration clip, all sharing the same
    quality values. Feeding that straight into an interpolator is meaningless:
    with duplicate x values the "curve" zigzags, and both helpers below then
    silently pick whichever clip happens to sort first -- in practice the
    easiest one. That is how a ladder ends up promising a 14% size ratio at a
    setting where the harder clip measured 39% and missed its VMAF target.

    VMAF aggregates with `min`, so the *hardest* content we sampled governs the
    setting. Picking the mean would put half the library below the target the
    user asked for, and every one of those files then burns hours only to fail
    verification. Size and speed aggregate with `mean`, because those are
    expectations across the library rather than promises about any one file.
    """
    grouped: dict[float, list[float]] = {}
    for quality, value in points:
        grouped.setdefault(quality, []).append(value)

    reducer = min if how == "min" else (lambda vs: sum(vs) / len(vs))
    return sorted((quality, reducer(values)) for quality, values in grouped.items())


def _spread(points: list[tuple[float, float]], quality: float) -> float:
    """How much the clips disagreed around one setting.

    Snaps to the nearest setting we actually measured, because the chosen
    quality is usually interpolated and lands between sweep points. A wide
    spread means the calibration clips were not representative of each other,
    let alone of the library, and that is worth saying out loud.
    """
    if not points:
        return 0.0
    nearest = min({q for q, _ in points}, key=lambda q: abs(q - quality))
    at_quality = [v for q, v in points if q == nearest]
    return max(at_quality) - min(at_quality) if len(at_quality) > 1 else 0.0


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


def _out_bitrate(m: Measurement) -> float | None:
    """Output bits per second at this setting.

    The planner estimates with a bitrate rather than a size ratio, because a
    ratio is tied to how fat the source happened to be while constant-quality
    output is not. Clip duration comes from the encoded frame count over the
    source frame rate -- the clip was stream-copied, so its rate is the
    source's.
    """
    if not (m.out_bytes and m.frames and m.src_fps):
        return None
    duration = m.frames / m.src_fps
    return m.out_bytes * 8 / duration if duration > 0 else None


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
        raw_vmaf = [(m.quality, _metric(m)) for m in members]  # type: ignore[misc]
        raw_size = [(m.quality, m.size_ratio) for m in members if m.size_ratio]

        # One value per setting, or the interpolation below is not interpolating
        # anything -- see _aggregate.
        quality_vmaf = _aggregate(raw_vmaf, "min")
        quality_size = _aggregate(raw_size, "mean")
        quality_fps = _aggregate([(m.quality, m.fps) for m in members if m.fps], "mean")
        quality_bitrate = _aggregate(
            [(m.quality, rate) for m in members
             if (rate := _out_bitrate(m)) is not None],
            "mean",
        )

        if len(quality_vmaf) < 2:
            continue

        # If no tested setting actually shrinks the file, this source class is
        # already more efficient than our encoder can manage. Emitting a rung
        # here would tell production to triple the size of every such file.
        shrinking = [(q, r) for q, r in quality_size if r <= MAX_USEFUL_SIZE_RATIO]
        if quality_size and not shrinking:
            best = min(r for _, r in quality_size)
            entries.append(
                LadderEntry(
                    encoder=encoder, content_class="unusable", resolution=resolution,
                    target_vmaf=0.0, quality=0.0,
                    quality_flag=QUALITY_FLAG.get(encoder, "q"),
                    expected_size_ratio=round(best, 4),
                    expected_out_bitrate=None, expected_fps=None,
                    extrapolated=True, samples=len(members),
                    note=(
                        f"NO USABLE SETTING: the smallest output was {best * 100:.0f}% "
                        "of the source, i.e. re-encoding makes these files bigger. "
                        "The sample was almost certainly already-efficient content "
                        "(HEVC/x265). Benchmark on the H.264 files that policy would "
                        "actually re-encode."
                    ),
                )
            )
            continue

        for content_class, target in targets.items():
            quality, extrapolated = _interpolate(quality_vmaf, target)
            # Hardware encoders take an integer QP. Floor rather than round, so
            # measurement noise can only ever move us toward higher quality --
            # this setting is about to be applied to thousands of files.
            if encoder.endswith(("_vaapi", "_qsv")):
                quality = float(int(quality))
            note = ""
            vmaf_spread = _spread(raw_vmaf, quality)
            if vmaf_spread >= WIDE_SPREAD_VMAF:
                note = (
                    f"the calibration clips disagreed by {vmaf_spread:.0f} VMAF at "
                    f"this setting, so it is tuned to the hardest of them. Benchmark "
                    f"more sources to narrow it."
                )
            if extrapolated:
                achieved = max(v for _, v in quality_vmaf)
                if achieved < target:
                    extra = (
                        f"no tested setting reached VMAF {target}; best was "
                        f"{achieved:.1f}. Using the highest-quality setting tried."
                    )
                else:
                    extra = (
                        "every tested setting beat the target; the true optimum is "
                        "likely coarser. Re-run the sweep with higher QP values."
                    )
                note = f"{note} {extra}".strip() if note else extra
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
                    expected_out_bitrate=int(
                        _expected_at(quality_bitrate, quality)
                    ) if quality_bitrate else None,
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
    resolution: str = "1080p",
) -> dict:
    """How long would a full first pass take, in real calendar terms?

    Deliberately reported in days rather than seconds: the point is to make the
    scale of the job obvious before anyone starts it.

    Projected from the 1080p rungs alone, not from the average of every rung.
    SD encodes several times faster than 1080p, so averaging them together
    produces a headline speed no real file will ever achieve -- and since SD
    holds a small share of the bytes, it is not where the time goes either. The
    honest per-file figure is the one for the content that actually gets
    encoded.
    """
    hw = [
        e for e in entries
        if e.expected_fps
        and e.encoder.endswith(("_vaapi", "_qsv"))
        and e.resolution == resolution
    ]
    if not hw:
        return {
            "error": (
                f"no hardware measurements at {resolution} to project from; "
                f"benchmark some {resolution} sources"
            )
        }

    fps = sum(e.expected_fps for e in hw) / len(hw)  # type: ignore[misc]
    frames_per_file = average_minutes_per_file * 60 * average_fps_of_source
    seconds_per_file = frames_per_file / fps
    total_hours = total_files * seconds_per_file / 3600

    return {
        "measured_encode_fps": round(fps, 1),
        "projected_from": resolution,
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


# ------------------------------------------------------------------ rebuild


def load_measurements(db, run_id: str) -> list[Measurement]:
    """Rebuild Measurement objects from stored bench_result rows.

    Every encode is recorded, so the ladder can be re-derived whenever the
    interpolation changes without spending the hours again. `src_fps` is not
    stored, so `expected_out_bitrate` comes back empty on a rebuild -- the
    planner treats it as optional and falls back to the policy target.
    """
    rows = db.query(
        "SELECT * FROM bench_result WHERE run_id=? ORDER BY id", (run_id,)
    )
    return [
        Measurement(
            clip=row["clip"], encoder=row["encoder"], quality=row["quality_value"],
            src_width=row["src_width"], src_height=row["src_height"],
            out_width=row["out_width"], out_height=row["out_height"],
            frames=row["frames"], wall_seconds=row["wall_seconds"] or 0.0,
            cpu_seconds=row["cpu_seconds"] or 0.0, fps=row["fps"],
            in_bytes=row["in_bytes"] or 0, out_bytes=row["out_bytes"] or 0,
            size_ratio=row["size_ratio"], src_fps=None,
            vmaf_mean=row["vmaf_mean"], vmaf_min=row["vmaf_min"],
            vmaf_p1=row["vmaf_p1"], ok=bool(row["ok"]), error=row["error"],
        )
        for row in rows
    ]


def latest_run_id(db) -> str | None:
    return db.scalar("SELECT run_id FROM bench_result ORDER BY id DESC LIMIT 1")


def main(argv: list[str] | None = None) -> int:
    """Re-derive profiles.yaml from measurements already in the database.

    `python -m bench.ladder` after a benchmark run costs seconds, where
    re-running the benchmark costs a night.
    """
    import argparse
    from pathlib import Path

    from app.config import load_config
    from app.db import Database

    parser = argparse.ArgumentParser(
        prog="bench.ladder",
        description="rebuild the quality ladder from stored benchmark results",
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--run-id", default=None,
                        help="which benchmark run to use (default: the most recent)")
    parser.add_argument("--out", default=None,
                        help="where to write profiles.yaml (default: the config dir)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the ladder without writing profiles.yaml")
    args = parser.parse_args(argv)

    config = load_config(Path(args.config) if args.config else None)
    db = Database(Path(args.db) if args.db else config.db_path)

    run_id = args.run_id or latest_run_id(db)
    if not run_id:
        print("No benchmark results stored. Run `bench` first.")
        return 2

    measurements = load_measurements(db, run_id)
    scored = [m for m in measurements if m.ok and _metric(m) is not None]
    print(f"run {run_id}: {len(measurements)} measurement(s), {len(scored)} scored\n")

    targets = {"movie": config.quality.movie_vmaf, "tv": config.quality.tv_vmaf}
    entries = build_ladder(measurements, targets)
    if not entries:
        print("Nothing usable in that run.")
        return 1

    print(render_text(entries))

    preferred = None
    hardware = [e.encoder for e in entries if e.encoder.endswith(("_vaapi", "_qsv"))]
    if hardware:
        preferred = max(set(hardware), key=hardware.count)

    if args.dry_run:
        print("\nDry run: profiles.yaml not written.")
        return 0

    out = Path(args.out) if args.out else config.profiles_path

    # bench_result does not store which decode path was chosen, so carry the
    # existing file's answer forward rather than silently dropping it --
    # production has to decode the way the benchmark measured or the fps is a
    # fiction.
    decode_modes = {}
    if out.exists():
        from app.plan.profiles import load_ladder

        existing = load_ladder(out)
        if existing:
            decode_modes = existing.hw_decode
            if decode_modes:
                print(f"\ncarrying forward hw_decode from the existing "
                      f"{out.name}: {decode_modes}")

    out.write_text(
        to_profiles_yaml(entries, preferred, decode_modes, run_id=run_id),
        encoding="utf-8",
    )
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
