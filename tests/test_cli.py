"""CLI behaviour and, above all, exit codes (documented in README.md)."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from config_saver.lib import paths
from config_saver.lib.backup_manager.backup_manager import BackupManager
from config_saver.lib.cli.cli import (
    CLI,
    EXIT_ENCRYPTION,
    EXIT_INCOMPLETE,
    EXIT_NO_SUCH_CONFIG,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_PERMISSION,
    EXIT_RUNTIME,
    EXIT_USAGE,
    EXIT_VALIDATION,
    BackupTable,
)
from config_saver.lib.errors import StoreError
from config_saver.lib.restic_store import NOTICES, Snapshot, StorageMode

from .conftest import write_config


@pytest.fixture
def data_dir(fake_home: Path) -> Path:
    data = fake_home / "data"
    data.mkdir()
    (data / "f.txt").write_text("hello\n", encoding="utf-8")
    return data


def run(argv: list[str]) -> int:
    return CLI(argv).run()


# ----------------------------------------------------------------- compress


def test_compress_single_config(tmp_path: Path, data_dir: Path) -> None:
    cfg = write_config(tmp_path / "c.yaml", [str(data_dir)])
    out = tmp_path / "out.tar.gz"
    assert run(["--compress", "--input", str(cfg), "--output", str(out)]) == EXIT_OK
    assert out.exists()


def test_compress_directory_of_configs(tmp_path: Path, data_dir: Path, fake_home: Path) -> None:
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "one.yaml", [str(data_dir)])
    assert run(["--compress", "--input", str(cfg_dir)]) == EXIT_OK
    archives = list((fake_home / ".config" / "config-saver" / "configs").rglob("*.tar.gz"))
    assert len(archives) == 1


def test_compress_directory_rejects_output(tmp_path: Path, data_dir: Path) -> None:
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "one.yaml", [str(data_dir)])
    assert run(["--compress", "--input", str(cfg_dir), "--output", str(tmp_path / "x.tar.gz")]) == EXIT_USAGE


def test_output_and_description_are_incompatible(tmp_path: Path, data_dir: Path) -> None:
    """--description stores the archive in a per-config dir, so --output was ignored."""
    cfg = write_config(tmp_path / "c.yaml", [str(data_dir)])
    out = tmp_path / "out.tar.gz"
    assert run(["--compress", "--input", str(cfg), "--output", str(out), "-m", "note"]) == EXIT_USAGE
    assert not out.exists()


def test_description_creates_a_timestamped_dir(tmp_path: Path, data_dir: Path, fake_home: Path) -> None:
    cfg = write_config(tmp_path / "zsh.yaml", [str(data_dir)])
    assert run(["--compress", "--input", str(cfg), "-m", "nightly"]) == EXIT_OK
    descriptions = list((fake_home / ".config" / "config-saver").rglob("description.txt"))
    assert [d.read_text() for d in descriptions] == ["nightly"]


def test_missing_config_file_exits_not_found(tmp_path: Path, fake_home: Path) -> None:
    assert run(["--compress", "--input", str(tmp_path / "nope.yaml")]) == EXIT_NOT_FOUND


def test_invalid_config_exits_validation(tmp_path: Path, fake_home: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(yaml.safe_dump({"directoriez": ["/tmp"]}))
    assert run(["--compress", "--input", str(cfg)]) == EXIT_VALIDATION


def test_root_only_config_exits_permission(tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "r.yaml"
    cfg.write_text(yaml.safe_dump({"directories": [str(fake_home)], "only_root_user": True}))
    monkeypatch.setattr("os.getuid", lambda: 1000)
    assert run(["--compress", "--input", str(cfg)]) == EXIT_PERMISSION


def test_strict_reports_missing_inputs(tmp_path: Path, fake_home: Path) -> None:
    cfg = write_config(tmp_path / "c.yaml", [str(fake_home / "not-here")])
    out = tmp_path / "out.tar.gz"
    assert run(["--compress", "--input", str(cfg), "--output", str(out)]) == EXIT_OK
    assert run(["--compress", "--input", str(cfg), "--output", str(out), "--strict"]) == EXIT_INCOMPLETE


def test_jobs_requires_directory_mode(tmp_path: Path, data_dir: Path) -> None:
    cfg = write_config(tmp_path / "c.yaml", [str(data_dir)])
    assert run(["--compress", "--input", str(cfg), "--jobs", "4"]) == EXIT_USAGE


def test_invalid_jobs_value(tmp_path: Path, data_dir: Path) -> None:
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "one.yaml", [str(data_dir)])
    assert run(["--compress", "--input", str(cfg_dir), "--jobs", "zero"]) == EXIT_USAGE
    assert run(["--compress", "--input", str(cfg_dir), "--jobs", "0"]) == EXIT_USAGE


def test_no_config_directory_available_is_actionable(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With nothing configured the run must say what to do, not back up whatever
    examples happen to be installed."""
    monkeypatch.setattr(paths, "SYSTEM_CONFIG_DIR", str(tmp_path / "absent"))
    monkeypatch.setattr(paths, "example_config_dir", lambda: str(tmp_path / "examples"))
    assert run(["--compress"]) == EXIT_USAGE
    out = capsys.readouterr().out
    assert "No configurations found." in out
    assert "configs.d" in out


# --------------------------------------------------------------- decompress


