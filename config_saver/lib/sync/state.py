"""Per-machine sync state: last synced head and file fingerprints per config."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field

from config_saver.lib.errors import SyncStateError
from config_saver.lib.sync.classify import Fingerprint, Side


@dataclass
class SyncState:
    heads: dict[str, str] = field(default_factory=dict)
    base: dict[str, Side] = field(default_factory=dict)
    # cfg -> parent id of the recorded head ("" = none): lets a later run find the recorded head's
    # nearest surviving ancestor when that head lost a fork or was pruned. Absent in old files.
    parents: dict[str, str] = field(default_factory=dict)


def _corrupt(path: str, why: str) -> SyncStateError:
    return SyncStateError(
        f"Sync state file '{path}' is unreadable ({why}). Delete it to start over: "
        "the next sync then treats every differing file as a conflict."
    )


def load(path: str) -> SyncState:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return SyncState()
    except UnicodeDecodeError as exc:
        raise _corrupt(path, f"not valid UTF-8: {exc}") from exc
    if not text.strip():
        return SyncState()
    try:
        data = json.loads(text)
        heads = data["heads"]
        base = data["base"]
        if not isinstance(heads, dict) or not isinstance(base, dict):
            raise TypeError("heads and base must be objects")
        parents = data.get("parents", {})
        if not isinstance(parents, dict):
            raise TypeError("parents must be an object")
        return SyncState(
            parents={str(k): str(v) for k, v in parents.items()},
            heads={str(k): str(v) for k, v in heads.items()},
            base={
                cfg: {p: Fingerprint(fp["sha256"], int(fp["mode"]), fp.get("link")) for p, fp in side.items()}
                for cfg, side in base.items()
            },
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise _corrupt(path, f"{type(exc).__name__}: {exc}") from exc


def _fsync_dir(directory: str) -> None:
    """Make the rename durable; best effort, some filesystems refuse to open a directory."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def save(path: str, state: SyncState) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    payload = {
        "heads": state.heads,
        "parents": state.parents,
        "base": {
            cfg: {p: {"sha256": fp.sha256, "mode": fp.mode, "link": fp.link} for p, fp in side.items()}
            for cfg, side in state.base.items()
        },
    }
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".sync-state.")  # mkstemp creates 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())  # the rename must not outlive the data it points at
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        _fsync_dir(parent)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
