"""Move existing tar archives into the restic store, deleting nothing that is not verified.

Each archive is decrypted (age/gpg, plaintext only ever in a pipe), gunzipped and streamed
into ``restic backup --stdin`` with its own timestamp. The sha256 of the tar bytes handed to
restic is the reference: an archive may be deleted (only when asked to) after ``restic dump``
of the new snapshot hashes to the very same value.
"""

from __future__ import annotations

import contextlib
import fnmatch
import gzip
import hashlib
import os
import shutil
import subprocess
import tarfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO

from . import crypto
from .archive_names import FILENAME_PATTERN
from .backup_manager.backup_manager import LEGACY_NAME, BackupManager
from .errors import EncryptionError, StoreError
from .restic_store import ResticStore, Snapshot, drain
from .tar_compressor.tar_compressor import CompressResult
from .tar_prune import ARCHIVE_GLOBS, TIMESTAMP_FORMAT

_CHUNK = 1024 * 1024
_LEGACY_STEM = "config-saver"
_KILL_GRACE_SECONDS = 5


@dataclass
class MigrationReport:
    migrated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    bytes_before: int = 0
    # Set when the closing ``restic check`` failed; deletes already made were individually verified.
    check_error: str | None = None
    checked: bool = False  # the closing ``restic check`` ran


class _HashSink:
    """A write-only file that hashes what it is given."""

    def __init__(self) -> None:
        self.digest = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self.digest.update(data)
        return len(data)


class _Tee:
    """Reads from ``source``, hashing every byte and copying it to ``sink`` as it goes by."""

    def __init__(self, source: IO[bytes], sink: IO[bytes], digest: hashlib._Hash) -> None:
        self.source = source
        self.sink = sink
        self.digest = digest

    def read(self, size: int = -1) -> bytes:
        data = self.source.read(size)
        if data:
            self.digest.update(data)
            self.sink.write(data)
        return data


def _mtime(path: str) -> datetime:
    return datetime.fromtimestamp(os.path.getmtime(path)).replace(microsecond=0)


