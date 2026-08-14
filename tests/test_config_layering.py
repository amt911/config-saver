"""Three configuration levels, merged with the most specific one winning.

    <prefix>/share/config-saver/configs   examples shipped by the package
    /etc/config-saver/configs             system policy (dasik writes it)
    ~/.config/config-saver/configs.d      the user's own configurations

The two active levels are merged by configuration name; the examples are never
picked up on their own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config_saver.lib import paths
from config_saver.lib.cli.cli import CLI, EXIT_OK, EXIT_USAGE

from .conftest import write_config


@pytest.fixture
def data_dir(fake_home: Path) -> Path:
    data = fake_home / "data"
    data.mkdir()
    (data / "f.txt").write_text("hello\n", encoding="utf-8")
    return data


@pytest.fixture
def levels(tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point the three levels at throwaway directories."""
    system = tmp_path / "etc"
    examples = tmp_path / "usr-share"
    user = fake_home / ".config" / "config-saver" / "configs.d"
    for directory in (system, examples, user):
        directory.mkdir(parents=True)
    monkeypatch.setattr(paths, "SYSTEM_CONFIG_DIR", str(system))
    monkeypatch.setattr(paths, "example_config_dir", lambda: str(examples))
    return {"system": system, "examples": examples, "user": user}


def _archives(fake_home: Path) -> list[str]:
    root = fake_home / ".config" / "config-saver" / "configs"
    return sorted(p.name.split("-2026")[0] for p in root.rglob("*.tar.gz"))


def test_both_levels_are_merged(levels: dict[str, Path], data_dir: Path, fake_home: Path) -> None:
    write_config(levels["system"] / "system-policy.json", [str(data_dir)])
    write_config(levels["user"] / "personal.yaml", [str(data_dir)])

    assert CLI(["--compress"]).run() == EXIT_OK
    assert _archives(fake_home) == ["personal", "system-policy"]


def test_the_user_wins_over_the_system_for_the_same_name(
    levels: dict[str, Path], data_dir: Path, fake_home: Path
) -> None:
    """Same configuration name at two levels: the user's is used and the
    system's is ignored, rather than both running."""
    system_only = fake_home / "system-only-data"
    system_only.mkdir()
    (system_only / "from-system.txt").write_text("system", encoding="utf-8")

    write_config(levels["system"] / "zsh.json", [str(system_only)])
    write_config(levels["user"] / "zsh.yaml", [str(data_dir)])

    assert CLI(["--compress"]).run() == EXIT_OK
    assert _archives(fake_home) == ["zsh"]

    import tarfile

    archive = next((fake_home / ".config" / "config-saver" / "configs" / "zsh").rglob("*.tar.gz"))
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert any("data" in name for name in names), names
    assert not any("system-only-data" in name for name in names), names


def test_examples_are_never_used_on_their_own(
    levels: dict[str, Path], data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A package that ships examples must not decide what gets backed up."""
    write_config(levels["examples"] / "default-config.yaml", [str(data_dir)])

    assert CLI(["--compress"]).run() == EXIT_USAGE
    out = capsys.readouterr().out
    assert str(levels["examples"]) in out
    assert "configs.d" in out


def test_examples_can_still_be_used_explicitly(levels: dict[str, Path], data_dir: Path, fake_home: Path) -> None:
    write_config(levels["examples"] / "default-config.yaml", [str(data_dir)])
    assert CLI(["--compress", "--input", str(levels["examples"])]).run() == EXIT_OK
    assert _archives(fake_home) == ["default-config"]
