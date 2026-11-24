"""SyncEngine end to end: two fake machines (own HOME, state, sync dir) and one bare remote.

Real restic and age are used on purpose (the repository is a local path in tmp_path): the
engine's job is the interplay of those with the real filesystem, and mocking them proves nothing.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from config_saver.lib.errors import (
    ArchiveError,
    SyncConflictError,
    SyncStateError,
    SyncTransportError,
    UnsafeArchiveError,
)
from config_saver.lib.restic_store import HOST_TAG_PREFIX, PARENT_TAG_PREFIX, SYNC_TAG, ResticStore, Snapshot
from config_saver.lib.sync import state as state_io
from config_saver.lib.sync.classify import Fingerprint
from config_saver.lib.sync.engine import SyncEngine
from config_saver.lib.sync.manifest import MANIFEST_MEMBER, SELF, Manifest, SnapRef, losers, select_head
from config_saver.lib.sync.remote import SyncRemote, derive_password
from tests.conftest import make_model

pytestmark = [
    pytest.mark.restic,
    pytest.mark.skipif(
        any(shutil.which(b) is None for b in ("restic", "age", "age-keygen")),
        reason="needs restic, age and age-keygen on PATH",
    ),
]

SECRET = "TOP-SECRET-MARKER-9f3a"


class Machine:
    """One fake machine: HOME, state, staging and lock paths, an engine on the shared repository."""

    def __init__(
        self, root: Path, name: str, user: str, key: str, repo: str, store_cls: type[ResticStore] | None = None
    ):
        self.home = root / name / "homes" / user
        self.home.mkdir(parents=True)
        self.sync_dir = str(root / name / "sync")
        self.state_file = str(root / name / "state.json")
        self.lock_file = str(root / name / "state" / "sync.lock")
        remote = SyncRemote(repo)
        store = None
        if store_cls is not None:
            store = store_cls.for_repository(repo, derive_password(key), ())
        self.engine = SyncEngine(remote, key, self.state_file, self.sync_dir, self.lock_file, name, store=store)

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
        """The winning head of cfg ``c`` in the shared repository."""
        snaps = self.engine.store.snapshots("c", tag=SYNC_TAG)
        return select_head([SnapRef(x.id, _parent(x), x.time) for x in snaps]) or ""


def _parent(snap) -> str | None:
    for tag in snap.tags:
        if tag.startswith(PARENT_TAG_PREFIX):
            value = tag[len(PARENT_TAG_PREFIX) :]
            return None if value == "none" else value
    return None


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RESTIC_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    key = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    return {"key": str(key), "repo": str(tmp_path / "repo"), "root": tmp_path}


def machine(env, name: str, user: str = "tester", store_cls: type[ResticStore] | None = None) -> Machine:
    return Machine(env["root"], name, user, env["key"], env["repo"], store_cls)


@pytest.fixture
def pair(env, monkeypatch):
    """Machine A creates the repository, machine B joins it; HOME is A's."""
    a, b = machine(env, "a"), machine(env, "b")
    a.use(monkeypatch)
    assert a.engine.init() == "created"
    b.use(monkeypatch)
    assert b.engine.init() == "joined"
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
    assert len(b.engine.store.snapshots("c", tag=SYNC_TAG)) == 2  # nothing of ours was published


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
    a.engine.init()
    b.use(monkeypatch)
    b.engine.init()
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

    store = m.engine.store
    tags = [SYNC_TAG, PARENT_TAG_PREFIX + parent, HOST_TAG_PREFIX + "evil"]
    snap, _, _ = store.backup(write, cfg, datetime.now(), None, extra_tags=tags)  # type: ignore[arg-type]
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


