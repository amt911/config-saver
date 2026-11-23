"""SyncEngine end to end: two fake machines (own HOME, state, sync dir) and one bare remote.

Real restic, age and git are used on purpose: the engine's job is the interplay of
those with the real filesystem, and mocking them would prove nothing.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path

import pytest

from config_saver.lib.errors import (
    ArchiveError,
    SyncConflictError,
    SyncNotSetUpError,
    SyncTransportError,
    UnsafeArchiveError,
)
from config_saver.lib.restic_store import HOST_TAG_PREFIX, PARENT_TAG_PREFIX, SYNC_TAG, ResticStore
from config_saver.lib.sync import state as state_io
from config_saver.lib.sync.classify import Fingerprint
from config_saver.lib.sync.engine import SyncEngine
from config_saver.lib.sync.git_repo import GitRepo
from config_saver.lib.sync.manifest import MANIFEST_MEMBER, SELF, Manifest
from tests.conftest import make_model

pytestmark = [
    pytest.mark.restic,
    pytest.mark.skipif(
        any(shutil.which(b) is None for b in ("restic", "age", "age-keygen", "git")),
        reason="needs restic, age, age-keygen and git on PATH",
    ),
]

SECRET = "TOP-SECRET-MARKER-9f3a"


def git(path: str, *args: str) -> str:
    return subprocess.run(["git", "-C", path, *args], check=True, capture_output=True, text=True).stdout


class Machine:
    """One fake machine: HOME, sync dir, state file, and an engine bound to them."""

    def __init__(self, root: Path, name: str, user: str, key: str, git_cls: type[GitRepo] = GitRepo):
        self.home = root / name / "homes" / user
        self.home.mkdir(parents=True)
        self.sync_dir = str(root / name / "sync")
        self.state_file = str(root / name / "state.json")
        self.git = git_cls(self.sync_dir)
        self.engine = SyncEngine(self.sync_dir, self.state_file, key, name, git=self.git)

    def model(self, *dirs: str, **kwargs):
        return make_model([str(self.home / d) for d in dirs], sync=True, **kwargs)

    def use(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(self.home))

    def write(self, rel: str, text: str, mode: int | None = None) -> Path:
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if mode is not None:
            path.chmod(mode)
        return path

    def head(self) -> str:
        return subprocess.run(
            ["git", "-C", self.sync_dir, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RESTIC_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    key = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    public = subprocess.run(["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    return {"key": str(key), "recipient": public.stdout.strip(), "remote": str(remote), "root": tmp_path}


def machine(env, name: str, user: str = "tester", git_cls: type[GitRepo] = GitRepo) -> Machine:
    return Machine(env["root"], name, user, env["key"], git_cls)


@pytest.fixture
def pair(env, monkeypatch):
    """Machine A creates the remote, machine B clones it; HOME is A's."""
    a, b = machine(env, "a"), machine(env, "b")
    a.use(monkeypatch)
    a.engine.init(env["remote"], env["recipient"])
    b.use(monkeypatch)
    b.engine.init(env["remote"], None)
    a.use(monkeypatch)
    return a, b


def test_edit_on_a_arrives_on_b(pair, monkeypatch):
    a, b = pair
    a.write("cfg/app.conf", "v1\n", mode=0o640)
    a.write("cfg/run.sh", "#!/bin/sh\n", mode=0o755)
    (a.home / "cfg" / "link").symlink_to("app.conf")
    report = a.engine.run({"c": a.model("cfg")})
    assert sorted(report.published["c"]) == [
        "home/user/cfg/app.conf",
        "home/user/cfg/link",
        "home/user/cfg/run.sh",
    ]

    b.use(monkeypatch)
    report = b.engine.run({"c": b.model("cfg")})
    assert len(report.applied["c"]) == 3
    assert (b.home / "cfg" / "app.conf").read_bytes() == b"v1\n"
    assert (b.home / "cfg" / "app.conf").stat().st_mode & 0o777 == 0o640
    assert (b.home / "cfg" / "run.sh").stat().st_mode & 0o777 == 0o755
    assert os.readlink(b.home / "cfg" / "link") == "app.conf"
    assert not os.path.exists(os.path.join(b.sync_dir, ".staging"))

    a.use(monkeypatch)
    a.write("cfg/app.conf", "v2\n", mode=0o640)
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    assert (b.home / "cfg" / "app.conf").read_bytes() == b"v2\n"


