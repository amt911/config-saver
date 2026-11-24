"""ResticStore with subprocess mocked: argv, env, error mapping, pure helpers."""

from __future__ import annotations

import io
import json
import os
import pickle
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from config_saver.lib import restic_store as rs
from config_saver.lib.errors import StoreError
from config_saver.lib.models.retention_model import RetentionModel
from config_saver.lib.restic_store import (
    NOTICES,
    PARENT_TAG_PREFIX,
    SYNC_TAG,
    ResticStore,
    StorageMode,
    decode_description,
    encode_description,
    parse_restic_time,
    select_mode,
)
from config_saver.lib.tar_compressor.tar_compressor import CompressResult

SID = "a" * 64


def _store(tmp_path: Path, identity: str | None = "/keys/my key.txt") -> ResticStore:
    return ResticStore(str(tmp_path), identity=identity)


def _make_store_dirs(tmp_path: Path) -> None:
    (tmp_path / "restic").mkdir()
    (tmp_path / "restic-password.age").write_bytes(b"x")


# ---------------------------------------------------------------- select_mode


def _which(available: set[str]):
    return lambda name: f"/usr/bin/{name}" if name in available else None


def test_select_mode_no_restic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs.shutil, "which", _which({"age"}))
    assert select_mode(str(tmp_path)) is StorageMode.TAR_NO_RESTIC


def test_select_mode_not_initialised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs.shutil, "which", _which({"restic"}))
    assert select_mode(str(tmp_path)) is StorageMode.TAR_NOT_INITIALISED


def test_select_mode_repo_without_password_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs.shutil, "which", _which({"restic", "age"}))
    (tmp_path / "restic").mkdir()
    with pytest.raises(StoreError, match="incomplete: password file .*restic-password.age is missing"):
        select_mode(str(tmp_path))


def test_select_mode_password_without_repo_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs.shutil, "which", _which({"restic", "age"}))
    (tmp_path / "restic-password.age").write_bytes(b"x")
    with pytest.raises(StoreError, match="incomplete: repository .*restic is missing"):
        select_mode(str(tmp_path))


def test_select_mode_restic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs.shutil, "which", _which({"restic", "age"}))
    _make_store_dirs(tmp_path)
    assert select_mode(str(tmp_path)) is StorageMode.RESTIC


@pytest.mark.parametrize("missing", ["restic", "age"])
def test_select_mode_store_exists_binary_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setattr(rs.shutil, "which", _which({"restic", "age"} - {missing}))
    _make_store_dirs(tmp_path)
    with pytest.raises(StoreError, match=missing):
        select_mode(str(tmp_path))


def test_notices_cover_the_tar_modes() -> None:
    assert set(NOTICES) == {StorageMode.TAR_NO_RESTIC, StorageMode.TAR_NOT_INITIALISED}
    assert all("\n" not in text for text in NOTICES.values())
    assert "--restic-init" in NOTICES[StorageMode.TAR_NOT_INITIALISED]


# ------------------------------------------------------------------ identity


def test_identity_defaults_to_recorded_then_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert ResticStore(str(tmp_path)).identity == str(tmp_path / "home" / ".config/age/key.txt")
    (tmp_path / "restic-store.json").write_text(json.dumps({"identity": "/recorded/key"}))
    assert ResticStore(str(tmp_path)).identity == "/recorded/key"
    assert ResticStore(str(tmp_path), identity="/explicit").identity == "/explicit"


def test_store_is_picklable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clone = pickle.loads(pickle.dumps(store))
    assert clone.__dict__ == store.__dict__


# ----------------------------------------------------------------------- init


class Recorder:
    """Stands in for subprocess.run/Popen, recording argv, env and input."""

    def __init__(self, tmp_path: Path) -> None:
        self.calls: list[dict[str, Any]] = []
        self.tmp_path = tmp_path
        self.stdout = b""
        self.stderr = b""
        self.returncode = 0
        self.fail_on: str | None = None

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append({"argv": argv, **kwargs})
        rc = self.returncode
        if argv[0] == "age":
            kwargs["stdout"].write(b"age-encrypted")
        if self.fail_on and argv[0] == self.fail_on:
            rc = 1
        return subprocess.CompletedProcess(argv, rc, self.stdout, self.stderr)


