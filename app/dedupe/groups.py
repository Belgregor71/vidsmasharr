"""Find files that are the same thing twice, and rank which copy to keep.

This module reports. It never deletes, moves, or queues anything -- that is a
locked decision, and it is the right one: a duplicate report that is wrong
costs a user five seconds of reading, while a duplicate *deleter* that is wrong
costs them a file they cannot get back.

The keeper is scored on source quality, not on size. A 4K HDR copy is the
keeper even though it is the biggest file in the group, because the whole point
of Phase 3 is that we can shrink the keeper later; we can never un-shrink a
copy we threw away.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.db import Database

TIER_SCORE = {"2160p": 400, "1080p": 300, "720p": 200, "sd": 100}
LOSSLESS = {"truehd", "dtshd", "flac", "mlp", "pcm"}

# Two copies whose runtimes differ by more than this are probably not the same
# cut -- theatrical vs extended, or one of them is truncated. Either way a
# human must look before anything is called a duplicate.
DURATION_TOLERANCE = 0.05

# If the best two copies score this close, the ranking is not meaningful enough
# to present as an answer.
AMBIGUOUS_MARGIN = 25


@dataclass
class Member:
    file_id: int
    path: str
    size_bytes: int
    score: float
    tier: str
    codec: str | None
    hdr_type: str | None
    duration_s: float | None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({
            "score": self.score, "tier": self.tier, "codec": self.codec,
            "hdr": self.hdr_type, "duration_s": self.duration_s,
            "notes": self.notes,
        })


@dataclass
class DedupeStats:
    groups: int = 0
    files_in_groups: int = 0
    reclaimable_bytes: int = 0
    needs_human: int = 0
    preserved: int = 0

    def summary(self) -> str:
        gb = self.reclaimable_bytes / 1024**3
        return (
            f"  {self.groups} duplicate group(s), {self.files_in_groups} file(s)\n"
            f"  reclaimable {gb:.1f} GB\n"
            f"  needs a human: {self.needs_human}\n"
            f"  left alone (already decided): {self.preserved}"
        )


def _tier(width: int | None, height: int | None) -> str:
    w, h = width or 0, height or 0
    if w >= 3000 or h >= 1700:
        return "2160p"
    if w >= 1800 or h >= 1000:
        return "1080p"
    if w >= 1200 or h >= 700:
        return "720p"
    return "sd"


def score_file(row: Any) -> Member:
    """Rank a copy by how good a *source* it is."""
    tier = _tier(row["v_width"], row["v_height"])
    score = float(TIER_SCORE.get(tier, 100))
    notes: list[str] = [tier]

    hdr = row["hdr_type"] or "sdr"
    if hdr != "sdr":
        # A wider-gamut master carries information an SDR copy simply lacks.
        score += 40
        notes.append(hdr)

    if (row["v_bit_depth"] or 8) > 8:
        score += 10
        notes.append(f"{row['v_bit_depth']}-bit")

    audio = []
    try:
        audio = json.loads(row["audio_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        audio = []
    if audio:
        best_channels = max((track.get("channels") or 0) for track in audio)
        score += min(best_channels, 8) * 2
        if any((track.get("codec") or "") in LOSSLESS for track in audio):
            score += 15
            notes.append("lossless audio")
        if best_channels >= 6:
            notes.append(f"{best_channels}ch")

    if (row["container"] or "").lower() in ("matroska", "matroska,webm", "mkv"):
        # Keeps subtitle and multi-track structure that mp4/avi lose.
        score += 5

    return Member(
        file_id=row["id"], path=row["path"], size_bytes=row["size_bytes"],
        score=score, tier=tier, codec=row["v_codec"], hdr_type=hdr,
        duration_s=row["duration_s"], notes=notes,
    )


def _judge(members: list[Member]) -> tuple[bool, str]:
    """Decide whether the ranking is safe to present as an answer."""
    best, second = members[0], members[1]

    durations = [m.duration_s for m in members if m.duration_s]
    if len(durations) > 1:
        longest, shortest = max(durations), min(durations)
        if shortest > 0 and (longest - shortest) / longest > DURATION_TOLERANCE:
            delta = (longest - shortest) / 60.0
            return True, (
                f"runtimes differ by {delta:.1f} min -- these may be different "
                f"cuts rather than duplicates"
            )

    if best.hdr_type != "sdr" and second.tier != best.tier:
        return True, (
            f"trade-off: {best.tier} {best.hdr_type} vs {second.tier} SDR. "
            f"HDR is never re-encoded here, so the SDR copy may be the one "
            f"that plays everywhere"
        )

    if best.score - second.score < AMBIGUOUS_MARGIN:
        return True, "copies are too similar in quality to rank confidently"

    return False, f"keeping {best.tier} {best.codec or '?'} ({', '.join(best.notes)})"


def _candidate_groups(db: Database) -> list[Any]:
    return db.query(
        """
        SELECT ft.title_id, ft.season, ft.episode, COUNT(*) AS n
        FROM file_title ft
        JOIN media_file mf ON mf.id = ft.file_id
        WHERE mf.missing = 0 AND mf.probe_version IS NOT NULL
        GROUP BY ft.title_id, ft.season, ft.episode
        HAVING COUNT(*) > 1
        """
    )


def _members_of(db: Database, title_id: int, season, episode) -> list[Any]:
    return db.query(
        """
        SELECT mf.* FROM media_file mf
        JOIN file_title ft ON ft.file_id = mf.id
        WHERE ft.title_id = ?
          AND ft.season IS ?
          AND ft.episode IS ?
          AND mf.missing = 0
        """,
        (title_id, season, episode),
    )


def build(db: Database, *, progress=None) -> DedupeStats:
    """Rebuild the duplicate report from current facts."""
    stats = DedupeStats()
    now = time.time()

    # Groups a human has already ruled on are theirs, not ours to regenerate.
    decided = {
        (r["title_id"], r["season"], r["episode"])
        for r in db.query(
            "SELECT title_id, season, episode FROM duplicate_group WHERE status != 'open'"
        )
    }
    stats.preserved = len(decided)

    db.execute("BEGIN")
    try:
        db.execute("DELETE FROM duplicate_group WHERE status = 'open'")

        for candidate in _candidate_groups(db):
            key = (candidate["title_id"], candidate["season"], candidate["episode"])
            if key in decided:
                continue

            rows = _members_of(db, *key)
            if len(rows) < 2:
                continue

            members = sorted(
                (score_file(row) for row in rows),
                key=lambda m: (m.score, m.size_bytes),
                reverse=True,
            )
            needs_human, reason = _judge(members)
            keeper = members[0]
            reclaimable = sum(m.size_bytes for m in members) - keeper.size_bytes

            db.execute(
                "INSERT INTO duplicate_group "
                "(title_id, season, episode, keeper_file_id, member_count, "
                " reclaimable_bytes, needs_human, reason, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,'open',?)",
                (*key, keeper.file_id, len(members), reclaimable,
                 1 if needs_human else 0, reason, now),
            )
            group_id = db.scalar("SELECT last_insert_rowid()")
            for rank, member in enumerate(members):
                db.execute(
                    "INSERT INTO duplicate_member (group_id, file_id, rank, score_json) "
                    "VALUES (?,?,?,?)",
                    (group_id, member.file_id, rank, member.to_json()),
                )

            stats.groups += 1
            stats.files_in_groups += len(members)
            stats.reclaimable_bytes += reclaimable
            stats.needs_human += 1 if needs_human else 0
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    db.set_kv("dedupe_built_at", str(now))
    return stats