def _synced_pair(pair, monkeypatch, text="v1\n"):
    a, b = pair
    a.write("cfg/app.conf", text)
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    return a, b


def test_conflict_aborts_without_writes(pair, monkeypatch):
    a, b = _synced_pair(pair, monkeypatch)
    a.use(monkeypatch)
    a.write("cfg/app.conf", "from a\n")
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    b.write("cfg/app.conf", "from b\n")
    before_state = Path(b.state_file).read_bytes()

    with pytest.raises(SyncConflictError) as exc:
        b.engine.run({"c": b.model("cfg")})
    assert exc.value.conflicts == {"c": ["home/user/cfg/app.conf"]}
    assert (b.home / "cfg" / "app.conf").read_text() == "from b\n"
    assert Path(b.state_file).read_bytes() == before_state
    # The pull that makes classification possible may fast-forward; nothing of ours is committed.
    assert git(b.sync_dir, "rev-list", "@{u}..HEAD") == ""
    assert git(b.sync_dir, "status", "--porcelain") == ""


def test_status_reports_without_writing(pair, monkeypatch):
    a, b = _synced_pair(pair, monkeypatch)
    a.use(monkeypatch)
    a.write("cfg/app.conf", "from a\n")
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    before_state = Path(b.state_file).read_bytes()
    report = b.engine.status({"c": b.model("cfg")})
    assert report.applied == {"c": ["home/user/cfg/app.conf"]}
    assert (b.home / "cfg" / "app.conf").read_text() == "v1\n"
    assert Path(b.state_file).read_bytes() == before_state


@pytest.mark.parametrize("side", ["local", "remote"])
def test_resolve_then_the_other_side_gets_it(pair, monkeypatch, side):
    a, b = _synced_pair(pair, monkeypatch)
    a.use(monkeypatch)
    a.write("cfg/app.conf", "from a\n")
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    b.write("cfg/app.conf", "from b\n")
    path = "home/user/cfg/app.conf"
    with pytest.raises(ValueError, match="nope"):
        b.engine.resolve({"c": b.model("cfg")}, side, ["nope"])
    b.engine.resolve({"c": b.model("cfg")}, side, [path])
    winner = "from b\n" if side == "local" else "from a\n"
    assert (b.home / "cfg" / "app.conf").read_text() == winner
    a.use(monkeypatch)
    a.engine.run({"c": a.model("cfg")})
    assert (a.home / "cfg" / "app.conf").read_text() == winner


def test_delete_propagates(pair, monkeypatch):
    a, b = _synced_pair(pair, monkeypatch)
    a.use(monkeypatch)
    (a.home / "cfg" / "app.conf").unlink()
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    report = b.engine.run({"c": b.model("cfg")})
    assert report.deleted == {"c": ["home/user/cfg/app.conf"]}
    assert not (b.home / "cfg" / "app.conf").exists()


def test_delete_vs_modify_conflicts(pair, monkeypatch):
    a, b = _synced_pair(pair, monkeypatch)
    a.use(monkeypatch)
    (a.home / "cfg" / "app.conf").unlink()
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    b.write("cfg/app.conf", "edited\n")
    with pytest.raises(SyncConflictError):
        b.engine.run({"c": b.model("cfg")})
    assert (b.home / "cfg" / "app.conf").read_text() == "edited\n"
    b.engine.resolve({"c": b.model("cfg")}, "local", None)
    a.use(monkeypatch)
    a.engine.run({"c": a.model("cfg")})
    assert (a.home / "cfg" / "app.conf").read_text() == "edited\n"


