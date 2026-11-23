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
    if not text.strip():
        return SyncState()
    try:
        data = json.loads(text)
        heads = data["heads"]
        base = data["base"]
        if not isinstance(heads, dict) or not isinstance(base, dict):
            raise TypeError("heads and base must be objects")
        return SyncState(
            heads={str(k): str(v) for k, v in heads.items()},
            base={
                cfg: {p: Fingerprint(fp["sha256"], int(fp["mode"]), fp.get("link")) for p, fp in side.items()}
                for cfg, side in base.items()
            },
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise _corrupt(path, f"{type(exc).__name__}: {exc}") from exc


def save(path: str, state: SyncState) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    payload = {
        "heads": state.heads,
        "base": {
            cfg: {p: {"sha256": fp.sha256, "mode": fp.mode, "link": fp.link} for p, fp in side.items()}
            for cfg, side in state.base.items()
        },
    }
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".sync-state.")  # mkstemp creates 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
