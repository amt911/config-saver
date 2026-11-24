"""SyncEngine end to end: two fake machines (own HOME, state, sync dir) and one bare remote.

Real restic and age are used on purpose (the repository is a local path in tmp_path): the
engine's job is the interplay of those with the real filesystem, and mocking them proves nothing.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from config_saver.lib.errors import (
    ArchiveError,
    StoreError,
    SyncConflictError,
    SyncStateError,
    SyncTransportError,
    UnsafeArchiveError,
)
from config_saver.lib.restic_store import (
    GEN_TAG_PREFIX,
    HOST_TAG_PREFIX,
    PARENT_TAG_PREFIX,
    SYNC_TAG,
    ResticStore,
    Snapshot,
)
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
        self,
        root: Path,
        name: str,
        user: str,
        key: str,
        repo: str,
        store_cls: type[ResticStore] | None = None,
        now: Callable[[], float] | None = None,
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
        extra = {"now": now} if now else {}
        self.engine = SyncEngine(
            remote, key, self.state_file, self.sync_dir, self.lock_file, name, store=store, **extra
        )

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
        return select_head([_ref(x) for x in snaps]) or ""


def _ref(snap) -> SnapRef:
    gen = next(int(t[len(GEN_TAG_PREFIX) :]) for t in snap.tags if t.startswith(GEN_TAG_PREFIX))
    return SnapRef(snap.canonical, _parent(snap), snap.time, gen)


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


def machine(
    env,
    name: str,
    user: str = "tester",
    store_cls: type[ResticStore] | None = None,
    now: Callable[[], float] | None = None,
) -> Machine:
    return Machine(env["root"], name, user, env["key"], env["repo"], store_cls, now)


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


def _without_cache(state_file: str) -> dict:
    doc = json.loads(Path(state_file).read_text())
    doc.pop("hash_cache", None)
    return doc


def test_conflict_aborts_without_writes(pair, monkeypatch):
    a, b = _synced_pair(pair, monkeypatch)
    a.use(monkeypatch)
    a.write("cfg/app.conf", "from a\n")
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    b.write("cfg/app.conf", "from b\n")
    before_state = _without_cache(b.state_file)

    with pytest.raises(SyncConflictError) as exc:
        b.engine.run({"c": b.model("cfg")})
    assert exc.value.conflicts == {"c": ["home/user/cfg/app.conf"]}
    assert (b.home / "cfg" / "app.conf").read_text() == "from b\n"
    assert _without_cache(b.state_file) == before_state  # only the hash cache may change on a conflict
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


def _craft_snapshot(
    m: Machine, cfg: str, members: dict[str, bytes], manifest: Manifest, parent: str = "none", gen: int = 0
):
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
    tags = [SYNC_TAG, PARENT_TAG_PREFIX + parent, f"{GEN_TAG_PREFIX}{gen}", HOST_TAG_PREFIX + "evil"]
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
    assert losers([_ref(x) for x in snaps]) == set()


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
                Snapshot("id1", "id1", when, "c", None, (SYNC_TAG, PARENT_TAG_PREFIX + "id2", "gen:1")),
                Snapshot("id2", "id2", when, "c", None, (SYNC_TAG, PARENT_TAG_PREFIX + "id1", "gen:1")),
            ]

    a.engine.store = Cyclic.for_repository(a.engine.store.repo, a.engine.store._password or "", ())
    a.write("cfg/app.conf", "x\n")
    with pytest.raises(SyncStateError):
        a.engine.run({"c": a.model("cfg")})


@pytest.mark.parametrize("tags", [(SYNC_TAG, "parent:none"), (SYNC_TAG, "parent:none", "gen:x"), (SYNC_TAG, "gen:-1")])
def test_a_sync_snapshot_without_a_valid_gen_tag_is_a_state_error(pair, tags):
    a, _b = pair

    class Untagged(ResticStore):
        def snapshots(self, cfg=None, tag=None):
            return [Snapshot("id1", "id1", datetime(2026, 1, 1), "c", None, tags)]

    a.engine.store = Untagged.for_repository(a.engine.store.repo, a.engine.store._password or "", ())
    a.write("cfg/app.conf", "x\n")
    with pytest.raises(SyncStateError, match="gen:"):
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
    _craft_snapshot(
        a, "c", {"home/user/cfg/new.txt": new, "home/user/../../evil": b"x"}, manifest, parent=good_head, gen=2
    )
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


def _flip(env, monkeypatch, *, same_path: bool, crash: bool = False, clock: Callable[[], float] | None = None):
    """Both machines synced on P. A publishes X, then B's earlier-dated Y on P wins. Returns (a, b)."""

    class Store(_Skewed):
        pass

    Store.fired, Store.crash = False, crash
    a, b = machine(env, "a", now=clock), machine(env, "b", store_cls=Store, now=clock)
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/app.conf", "v1\n")
    a.write("cfg/keep.txt", "keep\n")  # X carries it, so P stays referenced and survives A's housekeeping
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
    a.write("cfg/keep.txt", "keep\n")  # keeps P referenced
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


# ------------------------------------------------------------------ pruning

DAY = 86400.0
SUPERSEDE_GRACE_S = 24 * 3600
T0 = 1_000_000.0


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def late() -> Clock:
    """A clock two days ahead of the snapshots the real clock is about to stamp: grace has passed."""
    return Clock(time.time() + 2 * DAY)


def _ids(m: Machine) -> set[str]:
    return {s.id for s in m.engine.store.snapshots(tag=SYNC_TAG)}


def _referenced(m: Machine, cfg: str = "c") -> set[str]:
    """Every snapshot id the head manifest names (self resolved), plus the head."""
    engine = m.engine
    head = m.head()
    with engine._workspace():
        manifest = engine._read_manifest(head, cfg)
    return {head} | {snap for _, snap in manifest.entries.values()}


