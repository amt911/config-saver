"""BackupManager: archive discovery, batch mode, permissions, parallelism."""

from __future__ import annotations

import io
import os
import stat
import tarfile
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO

import pytest
import yaml

from config_saver.lib.backup_manager.backup_manager import BackupManager
from config_saver.lib.errors import StoreError
from config_saver.lib.models.retention_model import RetentionModel
from config_saver.lib.restic_store import Snapshot
from config_saver.lib.retention import keep_set
from config_saver.lib.tar_compressor.tar_compressor import CompressResult

from .conftest import write_config


def _touch_archive(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_list_archives_includes_legacy_and_per_config(tmp_path: Path) -> None:
    """A single per-config archive used to hide every top-level one."""
    saves = tmp_path / "saves"
    legacy = _touch_archive(saves / "config-saver-20260101-000000.tar.gz")
    per_config = _touch_archive(saves / "configs" / "zsh" / "20260102-000000" / "zsh-20260102-000000.tar.gz")

    assert BackupManager(str(saves)).list_archives() == sorted([str(legacy), str(per_config)])


def test_list_archives_is_sorted_and_deduplicated(tmp_path: Path) -> None:
    saves = tmp_path / "saves"
    _touch_archive(saves / "configs" / "b" / "1" / "b-20260101-000000.tar.gz")
    _touch_archive(saves / "configs" / "a" / "1" / "a-20260101-000000.tar.gz")
    archives = BackupManager(str(saves)).list_archives()
    assert archives == sorted(archives)
    assert len(set(archives)) == len(archives)


def test_saves_dir_is_private(tmp_path: Path) -> None:
    saves = tmp_path / "nested" / "saves"
    manager = BackupManager(str(saves))
    manager.ensure_saves_dir()
    assert stat.S_IMODE(saves.stat().st_mode) == 0o700


def test_batch_creates_private_dirs_and_archives(tmp_path: Path, fake_home: Path) -> None:
    data = fake_home / "data"
    data.mkdir()
    (data / "f.txt").write_text("x")

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "one.yaml", [str(data)])

    manager = BackupManager(str(tmp_path / "saves"))
    batch = manager.compress_directory_of_configs(str(cfg_dir), "20260101-000000")

    assert len(batch.created) == 1
    archive = Path(batch.created[0])
    assert archive.name == "one-20260101-000000.tar.gz"
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    # Every directory config-saver creates on the way is private too.
    for directory in (archive.parent, archive.parent.parent, archive.parent.parent.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_batch_accepts_json_configs(tmp_path: Path, fake_home: Path) -> None:
    """README advertises YAML *and* JSON; directory mode used to ignore .json."""
    data = fake_home / "data"
    data.mkdir()
    (data / "f.txt").write_text("x")

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "j.json").write_text(f'{{"directories": ["{data}"]}}')

    batch = BackupManager(str(tmp_path / "saves")).compress_directory_of_configs(str(cfg_dir), "20260101-000000")
    assert [Path(p).name for p in batch.created] == ["j-20260101-000000.tar.gz"]


def test_batch_skips_root_only_configs_by_type_not_message(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "ok.yaml", [str(fake_home)])
    (cfg_dir / "rooted.yaml").write_text(yaml.safe_dump({"directories": [str(fake_home)], "only_root_user": True}))
    monkeypatch.setattr("os.getuid", lambda: 1000)

    batch = BackupManager(str(tmp_path / "saves")).compress_directory_of_configs(str(cfg_dir), "20260101-000000")
    assert [Path(p).name for p in batch.skipped_root_only] == ["rooted.yaml"]
    assert len(batch.created) == 1
    assert batch.failures == []


def test_batch_reports_per_config_failures_without_aborting(tmp_path: Path, fake_home: Path) -> None:
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "good.yaml", [str(fake_home)])
    (cfg_dir / "bad.yaml").write_text(yaml.safe_dump({"directoriez": ["/tmp"]}))

    batch = BackupManager(str(tmp_path / "saves")).compress_directory_of_configs(str(cfg_dir), "20260101-000000")
    assert len(batch.created) == 1
    assert [o.name for o in batch.failures] == ["bad"]


def test_batch_missing_inputs_are_aggregated(tmp_path: Path, fake_home: Path) -> None:
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "one.yaml", [str(fake_home / "not-here")])

    batch = BackupManager(str(tmp_path / "saves")).compress_directory_of_configs(str(cfg_dir), "20260101-000000")
    assert batch.missing_inputs == [str(fake_home / "not-here")]


