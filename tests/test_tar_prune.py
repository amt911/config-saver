"""tar_prune: apply a retention policy to <cfg>/<YYYYMMDD-HHMMSS>/ directories."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from config_saver.lib.models.retention_model import RetentionModel
from config_saver.lib.retention import keep_set
from config_saver.lib.tar_prune import TIMESTAMP_FORMAT, prune_config_dir


def _stamps(count: int) -> list[datetime]:
    start = datetime(2026, 1, 1, 3, 0, 0)
    return [start + timedelta(days=i) for i in range(count)]


def _make_dirs(cfg_dir: Path, stamps: list[datetime]) -> None:
    for stamp in stamps:
        directory = cfg_dir / stamp.strftime(TIMESTAMP_FORMAT)
        directory.mkdir(parents=True)
        (directory / "archive.tar.gz").write_bytes(b"x")


def test_removes_the_complement_of_keep_set(tmp_path: Path) -> None:
    stamps = _stamps(55)
    _make_dirs(tmp_path, stamps)
    policy = RetentionModel(daily=3, weekly=0, monthly=0, yearly=0)

    removed = prune_config_dir(str(tmp_path), policy, dry_run=False)

    keep = keep_set(stamps, policy)
    expected_gone = sorted(str(tmp_path / s.strftime(TIMESTAMP_FORMAT)) for s in stamps if s not in keep)
    assert sorted(removed) == expected_gone
    assert len(removed) == 52
    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == sorted(s.strftime(TIMESTAMP_FORMAT) for s in keep)


def test_dry_run_removes_nothing(tmp_path: Path) -> None:
    _make_dirs(tmp_path, _stamps(10))
    removed = prune_config_dir(str(tmp_path), RetentionModel(daily=1, weekly=0, monthly=0, yearly=0), dry_run=True)
    assert len(removed) == 9
    assert len(list(tmp_path.iterdir())) == 10


def test_ignores_non_timestamp_entries(tmp_path: Path) -> None:
    _make_dirs(tmp_path, _stamps(5))
    (tmp_path / "notes").mkdir()
    (tmp_path / "20260101-000000.txt").write_text("a file, not a directory")
    (tmp_path / "20260101-000000x").mkdir()
    (tmp_path / "99999999-999999").mkdir()  # right shape, not a date
    (tmp_path / "legacy-20250101-000000.tar.gz").write_bytes(b"x")
    outside = tmp_path.parent / "outside-target"
    outside.mkdir()
    (tmp_path / "20250101-000000").symlink_to(outside, target_is_directory=True)

    removed = prune_config_dir(str(tmp_path), RetentionModel(daily=1, weekly=0, monthly=0, yearly=0), dry_run=False)

    assert len(removed) == 4
    for name in (
        "notes",
        "20260101-000000.txt",
        "20260101-000000x",
        "99999999-999999",
        "legacy-20250101-000000.tar.gz",
    ):
        assert (tmp_path / name).exists()
    assert (tmp_path / "20250101-000000").is_symlink()
    assert outside.exists()


def test_missing_directory_is_nothing_to_do(tmp_path: Path) -> None:
    assert prune_config_dir(str(tmp_path / "absent"), RetentionModel(), dry_run=False) == []


def test_zero_policy_still_keeps_the_newest(tmp_path: Path) -> None:
    stamps = _stamps(4)
    _make_dirs(tmp_path, stamps)
    prune_config_dir(str(tmp_path), RetentionModel(daily=0, weekly=0, monthly=0, yearly=0), dry_run=False)
    assert [p.name for p in tmp_path.iterdir()] == [stamps[-1].strftime(TIMESTAMP_FORMAT)]


def test_dirs_without_an_archive_are_not_backups(tmp_path: Path) -> None:
    """Regression: a failed run's empty directory used to count as a backup."""
    policy = RetentionModel(daily=2, weekly=0, monthly=0, yearly=0)
    _make_dirs(tmp_path, [datetime(2026, 1, 1, 3), datetime(2026, 1, 2, 3)])
    (tmp_path / "20260101-150000").mkdir()  # failed run: no archive

    removed = prune_config_dir(str(tmp_path), policy, dry_run=False)

    assert removed == []
    assert (tmp_path / "20260101-150000").is_dir()  # not ours to delete either
    assert (tmp_path / "20260101-030000").is_dir()


def test_also_keep_protects_this_runs_own_backup(tmp_path: Path) -> None:
    stamps = _stamps(5)
    _make_dirs(tmp_path, stamps)
    zero = RetentionModel(daily=0, weekly=0, monthly=0, yearly=0)
    removed = prune_config_dir(str(tmp_path), zero, dry_run=False, also_keep=[stamps[0]])
    assert len(removed) == 3
    assert (tmp_path / stamps[0].strftime(TIMESTAMP_FORMAT)).is_dir()
