"""Retention for tar-mode backups: prune ``<cfg>/<YYYYMMDD-HHMMSS>/`` directories."""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime

from .models.retention_model import RetentionModel
from .retention import keep_set

TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
_TIMESTAMP_DIR = re.compile(r"\d{8}-\d{6}")


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
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        try:
            found[datetime.strptime(name, TIMESTAMP_FORMAT)] = path
        except ValueError:
            continue
    return found


def prune_config_dir(cfg_dir: str, policy: RetentionModel, dry_run: bool) -> list[str]:
    """Remove the timestamp directories ``policy`` does not keep; return their paths.

    The newest is always kept. Legacy archives, files, symlinks and any other
    entry in ``cfg_dir`` are never candidates.
    """
    candidates = _timestamp_dirs(cfg_dir)
    keep = keep_set(candidates, policy)
    doomed = sorted(path for stamp, path in candidates.items() if stamp not in keep)
    if not dry_run:
        for path in doomed:
            shutil.rmtree(path)
    return doomed