def test_parallel_batch_matches_sequential_output(tmp_path: Path, fake_home: Path) -> None:
    data = fake_home / "data"
    data.mkdir()
    for i in range(3):
        (data / f"f{i}.txt").write_text("x" * 100)

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    for name in ("a", "b", "c"):
        write_config(cfg_dir / f"{name}.yaml", [str(data)])

    sequential = BackupManager(str(tmp_path / "s1")).compress_directory_of_configs(str(cfg_dir), "20260101-000000")
    parallel = BackupManager(str(tmp_path / "s2")).compress_directory_of_configs(
        str(cfg_dir), "20260101-000000", jobs=3
    )

    # Output order follows the config filename order regardless of who finishes first.
    assert [Path(p).name for p in parallel.created] == [Path(p).name for p in sequential.created]
    assert [o.name for o in parallel.outcomes] == ["a", "b", "c"]


def test_description_is_written_privately(tmp_path: Path, fake_home: Path) -> None:
    data = fake_home / "data"
    data.mkdir()
    (data / "f").write_text("x")
    cfg = write_config(tmp_path / "c.yaml", [str(data)])

    manager = BackupManager(str(tmp_path / "saves"))
    archive, _result = manager.compress_config_to_timestamp_dir(
        str(cfg), str(tmp_path / "saves" / "configs" / "c"), "20260101-000000", description="nightly"
    )
    desc = Path(archive).parent / "description.txt"
    assert desc.read_text() == "nightly"
    assert stat.S_IMODE(desc.stat().st_mode) == 0o600
    assert manager.get_description_for_archive(archive) == "nightly"


