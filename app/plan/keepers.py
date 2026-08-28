"""The hand-picked list that gets software x265 instead of the iGPU.

Hardware HEVC on an Apollo Lake iGPU is the right default for fifteen thousand
files: it runs at about 35 fps on 1080p, which is roughly half an hour for a
45-minute episode. x265 at `slow` on the same box does 3-6 fps -- six to ten
hours for the same episode. That is never worth spending on a queue, and it is
sometimes worth spending on a film you actually care about, where the software
encoder holds grain and dark detail the fixed-function block smears.

So it is a list, written by hand, and nothing puts a file on it automatically.
One path per line; `#` starts a comment. A line may be:

    /media/movies/Blade Runner 2049 (2017)/Blade Runner 2049.mkv   an exact file
    /media/movies/Studio Ghibli/                                   everything under it
    /media/movies/**/*Criterion*.mkv                               a glob
    Blade Runner 2049.mkv                                          just the file name

Matching is by path, because that is the only identifier that exists before the
planner has resolved a title. Case is ignored on Windows and respected on
Linux, matching how the filesystem underneath actually behaves.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

GLOB_CHARS = set("*?[")

# Whether the filesystem underneath ignores case. Asked of the platform rather
# than hard-coded, because this is written on Windows and runs on Linux. Note
# that `os.path.normcase` is not usable directly: on Windows it also rewrites
# every forward slash into a backslash, and paths here are kept in one shape.
FOLD_CASE = os.path.normcase("A") != "A"


def _normalise(text: str) -> str:
    value = str(text).strip().replace("\\", "/")
    return value.lower() if FOLD_CASE else value


@dataclass
class Keepers:
    """The parsed list. Empty is the normal state and costs nothing to ask."""

    patterns: list[str] = field(default_factory=list)
    source: str = ""
    error: str = ""

    def __bool__(self) -> bool:
        return bool(self.patterns)

    def matches(self, path: str | Path) -> bool:
        candidate = _normalise(path)
        name = candidate.rsplit("/", 1)[-1]
        for pattern in self.patterns:
            if GLOB_CHARS & set(pattern):
                if fnmatch.fnmatchcase(candidate, pattern) or fnmatch.fnmatchcase(
                    name, pattern
                ):
                    return True
            elif pattern.endswith("/"):
                if candidate.startswith(pattern):
                    return True
            elif candidate == pattern or candidate.startswith(pattern + "/"):
                return True
            elif name == pattern:
                return True
        return False


def parse(text: str, *, source: str = "") -> Keepers:
    patterns = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            patterns.append(_normalise(line))
    return Keepers(patterns=patterns, source=source)


def load(config) -> Keepers:
    """Read `encoder.keepers_file`, if one is configured.

    A configured file that is missing is reported rather than ignored: the
    difference between "no keepers" and "the keepers list did not load" is the
    difference between a night of hardware encoding you meant and one you
    didn't.
    """
    path = config.encoder.keepers_file
    if not path:
        return Keepers()
    path = Path(path)
    if not path.exists():
        return Keepers(
            source=str(path),
            error=f"keepers_file {path} is configured but does not exist",
        )
    try:
        return parse(path.read_text(encoding="utf-8"), source=str(path))
    except OSError as exc:
        return Keepers(source=str(path), error=f"could not read {path}: {exc}")
