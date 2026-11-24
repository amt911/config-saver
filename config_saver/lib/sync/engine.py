"""Sync engine: ties scan, classify, manifest and the restic store together.

Rules that bind every path through here: classify every config before touching
anything (a conflict anywhere means zero writes, zero snapshots, state untouched);
only paths inside the local scope are ever written or deleted; every restored byte
goes through TarDecompressor's member validation (this module only chooses which
members it may restore); state is saved only after the publish won.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shutil
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO, Literal, TypeVar

from config_saver.lib.errors import (
    ArchiveError,
    StoreError,
    SyncConflictError,
    SyncStateError,
    SyncTransportError,
)
from config_saver.lib.models.model import Model
from config_saver.lib.restic_store import (
    DEFAULT_IDENTITY,
    HOST_TAG_PREFIX,
    PARENT_TAG_PREFIX,
    SYNC_TAG,
    ResticStore,
    Snapshot,
)
from config_saver.lib.sync import state as state_file_io
from config_saver.lib.sync.classify import Side, classify
from config_saver.lib.sync.manifest import MANIFEST_MEMBER, SELF, Manifest, SnapRef, losers, select_head, unreferenced
from config_saver.lib.sync.remote import SyncRemote
from config_saver.lib.sync.scan import LocalScope, scan
from config_saver.lib.sync.state import SyncState
from config_saver.lib.tar_compressor.tar_compressor import CompressResult, TarCompressor
from config_saver.lib.tar_compressor.tar_decompressor import TarDecompressor

MAX_ATTEMPTS = 3
_NO_PARENT = "none"
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_STAGING = ".staging"
PRUNE_INTERVAL = 7 * 24 * 3600.0


@dataclass
class SyncReport:
    applied: dict[str, list[str]] = field(default_factory=dict)
    deleted: dict[str, list[str]] = field(default_factory=dict)
    published: dict[str, list[str]] = field(default_factory=dict)
    skipped_remote: list[str] = field(default_factory=list)
    conflicts: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class _Published:
    """A won publish: the new snapshot per config, their manifests, and the listing that confirmed the win."""

    ids: dict[str, str]
    manifests: dict[str, Manifest]
    by_cfg: dict[str, list[SnapRef]]


@dataclass
class _Cfg:
    """Everything known about one sync config after listing and scanning."""

    name: str
    snaps: list[SnapRef]  # this config's listing when it was analysed
    model: Model
    scope: LocalScope
    local: Side
    local_scope: set[str]
    head: str | None
    head_parent: str | None
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


_T = TypeVar("_T")


def _transport(call: Callable[[], _T]) -> _T:
    """Run a sync verb; a restic failure inside it is a transport failure (exit 10)."""
    try:
        return call()
    except StoreError as exc:
        raise SyncTransportError(str(exc)) from exc


class SyncEngine:
    def __init__(
        self,
        remote: SyncRemote,
        identity: str | None,
        state_file: str,
        staging_root: str,
        lock_file: str,
        host: str,
        store: ResticStore | None = None,
        now: Callable[[], float] = time.time,
    ):
        self.now = now
        self.remote = remote
        self.state_file = state_file
        self.lock_file = lock_file
        self.host = re.sub(r"[\s,]+", "-", host) or "unknown"
        self.store = store or remote.store(identity or DEFAULT_IDENTITY)
        self.staging_root = staging_root
        self.staging = os.path.join(staging_root, _STAGING)

    # ------------------------------------------------------------------- init

    def init(self) -> Literal["created", "joined"]:
        """Join the repository if it exists (nothing is written there), else create it."""

        def go() -> Literal["created", "joined"]:
            if self.store.exists():
                return "joined"
            self.store.init_plain()
            return "created"

        return _transport(go)

    # ------------------------------------------------------------ public verbs

    def status(self, configs: Mapping[str, Model]) -> SyncReport:
        with self._locked(), self._workspace():
            cfgs, skipped = _transport(lambda: self._analyse(configs, state_file_io.load(self.state_file)))
            return self._report(cfgs, skipped)

    def run(self, configs: Mapping[str, Model]) -> SyncReport:
        with self._locked():
            return _transport(lambda: self._run(configs, state_file_io.load(self.state_file)))

    def prune(self, configs: Mapping[str, Model]) -> list[str]:
        """Forget what no head needs, then reclaim the space whatever the clock says. Returns forgotten ids."""
        with self._locked():
            return _transport(lambda: self._prune(configs))

    def _prune(self, configs: Mapping[str, Model]) -> list[str]:
        state = state_file_io.load(self.state_file)
        with self._workspace():
            cfgs, _ = self._analyse(configs, state)
            forgotten = self._forget(cfgs, None, {c.name: c.snaps for c in cfgs.values()})
        self.store.prune()
        state.last_prune = self.now()
        state_file_io.save(self.state_file, state)
        return forgotten

    def resolve(
        self, configs: Mapping[str, Model], side: Literal["local", "remote"], paths: list[str] | None
    ) -> SyncReport:
        with self._locked():
            return _transport(lambda: self._resolve(configs, side, paths))

    def _resolve(
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
            base = dict(c.base)  # the effective base: after a fork it is not the recorded one
            other = (c.remote.side() if c.remote else {}) if side == "local" else c.local
            for path in c.conflicts:
                if path in chosen:
                    if path in other:
                        base[path] = other[path]
                    else:
                        base.pop(path, None)
            state.base[c.name] = base
            if c.head is None:
                state.heads.pop(c.name, None)
                state.parents.pop(c.name, None)
            else:  # the base now describes this head: later analyses must not re-derive it from the fork
                state.heads[c.name] = c.head
                state.parents[c.name] = c.head_parent or ""
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
                staged = [(c, self._stage(c)) for c in cfgs.values()]  # dump + vet everything first
                for c, groups in staged:
                    self._apply(c, groups)
                    _extend(total.applied, c.name, c.take_remote)
                    _extend(total.deleted, c.name, c.delete_local)
                merged = {c.name: self._merge(c) for c in cfgs.values()}
                published = self._publish(cfgs, merged)
                if published is None:  # lost the race to another machine: start again from the listing
                    continue
                new_ids = published.ids
                for c in cfgs.values():
                    side, publish = merged[c.name]
                    state.base[c.name] = side
                    head = new_ids.get(c.name, c.head)
                    if head is not None:
                        state.heads[c.name] = head
                        state.parents[c.name] = (c.head if c.name in new_ids else c.head_parent) or ""
                    if c.name in new_ids:
                        _extend(total.published, c.name, sorted(publish))
                state_file_io.save(self.state_file, state)
                # Housekeeping only after the publish is durable: a failure here loses nothing.
                self._forget(cfgs, published, published.by_cfg)
                if state.last_prune is None or self.now() - state.last_prune >= PRUNE_INTERVAL:
                    self.store.prune()
                    state.last_prune = self.now()
                    state_file_io.save(self.state_file, state)
                return total
        raise SyncTransportError("another machine kept publishing; try again")

    def _forget(
        self, cfgs: dict[str, _Cfg], published: _Published | None, listing: dict[str, list[SnapRef]]
    ) -> list[str]:
        """Forget snapshots no head needs, among the configs this run read, per the confirming listing.

        A config whose head in that listing is not the one this run holds a manifest for (another
        machine built on it since the analysis) is left alone: that head's manifest was never read,
        so nothing could safely be called unreferenced.
        """
        doomed: set[str] = set()
        for c in cfgs.values():
            snaps = listing.get(c.name, c.snaps)
            head, manifest = c.head, c.remote
            if published and c.name in published.ids:
                head, manifest = published.ids[c.name], published.manifests[c.name]
            if select_head(snaps) != head:
                continue
            known = {head: manifest} if head and manifest else {}
            doomed |= unreferenced(snaps, known) | losers(snaps)
        ids = sorted(doomed)
        self.store.forget_ids(ids)
        return ids

    def _publish(self, cfgs: dict[str, _Cfg], merged: dict[str, tuple[Side, set[str]]]) -> _Published | None:
        """Upload what only this machine has; empty ids if nothing, ``None`` if another machine's publish won."""
        created: dict[str, str] = {}
        manifests: dict[str, Manifest] = {}
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
            manifests[c.name] = manifest
        if not created:
            return _Published({}, {}, {})
        by_cfg = self._by_cfg(self.store.snapshots(tag=SYNC_TAG))
        listed = {s.id for snaps in by_cfg.values() for s in snaps}
        # A manifest naming a snapshot that is gone (another machine's housekeeping forgot it while we
        # uploaded) cannot be restored: treat it like a lost race, never publish it.
        dangling = [
            name
            for name, snap_id in created.items()
            if any(ref not in listed for _, ref in manifests[name].resolve(snap_id).entries.values())
        ]
        if dangling:
            self.store.forget_ids([created[n] for n in dangling])
            return None
        # Ours won when it is on the winning chain: another machine may already have built on top of it.
        if all(snap_id not in losers(by_cfg.get(name, [])) for name, snap_id in created.items()):
            return _Published(created, manifests, by_cfg)
        # Only the losers: a snapshot of another cfg that won may already have another machine on top of it.
        self.store.forget_ids([i for name, i in created.items() if i in losers(by_cfg.get(name, []))])
        return None

    @staticmethod
    def _writer(compressor: TarCompressor, only: set[str], manifest: Manifest) -> Callable[[IO[bytes]], CompressResult]:
        def write(out: IO[bytes]) -> CompressResult:
            return compressor.compress_to_stream(out, only=only, extra_members={MANIFEST_MEMBER: manifest.to_json()})

        return write

    # ------------------------------------------------------------- analysis

    def _analyse(self, configs: Mapping[str, Model], state: SyncState) -> tuple[dict[str, _Cfg], list[str]]:
        all_snaps = self.store.snapshots(tag=SYNC_TAG)
        by_cfg = self._by_cfg(all_snaps)
        cfgs: dict[str, _Cfg] = {}
        home = os.path.expanduser("~")
        if state.hash_cache_home != home:  # content normalization depends on it: start the cache over
            state.hash_cache = {}
            state.hash_cache_home = home
        seen: set[str] = set()
        for name in sorted(n for n, m in configs.items() if m.sync):
            cfgs[name] = self._analyse_one(name, configs[name], state, by_cfg.get(name, []), seen)
        for stale in set(state.hash_cache) - seen:
            del state.hash_cache[stale]
        return cfgs, sorted(set(by_cfg) - set(cfgs))

    @staticmethod
    def _by_cfg(snapshots: list[Snapshot]) -> dict[str, list[SnapRef]]:
        by_cfg: dict[str, list[SnapRef]] = {}
        for s in snapshots:
            by_cfg.setdefault(s.cfg, []).append(SnapRef(s.id, _parent_of(s), s.time))
        return by_cfg

    def _effective_base(self, name: str, state: SyncState, snaps: list[SnapRef]) -> Side:
        """The recorded base, unless the head it was recorded at is no longer on the winning chain.

        Another machine's earlier-dated publish can flip the fork winner after we finished. Our
        recorded base describes a head that lost (or was pruned), so comparing against it would let
        the winner overwrite our edits. Then the base is the manifest of the nearest ancestor of
        the recorded head that is on the winning chain, or empty when the fork reaches the root.
        """
        recorded = state.base.get(name, {})
        rec = state.heads.get(name)
        if rec is None:
            return recorded
        lost = losers(snaps)
        listed = {s.id: s for s in snaps}
        on_chain = set(listed) - lost
        if rec in on_chain:
            return recorded
        if rec not in listed and any(s.parent == rec for s in snaps if s.id in on_chain):
            return recorded  # pruned after the winning chain was built on top of it
        ancestor: str | None = state.parents.get(name) or None
        while ancestor is not None and ancestor not in on_chain:
            ancestor = listed[ancestor].parent if ancestor in listed else None
        return self._read_manifest(ancestor, name).side() if ancestor else {}

    def _analyse_one(self, name: str, model: Model, state: SyncState, snaps: list[SnapRef], seen: set[str]) -> _Cfg:
        scope, local, _total = scan(model, state.hash_cache, seen)
        head = select_head(snaps)
        if snaps and head is None:
            raise SyncStateError(f"the sync history of {name} has no usable head (a snapshot cycle?)")
        base = self._effective_base(name, state, snaps)
        head_parent = next((s.parent for s in snaps if s.id == head), None)
        remote = self._read_manifest(head, name) if head else None
        remote_side = remote.side() if remote else {}
        local_scope = {p for p in set(base) | set(local) | set(remote_side) if scope.contains(p)}
        plan = classify(base, local, remote_side, local_scope, set(remote.scope) if remote else set())
        return _Cfg(
            name, snaps, model, scope, local, local_scope, head, head_parent, remote, base,
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

    def _stage(self, c: _Cfg) -> list[tuple[str, list[str]]]:
        """Dump every snapshot this config restores from and vet all of it; writes nothing.

        Each group is ``(tar path, member names)``. A hostile member anywhere, a missing
        or duplicated wanted member: all stop the run here, before any user file changes.
        """
        if c.remote is None:
            return []
        by_snapshot: dict[str, list[str]] = {}
        for path in c.take_remote:
            by_snapshot.setdefault(c.remote.entries[path][1], []).append(path)
        groups = []
        for snapshot_id, names in by_snapshot.items():
            tar_path = self._dump_file(snapshot_id, c.name)
            counts = TarDecompressor(tar_path, members=names, staging_dir=self.staging).check_members()
            bad = sorted(n for n, k in counts.items() if k != 1)
            if bad:
                raise ArchiveError(f"snapshot {snapshot_id} of {c.name} must hold each of these exactly once: {bad}")
            groups.append((tar_path, names))
        return groups

    def _apply(self, c: _Cfg, groups: list[tuple[str, list[str]]]) -> None:
        """Restore what the remote changed. Only members chosen here are written."""
        for tar_path, names in groups:
            TarDecompressor(tar_path, members=names, staging_dir=self.staging).decompress()
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
        os.makedirs(self.staging_root, mode=0o700, exist_ok=True)
        os.makedirs(self.staging, mode=0o700)
        try:
            yield
        finally:
            shutil.rmtree(self.staging, ignore_errors=True)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """One sync at a time per machine (timer vs manual run): non-blocking, fail fast."""
        os.makedirs(os.path.dirname(self.lock_file), mode=0o700, exist_ok=True)
        fd = os.open(self.lock_file, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SyncTransportError("another sync is running") from exc
            yield
        finally:
            os.close(fd)  # closing releases the lock

    def _dump_file(self, snapshot_id: str, cfg: str) -> str:
        """Dump a snapshot's tar to a private file in the staging area (removed with it)."""
        fd, path = tempfile.mkstemp(dir=self.staging, prefix="dump.", suffix=".tar")  # 0600
        with os.fdopen(fd, "wb") as out:
            self.store.dump(snapshot_id, cfg, out)
        return path

    @contextlib.contextmanager
    def _dump(self, snapshot_id: str, cfg: str) -> Iterator[str]:
        path = self._dump_file(snapshot_id, cfg)
        try:
            yield path
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
