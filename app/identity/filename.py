"""Identity of last resort: work out what a file is from its name and path.

Plex and Sonarr/Radarr are authoritative and are tried first. This exists for
the leftovers -- files the *arrs never imported, loose rips, and anything Plex
has not scanned yet -- because a file with no identity can never be grouped for
duplicate detection, and an unmatched duplicate is a duplicate that keeps
wasting space.

Everything here reports a confidence below 1.0. Downstream code must not treat
a filename guess as equal to a database match: the duplicate report deletes
nothing, but it does tell a human that two files are the same thing, and being
wrong about that wastes their time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Release-scene noise. Once one of these appears in a name, everything after it
# is metadata about the encode rather than part of the title.
JUNK_TOKENS = {
    "1080p", "1080i", "720p", "480p", "576p", "2160p", "4k", "uhd",
    "bluray", "blu-ray", "brrip", "bdrip", "bdremux", "remux", "webrip",
    "web-dl", "webdl", "web", "hdtv", "dvdrip", "dvd", "hdrip", "pdtv",
    "x264", "x265", "h264", "h265", "hevc", "avc", "xvid", "divx", "av1",
    "aac", "ac3", "eac3", "dts", "dtshd", "truehd", "atmos", "flac", "mp3",
    "5", "1", "7", "2",  # stray channel-count fragments left by "5.1" splits
    "hdr", "hdr10", "dv", "dolbyvision", "sdr", "10bit", "8bit", "hi10p",
    "proper", "repack", "internal", "limited", "extended", "unrated",
    "remastered", "directors", "cut", "theatrical", "imax", "multi", "dual",
    "subbed", "dubbed", "complete", "season", "amzn", "nf", "dsnp", "hmax",
    "atvp", "pcok", "stan", "iplayer",
}

# Multi-episode forms collapse to their first episode: S01E01E02 and S01E01-E02
# are one file covering two episodes, and the group it belongs in is the first.
_TV_PATTERNS = [
    # S01E02, s01e02e03, S01E02-E03
    re.compile(r"(?P<show>.*?)[._\s-]*s(?P<season>\d{1,2})[._\s-]*e(?P<episode>\d{1,3})", re.I),
    # 1x02
    re.compile(r"(?P<show>.*?)[._\s-]+(?P<season>\d{1,2})x(?P<episode>\d{1,3})(?!\d)", re.I),
    # Season 1 ... Episode 2  (usually assembled from directory + filename)
    re.compile(
        r"(?P<show>.*?)[._\s-]*season[._\s-]*(?P<season>\d{1,2})"
        r".*?episode[._\s-]*(?P<episode>\d{1,3})",
        re.I | re.S,
    ),
]

# Daily shows and some documentaries are dated rather than numbered. We can
# identify the show but not the episode number, which is enough to group by
# air date but not enough to claim a season/episode.
_DATED = re.compile(r"(?P<show>.*?)[._\s-]+(?P<y>\d{4})[._\s-](?P<m>\d{2})[._\s-](?P<d>\d{2})")

_YEAR = re.compile(r"[\(\[\s._-](?P<year>19\d{2}|20\d{2})[\)\]\s._-]?")
_TRAILING_GROUP = re.compile(r"-[A-Za-z0-9]{2,20}$")


@dataclass
class ParsedName:
    kind: str  # "episode" | "movie"
    name: str | None  # show name for episodes, movie title for movies
    year: int | None
    season: int | None
    episode: int | None
    air_date: str | None
    confidence: float

    @property
    def is_usable(self) -> bool:
        return bool(self.name) and self.confidence > 0.0


def clean_title(raw: str) -> str:
    """Turn 'The.Show.Name.2019.1080p.WEB-DL.x265-GROUP' into 'The Show Name'."""
    text = raw.replace("_", " ").replace(".", " ")
    # A year in brackets is unambiguously a qualifier rather than part of the
    # name. Noting that before stripping brackets is what keeps "Blade Runner
    # 2049 (2017)" from losing the 2049, which is genuinely part of the title.
    has_bracketed_year = bool(re.search(r"[\(\[]\s*(19\d{2}|20\d{2})\s*[\)\]]", text))
    text = re.sub(r"[\[\({].*?[\]\)}]", " ", text)  # bracketed tags anywhere
    text = _TRAILING_GROUP.sub("", text.strip())

    words: list[str] = []
    for word in re.split(r"[\s\-]+", text):
        bare = word.strip("-_ ").lower()
        if not bare:
            continue
        if bare in JUNK_TOKENS:
            break  # everything from here on is encode metadata
        words.append(word.strip("-_ "))

    cleaned = " ".join(words).strip(" -_")
    # A bare trailing year is a qualifier -- but only when the bracketed form
    # did not already supply one, or "Blade Runner 2049" gets truncated.
    if not has_bracketed_year:
        cleaned = re.sub(r"[\s\(\[]*\b(19\d{2}|20\d{2})\b[\s\)\]]*$", "", cleaned).strip()
    return re.sub(r"\s{2,}", " ", cleaned)


def normalise(title: str) -> str:
    """Aggressive form used only for comparing two titles for equality."""
    text = title.lower()
    text = re.sub(r"\b(the|a|an)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


def _year_from(text: str) -> int | None:
    matches = _YEAR.findall(text)
    if not matches:
        return None
    # Last match wins: "2 Fast 2 Furious (2003)" and "Blade Runner 2049 (2017)"
    # both have a decoy earlier in the string.
    return int(matches[-1])


def parse(path: Path | str, library_kind: str | None = None) -> ParsedName:
    """Identify a file from its own name plus the two directories above it.

    The parent directories carry most of the signal in a Plex-shaped library --
    `Show Name/Season 02/episode.mkv` names the show even when the filename
    alone is a bare `s02e05.mkv`.
    """
    path = Path(path)
    stem = path.stem
    parents = [p.name for p in path.parents][:3]
    # Season folders name the season but never the show, so look past them.
    show_dirs = [p for p in parents if not re.fullmatch(r"(?i)season[\s._-]*\d+|specials", p)]
    context = f"{show_dirs[0]} {stem}" if show_dirs else stem

    for pattern in _TV_PATTERNS:
        match = pattern.search(stem) or pattern.search(context)
        if not match:
            continue
        season = int(match.group("season"))
        episode = int(match.group("episode"))
        show = clean_title(match.group("show") or "")
        confidence = 0.75
        if not show and show_dirs:
            # Filename had no show name; the directory is a better source and
            # is usually correct in a library Plex already accepts.
            show = clean_title(show_dirs[0])
            confidence = 0.7
        if not show:
            continue
        return ParsedName(
            kind="episode", name=show, year=_year_from(context),
            season=season, episode=episode, air_date=None, confidence=confidence,
        )

    dated = _DATED.search(stem)
    if dated and library_kind != "movie":
        show = clean_title(dated.group("show")) or (
            clean_title(show_dirs[0]) if show_dirs else ""
        )
        if show:
            return ParsedName(
                kind="episode", name=show, year=int(dated.group("y")),
                season=None, episode=None,
                air_date=f"{dated.group('y')}-{dated.group('m')}-{dated.group('d')}",
                confidence=0.5,
            )

    # Movie. Prefer the containing folder, which in a Plex movie library is
    # `Movie Name (2019)/` and is cleaner than any release filename.
    folder = show_dirs[0] if show_dirs else ""
    folder_year = _year_from(folder)
    if folder_year and clean_title(folder):
        return ParsedName(
            kind="movie", name=clean_title(folder), year=folder_year,
            season=None, episode=None, air_date=None, confidence=0.7,
        )

    name = clean_title(stem)
    year = _year_from(stem)
    if not name:
        return ParsedName("movie", None, None, None, None, None, 0.0)
    # Without a year there is nothing distinguishing one rip of a common title
    # from another, so this is a weak guess and is scored as one.
    return ParsedName(
        kind="movie", name=name, year=year, season=None, episode=None,
        air_date=None, confidence=0.55 if year else 0.35,
    )
