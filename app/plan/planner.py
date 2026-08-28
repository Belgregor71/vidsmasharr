"""Turn probe facts into a ranked plan.

The plan is a report, exactly like the duplicate report is: rows in the
`decision` table that say what we intend, why, what it should save and what it
should cost. Nothing here starts a job or touches a file -- Phase 3 will read
these rows, and until it exists this is a document.

Two things make the ordering worth more than any encoder tuning:

- **Priority is GB saved per encode-hour**, not size and not alphabetical. Two
  1080p episodes of the same length cost the same hour whether one is 4 Mbps
  and the other 18 Mbps, so the fat one is worth four times as much of that
  hour. The library census bears this out: the largest 20% of candidates hold
  60-65% of the available reclaim.
- **Free wins come first.** A remux that drops five foreign audio tracks costs
  minutes of disk I/O rather than hours of encoding, so it lands at the top of
  the queue on its own merits without needing a special case.

Decision states:

    pending      planned against a real calibrated ladder; Phase 3 may run it
    provisional  planned before the benchmark existed; Phase 3 must NOT run it
    deferred     good job, but this title already has its share of the queue
    skipped      we decided not to act, and the reason is recorded

Anything in another state has been picked up by Phase 3 and is no longer ours
to regenerate.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field

from app.db import Database
from app.plan import calibrate as calibrate_mod
from app.plan import estimate as estimate_mod
from app.plan import keepers as keepers_mod
from app.plan.profiles import Ladder, resolve_ladder
from app.plan.rules import CPU_ACTIONS, SKIP, FileFacts, decide

GB = 1024**3

PENDING = "pending"
PROVISIONAL = "provisional"
DEFERRED = "deferred"
SKIPPED = "skipped"

# States this module owns and rebuilds. Everything else belongs to the worker.
REGENERABLE = (PENDING, PROVISIONAL, DEFERRED, SKIPPED)


@dataclass
class PlanStats:
    considered: int = 0
    actions: Counter = field(default_factory=Counter)
    deferred: int = 0
    skipped: int = 0
    preserved: int = 0
    est_saved_bytes: int = 0
    est_cpu_seconds: float = 0.0
    skip_reasons: Counter = field(default_factory=Counter)
    provisional: bool = False
    notes: list[str] = field(default_factory=list)
    keepers: int = 0        # queued files on the hand-written x265 list
    software: int = 0       # of those, ones that really got a software rung

    @property
    def queued(self) -> int:
        return sum(self.actions.values())

    def summary(self) -> str:
        hours = self.est_cpu_seconds / 3600
        lines = [
            f"  considered {self.considered} probed file(s)",
            f"  queued     {self.queued}"
            + (f"  ({', '.join(f'{n} {a}' for a, n in self.actions.most_common())})"
               if self.actions else ""),
            f"  deferred   {self.deferred} (per-title cap)",
            f"  skipped    {self.skipped}",
            f"  reclaims   {self.est_saved_bytes / GB:,.0f} GB "
            f"for {hours:,.0f} encode-hour(s)",
        ]
        if hours > 0:
            lines.append(
                f"  that is    {self.est_saved_bytes / GB / hours:,.1f} GB per hour, "
                f"{hours / 8:,.0f} night(s) at 8h"
            )
        if self.preserved:
            lines.append(f"  left alone {self.preserved} decision(s) already in flight")
        if self.keepers:
            lines.append(
                f"  keepers    {self.keepers} queued from the x265 list "
                f"({self.software} on a software rung)"
            )
        return "\n".join(lines)


def _content_class(row) -> str:
    """movie or tv, from the resolved title if we have one, else the library.

    Only two classes exist because the ladder only has two VMAF targets. Anime
    lands in "tv", which is right: it is watched like TV and its files are
    episode-sized.
    """
    kind = (row["title_kind"] or "").lower()
    if kind == "movie":
        return "movie"
    if kind in ("show", "episode"):
        return "tv"
    return "movie" if "movie" in (row["library_root"] or "").lower() else "tv"


def _priority(saved_bytes: int | None, cpu_seconds: float | None) -> float:
    """GB reclaimed per hour of encoding.

    The floor on cpu_seconds keeps a near-instant remux from dividing by
    something close to zero and producing an unsortable infinity, while still
    leaving remuxes far above any encode -- which is exactly where they belong.
    """
    if not saved_bytes or saved_bytes <= 0:
        return 0.0
    hours = max(float(cpu_seconds or 0.0), 1.0) / 3600
    return (saved_bytes / GB) / hours


def _open_duplicate_members(db: Database) -> set[int]:
    return {
        row["file_id"]
        for row in db.query(
            "SELECT dm.file_id FROM duplicate_member dm "
            "JOIN duplicate_group g ON g.id = dm.group_id "
            "WHERE g.status = 'open'"
        )
    }


def _files(db: Database) -> list:
    return db.query(
        """
        SELECT mf.*, ft.title_id AS title_id, t.kind AS title_kind
        FROM media_file mf
        LEFT JOIN file_title ft ON ft.file_id = mf.id
        LEFT JOIN title t ON t.id = ft.title_id
        WHERE mf.missing = 0 AND mf.probe_version IS NOT NULL
        ORDER BY mf.size_bytes DESC
        """
    )


def build(
    db: Database,
    config,
    *,
    ladder: Ladder | None = None,
    estimator=None,
    progress=None,
) -> PlanStats:
    """Rebuild the plan from current facts. Writes rows; touches no media."""
    ladder = ladder or resolve_ladder(config)
    calibration = calibrate_mod.load(db)
    estimator = estimator or estimate_mod.Estimator(calibration)
    stats = PlanStats(provisional=ladder.provisional, notes=ladder.notes())
    now = time.time()

    keepers = keepers_mod.load(config)
    if keepers.error:
        stats.notes.append(keepers.error)
    if keepers and not ladder.has_software():
        stats.notes.append(
            f"{len(keepers.patterns)} keeper pattern(s) are configured but the "
            f"ladder has no software rung, so they will be encoded on the iGPU "
            f"like everything else. Run the benchmark with --include-software."
        )
    if calibration is not None:
        stats.notes.append(calibrate_mod.describe(calibration))

    in_flight = {
        row["file_id"]
        for row in db.query(
            "SELECT file_id FROM decision WHERE state NOT IN "
            "(?, ?, ?, ?)", REGENERABLE
        )
    }
    stats.preserved = len(in_flight)

    duplicates = _open_duplicate_members(db)
    rows = _files(db)

    planned = []
    for index, row in enumerate(rows, start=1):
        if row["id"] in in_flight:
            continue
        if progress and index % 500 == 0:
            progress(f"  planned {index}/{len(rows)}")

        facts = FileFacts.from_row(
            row,
            content_class=_content_class(row),
            title_id=row["title_id"],
            in_open_duplicate_group=row["id"] in duplicates,
            is_keeper=keepers.matches(row["path"]),
        )
        decision = decide(facts, config, ladder, estimator)
        decision.priority = _priority(decision.est_saved_bytes, decision.est_cpu_seconds)
        planned.append((facts, decision))
        stats.considered += 1

    _apply_title_cap(planned, config.policy.max_queued_per_title, stats)

    base_state = PROVISIONAL if ladder.provisional else PENDING
    db.execute("BEGIN")
    try:
        db.execute(
            "DELETE FROM decision WHERE state IN (?, ?, ?, ?)", REGENERABLE
        )
        for facts, decision in planned:
            if decision.action == SKIP:
                state = SKIPPED
                stats.skipped += 1
                stats.skip_reasons[_reason_key(decision.reason)] += 1
            elif decision.deferred:
                state = DEFERRED
                stats.deferred += 1
            else:
                state = base_state
                stats.actions[decision.action] += 1
                stats.est_saved_bytes += decision.est_saved_bytes or 0
                stats.est_cpu_seconds += decision.est_cpu_seconds or 0.0
                if facts.is_keeper:
                    stats.keepers += 1

            encoder = decision.detail.get("encoder")
            if state == base_state and facts.is_keeper and encoder and not (
                encoder.endswith("_vaapi") or encoder.endswith("_qsv")
            ):
                stats.software += 1

            db.execute(
                "INSERT INTO decision "
                "(file_id, title_id, action, profile, reason, est_out_bytes, "
                " est_saved_bytes, est_cpu_seconds, priority, state, "
                " estimate_basis, detail_json, encoder, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision.file_id, facts.title_id, decision.action,
                    decision.profile, decision.reason, decision.est_out_bytes,
                    decision.est_saved_bytes, decision.est_cpu_seconds,
                    decision.priority, state, decision.estimate_basis,
                    json.dumps(decision.detail) if decision.detail else None,
                    encoder, now,
                ),
            )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    db.set_kv("plan_built_at", str(now))
    db.set_kv("plan_ladder", ladder.source or "provisional")
    return stats


def _apply_title_cap(planned, cap: int, stats: PlanStats) -> None:
    """Stop one long-running show owning the queue for a month.

    Only encodes are capped. A remux costs minutes, so there is no reason to
    ration it, and rationing it would delay free space for no benefit.
    """
    if not cap or cap <= 0:
        return

    by_title: dict[int, list] = {}
    for facts, decision in planned:
        if decision.action not in CPU_ACTIONS:
            continue
        by_title.setdefault(facts.title_id, []).append(decision)

    for title_id, decisions in by_title.items():
        if title_id is None or len(decisions) <= cap:
            continue
        decisions.sort(key=lambda d: d.priority, reverse=True)
        for decision in decisions[cap:]:
            decision.deferred = True
            decision.reason = (
                f"{decision.reason} -- deferred: this title already has {cap} "
                f"file(s) queued"
            )


def _reason_key(reason: str) -> str:
    """Collapse a per-file reason into something countable for the report."""
    text = reason.split(" -- ")[0]
    for prefix, key in (
        ("protected:", "protected (HDR/DV/10-bit)"),
        ("in an unresolved duplicate", "waiting on a duplicate decision"),
        ("below the", "below the minimum source size"),
        ("already ", "already an efficient codec"),
        ("predicted saving", "saving below the floor"),
        ("no calibrated setting", "no ladder rung for that resolution"),
        ("no readable video", "no readable video stream"),
        ("not probed", "not probed yet"),
    ):
        if text.startswith(prefix):
            return key
    if "Mbps is already below" in text:
        return "already at a low bitrate"
    return text[:60]


# ------------------------------------------------------------------ reading


def top_jobs(db: Database, limit: int = 20, *, state: str | None = None) -> list:
    """The highest-value work in the plan, best first."""
    where = "d.state IN ('pending','provisional')" if state is None else "d.state = ?"
    params: tuple = () if state is None else (state,)
    return db.query(
        f"""
        SELECT d.*, mf.path, mf.size_bytes, mf.v_codec, mf.v_width, mf.v_height,
               mf.duration_s, t.name AS title_name
        FROM decision d
        JOIN media_file mf ON mf.id = d.file_id
        LEFT JOIN title t ON t.id = d.title_id
        WHERE {where}
        ORDER BY d.priority DESC
        LIMIT ?
        """,
        (*params, limit),
    )


def totals(db: Database) -> dict:
    """Plan-wide numbers for the CLI summary and the web dashboard."""
    row = db.one(
        "SELECT COUNT(*) n, COALESCE(SUM(est_saved_bytes),0) saved, "
        "COALESCE(SUM(est_cpu_seconds),0) cpu FROM decision "
        "WHERE state IN ('pending','provisional')"
    )
    by_action = db.query(
        "SELECT action, COUNT(*) n, COALESCE(SUM(est_saved_bytes),0) saved, "
        "COALESCE(SUM(est_cpu_seconds),0) cpu FROM decision "
        "WHERE state IN ('pending','provisional') GROUP BY action ORDER BY saved DESC"
    )
    skips = db.query(
        "SELECT reason, COUNT(*) n FROM decision WHERE state='skipped' "
        "GROUP BY reason ORDER BY n DESC LIMIT 12"
    )
    return {
        "queued": row["n"], "saved": row["saved"], "cpu_seconds": row["cpu"],
        "by_action": by_action, "skips": skips,
        "provisional": (db.scalar(
            "SELECT COUNT(*) FROM decision WHERE state='provisional'"
        ) or 0) > 0,
        "deferred": db.scalar(
            "SELECT COUNT(*) FROM decision WHERE state='deferred'"
        ) or 0,
        "built_at": db.get_kv("plan_built_at"),
        "ladder": db.get_kv("plan_ladder"),
    }
