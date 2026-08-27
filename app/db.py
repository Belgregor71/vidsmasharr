"""SQLite access layer.

Plain sqlite3 rather than an ORM: this runs on a 4GB NAS alongside Plex, the
query patterns are simple, and it keeps the container footprint down. Schema is
versioned with a sequential migration list; each entry bumps user_version by one.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA: list[str] = []


def migration(sql: str) -> None:
    SCHEMA.append(sql)


# --- 1: files discovered by the scanner + what ffprobe found in them ---------
migration("""
CREATE TABLE media_file (
    id            INTEGER PRIMARY KEY,
    path          TEXT NOT NULL UNIQUE,
    library_root  TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    probe_version INTEGER,
    probed_at     REAL,
    container     TEXT,
    duration_s    REAL,
    bitrate       INTEGER,
    v_codec       TEXT,
    v_profile     TEXT,
    v_bit_depth   INTEGER,
    v_width       INTEGER,
    v_height      INTEGER,
    v_bitrate     INTEGER,
    v_fps         REAL,
    hdr_type      TEXT,
    audio_json    TEXT,
    subs_json     TEXT,
    probe_error   TEXT,
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    missing       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_media_file_root  ON media_file(library_root);
CREATE INDEX idx_media_file_probe ON media_file(probe_version, missing);
CREATE INDEX idx_media_file_size  ON media_file(size_bytes DESC);
""")

# --- 2: canonical identity, resolved from Plex / *arr / filename -------------
migration("""
CREATE TABLE title (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,
    source       TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    name         TEXT,
    year         INTEGER,
    UNIQUE(kind, external_id)
);

CREATE TABLE file_title (
    file_id     INTEGER NOT NULL REFERENCES media_file(id) ON DELETE CASCADE,
    title_id    INTEGER NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    season      INTEGER,
    episode     INTEGER,
    resolved_by TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (file_id)
);
CREATE INDEX idx_file_title_title ON file_title(title_id, season, episode);
""")

# --- 3: duplicate groups (report only -- nothing here ever deletes) ----------
migration("""
CREATE TABLE duplicate_group (
    id                INTEGER PRIMARY KEY,
    title_id          INTEGER NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    season            INTEGER,
    episode           INTEGER,
    keeper_file_id    INTEGER REFERENCES media_file(id) ON DELETE SET NULL,
    member_count      INTEGER NOT NULL,
    reclaimable_bytes INTEGER NOT NULL,
    needs_human       INTEGER NOT NULL DEFAULT 0,
    reason            TEXT,
    status            TEXT NOT NULL DEFAULT 'open',
    created_at        REAL NOT NULL,
    UNIQUE(title_id, season, episode)
);

CREATE TABLE duplicate_member (
    group_id   INTEGER NOT NULL REFERENCES duplicate_group(id) ON DELETE CASCADE,
    file_id    INTEGER NOT NULL REFERENCES media_file(id) ON DELETE CASCADE,
    rank       INTEGER NOT NULL,
    score_json TEXT,
    PRIMARY KEY (group_id, file_id)
);
""")

# --- 4: what we intend to do, and what actually happened --------------------
migration("""
CREATE TABLE decision (
    id              INTEGER PRIMARY KEY,
    file_id         INTEGER NOT NULL REFERENCES media_file(id) ON DELETE CASCADE,
    action          TEXT NOT NULL,
    profile         TEXT,
    reason          TEXT NOT NULL,
    est_out_bytes   INTEGER,
    est_saved_bytes INTEGER,
    est_cpu_seconds REAL,
    priority        REAL,
    state           TEXT NOT NULL DEFAULT 'pending',
    created_at      REAL NOT NULL,
    UNIQUE(file_id)
);
CREATE INDEX idx_decision_priority ON decision(state, priority DESC);

CREATE TABLE job (
    id           INTEGER PRIMARY KEY,
    decision_id  INTEGER NOT NULL REFERENCES decision(id) ON DELETE CASCADE,
    state        TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    cmd          TEXT,
    scratch_path TEXT,
    started_at   REAL,
    ended_at     REAL,
    cpu_seconds  REAL,
    progress_pct REAL DEFAULT 0,
    vmaf_json    TEXT,
    error        TEXT
);
CREATE INDEX idx_job_state ON job(state);

CREATE TABLE outcome (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    file_path       TEXT NOT NULL,
    action          TEXT NOT NULL,
    before_bytes    INTEGER NOT NULL,
    after_bytes     INTEGER NOT NULL,
    saved_bytes     INTEGER NOT NULL,
    est_saved_bytes INTEGER,
    cpu_seconds     REAL,
    est_cpu_seconds REAL,
    vmaf_min        REAL,
    vmaf_mean       REAL,
    original_deleted INTEGER NOT NULL DEFAULT 0,
    completed_at    REAL NOT NULL
);
""")

# --- 5: Phase 0 benchmark results + generic key/value -----------------------
migration("""
CREATE TABLE bench_result (
    id            INTEGER PRIMARY KEY,
    run_id        TEXT NOT NULL,
    clip          TEXT NOT NULL,
    encoder       TEXT NOT NULL,
    quality_key   TEXT NOT NULL,
    quality_value REAL NOT NULL,
    src_width     INTEGER,
    src_height    INTEGER,
    out_width     INTEGER,
    out_height    INTEGER,
    frames        INTEGER,
    wall_seconds  REAL,
    cpu_seconds   REAL,
    fps           REAL,
    in_bytes      INTEGER,
    out_bytes     INTEGER,
    size_ratio    REAL,
    vmaf_mean     REAL,
    vmaf_min      REAL,
    vmaf_p1       REAL,
    ok            INTEGER NOT NULL DEFAULT 1,
    error         TEXT,
    created_at    REAL NOT NULL
);
CREATE INDEX idx_bench_run ON bench_result(run_id);

CREATE TABLE kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
""")


class Database:
    """Thread-local connections over one SQLite file in WAL mode."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            # Small cache: this shares 4GB of RAM with Plex.
            conn.execute("PRAGMA cache_size=-16000")
            self._local.conn = conn
        return conn

    def migrate(self) -> None:
        conn = self.conn
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for index, sql in enumerate(SCHEMA[current:], start=current):
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version={index + 1}")

    # -- helpers ------------------------------------------------------------

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.one(sql, params)
        return row[0] if row else None

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