def test_init_refuses_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    _make_store_dirs(tmp_path)
    with pytest.raises(StoreError, match="already exists"):
        _store(tmp_path).init("age1abc")
    (tmp_path / "restic").rmdir()
    with pytest.raises(StoreError, match="already exists"):  # password file alone also refuses
        _store(tmp_path).init("age1abc")
    assert rec.calls == []


def test_init_password_never_on_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    store = _store(tmp_path)
    store.init("age1abc")
    age_call = rec.calls[0]
    password = age_call["input"].decode()
    assert len(password) >= 32
    assert age_call["argv"][:3] == ["age", "-r", "age1abc"]
    for call in rec.calls:
        assert not any(password in arg for arg in call["argv"])
        assert password not in "".join(f"{k}={v}" for k, v in call.get("env", {}).items())
    assert rec.calls[1]["argv"] == ["restic", "init"]
    assert json.loads((tmp_path / "restic-store.json").read_text()) == {"identity": "/keys/my key.txt"}


def test_init_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    _store(tmp_path).init("age1abc")
    assert (tmp_path / "restic-password.age").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "restic-store.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "restic").stat().st_mode & 0o777 == 0o700


def test_relative_identity_is_stored_and_used_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    ResticStore(str(tmp_path), identity="key.txt").init("age1acde")
    assert json.loads((tmp_path / "restic-store.json").read_text())["identity"] == str(work / "key.txt")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert ResticStore(str(tmp_path)).identity == str(work / "key.txt")
    assert ResticStore(str(tmp_path), identity="other.txt").identity == str(elsewhere / "other.txt")


@pytest.mark.parametrize("bad", ["it's", 'say"hi', "back\\slash"])
def test_paths_restic_cannot_carry_are_refused_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    rec = Recorder(tmp_path)
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    _popen(monkeypatch, out=SUMMARY)
    for store in (_store(tmp_path, identity=f"/keys/{bad}"), ResticStore(str(tmp_path / bad), identity="/k")):
        with pytest.raises(StoreError, match="cannot carry"):
            store.init("age1abc")
        with pytest.raises(StoreError, match="cannot carry"):
            store.prune()
        with pytest.raises(StoreError, match="cannot carry"):
            store.backup(_writer(), "c", WHEN, None)
    assert rec.calls == []


def test_init_failure_after_password_file_removes_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    monkeypatch.setattr(rs.subprocess, "run", rec.run)

    def boom(*_a: Any, **_k: Any) -> None:
        raise PermissionError("nope")

    monkeypatch.setattr(rs.os, "makedirs", lambda p, *a, **k: boom() if str(p).endswith("restic") else None)
    with pytest.raises(PermissionError):
        _store(tmp_path).init("age1abc")
    assert not (tmp_path / "restic-password.age").exists()


def test_init_failure_leaves_nothing_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    rec.fail_on = "restic"
    rec.stderr = b"boom"
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    with pytest.raises(StoreError, match="boom"):
        _store(tmp_path).init("age1abc")
    assert not (tmp_path / "restic").exists()
    assert not (tmp_path / "restic-password.age").exists()
    assert not (tmp_path / "restic-store.json").exists()


def test_init_age_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    rec.fail_on = "age"
    rec.stderr = b"bad recipient"
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    with pytest.raises(StoreError, match="bad recipient"):
        _store(tmp_path).init("nope")
    assert not (tmp_path / "restic").exists()


# --------------------------------------------------------------------- backup


class FakeStdin:
    def __init__(self, broken: bool = False) -> None:
        self.data = bytearray()
        self.closed = False
        self.broken = broken

    def write(self, data: bytes) -> int:
        if self.broken:
            raise BrokenPipeError
        self.data += data
        return len(data)

    def close(self) -> None:
        self.closed = True


