"""The restic-backed snapshot store: the only module that talks to restic.

One repository per ``saves_dir`` holds every config's snapshots (tagged
``config-saver`` and ``cfg:<name>``), so data shared between configs is stored
once. The repository password is random, encrypted to an age recipient at init
and handed to restic through ``RESTIC_PASSWORD_COMMAND``: it never appears on
argv and is never written decrypted to disk.

Instances hold plain attributes only, so they can be pickled to worker processes.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import IO

from .errors import StoreError
from .models.retention_model import RetentionModel
from .retention import keep_set
from .tar_compressor.tar_compressor import CompressResult

REPO_DIRNAME = "restic"
PASSWORD_FILENAME = "restic-password.age"
SETTINGS_FILENAME = "restic-store.json"
DEFAULT_IDENTITY = "~/.config/age/key.txt"
BASE_TAG = "config-saver"
CFG_TAG_PREFIX = "cfg:"
DESC_TAG_PREFIX = "desc:"
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_PRIVATE_FILE = 0o600
_PRIVATE_DIR = 0o700


@dataclass(frozen=True)
class Snapshot:
    """One stored backup. ``time`` is a naive local datetime."""

    id: str
    short_id: str
    time: datetime
    cfg: str
    description: str | None


class StorageMode(Enum):
    TAR_NO_RESTIC = "tar-no-restic"
    TAR_NOT_INITIALISED = "tar-not-initialised"
    RESTIC = "restic"


NOTICES: dict[StorageMode, str] = {
    StorageMode.TAR_NO_RESTIC: "restic is not installed: backups are stored as full archives. "
    "Install restic (Arch: `pacman -S restic`) to deduplicate them.",
    StorageMode.TAR_NOT_INITIALISED: "restic is available but the store is not set up: backups are stored as full "
    "archives. Run `config-saver --restic-init --encrypt-to <age key>` to deduplicate them.",
}


def repo_path(saves_dir: str) -> str:
    return os.path.join(saves_dir, REPO_DIRNAME)


def password_path(saves_dir: str) -> str:
    return os.path.join(saves_dir, PASSWORD_FILENAME)


def settings_path(saves_dir: str) -> str:
    return os.path.join(saves_dir, SETTINGS_FILENAME)


def select_mode(saves_dir: str) -> StorageMode:
    """Decide how backups are stored in ``saves_dir``."""
    if os.path.isdir(repo_path(saves_dir)) and os.path.isfile(password_path(saves_dir)):
        for binary in ("restic", "age"):
            if shutil.which(binary) is None:
                raise StoreError(
                    f"The restic store in {saves_dir} needs the '{binary}' binary, which is not on PATH. Install it."
                )
        return StorageMode.RESTIC
    if shutil.which("restic") is not None:
        return StorageMode.TAR_NOT_INITIALISED
    return StorageMode.TAR_NO_RESTIC


def encode_description(text: str) -> str:
    """Tag-safe form of a free-text description (no commas, no padding)."""
    return DESC_TAG_PREFIX + base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def decode_description(tag: str) -> str | None:
    payload = tag[len(DESC_TAG_PREFIX) :]
    try:
        return base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True).decode("utf-8")
    except ValueError:  # includes UnicodeDecodeError and binascii.Error
        return None


_FRACTION = re.compile(r"\.(\d+)")


def parse_restic_time(text: str) -> datetime:
    """Parse restic's RFC3339 time (possibly nanoseconds) to a naive local datetime."""
    text = text.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Python 3.10 only accepts 3 or 6 fractional digits: normalise to 6.
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    return datetime.fromisoformat(text).astimezone().replace(tzinfo=None)


def _drain(stream: IO[bytes]) -> tuple[threading.Thread, list[bytes]]:
    """Read ``stream`` to EOF in a thread so the child can never block on a full pipe."""
    chunks: list[bytes] = []

    def pump() -> None:
        chunks.append(stream.read())

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    return thread, chunks


