"""Shared fixtures.

Every test runs against a fake ``$HOME`` under tmp_path: the tool normalizes and
restores paths relative to the user's home, and a test must never be able to
write into the real one.
"""

from __future__ import annotations

import os
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from config_saver.lib.models.model import Model


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point $HOME at a throwaway directory and return it."""
    home = tmp_path / "home" / "tester"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


@pytest.fixture
def sample_tree(fake_home: Path) -> Path:
    """A small but representative tree inside the fake home."""
    root = fake_home / "tree"
    (root / "nested" / "deep").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "plain.txt").write_text("hello world\n", encoding="utf-8")
    (root / "accented ñame.txt").write_text("olé\n", encoding="utf-8")
    (root / "nested" / "deep" / "binary.bin").write_bytes(bytes(range(256)))
    (root / "nested" / "latin1.txt").write_bytes("café\n".encode("latin-1"))
    (root / "executable.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    os.chmod(root / "executable.sh", 0o755)
    os.chmod(root / "plain.txt", 0o640)
    (root / "link.txt").symlink_to("plain.txt")
    return root


def make_model(directories: list[Any], **kwargs: Any) -> Model:
    """Build a validated Model without going through a file."""
    return Model.model_validate({"directories": directories, **kwargs})


def write_config(path: Path, directories: list[Any], **kwargs: Any) -> Path:
    """Write a YAML config file and return its path."""
    path.write_text(yaml.safe_dump({"directories": directories, **kwargs}), encoding="utf-8")
    return path


def archive_names(archive: Path) -> list[str]:
    """Member names inside an archive, metadata excluded."""
    with tarfile.open(archive, "r:gz") as tar:
        return [m.name for m in tar.getmembers() if not m.name.startswith(".config-saver-")]


def archive_member_bytes(archive: Path, name: str) -> bytes:
    with tarfile.open(archive, "r:gz") as tar:
        handle = tar.extractfile(name)
        assert handle is not None
        return handle.read()
