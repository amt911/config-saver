"""Where sync keeps its working tree and per-machine state (XDG-aware)."""

from __future__ import annotations

import os


def _xdg(var: str, default: str) -> str:
    return os.environ.get(var) or os.path.expanduser(default)


def sync_dir() -> str:
    return os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"), "config-saver", "sync")


def state_path() -> str:
    return os.path.join(_xdg("XDG_STATE_HOME", "~/.local/state"), "config-saver", "sync-state.json")


def remote_config_path() -> str:
    return os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), "config-saver", "sync.json")


def lock_path() -> str:
    return os.path.join(_xdg("XDG_STATE_HOME", "~/.local/state"), "config-saver", "sync.lock")


def manifest_cache_path(state_file: str) -> str:
    """The manifest cache lives next to the sync state it belongs to."""
    return os.path.join(os.path.dirname(state_file), "sync-manifests.json")