def test_narrower_yaml_does_not_delete_wider_files(pair, monkeypatch):
    a, b = pair
    a.write("one/x.txt", "x\n")
    a.write("two/y.txt", "y\n")
    a.engine.run({"c": a.model("one", "two")})
    b.use(monkeypatch)
    b.write("one/x.txt", "x\n")
    b.engine.run({"c": b.model("one")})  # narrower: never sees two/
    assert not (b.home / "two").exists()
    b.write("one/x.txt", "x2\n")
    b.engine.run({"c": b.model("one")})  # publishes while carrying two/y.txt
    a.use(monkeypatch)
    a.engine.run({"c": a.model("one", "two")})
    assert (a.home / "two" / "y.txt").read_text() == "y\n"
    assert (a.home / "one" / "x.txt").read_text() == "x2\n"


def test_different_usernames_no_conflict(env, monkeypatch):
    a, b = machine(env, "a", "tester"), machine(env, "b", "amt")
    a.use(monkeypatch)
    a.engine.init(env["remote"], env["recipient"])
    b.use(monkeypatch)
    b.engine.init(env["remote"], None)
    a.use(monkeypatch)
    a.write("cfg/paths.conf", f"home={a.home}/cfg\n")
    a.engine.run({"c": a.model("cfg", normalize_content=True)})
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg", normalize_content=True)})
    assert (b.home / "cfg" / "paths.conf").read_text() == f"home={b.home}/cfg\n"
    # no edits since: a re-run on either side is a no-op, not a conflict
    report = b.engine.run({"c": b.model("cfg", normalize_content=True)})
    assert report.applied == {} and report.published == {}
    a.use(monkeypatch)
    assert a.engine.run({"c": a.model("cfg", normalize_content=True)}).applied == {}


def _craft_snapshot(m: Machine, cfg: str, members: dict[str, bytes], manifest: Manifest, parent: str = "none"):
    """Publish a hand-made sync snapshot: the attack a hostile remote could mount."""

    def write(out) -> object:
        with tarfile.open(fileobj=out, mode="w|") as tar:
            for name, payload in {MANIFEST_MEMBER: manifest.to_json(), **members}.items():
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(payload))
        return None

    store = ResticStore(m.sync_dir, m.engine.store.identity)
    tags = [SYNC_TAG, PARENT_TAG_PREFIX + parent, HOST_TAG_PREFIX + "evil"]
    snap, _, _ = store.backup(write, cfg, datetime.now(), None, extra_tags=tags)  # type: ignore[arg-type]
    assert m.git.commit_all() and m.git.push()
    return snap


def _fp(data: bytes) -> Fingerprint:
    import hashlib

    return Fingerprint(hashlib.sha256(data).hexdigest(), 0o644)


def test_malicious_member_refused(pair, monkeypatch):
    a, b = pair
    good = b"fine\n"
    manifest = Manifest("c", ["home/user/cfg/ok.txt"], {"home/user/cfg/ok.txt": (_fp(good), SELF)})
    _craft_snapshot(
        a,
        "c",
        {"home/user/../../evil": b"pwned", "home/user/cfg/ok.txt": good},  # evil comes first: no partial restore
        manifest,
    )
    b.use(monkeypatch)
    before = sorted(p.name for p in b.home.parent.iterdir())
    with pytest.raises(UnsafeArchiveError):
        b.engine.run({"c": b.model("cfg")})
    assert sorted(p.name for p in b.home.parent.iterdir()) == before
    assert not (b.home.parent.parent / "evil").exists() and not (b.home.parent / "evil").exists()
    assert not (b.home / "cfg" / "ok.txt").exists()
    assert not os.path.exists(Path(b.state_file))


def test_malicious_member_after_good_one_writes_nothing(pair, monkeypatch):
    a, b = pair
    good = b"fine\n"
    manifest = Manifest("c", ["home/user/cfg/ok.txt"], {"home/user/cfg/ok.txt": (_fp(good), SELF)})
    snap_members = {"home/user/cfg/ok.txt": good, "home/user/../../evil": b"pwned"}
    _craft_snapshot(a, "c", snap_members, manifest)
    b.use(monkeypatch)
    with pytest.raises(UnsafeArchiveError):
        b.engine.run({"c": b.model("cfg")})
    assert not (b.home / "cfg" / "ok.txt").exists()


