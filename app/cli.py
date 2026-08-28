"""Command line entry point.

The container's ENTRYPOINT is `python3 -m`, so on the NAS these read as:

    docker compose ... run --rm vidsmasharr app scan
    docker compose ... run --rm vidsmasharr app identify
    docker compose ... run --rm vidsmasharr app duplicates
    docker compose ... run --rm vidsmasharr app phase1     (all three, in order)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import Config, load_config
from app.db import Database
from app.dedupe import groups
from app.identity import resolve
from app.scan import index
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
