"""Sync engine: ties scan, classify, manifest, the restic store and git together.

Rules that bind every path through here: classify every config before touching
anything (a conflict anywhere means zero writes, zero commits, state untouched);
only paths inside the local scope are ever written or deleted; every restored byte
goes through TarDecompressor's member validation (this module only chooses which
members it may restore); state is saved only after the push succeeded.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tarfile
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO, Literal

from config_saver.lib.errors import (
    ArchiveError,
    SyncConflictError,
    SyncNotSetUpError,
    SyncTransportError,
)
from config_saver.lib.models.model import Model
from config_saver.lib.restic_store import HOST_TAG_PREFIX, PARENT_TAG_PREFIX, SYNC_TAG, ResticStore, Snapshot
from config_saver.lib.sync import state as state_file_io
from config_saver.lib.sync.classify import Side, classify
from config_saver.lib.sync.git_repo import GitRepo
from config_saver.lib.sync.manifest import MANIFEST_MEMBER, SELF, Manifest, find_head
from config_saver.lib.sync.scan import LocalScope, scan
from config_saver.lib.sync.state import SyncState
from config_saver.lib.tar_compressor.tar_compressor import CompressResult, TarCompressor
from config_saver.lib.tar_compressor.tar_decompressor import TarDecompressor

MAX_ATTEMPTS = 3
_NO_PARENT = "none"
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_STAGING = ".staging"


@dataclass
class SyncReport:
    applied: dict[str, list[str]] = field(default_factory=dict)
    deleted: dict[str, list[str]] = field(default_factory=dict)
    published: dict[str, list[str]] = field(default_factory=dict)
    skipped_remote: list[str] = field(default_factory=list)
    conflicts: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class _Cfg:
    """Everything known about one sync config after pulling and scanning."""

    name: str
    model: Model
    scope: LocalScope
    local: Side
    local_scope: set[str]
    head: str | None
    remote: Manifest | None
    base: Side
    take_remote: list[str]
    delete_local: list[str]
    publish: list[str]
    conflicts: list[str]


def _extend(into: dict[str, list[str]], cfg: str, paths: list[str]) -> None:
    if paths:
        into.setdefault(cfg, []).extend(paths)


def _parent_of(snapshot: Snapshot) -> str | None:
    for tag in snapshot.tags:
        if tag.startswith(PARENT_TAG_PREFIX):
            value = tag[len(PARENT_TAG_PREFIX) :]
            return None if value == _NO_PARENT else value
    return None


class SyncEngine:
    def __init__(
        self,
        sync_dir: str,
        state_file: str,
        identity: str | None,
        host: str,
        git: GitRepo | None = None,
    ):
        self.sync_dir = sync_dir
        self.state_file = state_file
        self.host = re.sub(r"[\s,]+", "-", host) or "unknown"
        self.store = ResticStore(sync_dir, identity)
        self.git = git or GitRepo(sync_dir)
        self.staging = os.path.join(sync_dir, _STAGING)

    # ------------------------------------------------------------------- init

    def init(self, remote_url: str, recipient: str | None) -> None:
        if os.path.exists(os.path.join(self.sync_dir, ".git")):
            raise FileExistsError(f"sync is already set up in {self.sync_dir}")
        existed = os.path.isdir(self.sync_dir)
        if self.git.remote_has_branch(remote_url):
            self.git.clone(remote_url)  # the shared age key decrypts it: no recipient needed
            return
        if not recipient:
            raise SyncNotSetUpError("the remote is empty: pass --encrypt-to <age recipient> to create the sync store")
        try:
            os.makedirs(self.sync_dir, mode=0o700, exist_ok=True)
            self.store.init(recipient)
            self.git.init(remote_url)
            self.git.commit_all("config-saver sync: init")
            if not self.git.push():
                raise SyncTransportError("the remote changed while initialising; run the init again")
        except BaseException:
            if not existed:
                shutil.rmtree(self.sync_dir, ignore_errors=True)
            raise

    # ------------------------------------------------------------ public verbs

    def status(self, configs: Mapping[str, Model]) -> SyncReport:
        with self._workspace():
            cfgs, skipped = self._analyse(configs, state_file_io.load(self.state_file))
            return self._report(cfgs, skipped)

    def run(self, configs: Mapping[str, Model]) -> SyncReport:
        return self._run(configs, state_file_io.load(self.state_file))

    def resolve(
        self, configs: Mapping[str, Model], side: Literal["local", "remote"], paths: list[str] | None
    ) -> SyncReport:
        state = state_file_io.load(self.state_file)
        with self._workspace():
            cfgs, _ = self._analyse(configs, state)
        current = {p for c in cfgs.values() for p in c.conflicts}
        for path in paths or []:
            if path not in current:
                raise ValueError(f"not a current conflict: {path}")
        chosen = current if paths is None else set(paths)
        for c in cfgs.values():
            base = dict(state.base.get(c.name, {}))
            other = (c.remote.side() if c.remote else {}) if side == "local" else c.local
            for path in c.conflicts:
                if path in chosen:
                    if path in other:
                        base[path] = other[path]
                    else:
                        base.pop(path, None)
            state.base[c.name] = base
        return self._run(configs, state)  # in memory only: saved if the run succeeds

    # --------------------------------------------------------------- the run

    def _run(self, configs: Mapping[str, Model], state: SyncState) -> SyncReport:
        total = SyncReport()
        with self._workspace():
            for _attempt in range(MAX_ATTEMPTS):
                cfgs, skipped = self._analyse(configs, state)
                conflicts = {c.name: c.conflicts for c in cfgs.values() if c.conflicts}
                if conflicts:
                    raise SyncConflictError(conflicts)
                total.skipped_remote = skipped
                for c in cfgs.values():
                    self._apply(c)
                    _extend(total.applied, c.name, c.take_remote)
                    _extend(total.deleted, c.name, c.delete_local)
                merged = {c.name: self._merge(c) for c in cfgs.values()}
                new_ids = self._publish(cfgs, merged)
                if new_ids is None:  # the remote moved under us: start again from the pull
                    continue
                for c in cfgs.values():
                    side, publish = merged[c.name]
                    state.base[c.name] = side
                    head = new_ids.get(c.name, c.head)
                    if head is not None:
                        state.heads[c.name] = head
                    if c.name in new_ids:
                        _extend(total.published, c.name, sorted(publish))
                state_file_io.save(self.state_file, state)
                return total
        raise SyncTransportError("another machine kept pushing; try again")

    def _publish(self, cfgs: dict[str, _Cfg], merged: dict[str, tuple[Side, set[str]]]) -> dict[str, str] | None:
        """Upload what only this machine has; ``{}`` if nothing, ``None`` if the push lost a race."""
        created: dict[str, str] = {}
        for c in cfgs.values():
            side, publish = merged[c.name]
            if not publish:
                continue
            manifest = self._manifest(c, side, publish)
            only = set(publish)
            compressor = TarCompressor(c.model)
            tags = [SYNC_TAG, f"{PARENT_TAG_PREFIX}{c.head or _NO_PARENT}", f"{HOST_TAG_PREFIX}{self.host}"]
            snapshot, _, _ = self.store.backup(
                self._writer(compressor, only, manifest), c.name, datetime.now(), None, extra_tags=tags
            )
            created[c.name] = snapshot.id
        if not created:
            return created
        self.git.commit_all(f"sync from {self.host}")
        if self.git.push():
            return created
        self.store.forget_ids(list(created.values()))
        self.git.reset_to_upstream()
        return None

    @staticmethod
    def _writer(compressor: TarCompressor, only: set[str], manifest: Manifest) -> Callable[[IO[bytes]], CompressResult]:
        def write(out: IO[bytes]) -> CompressResult:
            return compressor.compress_to_stream(out, only=only, extra_members={MANIFEST_MEMBER: manifest.to_json()})

        return write

    # ------------------------------------------------------------- analysis

    def _analyse(self, configs: Mapping[str, Model], state: SyncState) -> tuple[dict[str, _Cfg], list[str]]:
        if not os.path.isdir(os.path.join(self.sync_dir, ".git")):
            raise SyncNotSetUpError("sync is not set up on this machine: run the sync init first")
        # Anything unpushed in the working tree is leftover of a run that died before
        # saving its state: it is disposable, and an orphan snapshot would fork the history.
        self.git.reset_to_upstream()
        self.git.pull()
        cfgs: dict[str, _Cfg] = {}
        for name in sorted(n for n, m in configs.items() if m.sync):
            cfgs[name] = self._analyse_one(name, configs[name], state.base.get(name, {}))
        remote_names = {s.cfg for s in self.store.snapshots(tag=SYNC_TAG)}
        return cfgs, sorted(remote_names - set(cfgs))

    def _analyse_one(self, name: str, model: Model, base: Side) -> _Cfg:
        scope, local, _total = scan(model)
        snaps = self.store.snapshots(name, tag=SYNC_TAG)
        head = find_head([(s.id, _parent_of(s)) for s in snaps], name)
        remote = self._read_manifest(head, name) if head else None
        remote_side = remote.side() if remote else {}
        local_scope = {p for p in set(base) | set(local) | set(remote_side) if scope.contains(p)}
        plan = classify(base, local, remote_side, local_scope, set(remote.scope) if remote else set())
        return _Cfg(
            name, model, scope, local, local_scope, head, remote, base,
            plan.take_remote, plan.delete_local, plan.publish, plan.conflicts,
        )  # fmt: skip

    def _read_manifest(self, snapshot_id: str, cfg: str) -> Manifest:
        with self._dump(snapshot_id, cfg) as tar_path:
            try:
                with tarfile.open(tar_path, "r:*") as tar:
                    handle = tar.extractfile(tar.getmember(MANIFEST_MEMBER))
                    data = handle.read(_MAX_MANIFEST_BYTES + 1) if handle else b""
            except KeyError as exc:
                raise ArchiveError(f"sync snapshot {snapshot_id} of {cfg} has no manifest") from exc
            except tarfile.TarError as exc:
                raise ArchiveError(f"sync snapshot {snapshot_id} of {cfg} is unreadable: {exc}") from exc
        if len(data) > _MAX_MANIFEST_BYTES:
            raise ArchiveError(f"sync manifest of {cfg} is implausibly large")
        manifest = Manifest.from_json(data)
        if manifest.cfg != cfg:
            raise ArchiveError(f"sync snapshot {snapshot_id} claims to be config {manifest.cfg!r}, not {cfg!r}")
        return manifest.resolve(snapshot_id)

    @staticmethod
    def _report(cfgs: dict[str, _Cfg], skipped: list[str]) -> SyncReport:
        report = SyncReport(skipped_remote=skipped)
        for c in cfgs.values():
            _extend(report.applied, c.name, c.take_remote)
            _extend(report.deleted, c.name, c.delete_local)
            _extend(report.published, c.name, c.publish)
            _extend(report.conflicts, c.name, c.conflicts)
        return report

    # ---------------------------------------------------------------- writes

    def _apply(self, c: _Cfg) -> None:
        """Restore what the remote changed. Only members chosen here are written."""
        if c.remote is None:
            return
        by_snapshot: dict[str, list[str]] = {}
        for path in c.take_remote:
            by_snapshot.setdefault(c.remote.entries[path][1], []).append(path)
        for snapshot_id, names in by_snapshot.items():
            with self._dump(snapshot_id, c.name) as tar_path:
                result = TarDecompressor(tar_path, members=names, staging_dir=self.staging).decompress()
            if result.extracted != len(names):
                raise ArchiveError(
                    f"snapshot {snapshot_id} of {c.name} does not hold all of {len(names)} expected paths"
                )
        locate = TarDecompressor("")
        for path in c.delete_local:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(locate._denormalize_path(path))

    @staticmethod
    def _merge(c: _Cfg) -> tuple[Side, set[str]]:
        """The side after this run, and the paths this machine must publish."""
        side = {p: fp for p, fp in c.local.items() if p in c.local_scope}
        remote_side = c.remote.side() if c.remote else {}
        for path in c.take_remote:
            side[path] = remote_side[path]
        for path in c.delete_local:
            side.pop(path, None)
        publish = set(c.publish)
        # Content no remote snapshot holds (the remote narrowed its scope) must travel too.
        publish |= {p for p, fp in side.items() if p not in publish and remote_side.get(p) != fp}
        return side, publish

    @staticmethod
    def _manifest(c: _Cfg, side: Side, publish: set[str]) -> Manifest:
        entries = {p: e for p, e in (c.remote.entries if c.remote else {}).items() if p not in c.local_scope}
        for path, fingerprint in side.items():
            entries[path] = (fingerprint, SELF if path in publish else c.remote.entries[path][1])  # type: ignore[union-attr]
        scope = sorted(c.local_scope | set(c.remote.scope if c.remote else []))
        manifest = Manifest(c.name, scope, entries)
        Manifest.from_json(manifest.to_json())  # never publish what no machine could read back
        return manifest

    # ------------------------------------------------------------- plumbing

    @contextlib.contextmanager
    def _workspace(self) -> Iterator[None]:
        """A fresh private staging area (it holds plaintext), removed on every path."""
        shutil.rmtree(self.staging, ignore_errors=True)
        os.makedirs(self.staging, mode=0o700)
        try:
            yield
        finally:
            shutil.rmtree(self.staging, ignore_errors=True)

    @contextlib.contextmanager
    def _dump(self, snapshot_id: str, cfg: str) -> Iterator[str]:
        fd, path = tempfile.mkstemp(dir=self.staging, prefix="dump.", suffix=".tar")  # 0600
        try:
            with os.fdopen(fd, "wb") as out:
                self.store.dump(snapshot_id, cfg, out)
            yield path
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