def test_unreferenced_snapshots_are_forgotten_and_carried_files_stay_restorable(env, monkeypatch):
    a, b, c = machine(env, "a", now=late()), machine(env, "b", now=late()), machine(env, "c")
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f1.txt", "one\n")
    a.engine.run({"c": a.model("cfg")})
    first = a.head()
    a.write("cfg/f1.txt", "one v2\n")
    a.engine.run({"c": a.model("cfg")})
    a.write("cfg/f2.txt", "two\n")
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    b.write("cfg/f3.txt", "three\n")
    b.engine.run({"c": b.model("cfg")})

    alive = _ids(b)
    assert first not in alive  # nothing references it any more
    assert _referenced(b) <= alive  # and nothing a head names was forgotten
    assert len(alive) < 4
    c.use(monkeypatch)
    c.engine.run({"c": c.model("cfg")})
    assert (c.home / "cfg" / "f1.txt").read_text() == "one v2\n"  # carried from an older snapshot
    assert (c.home / "cfg" / "f2.txt").read_text() == "two\n"
    assert (c.home / "cfg" / "f3.txt").read_text() == "three\n"


def test_cfgs_this_machine_does_not_sync_are_never_forgotten(env, monkeypatch):
    a, b = machine(env, "a"), machine(env, "b")
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg2/x.txt", "x1\n")
    a.engine.run({"d": a.model("cfg2")})
    a.write("cfg2/x.txt", "x2\n")
    a.engine.run({"d": a.model("cfg2")})
    d_before = {s.id for s in a.engine.store.snapshots("d", tag=SYNC_TAG)}
    b.use(monkeypatch)
    b.write("cfg/y.txt", "y\n")
    b.engine.run({"c": b.model("cfg")})
    assert {s.id for s in b.engine.store.snapshots("d", tag=SYNC_TAG)} == d_before


class _Counting(ResticStore):
    prunes = 0
    forgets: list[list[str]] = []
    fail_forget = False
    fail_prune = False

    def forget_ids(self, ids):
        type(self).forgets.append(list(ids))
        if type(self).fail_forget and ids:
            raise StoreError("restic forget failed")
        super().forget_ids(ids)

    def prune(self):
        type(self).prunes += 1
        if type(self).fail_prune:
            raise StoreError("restic prune failed")
        super().prune()


@pytest.fixture
def counting():
    _Counting.prunes, _Counting.forgets = 0, []
    _Counting.fail_forget = _Counting.fail_prune = False
    return _Counting


def test_prune_runs_at_most_once_a_week(env, monkeypatch, counting):
    clock = Clock()
    a = machine(env, "a", store_cls=counting, now=clock)
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f.txt", "1\n")
    a.engine.run({"c": a.model("cfg")})
    assert counting.prunes == 1  # never pruned before
    assert state_io.load(a.state_file).last_prune == T0
    clock.t = T0 + 6 * DAY
    a.write("cfg/f.txt", "2\n")
    a.engine.run({"c": a.model("cfg")})
    assert counting.prunes == 1
    assert state_io.load(a.state_file).last_prune == T0
    clock.t = T0 + 7 * DAY + 1
    a.engine.run({"c": a.model("cfg")})
    assert counting.prunes == 2
    assert state_io.load(a.state_file).last_prune == T0 + 7 * DAY + 1


def test_explicit_prune_forgets_and_prunes_regardless_of_the_clock(env, monkeypatch, counting):
    clock = late()
    start = clock.t
    a = machine(env, "a", store_cls=counting, now=clock)
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f.txt", "1\n")
    a.engine.run({"c": a.model("cfg")})
    a.write("cfg/f.txt", "2\n")
    a.engine.run({"c": a.model("cfg")})  # the weekly prune already ran here, and forgot the first snapshot
    # Plant a second root: it loses to the older chain, so it is a loser prune() must find.
    extra = a.engine.store.backup(
        _empty_tar, "c", datetime.now(), None, extra_tags=[SYNC_TAG, f"{PARENT_TAG_PREFIX}none", f"{GEN_TAG_PREFIX}0"]
    )[0].id
    before = counting.prunes
    clock.t = start + 60  # a minute later: well inside the week, so only an explicit prune() runs it
    forgotten = a.engine.prune({"c": a.model("cfg")})
    assert extra in forgotten
    assert extra not in _ids(a)
    assert counting.prunes == before + 1
    assert state_io.load(a.state_file).last_prune == start + 60


def _empty_tar(out):
    from config_saver.lib.tar_compressor.tar_compressor import CompressResult

    with tarfile.open(fileobj=out, mode="w|") as tar:
        info = tarfile.TarInfo(MANIFEST_MEMBER)
        data = Manifest("c", [], {}).to_json()
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return CompressResult("")


def test_forget_failure_after_publish_keeps_the_publish_and_the_state(env, monkeypatch, counting):
    a = machine(env, "a", store_cls=counting, now=late())
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f.txt", "1\n")
    a.engine.run({"c": a.model("cfg")})
    a.write("cfg/f.txt", "2\n")
    counting.fail_forget = True
    with pytest.raises(SyncTransportError, match="forget"):
        a.engine.run({"c": a.model("cfg")})
    assert state_io.load(a.state_file).heads["c"] == a.head()  # the publish was not lost
    counting.fail_forget = False
    a.engine.run({"c": a.model("cfg")})  # nothing to republish: housekeeping catches up
    assert (a.home / "cfg" / "f.txt").read_text() == "2\n"


def test_prune_failure_after_publish_keeps_state_and_retries_next_run(env, monkeypatch, counting):
    a = machine(env, "a", store_cls=counting, now=Clock())
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f.txt", "1\n")
    counting.fail_prune = True
    with pytest.raises(SyncTransportError, match="prune"):
        a.engine.run({"c": a.model("cfg")})
    saved = state_io.load(a.state_file)
    assert saved.heads["c"] == a.head()
    assert saved.last_prune is None  # not recorded: the next run tries again
    counting.fail_prune = False
    a.engine.run({"c": a.model("cfg")})  # inside the back-off window: no attempt, nothing recorded
    assert state_io.load(a.state_file).last_prune is None
    a.engine.now = Clock(T0 + DAY)  # a day later it tries again
    a.engine.run({"c": a.model("cfg")})
    assert state_io.load(a.state_file).last_prune == T0 + DAY


