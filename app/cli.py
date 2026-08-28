"""Command line entry point.

The container's ENTRYPOINT is `python3 -m`, so on the NAS these read as:

    docker compose ... run --rm vidsmasharr app scan
    docker compose ... run --rm vidsmasharr app identify
    docker compose ... run --rm vidsmasharr app duplicates
    docker compose ... run --rm vidsmasharr app phase1     (all three, in order)
    docker compose ... run --rm vidsmasharr app plan
    docker compose ... run --rm vidsmasharr app work
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import Config, load_config
from app.db import Database
from app.dedupe import groups
from app.identity import resolve
from app.plan import planner
from app.plan.profiles import resolve_ladder
from app.scan import index
from app.work import schedule, worker
from app.scan.walker import diagnose_roots

GB = 1024**3


def _header(text: str) -> None:
    print()
    print("=" * 72)
    print(f"  {text}")
    print("=" * 72)


def _load(args) -> tuple[Config, Database]:
    config = load_config(Path(args.config) if args.config else None)
    if args.libraries:
        config.libraries = [Path(p) for p in args.libraries]
    db = Database(Path(args.db) if args.db else config.db_path)
    return config, db


# ------------------------------------------------------------------ commands


def cmd_scan(args) -> int:
    config, db = _load(args)
    if not config.libraries:
        print("No libraries configured. Set `libraries:` in config.yaml or pass "
              "--libraries.")
        return 2

    _header("SCAN  walk libraries, then probe what changed")
    stats = index.scan(
        db, config,
        do_probe=not args.no_probe,
        probe_limit=args.probe_limit,
        workers=args.workers,
        progress=print if args.verbose else None,
    )
    print(stats.summary())

    if stats.seen == 0:
        print("\nNothing found. Here is what those paths actually are:\n")
        print(diagnose_roots(config.libraries))
        print("\nThese are paths as the CONTAINER sees them, not host paths.")
        return 2
    return 0


def cmd_identify(args) -> int:
    config, db = _load(args)
    _header("IDENTIFY  resolve files to titles via Plex, *arr, then filenames")

    total = db.scalar("SELECT COUNT(*) FROM media_file WHERE missing=0") or 0
    if not total:
        print("No files in the database yet. Run `app scan` first.")
        return 2

    stats = resolve.resolve(
        db, config,
        use_filenames=not args.no_filenames,
        progress=print,
    )
    print(stats.summary())
    return 0


def cmd_duplicates(args) -> int:
    config, db = _load(args)
    _header("DUPLICATES  group files that are the same thing twice")

    resolved = db.scalar("SELECT COUNT(*) FROM file_title") or 0
    if not resolved:
        print("Nothing is identified yet. Run `app identify` first.")
        return 2

    stats = groups.build(db)
    print(stats.summary())

    rows = db.query(
        """
        SELECT g.reclaimable_bytes, g.member_count, g.needs_human, g.reason,
               t.name, t.kind, g.season, g.episode
        FROM duplicate_group g JOIN title t ON t.id = g.title_id
        WHERE g.status = 'open'
        ORDER BY g.reclaimable_bytes DESC LIMIT ?
        """,
        (args.top,),
    )
    if rows:
        print(f"\n  Largest {len(rows)} group(s) by reclaimable space:\n")
        for row in rows:
            where = ""
            if row["season"] is not None:
                where = f" S{row['season']:02d}E{row['episode'] or 0:02d}"
            flag = "  [needs a human]" if row["needs_human"] else ""
            print(f"    {row['reclaimable_bytes'] / GB:7.2f} GB  "
                  f"{row['name']}{where}  ({row['member_count']} copies){flag}")
            print(f"                 {row['reason']}")

    print("\n  Nothing has been deleted or queued. This is a report.")
    return 0


def cmd_plan(args) -> int:
    config, db = _load(args)
    _header("PLAN  decide what to do with each file, and in what order")

    probed = db.scalar(
        "SELECT COUNT(*) FROM media_file WHERE missing=0 AND probe_version IS NOT NULL"
    ) or 0
    if not probed:
        print("Nothing is probed yet. Run `app scan` first.")
        return 2

    ladder = resolve_ladder(config)
    if ladder.provisional and not args.provisional:
        print(
            f"No quality ladder at {config.profiles_path}.\n\n"
            "Without it, every size and time below would be a guess from the\n"
            "policy targets rather than a measurement from this box. Run the\n"
            "benchmark first:\n\n"
            "    docker compose -f docker/docker-compose.yml run --rm "
            "vidsmasharr bench --libraries /media/tv /media/movies\n\n"
            "Or pass --provisional to see the shape of the plan anyway. Those\n"
            "decisions are written in a state Phase 3 will refuse to execute."
        )
        return 2

    stats = planner.build(
        db, config, ladder=ladder, progress=print if args.verbose else None
    )
    for note in stats.notes:
        print(f"\n  ! {note}")
    print()
    print(stats.summary())

    if stats.skip_reasons:
        print("\n  Not queued, because:\n")
        for reason, count in stats.skip_reasons.most_common(12):
            print(f"    {count:>6}  {reason}")

    rows = planner.top_jobs(db, args.top)
    if rows:
        print(f"\n  Best {len(rows)} job(s) by GB reclaimed per encode-hour:\n")
        for row in rows:
            hours = (row["est_cpu_seconds"] or 0) / 3600
            # The file name, not the title: forty episodes of one show all carry
            # the same title and the list becomes unreadable.
            name = Path(row["path"]).name
            print(f"    {row['priority']:6.1f} GB/h  "
                  f"{(row['est_saved_bytes'] or 0) / GB:6.2f} GB in {hours:5.1f}h  "
                  f"{row['action']:<9} {name[:52]}")
            print(f"                    {row['reason']}")

    print("\n  Nothing has been encoded, moved or deleted. This is a plan.")
    return 0


def cmd_work(args) -> int:
    config, db = _load(args)
    _header("WORK  encode, verify, and replace")

    if args.retry_failed:
        count = worker.retry_failed(db)
        print(f"  {count} failed decision(s) put back in the queue.\n")

    if args.install_held:
        stats = worker.install_held(db, config)
        print(stats.summary())
        return 0 if not stats.failed else 1

    pending = db.scalar("SELECT COUNT(*) FROM decision WHERE state='pending'") or 0
    if not pending:
        held = db.scalar("SELECT COUNT(*) FROM decision WHERE state='held'") or 0
        print("Nothing pending. Run `app plan` first.")
        if held:
            print(f"\n  {held} output(s) are encoded and waiting in scratch. Turn on\n"
                  "  safety.delete_original_on_success and run `app work "
                  "--install-held`\n  to install them without re-encoding.")
        return 2

    dry_run = config.safety.dry_run and not args.execute
    if dry_run:
        print("  DRY RUN -- printing commands, changing nothing.")
        print("  Pass --execute to really encode.\n")
    else:
        if config.safety.delete_original_on_success:
            print("  Originals WILL be deleted after verification passes.\n")
        else:
            print("  Originals will be kept; outputs stay in scratch for review.")
            print("  Turn on safety.delete_original_on_success when you are happy.\n")

    window = schedule.may_work_now(config)
    if not window.working and not args.now:
        print(f"  Not working right now: {window.reason}.")
        print("  Pass --now to override the schedule.")
        return 0

    stats = worker.run(
        db, config,
        limit=args.limit,
        execute=args.execute,
        ignore_schedule=args.now,
        progress=print,
    )
    print()
    print(stats.summary())
    return 0 if stats.failed == 0 else 1


def cmd_phase1(args) -> int:
    for step in (cmd_scan, cmd_identify, cmd_duplicates):
        code = step(args)
        if code != 0:
            return code
    return 0


def cmd_status(args) -> int:
    config, db = _load(args)
    _header("STATUS")

    files = db.scalar("SELECT COUNT(*) FROM media_file WHERE missing=0") or 0
    probed = db.scalar(
        "SELECT COUNT(*) FROM media_file WHERE missing=0 AND probe_version IS NOT NULL"
    ) or 0
    total_bytes = db.scalar(
        "SELECT COALESCE(SUM(size_bytes),0) FROM media_file WHERE missing=0"
    ) or 0
    print(f"  files      {files} ({probed} probed)")
    print(f"  size       {total_bytes / 1024**4:.2f} TB")
    print(f"  titles     {db.scalar('SELECT COUNT(*) FROM title') or 0}")
    print(f"  identified {db.scalar('SELECT COUNT(*) FROM file_title') or 0}")

    dupes = db.one(
        "SELECT COUNT(*) n, COALESCE(SUM(reclaimable_bytes),0) b "
        "FROM duplicate_group WHERE status='open'"
    )
    print(f"  duplicates {dupes['n']} group(s), {dupes['b'] / GB:.1f} GB reclaimable")

    if files:
        print("\n  codec mix (by bytes):")
        for row in db.query(
            "SELECT v_codec, COUNT(*) n, SUM(size_bytes) b FROM media_file "
            "WHERE missing=0 AND v_codec IS NOT NULL "
            "GROUP BY v_codec ORDER BY b DESC LIMIT 8"
        ):
            share = 100.0 * row["b"] / total_bytes if total_bytes else 0
            print(f"    {row['v_codec']:<12} {row['n']:>6} files  {share:5.1f}%")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    config, _ = _load(args)
    uvicorn.run(
        "app.web.app:create_app", factory=True,
        host=config.web_host, port=config.web_port,
        log_level=config.log_level.lower(),
    )
    return 0


# ---------------------------------------------------------------------- parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app", description="vidsmasharr")

    # These live on the subcommands rather than the top level: a global
    # --libraries with nargs="*" swallows the subcommand name itself, so
    # `app --libraries /media/tv scan` parses as a missing command.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None)
    common.add_argument("--db", default=None)
    common.add_argument("--libraries", nargs="*", default=None,
                        help="Override configured library roots (container paths)")
    common.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", parents=[common],
                          help="walk libraries and probe new/changed files")
    scan.add_argument("--no-probe", action="store_true", help="walk only")
    scan.add_argument("--probe-limit", type=int, default=None,
                      help="probe at most N files (largest first)")
    scan.add_argument("--workers", type=int, default=2)
    scan.set_defaults(func=cmd_scan)

    identify = sub.add_parser("identify", parents=[common],
                              help="resolve files to titles")
    identify.add_argument("--no-filenames", action="store_true",
                          help="database sources only; no filename guessing")
    identify.set_defaults(func=cmd_identify)

    duplicates = sub.add_parser("duplicates", parents=[common],
                                help="build the duplicate report")
    duplicates.add_argument("--top", type=int, default=20)
    duplicates.set_defaults(func=cmd_duplicates)

    phase1 = sub.add_parser("phase1", parents=[common],
                            help="scan, identify and report in one go")
    phase1.add_argument("--no-probe", action="store_true")
    phase1.add_argument("--probe-limit", type=int, default=None)
    phase1.add_argument("--workers", type=int, default=2)
    phase1.add_argument("--no-filenames", action="store_true")
    phase1.add_argument("--top", type=int, default=20)
    phase1.set_defaults(func=cmd_phase1)

    plan = sub.add_parser("plan", parents=[common],
                          help="decide what to do with each file, and rank it")
    plan.add_argument("--top", type=int, default=20,
                      help="how many of the best jobs to print")
    plan.add_argument("--provisional", action="store_true",
                      help="plan without a calibrated ladder; estimates are guesses "
                           "and the decisions are not executable")
    plan.set_defaults(func=cmd_plan)

    work = sub.add_parser("work", parents=[common],
                          help="encode, verify and replace the planned files")
    work.add_argument("--limit", type=int, default=None,
                      help="stop after N files")
    work.add_argument("--execute", action="store_true",
                      help="override safety.dry_run and really encode. Does NOT "
                           "override delete_original_on_success")
    work.add_argument("--now", action="store_true",
                      help="ignore the schedule window (still pauses for streams)")
    work.add_argument("--retry-failed", action="store_true",
                      help="put previously failed decisions back in the queue")
    work.add_argument("--install-held", action="store_true",
                      help="install outputs already encoded and verified in scratch")
    work.set_defaults(func=cmd_work)

    status = sub.add_parser("status", parents=[common],
                            help="what the database currently knows")
    status.set_defaults(func=cmd_status)

    serve = sub.add_parser("serve", parents=[common], help="run the web UI")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
