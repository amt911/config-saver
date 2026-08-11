"""Module providing a tar compressor based on a YAML configuration with pydantic validation"""

from __future__ import annotations

import io
import json
import os
import tarfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

from config_saver import __version__
from config_saver.lib.models.model import Model

# Placeholder for user home directory in file contents
HOME_CONTENT_PLACEHOLDER = "<<<HOME_PLACEHOLDER>>>"

# Archive-level metadata member. It records whether contents were normalized so
# decompression does not have to guess (a file may legitimately contain the
# placeholder string).
METADATA_MEMBER = ".config-saver-metadata.json"
METADATA_FORMAT = 1

# Archives may contain secrets (ssh keys, cloud tokens): never rely on the umask.
ARCHIVE_MODE = 0o600


def _no_emit(_message: str) -> None:
    """Discard progress messages when no progress bar is attached."""


@dataclass
class CompressResult:
    """Structured outcome of a compression run.

    The library returns this instead of printing: the CLI decides what to render
    and which exit code a partial backup deserves.
    """

    output_path: str
    added: int = 0
    skipped_root_owned: list[str] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True when every configured input made it into the archive."""
        return not self.missing_inputs and not self.skipped_root_owned


class TarCompressor:
    """Class representing a tar compressor"""

    def __init__(
        self,
        yaml_data: Model,
        output_path: str = "output.tar.gz",
        base_dir: str | None = None,
        show_progress: bool = False,
    ):
        # yaml_data is expected to be a validated Model instance
        self.yaml_data = yaml_data
        self.output_path = output_path
        self.base_dir = base_dir or os.getcwd()
        self.show_progress = show_progress
        # Get current user's home directory for path normalization
        self.user_home = os.path.expanduser("~")
        # Get current user uid for filtering
        self.current_uid = os.getuid()

    def _is_root_owned(self, file_path: str) -> bool:
        """Check if a file is owned by root (uid=0 or gid=0)"""
        try:
            stat_info = os.lstat(file_path)
            return stat_info.st_uid == 0 or stat_info.st_gid == 0
        except OSError:
            return False

    def _normalize_path(self, file_path: str) -> str:
        """Normalize path by replacing user's home directory with 'home/user/' placeholder"""
        # Ensure we're working with absolute paths
        abs_path = os.path.abspath(file_path)

        # Only a real path *inside* the home directory is normalized. A plain
        # startswith() would also match a sibling home such as /home/andres2.
        if abs_path == self.user_home or abs_path.startswith(self.user_home.rstrip(os.sep) + os.sep):
            relative_to_home = os.path.relpath(abs_path, self.user_home)
            if relative_to_home == os.curdir:
                return os.path.join("home", "user")
            return os.path.join("home", "user", relative_to_home)

        # For paths outside home, keep them as is but remove leading slash
        arcname = os.path.normpath(abs_path)
        if arcname.startswith(os.sep):
            arcname = arcname[1:]
        return arcname

    def _is_text_file(self, file_path: str) -> bool:
        """Check if a file is likely a text file (not binary)"""
        # Known binary extensions (images, fonts, archives, etc.)
        binary_extensions = {
            # Images
            ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp", ".tiff", ".tif",
            # Fonts
            ".ttf", ".otf", ".woff", ".woff2", ".eot",
            # Archives
            ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
            # Executables and libraries
            ".so", ".a", ".o", ".pyc", ".pyo", ".exe", ".dll", ".dylib",
            # Databases
            ".db", ".sqlite", ".sqlite3",
            # Media
            ".mp3", ".mp4", ".avi", ".mkv", ".wav", ".flac", ".ogg",
            # Documents (binary formats)
            ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        }  # fmt: skip

        # Check extension first (fast path)
        _, ext = os.path.splitext(file_path.lower())
        if ext in binary_extensions:
            return False

        try:
            # Try to read first 8192 bytes and check for null bytes
            with open(file_path, "rb") as f:
                chunk = f.read(8192)
        except OSError:
            return False

        # Null bytes mean binary; otherwise anything that decodes as UTF-8 or as
        # latin-1 is treated as text (latin-1 is the fallback used when writing
        # the normalized content back, so it must be accepted here too).
        if b"\0" in chunk:
            return False
        try:
            chunk.decode("utf-8")
            return True
        except UnicodeDecodeError:
            pass
        try:
            chunk.decode("latin-1")
            return True
        except UnicodeDecodeError:
            return False

    def _normalize_file_content(self, file_path: str) -> bytes | None:
        """Read file content and replace user home paths with placeholder.

        Returns None when the file must be archived unchanged.
        """
        if os.path.islink(file_path) or not self._is_text_file(file_path):
            return None

        try:
            with open(file_path, "rb") as f:
                content = f.read()
        except OSError:
            return None

        for encoding in ("utf-8", "latin-1"):
            try:
                text_content = content.decode(encoding)
            except UnicodeDecodeError:
                continue
            if self.user_home in text_content:
                return text_content.replace(self.user_home, HOME_CONTENT_PLACEHOLDER).encode(encoding)
            return None  # decodes fine, nothing to replace
        return None

    def _iter_entries(self, result: CompressResult) -> Iterator[tuple[str, bool]]:
        """Yield (path, is_dir) for every configured input, deterministically.

        Missing inputs are recorded on ``result`` instead of being dropped: a
        backup that silently skips what it was asked to save is worse than one
        that fails.
        """

        def walk(root: str) -> Iterator[tuple[str, bool]]:
            # The directory itself is archived so empty directories survive a round-trip.
            yield root, True
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                filenames.sort()
                for d in dirnames:
                    yield os.path.join(dirpath, d), True
                for f in filenames:
                    yield os.path.join(dirpath, f), False

        for entry in self.yaml_data.directories:
            if isinstance(entry, str):
                if "${" in entry or not os.path.lexists(entry):
                    result.missing_inputs.append(entry)
                    continue
                if os.path.isdir(entry) and not os.path.islink(entry):
                    yield from walk(entry)
                else:
                    yield entry, False
                continue

            source = entry.source
            if "${" in source or not os.path.isdir(source):
                result.missing_inputs.append(source)
                continue
            for file in entry.files:
                file_path = os.path.join(source, file)
                if not os.path.lexists(file_path):
                    result.missing_inputs.append(file_path)
                    continue
                if os.path.isdir(file_path) and not os.path.islink(file_path):
                    yield from walk(file_path)
                else:
                    yield file_path, False

    def _collect(self, result: CompressResult) -> Iterator[tuple[str, str, bool]]:
        """Yield (path, arcname, is_dir) with duplicates removed, order preserved."""
        seen: set[str] = set()
        for path, is_dir in self._iter_entries(result):
            arcname = self._normalize_path(path)
            if arcname in seen:
                continue
            seen.add(arcname)
            yield path, arcname, is_dir

    def _write_metadata(self, tar: tarfile.TarFile) -> None:
        """Store how the archive was produced, so restore does not have to guess."""
        payload = json.dumps(
            {
                "format": METADATA_FORMAT,
                "tool_version": __version__,
                "normalize_content": bool(self.yaml_data.normalize_content),
            },
            indent=2,
        ).encode("utf-8")
        info = tarfile.TarInfo(METADATA_MEMBER)
        info.size = len(payload)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(payload))

    def compress(self) -> CompressResult:
        """Compress every configured file/directory into ``self.output_path``.

        The archive is written to a temporary file in the destination directory
        and moved into place with ``os.replace`` only after a clean close, so an
        interrupted run never leaves a truncated file that looks like a backup.
        """
        result = CompressResult(output_path=self.output_path)

        emit: Callable[[str], None] = _no_emit
        items: Iterable[tuple[str, str, bool]] = self._collect(result)
        if self.show_progress:
            from tqdm import tqdm  # imported lazily: only needed for the progress bar

            # Materialise the list only when a total is actually needed; in headless
            # mode the generator keeps startup latency and memory flat.
            items = tqdm(list(items), desc="Compressing files", unit="file")
            emit = tqdm.write

        out_dir = os.path.dirname(os.path.abspath(self.output_path)) or "."
        os.makedirs(out_dir, exist_ok=True)
        tmp_path = os.path.join(out_dir, f".{os.path.basename(self.output_path)}.{os.getpid()}.part")

        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, ARCHIVE_MODE)
            with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
                self._write_metadata(tar)
                for path, arcname, is_dir in items:
                    # Skip root-owned files if only_root_user is false and current user is not root
                    if not self.yaml_data.only_root_user and self.current_uid != 0 and self._is_root_owned(path):
                        emit(f"Skipping root-owned file (only_root_user=false): {path}")
                        result.skipped_root_owned.append(path)
                        continue

                    if is_dir:
                        tar.add(path, arcname=arcname, recursive=False)
                        result.added += 1
                        continue

                    normalized_content = None
                    if self.yaml_data.normalize_content:
                        normalized_content = self._normalize_file_content(path)

                    if normalized_content is not None:
                        emit(f"Compressing (normalized): {path} -> {arcname}")
                        tarinfo = tar.gettarinfo(path, arcname=arcname)
                        tarinfo.size = len(normalized_content)
                        tar.addfile(tarinfo, fileobj=io.BytesIO(normalized_content))
                    else:
                        emit(f"Compressing: {path} -> {arcname}")
                        tar.add(path, arcname=arcname)
                    result.added += 1
            os.chmod(tmp_path, ARCHIVE_MODE)
            os.replace(tmp_path, self.output_path)
        except BaseException:
            # Never leave a partial archive behind, whatever went wrong.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return result