def classify(path: str, saves_dir: str) -> tuple[str, datetime]:
    """(config name, backup time) of an archive: from its name, else the config 'legacy' / its mtime."""
    name = os.path.basename(path)
    top_level = os.path.dirname(os.path.abspath(path)) == os.path.abspath(saves_dir)
    match = FILENAME_PATTERN.match(name)
    if match:
        cfg = match.group(1)
        if top_level and cfg == _LEGACY_STEM:
            cfg = LEGACY_NAME
        try:
            return cfg, datetime.strptime(match.group(2), TIMESTAMP_FORMAT)
        except ValueError:  # shaped like a timestamp but not one
            return cfg, _mtime(path)
    stem = name
    for suffix in (".age", ".gpg", ".gz", ".tar"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return (LEGACY_NAME if top_level and stem == _LEGACY_STEM else stem), _mtime(path)


@contextlib.contextmanager
def _plain_tar(path: str, identity: str | None) -> Iterator[IO[bytes]]:
    """The decompressed tar bytes of an archive as a stream; decryption runs in a child, never via a temp file."""
    method = crypto.detect_method(path)
    if method is None:
        with open(path, "rb") as raw, gzip.GzipFile(fileobj=raw) as plain:
            yield plain  # type: ignore[misc]
        return
    binary = crypto.require_binary(method)
    if method == "age":
        if not identity or not os.path.isfile(identity):
            raise EncryptionError(f"age identity file not found: {identity or '(none given)'}; pass --identity")
        argv = [binary, "-d", "-i", identity, path]
    else:
        argv = [binary, "--batch", "--decrypt", path]
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise EncryptionError(f"could not run {method}: {exc}") from exc
    assert proc.stdout is not None and proc.stderr is not None
    err_thread, err_chunks = drain(proc.stderr)

    def detail() -> str:
        err_thread.join()
        return b"".join(err_chunks).decode("utf-8", "replace").strip() or f"exit status {proc.returncode}"

    try:
        with gzip.GzipFile(fileobj=proc.stdout) as plain:
            yield plain  # type: ignore[misc]
        shutil.copyfileobj(proc.stdout, _HashSink())  # let the child finish
    except BaseException as exc:
        # A failed decrypt only shows up as "not a gzip file": prefer the tool's own words.
        try:
            proc.wait(timeout=_KILL_GRACE_SECONDS)
            died_alone = True
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            died_alone = False
        if died_alone and proc.returncode != 0 and isinstance(exc, Exception):
            raise EncryptionError(f"could not decrypt with {method}: {detail()}") from exc
        raise
    if proc.wait() != 0:
        raise EncryptionError(f"could not decrypt with {method}: {detail()}")


def _count_members(tee: _Tee) -> int:
    """Walk the tar as it streams (counting members) and drain the rest, so every byte is copied.

    Counting is best effort: a tar the parser cannot follow still gets copied whole, and the
    hash comparison is what protects it.
    """
    count = 0
    try:
        with tarfile.open(fileobj=tee, mode="r|") as archive:  # type: ignore[call-overload]
            for _member in archive:
                count += 1
    except tarfile.TarError:
        pass
    while tee.read(_CHUNK):
        pass
    return count


def _hash_source(path: str, identity: str | None) -> str:
    sink = _HashSink()
    with _plain_tar(path, identity) as plain:
        shutil.copyfileobj(plain, sink, _CHUNK)
    return sink.digest.hexdigest()


def _hash_snapshot(store: ResticStore, snap: Snapshot) -> str:
    sink = _HashSink()
    store.dump(snap.id, snap.cfg, sink)  # type: ignore[arg-type]
    return sink.digest.hexdigest()


def _delete(path: str, saves_dir: str) -> None:
    """Remove a verified archive, then its description.txt and timestamp dir once nothing else is in there."""
    os.remove(path)
    folder = os.path.dirname(os.path.abspath(path))
    if folder == os.path.abspath(saves_dir):
        return
    siblings = os.listdir(folder)
    if any(fnmatch.fnmatch(n, g) for n in siblings for g in ARCHIVE_GLOBS):
        return
    with contextlib.suppress(FileNotFoundError):
        os.remove(os.path.join(folder, "description.txt"))
    with contextlib.suppress(OSError):  # not empty: something else lives there, leave it
        os.rmdir(folder)


def _forget_bad_snapshot(store: ResticStore, snapshot: Snapshot) -> str:
    """Drop the snapshot this run just wrote and could not verify, so a re-run migrates the archive again."""
    message = f"hash mismatch: snapshot {snapshot.short_id} does not hash like the archive; archive kept"
    try:
        store.forget_ids([snapshot.id])
    except StoreError as exc:
        return f"{message}; could not forget it either ({exc}): run `restic forget {snapshot.id}`"
    return f"{message}; the bad snapshot was forgotten, re-run to migrate it again"


def _describe(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def migrate_archives(
    manager: BackupManager,
    store: ResticStore,
    identity: str | None,
    delete: bool,
    progress: Callable[[str], None] | None = None,
) -> MigrationReport:
    """Move every tar archive into ``store``, oldest first; see the module docstring for the safety rules."""
    say = progress or (lambda _line: None)
    report = MigrationReport()
    identity = identity or store.identity
    saves_dir = manager.saves_dir
    plan = sorted(((*classify(p, saves_dir), p) for p in manager.list_archives()), key=lambda e: (e[1], e[2]))
    existing: dict[tuple[str, datetime], Snapshot] = {(s.cfg, s.time): s for s in store.snapshots()}
    total = len(plan)
    in_plan: dict[tuple[str, datetime], str] = {}

    for index, (cfg, when, path) in enumerate(plan, start=1):
        when = when.replace(microsecond=0)
        tag = f"[{index}/{total}] {cfg} {when:%Y-%m-%d %H:%M:%S}"
        with contextlib.suppress(OSError):
            report.bytes_before += os.path.getsize(path)
        try:
            twin = in_plan.setdefault((cfg, when), path)
            if twin != path:
                raise StoreError(f"same config and timestamp as {twin}; not migrated (it would collide)")
            known = existing.get((cfg, when))
            if known is not None:
                if delete:
                    if _hash_source(path, identity) != _hash_snapshot(store, known):
                        raise StoreError(
                            f"snapshot {known.short_id} differs from {path}; inspect it and run "
                            f"`restic forget {known.id}` (or remove it) to re-migrate"
                        )
                    _delete(path, saves_dir)
                    report.deleted.append(path)
                report.skipped.append(path)
                say(f"{tag}: already in the store, skipped" + (" (verified, deleted)" if delete else ""))
                continue
            in_config_tree = os.path.dirname(os.path.abspath(path)) != os.path.abspath(saves_dir)
            description = manager.get_description_for_archive(path) if in_config_tree else None
            digest = hashlib.sha256()

            def write(stdin: IO[bytes], path: str = path, digest: hashlib._Hash = digest) -> CompressResult:
                with _plain_tar(path, identity) as plain:
                    members = _count_members(_Tee(plain, stdin, digest))
                return CompressResult(output_path=path, added=members)

            snapshot, _result, _added = store.backup(write, cfg, when, description or None)
            if _hash_snapshot(store, snapshot) != digest.hexdigest():
                raise StoreError(_forget_bad_snapshot(store, snapshot))
            existing[(cfg, when)] = snapshot
            if delete:
                _delete(path, saves_dir)
                report.deleted.append(path)
            report.migrated.append(path)
            say(f"{tag}: migrated as {snapshot.short_id}" + (" (verified, deleted)" if delete else ""))
        except Exception as exc:  # one archive failing never stops the others, and never deletes anything
            report.failed.append((path, _describe(exc)))
            say(f"{tag}: FAILED, kept: {_describe(exc)}")

    if report.migrated or report.skipped:
        try:
            store.check()
            report.checked = True
        except StoreError as exc:
            report.check_error = _describe(exc)
    return report
