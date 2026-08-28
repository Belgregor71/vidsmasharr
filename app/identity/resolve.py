"""Merge the identity sources into title / file_title rows.

Priority is Plex, then Sonarr/Radarr, then the filename parser. A file keeps
the best identity available to it, and records which source supplied it, so the
duplicate report can show a human why it believes two files are the same thing.

The subtle failure mode here is *title splitting*. If Plex names a show and the
filename parser names the same show for a file Plex has not scanned, two title
rows appear and the duplicate between them is never found -- which is precisely
the duplicate most worth finding. So a weaker source attaches to an existing
title when the name matches unambiguously, and only invents a new title when it
cannot.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.config import Config
from app.db import Database
from app.identity import arr, plex
from app.identity.filename import normalise, parse

Progress = Callable[[str], None]

# Database matches are authoritative; the parser never is.
CONFIDENCE = {"plex": 1.0, "sonarr": 0.95, "radarr": 0.95}


@dataclass
class ResolveStats:
    considered: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    unresolved: int = 0
    titles_created: int = 0
    titles_reused: int = 0
    warnings: list[str] = field(default_factory=list)

    def count(self, source: str) -> None:
        self.by_source[source] = self.by_source.get(source, 0) + 1

    def summary(self) -> str:
        parts = ", ".join(f"{k} {v}" for k, v in sorted(self.by_source.items()))
        lines = [
            f"  resolved {sum(self.by_source.values())}/{self.considered}  ({parts})",
            f"  unresolved {self.unresolved}",
            f"  titles created {self.titles_created}, reused {self.titles_reused}",
        ]
        for warning in self.warnings:
            lines.append(f"  !! {warning}")
        return "\n".join(lines)


class TitleIndex:
    """Find-or-create over the title table, with name-based reuse."""

    def __init__(self, db: Database, stats: ResolveStats):
        self.db = db
        self.stats = stats
        self._by_external: dict[tuple[str, str], int] = {}
        self._by_name: dict[tuple[str, str], list[tuple[int, int | None]]] = {}

        for row in db.query("SELECT id, kind, external_id, name, year FROM title"):
            self._by_external[(row["kind"], row["external_id"])] = row["id"]
            if row["name"]:
                key = (row["kind"], normalise(row["name"]))
                self._by_name.setdefault(key, []).append((row["id"], row["year"]))

    def get_or_create(
        self, kind: str, source: str, external_id: str, name: str, year: int | None
    ) -> int:
        key = (kind, external_id)
        existing = self._by_external.get(key)
        if existing is not None:
            return existing

        row = self.db.one(
            "SELECT id FROM title WHERE kind=? AND external_id=?", (kind, external_id)
        )
        if row is not None:
            self._by_external[key] = row["id"]
            return row["id"]

        self.db.execute(
            "INSERT INTO title (kind, source, external_id, name, year) VALUES (?,?,?,?,?)",
            (kind, source, external_id, name, year),
        )
        title_id = self.db.scalar("SELECT last_insert_rowid()")
        self._by_external[key] = title_id
        if name:
            self._by_name.setdefault((kind, normalise(name)), []).append((title_id, year))
        self.stats.titles_created += 1
        return title_id

    def find_by_name(self, kind: str, name: str, year: int | None) -> int | None:
        """Attach a filename guess to a title a real source already created.

        Only an unambiguous match counts. Two shows with the same name and no
        year to separate them is exactly the case where guessing wrong would
        tell a user to delete the wrong file, so it declines instead.
        """
        candidates = self._by_name.get((kind, normalise(name or "")))
        if not candidates:
            return None
        if year is not None:
            exact = [tid for tid, ty in candidates if ty == year]
            if len(exact) == 1:
                return exact[0]
            # A title whose year we never learned is still a plausible home.
            loose = [tid for tid, ty in candidates if ty is None or ty == year]
            return loose[0] if len(loose) == 1 else None
        return candidates[0][0] if len(candidates) == 1 else None


def _load_sources(
    config: Config, stats: ResolveStats, progress: Progress | None
) -> dict[str, dict]:
    sources: dict[str, dict] = {}

    if config.plex.enabled and config.plex.db_dir:
        try:
            sources["plex"] = plex.load(
                Path(config.plex.db_dir),
                Path(config.scratch_dir) / "plexdb",
                config.plex.path_map,
            )
            if progress:
                progress(f"  plex: {len(sources['plex'])} matched path(s)")
        except (plex.PlexUnavailable, OSError) as exc:
            stats.warnings.append(f"plex unavailable: {exc}")

    for name, loader, section in (
        ("sonarr", arr.load_sonarr, config.sonarr),
        ("radarr", arr.load_radarr, config.radarr),
    ):
        if not section.enabled:
            continue
        try:
            sources[name] = loader(section)
            if progress:
                progress(f"  {name}: {len(sources[name])} matched path(s)")
        except arr.ArrUnavailable as exc:
            stats.warnings.append(f"{name} unavailable: {exc}")

    return sources


def resolve(
    db: Database,
    config: Config,
    *,
    use_filenames: bool = True,
    progress: Progress | None = None,
) -> ResolveStats:
    stats = ResolveStats()
    sources = _load_sources(config, stats, progress)
    index = TitleIndex(db, stats)

    rows = db.query(
        "SELECT id, path FROM media_file WHERE missing=0 ORDER BY size_bytes DESC"
    )
    stats.considered = len(rows)

    db.execute("BEGIN")
    try:
        for row in rows:
            path = row["path"]
            resolved = False

            for source in ("plex", "sonarr", "radarr"):
                match = sources.get(source, {}).get(path)
                if match is None:
                    continue
                title_id = index.get_or_create(
                    match.title_kind, source, match.external_id, match.name, match.year,
                )
                _write(db, row["id"], title_id, match.season, match.episode,
                       source, CONFIDENCE[source])
                stats.count(source)
                resolved = True
                break

            if resolved or not use_filenames:
                if not resolved:
                    stats.unresolved += 1
                continue

            guess = parse(path)
            if not guess.is_usable:
                stats.unresolved += 1
                continue

            title_kind = "show" if guess.kind == "episode" else "movie"
            title_id = index.find_by_name(title_kind, guess.name, guess.year)
            if title_id is None:
                title_id = index.get_or_create(
                    title_kind, "filename",
                    f"filename:{title_kind}:{normalise(guess.name)}:{guess.year or ''}",
                    guess.name, guess.year,
                )
            else:
                stats.titles_reused += 1

            _write(db, row["id"], title_id, guess.season, guess.episode,
                   "filename", guess.confidence)
            stats.count("filename")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    db.set_kv("identity_resolved_at", str(time.time()))
    return stats


def _write(
    db: Database, file_id: int, title_id: int,
    season: int | None, episode: int | None, source: str, confidence: float,
) -> None:
    db.execute(
        "INSERT INTO file_title (file_id, title_id, season, episode, resolved_by, confidence) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(file_id) DO UPDATE SET "
        "title_id=excluded.title_id, season=excluded.season, episode=excluded.episode, "
        "resolved_by=excluded.resolved_by, confidence=excluded.confidence",
        (file_id, title_id, season, episode, source, confidence),
    )
