"""Putting the finished file into the library, and removing the original.

This is the only module in the project that deletes anything. It is written so
that at no point does neither file exist:

1. copy the output alongside the original as a hidden temp file
2. flush it to disk and check the size landed
3. `os.replace` the temp onto the final path -- atomic within a directory
4. only then remove the original, and only if the path actually changed

Step 3 is why the temp file is written next to the target rather than moved in
from `/scratch`: rename is only atomic within one filesystem, and scratch and
the media share are separate mounts inside the container even when they sit on
the same volume outside it.

Step 4 is the ugly case. A `.avi` source becomes a `.mkv` output, so the final
path is not the original path and there is nothing to atomically replace. The
order is still safe -- the new file is complete and fsynced before the old one
goes -- but there is a moment where both exist. That is the correct direction
for the window to point.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

TEMP_SUFFIX = ".vidsmasharr-tmp"

# Output is always Matroska: it is the only container that holds everything we
# might keep (PGS subtitles, chapters, multiple audio layouts) and Plex is
# entirely happy with it.
OUTPUT_SUFFIX = ".mkv"


@dataclass
class InstallResult:
    ok: bool
    final_path: Path | None = None
    original_deleted: bool = False
    error: str | None = None


def target_path(original: Path) -> Path:
    return original.with_suffix(OUTPUT_SUFFIX)


def free_bytes(path: Path) -> int:
    """Free space on the filesystem holding `path`, or its nearest parent."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def install(
    output: Path,
    original: Path,
    *,
    delete_original: bool,
    dry_run: bool = False,
) -> InstallResult:
    """Move a verified output into the library.

    With `delete_original` false nothing is installed at all. The output stays
    in scratch for you to copy onto a TV and look at, and the library is not
    touched -- installing beside the original would leave Plex seeing two
    copies of everything, which is worse than doing nothing.
    """
    if not delete_original:
        return InstallResult(
            ok=True, final_path=output, original_deleted=False,
            error=None,
        )

    final = target_path(original)
    temp = final.with_name(final.name + TEMP_SUFFIX)

    if dry_run:
        return InstallResult(ok=True, final_path=final, original_deleted=False)

    try:
        expected = output.stat().st_size
        if free_bytes(final.parent) < expected:
            return InstallResult(
                ok=False,
                error=f"not enough free space beside {final.parent} for the output",
            )

        with open(output, "rb") as src, open(temp, "wb") as dst:
            shutil.copyfileobj(src, dst, length=4 * 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())

        landed = temp.stat().st_size
        if landed != expected:
            temp.unlink(missing_ok=True)
            return InstallResult(
                ok=False,
                error=(
                    f"copy landed at {landed} bytes, expected {expected} -- "
                    f"refusing to install a short file"
                ),
            )

        os.replace(temp, final)

        deleted = False
        if final != original and original.exists():
            original.unlink()
            deleted = True
        elif final == original:
            # The replace consumed the original in place.
            deleted = True

        output.unlink(missing_ok=True)
        return InstallResult(ok=True, final_path=final, original_deleted=deleted)

    except OSError as exc:
        # Leave the original alone and clean up whatever we half-wrote.
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return InstallResult(ok=False, error=f"install failed: {exc}")


def quarantine(output: Path, scratch_dir: Path, reason: str) -> Path | None:
    """Keep a failed output where it can be looked at, with the reason beside it.

    Deleting the evidence of a verification failure would make the failure
    impossible to diagnose, and these are the encodes most worth understanding.
    """
    if not output.exists():
        return None

    quarantine_dir = scratch_dir / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    destination = quarantine_dir / output.name

    counter = 1
    while destination.exists():
        destination = quarantine_dir / f"{output.stem}.{counter}{output.suffix}"
        counter += 1

    try:
        shutil.move(str(output), str(destination))
        destination.with_suffix(destination.suffix + ".txt").write_text(
            reason, encoding="utf-8"
        )
        return destination
    except OSError:
        return None