class FakePopen:
    last: FakePopen

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.stdin = FakeStdin(broken=self.broken)
        self.stdout = io.BytesIO(self.out)
        self.stderr = io.BytesIO(self.err)
        self.killed = False
        FakePopen.last = self

    broken = False
    out = b""
    err = b""
    rc = 0

    def wait(self) -> int:
        return self.rc

    def kill(self) -> None:
        self.killed = True
        self.rc = -9


def _popen(monkeypatch: pytest.MonkeyPatch, **attrs: Any) -> type[FakePopen]:
    cls = type("P", (FakePopen,), attrs)
    monkeypatch.setattr(rs.subprocess, "Popen", cls)
    return cls


SUMMARY = (
    b'{"message_type":"status","percent_done":0.5}\n'
    b'{"message_type":"summary","snapshot_id":"' + SID.encode() + b'","data_added":4242}\n'
)
WHEN = datetime(2026, 10, 19, 12, 4, 56, 789)


def _writer(result: CompressResult | None = None):
    def write(stream: Any) -> CompressResult:
        stream.write(b"tar-bytes")
        return result or CompressResult(output_path="", added=3)

    return write


def test_backup_argv_and_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESTIC_PASSWORD", "leak")
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", "/leak")
    cls = _popen(monkeypatch, out=SUMMARY)
    marker = CompressResult(output_path="", added=7)
    snap, result, added = _store(tmp_path).backup(_writer(marker), "own-configs", WHEN, "héllo, wörld")
    argv = cls.last.argv
    assert argv[:2] == ["restic", "backup"]
    assert argv[argv.index("--stdin-filename") + 1] == "own-configs.tar"
    tags = [argv[i + 1] for i, a in enumerate(argv) if a == "--tag"]
    assert tags == ["config-saver", "cfg:own-configs", encode_description("héllo, wörld")]
    assert argv[argv.index("--time") + 1] == "2026-10-19 12:04:56"
    assert "--json" in argv
    env = cls.last.kwargs["env"]
    assert env["RESTIC_REPOSITORY"] == str(tmp_path / "restic")
    assert env["RESTIC_PASSWORD_COMMAND"] == f"age -d -i '/keys/my key.txt' {tmp_path}/restic-password.age"
    assert not [k for k in env if k.startswith("RESTIC_PASSWORD") and k != "RESTIC_PASSWORD_COMMAND"]
    assert bytes(cls.last.stdin.data) == b"tar-bytes" and cls.last.stdin.closed
    assert (snap.id, snap.short_id, snap.cfg, snap.description) == (SID, "a" * 8, "own-configs", "héllo, wörld")
    assert snap.time == WHEN.replace(microsecond=0)
    assert result is marker and added == 4242


def test_backup_without_description_has_no_desc_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cls = _popen(monkeypatch, out=SUMMARY)
    snap, _, _ = _store(tmp_path).backup(_writer(), "c", WHEN, None)
    assert "desc:" not in " ".join(cls.last.argv)
    assert snap.description is None


def test_backup_nonzero_raises_store_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _popen(monkeypatch, rc=1, err=b"repository is locked")
    with pytest.raises(StoreError, match="repository is locked"):
        _store(tmp_path).backup(_writer(), "c", WHEN, None)


def test_backup_missing_summary_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _popen(monkeypatch, out=b'{"message_type":"status"}\n')
    with pytest.raises(StoreError, match="snapshot id"):
        _store(tmp_path).backup(_writer(), "c", WHEN, None)


def test_backup_rejects_comma_in_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _popen(monkeypatch, out=SUMMARY)
    with pytest.raises(StoreError, match="comma"):
        _store(tmp_path).backup(_writer(), "a,b", WHEN, None)


def test_backup_extra_tags_are_appended_to_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cls = _popen(monkeypatch, out=SUMMARY)
    snap, _, _ = _store(tmp_path).backup(_writer(), "c", WHEN, None, extra_tags=[SYNC_TAG, f"{PARENT_TAG_PREFIX}none"])
    argv = cls.last.argv
    tags = [argv[i + 1] for i, a in enumerate(argv) if a == "--tag"]
    assert tags == ["config-saver", "cfg:c", "sync", "parent:none"]
    assert snap.tags == ()


