"""Module providing a tar decompressor that extracts files to their original directories"""

from __future__ import annotations

import json
import os
import tarfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from config_saver.lib.errors import ArchiveError, UnsafeArchiveError
from config_saver.lib.tar_compressor.tar_compressor import (
    HOME_CONTENT_PLACEHOLDER,
    METADATA_MEMBER,
)

# Mode bits that are never restored from an archive.
_FORBIDDEN_MODE_BITS = 0o6000  # setuid / setgid


def _no_emit(_message: str) -> None:
    """Discard progress messages when no progress bar is attached."""


@dataclass
class DecompressResult:
    """Structured outcome of an extraction run."""

    tar_path: str
    output_dir: str | None = None
    extracted: int = 0
    normalized_content: bool = False
    warnings: list[str] = field(default_factory=list)


class TarDecompressor:
    """Class representing a tar decompressor.

    Extraction is containment-checked: a member may never be written outside the
    extraction root, and link members may not point outside it either
    (CVE-2007-4559 / "zip slip").
    """

    def __init__(self, tar_path: str, output_dir: str | None = None, show_progress: bool = False):
        self.tar_path = tar_path
        self.output_dir = output_dir
        self.show_progress = show_progress
        # Get current user's home directory for path denormalization
        self.user_home = os.path.expanduser("~")

    # ------------------------------------------------------------------ paths

    def _denormalize_path(self, archived_path: str) -> str:
        """Map an archive member name back to the path it must be restored to."""
        prefix = os.path.join("home", "user")
        if archived_path == prefix:
            return self.user_home
        if archived_path.startswith(prefix + "/") or archived_path.startswith(prefix + "\\"):
            relative_part = archived_path[len(prefix) + 1 :]
            return os.path.join(self.user_home, relative_part)

        # For other paths, treat as absolute (with leading /)
        return os.path.join(os.sep, archived_path.lstrip(os.sep))

    def _extraction_root(self, member_name: str) -> str:
        """Return the directory a member is allowed to write inside."""
        if self.output_dir:
            return os.path.realpath(self.output_dir)
        # Restore-in-place mode intentionally targets absolute locations; the
        # home-relative members are still confined to the user's home.
        if member_name.startswith(os.path.join("home", "user")):
            return os.path.realpath(self.user_home)
        return os.sep

    def _destination(self, member_name: str) -> str:
        """Return the absolute destination path for a member name."""
        if self.output_dir:
            return os.path.join(os.path.realpath(self.output_dir), member_name)
        return self._denormalize_path(member_name)

    @staticmethod
    def _is_within(path: str, root: str) -> bool:
        """True when ``path`` is ``root`` itself or lives under it."""
        if root == os.sep:
            return True
        path = os.path.normpath(path)
        root = os.path.normpath(root)
        return path == root or path.startswith(root.rstrip(os.sep) + os.sep)

    # ----------------------------------------------------------- member checks

    def _validate_member(self, member: tarfile.TarInfo) -> str:
        """Validate a member and return its checked absolute destination.

        Raises UnsafeArchiveError for anything that could write outside the
        extraction root or materialise a device node.
        """
        name = member.name
        if not name or name in (".", "/"):
            raise UnsafeArchiveError(name, "empty member name")
        if os.path.isabs(name) or name.startswith(("/", "\\")):
            raise UnsafeArchiveError(name, "absolute member names are not allowed")
        parts = name.replace("\\", "/").split("/")
        if os.pardir in parts:
            raise UnsafeArchiveError(name, "path traversal ('..') is not allowed")
        if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
            raise UnsafeArchiveError(name, "device and fifo members are not allowed")

        root = self._extraction_root(name)
        destination = self._destination(name)
        if not self._is_within(destination, root):
            raise UnsafeArchiveError(name, f"resolves outside the extraction root ({root})")

        # An existing symlink in the destination path would redirect the write.
        parent = os.path.dirname(destination)
        if os.path.exists(parent) and not self._is_within(os.path.realpath(parent), root):
            raise UnsafeArchiveError(name, "parent directory escapes the extraction root via a symlink")

        if member.issym() or member.islnk():
            self._validate_link(member, destination, root)

        return destination

    def _validate_link(self, member: tarfile.TarInfo, destination: str, root: str) -> None:
        """Reject links whose target escapes the extraction root."""
        link_target = member.linkname
        if member.islnk():
            # Hard link targets are member names, i.e. relative to the archive root.
            if os.path.isabs(link_target) or os.pardir in link_target.replace("\\", "/").split("/"):
                raise UnsafeArchiveError(member.name, "hard link target escapes the archive")
            resolved = self._destination(link_target)
        else:
            if os.path.isabs(link_target):
                resolved = link_target
            else:
                resolved = os.path.normpath(os.path.join(os.path.dirname(destination), link_target))
        if not self._is_within(resolved, root):
            raise UnsafeArchiveError(member.name, f"link target '{link_target}' escapes the extraction root")

    # -------------------------------------------------------------- contents

    def _is_text_file_content(self, content: bytes) -> bool:
        """Check if content is likely text (not binary)"""
        if b"\0" in content[:8192]:
            return False
        try:
            content[:8192].decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False

    def _denormalize_file_content(self, content: bytes) -> bytes:
        """Replace HOME_CONTENT_PLACEHOLDER with actual user home in file content"""
        if not self._is_text_file_content(content):
            return content

        for encoding in ("utf-8", "latin-1"):
            try:
                text_content = content.decode(encoding)
            except UnicodeDecodeError:
                continue
            if HOME_CONTENT_PLACEHOLDER in text_content:
                return text_content.replace(HOME_CONTENT_PLACEHOLDER, self.user_home).encode(encoding)
            return content
        return content

    @staticmethod
    def _read_metadata(tar: tarfile.TarFile) -> dict[str, object] | None:
        """Return the archive metadata, or None for archives written before it existed."""
        try:
            member = tar.getmember(METADATA_MEMBER)
        except KeyError:
            return None
        handle = tar.extractfile(member)
        if handle is None:
            return None
        try:
            data = json.loads(handle.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # -------------------------------------------------------------- extraction

    def _write_file(
        self,
        tar: tarfile.TarFile,
        member: tarfile.TarInfo,
        destination: str,
        denormalize: bool,
    ) -> None:
        handle = tar.extractfile(member)
        if handle is None:
            return
        content = handle.read()
        if denormalize:
            content = self._denormalize_file_content(content)

        os.makedirs(os.path.dirname(destination) or os.sep, exist_ok=True)
        if os.path.islink(destination):
            raise UnsafeArchiveError(member.name, "destination already exists as a symlink")
        mode = member.mode & ~_FORBIDDEN_MODE_BITS
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        os.chmod(destination, mode)

    def _write_other(self, member: tarfile.TarInfo, destination: str) -> None:
        if member.isdir():
            os.makedirs(destination, exist_ok=True)
            os.chmod(destination, member.mode & ~_FORBIDDEN_MODE_BITS)
            return

        os.makedirs(os.path.dirname(destination) or os.sep, exist_ok=True)
        if member.issym():
            if os.path.lexists(destination):
                os.unlink(destination)
            os.symlink(member.linkname, destination)
            return
        if member.islnk():
            source = self._destination(member.linkname)
            if os.path.lexists(destination):
                os.unlink(destination)
            os.link(source, destination)

    def decompress(self) -> DecompressResult:
        """Extract every archive member to its destination.

        Raises FileNotFoundError, ArchiveError or UnsafeArchiveError instead of
        printing and returning, so the CLI can map failures to exit codes.
        """
        if not os.path.exists(self.tar_path):
            raise FileNotFoundError(2, "Tar file does not exist", self.tar_path)

        result = DecompressResult(tar_path=self.tar_path, output_dir=self.output_dir)

        if self.output_dir:
            # Create it up front: an archive with no extractable member would
            # otherwise report success into a directory that does not exist.
            os.makedirs(self.output_dir, exist_ok=True)

        emit: Callable[[str], None] = _no_emit
        try:
            with tarfile.open(self.tar_path, "r:gz") as tar:
                metadata = self._read_metadata(tar)
                # Archives written before the metadata member existed are assumed
                # to have been normalized, which is the historical behaviour.
                denormalize = True if metadata is None else bool(metadata.get("normalize_content", True))
                result.normalized_content = denormalize

                members = [m for m in tar.getmembers() if m.name != METADATA_MEMBER]
                iterator: Iterable[tarfile.TarInfo] = members
                if self.show_progress:
                    from tqdm import (
                        tqdm,  # imported lazily: only needed for the progress bar
                    )

                    iterator = tqdm(members, desc="Extracting files", unit="file")
                    emit = tqdm.write

                for member in iterator:
                    destination = self._validate_member(member)
                    emit(f"Extracting: {member.name} -> {destination}")
                    if member.isfile():
                        self._write_file(tar, member, destination, denormalize)
                    else:
                        self._write_other(member, destination)
                    result.extracted += 1
        except tarfile.TarError as e:
            raise ArchiveError(f"Could not read archive '{self.tar_path}': {e}") from e

        return result
