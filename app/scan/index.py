"""Incremental library sync: filesystem -> media_file rows -> ffprobe facts.

Two passes, deliberately separate. The walk is cheap and touches every file;
probing is expensive and only touches files whose facts we do not already have.
On a J3455 with ~15,000 candidates that difference is hours, so a rescan after
adding one episode must not re-probe the library.

Nothing here modifies or deletes media. The scanner only reads.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from app.config import Config
from app.db import Database
from app.scan.probe import PROBE_VERSION, ProbeError, probe
from app.scan.walker import iter_media_files

Progress = Callable[[str], None]

# If a library root that previously held files suddenly walks up nearly empty,
# the far more likely explanation is an unmounted share than a user deleting
# their library. Marking thousands of rows missing on that basis would poison
# every downstream report, so we refuse and say why. This project has already
# been bitten once by two similar-looking media shares: /volume1/Media walks
# perfectly well and contains nothing.
VANISH_GUARD_RATIO = 0.5
VANISH_GUARD_FLOOR = 20


@dataclass
class ScanStats:
    roots: list[str] = field(default_factory=list)
    seen: int = 0
    added: int = 0
    changed: int = 0
    unchanged: int = 0
    marked_missing: int = 0
    restored: int = 0
    probed: int = 0
    probe_failed: int = 0
    skipped_guard: list[str] = field(default_factory=list)
    walk_seconds: float = 0.0
    probe_seconds: float = 0.0

    def summary(self) -> str:
        lines = [
            f"  seen {self.seen}  added {self.added}  changed {self.changed}"
            f"  unchanged {self.unchanged}",
            f"  missing {self.marked_missing}  restored {self.restored}",
            f"  probed {self.probed}  probe failures {self.probe_failed}",
            f"  walk {self.walk_seconds:.1f}s  probe {self.probe_seconds:.1f}s",
        ]
        for warning in self.skipped_guard:
            lines.append(f"  !! {warning}")
        return "\n".join(lines)


# ---------------------------------------------------------------- pass 1: walk


def sync_files(
    db: Database,
    roots: Iterable[Path],
    *,
    extensions: set[str] | None = None,
    min_bytes: int | None = None,
    stats: ScanStats | None = None,
    progress: Progress | None = None,
) -> ScanStats:
    """Reconcile the database against what is on disk right now."""
    stats = stats or ScanStats()
    roots = [Path(r) for r in roots]
    stats.roots = [str(r) for r in roots]
    started = time.time()
    now = time.time()

    walk_kwargs: dict = {}
    if extensions is not None:
        walk_kwargs["extensions"] = extensions
    if min_bytes is not None:
        walk_kwargs["min_bytes"] = min_bytes

    for root in roots:
        known_before = db.scalar(
            "SELECT COUNT(*) FROM media_file WHERE library_root=? AND missing=0",
            (str(root),),
        ) or 0

        seen_paths: set[str] = set()
        db.execute("BEGIN")
        try:
            for index, found in enumerate(iter_media_files([root], **walk_kwargs), start=1):
                path = str(found.path)
                seen_paths.add(path)
                stats.seen += 1

                row = db.one(
                    "SELECT id, size_bytes, mtime, missing FROM media_file WHERE path=?",
                    (path,),
                )
                if row is None:
                    db.execute(
                        "INSERT INTO media_file "
                        "(path, library_root, size_bytes, mtime, first_seen, last_seen, missing) "
                        "VALUES (?,?,?,?,?,?,0)",
                        (path, str(root), found.size_bytes, found.mtime, now, now),
                    )
                    stats.added += 1
                    continue

                # Size or mtime moving means the bytes changed underneath us, so
                # every probed fact about this file is now suspect. Clearing
                # probe_version is what schedules the re-probe in pass 2.
                content_changed = (
                    row["size_bytes"] != found.size_bytes
                    or abs((row["mtime"] or 0) - found.mtime) > 1e-6
                )
                if content_changed:
                    db.execute(
                        "UPDATE media_file SET library_root=?, size_bytes=?, mtime=?, "
                        "last_seen=?, missing=0, probe_version=NULL WHERE id=?",
                        (str(root), found.size_bytes, found.mtime, now, row["id"]),
                    )
                    stats.changed += 1
                else:
                    db.execute(
                        "UPDATE media_file SET library_root=?, last_seen=?, missing=0 "
                        "WHERE id=?",
                        (str(root), now, row["id"]),
                    )
                    stats.unchanged += 1
                if row["missing"]:
                    stats.restored += 1

                if progress and index % 500 == 0:
                    progress(f"  {root}: {index} files...")
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise

        stats.marked_missing += _mark_missing(
            db, root, seen_paths, known_before, now, stats
        )

    stats.walk_seconds = time.time() - started
    return stats


def _mark_missing(
    db: Database,
    root: Path,
    seen_paths: set[str],
    known_before: int,
    now: float,
    stats: ScanStats,
) -> int:
    """Flag rows under `root` this walk did not find -- unless the whole root
    looks like it fell off the machine."""
    found = len(seen_paths)
    if known_before >= VANISH_GUARD_FLOOR and found < known_before * VANISH_GUARD_RATIO:
        stats.skipped_guard.append(
            f"{root}: walk found {found} file(s) but {known_before} were known. "
            f"Refusing to mark anything missing -- this looks like an unmounted "
            f"share, not a deletion. Check the mount, then rescan."
        )
        return 0

    rows = db.query(
        "SELECT id, path FROM media_file WHERE library_root=? AND missing=0",
        (str(root),),
    )
    gone = [r["id"] for r in rows if r["path"] not in seen_paths]
    if not gone:
        return 0

    db.execute("BEGIN")
    try:
        for chunk_start in range(0, len(gone), 500):
            chunk = gone[chunk_start:chunk_start + 500]
            placeholders = ",".join("?" * len(chunk))
            db.execute(
                f"UPDATE media_file SET missing=1, last_seen=? WHERE id IN ({placeholders})",
                (now, *chunk),
            )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return len(gone)


# --------------------------------------------------------------- pass 2: probe


def pending_probe(db: Database, limit: int | None = None) -> list[tuple[int, str, int]]:
    sql = (
        "SELECT id, path, size_bytes FROM media_file "
        "WHERE missing=0 AND (probe_version IS NULL OR probe_version < ?) "
        "ORDER BY size_bytes DESC"
    )
    params: list = [PROBE_VERSION]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [(r["id"], r["path"], r["size_bytes"]) for r in db.query(sql, params)]


def probe_pending(
    db: Database,
    config: Config,
    *,
    limit: int | None = None,
    workers: int = 2,
    stats: ScanStats | None = None,
    progress: Progress | None = None,
) -> ScanStats:
    """ffprobe every file whose facts are missing or stale.

    Largest first: the planner cares most about big files, so a scan
    interrupted halfway is still useful for the part of the library that
    matters. Concurrency stays low by default -- ffprobe is cheap, but this box
    has four threads and Plex wants some of them.
    """
    stats = stats or ScanStats()
    pending = pending_probe(db, limit)
    if not pending:
        return stats
    started = time.time()
    total = len(pending)

    def run(item: tuple[int, str, int]):
        file_id, path, _size = item
        try:
            return file_id, probe(path, ffprobe=config.ffprobe), None
        except (ProbeError, OSError) as exc:
            return file_id, None, str(exc)

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for file_id, info, error in pool.map(run, pending):
            done += 1
            if info is None:
                # Record the failure against the current probe version so a
                # broken file is not retried on every single scan; bumping
                # PROBE_VERSION is what gives it another chance.
                db.execute(
                    "UPDATE media_file SET probe_version=?, probed_at=?, probe_error=? "
                    "WHERE id=?",
                    (PROBE_VERSION, time.time(), error, file_id),
                )
                stats.probe_failed += 1
            else:
                row = info.to_row()
                columns = ", ".join(f"{key}=?" for key in row)
                db.execute(
                    f"UPDATE media_file SET {columns}, probe_version=?, probed_at=?, "
                    f"probe_error=NULL WHERE id=?",
                    (*row.values(), PROBE_VERSION, time.time(), file_id),
                )
                stats.probed += 1
            if progress and done % 100 == 0:
                progress(f"  probed {done}/{total}...")

    stats.probe_seconds = time.time() - started
    return stats


def scan(
    db: Database,
    config: Config,
    *,
    roots: Iterable[Path] | None = None,
    do_probe: bool = True,
    probe_limit: int | None = None,
    workers: int = 2,
    min_bytes: int | None = None,
    progress: Progress | None = None,
) -> ScanStats:
    roots = list(roots or config.libraries)
    stats = ScanStats()
    sync_files(
        db, roots,
        extensions=config.media_extensions,
        min_bytes=min_bytes,
        stats=stats, progress=progress,
    )
    if do_probe:
        probe_pending(
            db, config, limit=probe_limit, workers=workers,
            stats=stats, progress=progress,
        )
    return stats