@pytest.mark.parametrize("bad", ["a,b", "", "has space", "tab\tx"])
def test_backup_rejects_bad_extra_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("restic must not be started for an invalid tag")

    monkeypatch.setattr(rs.subprocess, "Popen", forbidden)
    with pytest.raises(StoreError, match="tag"):
        _store(tmp_path).backup(_writer(), "c", WHEN, None, extra_tags=[bad])


def test_backup_writer_failure_kills_restic_and_reraises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cls = _popen(monkeypatch, out=SUMMARY)

    def write(_stream: Any) -> CompressResult:
        raise ValueError("disk on fire")

    with pytest.raises(ValueError, match="disk on fire"):
        _store(tmp_path).backup(write, "c", WHEN, None)
    assert cls.last.killed


def test_backup_broken_pipe_surfaces_restic_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cls = _popen(monkeypatch, broken=True, rc=1, err=b"Fatal: wrong password")
    with pytest.raises(StoreError, match="wrong password"):
        _store(tmp_path).backup(_writer(), "c", WHEN, None)
    assert not cls.last.killed


def test_backup_broken_pipe_with_clean_exit_still_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _popen(monkeypatch, broken=True, rc=0)
    with pytest.raises(StoreError, match="stopped reading"):
        _store(tmp_path).backup(_writer(), "c", WHEN, None)


def test_identity_failure_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _popen(monkeypatch, rc=1, err=b"Fatal: error running password command: exit status 1")
    with pytest.raises(StoreError) as info:
        _store(tmp_path, identity="/keys/gone.txt").backup(_writer(), "c", WHEN, None)
    assert str(info.value) == "cannot decrypt the store password with /keys/gone.txt"


def test_identity_failure_message_for_real_restic_wording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    rec.returncode = 1
    rec.stderr = b'age: error: no identity matched\n{"message":"Fatal: Resolving password failed: exit status 1"}'
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    with pytest.raises(StoreError, match="cannot decrypt the store password with /keys/my key.txt"):
        _store(tmp_path).prune()


# ------------------------------------------------------------------ snapshots


def _entry(sid: str, time: str, cfg: str | None = "c", desc: str | None = None) -> dict[str, Any]:
    tags = ["config-saver"] + ([f"cfg:{cfg}"] if cfg else []) + ([encode_description(desc)] if desc else [])
    return {"id": sid * 64, "short_id": sid * 8, "time": time, "tags": tags}


def test_snapshots_parse_sort_and_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    rec.stdout = json.dumps(
        [
            _entry("b", "2026-10-19T12:00:00.123456789+00:00", desc="uno, dos"),
            _entry("a", "2026-10-18T12:00:00Z"),
            _entry("c", "2026-10-20T12:00:00+00:00", cfg=None),  # no cfg tag: ignored
        ]
    ).encode()
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    snaps = _store(tmp_path).snapshots("c")
    assert [s.short_id for s in snaps] == ["a" * 8, "b" * 8]
    assert snaps[1].description == "uno, dos"
    assert snaps[0].description is None
    argv = rec.calls[0]["argv"]
    assert argv[:3] == ["restic", "snapshots", "--json"]
    assert argv[argv.index("--tag") + 1] == "config-saver,cfg:c"
    _store(tmp_path).snapshots()
    argv = rec.calls[1]["argv"]
    assert argv[argv.index("--tag") + 1] == "config-saver"


def test_snapshots_empty_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    rec.stdout = b"null"
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    assert _store(tmp_path).snapshots() == []


def test_check_runs_restic_check_and_raises_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    _store(tmp_path).check()
    assert rec.calls[0]["argv"] == ["restic", "check"]
    rec.returncode = 1
    rec.stderr = b"pack abc is damaged"
    with pytest.raises(StoreError, match="damaged"):
        _store(tmp_path).check()


