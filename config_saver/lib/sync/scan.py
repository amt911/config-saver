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

from config_saver.lib import paths
from config_saver.lib.errors import SyncSystemConfigError, SyncTooLargeError, SyncUnreadableError
from config_saver.lib.models.model import Model
from config_saver.lib.sync.classify import CacheEntry, Fingerprint, HashCache, Side
from config_saver.lib.tar_compressor.tar_compressor import TarCompressor

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


def _fingerprint(compressor: TarCompressor, path: str, problems: list[str]) -> tuple[Fingerprint, int] | None:
    """Fingerprint of one file or symlink, and its raw size.

    None when it vanished (that is a deletion). Any other failure is recorded in
    ``problems``: a file we cannot read is not a file that is gone.
    """
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
    except FileNotFoundError:
        return None
    except OSError as exc:
        problems.append(f"{path} ({exc.strerror or exc})")
        return None


def _too_large(total: int, scope: LocalScope, limit_mb: int) -> NoReturn:
    raise SyncTooLargeError(
        f"sync configuration too large: {total} bytes over the {limit_mb} MiB limit (roots: {sorted(scope.roots)})"
    )


def scan(model: Model, cache: HashCache | None = None, seen: set[str] | None = None) -> tuple[LocalScope, Side, int]:
    """Return ``(scope, fingerprints, total_bytes)`` for a sync-enabled model.

    Only regular files and symlinks are fingerprinted; symlinks are not followed.
    Raises SyncTooLargeError past ``model.sync_max_mb`` MiB of regular-file content.

    ``cache`` (updated in place) skips re-hashing a file whose size, mtime_ns, ctime_ns, inode
    and ``normalize_content`` all match the entry. Entries for paths this scan did not see are
    dropped, unless the caller passes ``seen`` to collect them across several scans and prune itself.
    """
    limit = model.sync_max_mb * 1024 * 1024
    compressor = TarCompressor(model)
    root_paths = {arcname: path for path, arcname in compressor.declared_paths()}
    scope = LocalScope(frozenset(root_paths), root_paths, compressor.is_excluded)

    for declared_path, _arc in compressor.declared_paths():
        norm = os.path.normpath(declared_path)
        if norm == paths.SYSTEM_CONFIG_DIR or norm.startswith(paths.SYSTEM_CONFIG_DIR + "/"):
            raise SyncSystemConfigError(
                f"a sync configuration cannot declare '{declared_path}': {paths.SYSTEM_CONFIG_DIR} is "
                "system policy and is never restored, so it would read as a deletion on the next run"
            )

    problems: list[str] = []
    fingerprints: Side = {}
    total = 0
    visited: set[str] = set()
    for path, arcname in compressor.iter_files(problems):
        # Refuse on the size alone, before reading a huge file.
        try:
            declared = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            problems.append(f"{path} ({exc.strerror or exc})")
            continue
        if stat.S_ISREG(declared.st_mode) and total + declared.st_size > limit:
            _too_large(total + declared.st_size, scope, model.sync_max_mb)
        hit = cache.get(path) if cache is not None else None
        if (
            hit is not None
            and hit.size == declared.st_size
            and hit.mtime_ns == declared.st_mtime_ns
            and hit.ctime_ns == declared.st_ctime_ns
            and hit.inode == declared.st_ino
            and hit.normalize_content == model.normalize_content
        ):
            fingerprint, size = hit.fingerprint, (declared.st_size if stat.S_ISREG(declared.st_mode) else 0)
        else:
            found = _fingerprint(compressor, path, problems)
            if found is None:
                continue
            fingerprint, size = found
            if cache is not None:
                # The stat is from before the read: a write racing the hash makes the next scan miss.
                cache[path] = CacheEntry(
                    declared.st_size, declared.st_mtime_ns, declared.st_ctime_ns, declared.st_ino,
                    model.normalize_content, fingerprint,
                )  # fmt: skip
        visited.add(path)
        total += size
        if total > limit:
            _too_large(total, scope, model.sync_max_mb)
        fingerprints[arcname] = fingerprint
    if problems:
        raise SyncUnreadableError(
            "cannot sync: these paths exist but cannot be read, and publishing them as missing "
            "would delete them on every other machine. Fix their permissions or ownership: "
            + "; ".join(sorted(problems))
        )
    if cache is not None:
        if seen is None:
            for stale in set(cache) - visited:
                del cache[stale]
        else:
            seen |= visited
    return scope, fingerprints, total
