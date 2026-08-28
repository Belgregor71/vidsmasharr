"""The loop that actually does the work.

Reads `pending` decisions best-first, encodes into scratch, verifies, and only
then installs. Everything dangerous is gated, and every gate fails closed.

The order of the safety checks is the design:

1. **Is this still the file we planned for?** Size and mtime are compared
   against the database, and the file is re-probed from scratch. A plan can be
   days old; an *arr upgrade that swapped an SDR release for an HDR one in the
   meantime must not be encoded against the old facts.
2. **Is it still allowed?** The protection rule is re-evaluated against the
   fresh probe, not trusted from the decision row. This is the check that stops
   an HDR grade being flattened to 8-bit SDR.
3. **Is there room?** Free space on the library volume has to stay above the
   floor, and scratch has to hold the output with headroom.
4. **Encode**, into scratch. The original is not touched.
5. **Verify.** Structure always, sampled VMAF for anything re-encoded.
6. **Install**, and only now delete -- and only if the config says so.

`safety.dry_run` prints the commands and stops. `--execute` overrides it, so a
run can be started without editing config.yaml on a NAS that has no git. It
does **not** override `delete_original_on_success`: starting work is a command
you can type, but authorising deletion stays a deliberate edit to the config.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.db import Database
from app.plan.rules import DOWNSCALE, ENCODE, REMUX
from app.scan.probe import PROBE_VERSION, MediaInfo, ProbeError, probe
from app.work import schedule, swap, verify
from app.work.ffmpeg_cmd import (
    VideoSpec,
    build_encode_command,
    build_remux_command,
    is_hardware,
)
from app.work.streams import build_stream_plan

GB = 1024**3

# How long to allow an encode before calling it hung, relative to the estimate.
# Generous: the estimate is the thing being validated, and killing a job that
# was merely slower than predicted wastes everything it had done.
TIMEOUT_FACTOR = 5
MIN_TIMEOUT_S = 3600


@dataclass
class JobResult:
    decision_id: int
    file_id: int
    path: str
    action: str
    ok: bool = False
    error: str | None = None
    saved_bytes: int = 0
    cpu_seconds: float = 0.0
    vmaf_mean: float | None = None
    original_deleted: bool = False
    note: str = ""


@dataclass
class WorkerStats:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    deleted: int = 0
    saved_bytes: int = 0
    elapsed_s: float = 0.0
    stopped_because: str = ""
    results: list[JobResult] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"  attempted  {self.attempted}",
            f"  succeeded  {self.succeeded}",
            f"  failed     {self.failed}",
            f"  reclaimed  {self.saved_bytes / GB:,.2f} GB",
            f"  originals deleted {self.deleted}",
        ]
        if self.elapsed_s:
            lines.append(f"  elapsed    {self.elapsed_s / 3600:.2f}h")
        if self.stopped_because:
            lines.append(f"  stopped:   {self.stopped_because}")
        return "\n".join(lines)


# ------------------------------------------------------------------ selection


def next_decision(db: Database, exclude: set[int] | None = None):
    """The best remaining job. Ordering was settled by the planner.

    `exclude` exists for dry runs: they change no state, so without it the same
    decision comes back every time and the loop never ends.
    """
    skip = ""
    params: tuple = ()
    if exclude:
        skip = f"AND d.id NOT IN ({','.join('?' * len(exclude))})"
        params = tuple(exclude)
    return db.one(
        f"""
        SELECT d.*, mf.path, mf.size_bytes, mf.mtime, mf.duration_s,
               mf.v_height, mf.library_root, t.kind AS title_kind
        FROM decision d
        JOIN media_file mf ON mf.id = d.file_id
        LEFT JOIN title t ON t.id = d.title_id
        WHERE d.state = 'pending' AND mf.missing = 0 {skip}
        ORDER BY d.priority DESC
        LIMIT 1
        """,
        params,
    )


def _detail(row) -> dict:
    try:
        return json.loads(row["detail_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


# ------------------------------------------------------------------ preflight


@dataclass
class Preflight:
    ok: bool
    info: MediaInfo | None = None
    error: str | None = None
    permanent: bool = False   # true when re-planning will not help


def preflight(db: Database, config, row) -> Preflight:
    source = Path(row["path"])

    if not source.exists():
        return Preflight(False, error="file no longer exists on disk", permanent=True)

    stat = source.stat()
    if stat.st_size != row["size_bytes"] or abs(stat.st_mtime - row["mtime"]) > 1.0:
        return Preflight(
            False,
            error=(
                "file changed on disk since it was planned; rescan and re-plan "
                "before encoding it"
            ),
            permanent=True,
        )

    try:
        info = probe(source, config.ffprobe)
    except ProbeError as exc:
        return Preflight(False, error=f"re-probe failed: {exc}", permanent=True)

    # Re-check protection against what is on disk right now, never against the
    # decision row. This is the check that cannot be allowed to go stale.
    from app.plan.rules import FileFacts

    facts = FileFacts.from_media_info(info)
    if facts.is_protected:
        return Preflight(
            False,
            error=(
                f"re-probe says {info.hdr_type}: protected content is never "
                f"rewritten. The plan is stale or the file was replaced."
            ),
            permanent=True,
        )

    free = swap.free_bytes(source.parent)
    if free < config.safety.min_free_bytes:
        return Preflight(
            False,
            error=(
                f"only {free / GB:.0f} GB free on the library volume, below the "
                f"{config.safety.min_free_bytes / GB:.0f} GB floor"
            ),
        )

    needed = int(stat.st_size * config.safety.scratch_headroom_factor)
    scratch = Path(config.scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    if swap.free_bytes(scratch) < needed:
        return Preflight(
            False,
            error=(
                f"scratch needs {needed / GB:.1f} GB for this job and does not "
                f"have it"
            ),
        )

    return Preflight(True, info=info)


# ------------------------------------------------------------------ running


def with_progress(cmd: list[str]) -> list[str]:
    """Ask ffmpeg for machine-readable progress on stdout.

    `-progress` writes `key=value` lines, which is far less fragile to parse
    than the carriage-return status line on stderr.
    """
    return [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]


def _run_with_progress(
    cmd: list[str], *, timeout: int, nice: int = 0, on_progress=None
) -> tuple[int, str, float]:
    """Run ffmpeg, reporting progress as it goes. Returns (rc, stderr, seconds)."""
    preexec = None
    if nice and hasattr(os, "nice"):
        def preexec() -> None:  # pragma: no cover - POSIX only
            os.nice(nice)

    started = time.monotonic()
    process = subprocess.Popen(
        with_progress(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=preexec,
    )

    out_time_s = 0.0
    try:
        for line in process.stdout or []:
            key, _, value = line.strip().partition("=")
            if key == "out_time_ms" and value.isdigit():
                out_time_s = int(value) / 1_000_000
                if on_progress:
                    on_progress(out_time_s)
            if time.monotonic() - started > timeout:
                process.kill()
                return -1, f"killed after {timeout}s", time.monotonic() - started
        process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        return -1, f"timed out after {timeout}s", time.monotonic() - started
    finally:
        if process.stdout:
            process.stdout.close()

    stderr = process.stderr.read() if process.stderr else ""
    if process.stderr:
        process.stderr.close()
    return process.returncode, stderr, time.monotonic() - started


def build_command(row, info: MediaInfo, config, *, dest: Path, threads: int):
    """The ffmpeg command for this decision, plus what it will keep."""
    detail = _detail(row)
    plan = build_stream_plan(info, config)

    if row["action"] == REMUX:
        cmd = build_remux_command(
            ffmpeg=config.ffmpeg, source=Path(row["path"]), dest=dest,
            audio_args=plan.args,
        )
        return cmd, plan, None

    encoder = detail.get("encoder") or "hevc_vaapi"
    spec = VideoSpec(
        encoder=encoder,
        quality=float(detail.get("quality") or 24),
        target_height=detail.get("target_height"),
        preset=config.encoder.x265_preset,
        hw_decode=True,
    )
    cmd = build_encode_command(
        ffmpeg=config.ffmpeg,
        source=Path(row["path"]),
        dest=dest,
        spec=spec,
        vaapi_device=config.encoder.vaapi_device,
        audio_args=plan.args,
        threads=None if is_hardware(encoder) else threads,
    )
    return cmd, plan, spec


def run_decision(
    db: Database, config, row, *, window, dry_run: bool, progress=None
) -> JobResult:
    result = JobResult(
        decision_id=row["id"], file_id=row["file_id"], path=row["path"],
        action=row["action"],
    )
    source = Path(row["path"])
    detail = _detail(row)

    check = preflight(db, config, row)
    if not check.ok:
        result.error = check.error
        # A dry run reports what it found and writes nothing, including this.
        if not dry_run:
            _fail(db, row, check.error or "preflight failed", permanent=check.permanent)
        return result

    info = check.info
    scratch = Path(config.scratch_dir) / "encoding"
    scratch.mkdir(parents=True, exist_ok=True)
    dest = scratch / (source.stem + swap.OUTPUT_SUFFIX)
    dest.unlink(missing_ok=True)

    cmd, stream_plan, spec = build_command(
        row, info, config, dest=dest, threads=window.threads
    )

    if dry_run:
        result.ok = True
        result.note = "dry run"
        if progress:
            progress(f"    would run: {' '.join(cmd)}")
            progress(f"    {stream_plan.summary}")
        return result

    job_id = _start_job(db, row, cmd, dest)
    timeout = max(MIN_TIMEOUT_S, int((row["est_cpu_seconds"] or 0) * TIMEOUT_FACTOR))

    def report(seconds: float) -> None:
        if info.duration_s > 0:
            pct = min(100.0, 100.0 * seconds / info.duration_s)
            db.execute("UPDATE job SET progress_pct=? WHERE id=?", (pct, job_id))

    code, stderr, elapsed = _run_with_progress(
        cmd, timeout=timeout, nice=window.nice, on_progress=report
    )
    result.cpu_seconds = elapsed

    if code != 0:
        tail = " | ".join((stderr or "").strip().splitlines()[-3:])
        message = f"encode failed ({code}): {tail[:300]}"
        dest.unlink(missing_ok=True)
        _finish_job(db, job_id, "failed", elapsed, error=message)
        _fail(db, row, message)
        result.error = message
        return result

    # --- verification -------------------------------------------------------
    if progress:
        progress("    verifying ...")

    expect_codec = None if row["action"] == REMUX else "hevc"
    structure = verify.check_structure(
        info, dest, ffprobe=config.ffprobe,
        expect_codec=expect_codec,
        expect_height=detail.get("target_height"),
        expect_audio_tracks=len(stream_plan.kept_audio) or None,
    )

    quality = None
    if structure.ok and row["action"] in (ENCODE, DOWNSCALE):
        target = detail.get("target_vmaf") or (
            config.quality.movie_vmaf
            if (row["title_kind"] or "").lower() == "movie"
            else config.quality.tv_vmaf
        )
        quality = verify.check_quality(
            source, dest, source=info, config=config, target_vmaf=float(target),
            work_dir=Path(config.scratch_dir) / "vmaf",
            downscaled=bool(detail.get("target_height")),
            threads=max(1, window.threads // 2),
            progress=progress,
        )

    verdict = verify.merge(structure, quality)
    result.vmaf_mean = verdict.vmaf_mean

    if not verdict.ok:
        held = swap.quarantine(dest, Path(config.scratch_dir), verdict.summary)
        message = verdict.summary + (f" (kept at {held})" if held else "")
        _finish_job(db, job_id, "failed", elapsed, error=message,
                    vmaf=_vmaf_json(verdict))
        _fail(db, row, message)
        result.error = message
        return result

    # --- install ------------------------------------------------------------
    installed = swap.install(
        dest, source,
        delete_original=config.safety.delete_original_on_success,
    )
    if not installed.ok:
        _finish_job(db, job_id, "failed", elapsed, error=installed.error)
        _fail(db, row, installed.error or "install failed")
        result.error = installed.error
        return result

    out_bytes = (verdict.out_info.size_bytes if verdict.out_info else 0)
    result.ok = True
    result.saved_bytes = max(0, info.size_bytes - out_bytes)
    result.original_deleted = installed.original_deleted
    result.note = (
        f"kept at {installed.final_path}" if not installed.original_deleted
        else f"replaced {source.name}"
    )

    _finish_job(db, job_id, "done", elapsed, vmaf=_vmaf_json(verdict))
    _record_outcome(db, job_id, row, info, verdict, installed, elapsed)

    if installed.original_deleted and verdict.out_info and installed.final_path:
        _adopt_output(db, row["file_id"], installed.final_path, verdict.out_info)
        # The intent has been carried out. `outcome` is the permanent record;
        # leaving the decision behind would stop this path being planned again
        # if a future release replaces the file.
        db.execute("DELETE FROM decision WHERE id=?", (row["id"],))
    else:
        # Encoded and verified, but `delete_original_on_success` is off, so the
        # output is sitting in scratch. `held` rather than `done`: turning
        # deletion on later should install these, not re-encode them.
        db.execute("UPDATE decision SET state='held' WHERE id=?", (row["id"],))

    return result


# ------------------------------------------------------------------ the loop


def run(
    db: Database,
    config,
    *,
    limit: int | None = None,
    execute: bool = False,
    ignore_schedule: bool = False,
    progress=print,
) -> WorkerStats:
    stats = WorkerStats()
    started = time.monotonic()
    dry_run = config.safety.dry_run and not execute
    deletes = 0
    seen: set[int] = set()

    reclaimed = reclaim_stale(db)
    if reclaimed and progress:
        progress(f"  recovered {reclaimed} decision(s) left mid-flight by an "
                 f"interrupted run")

    while limit is None or stats.attempted < limit:
        window = (
            schedule.WorkWindow(True, config.schedule.night_threads, 0, "schedule ignored")
            if ignore_schedule
            else schedule.may_work_now(config)
        )
        if not window.working:
            stats.stopped_because = window.reason
            break

        if deletes >= config.safety.max_deletes_per_run:
            stats.stopped_because = (
                f"hit the {config.safety.max_deletes_per_run}-delete ceiling for "
                f"one run"
            )
            break

        row = next_decision(db, seen if dry_run else None)
        if row is None:
            stats.stopped_because = "nothing pending"
            break
        if dry_run:
            seen.add(row["id"])

        stats.attempted += 1
        if progress:
            saved = (row["est_saved_bytes"] or 0) / GB
            progress(
                f"\n  [{stats.attempted}] {row['action']} {Path(row['path']).name}\n"
                f"      {saved:.2f} GB expected, {window.reason}"
            )

        result = run_decision(
            db, config, row, window=window, dry_run=dry_run, progress=progress
        )
        stats.results.append(result)

        if result.ok:
            stats.succeeded += 1
            stats.saved_bytes += result.saved_bytes
            if result.original_deleted:
                stats.deleted += 1
                deletes += 1
            if progress and not dry_run:
                progress(
                    f"      done: {result.saved_bytes / GB:.2f} GB saved"
                    + (f", VMAF {result.vmaf_mean:.1f}" if result.vmaf_mean else "")
                    + f" -- {result.note}"
                )
        else:
            stats.failed += 1
            if progress:
                progress(f"      FAILED: {result.error}")

    stats.elapsed_s = time.monotonic() - started
    if dry_run and not stats.stopped_because:
        stats.stopped_because = "dry run"
    return stats


# ------------------------------------------------------------------ db writes


def _start_job(db: Database, row, cmd: list[str], dest: Path) -> int:
    db.execute("UPDATE decision SET state='running' WHERE id=?", (row["id"],))
    db.execute(
        "INSERT INTO job (decision_id, state, attempts, cmd, scratch_path, started_at) "
        "VALUES (?,'running',1,?,?,?)",
        (row["id"], " ".join(cmd), str(dest), time.time()),
    )
    return db.scalar("SELECT last_insert_rowid()")


def _finish_job(
    db: Database, job_id: int, state: str, seconds: float, *,
    error: str | None = None, vmaf: str | None = None,
) -> None:
    db.execute(
        "UPDATE job SET state=?, ended_at=?, cpu_seconds=?, error=?, vmaf_json=?, "
        "progress_pct=? WHERE id=?",
        (state, time.time(), seconds, error, vmaf,
         100.0 if state == "done" else None, job_id),
    )


def _fail(db: Database, row, message: str, *, permanent: bool = False) -> None:
    """Take a decision out of the queue, in one of two different ways.

    A *stale plan* -- the file moved, changed, or turned out to be protected on
    re-probe -- goes back to `skipped`, which the planner regenerates. Nothing
    is wrong with the file; the plan was simply out of date, and the next plan
    will look at it again with fresh facts.

    A real *failure* -- the encode died, verification said no -- sticks as
    `failed`, which the planner preserves. A file that failed verification must
    not quietly climb back to the top of a queue that is months long.
    `app work --retry-failed` clears those deliberately.
    """
    state = "skipped" if permanent else "failed"
    db.execute(
        "UPDATE decision SET state=?, reason=? WHERE id=?",
        (state, message[:500], row["id"]),
    )


def _vmaf_json(verdict) -> str | None:
    if verdict.vmaf_mean is None:
        return None
    return json.dumps({
        "mean": verdict.vmaf_mean, "min": verdict.vmaf_min,
        "p1": verdict.vmaf_p1, "samples": verdict.samples,
    })


def _record_outcome(db: Database, job_id, row, info, verdict, installed, seconds) -> None:
    out_bytes = verdict.out_info.size_bytes if verdict.out_info else 0
    db.execute(
        "INSERT INTO outcome (job_id, file_path, action, before_bytes, after_bytes, "
        " saved_bytes, est_saved_bytes, cpu_seconds, est_cpu_seconds, vmaf_min, "
        " vmaf_mean, original_deleted, completed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            job_id, row["path"], row["action"], info.size_bytes, out_bytes,
            info.size_bytes - out_bytes, row["est_saved_bytes"], seconds,
            row["est_cpu_seconds"], verdict.vmaf_min, verdict.vmaf_mean,
            1 if installed.original_deleted else 0, time.time(),
        ),
    )


def _adopt_output(db: Database, file_id: int, path: Path, info: MediaInfo) -> None:
    """Point the database at the file we just wrote.

    Without this the scanner would report the old path as missing and the new
    one as new, and the file would lose its identity and its history until the
    next identify run.
    """
    stat = path.stat()
    row = info.to_row()
    db.execute(
        "UPDATE media_file SET path=?, size_bytes=?, mtime=?, probe_version=?, "
        " probed_at=?, container=?, duration_s=?, bitrate=?, v_codec=?, v_profile=?, "
        " v_bit_depth=?, v_width=?, v_height=?, v_bitrate=?, v_fps=?, hdr_type=?, "
        " audio_json=?, subs_json=?, last_seen=? "
        "WHERE id=?",
        (
            str(path), stat.st_size, stat.st_mtime, PROBE_VERSION, time.time(),
            row["container"], row["duration_s"], row["bitrate"], row["v_codec"],
            row["v_profile"], row["v_bit_depth"], row["v_width"], row["v_height"],
            row["v_bitrate"], row["v_fps"], row["hdr_type"], row["audio_json"],
            row["subs_json"], time.time(), file_id,
        ),
    )


def reclaim_stale(db: Database) -> int:
    """Recover decisions a killed worker left marked `running`.

    Only one worker runs at a time, so anything still `running` when a new one
    starts is the wreckage of an interrupted run, not a live job. Its scratch
    output is incomplete and worthless; the decision goes back in the queue.
    """
    stale = db.query("SELECT id FROM decision WHERE state='running'")
    if not stale:
        return 0
    db.execute(
        "UPDATE job SET state='interrupted', ended_at=? WHERE state='running'",
        (time.time(),),
    )
    db.execute("UPDATE decision SET state='pending' WHERE state='running'")
    return len(stale)


def install_held(db: Database, config, *, progress=print) -> WorkerStats:
    """Install outputs that were encoded and verified but never installed.

    This is the second half of the trial-batch flow. The first batch runs with
    `delete_original_on_success` off, so the outputs pile up in scratch for you
    to watch on the TVs. Once you are happy and turn deletion on, this installs
    what is already there rather than spending those hours again.

    The structural checks are re-run first -- the file has been sitting on disk
    and may have been touched -- but not VMAF, which passed at encode time and
    is recorded on the job row.
    """
    stats = WorkerStats()
    if not config.safety.delete_original_on_success:
        stats.stopped_because = (
            "delete_original_on_success is off, so there is nowhere to install to"
        )
        return stats

    rows = db.query(
        """
        SELECT d.*, mf.path, mf.size_bytes, j.scratch_path, j.id AS job_id
        FROM decision d
        JOIN media_file mf ON mf.id = d.file_id
        JOIN job j ON j.decision_id = d.id AND j.state = 'done'
        WHERE d.state = 'held'
        ORDER BY d.priority DESC
        """
    )
    for row in rows:
        stats.attempted += 1
        output = Path(row["scratch_path"] or "")
        source = Path(row["path"])
        if not output.exists():
            db.execute(
                "UPDATE decision SET state='pending', reason=? WHERE id=?",
                ("held output is gone from scratch; re-encode it", row["id"]),
            )
            stats.failed += 1
            continue

        try:
            info = probe(source, config.ffprobe)
        except ProbeError as exc:
            stats.failed += 1
            _fail(db, row, f"source will not probe: {exc}", permanent=True)
            continue

        structure = verify.check_structure(
            info, output, ffprobe=config.ffprobe,
            expect_codec=None if row["action"] == REMUX else "hevc",
        )
        if not structure.ok:
            stats.failed += 1
            _fail(db, row, f"held output no longer verifies: {structure.summary}")
            continue

        installed = swap.install(output, source, delete_original=True)
        if not installed.ok:
            stats.failed += 1
            _fail(db, row, installed.error or "install failed")
            continue

        out_bytes = structure.out_info.size_bytes if structure.out_info else 0
        stats.succeeded += 1
        stats.saved_bytes += max(0, info.size_bytes - out_bytes)
        stats.deleted += 1 if installed.original_deleted else 0
        if progress:
            progress(f"  installed {source.name} "
                     f"({(info.size_bytes - out_bytes) / GB:.2f} GB)")

        _record_outcome(db, row["job_id"], row, info, structure, installed, 0.0)
        if installed.final_path:
            _adopt_output(db, row["file_id"], installed.final_path, structure.out_info)
        db.execute("DELETE FROM decision WHERE id=?", (row["id"],))

    return stats


def retry_failed(db: Database) -> int:
    """Put failed decisions back in the queue. Returns how many."""
    count = db.scalar("SELECT COUNT(*) FROM decision WHERE state='failed'") or 0
    db.execute("UPDATE decision SET state='pending' WHERE state='failed'")
    return count