def test_a_publish_built_on_a_snapshot_forgotten_meanwhile_retries(env, monkeypatch):
    """B's manifest would carry a file from P; A forgets P while B uploads. B must not publish that."""
    fired: list[int] = []

    class Slow(ResticStore):
        def backup(self, write, cfg, when, description, extra_tags: Sequence[str] = ()):
            if not fired:
                fired.append(1)
                a.use(monkeypatch)
                a.write("cfg/app.conf", "from a\n")  # P's only file is replaced: P is unreferenced
                a.engine.run({"c": a.model("cfg")})
                b.use(monkeypatch)
            return super().backup(write, cfg, when, description, extra_tags)

    a, b, c = machine(env, "a", now=late()), machine(env, "b", store_cls=Slow), machine(env, "c")
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/app.conf", "v1\n")
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    b.write("cfg/b_new.txt", "b\n")
    b.engine.run({"c": b.model("cfg")})
    assert _referenced(b) <= _ids(b)  # the head names nothing that is gone
    c.use(monkeypatch)
    c.engine.run({"c": c.model("cfg")})
    assert (c.home / "cfg" / "app.conf").read_text() == "from a\n"
    assert (c.home / "cfg" / "b_new.txt").read_text() == "b\n"


def test_successful_run_persists_the_hash_cache_and_reuses_it(pair, monkeypatch):
    import config_saver.lib.sync.scan as scan_mod

    a, _b = pair
    a.write("cfg/app.conf", "v1\n")
    a.engine.run({"c": a.model("cfg")})
    saved = state_io.load(a.state_file)
    key = str(a.home / "cfg" / "app.conf")
    assert set(saved.hash_cache) == {key}
    assert saved.hash_cache_home == str(a.home)

    calls: list[str] = []
    real = scan_mod._fingerprint
    monkeypatch.setattr(scan_mod, "_fingerprint", lambda c, p, pr: (calls.append(p), real(c, p, pr))[1])
    a.engine.run({"c": a.model("cfg")})
    assert calls == []  # unchanged file: served from the persisted cache


def test_cache_recorded_for_another_home_is_discarded(pair, monkeypatch):
    import config_saver.lib.sync.scan as scan_mod

    a, _b = pair
    a.write("cfg/app.conf", "v1\n")
    a.engine.run({"c": a.model("cfg")})
    st = state_io.load(a.state_file)
    st.hash_cache_home = "/somewhere/else"
    state_io.save(a.state_file, st)

    calls: list[str] = []
    real = scan_mod._fingerprint
    monkeypatch.setattr(scan_mod, "_fingerprint", lambda c, p, pr: (calls.append(p), real(c, p, pr))[1])
    a.engine.run({"c": a.model("cfg")})
    assert calls == [str(a.home / "cfg" / "app.conf")]
    assert state_io.load(a.state_file).hash_cache_home == str(a.home)


# ----------------------------------------------- a superseded snapshot waits out the grace period


def test_superseded_snapshot_is_kept_until_its_successor_is_a_day_old(env, monkeypatch):
    clock = Clock(time.time())
    a = machine(env, "a", now=clock)
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f.txt", "1\n")
    a.engine.run({"c": a.model("cfg")})
    first = a.head()
    a.write("cfg/f.txt", "2\n")
    a.engine.run({"c": a.model("cfg")})
    assert first in _ids(a)  # unreferenced, but its successor is minutes old
    clock.t += SUPERSEDE_GRACE_S - 3600
    a.write("cfg/f.txt", "3\n")
    a.engine.run({"c": a.model("cfg")})
    assert first in _ids(a)  # the successor is 23 hours old
    clock.t += 2 * 3600
    a.write("cfg/f.txt", "4\n")
    a.engine.run({"c": a.model("cfg")})
    assert first not in _ids(a)
    assert _referenced(a) <= _ids(a)


def test_explicit_prune_honours_the_grace_period_too(env, monkeypatch):
    clock = Clock(time.time())
    a = machine(env, "a", now=clock)
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f.txt", "1\n")
    a.engine.run({"c": a.model("cfg")})
    first = a.head()
    a.write("cfg/f.txt", "2\n")
    a.engine.run({"c": a.model("cfg")})
    assert first not in a.engine.prune({"c": a.model("cfg")})
    assert first in _ids(a)
    clock.t += 25 * 3600
    assert first in a.engine.prune({"c": a.model("cfg")})
    assert first not in _ids(a)


class _Deferring(ResticStore):
    """A's store: its forgets are only recorded, so the test can run them at the worst moment."""

    deferred: list[str] = []

    def forget_ids(self, ids):
        type(self).deferred += list(ids)


def test_a_slow_machine_publishing_on_a_superseded_snapshot_loses_nothing(env, monkeypatch):
    """A supersedes P and decides what to forget; slower B (analysed before) then publishes Y on P.

    Y is dated earlier, so it wins and carries P's file: forgetting P at that point would leave every
    machine unable to restore it. The grace period keeps P out of what A decided to forget.
    """

    class Store(_Skewed):
        pass

    Store.fired, Store.crash = False, False
    _Deferring.deferred = []
    a = machine(env, "a", store_cls=_Deferring)
    b = machine(env, "b", store_cls=Store)
    c = machine(env, "c")
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/app.conf", "v1\n")
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})

    def a_supersedes() -> None:
        a.use(monkeypatch)
        a.write("cfg/app.conf", "from a\n")  # X replaces every file P had: P is unreferenced
        a.engine.run({"c": a.model("cfg")})
        b.use(monkeypatch)

    Store.before = staticmethod(a_supersedes)
    b.write("cfg/b_new.txt", "from b\n")
    b.engine.run({"c": b.model("cfg")})  # Y on P, dated earlier: wins, carries app.conf from P
    ResticStore.forget_ids(a.engine.store, _Deferring.deferred)  # A's decision lands only now
    assert _referenced(b) <= _ids(b)
    c.use(monkeypatch)
    c.engine.run({"c": c.model("cfg")})
    assert (c.home / "cfg" / "b_new.txt").read_text() == "from b\n"
    assert (c.home / "cfg" / "app.conf").exists()