def test_out_of_scope_member_not_written(pair, monkeypatch):
    a, b = pair
    a.write("cfg/ok.txt", "ok\n")
    a.write("other/secret.txt", "private\n")
    a.engine.run({"c": a.model("cfg", "other")})
    b.use(monkeypatch)
    report = b.engine.run({"c": b.model("cfg")})
    assert report.applied == {"c": ["home/user/cfg/ok.txt"]}
    assert not (b.home / "other").exists()


def test_rerun_after_push_before_state_save(pair, monkeypatch):
    a, b = pair
    a.write("cfg/app.conf", "v1\n")
    a.engine.run({"c": a.model("cfg")})
    published_head = a.head()
    os.unlink(a.state_file)  # the crash: push done, state never saved
    report = a.engine.run({"c": a.model("cfg")})
    assert report.applied == {} and report.published == {} and report.conflicts == {}
    assert a.head() == published_head
    assert state_io.load(a.state_file).base["c"]


def test_concurrent_push_retries(env, monkeypatch):
    log: list[str] = []

    class Racing(GitRepo):
        """Lets machine A publish between B's pull and B's first push."""

        def push(self) -> bool:
            if not log:
                log.append("raced")
                a.use(monkeypatch)
                a.write("cfg/from_a.txt", "a\n")
                a.engine.run({"c": a.model("cfg")})
                b.use(monkeypatch)
            return super().push()

    a, b = machine(env, "a"), machine(env, "b", git_cls=Racing)
    a.use(monkeypatch)
    a.engine.init(env["remote"], env["recipient"])
    b.use(monkeypatch)
    b.engine.init(env["remote"], None)
    b.write("cfg/from_b.txt", "b\n")
    b.engine.run({"c": b.model("cfg")})
    assert log == ["raced"]
    assert (b.home / "cfg" / "from_a.txt").read_text() == "a\n"
    a.use(monkeypatch)
    a.engine.run({"c": a.model("cfg")})
    assert (a.home / "cfg" / "from_b.txt").read_text() == "b\n"
    # one linear chain of sync snapshots: exactly one head, every other is somebody's parent
    snaps = b.engine.store.snapshots("c", tag=SYNC_TAG)
    parents = {t for s in snaps for t in s.tags if t.startswith(PARENT_TAG_PREFIX)}
    assert len(snaps) == 2 and len(parents) == 2


def _all_bytes(*roots: str) -> bytes:
    blob = b""
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                with open(os.path.join(dirpath, name), "rb") as fh:
                    blob += fh.read()
    return blob


def test_non_sync_config_never_uploaded(pair):
    a, _b = pair
    a.write("cfg/app.conf", "shared\n")
    a.write("private/notes.txt", SECRET + "\n")
    private = make_model([str(a.home / "private")])  # no sync: true
    a.engine.run({"c": a.model("cfg"), "p": private})
    assert not a.engine.store.snapshots("p")
    dumped = io.BytesIO()
    a.engine.store.dump(a.engine.store.snapshots("c")[-1].id, "c", dumped)
    assert SECRET.encode() not in dumped.getvalue()
    assert b"private" not in dumped.getvalue()


def test_remote_has_no_plaintext(pair, env):
    a, _b = pair
    a.write("cfg/app.conf", f"token={SECRET}\n")
    a.engine.run({"c": a.model("cfg")})
    assert SECRET.encode() not in _all_bytes(env["remote"])
    assert b"app.conf" not in _all_bytes(env["remote"])  # the manifest (paths) is encrypted too


def test_not_set_up(env, monkeypatch):
    a = machine(env, "a")
    a.use(monkeypatch)
    with pytest.raises(SyncNotSetUpError):
        a.engine.run({"c": a.model("cfg")})


