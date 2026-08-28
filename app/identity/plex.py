"""Read identity out of the Plex library database.

Plex already did the hard part -- it matched every file against TVDB/TMDB and a
human has been correcting it for years. Re-deriving that from filenames would
be strictly worse, so Plex is the primary source and everything else is a
fallback.

We never open the live database. Plex writes to it constantly and holds a WAL;
attaching to it read-only while the server runs risks lock contention against
the thing our users are actually trying to watch. Copying a few hundred MB to
scratch costs seconds and removes the whole class of problem.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

PLEX_DB_NAME = "com.plexapp.plugins.library.db"

# metadata_items.metadata_type
TYPE_MOVIE = 1
TYPE_SHOW = 2
TYPE_SEASON = 3
TYPE_EPISODE = 4

_QUERY = """
SELECT
    mp.file                AS file,
    mi.metadata_type       AS kind,
    mi.title               AS item_title,
    mi.year                AS item_year,
    mi."index"             AS item_index,
    mi.guid                AS item_guid,
    season."index"         AS season_index,
    show.title             AS show_title,
    show.year              AS show_year,
    show.guid              AS show_guid
FROM media_parts mp
JOIN media_items    m      ON m.id       = mp.media_item_id
JOIN metadata_items mi     ON mi.id      = m.metadata_item_id
LEFT JOIN metadata_items season ON season.id = mi.parent_id
LEFT JOIN metadata_items show   ON show.id   = season.parent_id
WHERE mp.file IS NOT NULL
  AND mi.metadata_type IN (?, ?)
"""


class PlexUnavailable(RuntimeError):
    pass


@dataclass
class PlexMatch:
    path: str  # already rewritten into container-visible form
    kind: str  # "movie" | "episode"
    title_kind: str  # "movie" | "show"
    external_id: str
    name: str
    year: int | None
    season: int | None
    episode: int | None


def map_path(path: str, path_map: dict[str, str]) -> str:
    """Rewrite a Plex-visible path into a container-visible one.

    Plex runs as a DSM package and sees /volume1/data/media/tv; we see /media/tv
    through a bind mount. Longest prefix wins, so a specific mapping can
    override a general one.
    """
    for source in sorted(path_map, key=len, reverse=True):
        if path.startswith(source):
            return path_map[source] + path[len(source):]
    return path


def copy_database(db_dir: Path, scratch: Path) -> Path:
    """Snapshot the live Plex database (and its WAL) into scratch."""
    source = Path(db_dir) / PLEX_DB_NAME
    if not source.exists():
        raise PlexUnavailable(
            f"No Plex database at {source}. On DSM the live one lives under "
            f"/volume1/PlexMediaServer/AppData/Plex Media Server/Plug-in Support/"
            f"Databases/ -- note that the copy under /volume1/@appstore/... is an "
            f"empty package template."
        )

    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    dest = scratch / PLEX_DB_NAME
    shutil.copy2(source, dest)
    # Recent writes may live only in the WAL. Without it the snapshot is a
    # slightly stale library, which shows up as "Plex doesn't know about last
    # night's episodes".
    for suffix in ("-wal", "-shm"):
        side = source.with_name(source.name + suffix)
        if side.exists():
            shutil.copy2(side, dest.with_name(dest.name + suffix))
    return dest


def read_matches(
    db_path: Path, path_map: dict[str, str] | None = None
) -> Iterator[PlexMatch]:
    path_map = path_map or {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute(_QUERY, (TYPE_MOVIE, TYPE_EPISODE)):
            match = _to_match(row, path_map)
            if match is not None:
                yield match
    finally:
        conn.close()


def _to_match(row: sqlite3.Row, path_map: dict[str, str]) -> PlexMatch | None:
    path = map_path(row["file"], path_map)

    if row["kind"] == TYPE_MOVIE:
        guid = row["item_guid"]
        title = row["item_title"]
        if not guid or not title:
            return None
        return PlexMatch(
            path=path, kind="movie", title_kind="movie",
            external_id=guid, name=title, year=row["item_year"],
            season=None, episode=None,
        )

    # Episode. Identity belongs to the show; the episode contributes its
    # numbering. An episode whose show row is missing is an orphan in Plex's
    # own database and is not something we can group on.
    show_guid = row["show_guid"]
    show_title = row["show_title"]
    if not show_guid or not show_title:
        return None
    return PlexMatch(
        path=path, kind="episode", title_kind="show",
        external_id=show_guid, name=show_title, year=row["show_year"],
        season=row["season_index"], episode=row["item_index"],
    )


def load(
    db_dir: Path, scratch: Path, path_map: dict[str, str] | None = None
) -> dict[str, PlexMatch]:
    """Snapshot Plex and return a container-path -> match index."""
    snapshot = copy_database(Path(db_dir), Path(scratch))
    return {m.path: m for m in read_matches(snapshot, path_map)}