def test_recorded_head_and_its_parent_both_forgotten_never_overwrites(env, monkeypatch):
    a, b = machine(env, "a", now=late()), machine(env, "b", now=late())
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/app.conf", "v1\n")
    a.engine.run({"c": a.model("cfg")})
    a.write("cfg/app.conf", "v2\n")
    a.engine.run({"c": a.model("cfg")})
    x = a.head()
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    b.write("cfg/app.conf", "from b\n")
    b.engine.run({"c": b.model("cfg")})  # Y on X, replacing everything: X and its parent are unreferenced
    a.use(monkeypatch)
    ids = _ids(a)
    a.engine.store.forget_ids([i for i in ids if i != b.head()])  # the recorded head and its parent are gone
    assert x not in _ids(a)
    a.write("cfg/app.conf", "from a\n")
    a.write("cfg/a_only.txt", "a only\n")
    with pytest.raises(SyncConflictError):
        a.engine.run({"c": a.model("cfg")})
    assert (a.home / "cfg" / "app.conf").read_text() == "from a\n"  # nothing was overwritten
    assert (a.home / "cfg" / "a_only.txt").read_text() == "a only\n"


def test_a_last_prune_in_the_future_counts_as_due(env, monkeypatch, counting):
    clock = Clock()
    a = machine(env, "a", store_cls=counting, now=clock)
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f.txt", "1\n")
    a.engine.run({"c": a.model("cfg")})
    state = state_io.load(a.state_file)
    state.last_prune = T0 + 30 * DAY  # the clock went backwards since
    state_io.save(a.state_file, state)
    before = counting.prunes
    a.write("cfg/f.txt", "2\n")
    a.engine.run({"c": a.model("cfg")})
    assert counting.prunes == before + 1
    assert state_io.load(a.state_file).last_prune == T0


# ------------------------------------------- canonical ids: snapshots carried by `restic copy`


def _copy_all(env, src: Machine) -> str:
    """Copy every sync snapshot of the shared repository to a second one; return its path."""
    dest = str(env["root"] / "repo2")
    store = ResticStore.for_repository(dest, derive_password(env["key"]), ())
    store.init_plain()
    ids = [s.id for s in src.engine.store.snapshots(tag=SYNC_TAG)]
    store.copy_from(src.engine.store, ids)
    return dest


def _carried(env, old: Machine, name: str, monkeypatch) -> Machine:
    """A machine on the copied repository holding the old machine's files and recorded state."""
    new = Machine(env["root"], name, "tester", env["key"], _copy_all(env, old))
    shutil.copytree(old.home, new.home, symlinks=True, dirs_exist_ok=True)
    shutil.copy(old.state_file, new.state_file)
    new.use(monkeypatch)
    return new


def test_every_snapshot_of_a_copy_has_a_new_id_but_keeps_its_canonical_one(env, monkeypatch):
    a = machine(env, "a")
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f.txt", "one\n")
    a.engine.run({"c": a.model("cfg")})
    c = _carried(env, a, "c", monkeypatch)
    old, new = a.engine.store.snapshots(tag=SYNC_TAG)[0], c.engine.store.snapshots(tag=SYNC_TAG)[0]
    assert new.id != old.id and new.original == old.id
    assert new.canonical == old.id and old.canonical == old.id


def test_recorded_head_stays_valid_after_a_copy(env, monkeypatch):
    a = machine(env, "a")
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f.txt", "one\n")
    a.engine.run({"c": a.model("cfg")})
    a.write("cfg/f.txt", "two\n")
    a.engine.run({"c": a.model("cfg")})
    c = _carried(env, a, "c", monkeypatch)
    before = [s.id for s in c.engine.store.snapshots(tag=SYNC_TAG)]
    report = c.engine.run({"c": c.model("cfg")})
    assert not (report.applied or report.deleted or report.published or report.conflicts)
    assert [s.id for s in c.engine.store.snapshots(tag=SYNC_TAG)] == before  # nothing was re-published


def test_carried_files_restore_from_copied_snapshots(env, monkeypatch):
    a = machine(env, "a", now=late())
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f1.txt", "one\n")
    a.engine.run({"c": a.model("cfg")})
    a.write("cfg/f2.txt", "two\n")
    a.engine.run({"c": a.model("cfg")})
    c = _carried(env, a, "c", monkeypatch)
    fresh = Machine(env["root"], "fresh", "tester", env["key"], c.engine.remote.repository)
    fresh.use(monkeypatch)
    fresh.engine.run({"c": fresh.model("cfg")})
    assert (fresh.home / "cfg" / "f1.txt").read_text() == "one\n"  # held by the older snapshot
    assert (fresh.home / "cfg" / "f2.txt").read_text() == "two\n"


def test_a_new_publish_on_a_copy_is_parented_on_the_canonical_head(env, monkeypatch):
    a = machine(env, "a")
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f.txt", "one\n")
    a.engine.run({"c": a.model("cfg")})
    first = a.head()
    c = _carried(env, a, "c", monkeypatch)
    c.write("cfg/f.txt", "two\n")
    c.engine.run({"c": c.model("cfg")})
    newest = c.engine.store.snapshots(tag=SYNC_TAG)[-1]
    assert f"{PARENT_TAG_PREFIX}{first}" in newest.tags
    assert c.head() == newest.id


def test_forget_works_on_copied_snapshots(env, monkeypatch):
    a = machine(env, "a")
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f1.txt", "one\n")
    a.engine.run({"c": a.model("cfg")})
    a.write("cfg/f1.txt", "one v2\n")
    a.engine.run({"c": a.model("cfg")})
    a.write("cfg/f2.txt", "two\n")
    a.engine.run({"c": a.model("cfg")})
    c = _carried(env, a, "c", monkeypatch)
    c.engine.now = late()
    forgotten = c.engine.prune({"c": c.model("cfg")})
    assert forgotten and all(f not in {s.canonical for s in c.engine.store.snapshots(tag=SYNC_TAG)} for f in forgotten)
    fresh = Machine(env["root"], "fresh", "tester", env["key"], c.engine.remote.repository)
    fresh.use(monkeypatch)
    fresh.engine.run({"c": fresh.model("cfg")})
    assert (fresh.home / "cfg" / "f1.txt").read_text() == "one v2\n"
    assert (fresh.home / "cfg" / "f2.txt").read_text() == "two\n"


