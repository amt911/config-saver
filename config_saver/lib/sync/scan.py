"""Local side of a sync: which paths are in scope and what their content hashes to.

Reuses TarCompressor for path normalization, exclusion and content normalization,
so a fingerprint is computed over exactly the bytes that would be archived.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import NoReturn

from config_saver.lib.errors import SyncTooLargeError
from config_saver.lib.models.model import Model
from config_saver.lib.sync.classify import Fingerprint, Side
from config_saver.lib.tar_compressor.tar_compressor import TarCompressor

MAX_SYNC_BYTES = 50 * 1024 * 1024
_CHUNK = 256 * 1024


def _never(_path: str) -> bool:
    return False


@dataclass(frozen=True)
class LocalScope:
    """What this machine's configuration declares, whether or not it exists on disk.

    A deleted local file stays in scope so its deletion can propagate.
    """

    roots: frozenset[str]
    # arcname of each root -> the local path it was normalized from.
    root_paths: Mapping[str, str] = field(default_factory=dict, compare=False)
    is_excluded: Callable[[str], bool] = field(default=_never, compare=False)

    def contains(self, arcname: str) -> bool:
        """True when ``arcname`` is a root or lies under one, and no exclude prunes it."""
        for root in self.roots:
            if arcname == root:
                rest: list[str] = []
            elif arcname.startswith(root + "/"):
                rest = arcname[len(root) + 1 :].split("/")
            else:
                continue
            path = self.root_paths.get(root)
            if path is None:
                return True
            # The root, every directory down to the file, and the file itself.
            if self.is_excluded(path):
                continue
            pruned = False
            for part in rest:
                path = os.path.join(path, part)
                if self.is_excluded(path):
                    pruned = True
                    break
            if not pruned:
                return True
        return False


def _fingerprint(compressor: TarCompressor, path: str) -> tuple[Fingerprint, int] | None:
    """Fingerprint of one file or symlink, and its raw size; None if it vanished."""
    try:
        info = os.lstat(path)
        mode = info.st_mode & 0o777
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            digest = hashlib.sha256(os.fsencode(target)).hexdigest()
            return Fingerprint(digest, mode, target), 0
        sha = hashlib.sha256()
        with compressor.content_stream(path) as stream:
            while chunk := stream.read(_CHUNK):
                sha.update(chunk)
        return Fingerprint(sha.hexdigest(), mode), info.st_size
    except OSError:
        return None


def _too_large(total: int, scope: LocalScope) -> NoReturn:
    raise SyncTooLargeError(
        f"sync configuration too large: {total} bytes over the 50 MiB limit (roots: {sorted(scope.roots)})"
    )


def scan(model: Model) -> tuple[LocalScope, Side, int]:
    """Return ``(scope, fingerprints, total_bytes)`` for a sync-enabled model.

    Only regular files and symlinks are fingerprinted; symlinks are not followed.
    Raises SyncTooLargeError past 50 MiB of regular-file content.
    """
    compressor = TarCompressor(model)
    root_paths = {arcname: path for path, arcname in compressor.declared_paths()}
    scope = LocalScope(frozenset(root_paths), root_paths, compressor.is_excluded)

    fingerprints: Side = {}
    total = 0
    for path, arcname in compressor.iter_files():
        # Refuse on the size alone, before reading a huge file.
        try:
            declared = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(declared.st_mode) and total + declared.st_size > MAX_SYNC_BYTES:
            _too_large(total + declared.st_size, scope)
        found = _fingerprint(compressor, path)
        if found is None:
            continue
        fingerprint, size = found
        total += size
        if total > MAX_SYNC_BYTES:
            _too_large(total, scope)
        fingerprints[arcname] = fingerprint
    return scope, fingerprints, total