def test_forget_ids_forgets_only_those_and_never_prunes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    _store(tmp_path).forget_ids(["a1", "b2"])
    _store(tmp_path).forget_ids([])
    assert [c["argv"] for c in rec.calls] == [["restic", "forget", "a1", "b2"]]


def test_parse_restic_time_is_naive_local_and_handles_nanoseconds() -> None:
    parsed = parse_restic_time("2026-10-19T12:00:00.123456789+02:00")
    assert parsed.tzinfo is None
    expected = datetime.fromisoformat("2026-10-19T12:00:00.123456+02:00").astimezone().replace(tzinfo=None)
    assert parsed == expected
    assert parse_restic_time("2026-10-19T10:00:00Z") == parse_restic_time("2026-10-19T12:00:00+02:00")
    assert parse_restic_time("2026-10-19T10:00:00.5Z").microsecond == 500000


def test_description_roundtrip_unicode_and_commas() -> None:
    for text in ["héllo, wörld", "a,b,c", "日本語 ☃", "x", "ab", "abc", "tag:with:colons=and spaces"]:
        tag = encode_description(text)
        assert tag.startswith("desc:") and "," not in tag and "=" not in tag
        assert decode_description(tag) == text
    assert decode_description("desc:@@@@") is None


# ------------------------------------------------------------------------ dump


def test_dump_streams_and_maps_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cls = _popen(monkeypatch, out=b"TARDATA")
    out = io.BytesIO()
    _store(tmp_path).dump(SID, "own-configs", out)
    assert out.getvalue() == b"TARDATA"
    assert cls.last.argv == ["restic", "dump", SID, "/own-configs.tar"]
    _popen(monkeypatch, rc=1, err=b"no such snapshot")
    with pytest.raises(StoreError, match="no such snapshot"):
        _store(tmp_path).dump(SID, "c", io.BytesIO())


# ---------------------------------------------------------------------- forget


def _stamped_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, days: list[int]) -> Recorder:
    rec = Recorder(tmp_path)
    rec.stdout = json.dumps(
        [_entry(chr(97 + i), f"2026-10-{d:02d}T12:00:00+00:00") for i, d in enumerate(days)]
    ).encode()
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    return rec


def test_forget_uses_keep_set_and_never_newest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _stamped_store(tmp_path, monkeypatch, [10, 11, 12, 13])
    zero = RetentionModel(daily=0, weekly=0, monthly=0, yearly=0)
    removed = _store(tmp_path).forget("c", zero, dry_run=False)
    assert [s.short_id for s in removed] == ["a" * 8, "b" * 8, "c" * 8]  # newest ("d") survives
    forget_argv = rec.calls[-1]["argv"]
    assert forget_argv == ["restic", "forget", "a" * 64, "b" * 64, "c" * 64]
    assert "prune" not in forget_argv


def test_forget_matches_keep_set_for_default_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _stamped_store(tmp_path, monkeypatch, [1, 2, 3])
    removed = _store(tmp_path).forget("c", RetentionModel(), dry_run=False)
    assert removed == []
    assert len(rec.calls) == 1  # only the listing; nothing to forget


def test_forget_dry_run_runs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _stamped_store(tmp_path, monkeypatch, [10, 11, 12])
    zero = RetentionModel(daily=0, weekly=0, monthly=0, yearly=0)
    removed = _store(tmp_path).forget("c", zero, dry_run=True)
    assert len(removed) == 2
    assert [c["argv"][1] for c in rec.calls] == ["snapshots"]


def test_forget_empty_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    rec.stdout = b"[]"
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    assert _store(tmp_path).forget("c", RetentionModel(), dry_run=False) == []


def test_prune_and_run_error_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    _store(tmp_path).prune()
    assert rec.calls[0]["argv"] == ["restic", "prune"]
    assert rec.calls[0]["env"]["RESTIC_REPOSITORY"] == str(tmp_path / "restic")
    rec.returncode = 1
    rec.stderr = b"Fatal: unable to create lock in backend"
    with pytest.raises(StoreError, match="unable to create lock"):
        _store(tmp_path).prune()