def test_winner_flip_recovery_on_a_copy(env, monkeypatch):
    a, b = _flip(env, monkeypatch, same_path=False)
    a2 = _carried(env, a, "a2", monkeypatch)
    a2.engine.run({"c": a2.model("cfg")})
    assert (a2.home / "cfg" / "app.conf").read_text() == "from a\n"
    assert (a2.home / "cfg" / "a_new.txt").read_text() == "a new\n"
    assert (a2.home / "cfg" / "b_new.txt").read_text() == "from b\n"


# ------------------------------------------------------------------------------------ move


def _target(env, name: str = "repo2") -> SyncRemote:
    return SyncRemote(str(env["root"] / name))


def _published(env, monkeypatch, name: str = "a", steps: int = 2) -> Machine:
    m = machine(env, name)
    m.use(monkeypatch)
    m.engine.init()
    for i in range(steps):
        m.write("cfg/f.txt", f"v{i}\n")
        m.write(f"cfg/extra{i}.txt", "x\n")
        m.engine.run({"c": m.model("cfg")})
    return m


def _on(env, name: str, repo: SyncRemote, monkeypatch, like: Machine | None = None) -> Machine:
    m = Machine(env["root"], name, "tester", env["key"], repo.repository)
    if like is not None:
        shutil.copytree(like.home, m.home, symlinks=True, dirs_exist_ok=True)
        shutil.copy(like.state_file, m.state_file)
    m.use(monkeypatch)
    return m


def test_move_copies_everything_and_the_other_machine_keeps_syncing(env, monkeypatch):
    a = _published(env, monkeypatch)
    b = machine(env, "b")
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    target = _target(env)
    copied = a.engine.move(target)
    assert copied == len(a.engine.store.snapshots(tag=SYNC_TAG))
    new_store = target.store(env["key"])
    assert {s.canonical for s in new_store.snapshots(tag=SYNC_TAG)} == {
        s.canonical for s in a.engine.store.snapshots(tag=SYNC_TAG)
    }
    # B moves too (its recorded head is still valid), then edits and A, on the new repository, receives it
    b2 = _on(env, "b2", target, monkeypatch, like=b)
    b2.write("cfg/f.txt", "from b\n")
    assert b2.engine.run({"c": b2.model("cfg")}).published["c"]
    a2 = _on(env, "a2", target, monkeypatch, like=a)
    a2.engine.run({"c": a2.model("cfg")})
    assert (a2.home / "cfg" / "f.txt").read_text() == "from b\n"


def test_move_into_an_existing_repository_skips_what_was_already_copied(env, monkeypatch):
    a = _published(env, monkeypatch)
    target = _target(env)
    first = a.engine.move(target)
    again = a.engine.move(target)  # second machine's move after the first one's
    assert again == first
    assert len(target.store(env["key"]).snapshots(tag=SYNC_TAG)) == first


def test_move_on_a_second_machine_after_the_first_moved_copies_what_it_published_meanwhile(env, monkeypatch):
    a = _published(env, monkeypatch)
    b = machine(env, "b")
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    target = _target(env)
    a.use(monkeypatch)
    a.engine.move(target)
    b.use(monkeypatch)
    b.write("cfg/b_only.txt", "meanwhile\n")
    b.engine.run({"c": b.model("cfg")})  # published to the OLD repository
    b.engine.move(target)
    c = _on(env, "c", target, monkeypatch)
    c.engine.run({"c": c.model("cfg")})
    assert (c.home / "cfg" / "b_only.txt").read_text() == "meanwhile\n"
    assert (c.home / "cfg" / "f.txt").read_text() == "v1\n"


def test_move_keeps_the_chunker_parameters_of_the_source(env, monkeypatch):
    a = _published(env, monkeypatch)
    target = _target(env)
    a.engine.move(target)

    def poly(store: ResticStore) -> str:
        out = subprocess.run(["restic", "cat", "config"], env=store._env(), capture_output=True, text=True, check=True)
        return str(json.loads(out.stdout)["chunker_polynomial"])

    assert poly(target.store(env["key"])) == poly(a.engine.store)


def test_move_failure_in_the_copy_is_a_transport_error(env, monkeypatch):
    a = _published(env, monkeypatch)

    def boom(self, src, ids):
        raise StoreError("restic failed: network down")

    monkeypatch.setattr(ResticStore, "copy_from", boom)
    with pytest.raises(SyncTransportError, match="network down"):
        a.engine.move(_target(env))


def test_move_that_fails_verification_raises_and_names_the_mismatch(env, monkeypatch):
    a = _published(env, monkeypatch)
    real = ResticStore.copy_from

    def lossy(self, src, ids):
        real(self, src, ids[:-1])  # the newest snapshot never arrives

    monkeypatch.setattr(ResticStore, "copy_from", lossy)
    with pytest.raises(SyncTransportError, match="verif"):
        a.engine.move(_target(env))


def test_move_with_no_snapshots_just_creates_the_target(env, monkeypatch):
    a = machine(env, "a")
    a.use(monkeypatch)
    a.engine.init()
    target = _target(env)
    assert a.engine.move(target) == 0
    assert target.store(env["key"]).exists()


# ------------------------------ an idle machine, and a lost fork that must outlive its machine's absence

LOSER_RETENTION_S = 30 * DAY


def _idle_scenario(env, monkeypatch):
    """A syncs, then rewrites f1/f2 (R); B, two days later by the clocks, publishes four full rewrites on top.

    f3 stays in A's first snapshot P, so P survives while R and B's intermediate snapshots are forgotten.
    """
    a, b = machine(env, "a", now=late()), machine(env, "b", now=late())
    a.use(monkeypatch)
    a.engine.init()
    for name in ("f1", "f2", "f3"):
        a.write(f"cfg/{name}.txt", f"{name} v1\n")
    a.engine.run({"c": a.model("cfg")})
    a.write("cfg/f1.txt", "f1 v1b\n")
    a.write("cfg/f2.txt", "f2 v1b\n")
    a.engine.run({"c": a.model("cfg")})
    first = a.head()
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    for i in range(2, 6):
        b.write("cfg/f1.txt", f"f1 v{i}\n")
        b.write("cfg/f2.txt", f"f2 v{i}\n")
        b.engine.run({"c": b.model("cfg")})
    assert first not in _ids(b)  # superseded and forgotten after its grace: the normal life of a snapshot
    a.use(monkeypatch)
    return a, b, first


