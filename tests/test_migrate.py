"""Migration of tar archives into the restic store, with a fake store (no restic needed)."""

from __future__ import annotations

import gzip
import io
import shutil
import tarfile
from datetime import datetime
from pathlib import Path
from typing import IO

import pytest

from config_saver.lib.backup_manager.backup_manager import BackupManager
from config_saver.lib.errors import StoreError
from config_saver.lib.migrate import migrate_archives
from config_saver.lib.restic_store import Snapshot
from config_saver.lib.tar_compressor.tar_compressor import CompressResult


def _tar_bytes(payload: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for i in range(3):
            data = f"{payload}-{i}".encode()
            info = tarfile.TarInfo(f"home/u/{i}.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _archive(saves: Path, cfg: str, stamp: str, payload: str = "x", description: str | None = None) -> Path:
    folder = saves / "configs" / cfg / stamp
    folder.mkdir(parents=True)
    path = folder / f"{cfg}-{stamp}.tar.gz"
    path.write_bytes(gzip.compress(_tar_bytes(payload)))
    if description:
        (folder / "description.txt").write_text(description)
    return path


class FakeStore:
    identity = "/nonexistent/key.txt"

    def __init__(self) -> None:
        self.snaps: list[Snapshot] = []
        self.data: dict[str, bytes] = {}
        self.dump_override: bytes | None = None
        self.fail_backup: Exception | None = None
        self.fail_check = False
        self.checks = 0
        self.forgotten: list[str] = []
        self.fail_forget = False
        self.backups: list[tuple[str, datetime, str | None]] = []

    def backup(self, write, cfg, when, description):  # type: ignore[no-untyped-def]
        if self.fail_backup:
            raise self.fail_backup
        buf = io.BytesIO()
        result: CompressResult = write(buf)
        snap = Snapshot(f"id{len(self.snaps)}", f"sh{len(self.snaps)}", when, cfg, description)
        self.snaps.append(snap)
        self.data[snap.id] = buf.getvalue()
        self.backups.append((cfg, when, description))
        return snap, result, 1

    def snapshots(self, cfg: str | None = None) -> list[Snapshot]:
        return sorted((s for s in self.snaps if cfg in (None, s.cfg)), key=lambda s: s.time)

    def dump(self, snapshot_id: str, cfg: str, out: IO[bytes]) -> None:
        out.write(self.dump_override if self.dump_override is not None else self.data[snapshot_id])

    def forget_ids(self, ids: list[str]) -> None:
        if self.fail_forget:
            raise StoreError("forget failed")
        self.forgotten += ids
        self.snaps = [s for s in self.snaps if s.id not in ids]

    def check(self) -> None:
        self.checks += 1
        if self.fail_check:
            raise StoreError("check failed")


@pytest.fixture
def saves(tmp_path: Path) -> Path:
    path = tmp_path / "saves"
    path.mkdir()
    return path


def _run(saves: Path, store: FakeStore, delete: bool = False):  # type: ignore[no-untyped-def]
    return migrate_archives(BackupManager(str(saves)), store, None, delete)  # type: ignore[arg-type]


def test_migrate_oldest_first_preserves_time_and_description(saves: Path) -> None:
    _archive(saves, "a", "20260103-030000", "c")
    _archive(saves, "a", "20260101-030000", "a", description="first one")
    _archive(saves, "b", "20260102-040506", "b")
    store = FakeStore()
    report = _run(saves, store)
    assert [(c, w) for c, w, _ in store.backups] == [
        ("a", datetime(2026, 1, 1, 3, 0, 0)),
        ("b", datetime(2026, 1, 2, 4, 5, 6)),
        ("a", datetime(2026, 1, 3, 3, 0, 0)),
    ]
    assert store.backups[0][2] == "first one" and store.backups[1][2] is None
    assert len(report.migrated) == 3 and not report.skipped and not report.failed and not report.deleted
    assert report.bytes_before > 0
    assert store.data["id0"] == _tar_bytes("a")
    assert store.checks == 1
    assert len(list((saves / "configs").rglob("*.tar.gz"))) == 3  # nothing deleted without the flag


def test_migrate_is_idempotent_on_rerun(saves: Path) -> None:
    _archive(saves, "a", "20260101-030000")
    store = FakeStore()
    _run(saves, store)
    again = _run(saves, store)
    assert len(store.backups) == 1
    assert len(again.skipped) == 1 and not again.migrated and not again.failed


def test_migrate_legacy_top_level_tagged_legacy(saves: Path) -> None:
    (saves / "config-saver-20251231-235959.tar.gz").write_bytes(gzip.compress(_tar_bytes("L")))
    store = FakeStore()
    _run(saves, store)
    assert store.backups == [("legacy", datetime(2025, 12, 31, 23, 59, 59), None)]


def test_migrate_hyphenated_cfg_name(saves: Path) -> None:
    _archive(saves, "claude-conversations", "20261019-120456")
    store = FakeStore()
    _run(saves, store)
    assert store.backups[0][:2] == ("claude-conversations", datetime(2026, 10, 19, 12, 4, 56))


def test_migrate_unparsable_name_uses_mtime(saves: Path) -> None:
    odd = saves / "weird.tar.gz"
    odd.write_bytes(gzip.compress(_tar_bytes("w")))
    import os

    os.utime(odd, (1_700_000_000, 1_700_000_000))
    store = FakeStore()
    _run(saves, store)
    assert store.backups[0][0] == "weird"
    assert store.backups[0][1] == datetime.fromtimestamp(1_700_000_000)


def test_migrate_delete_removes_verified_archive_description_and_dir(saves: Path) -> None:
    archive = _archive(saves, "a", "20260101-030000", description="d")
    other = _archive(saves, "b", "20260102-030000")
    store = FakeStore()
    report = _run(saves, store, delete=True)
    assert sorted(report.deleted) == sorted([str(archive), str(other)])
    assert not archive.parent.exists() and not other.parent.exists()
    assert (saves / "configs" / "a").is_dir()  # only the timestamp dir goes


def test_migrate_delete_only_after_hash_match(saves: Path) -> None:
    archive = _archive(saves, "a", "20260101-030000", description="d")
    store = FakeStore()
    store.dump_override = b"corrupted"
    report = _run(saves, store, delete=True)
    assert archive.exists() and (archive.parent / "description.txt").exists()
    assert not report.deleted and not report.migrated
    [(failed_path, reason)] = report.failed
    assert failed_path == str(archive) and "hash" in reason.lower()


def test_migrate_restic_lock_error_is_failure_not_crash(saves: Path) -> None:
    good = _archive(saves, "a", "20260101-030000")
    store = FakeStore()
    store.fail_backup = StoreError("restic failed: repository is already locked")
    report = _run(saves, store, delete=True)
    assert good.exists()
    assert [p for p, _ in report.failed] == [str(good)] and "locked" in report.failed[0][1]
    assert not report.deleted and not report.migrated


def test_migrate_corrupt_gzip_is_failure_and_writes_no_snapshot_data_kept(saves: Path) -> None:
    archive = _archive(saves, "a", "20260101-030000")
    archive.write_bytes(archive.read_bytes()[:-20])  # truncated gzip
    store = FakeStore()
    report = _run(saves, store, delete=True)
    assert archive.exists() and len(report.failed) == 1 and not report.deleted


def test_migrate_skipped_archive_is_verified_before_delete(saves: Path) -> None:
    archive = _archive(saves, "a", "20260101-030000")
    store = FakeStore()
    _run(saves, store)  # migrated, nothing deleted
    store.dump_override = b"not the same"
    bad = _run(saves, store, delete=True)
    assert archive.exists() and bad.failed and not bad.deleted and not bad.skipped
    store.dump_override = None
    good = _run(saves, store, delete=True)
    assert not archive.exists() and good.deleted == [str(archive)] and good.skipped == [str(archive)]
    assert len(store.backups) == 1


def test_migrate_reports_check_failure_and_still_returns(saves: Path) -> None:
    _archive(saves, "a", "20260101-030000")
    store = FakeStore()
    store.fail_check = True
    report = _run(saves, store)
    assert report.check_error == "check failed" and len(report.migrated) == 1


def test_migrate_progress_lines(saves: Path) -> None:
    _archive(saves, "a", "20260101-030000")
    lines: list[str] = []
    migrate_archives(BackupManager(str(saves)), FakeStore(), None, False, progress=lines.append)  # type: ignore[arg-type]
    assert len(lines) == 1 and "migrated" in lines[0]


@pytest.mark.skipif(shutil.which("age") is None, reason="needs age")
def test_migrate_age_archive_with_missing_identity_fails_that_archive_only(saves: Path) -> None:
    plain_a = _archive(saves, "a", "20260101-030000")
    enc = plain_a.rename(plain_a.with_name(plain_a.name + ".age"))
    enc.write_bytes(b"age-encryption.org/v1 junk")
    plain = _archive(saves, "b", "20260102-030000")
    store = FakeStore()
    report = _run(saves, store, delete=True)
    assert [p for p, _ in report.failed] == [str(enc)] and "identity" in report.failed[0][1].lower()
    assert report.migrated == [str(plain)]


def test_migrate_bad_new_snapshot_is_forgotten_and_rerun_migrates_again(saves: Path) -> None:
    archive = _archive(saves, "a", "20260101-030000")
    store = FakeStore()
    store.dump_override = b"corrupted"
    report = _run(saves, store)  # even without --delete-migrated
    assert [p for p, _ in report.failed] == [str(archive)] and not report.migrated
    assert store.forgotten == ["id0"] and store.snapshots() == []
    assert "forgotten" in report.failed[0][1]
    store.dump_override = None
    again = _run(saves, store)
    assert again.migrated == [str(archive)] and not again.skipped and len(store.backups) == 2


def test_migrate_bad_snapshot_that_cannot_be_forgotten_says_how(saves: Path) -> None:
    _archive(saves, "a", "20260101-030000")
    store = FakeStore()
    store.dump_override = b"corrupted"
    store.fail_forget = True
    report = _run(saves, store)
    assert "restic forget id0" in report.failed[0][1]


def test_migrate_existing_mismatch_tells_how_to_re_migrate_and_never_forgets(saves: Path) -> None:
    archive = _archive(saves, "a", "20260101-030000")
    store = FakeStore()
    _run(saves, store)
    store.dump_override = b"other"
    report = _run(saves, store, delete=True)
    assert f"differs from {archive}" in report.failed[0][1] and "restic forget id0" in report.failed[0][1]
    assert store.forgotten == [] and len(store.snaps) == 1


def test_migrate_duplicate_cfg_and_second_is_failed_never_skipped(saves: Path) -> None:
    first = _archive(saves, "a", "20260101-030000")
    twin = first.with_name(first.name + ".gpg")  # same cfg and second, different file
    shutil.copy(first, twin)
    store = FakeStore()
    report = _run(saves, store)
    assert report.migrated == [str(first)] and not report.skipped
    assert [p for p, _ in report.failed] == [str(twin)]
    assert f"same config and timestamp as {first}" in report.failed[0][1]