def test_missing_restic_binary_is_store_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("restic")

    monkeypatch.setattr(rs.subprocess, "run", boom)
    with pytest.raises(StoreError, match="could not run restic"):
        _store(tmp_path).prune()
    monkeypatch.setattr(rs.subprocess, "Popen", boom)
    with pytest.raises(StoreError, match="could not run restic"):
        _store(tmp_path).backup(_writer(), "c", WHEN, None)
    with pytest.raises(StoreError, match="could not run restic"):
        _store(tmp_path).dump(SID, "c", io.BytesIO())


def test_env_drops_inherited_restic_vars_except_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESTIC_REPOSITORY_FILE", "/elsewhere")
    monkeypatch.setenv("RESTIC_KEY_HINT", "2")
    monkeypatch.setenv("RESTIC_CACHE_DIR", "/cache")
    env = _store(tmp_path)._env()
    assert "RESTIC_REPOSITORY_FILE" not in env and "RESTIC_KEY_HINT" not in env
    assert env["RESTIC_CACHE_DIR"] == "/cache"
    assert env["RESTIC_REPOSITORY"] == str(tmp_path / "restic")


def test_env_removes_restic_password_vars_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESTIC_PASSWORD", "x")
    monkeypatch.setenv("RESTIC_CACHE_DIR", "/cache")
    env = _store(tmp_path)._env()
    assert "RESTIC_PASSWORD" not in env
    assert env["RESTIC_CACHE_DIR"] == "/cache"
    assert os.environ["RESTIC_PASSWORD"] == "x"


def test_forget_also_keep_protects_a_snapshot_the_policy_would_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _stamped_store(tmp_path, monkeypatch, [10, 11, 12, 13])
    zero = RetentionModel(daily=0, weekly=0, monthly=0, yearly=0)
    protect = _store(tmp_path).snapshots("c")[1].time  # "b"
    removed = _store(tmp_path).forget("c", zero, dry_run=False, also_keep=[protect])
    assert [s.short_id for s in removed] == ["a" * 8, "c" * 8]
    assert rec.calls[-1]["argv"] == ["restic", "forget", "a" * 64, "c" * 64]


def test_snapshots_fill_tags_and_and_the_extra_tag_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    entry = _entry("a", "2026-10-18T12:00:00Z")
    entry["tags"] += ["sync", "parent:none"]
    rec.stdout = json.dumps([entry]).encode()
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    snaps = _store(tmp_path).snapshots("c", tag=SYNC_TAG)
    assert snaps[0].tags == ("config-saver", "cfg:c", "sync", "parent:none")
    argv = rec.calls[0]["argv"]
    assert argv[argv.index("--tag") + 1] == "config-saver,cfg:c,sync"
    _store(tmp_path).snapshots(tag=SYNC_TAG)
    argv = rec.calls[1]["argv"]
    assert argv[argv.index("--tag") + 1] == "config-saver,sync"


def test_snapshots_rejects_bad_tag_filter(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="tag"):
        _store(tmp_path).snapshots(tag="a,b")


# ------------------------------------------------------------ for_repository


def test_for_repository_env_has_password_and_nothing_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESTIC_PASSWORD_COMMAND", "leak")
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", "/leak")
    env = ResticStore.for_repository("/srv/r", "s3cret")._env()
    assert env["RESTIC_REPOSITORY"] == "/srv/r"
    assert env["RESTIC_PASSWORD"] == "s3cret"
    assert "RESTIC_PASSWORD_COMMAND" not in env and "RESTIC_PASSWORD_FILE" not in env


def test_for_repository_options_become_o_pairs_and_password_not_on_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Recorder(tmp_path)
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    store = ResticStore.for_repository("/srv/r", "s3cret", ["a=b", "c=d e"])
    store.init_plain()
    argv = rec.calls[0]["argv"]
    assert argv == ["restic", "-o", "a=b", "-o", "c=d e", "init"]
    assert "s3cret" not in " ".join(argv)
    assert rec.calls[0]["env"]["RESTIC_PASSWORD"] == "s3cret"


