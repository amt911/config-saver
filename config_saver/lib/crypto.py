"""Optional archive encryption, delegated to `age` or `gpg`.

gzip is compression, not confidentiality: an archive built from a config that
lists `~/.ssh` or `~/.config/rclone` holds those secrets in the clear. This
module wraps the two tools people already have for the job instead of inventing
crypto: it shells out, checks the exit status, and never leaves a plaintext
temporary file behind.

The encryption step runs on the finished archive, so the archive format itself
is unchanged: `age -d file.tar.gz.age > file.tar.gz` (or `gpg -d`) recovers
exactly the file config-saver would have written without encryption.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from config_saver.lib.errors import EncryptionError
from config_saver.lib.models.encryption_model import EncryptionModel

# Suffix appended to the archive name, and the binary that produces it.
SUFFIXES: dict[str, str] = {"age": ".age", "gpg": ".gpg"}
BINARIES: dict[str, str] = {"age": "age", "gpg": "gpg"}

# Encrypted archives are still secrets at rest (metadata, size); keep them private.
_FILE_MODE = 0o600


@dataclass(frozen=True)
class EncryptionInfo:
    """What was done to an archive, for the CLI to report."""

    method: str
    recipients: tuple[str, ...]


def suffix_for(method: str) -> str:
    """Return the file suffix an encrypted archive carries."""
    try:
        return SUFFIXES[method]
    except KeyError:
        raise EncryptionError(f"Unknown encryption method '{method}'.") from None


def detect_method(path: str) -> str | None:
    """Return the encryption method an archive path implies, if any."""
    for method, suffix in SUFFIXES.items():
        if path.endswith(suffix):
            return method
    return None


def require_binary(method: str) -> str:
    """Return the path to the backend binary, or explain what to install."""
    binary = BINARIES.get(method)
    if binary is None:
        raise EncryptionError(f"Unknown encryption method '{method}'.")
    resolved = shutil.which(binary)
    if resolved is None:
        raise EncryptionError(
            f"Encryption method '{method}' needs the '{binary}' binary, which is not on PATH. "
            f"Install it (Arch: `pacman -S {'age' if method == 'age' else 'gnupg'}`) or choose the other method."
        )
    return resolved


def _run(command: list[str], *, action: str) -> None:
    try:
        completed = subprocess.run(command, capture_output=True, check=False)
    except OSError as exc:  # binary vanished between which() and run()
        raise EncryptionError(f"Could not {action}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip() or f"exit status {completed.returncode}"
        raise EncryptionError(f"Could not {action}: {detail}")


def encrypt_file(source: str, destination: str, encryption: EncryptionModel) -> EncryptionInfo:
    """Encrypt `source` into `destination` for the configured recipients."""
    binary = require_binary(encryption.method)

    if encryption.method == "age":
        command = [binary]
        for recipient in encryption.recipients:
            command += ["-r", recipient]
        command += ["-o", destination, source]
    else:
        command = [binary, "--batch", "--yes", "--trust-model", "always"]
        for recipient in encryption.recipients:
            command += ["--recipient", recipient]
        command += ["--output", destination, "--encrypt", source]

    _run(command, action=f"encrypt the archive with {encryption.method}")
    try:
        os.chmod(destination, _FILE_MODE)
    except OSError:
        pass
    return EncryptionInfo(method=encryption.method, recipients=tuple(encryption.recipients))


def decrypt_file(source: str, destination: str, *, method: str, identity: str | None = None) -> None:
    """Decrypt `source` into `destination`.

    `age` needs an identity file; `gpg` uses the agent and its own keyring, so an
    identity is optional there.
    """
    binary = require_binary(method)

    if method == "age":
        if not identity:
            raise EncryptionError("Decrypting an age archive needs an identity file: pass --identity <key file>.")
        if not os.path.isfile(identity):
            raise EncryptionError(f"Identity file not found: {identity}")
        command = [binary, "-d", "-i", identity, "-o", destination, source]
    else:
        command = [binary, "--batch", "--yes"]
        if identity:
            command += ["--keyring", identity]
        command += ["--output", destination, "--decrypt", source]

    _run(command, action=f"decrypt the archive with {method}")
    try:
        os.chmod(destination, _FILE_MODE)
    except OSError:
        pass