def test_fork_race_one_winner_loser_retries(env, monkeypatch):
    log: list[str] = []

    class Racing(ResticStore):
        """Machine A publishes (two roots race) just before B's first backup lands; B's clock is ahead, so B loses."""

        def backup(self, write, cfg, when, description, extra_tags: Sequence[str] = ()):
            if not log:
                log.append("raced")
                a.use(monkeypatch)
                a.write("cfg/from_a.txt", "a\n")
                a.engine.run({"c": a.model("cfg")})
                b.use(monkeypatch)
                when = when + timedelta(hours=1)
            return super().backup(write, cfg, when, description, extra_tags)

    a, b = machine(env, "a"), machine(env, "b", store_cls=Racing)
    a.use(monkeypatch)
    a.engine.init()
    b.use(monkeypatch)
    b.write("cfg/from_b.txt", "b\n")
    b.engine.run({"c": b.model("cfg")})
    assert log == ["raced"]
    assert (b.home / "cfg" / "from_a.txt").read_text() == "a\n"
    a.use(monkeypatch)
    a.engine.run({"c": a.model("cfg")})
    assert (a.home / "cfg" / "from_b.txt").read_text() == "b\n"
    # B's losing first snapshot was forgotten: one linear chain of two snapshots
    snaps = b.engine.store.snapshots("c", tag=SYNC_TAG)
    assert len(snaps) == 2
    assert losers([SnapRef(x.id, _parent(x), x.time) for x in snaps]) == set()


def test_a_snapshot_another_machine_built_on_is_not_forgotten(env, monkeypatch):
    log: list[str] = []

    class Overtaken(ResticStore):
        """Machine A analyses right after B's backup lands, so A's snapshot is B's child."""

        def backup(self, write, cfg, when, description, extra_tags: Sequence[str] = ()):
            snap = super().backup(write, cfg, when, description, extra_tags)
            if not log:
                log.append("built-on")
                a.use(monkeypatch)
                a.write("cfg/from_a.txt", "a\n")
                a.engine.run({"c": a.model("cfg")})
                b.use(monkeypatch)
            return snap

    a, b = machine(env, "a"), machine(env, "b", store_cls=Overtaken)
    a.use(monkeypatch)
    a.engine.init()
    b.use(monkeypatch)
    b.write("cfg/from_b.txt", "b\n")
    b.engine.run({"c": b.model("cfg")})
    assert len(b.engine.store.snapshots("c", tag=SYNC_TAG)) == 2  # B's was not forgotten under A's feet
    b.engine.run({"c": b.model("cfg")})
    assert (b.home / "cfg" / "from_a.txt").read_text() == "a\n"
    a.use(monkeypatch)
    a.engine.run({"c": a.model("cfg")})
    assert (a.home / "cfg" / "from_b.txt").read_text() == "b\n"


def test_join_existing_repo(pair, env):
    a, _b = pair
    a.write("cfg/app.conf", "x\n")
    a.engine.run({"c": a.model("cfg")})
    before = [s.id for s in a.engine.store.snapshots()]
    c = machine(env, "c")
    assert c.engine.init() == "joined"
    assert [s.id for s in c.engine.store.snapshots()] == before  # nothing was written


def test_create_new_repo(env):
    assert not os.path.exists(env["repo"])
    a = machine(env, "a")
    assert a.engine.init() == "created"
    assert os.path.exists(os.path.join(env["repo"], "config"))
    assert a.engine.init() == "joined"


def test_restic_failure_is_transport_error(pair, env):
    a, _b = pair
    a.write("cfg/app.conf", "x\n")
    other_key = env["root"] / "other-key.txt"
    subprocess.run(["age-keygen", "-o", str(other_key)], check=True, capture_output=True)
    wrong = SyncEngine(SyncRemote(env["repo"]), str(other_key), a.state_file, a.sync_dir, a.lock_file, "x")
    with pytest.raises(SyncTransportError):  # a wrong password is not "no repository"
        wrong.init()
    shutil.rmtree(env["repo"])
    for verb in (
        lambda: a.engine.run({"c": a.model("cfg")}),
        lambda: a.engine.status({"c": a.model("cfg")}),
        lambda: a.engine.resolve({"c": a.model("cfg")}, "local", None),
    ):
        with pytest.raises(SyncTransportError, match="restic"):
            verb()
    assert not os.path.exists(a.state_file)


