"""Where sync keeps its working tree and per-machine state (XDG-aware)."""

from __future__ import annotations

import os


def _xdg(var: str, default: str) -> str:
    return os.environ.get(var) or os.path.expanduser(default)


def sync_dir() -> str:
    return os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"), "config-saver", "sync")


def state_path() -> str:
    return os.path.join(_xdg("XDG_STATE_HOME", "~/.local/state"), "config-saver", "sync-state.json")
