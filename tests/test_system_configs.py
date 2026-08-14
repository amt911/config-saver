"""Whether an archive carries, and restores, the system configuration level.

Personal configurations live under $HOME, so they are already inside any
archive that backs up the home directory and come back with it. The system
level (/etc/config-saver/configs) is different: a declarative installer owns
it, and silently restoring it would make the machine diverge from what the
installer declared. Both directions are therefore opt-in.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from config_saver.lib import paths
from config_saver.lib.cli.cli import CLI, EXIT_OK

from .conftest import write_config


@pytest.fixture
def data_dir(fake_home: Path) -> Path:
    data = fake_home / "data"
    data.mkdir()
    (data / "f.txt").write_text("hello\n", encoding="utf-8")
    return data


def test_system_configs_are_not_archived_by_default(
    tmp_path: Path, fake_home: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = tmp_path / "etc"
    system.mkdir()
    write_config(system / "policy.json", [str(data_dir)])
    monkeypatch.setattr(paths, "SYSTEM_CONFIG_DIR", str(system))

    cfg = write_config(tmp_path / "c.yaml", [str(data_dir)])
    archive = tmp_path / "a.tar.gz"
    assert CLI(["--compress", "-i", str(cfg), "-o", str(archive)]).run() == EXIT_OK

    with tarfile.open(archive, "r:gz") as tar:
        assert not any("policy.json" in name for name in tar.getnames())


def test_include_system_configs_adds_them(
    tmp_path: Path, fake_home: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opt-in for machines that have no declarative installer to rebuild /etc."""
    system = tmp_path / "etc"
    system.mkdir()
    write_config(system / "policy.json", [str(data_dir)])
    monkeypatch.setattr(paths, "SYSTEM_CONFIG_DIR", str(system))

    cfg = write_config(tmp_path / "c.yaml", [str(data_dir)])
    archive = tmp_path / "a.tar.gz"
    assert CLI(["--compress", "-i", str(cfg), "-o", str(archive), "--include-system-configs"]).run() == EXIT_OK

    with tarfile.open(archive, "r:gz") as tar:
        assert any(name.endswith("policy.json") for name in tar.getnames())


def _archive_with_system_config(tmp_path: Path, system_dir: str) -> Path:
    """An archive holding one member under the system configuration directory."""
    archive = tmp_path / "restore.tar.gz"
    payload = b'{"directories": []}\n'
    member_name = str(Path(system_dir) / "policy.json").lstrip("/")
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(member_name)
        info.size = len(payload)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(payload))
    return archive


def test_system_configs_are_not_restored_by_default(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """dasik owns /etc declaratively: a restore that overwrote it would make the
    machine diverge from its own configuration."""
    system = tmp_path / "etc"
    system.mkdir()
    monkeypatch.setattr(paths, "SYSTEM_CONFIG_DIR", str(system))
    archive = _archive_with_system_config(tmp_path, str(system))

    assert CLI(["--decompress", "-i", str(archive)]).run() == EXIT_OK
    assert not (system / "policy.json").exists()
    assert "system configuration" in capsys.readouterr().out


def test_restore_system_configs_opt_in(tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    system = tmp_path / "etc"
    system.mkdir()
    monkeypatch.setattr(paths, "SYSTEM_CONFIG_DIR", str(system))
    archive = _archive_with_system_config(tmp_path, str(system))

    assert CLI(["--decompress", "-i", str(archive), "--restore-system-configs"]).run() == EXIT_OK
    assert (system / "policy.json").read_text() == '{"directories": []}\n'


def test_extraction_into_a_directory_is_unaffected(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--output writes below a sandbox, so nothing can clash with the real /etc
    and the member must be extracted."""
    system = tmp_path / "etc"
    system.mkdir()
    monkeypatch.setattr(paths, "SYSTEM_CONFIG_DIR", str(system))
    archive = _archive_with_system_config(tmp_path, str(system))
    out = tmp_path / "out"

    assert CLI(["--decompress", "-i", str(archive), "-o", str(out)]).run() == EXIT_OK
    assert list(out.rglob("policy.json"))
