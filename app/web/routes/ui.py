"""The three pages Phase 1 needs: what we found, what is duplicated, what is where.

Every write here changes the *report*, never a file. Dismissing a group or
choosing a different keeper records a human's decision so the next rebuild
leaves it alone -- see dedupe/groups.build().
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

GB = 1024**3
PAGE_SIZE = 100


def _render(request: Request, template: str, **extra):
    # Starlette wants the request first; it injects it into the context itself.
    context = {"nav": request.url.path, "safety_note": _safety_note(request)}
    context.update(extra)
    return request.app.state.templates.TemplateResponse(request, template, context)


def _safety_note(request: Request) -> str:
    """The header banner has to say what is actually true.

    Through Phase 2 nothing could delete anything and the banner said so. Now
    that the worker exists, a stale reassurance in the header is worse than no
    banner at all.
    """
    safety = request.app.state.config.safety
    if safety.dry_run:
        return "dry run · nothing is encoded or deleted"
    if not safety.delete_original_on_success:
        return "encoding · originals are kept"
    return "encoding · originals are deleted after verification"


@router.get("/")
def dashboard(request: Request):
    db = request.app.state.db

    files = db.scalar("SELECT COUNT(*) FROM media_file WHERE missing=0") or 0
    probed = db.scalar(
        "SELECT COUNT(*) FROM media_file WHERE missing=0 AND probe_version IS NOT NULL"
    ) or 0
    total_bytes = db.scalar(
        "SELECT COALESCE(SUM(size_bytes),0) FROM media_file WHERE missing=0"
    ) or 0
    missing = db.scalar("SELECT COUNT(*) FROM media_file WHERE missing=1") or 0

    codecs = db.query(
        "SELECT v_codec, COUNT(*) n, SUM(size_bytes) b FROM media_file "
        "WHERE missing=0 AND v_codec IS NOT NULL GROUP BY v_codec ORDER BY b DESC"
    )
    tiers = db.query(
        """
        SELECT CASE
                 WHEN v_height >= 1700 THEN '2160p'
                 WHEN v_height >= 1000 THEN '1080p'
                 WHEN v_height >=  700 THEN '720p'
                 ELSE 'SD' END AS tier,
               COUNT(*) n, SUM(size_bytes) b
        FROM media_file WHERE missing=0 AND v_height IS NOT NULL
        GROUP BY tier ORDER BY b DESC
        """
    )
    dupes = db.one(
        "SELECT COUNT(*) n, COALESCE(SUM(reclaimable_bytes),0) b, "
        "COALESCE(SUM(needs_human),0) h FROM duplicate_group WHERE status='open'"
    )
    unresolved = db.scalar(
        "SELECT COUNT(*) FROM media_file mf LEFT JOIN file_title ft ON ft.file_id=mf.id "
        "WHERE mf.missing=0 AND ft.file_id IS NULL"
    ) or 0

    return _render(
        request, "dashboard.html",
        files=files, probed=probed, total_bytes=total_bytes, missing=missing,
        codecs=codecs, tiers=tiers, dupes=dupes, unresolved=unresolved,
        titles=db.scalar("SELECT COUNT(*) FROM title") or 0,
    )


@router.get("/duplicates")
def duplicates(request: Request, only: str = "all", page: int = 1):
    db = request.app.state.db
    where = "WHERE g.status='open'"
    if only == "needs_human":
        where += " AND g.needs_human=1"
    elif only == "clear":
        where += " AND g.needs_human=0"

    total = db.scalar(f"SELECT COUNT(*) FROM duplicate_group g {where}") or 0
    offset = max(0, (page - 1) * PAGE_SIZE)
    rows = db.query(
        f"""
        SELECT g.*, t.name, t.kind
        FROM duplicate_group g JOIN title t ON t.id=g.title_id
        {where}
        ORDER BY g.reclaimable_bytes DESC
        LIMIT ? OFFSET ?
        """,
        (PAGE_SIZE, offset),
    )

    members: dict[int, list] = {}
    for group in rows:
        members[group["id"]] = db.query(
            """
            SELECT m.rank, m.score_json, f.id, f.path, f.size_bytes, f.v_codec,
                   f.v_width, f.v_height, f.hdr_type, f.duration_s
            FROM duplicate_member m JOIN media_file f ON f.id=m.file_id
            WHERE m.group_id=? ORDER BY m.rank
            """,
            (group["id"],),
        )

    reclaimable = db.scalar(
        f"SELECT COALESCE(SUM(g.reclaimable_bytes),0) FROM duplicate_group g {where}"
    ) or 0

    return _render(
        request, "duplicates.html",
        groups=rows, members=members, only=only, page=page,
        total=total, reclaimable=reclaimable,
        pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
    )


@router.get("/plan")
def plan(request: Request, page: int = 1):
    """The ranked plan. Read-only, like every other page here."""
    from app.plan import planner

    db = request.app.state.db
    totals = planner.totals(db)

    offset = max(0, (page - 1) * PAGE_SIZE)
    jobs = db.query(
        """
        SELECT d.*, mf.path, mf.size_bytes, t.name AS title_name
        FROM decision d
        JOIN media_file mf ON mf.id = d.file_id
        LEFT JOIN title t ON t.id = d.title_id
        WHERE d.state IN ('pending','provisional')
        ORDER BY d.priority DESC
        LIMIT ? OFFSET ?
        """,
        (PAGE_SIZE, offset),
    )
    hours = (totals["cpu_seconds"] or 0) / 3600

    return _render(
        request, "plan.html",
        totals=totals, jobs=jobs, skips=totals["skips"], page=page, offset=offset,
        pages=max(1, (totals["queued"] + PAGE_SIZE - 1) // PAGE_SIZE),
        rate=(totals["saved"] / GB / hours) if hours else 0.0,
    )


@router.get("/activity")
def activity(request: Request):
    """What the worker has done. Read-only, like everything else here."""
    db = request.app.state.db

    totals = db.one(
        "SELECT COUNT(*) n, COALESCE(SUM(saved_bytes),0) saved, "
        "COALESCE(SUM(cpu_seconds),0) cpu, COALESCE(SUM(est_saved_bytes),0) est "
        "FROM outcome"
    )
    outcomes = db.query(
        "SELECT * FROM outcome ORDER BY completed_at DESC LIMIT ?", (PAGE_SIZE,)
    )
    running = db.query(
        """
        SELECT j.progress_pct, j.started_at, d.action, mf.path
        FROM job j
        JOIN decision d ON d.id = j.decision_id
        JOIN media_file mf ON mf.id = d.file_id
        WHERE j.state = 'running'
        """
    )
    failures = db.query(
        """
        SELECT d.reason, mf.path FROM decision d
        JOIN media_file mf ON mf.id = d.file_id
        WHERE d.state = 'failed' ORDER BY d.priority DESC LIMIT 50
        """
    )

    # How well the planner predicted. Above 1 means we saved more than promised.
    accuracy = None
    if totals["est"]:
        accuracy = {"ratio": totals["saved"] / totals["est"]}

    # Broken out per estimator model, because they are wrong in different
    # directions and one aggregate number hides both. This is the same
    # measurement `app calibrate` acts on; the page only reads it.
    from app.plan import calibrate

    report = calibrate.measure(db)

    return _render(
        request, "activity.html",
        totals=totals, outcomes=outcomes, running=running, failures=failures,
        accuracy=accuracy,
        failed=len(failures),
        held=db.scalar("SELECT COUNT(*) FROM decision WHERE state='held'") or 0,
        models=report.size + report.speed,
        model_warnings=report.warnings,
        in_force=calibrate.load(db),
        min_samples=calibrate.MIN_SAMPLES,
    )


@router.post("/duplicates/{group_id}/keeper")
def set_keeper(request: Request, group_id: int, file_id: int = Form(...)):
    """Record that a human chose a different copy to keep."""
    db = request.app.state.db
    member = db.one(
        "SELECT file_id FROM duplicate_member WHERE group_id=? AND file_id=?",
        (group_id, file_id),
    )
    if member:
        keeper_size = db.scalar("SELECT size_bytes FROM media_file WHERE id=?", (file_id,)) or 0
        total = db.scalar(
            "SELECT COALESCE(SUM(f.size_bytes),0) FROM duplicate_member m "
            "JOIN media_file f ON f.id=m.file_id WHERE m.group_id=?",
            (group_id,),
        ) or 0
        db.execute(
            "UPDATE duplicate_group SET keeper_file_id=?, reclaimable_bytes=?, "
            "needs_human=0, status='chosen', reason='keeper chosen by hand' WHERE id=?",
            (file_id, total - keeper_size, group_id),
        )
    return RedirectResponse("/duplicates", status_code=303)


@router.post("/duplicates/{group_id}/dismiss")
def dismiss(request: Request, group_id: int):
    """Not a duplicate, or not one worth acting on. Survives future rebuilds."""
    db = request.app.state.db
    db.execute(
        "UPDATE duplicate_group SET status='dismissed', needs_human=0, "
        "reason='dismissed by hand' WHERE id=?",
        (group_id,),
    )
    return RedirectResponse("/duplicates", status_code=303)


@router.post("/duplicates/{group_id}/reopen")
def reopen(request: Request, group_id: int):
    db = request.app.state.db
    db.execute("UPDATE duplicate_group SET status='open' WHERE id=?", (group_id,))
    return RedirectResponse("/duplicates?only=all", status_code=303)


@router.get("/library")
def library(
    request: Request, page: int = 1, codec: str = "", tier: str = "",
    hdr: str = "", q: str = "", sort: str = "size",
):
    db = request.app.state.db
    clauses = ["mf.missing=0"]
    params: list = []

    if codec:
        clauses.append("mf.v_codec=?")
        params.append(codec)
    if hdr == "protected":
        clauses.append("mf.hdr_type IS NOT NULL AND mf.hdr_type != 'sdr'")
    elif hdr == "sdr":
        clauses.append("mf.hdr_type = 'sdr'")
    if tier:
        bounds = {"2160p": (1700, 99999), "1080p": (1000, 1699),
                  "720p": (700, 999), "sd": (0, 699)}.get(tier)
        if bounds:
            clauses.append("mf.v_height BETWEEN ? AND ?")
            params.extend(bounds)
    if q:
        clauses.append("mf.path LIKE ?")
        params.append(f"%{q}%")

    where = " AND ".join(clauses)
    order = {
        "size": "mf.size_bytes DESC",
        "bitrate": "mf.v_bitrate DESC",
        "path": "mf.path ASC",
    }.get(sort, "mf.size_bytes DESC")

    total = db.scalar(f"SELECT COUNT(*) FROM media_file mf WHERE {where}", params) or 0
    offset = max(0, (page - 1) * PAGE_SIZE)
    rows = db.query(
        f"""
        SELECT mf.*, t.name AS title_name, ft.season, ft.episode,
               ft.resolved_by, ft.confidence
        FROM media_file mf
        LEFT JOIN file_title ft ON ft.file_id = mf.id
        LEFT JOIN title t ON t.id = ft.title_id
        WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?
        """,
        (*params, PAGE_SIZE, offset),
    )
    codec_options = [
        r["v_codec"] for r in db.query(
            "SELECT DISTINCT v_codec FROM media_file WHERE v_codec IS NOT NULL "
            "ORDER BY v_codec"
        )
    ]

    return _render(
        request, "library.html",
        rows=rows, total=total, page=page,
        pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
        codec=codec, tier=tier, hdr=hdr, q=q, sort=sort,
        codec_options=codec_options,
    )