def test_init_needs_recipient_on_empty_remote_and_refuses_twice(env, monkeypatch):
    a = machine(env, "a")
    a.use(monkeypatch)
    with pytest.raises(SyncNotSetUpError, match="--encrypt-to"):
        a.engine.init(env["remote"], None)
    assert not os.path.exists(a.sync_dir)
    a.engine.init(env["remote"], env["recipient"])
    assert os.stat(a.sync_dir).st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        a.engine.init(env["remote"], env["recipient"])


def test_skipped_remote_lists_unknown_configs(pair, monkeypatch):
    a, b = pair
    a.write("cfg/app.conf", "x\n")
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    assert b.engine.status({"other": b.model("cfg2")}).skipped_remote == ["c"]


def test_symlink_and_file_swap_across_machines(pair, monkeypatch):
    a, b = pair
    a.write("cfg/victim.txt", "victim\n")
    a.write("cfg/was_file", "plain\n")
    (a.home / "cfg" / "was_link").symlink_to("victim.txt")
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    a.use(monkeypatch)
    (a.home / "cfg" / "was_file").unlink()
    (a.home / "cfg" / "was_file").symlink_to("victim.txt")  # file -> symlink
    (a.home / "cfg" / "was_link").unlink()
    a.write("cfg/was_link", "now a file\n")  # symlink -> file
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    assert os.readlink(b.home / "cfg" / "was_file") == "victim.txt"
    assert not (b.home / "cfg" / "was_link").is_symlink()
    assert (b.home / "cfg" / "was_link").read_text() == "now a file\n"
    assert (b.home / "cfg" / "victim.txt").read_text() == "victim\n"  # never written through the old link


def test_hostile_later_group_changes_nothing(pair, monkeypatch):
    a, b = pair
    a.write("cfg/old.txt", "old\n")
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    # A edits old.txt (group 1: a good snapshot), then a crafted snapshot adds new.txt next to a hostile member.
    a.use(monkeypatch)
    a.write("cfg/old.txt", "old v2\n")
    a.engine.run({"c": a.model("cfg")})
    good_head = a.engine.store.snapshots("c", tag=SYNC_TAG)[-1].id
    new = b"new\n"
    manifest = Manifest(
        "c",
        ["home/user/cfg/old.txt", "home/user/cfg/new.txt"],
        {
            "home/user/cfg/old.txt": (_fp(b"old v2\n"), good_head),
            "home/user/cfg/new.txt": (_fp(new), SELF),
        },
    )
    _craft_snapshot(a, "c", {"home/user/cfg/new.txt": new, "home/user/../../evil": b"x"}, manifest, parent=good_head)
    b.use(monkeypatch)
    with pytest.raises(UnsafeArchiveError):
        b.engine.run({"c": b.model("cfg")})
    assert (b.home / "cfg" / "old.txt").read_text() == "old\n"
    assert not (b.home / "cfg" / "new.txt").exists()


def test_a_snapshot_missing_a_wanted_member_changes_nothing(pair, monkeypatch):
    a, b = pair
    manifest = Manifest("c", ["home/user/cfg/x.txt"], {"home/user/cfg/x.txt": (_fp(b"x"), SELF)})
    _craft_snapshot(a, "c", {}, manifest)  # promises x.txt, holds nothing
    b.use(monkeypatch)
    with pytest.raises(ArchiveError):
        b.engine.run({"c": b.model("cfg")})
    assert not (b.home / "cfg").exists()