def test_for_repository_options_reach_backup_and_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    cls = _popen(monkeypatch, out=SUMMARY)
    store = ResticStore.for_repository("/srv/r", "p", ["a=b"])
    store.backup(_writer(), "cfg", WHEN, None)
    assert cls.last.argv[:4] == ["restic", "-o", "a=b", "backup"]
    store.dump(SID, "cfg", io.BytesIO())
    assert cls.last.argv[:5] == ["restic", "-o", "a=b", "dump", SID]


def test_default_constructor_is_unchanged(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store._argv(["snapshots"]) == ["restic", "snapshots"]
    assert "RESTIC_PASSWORD" not in store._env()


def test_for_repository_store_is_picklable() -> None:
    store = ResticStore.for_repository("/srv/r", "p", ["a=b"])
    again = pickle.loads(pickle.dumps(store))
    assert (again.repo, again._password, again._options) == ("/srv/r", "p", ("a=b",))


def test_exists_distinguishes_missing_from_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ResticStore.for_repository("/srv/r", "p")
    for rc, expected in ((0, True), (10, False)):
        rec = Recorder(tmp_path)
        rec.returncode = rc
        monkeypatch.setattr(rs.subprocess, "run", rec.run)
        assert store.exists() is expected
        assert rec.calls[0]["argv"] == ["restic", "cat", "config"]
    rec.returncode = 12
    with pytest.raises(StoreError):
        store.exists()


def test_copy_from_passes_source_credentials_by_env_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    dst = ResticStore.for_repository("/dst", "dstpw")
    dst.copy_from(ResticStore.for_repository("/src", "srcpw"), [SID])
    call = rec.calls[0]
    assert call["argv"] == ["restic", "copy", SID]
    assert call["env"]["RESTIC_FROM_REPOSITORY"] == "/src"
    assert call["env"]["RESTIC_FROM_PASSWORD"] == "srcpw"
    assert call["env"]["RESTIC_PASSWORD"] == "dstpw" and call["env"]["RESTIC_REPOSITORY"] == "/dst"
    assert "srcpw" not in " ".join(call["argv"])


def test_copy_from_age_store_uses_password_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    ResticStore.for_repository("/dst", "p").copy_from(_store(tmp_path), [SID])
    env = rec.calls[0]["env"]
    assert env["RESTIC_FROM_REPOSITORY"] == str(tmp_path / "restic")
    assert env["RESTIC_FROM_PASSWORD_COMMAND"].startswith("age -d -i ")
    assert "RESTIC_FROM_PASSWORD" not in env


def _copy_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dst: list[str], src: list[str]) -> list[str]:
    rec = Recorder(tmp_path)
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    ResticStore.for_repository("/dst", "p", dst).copy_from(ResticStore.for_repository("/src", "q", src), [SID])
    return rec.calls[0]["argv"]


def test_copy_from_applies_source_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _copy_argv(tmp_path, monkeypatch, [], ["rclone.args=serve"]) == [
        "restic", "-o", "rclone.args=serve", "copy", SID,
    ]  # fmt: skip


def test_copy_from_dedupes_identical_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv = _copy_argv(tmp_path, monkeypatch, ["a=b", "c=d"], ["a=b", "e=f"])
    assert argv == ["restic", "-o", "a=b", "-o", "c=d", "-o", "e=f", "copy", SID]


def test_copy_from_conflicting_options_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(StoreError, match="conflicting"):
        _copy_argv(tmp_path, monkeypatch, ["a=b"], ["a=c"])


def test_copy_from_without_ids_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path)
    monkeypatch.setattr(rs.subprocess, "run", rec.run)
    with pytest.raises(StoreError, match="no snapshot ids"):
        ResticStore.for_repository("/dst", "p").copy_from(ResticStore.for_repository("/src", "q"), [])
    assert rec.calls == []
