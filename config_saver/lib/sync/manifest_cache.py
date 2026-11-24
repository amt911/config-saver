"""Manifest cache: snapshots are immutable, so a manifest read once never has to be dumped again.

Keyed by canonical snapshot id. The file is only ever a hint: anything malformed in it means an empty
cache (the manifests are then read from the repository), never an error and never a partial trust.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Collection, Mapping

from config_saver.lib.errors import ArchiveError
from config_saver.lib.sync.manifest import Manifest

_ID = re.compile(r"[0-9a-f]{64}")
_VERSION = 1


def load(path: str) -> dict[str, Manifest]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict) or set(doc) != {"version", "manifests"} or doc["version"] != _VERSION:
            return {}
        raw = doc["manifests"]
        if not isinstance(raw, dict):
            return {}
        out: dict[str, Manifest] = {}
        for snapshot_id, text in raw.items():
            if not _ID.fullmatch(snapshot_id) or not isinstance(text, str):
                return {}
            out[snapshot_id] = Manifest.from_json(text.encode())
        return out
    except (OSError, ValueError, ArchiveError):  # unreadable, not JSON / UTF-8, or a manifest that fails validation
        return {}


def save(path: str, manifests: Mapping[str, Manifest], listed: Collection[str]) -> None:
    """Write the entries whose snapshot is still in the listing; best effort (a cache is never worth a failure)."""
    kept = {i: m.to_json().decode() for i, m in manifests.items() if i in listed}
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".sync-manifests.")  # 0600
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": _VERSION, "manifests": kept}, fh, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            os.unlink(tmp)
