"""Library traversal.

Phase 0 only needs to find and sample media files. Phase 1 adds the incremental
DB sync on top of `iter_media_files`.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

DEFAULT_MEDIA_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv",
    ".ts", ".m2ts", ".mpg", ".mpeg", ".vob", ".divx",
}

# Directories that hold working files, extras or Synology bookkeeping rather
# than library content.
SKIP_DIRS = {
    "@eaDir", "#recycle", ".@__thumb", "@tmp", ".git",
    "featurettes", "extras", "behind the scenes", "trailers",
    "deleted scenes", "interviews", "scenes", "shorts", "other",
    "sample", "samples",
}

# Sample files are tiny throwaway copies; never encode or count them.
SAMPLE_MARKERS = ("-sample", ".sample", "sample-", "_sample")
MIN_USEFUL_BYTES = 50 * 1024 * 1024


@dataclass
class FoundFile:
    path: Path
    library_root: Path
    size_bytes: int
    mtime: float


def _is_sample(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in SAMPLE_MARKERS)


def iter_media_files(
    roots: list[Path] | list[str],
    *,
    extensions: set[str] | None = None,
    min_bytes: int = MIN_USEFUL_BYTES,
) -> Iterator[FoundFile]:
    extensions = extensions or DEFAULT_MEDIA_EXTENSIONS

    for raw_root in roots:
        root = Path(raw_root)
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Prune in place so os.walk never descends into them.
            dirnames[:] = [d for d in dirnames if d.lower() not in
                           {s.lower() for s in SKIP_DIRS}]
            for filename in filenames:
                if Path(filename).suffix.lower() not in extensions:
                    continue
                if _is_sample(filename):
                    continue
                full = Path(dirpath) / filename
                try:
                    stat = full.stat()
                except OSError:
                    continue
                if stat.st_size < min_bytes:
                    continue
                yield FoundFile(
                    path=full, library_root=root,
                    size_bytes=stat.st_size, mtime=stat.st_mtime,
                )


def sample_files(
    roots: list[Path] | list[str],
    *,
    count: int,
    seed: int = 1019,
    extensions: set[str] | None = None,
    scan_limit: int = 20000,
) -> list[FoundFile]:
    """Reservoir-sample the library.

    Random rather than "the first N" so the benchmark sees a real mix of
    content -- animation, grainy film and flat sitcom footage compress very
    differently, and calibrating on one of them alone gives a ladder that is
    wrong for the other two.
    """
    rng = random.Random(seed)
    reservoir: list[FoundFile] = []

    for index, found in enumerate(iter_media_files(roots, extensions=extensions)):
        if index >= scan_limit:
            break
        if len(reservoir) < count:
            reservoir.append(found)
        else:
            slot = rng.randint(0, index)
            if slot < count:
                reservoir[slot] = found
    return reservoir
