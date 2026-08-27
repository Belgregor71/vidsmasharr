"""Library census: what is this library actually made of?

Answers the strategic question before any encoding work is built -- how much of
the library is even a candidate? A library that already ships mostly x265 has
little encoding value and the reclaim lives in deduplication instead. That
changes which phases are worth building, so it is worth two minutes to know.

Samples rather than scanning everything: a few hundred ffprobes give a
confident picture of a 7,000-file library, and take minutes rather than hours.
Phase 1's scanner does the exhaustive version.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from collections import Counter
from pathlib import Path

from app.config import load_config
from app.scan.probe import ProbeError, probe
from app.scan.walker import iter_media_files
from bench.runner import calibration_reject_reason

GB = 1024**3


def _size(num_bytes: float) -> str:
    """Human-readable, keeping precision on small values so a test library
    does not report itself as '0 GB'."""
    for unit, scale in (("TB", GB * 1024), ("GB", GB), ("MB", 1024**2)):
        if num_bytes >= scale:
            value = num_bytes / scale
            return f"{value:,.1f} {unit}" if value < 100 else f"{value:,.0f} {unit}"
    return f"{num_bytes / 1024:,.0f} KB"


def _bar(fraction: float, width: int = 28) -> str:
    filled = int(round(fraction * width))
    return "#" * filled + "." * (width - filled)


def run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config) if args.config else None)
    ffprobe = args.ffprobe or config.ffprobe
    libraries = [Path(p) for p in (args.libraries or config.libraries)]

    if not libraries:
        print("No library paths given. Pass --libraries or set them in config.yaml.")
        return 2

    print(f"Indexing {', '.join(str(p) for p in libraries)} ...")
    everything = list(iter_media_files(libraries))
    if not everything:
        print("No media files found.")
        return 2

    total_files = len(everything)
    total_bytes = sum(f.size_bytes for f in everything)
    print(f"  {total_files:,} files, {_size(total_bytes)} total\n")

    rng = random.Random(args.seed)
    sample = everything if total_files <= args.sample else rng.sample(everything, args.sample)
    print(f"Probing a random sample of {len(sample):,} ...")

    codecs: Counter[str] = Counter()
    codec_bytes: Counter[str] = Counter()
    resolutions: Counter[str] = Counter()
    hdr: Counter[str] = Counter()
    reject_reasons: Counter[str] = Counter()

    candidate_sizes: list[int] = []
    candidate_count = 0
    candidate_bytes = 0
    sampled_bytes = 0
    failed = 0

    for index, found in enumerate(sample, start=1):
        if index % 25 == 0 or index == len(sample):
            print(f"  {index}/{len(sample)}", end="\r", flush=True)
        try:
            info = probe(found.path, ffprobe)
        except ProbeError:
            failed += 1
            continue

        sampled_bytes += found.size_bytes
        codec = info.v_codec or "unknown"
        codecs[codec] += 1
        codec_bytes[codec] += found.size_bytes
        resolutions[info.resolution_tier] += 1
        hdr[info.hdr_type] += 1

        reason = calibration_reject_reason(info)
        if reason is None:
            candidate_count += 1
            candidate_bytes += found.size_bytes
            candidate_sizes.append(found.size_bytes)
        else:
            # Collapse to a short category for counting.
            key = ("already efficient codec" if "already" in reason and "bigger" in reason
                   else "protected (HDR/DV)" if "protected" in reason
                   else "already low bitrate" if "below the" in reason
                   else "unreadable")
            reject_reasons[key] += 1

    print(" " * 30, end="\r")
    scanned = sum(codecs.values())
    if not scanned:
        print("Every probe failed -- check the ffprobe path.")
        return 2

    def section(title: str) -> None:
        print(f"\n{title}")
        print("-" * len(title))

    section("Video codec (by file count, and share of bytes)")
    for codec, count in codecs.most_common():
        share = count / scanned
        byte_share = codec_bytes[codec] / sampled_bytes if sampled_bytes else 0
        print(f"  {codec:<10} {_bar(share)} {share * 100:5.1f}%  "
              f"({byte_share * 100:4.1f}% of bytes)")

    section("Resolution")
    for tier, count in resolutions.most_common():
        share = count / scanned
        print(f"  {tier:<10} {_bar(share)} {share * 100:5.1f}%")

    section("Dynamic range")
    for kind, count in hdr.most_common():
        share = count / scanned
        marker = "" if kind == "sdr" else "  <- protected, never re-encoded"
        print(f"  {kind:<14} {_bar(share)} {share * 100:5.1f}%{marker}")

    # ---- the actual question ------------------------------------------------
    candidate_share = candidate_count / scanned
    candidate_byte_share = candidate_bytes / sampled_bytes if sampled_bytes else 0
    projected_files = candidate_share * total_files
    projected_bytes = candidate_byte_share * total_bytes

    section("Is encoding worth it?")
    print(f"  Encode candidates : {candidate_count}/{scanned} sampled "
          f"({candidate_share * 100:.0f}% of files, "
          f"{candidate_byte_share * 100:.0f}% of bytes)")
    if reject_reasons:
        print("  Rejected because  :")
        for reason, count in reject_reasons.most_common():
            print(f"      {count:>4}  {reason}")
    if failed:
        print(f"  Unreadable        : {failed}")

    print(f"\n  Extrapolated to the whole library:")
    print(f"      ~{projected_files:,.0f} files worth encoding")
    print(f"      ~{_size(projected_bytes)} of candidate material")
    for pct in (40, 50, 60):
        print(f"      at {pct}% reduction -> "
              f"~{_size(projected_bytes * pct / 100)} reclaimed")

    # ---- where the reclaim actually concentrates ---------------------------
    # File size is wildly uneven: a 1080p feature holds as many bytes as fifty
    # SD episodes, but costs nothing like fifty times the CPU to encode. So the
    # order the queue runs in decides whether the first months reclaim most of
    # the available space or a rounding error of it.
    if len(candidate_sizes) >= 20:
        section("Where the reclaim concentrates")
        ordered = sorted(candidate_sizes, reverse=True)
        total_candidate = sum(ordered)
        print("  Encoding only the largest candidates, biggest first:\n")
        print(f"  {'share of files':>16}   {'of the reclaim':>14}   "
              f"{'files':>7}   {'reclaimed @50%':>15}")
        for share in (0.10, 0.20, 0.30, 0.50, 1.00):
            cut = max(1, int(len(ordered) * share))
            got = sum(ordered[:cut]) / total_candidate
            files = projected_files * share
            reclaimed = projected_bytes * got * 0.50
            print(f"  {share * 100:>14.0f}%   {got * 100:>13.0f}%   "
                  f"{files:>7,.0f}   {_size(reclaimed):>15}")
        top20 = sum(ordered[:max(1, int(len(ordered) * 0.20))]) / total_candidate
        print(f"\n  The largest 20% of candidates hold {top20 * 100:.0f}% of "
              f"the available reclaim.")
        print("  Ordering the queue by size is worth more than any encoder tuning.")

    section("Read this as")
    if candidate_share < 0.15:
        print("  This library is already efficient. Encoding it would cost months of\n"
              "  CPU for little return -- the reclaim is in duplicates and redundant\n"
              "  audio tracks. Prioritise the dedupe report over the encode pipeline.")
    elif candidate_share < 0.45:
        print("  A meaningful minority is worth encoding. Prioritisation matters more\n"
              "  than throughput: rank by GB-saved-per-CPU-hour and let dedupe run\n"
              "  first, since it costs almost nothing.")
    else:
        print("  Most of this library is H.264 and genuinely worth re-encoding. The\n"
              "  constraint is CPU time, not opportunity -- expect the queue to be the\n"
              "  bottleneck for a long time, so ordering it well is the whole game.")

    print("\n  Sampled, not exhaustive. Phase 1's scanner produces exact figures.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench.census", description="What is this media library made of?"
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--libraries", nargs="*", default=None)
    parser.add_argument("--ffprobe", default=os.environ.get("VIDSMASHARR_FFPROBE"))
    parser.add_argument("--sample", type=int, default=300,
                        help="how many files to probe (default 300)")
    parser.add_argument("--seed", type=int, default=1019)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        return run(build_parser().parse_args(argv))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