def test_an_idle_machine_picks_up_a_remote_that_moved_on_without_a_conflict(env, monkeypatch):
    a, _b, _first = _idle_scenario(env, monkeypatch)
    before = _ids(a)
    report = a.engine.run({"c": a.model("cfg")})
    assert (a.home / "cfg" / "f1.txt").read_text() == "f1 v5\n"
    assert (a.home / "cfg" / "f2.txt").read_text() == "f2 v5\n"
    assert (a.home / "cfg" / "f3.txt").read_text() == "f3 v1\n"
    assert sorted(report.applied["c"]) == ["home/user/cfg/f1.txt", "home/user/cfg/f2.txt"]
    assert report.published == {} and report.conflicts == {} and report.deleted == {}
    assert _ids(a) - before == set()  # nothing was written to the repository


def test_an_idle_machine_with_a_local_edit_publishes_it_and_takes_the_remote_changes(env, monkeypatch):
    a, b, _first = _idle_scenario(env, monkeypatch)
    a.write("cfg/f3.txt", "f3 from a\n")
    report = a.engine.run({"c": a.model("cfg")})
    assert report.published == {"c": ["home/user/cfg/f3.txt"]}
    assert (a.home / "cfg" / "f1.txt").read_text() == "f1 v5\n"
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    assert (b.home / "cfg" / "f3.txt").read_text() == "f3 from a\n"
    assert (b.home / "cfg" / "f1.txt").read_text() == "f1 v5\n"


def test_a_deletion_made_while_idle_is_published_not_undone(env, monkeypatch):
    a, b, _first = _idle_scenario(env, monkeypatch)
    (a.home / "cfg" / "f3.txt").unlink()
    a.engine.run({"c": a.model("cfg")})
    assert not (a.home / "cfg" / "f3.txt").exists()
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    assert not (b.home / "cfg" / "f3.txt").exists()


def _age_the_record(m: Machine, seconds: float) -> None:
    st = state_io.load(m.state_file)
    st.recorded_at = {k: v - seconds for k, v in st.recorded_at.items()}
    state_io.save(m.state_file, st)


def test_a_record_older_than_29_days_with_the_head_gone_conflicts_and_loses_nothing(env, monkeypatch):
    a, _b, _first = _idle_scenario(env, monkeypatch)
    _age_the_record(a, 29 * DAY + 60)
    with pytest.raises(SyncConflictError):
        a.engine.run({"c": a.model("cfg")})
    assert (a.home / "cfg" / "f1.txt").read_text() == "f1 v1b\n"  # zero writes


def test_a_record_just_under_29_days_old_still_counts_as_superseded(env, monkeypatch):
    a, _b, _first = _idle_scenario(env, monkeypatch)
    _age_the_record(a, 29 * DAY - 3600)
    a.engine.run({"c": a.model("cfg")})
    assert (a.home / "cfg" / "f1.txt").read_text() == "f1 v5\n"


def test_a_state_file_without_recorded_at_takes_the_cautious_path(env, monkeypatch):
    a, _b, _first = _idle_scenario(env, monkeypatch)
    st = state_io.load(a.state_file)
    st.recorded_at = {}
    state_io.save(a.state_file, st)
    with pytest.raises(SyncConflictError):
        a.engine.run({"c": a.model("cfg")})


def test_a_single_machine_never_conflicts_with_itself_as_the_chain_loses_its_middle(env, monkeypatch):
    """Carried files keep the first snapshot alive while the ones after it are forgotten: the head stays the head."""
    a = machine(env, "a", now=late())
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/keep.txt", "keep\n")
    a.write("cfg/f2.txt", "w1\n")
    a.engine.run({"c": a.model("cfg")})
    first = a.head()
    for i in range(2, 7):
        a.write("cfg/f2.txt", f"w{i}\n")
        a.engine.run({"c": a.model("cfg")})  # a conflict here would raise
    assert first in _ids(a) and len(_ids(a)) < 6  # the middle was forgotten, the carried-from snapshot stayed
    assert a.head() != first
    assert (a.home / "cfg" / "f2.txt").read_text() == "w6\n"


def test_a_lost_fork_and_its_parent_outlive_the_machine_that_made_it(env, monkeypatch):
    clock = Clock(time.time())
    a, b = _flip(env, monkeypatch, same_path=False, clock=clock)
    st = state_io.load(a.state_file)
    x, p = st.heads["c"], st.parents["c"]
    assert x in _ids(b) and p in _ids(b)  # the loser is not forgotten at once
    clock.t += 3 * DAY
    _assert_a_edits_survive(a, b, monkeypatch)


def test_a_lost_fork_is_forgotten_only_after_thirty_days(env, monkeypatch):
    clock = Clock(time.time())
    a, b = _flip(env, monkeypatch, same_path=False, clock=clock)
    x = state_io.load(a.state_file).heads["c"]
    clock.t += LOSER_RETENTION_S - DAY
    b.use(monkeypatch)
    b.write("cfg/b_more.txt", "1\n")
    b.engine.run({"c": b.model("cfg")})
    assert x in _ids(b)
    clock.t += 2 * DAY
    b.write("cfg/b_more.txt", "2\n")
    b.engine.run({"c": b.model("cfg")})
    assert x not in _ids(b)
    assert _referenced(b) <= _ids(b)


def test_explicit_prune_keeps_a_young_loser_and_forgets_an_old_one(env, monkeypatch):
    clock = Clock(time.time())
    a, b = _flip(env, monkeypatch, same_path=False, clock=clock)
    x = state_io.load(a.state_file).heads["c"]
    b.use(monkeypatch)
    assert x not in b.engine.prune({"c": b.model("cfg")})
    clock.t += LOSER_RETENTION_S + DAY
    assert x in b.engine.prune({"c": b.model("cfg")})
    assert x not in _ids(b)