def test_decompress_roundtrip(tmp_path: Path, data_dir: Path) -> None:
    cfg = write_config(tmp_path / "c.yaml", [str(data_dir)])
    archive = tmp_path / "out.tar.gz"
    assert run(["--compress", "--input", str(cfg), "--output", str(archive)]) == EXIT_OK

    dest = tmp_path / "restored"
    assert run(["--decompress", "--input", str(archive), "--output", str(dest)]) == EXIT_OK
    assert (dest / "home" / "user" / "data" / "f.txt").read_text() == "hello\n"


def test_decompress_missing_archive(tmp_path: Path, fake_home: Path) -> None:
    assert run(["--decompress", "--input", str(tmp_path / "nope.tar.gz")]) == EXIT_NOT_FOUND


def test_decompress_corrupt_archive(tmp_path: Path, fake_home: Path) -> None:
    corrupt = tmp_path / "c.tar.gz"
    corrupt.write_bytes(b"not gzip")
    assert run(["--decompress", "--input", str(corrupt)]) == EXIT_RUNTIME


def test_decompress_malicious_archive_exits_runtime(tmp_path: Path, fake_home: Path) -> None:
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("../../escaped.txt")
        payload = b"PWNED"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    dest = tmp_path / "out" / "a"
    dest.mkdir(parents=True)

    assert run(["--decompress", "--input", str(archive), "--output", str(dest)]) == EXIT_RUNTIME
    assert not (tmp_path / "out" / "escaped.txt").exists()


def test_decompress_requires_input(fake_home: Path) -> None:
    assert run(["--decompress"]) == EXIT_USAGE


# ------------------------------------------------------ listing and exports


def _make_saved_archive(fake_home: Path, name: str, ts: str) -> Path:
    directory = fake_home / ".config" / "config-saver" / "configs" / name / ts
    directory.mkdir(parents=True)
    archive = directory / f"{name}-{ts}.tar.gz"
    with tarfile.open(archive, "w:gz"):
        pass
    (directory / "description.txt").write_text(f"desc for {name}")
    return archive


