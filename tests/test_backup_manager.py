"""BackupManager: archive discovery, batch mode, permissions, parallelism."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

from config_saver.lib.backup_manager.backup_manager import BackupManager

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
