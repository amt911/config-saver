"""sync.json and the password derived from the age identity."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from config_saver.lib.errors import SyncNotSetUpError, SyncStateError
from config_saver.lib.sync import paths
from config_saver.lib.sync.remote import SyncRemote, derive_password, load_remote, save_remote

KEY = "AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ"
OTHER = "AGE-SECRET-KEY-1ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ"


def _identity(tmp_path: Path, text: str, name: str = "key.txt") -> str:
    path = tmp_path / name
    path.write_bytes(text.encode())
    return str(path)


def test_derive_password_ignores_comments_line_endings_and_blank_lines(tmp_path: Path) -> None:
    plain = _identity(tmp_path, f"# created: 2026\n# public key: age1x\n{KEY}\n", "a")
    crlf = _identity(tmp_path, f"# created: 2026\r\n# public key: age1x\r\n{KEY}\r\n", "b")
    blanks = _identity(tmp_path, f"\n\n  {KEY}  \n\n\n", "c")
    assert derive_password(plain) == derive_password(crlf) == derive_password(blanks)
    assert len(derive_password(plain)) == 64
    assert derive_password(plain) == derive_password(plain).lower()


def test_derive_password_differs_per_key(tmp_path: Path) -> None:
    assert derive_password(_identity(tmp_path, KEY, "a")) != derive_password(_identity(tmp_path, OTHER, "b"))


def test_derive_password_is_the_documented_sha256(tmp_path: Path) -> None:
    expected = hashlib.sha256(b"config-saver-sync-v1\0" + KEY.encode()).hexdigest()
    assert derive_password(_identity(tmp_path, f"# c\n{KEY}\n")) == expected


def test_derive_password_without_key_line(tmp_path: Path) -> None:
    with pytest.raises(SyncNotSetUpError):
        derive_password(_identity(tmp_path, "# only a comment\nage1public\n"))


def test_derive_password_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SyncNotSetUpError):
        derive_password(str(tmp_path / "nope"))


def test_remote_round_trip_and_permissions(tmp_path: Path) -> None:
    path = str(tmp_path / "cfg" / "config-saver" / "sync.json")
    remote = SyncRemote("rclone:drive:backup", ("rclone.args=serve restic --stdio",))
    save_remote(path, remote)
    assert load_remote(path) == remote
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert json.loads(Path(path).read_text()) == {
        "repository": "rclone:drive:backup",
        "options": ["rclone.args=serve restic --stdio"],
    }
    assert not [f for f in os.listdir(os.path.dirname(path)) if f != "sync.json"]


def test_save_overwrites_atomically(tmp_path: Path) -> None:
    path = str(tmp_path / "sync.json")
    save_remote(path, SyncRemote("/a"))
    save_remote(path, SyncRemote("/b", ("x=y",)))
    assert load_remote(path) == SyncRemote("/b", ("x=y",))


def test_load_missing_points_at_sync_init(tmp_path: Path) -> None:
    with pytest.raises(
        SyncNotSetUpError, match=r"--sync-init <restic repository, e\.g\. rclone:Google Drive:config-saver-sync>"
    ):
        load_remote(str(tmp_path / "sync.json"))


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"repository": "/r"}',
        '{"repository": "/r", "options": [], "extra": 1}',
        '{"repository": "", "options": []}',
        '{"repository": 3, "options": []}',
        '{"repository": "/r", "options": "x"}',
        '{"repository": "/r", "options": [1]}',
    ],
)
def test_load_rejects_bad_shape(tmp_path: Path, content: str) -> None:
    path = tmp_path / "sync.json"
    path.write_text(content)
    with pytest.raises(SyncStateError):
        load_remote(str(path))


def test_needs_rclone() -> None:
    assert SyncRemote("rclone:x:y").needs_rclone
    assert not SyncRemote("/srv/repo").needs_rclone
    assert not SyncRemote("sftp:h:/r").needs_rclone


def test_paths_follow_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    assert paths.remote_config_path() == str(tmp_path / "c" / "config-saver" / "sync.json")
    assert paths.lock_path() == str(tmp_path / "s" / "config-saver" / "sync.lock")


def test_store_uses_derived_password(tmp_path: Path) -> None:
    identity = _identity(tmp_path, KEY)
    store = SyncRemote("/srv/r", ("a=b",)).store(identity)
    assert store.repo == "/srv/r"
    assert store._password == derive_password(identity)
    assert store._options == ("a=b",)
