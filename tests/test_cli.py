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

from config_saver.lib.backup_manager.backup_manager import BackupManager
from config_saver.lib.cli import cli as cli_module
from config_saver.lib.cli.cli import (
    CLI,
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
    monkeypatch.setattr(CLI, "DEFAULT_SYSTEM_CONFIG", str(tmp_path / "absent"))
    monkeypatch.setattr(cli_module.sys, "prefix", str(tmp_path / "prefix"))
    assert run(["--compress"]) == EXIT_USAGE
    assert "AUR" in capsys.readouterr().out


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

    original = BackupManager.compress_directory_of_configs

    def spy(self, input_dir, timestamp, *, show_progress=False, description=None, jobs=1):
        seen["jobs"] = jobs
        return original(self, input_dir, timestamp, show_progress=show_progress, description=description, jobs=jobs)

    monkeypatch.setattr(BackupManager, "compress_directory_of_configs", spy)
    assert run(["--compress", "--input", str(cfg_dir)]) == EXIT_OK
    assert seen["jobs"] == (os.cpu_count() or 1)


def test_progress_falls_back_to_sequential(tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Several workers and a per-file progress bar are unreadable together."""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "one.yaml", [str(data_dir)])
    seen: dict[str, int] = {}

    original = BackupManager.compress_directory_of_configs

    def spy(self, input_dir, timestamp, *, show_progress=False, description=None, jobs=1):
        seen["jobs"] = jobs
        return original(self, input_dir, timestamp, show_progress=show_progress, description=description, jobs=jobs)

    monkeypatch.setattr(BackupManager, "compress_directory_of_configs", spy)
    assert run(["--compress", "--progress", "--input", str(cfg_dir)]) == EXIT_OK
    assert seen["jobs"] == 1

    # Asking for both explicitly is still honoured.
    assert run(["--compress", "--progress", "--jobs", "2", "--input", str(cfg_dir)]) == EXIT_OK
    assert seen["jobs"] == 2
