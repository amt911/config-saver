"""Retention for tar-mode backups: prune ``<cfg>/<YYYYMMDD-HHMMSS>/`` directories."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
from collections.abc import Iterable
from datetime import datetime

from . import crypto
from .models.retention_model import RetentionModel
from .retention import keep_set

TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
_TIMESTAMP_DIR = re.compile(r"\d{8}-\d{6}")

# Archive names, plain and encrypted.
ARCHIVE_GLOBS = ("*.tar.gz", *(f"*.tar.gz{suffix}" for suffix in crypto.SUFFIXES.values()))


def _has_archive(directory: str) -> bool:
    """A timestamp directory is a backup only if it holds one; a failed run leaves it empty."""
    try:
        names = os.listdir(directory)
    except OSError:
        return False
    return any(fnmatch.fnmatch(name, pattern) for name in names for pattern in ARCHIVE_GLOBS)


def _timestamp_dirs(cfg_dir: str) -> dict[datetime, str]:
    """Real subdirectories named like a batch timestamp. Everything else is not ours."""
    found: dict[datetime, str] = {}
    try:
        names = os.listdir(cfg_dir)
    except FileNotFoundError:
        return found
    for name in names:
        if not _TIMESTAMP_DIR.fullmatch(name):
            continue
        path = os.path.join(cfg_dir, name)
        if os.path.islink(path) or not os.path.isdir(path) or not _has_archive(path):
            continue
        try:
            found[datetime.strptime(name, TIMESTAMP_FORMAT)] = path
        except ValueError:
            continue
    return found


def has_archives(cfg_dir: str) -> bool:
    """True while ``cfg_dir`` still holds at least one backup timestamp directory with an archive."""
    return bool(_timestamp_dirs(cfg_dir))


def prune_config_dir(
    cfg_dir: str, policy: RetentionModel, dry_run: bool, also_keep: Iterable[datetime] = ()
) -> list[str]:
    """Remove the timestamp directories ``policy`` does not keep; return their paths.

    The newest is always kept, and so is everything in ``also_keep`` (the backup
    this run just made, even when clock skew leaves an older-looking one newer).
    Directories without an archive are not backups: never counted, never removed. Legacy archives, files, symlinks and any other
    entry in ``cfg_dir`` are never candidates.
    """
    candidates = _timestamp_dirs(cfg_dir)
    keep = keep_set(candidates, policy) | set(also_keep)
    doomed = sorted(path for stamp, path in candidates.items() if stamp not in keep)
    if not dry_run:
        for path in doomed:
            shutil.rmtree(path)
    return doomed
