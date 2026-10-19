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
    _keygen(monkeypatch, "age1acde\n")
    assert maintenance.check_identity_matches(str(key), "age1acde") is None


def test_mismatching_identity_is_explained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = tmp_path / "k"
    key.write_text("x")
    _keygen(monkeypatch, "age1zzzz\n")
    problem = maintenance.check_identity_matches(str(key), "age1acde")
    assert problem and "age1zzzz" in problem and "age1acde" in problem


def test_missing_identity_file(tmp_path: Path) -> None:
    problem = maintenance.check_identity_matches(str(tmp_path / "nope"), "age1acde")
    assert problem and "not found" in problem


def test_unreadable_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = tmp_path / "k"
    key.write_text("x")
    _keygen(monkeypatch, "", rc=1, err="bad key")
    problem = maintenance.check_identity_matches(str(key), "age1acde")
    assert problem and "bad key" in problem


def test_non_native_recipients_are_not_compared(tmp_path: Path) -> None:
    assert maintenance.check_identity_matches(str(tmp_path / "nope"), "ssh-ed25519 AAAA") is None


@pytest.mark.parametrize("recipient", ["age1yubikey1qfabc", "age1tpm1qabc", "age1se1qabc"])
def test_plugin_recipients_are_not_compared(tmp_path: Path, recipient: str) -> None:
    assert maintenance.check_identity_matches(str(tmp_path / "nope"), recipient) is None


def test_multi_key_identity_file_matches_any_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = tmp_path / "k"
    key.write_text("x")
    _keygen(monkeypatch, "age1pppp\nage1qqqq\n")
    assert maintenance.check_identity_matches(str(key), "age1qqqq") is None
    problem = maintenance.check_identity_matches(str(key), "age1ssss")
    assert problem and "age1pppp" in problem and "age1qqqq" in problem