def test_empty_config_directory_raises(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        BackupManager(str(tmp_path / "saves")).compress_directory_of_configs(str(empty), "20260101-000000")


def test_find_config_files_is_sorted_and_top_level_only(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "configs"
    (cfg_dir / "sub").mkdir(parents=True)
    for name in ("z.yaml", "a.yml", "m.json", "ignored.txt"):
        (cfg_dir / name).write_text("directories: []\n")
    (cfg_dir / "sub" / "nested.yaml").write_text("directories: []\n")

    found = [os.path.basename(p) for p in BackupManager(str(tmp_path)).find_config_files(str(cfg_dir))]
    assert found == ["a.yml", "m.json", "z.yaml"]


def test_worker_count_is_capped_at_the_number_of_configs(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process pool for a single configuration is measurably slower than doing
    the work inline (docs/BENCHMARKS.md), so it must not be created."""
    data = fake_home / "data"
    data.mkdir()
    (data / "f.txt").write_text("x")
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "only.yaml", [str(data)])

    def fail(*_args, **_kwargs):
        raise AssertionError("ProcessPoolExecutor must not be used for a single configuration")

    monkeypatch.setattr("config_saver.lib.backup_manager.backup_manager.ProcessPoolExecutor", fail)
    batch = BackupManager(str(tmp_path / "saves")).compress_directory_of_configs(
        str(cfg_dir), "20260101-000000", jobs=8
    )
    assert len(batch.created) == 1


# ------------------------------------------------- restic store and pruning


class FakeStore:
    """Records what the manager asks of the store; nothing touches restic."""

    def __init__(self, forgotten: dict[str, list[Snapshot]] | None = None) -> None:
        self.backups: list[tuple[str, datetime, str | None]] = []
        self.forgets: list[tuple[str, RetentionModel, bool]] = []
        self.prunes = 0
        self.also_keep: dict[str, list[datetime]] = {}
        self.forgotten = forgotten or {}
        self.fail_forget: set[str] = set()
        self.fail_prune = False

    def backup(self, write: Callable[[IO[bytes]], CompressResult], cfg: str, when: datetime, description: str | None):
        buffer = io.BytesIO()
        result = write(buffer)
        buffer.seek(0)
        with tarfile.open(fileobj=buffer) as tar:
            assert tar.getmembers(), "a real tar stream was written"
        self.backups.append((cfg, when, description))
        snapshot = Snapshot(id=f"{cfg}-id", short_id=f"{cfg}-sh", time=when, cfg=cfg, description=description)
        return snapshot, result, 123

    def forget(
        self, cfg: str, policy: RetentionModel, dry_run: bool, also_keep: Iterable[datetime] = ()
    ) -> list[Snapshot]:
        self.forgets.append((cfg, policy, dry_run))
        self.also_keep[cfg] = list(also_keep)
        if cfg in self.fail_forget:
            raise StoreError(f"forget failed for {cfg}")
        return self.forgotten.get(cfg, [])

    def prune(self) -> None:
        self.prunes += 1
        if self.fail_prune:
            raise StoreError("prune failed")


def _snap(cfg: str, n: int) -> Snapshot:
    return Snapshot(id=f"{cfg}-old{n}", short_id=f"old{n}", time=datetime(2026, 1, n), cfg=cfg, description=None)


def _two_configs(tmp_path: Path, fake_home: Path, **second: object) -> Path:
    data = fake_home / "data"
    data.mkdir()
    (data / "f.txt").write_text("x")
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "one.yaml", [str(data)])
    write_config(cfg_dir / "two.yaml", [str(data)], **second)
    return cfg_dir


def test_batch_uses_store_when_given(tmp_path: Path, fake_home: Path) -> None:
    cfg_dir = _two_configs(tmp_path, fake_home)
    saves = tmp_path / "saves"
    store = FakeStore()

    batch = BackupManager(str(saves)).compress_directory_of_configs(
        str(cfg_dir),
        "20260102-030405",
        description="why",
        store=store,  # type: ignore[arg-type]
    )

    assert store.backups == [
        ("one", datetime(2026, 1, 2, 3, 4, 5), "why"),
        ("two", datetime(2026, 1, 2, 3, 4, 5), "why"),
    ]
    assert [o.snapshot.short_id for o in batch.outcomes if o.snapshot] == ["one-sh", "two-sh"]
    assert batch.created == []  # nothing was written as a .tar.gz
    assert batch.failures == []
    assert not (saves / "configs").exists() or not list((saves / "configs").rglob("*.tar.gz"))


def test_store_job_applies_extra_directories(tmp_path: Path, fake_home: Path) -> None:
    cfg_dir = _two_configs(tmp_path, fake_home)
    extra = fake_home / "extra"
    extra.mkdir()
    (extra / "e.txt").write_text("e")
    seen: list[str] = []

    class Spy(FakeStore):
        def backup(self, write, cfg, when, description):  # type: ignore[no-untyped-def]
            buffer = io.BytesIO()
            result = write(buffer)
            buffer.seek(0)
            with tarfile.open(fileobj=buffer) as tar:
                seen.extend(m.name for m in tar.getmembers() if m.name.endswith("e.txt"))
            return super().backup(lambda _s: result, cfg, when, description)

    BackupManager(str(tmp_path / "saves")).compress_directory_of_configs(
        str(cfg_dir),
        "20260102-030405",
        store=Spy(),
        extra_directories=(str(extra),),  # type: ignore[arg-type]
    )
    assert len(seen) == 2


def test_store_error_is_that_configs_outcome_and_the_batch_continues(tmp_path: Path, fake_home: Path) -> None:
    cfg_dir = _two_configs(tmp_path, fake_home)

    class Flaky(FakeStore):
        def backup(self, write, cfg, when, description):  # type: ignore[no-untyped-def]
            if cfg == "one":
                raise StoreError("restic failed: boom")
            return super().backup(write, cfg, when, description)

    batch = BackupManager(str(tmp_path / "saves")).compress_directory_of_configs(
        str(cfg_dir),
        "20260102-030405",
        store=Flaky(),  # type: ignore[arg-type]
    )
    assert [o.name for o in batch.failures] == ["one"]
    assert "boom" in (batch.failures[0].error or "")
    assert batch.outcomes[1].snapshot is not None


def _timestamp_dirs(saves: Path, name: str, count: int) -> None:
    for i in range(count):
        stamp = datetime(2026, 1, 1) + timedelta(days=i)
        directory = saves / "configs" / name / stamp.strftime("%Y%m%d-%H%M%S")
        directory.mkdir(parents=True)
        (directory / f"{name}.tar.gz").write_bytes(b"x")


def test_tar_mode_without_retention_prunes_nothing(tmp_path: Path, fake_home: Path) -> None:
    cfg_dir = _two_configs(tmp_path, fake_home)
    saves = tmp_path / "saves"
    manager = BackupManager(str(saves))
    _timestamp_dirs(saves, "one", 55)
    _timestamp_dirs(saves, "two", 55)
    batch = manager.compress_directory_of_configs(str(cfg_dir), "20270101-000000")

    report = manager.prune_after_batch(batch, None)

    assert report.removed == {} and report.errors == []
    assert len(list((saves / "configs" / "one").iterdir())) == 56
    assert all(o.pruned == [] for o in batch.outcomes)


def test_tar_mode_with_retention_prunes(tmp_path: Path, fake_home: Path) -> None:
    policy = {"daily": 3, "weekly": 0, "monthly": 0, "yearly": 0}
    cfg_dir = _two_configs(tmp_path, fake_home, retention=policy)
    saves = tmp_path / "saves"
    manager = BackupManager(str(saves))
    _timestamp_dirs(saves, "one", 55)
    _timestamp_dirs(saves, "two", 55)
    batch = manager.compress_directory_of_configs(str(cfg_dir), "20270101-000000")

    report = manager.prune_after_batch(batch, None)

    stamps = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(55)] + [datetime(2027, 1, 1)]
    keep = keep_set(stamps, RetentionModel(**policy))
    expected = {str(saves / "configs" / "two" / s.strftime("%Y%m%d-%H%M%S")) for s in stamps if s not in keep}
    assert set(report.removed) == {"two"}
    assert set(report.removed["two"]) == expected and len(expected) == 53
    assert len(list((saves / "configs" / "one").iterdir())) == 56  # no block: untouched
    assert (saves / "configs" / "two" / "20270101-000000").is_dir()
    assert set(batch.outcomes[1].pruned) == expected


def test_tar_prune_skips_failed_configs(tmp_path: Path, fake_home: Path) -> None:
    cfg_dir = _two_configs(tmp_path, fake_home, retention={"daily": 1, "weekly": 0, "monthly": 0, "yearly": 0})
    saves = tmp_path / "saves"
    manager = BackupManager(str(saves))
    _timestamp_dirs(saves, "two", 5)
    batch = manager.compress_directory_of_configs(str(cfg_dir), "20270101-000000")
    batch.outcomes[1].error = "RuntimeError: it failed"

    report = manager.prune_after_batch(batch, None)
    assert report.removed == {}
    assert len(list((saves / "configs" / "two").iterdir())) == 6


def _ok_batch(tmp_path: Path, fake_home: Path, store: FakeStore, **second: object):  # type: ignore[no-untyped-def]
    cfg_dir = _two_configs(tmp_path, fake_home, **second)
    manager = BackupManager(str(tmp_path / "saves"))
    batch = manager.compress_directory_of_configs(str(cfg_dir), "20270101-000000", store=store)  # type: ignore[arg-type]
    return manager, batch


def test_restic_mode_prunes_with_defaults(tmp_path: Path, fake_home: Path) -> None:
    store = FakeStore({"one": [_snap("one", 1), _snap("one", 2)]})
    custom = {"daily": 1, "weekly": 0, "monthly": 0, "yearly": 0}
    manager, batch = _ok_batch(tmp_path, fake_home, store, retention=custom)

    report = manager.prune_after_batch(batch, store)  # type: ignore[arg-type]

    assert store.forgets == [("one", RetentionModel(), False), ("two", RetentionModel(**custom), False)]
    assert store.prunes == 1
    assert report.removed == {"one": ["old1", "old2"]}
    assert batch.outcomes[0].pruned == ["old1", "old2"]
    assert report.errors == []


def test_restic_mode_does_not_prune_when_nothing_was_forgotten(tmp_path: Path, fake_home: Path) -> None:
    store = FakeStore()
    manager, batch = _ok_batch(tmp_path, fake_home, store)
    report = manager.prune_after_batch(batch, store)  # type: ignore[arg-type]
    assert store.prunes == 0
    assert report.removed == {} and report.errors == []


def test_restic_mode_leaves_failed_and_skipped_configs_alone(tmp_path: Path, fake_home: Path) -> None:
    store = FakeStore({"one": [_snap("one", 1)]})
    manager, batch = _ok_batch(tmp_path, fake_home, store)
    batch.outcomes[0].error = "StoreError: boom"
    batch.outcomes[1].skipped_root_only = True
    batch.outcomes[1].snapshot = None

    report = manager.prune_after_batch(batch, store)  # type: ignore[arg-type]
    assert store.forgets == [] and store.prunes == 0
    assert report.removed == {}


def test_restic_forget_error_is_reported_and_others_still_run(tmp_path: Path, fake_home: Path) -> None:
    store = FakeStore({"two": [_snap("two", 1)]})
    store.fail_forget.add("one")
    manager, batch = _ok_batch(tmp_path, fake_home, store)

    report = manager.prune_after_batch(batch, store)  # type: ignore[arg-type]

    assert [c for c, _p, _d in store.forgets] == ["one", "two"]
    assert report.removed == {"two": ["old1"]}
    assert store.prunes == 1
    assert len(report.errors) == 1 and "one" in report.errors[0] and "forget failed" in report.errors[0]
    assert batch.failures == []  # the backups themselves are untouched


def test_restic_prune_error_is_reported_not_raised(tmp_path: Path, fake_home: Path) -> None:
    store = FakeStore({"one": [_snap("one", 1)]})
    store.fail_prune = True
    manager, batch = _ok_batch(tmp_path, fake_home, store)

    report = manager.prune_after_batch(batch, store)  # type: ignore[arg-type]

    assert report.removed == {"one": ["old1"]}
    assert len(report.errors) == 1 and "prune failed" in report.errors[0]
    assert batch.failures == []


def test_tar_batch_forwards_extra_directories(tmp_path: Path, fake_home: Path) -> None:
    """--include-system-configs was accepted by the batch path and then dropped."""
    cfg_dir = _two_configs(tmp_path, fake_home)
    extra = fake_home / "extra"
    extra.mkdir()
    (extra / "e.txt").write_text("e")

    batch = BackupManager(str(tmp_path / "saves")).compress_directory_of_configs(
        str(cfg_dir), "20260102-030405", extra_directories=(str(extra),)
    )
    for archive in batch.created:
        with tarfile.open(archive, "r:gz") as tar:
            assert any(m.name.endswith("extra/e.txt") for m in tar.getmembers())


def test_timestamp_dir_forwards_extra_directories(tmp_path: Path, fake_home: Path) -> None:
    cfg_dir = _two_configs(tmp_path, fake_home)
    extra = fake_home / "extra"
    extra.mkdir()
    (extra / "e.txt").write_text("e")

    out, _ = BackupManager(str(tmp_path / "saves")).compress_config_to_timestamp_dir(
        str(cfg_dir / "one.yaml"), str(tmp_path / "base"), "20260102-030405", extra_directories=(str(extra),)
    )
    with tarfile.open(out, "r:gz") as tar:
        assert any(m.name.endswith("extra/e.txt") for m in tar.getmembers())


def test_failed_job_leaves_no_empty_dir_and_the_real_backup_survives_prune(tmp_path: Path, fake_home: Path) -> None:
    """The reviewer's sequence: ok, failed, ok with daily=2 must not eat the only Jan 1 backup."""
    data = fake_home / "data"
    data.mkdir()
    (data / "f.txt").write_text("x")
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    cfg = cfg_dir / "one.yaml"
    policy = {"daily": 2, "weekly": 0, "monthly": 0, "yearly": 0}
    write_config(cfg, [str(data)], retention=policy)
    saves = tmp_path / "saves"
    manager = BackupManager(str(saves))

    manager.compress_directory_of_configs(str(cfg_dir), "20260101-030000")
    cfg.write_text("directoriez: [nope]\n")  # the second run fails
    failed = manager.compress_directory_of_configs(str(cfg_dir), "20260101-150000")
    assert [o.name for o in failed.failures] == ["one"]
    assert not (saves / "configs" / "one" / "20260101-150000").exists()
    write_config(cfg, [str(data)], retention=policy)
    batch = manager.compress_directory_of_configs(str(cfg_dir), "20260102-030000")

    report = manager.prune_after_batch(batch, None)

    assert report.removed == {}
    assert sorted(p.name for p in (saves / "configs" / "one").iterdir()) == ["20260101-030000", "20260102-030000"]


def test_root_only_skip_leaves_no_empty_dir(tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "rooted.yaml").write_text(yaml.safe_dump({"directories": [str(fake_home)], "only_root_user": True}))
    monkeypatch.setattr("os.getuid", lambda: 1000)
    saves = tmp_path / "saves"
    BackupManager(str(saves)).compress_directory_of_configs(str(cfg_dir), "20260101-000000")
    assert not (saves / "configs" / "rooted" / "20260101-000000").exists()


def test_legacy_config_is_never_auto_pruned(tmp_path: Path, fake_home: Path) -> None:
    data = fake_home / "data"
    data.mkdir()
    (data / "f.txt").write_text("x")
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    zero = {"daily": 0, "weekly": 0, "monthly": 0, "yearly": 0}
    write_config(cfg_dir / "legacy.yaml", [str(data)], retention=zero)
    saves = tmp_path / "saves"
    manager = BackupManager(str(saves))
    _timestamp_dirs(saves, "legacy", 4)

    tar_batch = manager.compress_directory_of_configs(str(cfg_dir), "20270101-000000")
    assert manager.prune_after_batch(tar_batch, None).removed == {}
    assert len(list((saves / "configs" / "legacy").iterdir())) == 5

    store = FakeStore({"legacy": [_snap("legacy", 1)]})
    restic_batch = manager.compress_directory_of_configs(str(cfg_dir), "20270102-000000", store=store)  # type: ignore[arg-type]
    assert manager.prune_after_batch(restic_batch, store).removed == {}  # type: ignore[arg-type]
    assert store.forgets == [] and store.prunes == 0


def test_tar_prune_never_removes_this_runs_backup_under_clock_skew(tmp_path: Path, fake_home: Path) -> None:
    zero = {"daily": 0, "weekly": 0, "monthly": 0, "yearly": 0}
    cfg_dir = _two_configs(tmp_path, fake_home, retention=zero)
    saves = tmp_path / "saves"
    manager = BackupManager(str(saves))
    _timestamp_dirs(saves, "two", 1)  # 2026-01-01, then a run stamped *earlier* than a newer existing one
    (saves / "configs" / "two" / "20990101-000000").mkdir()
    (saves / "configs" / "two" / "20990101-000000" / "two.tar.gz").write_bytes(b"x")
    batch = manager.compress_directory_of_configs(str(cfg_dir), "20270101-000000")

    manager.prune_after_batch(batch, None)

    assert (saves / "configs" / "two" / "20270101-000000").is_dir()


def test_restic_prune_passes_this_runs_snapshot_as_also_keep(tmp_path: Path, fake_home: Path) -> None:
    store = FakeStore()
    manager, batch = _ok_batch(tmp_path, fake_home, store)
    manager.prune_after_batch(batch, store)  # type: ignore[arg-type]
    assert store.also_keep == {"one": [datetime(2027, 1, 1)], "two": [datetime(2027, 1, 1)]}


def test_job_errors_use_the_same_wording_as_prune_errors(tmp_path: Path, fake_home: Path) -> None:
    cfg_dir = _two_configs(tmp_path, fake_home)

    class Flaky(FakeStore):
        def backup(self, write, cfg, when, description):  # type: ignore[no-untyped-def]
            raise StoreError("restic failed: boom")

    batch = BackupManager(str(tmp_path / "saves")).compress_directory_of_configs(
        str(cfg_dir),
        "20260102-030405",
        store=Flaky(),  # type: ignore[arg-type]
    )
    assert batch.failures[0].error == "restic failed: boom"


def _leave_archive(tmp_path: Path, cfg: str) -> None:
    stamp = tmp_path / "saves" / "configs" / cfg / "20260101-000000"
    stamp.mkdir(parents=True)
    (stamp / f"{cfg}-20260101-000000.tar.gz.age").write_bytes(b"x")


def test_restic_prune_is_deferred_while_unmigrated_archives_remain(tmp_path: Path, fake_home: Path) -> None:
    store = FakeStore({"one": [_snap("one", 1)], "two": [_snap("two", 1)]})
    manager, batch = _ok_batch(tmp_path, fake_home, store)
    _leave_archive(tmp_path, "one")

    report = manager.prune_after_batch(batch, store)  # type: ignore[arg-type]

    assert [c for c, _p, _d in store.forgets] == ["two"]  # "one" is not pruned
    assert report.deferred == ["one"]
    assert report.removed == {"two": ["old1"]}


def test_restic_prune_runs_normally_once_no_archive_remains(tmp_path: Path, fake_home: Path) -> None:
    store = FakeStore({"one": [_snap("one", 1)]})
    manager, batch = _ok_batch(tmp_path, fake_home, store)
    (tmp_path / "saves" / "configs" / "one" / "20260101-000000").mkdir(parents=True)  # empty dir: not an archive

    report = manager.prune_after_batch(batch, store)  # type: ignore[arg-type]

    assert [c for c, _p, _d in store.forgets] == ["one", "two"]
    assert report.deferred == []


def test_tar_prune_is_not_deferred_by_archives(tmp_path: Path, fake_home: Path) -> None:
    store = FakeStore()
    manager, batch = _ok_batch(
        tmp_path, fake_home, store, retention={"daily": 1, "weekly": 0, "monthly": 0, "yearly": 0}
    )
    _leave_archive(tmp_path, "two")
    report = manager.prune_after_batch(batch, None)
    assert report.deferred == []