def test_show_configs(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _make_saved_archive(fake_home, "zsh", "20260101-000000")
    _make_saved_archive(fake_home, "kde", "20260102-000000")
    assert run(["--show-configs"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "- zsh" in out and "- kde" in out


def test_show_configs_when_empty(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["--show-configs"]) == EXIT_OK
    assert "No saved configurations found." in capsys.readouterr().out


def test_list_uses_the_filename_timestamp_not_the_mtime(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive = _make_saved_archive(fake_home, "zsh", "20260101-120000")
    # Simulate a copied/synced file whose mtime no longer matches the backup date.
    os.utime(archive, (0, 0))
    assert run(["--list"]) == EXIT_OK
    assert "2026-01-01 12:00:00" in capsys.readouterr().out


def test_list_shows_descriptions(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _make_saved_archive(fake_home, "zsh", "20260101-120000")
    assert run(["--list"]) == EXIT_OK
    assert "desc for zsh" in capsys.readouterr().out


def test_list_without_archives(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["--list"]) == EXIT_OK
    assert "No config-saver tar.gz files found" in capsys.readouterr().out


def test_parse_ts_reads_group_two(tmp_path: Path) -> None:
    table = BackupTable(str(tmp_path))
    archive = tmp_path / "zsh-20260811-120000.tar.gz"
    archive.write_bytes(b"")
    os.utime(archive, (0, 0))
    assert table._parse_ts(str(archive)) == datetime(2026, 8, 11, 12, 0, 0)


def test_export_config(fake_home: Path, tmp_path: Path) -> None:
    _make_saved_archive(fake_home, "zsh", "20260101-000000")
    latest = _make_saved_archive(fake_home, "zsh", "20260301-000000")
    dest = tmp_path / "exported.tar.gz"
    assert run(["--export-config", "zsh", "--output", str(dest)]) == EXIT_OK
    assert dest.read_bytes() == latest.read_bytes()


def test_export_config_unknown_name(fake_home: Path) -> None:
    assert run(["--export-config", "nope"]) == EXIT_NO_SUCH_CONFIG


def test_export_all_configs(fake_home: Path, tmp_path: Path) -> None:
    _make_saved_archive(fake_home, "zsh", "20260101-000000")
    _make_saved_archive(fake_home, "kde", "20260101-000000")
    dest = tmp_path / "exports"
    assert run(["--export-all-configs", "--output", str(dest)]) == EXIT_OK
    assert sorted(p.name for p in dest.iterdir()) == [
        "kde-20260101-000000.tar.gz",
        "zsh-20260101-000000.tar.gz",
    ]


def test_export_all_configs_when_empty(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["--export-all-configs"]) == EXIT_OK
    assert "No saved configurations found." in capsys.readouterr().out


# ----------------------------------------------------------------- process


def test_version_and_help_via_subprocess() -> None:
    for flag in ("--version", "--help"):
        proc = subprocess.run([sys.executable, "-m", "config_saver", flag], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip()


def test_no_action_is_a_usage_error() -> None:
    proc = subprocess.run([sys.executable, "-m", "config_saver"], capture_output=True, text=True, check=False)
    assert proc.returncode == 2  # argparse's own usage exit code


def test_directory_mode_is_parallel_by_default(tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Parallel batch compression is a 2-3x win whenever there is more than one
    configuration (docs/BENCHMARKS.md), so it is the default."""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "one.yaml", [str(data_dir)])
    seen: dict[str, int] = {}

    original = BackupManager.compress_config_files

    def spy(self, cfg_files, timestamp, **kwargs):
        seen["jobs"] = kwargs.get("jobs", 1)
        return original(self, cfg_files, timestamp, **kwargs)

    monkeypatch.setattr(BackupManager, "compress_config_files", spy)
    assert run(["--compress", "--input", str(cfg_dir)]) == EXIT_OK
    assert seen["jobs"] == (os.cpu_count() or 1)


def test_progress_falls_back_to_sequential(tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Several workers and a per-file progress bar are unreadable together."""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "one.yaml", [str(data_dir)])
    seen: dict[str, int] = {}

    original = BackupManager.compress_config_files

    def spy(self, cfg_files, timestamp, **kwargs):
        seen["jobs"] = kwargs.get("jobs", 1)
        return original(self, cfg_files, timestamp, **kwargs)

    monkeypatch.setattr(BackupManager, "compress_config_files", spy)
    assert run(["--compress", "--progress", "--input", str(cfg_dir)]) == EXIT_OK
    assert seen["jobs"] == 1

    # Asking for both explicitly is still honoured.
    assert run(["--compress", "--progress", "--jobs", "2", "--input", str(cfg_dir)]) == EXIT_OK
    assert seen["jobs"] == 2


# ------------------------------------------------- several config directories


def test_several_config_directories_are_combined(tmp_path: Path, data_dir: Path, fake_home: Path) -> None:
    """A private repo of personal configs alongside the system directory."""
    system_dir = tmp_path / "system"
    personal_dir = tmp_path / "personal"
    system_dir.mkdir()
    personal_dir.mkdir()
    write_config(system_dir / "zsh.yaml", [str(data_dir)])
    write_config(personal_dir / "work.yaml", [str(data_dir)])
    write_config(personal_dir / "laptop.yaml", [str(data_dir)])

    assert run(["--compress", "-i", str(system_dir), "-i", str(personal_dir)]) == EXIT_OK
    archives = sorted(p.name for p in (fake_home / ".config" / "config-saver" / "configs").rglob("*.tar.gz"))
    assert [name.split("-")[0] for name in archives] == ["laptop", "work", "zsh"]


def test_a_name_defined_twice_is_refused(tmp_path: Path, data_dir: Path, fake_home: Path) -> None:
    """Both would write to <saves>/configs/<name>/<timestamp>/ and the second
    would quietly overwrite the first."""
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    write_config(first / "zsh.yaml", [str(data_dir)])
    write_config(second / "zsh.yaml", [str(data_dir)])

    assert run(["--compress", "-i", str(first), "-i", str(second)]) == EXIT_USAGE


def test_an_empty_extra_directory_is_reported(tmp_path: Path, data_dir: Path, fake_home: Path) -> None:
    good = tmp_path / "good"
    empty = tmp_path / "empty"
    good.mkdir()
    empty.mkdir()
    write_config(good / "one.yaml", [str(data_dir)])

    assert run(["--compress", "-i", str(good), "-i", str(empty)]) == EXIT_USAGE


def test_mixing_a_file_and_a_directory_is_refused(tmp_path: Path, data_dir: Path, fake_home: Path) -> None:
    cfg_dir = tmp_path / "dir"
    cfg_dir.mkdir()
    write_config(cfg_dir / "one.yaml", [str(data_dir)])
    single = write_config(tmp_path / "single.yaml", [str(data_dir)])

    assert run(["--compress", "-i", str(cfg_dir), "-i", str(single)]) == EXIT_USAGE


def test_decompress_takes_a_single_archive(tmp_path: Path, data_dir: Path) -> None:
    cfg = write_config(tmp_path / "c.yaml", [str(data_dir)])
    archive = tmp_path / "a.tar.gz"
    assert run(["--compress", "-i", str(cfg), "-o", str(archive)]) == EXIT_OK
    assert run(["--decompress", "-i", str(archive), "-i", str(archive), "-o", str(tmp_path / "out")]) == EXIT_USAGE


# ------------------------------------------------- restic store and pruning


class _CliFakeStore:
    """Stands in for ResticStore; ``instances`` records every one the CLI built."""

    instances: list[_CliFakeStore] = []

    def __init__(self, saves_dir: str, identity: str | None = None) -> None:
        self.saves_dir = saves_dir
        self.backups: list[tuple[str, datetime, str | None]] = []
        self.forgets: list[str] = []
        self.prunes = 0
        self.forgotten: list[Snapshot] = []
        self.fail_prune = False
        _CliFakeStore.instances.append(self)

    def backup(self, write, cfg, when, description):  # type: ignore[no-untyped-def]
        result = write(io.BytesIO())
        self.backups.append((cfg, when, description))
        return Snapshot(f"{cfg}-id", f"{cfg}-sh", when, cfg, description), result, 1

    def forget(self, cfg, policy, dry_run, also_keep=()):  # type: ignore[no-untyped-def]
        self.forgets.append(cfg)
        return self.forgotten

    def prune(self) -> None:
        self.prunes += 1
        if self.fail_prune:
            raise StoreError("prune exploded")


def _mode(monkeypatch: pytest.MonkeyPatch, mode: StorageMode) -> None:
    _CliFakeStore.instances = []
    monkeypatch.setattr("config_saver.lib.cli.cli.select_mode", lambda _saves: mode)
    monkeypatch.setattr("config_saver.lib.cli.cli.ResticStore", _CliFakeStore)


def _cfg_dir(tmp_path: Path, data_dir: Path, **extra: object) -> Path:
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "one.yaml", [str(data_dir)])
    write_config(cfg_dir / "two.yaml", [str(data_dir)], **extra)
    return cfg_dir


def test_restic_mode_stores_snapshots_and_writes_no_archives(
    tmp_path: Path, data_dir: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mode(monkeypatch, StorageMode.RESTIC)
    cfg_dir = _cfg_dir(tmp_path, data_dir)

    assert run(["--compress", "-i", str(cfg_dir), "--jobs", "1"]) == EXIT_OK

    store = _CliFakeStore.instances[0]
    assert [b[0] for b in store.backups] == ["one", "two"]
    assert not list((fake_home / ".config" / "config-saver").rglob("*.tar.gz"))
    out = capsys.readouterr()
    assert "one-sh" in out.out and "two-sh" in out.out
    assert out.err == ""  # restic mode prints no notice


@pytest.mark.parametrize("mode", [StorageMode.TAR_NO_RESTIC, StorageMode.TAR_NOT_INITIALISED])
def test_notice_printed_once_in_tar_modes(
    tmp_path: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: StorageMode,
) -> None:
    _mode(monkeypatch, mode)
    cfg_dir = _cfg_dir(tmp_path, data_dir)

    assert run(["--compress", "-i", str(cfg_dir), "--jobs", "1"]) == EXIT_OK

    captured = capsys.readouterr()
    assert captured.err.count(NOTICES[mode]) == 1
    assert NOTICES[mode] not in captured.out


def test_notice_is_not_printed_for_other_verbs(
    data_dir: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mode(monkeypatch, StorageMode.TAR_NO_RESTIC)
    assert run(["--list"]) == EXIT_OK
    assert capsys.readouterr().err == ""


def test_store_error_from_select_mode_is_exit_runtime_in_red(
    tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(_saves: str) -> StorageMode:
        raise StoreError("restic store is incomplete: password file is missing")

    monkeypatch.setattr("config_saver.lib.cli.cli.select_mode", broken)
    cfg_dir = _cfg_dir(tmp_path, data_dir)

    assert run(["--compress", "-i", str(cfg_dir)]) == EXIT_RUNTIME
    assert "restic store is incomplete" in capsys.readouterr().out


@pytest.mark.parametrize("extra", [["--encrypt-to", "age1abc"], ["--encrypt-method", "gpg"]])
def test_encrypt_flags_are_rejected_in_restic_mode(
    tmp_path: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
) -> None:
    _mode(monkeypatch, StorageMode.RESTIC)
    cfg_dir = _cfg_dir(tmp_path, data_dir)

    assert run(["--compress", "-i", str(cfg_dir), *extra]) == EXIT_USAGE
    assert "always encrypted" in capsys.readouterr().out
    assert [b for s in _CliFakeStore.instances for b in s.backups] == []


def test_single_file_without_output_goes_to_the_store(
    tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mode(monkeypatch, StorageMode.RESTIC)
    cfg = write_config(tmp_path / "solo.yaml", [str(data_dir)])

    assert run(["--compress", "-i", str(cfg), "-m", "hello"]) == EXIT_OK

    store = _CliFakeStore.instances[0]
    assert [(b[0], b[2]) for b in store.backups] == [("solo", "hello")]
    assert store.forgets == [] and store.prunes == 0  # single-file is never pruned
    assert "solo-sh" in capsys.readouterr().out


def test_single_file_output_still_writes_targz_in_restic_mode(
    tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, StorageMode.RESTIC)
    cfg = write_config(tmp_path / "solo.yaml", [str(data_dir)])
    out = tmp_path / "named.tar.gz"

    assert run(["--compress", "-i", str(cfg), "-o", str(out)]) == EXIT_OK

    assert out.is_file()
    assert [b for s in _CliFakeStore.instances for b in s.backups] == []


def test_restic_batch_prunes_and_reports_it(
    tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mode(monkeypatch, StorageMode.RESTIC)
    cfg_dir = _cfg_dir(tmp_path, data_dir)
    original = _CliFakeStore.__init__

    def init(self: _CliFakeStore, saves_dir: str, identity: str | None = None) -> None:
        original(self, saves_dir, identity)
        self.forgotten = [Snapshot("old-id", "old1", datetime(2026, 1, 1), "x", None)]

    monkeypatch.setattr(_CliFakeStore, "__init__", init)

    assert run(["--compress", "-i", str(cfg_dir), "--jobs", "1"]) == EXIT_OK

    store = _CliFakeStore.instances[0]
    assert store.forgets == ["one", "two"] and store.prunes == 1
    assert "pruned" in capsys.readouterr().out.lower()


def test_prune_error_after_successful_batch_exit_runtime_report_printed(
    tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mode(monkeypatch, StorageMode.RESTIC)
    cfg_dir = _cfg_dir(tmp_path, data_dir)
    original = _CliFakeStore.__init__

    def init(self: _CliFakeStore, saves_dir: str, identity: str | None = None) -> None:
        original(self, saves_dir, identity)
        self.forgotten = [Snapshot("old-id", "old1", datetime(2026, 1, 1), "x", None)]
        self.fail_prune = True

    monkeypatch.setattr(_CliFakeStore, "__init__", init)

    assert run(["--compress", "-i", str(cfg_dir), "--jobs", "1"]) == EXIT_RUNTIME

    out = capsys.readouterr().out
    # The batch report comes first and still says the backups succeeded.
    assert out.index("one-sh") < out.index("prune exploded")
    assert "] failed:" not in out


def test_tar_batch_prunes_only_configs_with_a_retention_block(
    tmp_path: Path, data_dir: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, StorageMode.TAR_NOT_INITIALISED)
    cfg_dir = _cfg_dir(tmp_path, data_dir, retention={"daily": 1, "weekly": 0, "monthly": 0, "yearly": 0})
    saves = fake_home / ".config" / "config-saver" / "configs"
    for name in ("one", "two"):
        for day in (1, 2, 3):
            stamp = saves / name / f"2026010{day}-000000"
            stamp.mkdir(parents=True)
            (stamp / f"{name}.tar.gz").write_bytes(b"x")

    assert run(["--compress", "-i", str(cfg_dir), "--jobs", "1"]) == EXIT_OK

    assert len(list((saves / "one").iterdir())) == 4
    assert len(list((saves / "two").iterdir())) == 1


# ---------------------------------------- read verbs in restic mode (list/export)


class _ReadStore:
    """A store holding snapshots, each dumping a real tar of one file."""

    def __init__(self, saves_dir: str, identity: str | None = None) -> None:
        self.identity = identity
        self.snaps: list[Snapshot] = list(_ReadStore.seed)

    seed: list[Snapshot] = []
    fail_dump = False

    def snapshots(self, cfg=None):  # type: ignore[no-untyped-def]
        return [s for s in self.snaps if cfg is None or s.cfg == cfg]

    def dump(self, snapshot_id, cfg, out):  # type: ignore[no-untyped-def]
        buf = io.BytesIO()
        payload = f"payload of {snapshot_id}\n".encode()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo("home/tester/exported.txt")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        out.write(buf.getvalue()[:1024])
        if _ReadStore.fail_dump:
            raise StoreError("dump exploded")
        out.write(buf.getvalue()[1024:])


def _read_mode(monkeypatch: pytest.MonkeyPatch, *snaps: Snapshot) -> None:
    _ReadStore.seed = list(snaps)
    _ReadStore.fail_dump = False
    monkeypatch.setattr("config_saver.lib.cli.cli.select_mode", lambda _saves: StorageMode.RESTIC)
    monkeypatch.setattr("config_saver.lib.cli.cli.ResticStore", _ReadStore)


def _snap(cfg: str, when: datetime, description: str | None = None) -> Snapshot:
    return Snapshot(f"{cfg}-{when:%H%M%S}-fullid", f"{cfg}-sh", when, cfg, description)


def _write_user_config(fake_home: Path, name: str, **extra: object) -> Path:
    cfg_dir = fake_home / ".config" / "config-saver" / "configs.d"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    return write_config(cfg_dir / f"{name}.yaml", [str(fake_home)], **extra)


def _age_key(tmp_path: Path) -> tuple[str, str]:
    import shutil

    if shutil.which("age") is None or shutil.which("age-keygen") is None:
        pytest.skip("needs age and age-keygen")
    key = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    public = subprocess.run(["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True)
    return str(key), public.stdout.strip()


def test_list_restic_mode_groups_by_cfg_and_shows_unmigrated(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _read_mode(
        monkeypatch,
        _snap("zsh", datetime(2026, 10, 1, 3, 0, 0), "first run"),
        _snap("zsh", datetime(2026, 10, 2, 3, 0, 0)),
        _snap("kde", datetime(2026, 10, 2, 4, 0, 0)),
    )
    _make_saved_archive(fake_home, "zsh", "20260801-120000")
    assert run(["--list"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "zsh" in out and "kde" in out
    assert "2026-10-01 03:00:00" in out and "first run" in out
    assert "2026-08-01 12:00:00" in out
    assert "not migrated" in out
    rows = [ln for ln in out.splitlines() if "│" in ln or "┃" in ln]
    assert sum(" tar " in ln for ln in rows) == 1
    assert sum(" restic " in ln for ln in rows) == 3
    assert [ln for ln in rows if "2026-08-01 12:00:00" in ln and " tar " in ln]


def test_show_configs_restic_unions_snapshots_and_tars(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _read_mode(monkeypatch, _snap("kde", datetime(2026, 10, 2, 4, 0, 0)))
    _make_saved_archive(fake_home, "zsh", "20260801-120000")
    assert run(["--show-configs"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "- kde" in out and "- zsh" in out


def test_list_restic_mode_empty(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _read_mode(monkeypatch)
    assert run(["--list"]) == EXIT_OK
    assert "No config-saver" in capsys.readouterr().out


def test_read_verbs_store_error_is_exit_runtime(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_saves: str) -> StorageMode:
        raise StoreError("restic store is incomplete")

    monkeypatch.setattr("config_saver.lib.cli.cli.select_mode", boom)
    for verb in (["--list"], ["--show-configs"], ["--export-config", "x"], ["--export-all-configs"]):
        assert run(verb) == EXIT_RUNTIME


def test_export_config_restic_gz_then_age(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from config_saver.lib.tar_compressor.tar_decompressor import TarDecompressor

    key, recipient = _age_key(tmp_path)
    when = datetime(2026, 10, 2, 3, 4, 5)
    _read_mode(monkeypatch, _snap("zsh", datetime(2026, 10, 1, 3, 0, 0)), _snap("zsh", when))
    _write_user_config(fake_home, "zsh", encrypt={"method": "age", "recipients": [recipient]})
    dest = tmp_path / "out"
    dest.mkdir()
    out_file = dest / "zsh-20261002-030405.tar.gz.age"

    assert run(["--export-config", "zsh", "--output", str(out_file), "--identity", key]) == EXIT_OK

    assert sorted(p.name for p in dest.iterdir()) == [out_file.name]  # no temp left
    assert oct(out_file.stat().st_mode & 0o777) == "0o600"
    assert b"payload" not in out_file.read_bytes()
    assert "not encrypted" not in capsys.readouterr().out
    extract = tmp_path / "extract"
    extract.mkdir()
    TarDecompressor(str(out_file), str(extract), identity=key).decompress()
    assert "zsh-030405-fullid" in (extract / "home/tester/exported.txt").read_text()


def test_export_config_default_name_from_snapshot_time(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _read_mode(monkeypatch, _snap("zsh", datetime(2026, 10, 2, 3, 4, 5)))
    assert run(["--export-config", "zsh"]) == EXIT_OK
    assert (fake_home / "zsh-20261002-030405.tar.gz").is_file()


def test_export_config_missing_yaml_plain_and_warns(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import gzip

    _read_mode(monkeypatch, _snap("zsh", datetime(2026, 10, 2, 3, 4, 5)))
    dest = tmp_path / "plain.tar.gz"
    assert run(["--export-config", "zsh", "--output", str(dest)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "no configuration file" in out.lower() and "not encrypted" in out
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(dest.read_bytes()))) as tar:
        assert tar.getnames() == ["home/tester/exported.txt"]


def test_export_config_cli_encrypt_to_overrides(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, recipient = _age_key(tmp_path)
    _read_mode(monkeypatch, _snap("zsh", datetime(2026, 10, 2, 3, 4, 5)))
    dest = tmp_path / "x"
    dest.mkdir()
    assert run(["--export-config", "zsh", "--output", str(dest / "z"), "--encrypt-to", recipient]) == EXIT_OK
    assert [p.name for p in dest.iterdir()] == ["z.age"]


def test_export_no_plaintext_left_on_encrypt_failure(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _read_mode(monkeypatch, _snap("zsh", datetime(2026, 10, 2, 3, 4, 5)))
    _write_user_config(fake_home, "zsh", encrypt={"method": "age", "recipients": ["not-a-real-recipient"]})
    dest = tmp_path / "out"
    dest.mkdir()
    code = run(["--export-config", "zsh", "--output", str(dest / "z.tar.gz.age")])
    assert code == EXIT_ENCRYPTION
    assert list(dest.iterdir()) == []


def test_export_no_temp_left_when_dump_fails(fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _read_mode(monkeypatch, _snap("zsh", datetime(2026, 10, 2, 3, 4, 5)))
    _ReadStore.fail_dump = True
    dest = tmp_path / "out"
    dest.mkdir()
    assert run(["--export-config", "zsh", "--output", str(dest / "z.tar.gz")]) == EXIT_RUNTIME
    assert list(dest.iterdir()) == []


def test_export_config_unknown_name_restic(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _read_mode(monkeypatch, _snap("kde", datetime(2026, 10, 2, 3, 4, 5)))
    assert run(["--export-config", "nope"]) == EXIT_NO_SUCH_CONFIG


def test_export_config_newer_tar_is_copied(fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _read_mode(monkeypatch, _snap("zsh", datetime(2026, 10, 2, 3, 4, 5)))
    newer = _make_saved_archive(fake_home, "zsh", "20261101-000000")
    dest = tmp_path / "copy.tar.gz"
    assert run(["--export-config", "zsh", "--output", str(dest)]) == EXIT_OK
    assert dest.read_bytes() == newer.read_bytes()


def test_export_config_older_tar_loses_to_snapshot(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _read_mode(monkeypatch, _snap("zsh", datetime(2026, 10, 2, 3, 4, 5)))
    older = _make_saved_archive(fake_home, "zsh", "20260101-000000")
    dest = tmp_path / "snap.tar.gz"
    assert run(["--export-config", "zsh", "--output", str(dest)]) == EXIT_OK
    assert dest.read_bytes() != older.read_bytes()


def test_export_all_configs_restic(fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _read_mode(
        monkeypatch,
        _snap("zsh", datetime(2026, 10, 1, 3, 0, 0)),
        _snap("zsh", datetime(2026, 10, 2, 3, 0, 0)),
        _snap("kde", datetime(2026, 10, 2, 4, 0, 0)),
    )
    _make_saved_archive(fake_home, "old", "20260801-120000")
    dest = tmp_path / "exports"
    assert run(["--export-all-configs", "--output", str(dest)]) == EXIT_OK
    assert sorted(p.name for p in dest.iterdir()) == [
        "kde-20261002-040000.tar.gz",
        "old-20260801-120000.tar.gz",
        "zsh-20261002-030000.tar.gz",
    ]


def test_export_config_to_existing_directory_lands_inside(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _read_mode(monkeypatch, _snap("zsh", datetime(2026, 10, 2, 3, 4, 5)))
    parent = tmp_path / "parent"
    dest = parent / "dest"
    dest.mkdir(parents=True)
    assert run(["--export-config", "zsh", "--output", str(dest)]) == EXIT_OK
    assert [p.name for p in dest.iterdir()] == ["zsh-20261002-030405.tar.gz"]
    assert [p.name for p in parent.iterdir()] == ["dest"]


def test_export_all_continues_after_a_failing_config(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _read_mode(
        monkeypatch,
        _snap("aaa", datetime(2026, 10, 2, 3, 0, 0)),
        _snap("zzz", datetime(2026, 10, 2, 4, 0, 0)),
    )
    _write_user_config(fake_home, "aaa", encrypt={"method": "age", "recipients": ["not-a-real-recipient"]})
    dest = tmp_path / "exports"
    assert run(["--export-all-configs", "--output", str(dest)]) == EXIT_ENCRYPTION
    assert [p.name for p in dest.iterdir()] == ["zzz-20261002-040000.tar.gz"]
    assert "not encrypted" in capsys.readouterr().out


def test_export_invalid_config_is_a_validation_error(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _read_mode(monkeypatch, _snap("zsh", datetime(2026, 10, 2, 3, 4, 5)))
    _write_user_config(fake_home, "zsh", encrypt={"method": "age", "recipients": []})
    out = tmp_path / "o.tar.gz"
    assert run(["--export-config", "zsh", "--output", str(out)]) == EXIT_VALIDATION
    assert not out.exists()


def test_export_encrypt_to_with_newer_tar_warns_not_reencrypted(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _read_mode(monkeypatch, _snap("zsh", datetime(2026, 10, 2, 3, 4, 5)))
    _make_saved_archive(fake_home, "zsh", "20261101-000000")
    assert run(["--export-config", "zsh", "--output", str(tmp_path / "c.tar.gz"), "--encrypt-to", "age1abc"]) == EXIT_OK
    assert "not re-encrypted" in capsys.readouterr().out


def test_export_all_restic_includes_oddly_named_archives(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _read_mode(monkeypatch, _snap("zsh", datetime(2026, 10, 2, 3, 0, 0)))
    odd = fake_home / ".config" / "config-saver" / "odd.tar.gz"
    odd.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(odd, "w:gz"):
        pass
    dest = tmp_path / "exports"
    assert run(["--export-all-configs", "--output", str(dest)]) == EXIT_OK
    assert "odd.tar.gz" in [p.name for p in dest.iterdir()]


# ----------------------------------------- --restic-init, --migrate, --prune


class _MaintStore:
    """A store with snapshots per config; records what forget/prune/init were asked."""

    seed: list[Snapshot] = []
    instances: list[_MaintStore] = []

    def __init__(self, saves_dir: str, identity: str | None = None) -> None:
        self.identity = identity or "/key"
        self.forgets: list[tuple[str, object, bool]] = []
        self.prunes = 0
        self.inits: list[str] = []
        _MaintStore.instances.append(self)

    def init(self, recipient: str) -> None:
        self.inits.append(recipient)

    def snapshots(self, cfg=None):  # type: ignore[no-untyped-def]
        return [s for s in _MaintStore.seed if cfg in (None, s.cfg)]

    def forget(self, cfg, policy, dry_run, also_keep=()):  # type: ignore[no-untyped-def]
        self.forgets.append((cfg, policy, dry_run))
        return list(self.snapshots(cfg))[:-1]  # everything but the newest

    def prune(self) -> None:
        self.prunes += 1


def _maint_mode(monkeypatch: pytest.MonkeyPatch, mode: StorageMode, *snaps: Snapshot) -> None:
    _MaintStore.seed = list(snaps)
    _MaintStore.instances = []
    monkeypatch.setattr("config_saver.lib.cli.cli.select_mode", lambda _saves: mode)
    monkeypatch.setattr("config_saver.lib.cli.cli.ResticStore", _MaintStore)


def _snaps(cfg: str, n: int = 3) -> list[Snapshot]:
    return [_snap(cfg, datetime(2026, 1, d, 3, 0, 0)) for d in range(1, n + 1)]


def test_cli_restic_init_requires_encrypt_to(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _maint_mode(monkeypatch, StorageMode.TAR_NOT_INITIALISED)
    assert run(["--restic-init"]) == EXIT_USAGE
    assert run(["--restic-init", "--encrypt-to", "gpgkey", "--encrypt-method", "gpg"]) == EXIT_USAGE
    assert run(["--restic-init", "--encrypt-to", "a", "--encrypt-to", "b"]) == EXIT_USAGE
    assert all(not s.inits for s in _MaintStore.instances)


def test_cli_restic_init_creates_store_and_warns_about_the_key(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _maint_mode(monkeypatch, StorageMode.TAR_NOT_INITIALISED)
    monkeypatch.setattr("config_saver.lib.maintenance.check_identity_matches", lambda identity, recipient: None)
    assert run(["--restic-init", "--encrypt-to", "age1abc", "--identity", "/k"]) == EXIT_OK
    assert _MaintStore.instances[-1].inits == ["age1abc"]
    assert "only thing" in capsys.readouterr().out.lower()


def test_cli_modifier_flags_need_their_verb(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _maint_mode(monkeypatch, StorageMode.RESTIC)
    assert run(["--list", "--delete-migrated"]) == EXIT_USAGE
    assert run(["--list", "--dry-run"]) == EXIT_USAGE
    assert run(["--migrate", "--dry-run"]) == EXIT_USAGE
    assert run(["--prune", "--delete-migrated"]) == EXIT_USAGE


@pytest.mark.parametrize("mode", [StorageMode.TAR_NO_RESTIC, StorageMode.TAR_NOT_INITIALISED])
def test_cli_migrate_in_tar_mode_is_usage_with_the_notice(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: StorageMode
) -> None:
    _maint_mode(monkeypatch, mode)
    assert run(["--migrate"]) == EXIT_USAGE
    assert NOTICES[mode] in capsys.readouterr().out


def _migration(monkeypatch: pytest.MonkeyPatch, **fields: object) -> list[tuple[object, ...]]:
    from config_saver.lib.migrate import MigrationReport

    calls: list[tuple[object, ...]] = []

    def fake(manager, store, identity, delete, progress=None):  # type: ignore[no-untyped-def]
        calls.append((identity, delete))
        return MigrationReport(**fields)  # type: ignore[arg-type]

    monkeypatch.setattr("config_saver.lib.cli.cli.migrate_archives", fake)
    return calls


def test_cli_migrate_exit_codes_and_flags(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _maint_mode(monkeypatch, StorageMode.RESTIC)
    calls = _migration(monkeypatch, migrated=["a"])
    assert run(["--migrate", "--identity", "/k", "--delete-migrated"]) == EXIT_OK
    assert calls == [("/k", True)]
    _migration(monkeypatch, migrated=["a"], failed=[("b", "boom")])
    assert run(["--migrate"]) == EXIT_RUNTIME
    _migration(monkeypatch, migrated=["a"], check_error="bad pack")
    assert run(["--migrate"]) == EXIT_RUNTIME


def test_cli_prune_dry_run_deletes_nothing(
    tmp_path: Path, data_dir: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _maint_mode(monkeypatch, StorageMode.RESTIC, *_snaps("one"))
    cfg_dir = _cfg_dir(tmp_path, data_dir)
    assert run(["--prune", "--dry-run", "-i", str(cfg_dir)]) == EXIT_OK
    store = _MaintStore.instances[-1]
    assert store.forgets and all(dry for _, _, dry in store.forgets) and store.prunes == 0
    out = capsys.readouterr().out
    assert "would remove 2" in out and "Total" in out


def test_cli_prune_skips_legacy_by_default(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _maint_mode(monkeypatch, StorageMode.RESTIC, *_snaps("legacy"), *_snaps("kept"))
    cfg_dir = fake_home / "empty-cfgs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "kept.yaml", [str(fake_home)])
    assert run(["--prune", "-i", str(cfg_dir)]) == EXIT_OK
    store = _MaintStore.instances[-1]
    assert [c for c, _, _ in store.forgets] == ["kept"] and store.prunes == 1
    assert run(["--prune", "--include-legacy", "-i", str(cfg_dir)]) == EXIT_OK
    assert sorted(c for c, _, _ in _MaintStore.instances[-1].forgets) == ["kept", "legacy"]


def test_cli_prune_restic_uses_block_for_configured_and_defaults_for_orphans(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from config_saver.lib.models.retention_model import RetentionModel

    _maint_mode(monkeypatch, StorageMode.RESTIC, *_snaps("mine"), *_snaps("gone"))
    cfg_dir = fake_home / "cfgs"
    cfg_dir.mkdir()
    write_config(
        cfg_dir / "mine.yaml", [str(fake_home)], retention={"daily": 1, "weekly": 0, "monthly": 0, "yearly": 0}
    )
    assert run(["--prune", "-i", str(cfg_dir)]) == EXIT_OK
    policies = {c: p for c, p, _ in _MaintStore.instances[-1].forgets}
    assert policies["mine"] == RetentionModel(daily=1, weekly=0, monthly=0, yearly=0)
    assert policies["gone"] == RetentionModel()
    assert "no configuration file" in capsys.readouterr().out


def _tar_stamps(saves: Path, name: str, days: int = 3) -> list[Path]:
    dirs = []
    for day in range(1, days + 1):
        stamp = saves / "configs" / name / f"2026010{day}-000000"
        stamp.mkdir(parents=True)
        (stamp / f"{name}.tar.gz").write_bytes(b"x")
        dirs.append(stamp)
    return dirs


def test_cli_prune_tar_mode_explicit_retention_only(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _maint_mode(monkeypatch, StorageMode.TAR_NOT_INITIALISED)
    saves = fake_home / ".config" / "config-saver"
    cfg_dir = fake_home / "cfgs"
    cfg_dir.mkdir()
    write_config(
        cfg_dir / "with.yaml", [str(fake_home)], retention={"daily": 1, "weekly": 0, "monthly": 0, "yearly": 0}
    )
    write_config(cfg_dir / "without.yaml", [str(fake_home)])
    with_dirs = _tar_stamps(saves, "with")
    without_dirs = _tar_stamps(saves, "without")
    _tar_stamps(saves, "orphan")

    assert run(["--prune", "--dry-run", "-i", str(cfg_dir)]) == EXIT_OK
    assert all(d.exists() for d in with_dirs)

    assert run(["--prune", "-i", str(cfg_dir)]) == EXIT_OK
    assert [d.exists() for d in with_dirs] == [False, False, True]
    assert all(d.exists() for d in without_dirs)
    assert len(list((saves / "configs" / "orphan").iterdir())) == 3
    assert "Total" in capsys.readouterr().out


def test_cli_restic_init_refuses_a_mismatched_identity(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _maint_mode(monkeypatch, StorageMode.TAR_NOT_INITIALISED)
    monkeypatch.setattr("config_saver.lib.maintenance.check_identity_matches", lambda identity, recipient: "wrong key")
    assert run(["--restic-init", "--encrypt-to", "age1abc", "--identity", "/k"]) == EXIT_USAGE
    assert _MaintStore.instances[-1].inits == [] and "wrong key" in capsys.readouterr().out


def test_cli_prune_lists_what_it_removes(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _maint_mode(monkeypatch, StorageMode.RESTIC, *_snaps("mine"))
    cfg_dir = fake_home / "cfgs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "mine.yaml", [str(fake_home)])
    assert run(["--prune", "--dry-run", "-i", str(cfg_dir)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "2026-01-01 03:00:00 (mine-sh)" in out and "2026-01-02 03:00:00 (mine-sh)" in out
    assert "2026-01-03" not in out