def test_second_sync_at_the_same_time_fails_fast(pair):
    import fcntl

    a, _b = pair
    a.write("cfg/app.conf", "x\n")
    fd = os.open(os.path.join(a.sync_dir, ".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for verb in (
            lambda: a.engine.run({"c": a.model("cfg")}),
            lambda: a.engine.status({"c": a.model("cfg")}),
            lambda: a.engine.resolve({"c": a.model("cfg")}, "local", None),
        ):
            with pytest.raises(SyncTransportError, match="another sync is running"):
                verb()
        assert not subprocess.run(
            ["git", "-C", a.sync_dir, "status", "--porcelain"], capture_output=True, text=True
        ).stdout
    finally:
        os.close(fd)
    a.engine.run({"c": a.model("cfg")})  # the lock is released: it works again


def test_git_metadata_names_nothing(pair, env):
    a, _b = pair
    a.write("cfg/app.conf", "x\n")
    a.engine.run({"c": a.model("cfg")})
    log = git(env["remote"], "log", "--all", "--format=%an|%ae|%cn|%ce|%s|%b")
    assert log.strip()
    for line in log.splitlines():
        assert line == "config-saver|config-saver@localhost|config-saver|config-saver@localhost|sync|"
    for secret in ("cfg", "app.conf", "homes", env["root"].name):
        assert secret not in log


def test_push_race_lost_every_time_gives_up(env, monkeypatch):
    class AlwaysStale(GitRepo):
        def push(self) -> bool:
            return False

    a = machine(env, "a")
    a.use(monkeypatch)
    a.engine.init(env["remote"], env["recipient"])
    b = machine(env, "b", git_cls=AlwaysStale)
    b.use(monkeypatch)
    b.engine.init(env["remote"], None)
    b.write("cfg/x.txt", "x\n")
    with pytest.raises(SyncTransportError, match="kept pushing"):
        b.engine.run({"c": b.model("cfg")})
    assert not os.path.exists(b.state_file)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 file")
def test_unreadable_file_never_publishes_a_deletion(pair, monkeypatch):
    from config_saver.lib.errors import SyncUnreadableError

    a, b = pair
    f = a.write("cfg/app.conf", "v1\n")
    a.engine.run({"c": a.model("cfg")})
    state_before = Path(a.state_file).read_bytes()
    head_before = a.head()
    f.chmod(0)
    try:
        with pytest.raises(SyncUnreadableError, match="app.conf"):
            a.engine.run({"c": a.model("cfg")})
    finally:
        f.chmod(0o600)
    assert Path(a.state_file).read_bytes() == state_before
    assert a.head() == head_before
    git(a.sync_dir, "fetch", "origin")
    assert git(a.sync_dir, "rev-list", "HEAD..@{u}") == ""  # nothing new on the remote
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    assert (b.home / "cfg" / "app.conf").read_text() == "v1\n"  # B still has its copy


def test_root_owned_file_never_publishes_a_deletion(pair, monkeypatch):
    from config_saver.lib.errors import SyncUnreadableError
    from config_saver.lib.tar_compressor.tar_compressor import TarCompressor

    a, _b = pair
    a.write("cfg/app.conf", "v1\n")
    a.engine.run({"c": a.model("cfg")})
    state_before = Path(a.state_file).read_bytes()
    head_before = a.head()
    monkeypatch.setattr(TarCompressor, "_skips_root_owned", lambda self, path: path.endswith("app.conf"))
    with pytest.raises(SyncUnreadableError, match="owned by root"):
        a.engine.run({"c": a.model("cfg")})
    assert Path(a.state_file).read_bytes() == state_before
    assert a.head() == head_before


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches a 0o000 directory")
def test_unreachable_parent_never_publishes_a_deletion(pair):
    from config_saver.lib.errors import SyncUnreadableError

    a, _b = pair
    a.write("d/f", "v1\n")
    model = make_model([str(a.home / "d" / "f")], sync=True)
    a.engine.run({"c": model})
    state_before = Path(a.state_file).read_bytes()
    head_before = a.head()
    (a.home / "d").chmod(0)
    try:
        with pytest.raises(SyncUnreadableError, match="d/f"):
            a.engine.run({"c": model})
    finally:
        (a.home / "d").chmod(0o700)
    assert Path(a.state_file).read_bytes() == state_before
    assert a.head() == head_before
    git(a.sync_dir, "fetch", "origin")
    assert git(a.sync_dir, "rev-list", "HEAD..@{u}") == ""