class ResticStore:
    """Thin, picklable wrapper over one restic repository."""

    def __init__(self, saves_dir: str, identity: str | None = None):
        self.saves_dir = saves_dir
        self.repo = repo_path(saves_dir)
        self.password_file = password_path(saves_dir)
        self.settings_file = settings_path(saves_dir)
        self.identity = identity or self._recorded_identity() or os.path.expanduser(DEFAULT_IDENTITY)

    def _recorded_identity(self) -> str | None:
        try:
            with open(self.settings_file, encoding="utf-8") as handle:
                value = json.load(handle).get("identity")
        except (OSError, ValueError, AttributeError):
            return None
        return value if isinstance(value, str) and value else None

    # ----------------------------------------------------------------- plumbing

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_PASSWORD")}
        env["RESTIC_REPOSITORY"] = self.repo
        env["RESTIC_PASSWORD_COMMAND"] = f"age -d -i {shlex.quote(self.identity)} {shlex.quote(self.password_file)}"
        return env

    def _explain(self, stderr: bytes, returncode: int) -> StoreError:
        text = stderr.decode("utf-8", "replace").strip()
        lowered = text.lower()
        if any(m in lowered for m in ("password command", "restic_password_command", "resolving password failed")):
            return StoreError(f"cannot decrypt the store password with {self.identity}")
        return StoreError(f"restic failed: {text or f'exit status {returncode}'}")

    def _run(self, args: list[str]) -> bytes:
        try:
            done = subprocess.run(["restic", *args], env=self._env(), capture_output=True, check=False)
        except OSError as exc:
            raise StoreError(f"could not run restic: {exc}") from exc
        if done.returncode != 0:
            raise self._explain(done.stderr, done.returncode)
        return bytes(done.stdout)

    @staticmethod
    def _check_cfg(cfg: str) -> None:
        if "," in cfg:
            raise StoreError(f"config name '{cfg}' contains a comma, which restic tags cannot carry")

    # ------------------------------------------------------------------- verbs

    def init(self, recipient: str) -> None:
        """Create the repository and its age-encrypted random password."""
        if os.path.exists(self.repo) or os.path.exists(self.password_file):
            raise StoreError(f"a restic store already exists in {self.saves_dir}; refusing to overwrite it")
        os.makedirs(self.saves_dir, exist_ok=True)
        password = secrets.token_urlsafe(32)
        try:
            done = subprocess.run(
                ["age", "-r", recipient, "-o", self.password_file],
                input=password.encode("utf-8"),
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise StoreError(f"could not run age: {exc}") from exc
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", "replace").strip() or f"exit status {done.returncode}"
            raise StoreError(f"could not encrypt the store password: {detail}")
        os.chmod(self.password_file, _PRIVATE_FILE)
        os.makedirs(self.repo, mode=_PRIVATE_DIR)
        os.chmod(self.repo, _PRIVATE_DIR)
        try:
            self._run(["init"])
        except StoreError:
            # Leave nothing half-made behind, or a retry would be refused.
            shutil.rmtree(self.repo, ignore_errors=True)
            try:
                os.remove(self.password_file)
            except OSError:
                pass
            raise
        fd = os.open(self.settings_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_FILE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"identity": self.identity}, handle)
        os.chmod(self.settings_file, _PRIVATE_FILE)

    def backup(
        self,
        write: Callable[[IO[bytes]], CompressResult],
        cfg: str,
        when: datetime,
        description: str | None,
    ) -> tuple[Snapshot, CompressResult, int]:
        """Stream ``write``'s tar into restic; return (snapshot, result, data_added)."""
        self._check_cfg(cfg)
        argv = [
            "restic",
            "backup",
            "--stdin",
            "--stdin-filename",
            f"{cfg}.tar",
            "--tag",
            BASE_TAG,
            "--tag",
            f"{CFG_TAG_PREFIX}{cfg}",
        ]
        if description:
            argv += ["--tag", encode_description(description)]
        argv += ["--time", when.strftime(_TIME_FORMAT), "--json"]
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env(),
            )
        except OSError as exc:
            raise StoreError(f"could not run restic: {exc}") from exc
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        out_thread, out_chunks = _drain(proc.stdout)
        err_thread, err_chunks = _drain(proc.stderr)

        def finish() -> int:
            returncode = proc.wait()
            out_thread.join()
            err_thread.join()
            return returncode

        try:
            result = write(proc.stdin)
            proc.stdin.close()
        except BrokenPipeError:
            returncode = finish()
            error = self._explain(b"".join(err_chunks), returncode)
            if returncode == 0:
                error = StoreError("restic stopped reading before the archive was complete")
            raise error from None
        except BaseException:
            # restic only commits a snapshot when stdin ends cleanly: killing it aborts.
            proc.kill()
            finish()
            try:
                proc.stdin.close()
            except OSError:
                pass
            raise
        returncode = finish()
        if returncode != 0:
            raise self._explain(b"".join(err_chunks), returncode)
        summary = self._summary(b"".join(out_chunks))
        snapshot_id = str(summary["snapshot_id"])
        added = int(str(summary.get("data_added", 0)))
        snapshot = Snapshot(
            id=snapshot_id,
            short_id=snapshot_id[:8],
            time=when.replace(microsecond=0),
            cfg=cfg,
            description=description or None,
        )
        return snapshot, result, added

    @staticmethod
    def _summary(stdout: bytes) -> dict[str, object]:
        for line in reversed(stdout.decode("utf-8", "replace").splitlines()):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("message_type") == "summary" and data.get("snapshot_id"):
                return data
        raise StoreError("restic finished without reporting a snapshot id")

    def snapshots(self, cfg: str | None = None) -> list[Snapshot]:
        """Snapshots of this tool, oldest first, optionally of one config."""
        tags = BASE_TAG
        if cfg is not None:
            self._check_cfg(cfg)
            tags += f",{CFG_TAG_PREFIX}{cfg}"
        raw = self._run(["snapshots", "--json", "--tag", tags])
        try:
            entries = json.loads(raw or b"[]")
        except ValueError as exc:
            raise StoreError(f"restic returned unreadable snapshot data: {exc}") from exc
        found: list[Snapshot] = []
        for entry in entries or []:
            tag_list = entry.get("tags") or []
            name = next((t[len(CFG_TAG_PREFIX) :] for t in tag_list if t.startswith(CFG_TAG_PREFIX)), None)
            if name is None:
                continue
            desc_tag = next((t for t in tag_list if t.startswith(DESC_TAG_PREFIX)), None)
            found.append(
                Snapshot(
                    id=entry["id"],
                    short_id=entry.get("short_id") or entry["id"][:8],
                    time=parse_restic_time(entry["time"]),
                    cfg=name,
                    description=decode_description(desc_tag) if desc_tag else None,
                )
            )
        return sorted(found, key=lambda s: (s.time, s.id))

    def dump(self, snapshot_id: str, cfg: str, out: IO[bytes]) -> None:
        """Write snapshot ``snapshot_id``'s tar of ``cfg`` to ``out``."""
        try:
            proc = subprocess.Popen(
                ["restic", "dump", snapshot_id, f"/{cfg}.tar"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env(),
            )
        except OSError as exc:
            raise StoreError(f"could not run restic: {exc}") from exc
        assert proc.stdout is not None and proc.stderr is not None
        err_thread, err_chunks = _drain(proc.stderr)
        try:
            shutil.copyfileobj(proc.stdout, out)
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        returncode = proc.wait()
        err_thread.join()
        if returncode != 0:
            raise self._explain(b"".join(err_chunks), returncode)

    def forget(self, cfg: str, policy: RetentionModel, dry_run: bool) -> list[Snapshot]:
        """Forget what ``policy`` does not keep (never the newest); return those."""
        snaps = self.snapshots(cfg)
        if not snaps:
            return []
        keep = keep_set((s.time for s in snaps), policy)
        newest = max(snaps, key=lambda s: (s.time, s.id))
        removed = [s for s in snaps if s.time not in keep and s.id != newest.id]
        if removed and not dry_run:
            self._run(["forget", *(s.id for s in removed)])
        return removed

    def prune(self) -> None:
        """Reclaim the space of forgotten snapshots."""
        self._run(["prune"])
