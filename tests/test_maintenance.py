"""check_identity_matches with a mocked age-keygen."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from config_saver.lib import maintenance


def _keygen(monkeypatch: pytest.MonkeyPatch, out: str = "", rc: int = 0, err: str = "") -> None:
    monkeypatch.setattr(
        maintenance.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, rc, out.encode(), err.encode()),
    )


def test_matching_identity_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = tmp_path / "k"
    key.write_text("x")
    _keygen(monkeypatch, "age1abc\n")
    assert maintenance.check_identity_matches(str(key), "age1abc") is None


def test_mismatching_identity_is_explained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = tmp_path / "k"
    key.write_text("x")
    _keygen(monkeypatch, "age1other\n")
    problem = maintenance.check_identity_matches(str(key), "age1abc")
    assert problem and "age1other" in problem and "age1abc" in problem


def test_missing_identity_file(tmp_path: Path) -> None:
    problem = maintenance.check_identity_matches(str(tmp_path / "nope"), "age1abc")
    assert problem and "not found" in problem


def test_unreadable_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = tmp_path / "k"
    key.write_text("x")
    _keygen(monkeypatch, "", rc=1, err="bad key")
    problem = maintenance.check_identity_matches(str(key), "age1abc")
    assert problem and "bad key" in problem


def test_non_native_recipients_are_not_compared(tmp_path: Path) -> None:
    assert maintenance.check_identity_matches(str(tmp_path / "nope"), "ssh-ed25519 AAAA") is None