# ------------------------------------------------ the manifest is streamed and cached, never the whole tar


class _Dumping(ResticStore):
    """Counts how often snapshot data is pulled from the repository, and how many bytes of the streams."""

    dumps = 0
    streamed = 0

    def dump(self, snapshot_id, cfg, out):
        type(self).dumps += 1
        super().dump(snapshot_id, cfg, out)

    def open_dump(self, snapshot_id, cfg):
        type(self).dumps += 1
        return super().open_dump(snapshot_id, cfg)


def _fresh_engine(m: Machine, env, store_cls: type[ResticStore]) -> SyncEngine:
    """A second process on the same machine: same state files, new engine object, new in-memory state."""
    store = store_cls.for_repository(env["repo"], derive_password(env["key"]), ())
    return SyncEngine(SyncRemote(env["repo"]), env["key"], m.state_file, m.sync_dir, m.lock_file, "x", store=store)


def test_a_second_run_with_an_unchanged_head_dumps_nothing(env, monkeypatch):
    class Store(_Dumping):
        pass

    Store.dumps = 0
    a, b = machine(env, "a", store_cls=Store), machine(env, "b", store_cls=Store)
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f.txt", "1\n")
    a.engine.run({"c": a.model("cfg")})
    Store.dumps = 0
    _fresh_engine(a, env, Store).run({"c": a.model("cfg")})
    assert Store.dumps == 0  # the publish seeded the cache; the listing is the only repository traffic
    b.use(monkeypatch)
    b.engine.run({"c": b.model("cfg")})
    assert Store.dumps >= 1  # B had never seen this head: it reads the manifest and restores the file
    Store.dumps = 0
    _fresh_engine(b, env, Store).run({"c": b.model("cfg")})
    assert Store.dumps == 0


def test_the_cache_lists_only_snapshots_that_still_exist(env, monkeypatch):
    a = machine(env, "a", now=late())
    a.use(monkeypatch)
    a.engine.init()
    for i in range(3):
        a.write("cfg/f.txt", f"{i}\n")
        a.engine.run({"c": a.model("cfg")})
    cached = json.loads(Path(a.engine.manifest_cache).read_text())["manifests"]
    assert set(cached) <= _ids(a)
    assert a.head() in cached
    assert (os.stat(a.engine.manifest_cache).st_mode & 0o777) == 0o600


def test_a_hostile_cache_file_is_ignored(env, monkeypatch):
    class Store(_Dumping):
        pass

    Store.dumps = 0
    a = machine(env, "a", store_cls=Store)
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f.txt", "1\n")
    a.engine.run({"c": a.model("cfg")})
    head = a.head()
    hostile = Manifest("c", ["x"], {"../../etc/passwd": (Fingerprint("a" * 64, 0o644, None), "@")}).to_json().decode()
    Path(a.engine.manifest_cache).write_text(json.dumps({"version": 1, "manifests": {head: hostile}}))
    Store.dumps = 0
    _fresh_engine(a, env, Store).run({"c": a.model("cfg")})
    assert Store.dumps >= 1  # the cache was not trusted: the manifest was read from the repository
    assert (a.home / "cfg" / "f.txt").read_text() == "1\n"


class _Counted:
    def __init__(self, stream):
        self.stream, self.n = stream, 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.n += len(data)
        return data

    def __getattr__(self, name):
        return getattr(self.stream, name)