def test_all_cycle_history_is_a_state_error_not_a_fresh_root(pair):
    a, _b = pair

    class Cyclic(ResticStore):
        """A listing in which two sync snapshots name each other: no root, no head."""

        def snapshots(self, cfg=None, tag=None):
            when = datetime(2026, 1, 1)
            return [
                Snapshot("id1", "id1", when, "c", None, (SYNC_TAG, PARENT_TAG_PREFIX + "id2")),
                Snapshot("id2", "id2", when, "c", None, (SYNC_TAG, PARENT_TAG_PREFIX + "id1")),
            ]

    a.engine.store = Cyclic.for_repository(a.engine.store.repo, a.engine.store._password or "", ())
    a.write("cfg/app.conf", "x\n")
    with pytest.raises(SyncStateError):
        a.engine.run({"c": a.model("cfg")})


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
    assert SECRET.encode() not in _all_bytes(env["repo"])
    assert b"app.conf" not in _all_bytes(env["repo"])  # the manifest (paths) is encrypted too


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
    os.makedirs(os.path.dirname(a.lock_file))
    fd = os.open(a.lock_file, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for verb in (
            lambda: a.engine.run({"c": a.model("cfg")}),
            lambda: a.engine.status({"c": a.model("cfg")}),
            lambda: a.engine.resolve({"c": a.model("cfg")}, "local", None),
        ):
            with pytest.raises(SyncTransportError, match="another sync is running"):
                verb()
        assert not a.engine.store.snapshots("c")  # the refused verbs published nothing
    finally:
        os.close(fd)
    a.engine.run({"c": a.model("cfg")})  # the lock is released: it works again


def test_publish_race_lost_every_time_gives_up(env, monkeypatch):
    class AlwaysLosing(ResticStore):
        """B's clock runs ahead and machine A publishes something new just before every backup of B."""

        def backup(self, write, cfg, when, description, extra_tags: Sequence[str] = ()):
            a.use(monkeypatch)
            a.write(f"cfg/from_a_{len(raced)}.txt", "a\n")
            raced.append(1)
            a.engine.run({"c": a.model("cfg")})
            b.use(monkeypatch)
            return super().backup(write, cfg, when + timedelta(hours=1), description, extra_tags)

    raced: list[int] = []
    a = machine(env, "a")
    a.use(monkeypatch)
    a.engine.init()
    b = machine(env, "b", store_cls=AlwaysLosing)
    b.use(monkeypatch)
    b.write("cfg/x.txt", "x\n")
    with pytest.raises(SyncTransportError, match="kept publishing"):
        b.engine.run({"c": b.model("cfg")})
    assert len(raced) == 3
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
    assert a.head() == head_before  # nothing new in the repository
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
    assert len(a.engine.store.snapshots("c", tag=SYNC_TAG)) == 1


# ------------------------------------------------- the winner flips after a publisher finished


class _Skewed(ResticStore):
    """B's store: before its first backup lands, `before(self)` runs (A publishes); the backup is dated earlier.

    ``crash`` simulates B dying right after its backup, before it re-lists (an orphan snapshot).
    """

    before = None
    crash = False
    fired = False

    def backup(self, write, cfg, when, description, extra_tags: Sequence[str] = ()):
        if not type(self).fired:
            type(self).fired = True
            type(self).before()  # type: ignore[misc]
            when = when - timedelta(hours=1)
            snap = super().backup(write, cfg, when, description, extra_tags)
            if type(self).crash:
                raise RuntimeError("B crashed before re-listing")
            return snap
        return super().backup(write, cfg, when, description, extra_tags)


def _flip(env, monkeypatch, *, same_path: bool, crash: bool = False):
    """Both machines synced on P. A publishes X, then B's earlier-dated Y on P wins. Returns (a, b)."""

    class Store(_Skewed):
        pass

    Store.fired, Store.crash = False, crash
    a, b = machine(env, "a"), machine(env, "b", store_cls=Store)
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/app.conf", "v1\n")
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})

    def a_publishes() -> None:
        a.use(monkeypatch)
        a.write("cfg/app.conf", "from a\n")
        a.write("cfg/a_new.txt", "a new\n")
        a.engine.run({"c": a.model("cfg")})
        b.use(monkeypatch)

    Store.before = staticmethod(a_publishes)
    b.write("cfg/app.conf" if same_path else "cfg/b_new.txt", "from b\n")
    if crash:
        with pytest.raises(RuntimeError, match="crashed"):
            b.engine.run({"c": b.model("cfg")})
    else:
        b.engine.run({"c": b.model("cfg")})
    a.use(monkeypatch)
    return a, b


