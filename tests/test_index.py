import os

import pytest

from app.config import Config
from app.db import Database
from app.scan import index


MB = 1024 * 1024


def make_file(path, size_bytes=MB):
    """Small stand-in files. Only relative size matters, and every call here
    passes min_bytes=0 so the walker's 50MB floor does not filter them out."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size_bytes)
    return path


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "tv"
    make_file(root / "Show A" / "Season 01" / "a.s01e01.mkv")
    make_file(root / "Show A" / "Season 01" / "a.s01e02.mkv")
    make_file(root / "Show B" / "b.s01e01.mkv")
    return root


def sync(db, root, **kwargs):
    kwargs.setdefault("min_bytes", 0)
    return index.sync_files(db, [root], **kwargs)


class TestSync:
    def test_first_scan_adds_everything(self, db, library):
        stats = sync(db, library)
        assert stats.seen == 3
        assert stats.added == 3
        assert db.scalar("SELECT COUNT(*) FROM media_file") == 3

    def test_rescan_is_idempotent(self, db, library):
        sync(db, library)
        stats = sync(db, library)
        assert (stats.added, stats.changed, stats.unchanged) == (0, 0, 3)
        assert db.scalar("SELECT COUNT(*) FROM media_file") == 3

    def test_changed_file_is_rescheduled_for_probe(self, db, library):
        sync(db, library)
        target = library / "Show B" / "b.s01e01.mkv"
        db.execute(
            "UPDATE media_file SET probe_version=1, v_codec='h264' WHERE path=?",
            (str(target),),
        )

        target.write_bytes(b"\0" * (2 * MB))
        os.utime(target, (1_700_000_000, 1_700_000_000))
        stats = sync(db, library)

        assert stats.changed == 1
        row = db.one("SELECT probe_version FROM media_file WHERE path=?", (str(target),))
        # Facts about bytes that no longer exist must not be trusted.
        assert row["probe_version"] is None

    def test_deleted_file_marked_missing_not_removed(self, db, library):
        sync(db, library)
        (library / "Show B" / "b.s01e01.mkv").unlink()
        stats = sync(db, library)

        assert stats.marked_missing == 1
        # The row survives: history and outcomes reference it.
        assert db.scalar("SELECT COUNT(*) FROM media_file") == 3
        assert db.scalar("SELECT COUNT(*) FROM media_file WHERE missing=1") == 1

    def test_returning_file_is_restored(self, db, library):
        sync(db, library)
        target = library / "Show B" / "b.s01e01.mkv"
        content = target.read_bytes()
        target.unlink()
        sync(db, library)

        target.write_bytes(content)
        stats = sync(db, library)
        assert stats.restored == 1
        assert db.scalar("SELECT COUNT(*) FROM media_file WHERE missing=1") == 0


class TestVanishGuard:
    """An unmounted share walks up empty. It must never look like a deletion."""

    def test_empty_root_does_not_mark_a_known_library_missing(self, db, tmp_path):
        root = tmp_path / "media"
        for n in range(30):
            make_file(root / f"file{n}.mkv")
        sync(db, root)
        assert db.scalar("SELECT COUNT(*) FROM media_file WHERE missing=0") == 30

        # Simulate the mount disappearing: directory present, contents gone.
        for child in root.rglob("*.mkv"):
            child.unlink()
        stats = sync(db, root)

        assert stats.marked_missing == 0
        assert db.scalar("SELECT COUNT(*) FROM media_file WHERE missing=1") == 0
        assert stats.skipped_guard, "the refusal must be reported, not silent"
        assert "unmounted" in stats.skipped_guard[0]

    def test_small_library_is_not_guarded(self, db, library):
        # Below the floor the guard would block ordinary tidying, so it is off.
        sync(db, library)
        for child in library.rglob("*.mkv"):
            child.unlink()
        stats = sync(db, library)
        assert stats.marked_missing == 3
        assert not stats.skipped_guard

    def test_partial_deletion_still_recorded(self, db, tmp_path):
        root = tmp_path / "media"
        for n in range(30):
            make_file(root / f"file{n}.mkv")
        sync(db, root)

        for n in range(5):
            (root / f"file{n}.mkv").unlink()
        stats = sync(db, root)
        assert stats.marked_missing == 5


class TestProbeQueue:
    def test_pending_is_largest_first(self, db, tmp_path):
        root = tmp_path / "tv"
        make_file(root / "small.mkv")
        make_file(root / "big.mkv", 5 * MB)
        sync(db, root)

        pending = index.pending_probe(db)
        assert [p[1] for p in pending][0].endswith("big.mkv")

    def test_probed_files_leave_the_queue(self, db, library):
        sync(db, library)
        assert len(index.pending_probe(db)) == 3
        db.execute("UPDATE media_file SET probe_version=999")
        assert index.pending_probe(db) == []

    def test_missing_files_are_not_probed(self, db, library):
        sync(db, library)
        (library / "Show B" / "b.s01e01.mkv").unlink()
        sync(db, library)
        paths = [p[1] for p in index.pending_probe(db)]
        assert not any("b.s01e01" in p for p in paths)


class TestScanEntryPoint:
    def test_scan_without_probe_only_walks(self, db, library, tmp_path):
        config = Config(libraries=[library], config_dir=tmp_path)
        stats = index.scan(db, config, do_probe=False, min_bytes=0)
        assert stats.added == 3
        assert stats.probed == 0