def test_the_manifest_of_a_big_snapshot_is_read_without_pulling_the_whole_tar(env, monkeypatch):
    import contextlib
    import subprocess as sp

    seen: list[_Counted] = []
    procs: list[sp.Popen] = []

    class Recording(sp.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            procs.append(self)

    monkeypatch.setattr("config_saver.lib.restic_store.subprocess.Popen", Recording)

    class Store(ResticStore):
        @contextlib.contextmanager
        def open_dump(self, snapshot_id, cfg):
            with super().open_dump(snapshot_id, cfg) as stream:
                seen.append(_Counted(stream))
                yield seen[-1]

    a = machine(env, "a", store_cls=Store)
    a.use(monkeypatch)
    a.engine.init()
    big = os.urandom(20 * 1024 * 1024)  # incompressible, and after the manifest in the tar
    manifest = Manifest("c", ["home/user/cfg/f.txt"], {"home/user/cfg/f.txt": (_fp(b"x"), SELF)})
    snap = _craft_snapshot(a, "c", {"home/user/cfg/big.bin": big}, manifest)
    with a.engine._workspace():
        read = a.engine._read_manifest(snap.id, "c")
    assert read.entries["home/user/cfg/f.txt"][1] == snap.id
    assert seen and seen[0].n < 2 * 1024 * 1024  # the 20 MB behind the manifest was never read
    assert procs and all(p.returncode is not None for p in procs)  # restic was stopped and reaped: no zombie
    assert all(p.poll() is not None for p in procs)


def test_a_manifest_that_is_not_among_the_first_members_is_refused(env, monkeypatch):
    a = machine(env, "a")
    a.use(monkeypatch)
    a.engine.init()
    manifest = Manifest("c", [], {})

    def write(out) -> object:
        with tarfile.open(fileobj=out, mode="w|") as tar:
            for i in range(12):
                info = tarfile.TarInfo(f"m{i}")
                tar.addfile(info, io.BytesIO(b""))
            info = tarfile.TarInfo(MANIFEST_MEMBER)
            info.size = len(manifest.to_json())
            tar.addfile(info, io.BytesIO(manifest.to_json()))
        return None

    snap, _, _ = a.engine.store.backup(
        write,
        "c",
        datetime.now(),
        None,
        extra_tags=[SYNC_TAG, "parent:none", "gen:0"],  # type: ignore[arg-type]
    )
    with a.engine._workspace(), pytest.raises(ArchiveError, match="no manifest"):
        a.engine._read_manifest(snap.id, "c")


def test_a_snapshot_restic_cannot_dump_is_a_store_error_not_an_unreadable_archive(env, monkeypatch):
    a = machine(env, "a")
    a.use(monkeypatch)
    a.engine.init()
    with a.engine._workspace(), pytest.raises(StoreError, match="restic failed"):
        a.engine._read_manifest("0" * 64, "c")


# ------------------------------------------------------------------------ planned minors


def test_a_conflict_run_keeps_the_sync_state_but_saves_the_hash_cache(pair, monkeypatch):
    import config_saver.lib.sync.scan as scan_mod

    a, b = _synced_pair(pair, monkeypatch)
    a.use(monkeypatch)
    a.write("cfg/app.conf", "from a\n")
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    b.write("cfg/app.conf", "from b, much longer\n")
    before = state_io.load(b.state_file)
    with pytest.raises(SyncConflictError):
        b.engine.run({"c": b.model("cfg")})
    after = state_io.load(b.state_file)
    for name in ("heads", "base", "parents", "last_prune", "last_prune_attempt", "recorded_at", "gens"):
        assert getattr(after, name) == getattr(before, name), name
    key = str(b.home / "cfg" / "app.conf")
    assert key in after.hash_cache and after.hash_cache.get(key) != before.hash_cache.get(
        key
    )  # hashed once, remembered
    calls: list[str] = []
    real = scan_mod._fingerprint
    monkeypatch.setattr(scan_mod, "_fingerprint", lambda c, p, pr: (calls.append(p), real(c, p, pr))[1])
    with pytest.raises(SyncConflictError):
        b.engine.run({"c": b.model("cfg")})
    assert calls == []  # the timer would otherwise rehash everything every run while the conflict stands


def test_resolve_that_conflicts_nowhere_still_saves_state_normally(pair, monkeypatch):
    a, b = _synced_pair(pair, monkeypatch)
    a.use(monkeypatch)
    a.write("cfg/app.conf", "from a\n")
    a.engine.run({"c": a.model("cfg")})
    b.use(monkeypatch)
    b.write("cfg/app.conf", "from b\n")
    with pytest.raises(SyncConflictError):
        b.engine.run({"c": b.model("cfg")})
    b.engine.resolve({"c": b.model("cfg")}, "remote", None)
    assert state_io.load(b.state_file).heads["c"] == b.head()


def test_a_failed_prune_is_not_retried_by_sync_runs_for_a_day(env, monkeypatch, counting):
    clock = Clock()
    a = machine(env, "a", store_cls=counting, now=clock)
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f.txt", "1\n")
    counting.fail_prune = True
    with pytest.raises(SyncTransportError, match="prune"):
        a.engine.run({"c": a.model("cfg")})
    assert state_io.load(a.state_file).last_prune_attempt == T0
    assert counting.prunes == 1
    a.write("cfg/f.txt", "2\n")
    clock.t += DAY - 60
    a.engine.run({"c": a.model("cfg")})  # inside the back-off: the run succeeds without trying
    assert counting.prunes == 1
    clock.t += 120
    a.write("cfg/f.txt", "3\n")
    with pytest.raises(SyncTransportError, match="prune"):
        a.engine.run({"c": a.model("cfg")})  # a day on: tried again, failing again
    assert counting.prunes == 2
    counting.fail_prune = False
    a.engine.prune({"c": a.model("cfg")})  # --sync-prune always tries, whatever the back-off says
    assert counting.prunes == 3
    assert state_io.load(a.state_file).last_prune == clock.t


def test_a_successful_prune_clears_the_back_off_and_a_clock_in_the_past_does_not_extend_it(env, monkeypatch, counting):
    clock = Clock()
    a = machine(env, "a", store_cls=counting, now=clock)
    a.use(monkeypatch)
    a.engine.init()
    a.write("cfg/f.txt", "1\n")
    counting.fail_prune = True
    with pytest.raises(SyncTransportError):
        a.engine.run({"c": a.model("cfg")})
    clock.t -= 3600  # the clock went backwards: a stale attempt in the "future" must not block pruning
    counting.fail_prune = False
    a.write("cfg/f.txt", "2\n")
    a.engine.run({"c": a.model("cfg")})
    assert counting.prunes == 2
    assert state_io.load(a.state_file).last_prune == clock.t


def test_a_move_half_copied_earlier_completes_on_retry(env, monkeypatch):
    a = _published(env, monkeypatch, steps=3)
    target = _target(env)
    new_store = target.store(env["key"])
    new_store.init_from(a.engine.store)
    ids = [s.id for s in a.engine.store.snapshots(tag=SYNC_TAG)]
    new_store.copy_from(a.engine.store, ids[:-1])  # the interrupted first attempt
    assert len(new_store.snapshots(tag=SYNC_TAG)) == len(ids) - 1
    assert a.engine.move(target) == len(ids)
    assert {s.canonical for s in new_store.snapshots(tag=SYNC_TAG)} == {
        s.canonical for s in a.engine.store.snapshots(tag=SYNC_TAG)
    }


def test_move_runs_the_switch_inside_the_lock_and_only_after_verification(env, monkeypatch):
    import fcntl

    a = _published(env, monkeypatch)
    target = _target(env)
    held: list[bool] = []

    def switch() -> None:
        fd = os.open(a.lock_file, os.O_WRONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(False)  # we got it: the engine had already let go
        except OSError:
            held.append(True)
        finally:
            os.close(fd)
        with pytest.raises(SyncTransportError, match="another sync is running"):
            a.engine.run({"c": a.model("cfg")})  # a concurrent --sync cannot slip in before sync.json changes

    a.engine.move(target, on_verified=switch)
    assert held == [True]
    a.engine.run({"c": a.model("cfg")})  # released afterwards


def test_the_switch_is_not_called_when_verification_fails(env, monkeypatch):
    a = _published(env, monkeypatch)
    called: list[int] = []

    class Lossy(ResticStore):
        def copy_from(self, src, ids):
            super().copy_from(src, ids[:-1])

    target = _target(env)
    a.engine.store = Lossy.for_repository(a.engine.store.repo, derive_password(env["key"]), ())
    # The verifying target is the SyncRemote's own store; make it the lossy one
    monkeypatch.setattr(
        SyncRemote, "store", lambda self, identity: Lossy.for_repository(self.repository, derive_password(identity), ())
    )
    with pytest.raises(SyncTransportError, match="verification failed"):
        a.engine.move(target, on_verified=lambda: called.append(1))
    assert called == []