def _assert_a_edits_survive(a: Machine, b: Machine, monkeypatch) -> None:
    a.use(monkeypatch)
    a.engine.run({"c": a.model("cfg")})
    assert (a.home / "cfg" / "app.conf").read_text() == "from a\n"
    assert (a.home / "cfg" / "a_new.txt").read_text() == "a new\n"
    assert (a.home / "cfg" / "b_new.txt").read_text() == "from b\n"
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    assert (b.home / "cfg" / "app.conf").read_text() == "from a\n"
    assert (b.home / "cfg" / "a_new.txt").read_text() == "a new\n"
    assert (b.home / "cfg" / "b_new.txt").read_text() == "from b\n"


def test_winner_flip_after_publisher_finished_loses_nothing(env, monkeypatch):
    a, b = _flip(env, monkeypatch, same_path=False)
    _assert_a_edits_survive(a, b, monkeypatch)


def test_winner_flip_same_path_is_a_conflict_with_zero_writes(env, monkeypatch):
    a, _b = _flip(env, monkeypatch, same_path=True)
    with pytest.raises(SyncConflictError) as exc:
        a.engine.run({"c": a.model("cfg")})
    assert exc.value.conflicts == {"c": ["home/user/cfg/app.conf"]}
    assert (a.home / "cfg" / "app.conf").read_text() == "from a\n"
    assert (a.home / "cfg" / "a_new.txt").read_text() == "a new\n"
    assert not (a.home / "cfg" / "b_new.txt").exists()
    assert a.engine.resolve({"c": a.model("cfg")}, "local", None).published  # resolve still works from a fork


def test_orphan_with_an_earlier_time_wins_without_loss(env, monkeypatch):
    a, b = _flip(env, monkeypatch, same_path=False, crash=True)
    _assert_a_edits_survive(a, b, monkeypatch)


def test_recorded_head_absent_from_the_listing_loses_nothing(pair, monkeypatch):
    a, b = pair
    a.write("cfg/app.conf", "v1\n")
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    b.write("cfg/b_new.txt", "from b\n")
    a.use(monkeypatch)
    a.write("cfg/app.conf", "from a\n")
    a.write("cfg/a_new.txt", "a new\n")
    a.engine.run({"c": a.model("cfg")})
    a.engine.store.forget_ids([a.head()])  # the head A recorded is gone (pruned, forgotten)
    _assert_a_edits_survive_after_forget(a, b, monkeypatch)


def _assert_a_edits_survive_after_forget(a: Machine, b: Machine, monkeypatch) -> None:
    a.engine.run({"c": a.model("cfg")})
    assert (a.home / "cfg" / "app.conf").read_text() == "from a\n"
    assert (a.home / "cfg" / "a_new.txt").read_text() == "a new\n"
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    assert (b.home / "cfg" / "app.conf").read_text() == "from a\n"
    assert (b.home / "cfg" / "a_new.txt").read_text() == "a new\n"


def test_only_the_losing_cfg_snapshot_is_forgotten(env, monkeypatch):
    created: dict[str, list[str]] = {}

    class Racing(ResticStore):
        def backup(self, write, cfg, when, description, extra_tags: Sequence[str] = ()):
            if not created:
                a.use(monkeypatch)
                a.write("cfg/from_a.txt", "a\n")
                a.engine.run({"c": a.model("cfg")})
                b.use(monkeypatch)
                when = when + timedelta(hours=1)  # B's c loses
            snap = super().backup(write, cfg, when, description, extra_tags)
            created.setdefault(cfg, []).append(snap[0].id)
            return snap

    a, b = machine(env, "a"), machine(env, "b", store_cls=Racing)
    a.use(monkeypatch)
    a.engine.init()
    b.use(monkeypatch)
    b.write("cfg/from_b.txt", "b\n")
    b.write("cfg2/x.txt", "x\n")
    models = {"c": b.model("cfg"), "d": b.model("cfg2")}
    b.engine.run(models)
    alive = {s.id for s in b.engine.store.snapshots(tag=SYNC_TAG)}
    assert created["c"][0] not in alive  # the loser
    assert created["d"][0] in alive  # d's first snapshot won: another machine may already build on it
    assert len(created["d"]) == 1
