"""Typed exceptions shared by the library and mapped to CLI exit codes.

Control flow keys off these types, never off the wording of a message: rewording
(or translating) an error must not change what the program does.
"""

from __future__ import annotations


class ConfigSaverError(Exception):
    """Base class for every error raised on purpose by config-saver."""


class RootRequiredError(PermissionError, ConfigSaverError):
    """A configuration declares ``only_root_user: true`` but the process is not root."""

    def __init__(self, config_path: str):
        self.config_path = config_path
        super().__init__(
            f"Configuration '{config_path}' requires root privileges (only_root_user: true). "
            "Please run with sudo or as root user."
        )


class UnsafeArchiveError(RuntimeError, ConfigSaverError):
    """An archive member would be written outside the allowed extraction root."""

    def __init__(self, member_name: str, reason: str):
        self.member_name = member_name
        self.reason = reason
        super().__init__(f"Refusing to extract unsafe archive member '{member_name}': {reason}")


class EncryptionError(RuntimeError, ConfigSaverError):
    """The configured encryption backend is missing, or refused to run."""


class ArchiveError(RuntimeError, ConfigSaverError):
    """The archive is missing, truncated or otherwise unreadable."""


class IncompleteBackupError(ConfigSaverError):
    """Configured inputs were missing and the caller asked for strict mode."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(f"{len(missing)} configured path(s) were missing: " + ", ".join(missing))


class StoreError(RuntimeError, ConfigSaverError):
    """The restic-backed snapshot store is unusable, or restic/age refused to run."""


class SyncTooLargeError(ValueError, ConfigSaverError):
    """A sync-enabled configuration covers more data than a snapshot should carry."""


class SyncTransportError(OSError, ConfigSaverError):
    """git could not reach, update or publish to the sync remote."""


class SyncNotSetUpError(FileNotFoundError, ConfigSaverError):
    """Sync was not initialised on this machine, or git itself is missing."""


class SyncConflictError(ConfigSaverError):
    """Local and remote changed the same paths since the last sync."""

    def __init__(self, conflicts: dict[str, list[str]]):
        self.conflicts = conflicts
        total = sum(len(paths) for paths in conflicts.values())
        super().__init__(f"{total} path(s) changed on both sides since the last sync")


class SyncStateError(ValueError, ConfigSaverError):
    """The per-machine sync state file is unreadable."""
