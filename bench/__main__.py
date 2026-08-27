"""Phase 0 entry point:  python -m bench [--libraries ...]

Runs the whole calibration in order:
  1. capability   -- what encoders actually work on this box
  2. clips        -- cut representative samples from the real library
  3. matrix       -- sweep encoder x quality, measuring speed, size and VMAF
  4. ladder       -- interpolate the settings that hit the VMAF targets
  5. projection   -- how long a full library pass would really take

Writes profiles.yaml, which the encoder uses in production. Nothing in this
module modifies the media library: clips are stream-copied out to a work dir
and everything happens there.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

from app.config import load_config
from app.db import Database
from app.scan.walker import diagnose_roots, sample_files
from bench import capability, ladder as ladder_mod, runner

# Sweep points. Wide enough to bracket the VMAF targets from both sides, coarse
# enough to finish in an evening on a Celeron.
QP_SWEEP = [20, 23, 26, 29, 32]
CRF_SWEEP = [19, 22, 25, 28]


def _print_header(text: str) -> None:
    print()
    print("=" * 72)
    print(text)
    print("=" * 72)


def run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config) if args.config else None)
    ffmpeg = args.ffmpeg or config.ffmpeg
    ffprobe = args.ffprobe or config.ffprobe
    ffmpeg_vmaf = (args.ffmpeg_vmaf or os.environ.get("VIDSMASHARR_FFMPEG_VMAF")
                   or config.ffmpeg_vmaf or ffmpeg)

    work_dir = Path(args.work_dir)
    clips_dir = work_dir / "clips"
    work_dir.mkdir(parents=True, exist_ok=True)

    run_id = uuid.uuid4().hex[:12]
    started = time.time()

    # ---------------------------------------------------------- 1. capability
    _print_header("STEP 1/5  capability detection")
    caps = capability.detect(ffmpeg, ffprobe, args.device, ffmpeg_vmaf)
    print(capability.render_text(caps))

    device = args.device or (caps.render_nodes[0] if caps.render_nodes
                             else "/dev/dri/renderD128")

    working = [c.encoder for c in caps.encoders if c.works]
    if not working:
        print("\nFATAL: no working encoder at all. Fix the container before continuing.")
        return 2
    if caps.preferred_hw_encoder is None and not args.allow_software_only:
        print(
            "\nFATAL: no working hardware encoder.\n"
            "Software-only encoding of a 15TB library on a J3455 is not viable "
            "(months of 100% CPU). Fix /dev/dri access first, or re-run with "
            "--allow-software-only if you really want software numbers."
        )
        return 2
    if not caps.has_libvmaf:
        print("\nWARNING: no libvmaf -- the ladder cannot be calibrated, only speed measured.")

    # ---------------------------------------------------------- 2. clips
    _print_header("STEP 2/5  extracting sample clips")
    libraries = [Path(p) for p in (args.libraries or config.libraries)]
    if not libraries:
        print("No library paths given. Pass --libraries or set them in config.yaml.")
        return 2

    if args.reuse_clips and clips_dir.is_dir() and any(clips_dir.glob("*.mkv")):
        print(f"Reusing existing clips in {clips_dir}")
        from app.scan.probe import probe as probe_file
        clips = []
        for path in sorted(clips_dir.glob("*.mkv")):
            try:
                clips.append(runner.Clip(path=path, source_path=str(path),
                                         label=path.stem,
                                         content_class=args.content_class,
                                         info=probe_file(path, ffprobe)))
            except Exception as exc:  # noqa: BLE001
                print(f"  ! could not probe {path.name}: {exc}")
    else:
        # Oversample: most of a Plex library is already-HEVC or low-bitrate
        # content that policy would skip, and calibrating on those measures a
        # job we will never run. Take a wide sample and let the filter choose.
        pool = args.sources * args.oversample
        print(f"Sampling up to {pool} files to find {args.sources} calibration "
              f"candidate(s) in: {', '.join(str(p) for p in libraries)}")
        sources = sample_files(libraries, count=pool, seed=args.seed)
        if not sources:
            print("\nNo media files found. Here is what those paths actually are:\n")
            print(diagnose_roots(libraries))
            print(
                "\nRemember these are paths as the CONTAINER sees them: /media is "
                "whatever the compose file mounts there, not a host path."
            )
            return 2
        clips = runner.extract_clips(
            [f.path for f in sources], clips_dir,
            ffmpeg=ffmpeg, ffprobe=ffprobe,
            per_source=args.clips_per_source, seconds=args.clip_seconds,
            content_class=args.content_class, max_sources=args.sources,
        )

    if not clips:
        print(
            "\nNo usable calibration clips. Every sampled file was rejected for a "
            "reason listed above -- typically already HEVC/x265, HDR (protected), "
            "or already below the bitrate floor.\n\n"
            "That is informative in itself: if most of this library is already "
            "efficient, there is little encoding work worth doing here and the "
            "value is in deduplication instead.\n\n"
            "Try --oversample 40 to search harder, or point --libraries at a "
            "library with more H.264 content."
        )
        return 2
    print(f"\n{len(clips)} clip(s) ready in {clips_dir}")

    # ------------------------------------------------- 2b. decode-mode probe
    # A full hardware pipeline avoids a round trip through system RAM, which
    # matters on a memory-starved Celeron -- but it fails on codecs the
    # fixed-function decoder cannot handle. Measure once, then sweep with the
    # winner, so the benchmark runs the same command shape production will.
    decode_modes: dict[str, bool] = {}
    hw_encoders = [e for e in working if e.endswith(("_vaapi", "_qsv"))]
    if hw_encoders and not args.dry_run:
        _print_header("STEP 2b   decode path selection")
        probe_clip = clips[0]
        probe_height = 1080 if (probe_clip.info.v_height or 0) >= 1700 else None
        for encoder in hw_encoders:
            use_hw, reason = runner.choose_decode_mode(
                probe_clip, encoder, ffmpeg=ffmpeg, work_dir=work_dir,
                vaapi_device=device, target_height=probe_height,
            )
            decode_modes[encoder] = use_hw
            print(f"  {encoder:<12} {'hardware' if use_hw else 'software'} decode  -- {reason}")

    # ---------------------------------------------------------- 3. matrix
    _print_header("STEP 3/5  encode matrix")

    jobs: list[tuple[runner.Clip, str, float, int | None]] = []
    for clip in clips:
        is_4k = (clip.info.v_height or 0) >= 1700
        for encoder in working:
            if encoder in ("libx264",) and not args.include_h264:
                continue
            if encoder == "h264_vaapi" and not args.include_h264:
                continue
            sweep = CRF_SWEEP if encoder.startswith("lib") else QP_SWEEP
            if encoder.startswith("lib") and not args.include_software:
                continue
            for quality in sweep:
                # 4K SDR is downscaled to 1080p by policy, so benchmark it that way.
                target_height = 1080 if is_4k else None
                jobs.append((clip, encoder, quality, target_height))

    print(f"{len(jobs)} encode(s) queued. VMAF {'on' if caps.has_libvmaf else 'OFF'}.")
    if args.dry_run:
        for clip, encoder, quality, height in jobs:
            scale = f" -> {height}p" if height else ""
            print(f"  would run: {clip.label} {encoder} q={quality}{scale}")
        return 0

    measurements: list[runner.Measurement] = []
    for index, (clip, encoder, quality, target_height) in enumerate(jobs, start=1):
        scale = f" -> {target_height}p" if target_height else ""
        print(f"[{index}/{len(jobs)}] {clip.label} {encoder} q={quality}{scale} ... ",
              end="", flush=True)
        m = runner.measure(
            clip, encoder, quality, ffmpeg=ffmpeg, work_dir=work_dir,
            vaapi_device=device, target_height=target_height,
            preset=args.x265_preset,
            threads=args.threads if encoder.startswith("lib") else None,
            run_vmaf=caps.has_libvmaf,
            hw_decode=decode_modes.get(encoder, False),
            ffmpeg_vmaf=ffmpeg_vmaf,
        )
        measurements.append(m)
        if not m.ok:
            print(f"FAILED: {m.error}")
            continue
        parts = [f"{m.fps:.1f}fps" if m.fps else "?fps",
                 f"{m.size_ratio * 100:.0f}% size" if m.size_ratio else "?"]
        if m.vmaf_mean is not None:
            parts.append(f"VMAF {m.vmaf_mean:.1f} (p1 {m.vmaf_p1:.1f})"
                         if m.vmaf_p1 is not None else f"VMAF {m.vmaf_mean:.1f}")
        elif caps.has_libvmaf:
            # The encode succeeded but scoring did not. Without a VMAF score
            # this measurement cannot contribute to the ladder, so say so here
            # rather than letting step 4 report an empty result with no cause.
            parts.append("VMAF FAILED")
        print("  ".join(parts))
        if m.vmaf_mean is None and m.error and caps.has_libvmaf:
            print(f"        -> {m.error[:200]}")

    db = Database(config.db_path if not args.db else Path(args.db))
    runner.persist(db, run_id, measurements)
    print(f"\nStored {len(measurements)} measurement(s) under run_id {run_id}")

    # ---------------------------------------------------------- 4. ladder
    _print_header("STEP 4/5  quality ladder")
    targets = {"movie": config.quality.movie_vmaf, "tv": config.quality.tv_vmaf}
    entries = ladder_mod.build_ladder(measurements, targets)
    print(ladder_mod.render_text(entries))

    if not entries:
        succeeded = [m for m in measurements if m.ok]
        scored = [m for m in succeeded if m.vmaf_mean is not None]
        print()
        if not succeeded:
            print("Cause: every encode failed. See the errors above.")
        elif not scored:
            print(
                f"Cause: {len(succeeded)} encode(s) succeeded but none produced a "
                "VMAF score, so there is nothing to calibrate against. Check the "
                "VMAF errors above -- usually an ffmpeg built without libvmaf, or a "
                "missing VMAF model file.\n"
                "The speed and size numbers above are still valid."
            )
        else:
            print("Cause: fewer than two scored measurements per encoder/resolution "
                  "group -- widen the sweep or add clips.")

    if entries:
        profiles_path = Path(args.profiles_out or config.profiles_path)
        profiles_path.parent.mkdir(parents=True, exist_ok=True)
        profiles_path.write_text(
            ladder_mod.to_profiles_yaml(
                entries, caps.preferred_hw_encoder, decode_modes, run_id
            ),
            encoding="utf-8",
        )
        print(f"\nWrote {profiles_path}")

    # ---------------------------------------------------------- 5. projection
    _print_header("STEP 5/5  library projection")
    projection = ladder_mod.project_timeline(
        entries,
        total_files=args.library_files,
        average_minutes_per_file=args.average_minutes,
        hours_per_day=args.night_hours,
    )
    if "error" in projection:
        print(projection["error"])
    else:
        print(f"measured encode speed   : {projection['measured_encode_fps']} fps")
        print(f"time per file           : {projection['minutes_per_file']} min")
        print(f"assumed library size    : {projection['total_files']} files "
              f"@ {args.average_minutes} min average")
        print(f"total encoding time     : {projection['total_cpu_hours']} hours")
        print(f"  overnight only ({args.night_hours}h/day): "
              f"{projection['calendar_days_at_night_only']} days")
        print(f"  running 24/7           : {projection['calendar_days_24x7']} days")
        print(
            "\nThis is why the planner ranks by GB-saved-per-CPU-hour rather than "
            "working alphabetically."
        )

    report = {
        "run_id": run_id,
        "elapsed_seconds": round(time.time() - started, 1),
        "capabilities": caps.to_dict(),
        "measurements": [m.__dict__ for m in measurements],
        "ladder": [e.__dict__ for e in entries],
        "projection": projection,
    }
    report_path = work_dir / f"bench-report-{run_id}.json"
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nFull report: {report_path}")

    if not args.keep_clips:
        shutil.rmtree(clips_dir, ignore_errors=True)

    _print_header("NEXT: verify direct play")
    print(
        "The numbers above say nothing about whether your TVs will play the output.\n"
        "Before any bulk encoding:\n"
        "  1. Encode 2-3 real files with the ladder settings.\n"
        "  2. Drop them in the Plex library and play one on each TV.\n"
        "  3. In Plex 'Now Playing', confirm each says Direct Play, not Transcode.\n"
        "If either TV transcodes, the CPU cost moves to playback and the whole\n"
        "HEVC plan needs revisiting -- stop here rather than encoding 7,000 files."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bench", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--libraries", nargs="*", default=None,
                        help="library paths to sample from")
    parser.add_argument("--work-dir", default=os.environ.get("VIDSMASHARR_BENCH_DIR", "/scratch/bench"))
    parser.add_argument("--profiles-out", default=None)
    parser.add_argument("--device", default=None, help="VAAPI/QSV render node")
    parser.add_argument("--ffmpeg", default=None)
    parser.add_argument("--ffprobe", default=None)
    parser.add_argument("--ffmpeg-vmaf", default=None,
                        help="ffmpeg binary used for VMAF scoring (needs libvmaf)")

    parser.add_argument("--sources", type=int, default=4,
                        help="how many usable calibration files to end up with")
    parser.add_argument("--oversample", type=int, default=12,
                        help="files examined per wanted source; most get "
                             "rejected as already-efficient")
    parser.add_argument("--clips-per-source", type=int, default=2)
    parser.add_argument("--clip-seconds", type=int, default=30)
    parser.add_argument("--content-class", default="tv", choices=["tv", "movie"])
    parser.add_argument("--seed", type=int, default=1019)
    parser.add_argument("--reuse-clips", action="store_true")
    parser.add_argument("--keep-clips", action="store_true")

    parser.add_argument("--include-software", action="store_true",
                        help="also sweep libx265 (slow: hours, not minutes)")
    parser.add_argument("--include-h264", action="store_true")
    parser.add_argument("--x265-preset", default="slow")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--allow-software-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="list the encode matrix without running it")

    parser.add_argument("--library-files", type=int, default=6000,
                        help="file count used for the timeline projection")
    parser.add_argument("--average-minutes", type=float, default=45.0)
    parser.add_argument("--night-hours", type=float, default=8.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
