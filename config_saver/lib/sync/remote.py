"""The sync remote: where the restic repository lives (``sync.json``) and its derived password."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass

from ..errors import SyncNotSetUpError, SyncStateError
from ..restic_store import ResticStore

_KEY_PREFIX = "AGE-SECRET-KEY-"
_PASSWORD_DOMAIN = b"config-saver-sync-v1\0"
_PRIVATE_FILE = 0o600
_PRIVATE_DIR = 0o700
_KEYS = {"repository", "options"}


def derive_password(identity_path: str) -> str:
    """Lowercase hex sha256 of the domain tag plus the identity's ``AGE-SECRET-KEY-`` line.

    Every machine holding the same age identity derives the same restic password, so none is stored.
    """
    path = os.path.expanduser(identity_path)
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        raise SyncNotSetUpError(f"cannot read the age identity {path}: {exc}") from exc
    for line in lines:
        key = line.strip()
        if key.startswith(_KEY_PREFIX):
            return hashlib.sha256(_PASSWORD_DOMAIN + key.encode("utf-8")).hexdigest()
    raise SyncNotSetUpError(f"no {_KEY_PREFIX}... line in the age identity {path}")


@dataclass(frozen=True)
class SyncRemote:
    repository: str
    options: tuple[str, ...] = ()

    @property
    def needs_rclone(self) -> bool:
        return self.repository.startswith("rclone:")

    def store(self, identity_path: str) -> ResticStore:
        return ResticStore.for_repository(self.repository, derive_password(identity_path), self.options)


def load_remote(path: str) -> SyncRemote:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise SyncNotSetUpError(
            "sync is not set up: run config-saver --sync-init <restic repository, "
            f"e.g. rclone:Google Drive:config-saver-sync> (missing {path})"
        ) from exc
    except (OSError, ValueError) as exc:
        raise SyncStateError(f"the sync remote file {path} is unreadable: {exc}") from exc
    if not isinstance(data, dict) or set(data) != _KEYS:
        raise SyncStateError(f"the sync remote file {path} must hold exactly the keys {sorted(_KEYS)}")
    repository, options = data["repository"], data["options"]
    if not isinstance(repository, str) or not repository:
        raise SyncStateError(f"the sync remote file {path}: 'repository' must be a non-empty string")
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        raise SyncStateError(f"the sync remote file {path}: 'options' must be a list of strings")
    return SyncRemote(repository, tuple(options))


def save_remote(path: str, remote: SyncRemote) -> None:
    """Write ``sync.json`` atomically, 0600 in a 0700 directory."""
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=_PRIVATE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".sync.json.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"repository": remote.repository, "options": list(remote.options)}, handle)
        os.chmod(tmp, _PRIVATE_FILE)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
