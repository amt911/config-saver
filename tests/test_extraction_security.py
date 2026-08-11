"""Regression tests for CVE-2007-4559 style extraction escapes.

Each test builds a hostile archive by hand and asserts extraction refuses it.
Before the containment checks landed, every one of these wrote outside the
requested destination.
"""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest

from config_saver.lib.errors import UnsafeArchiveError
from config_saver.lib.tar_compressor.tar_decompressor import TarDecompressor


def _archive(tmp_path: Path, build) -> Path:
    path = tmp_path / "evil.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        build(tar)
    return path


def _add_file(tar: tarfile.TarFile, name: str, data: bytes = b"PWNED", mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    tar.addfile(info, io.BytesIO(data))


def test_parent_traversal_is_refused(tmp_path: Path, fake_home: Path) -> None:
    archive = _archive(tmp_path, lambda tar: _add_file(tar, "../../escaped.txt"))
    out = tmp_path / "out" / "a" / "b"
    out.mkdir(parents=True)

    with pytest.raises(UnsafeArchiveError, match="traversal"):
        TarDecompressor(str(archive), str(out)).decompress()
    assert not (tmp_path / "out" / "escaped.txt").exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_absolute_member_name_is_refused(tmp_path: Path, fake_home: Path) -> None:
    archive = _archive(tmp_path, lambda tar: _add_file(tar, "/tmp/config-saver-absolute-escape.txt"))
    out = tmp_path / "out"
    out.mkdir()

    with pytest.raises(UnsafeArchiveError, match="absolute"):
        TarDecompressor(str(archive), str(out)).decompress()
    assert not Path("/tmp/config-saver-absolute-escape.txt").exists()


def test_symlink_escaping_the_root_is_refused(tmp_path: Path, fake_home: Path) -> None:
    def build(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../outside"
        tar.addfile(info)

    archive = _archive(tmp_path, build)
    out = tmp_path / "out" / "deep"
    out.mkdir(parents=True)

    with pytest.raises(UnsafeArchiveError, match="link target"):
        TarDecompressor(str(archive), str(out)).decompress()
    assert not (out / "link").exists()


def test_absolute_symlink_target_is_refused(tmp_path: Path, fake_home: Path) -> None:
    def build(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)

    archive = _archive(tmp_path, build)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(UnsafeArchiveError, match="link target"):
        TarDecompressor(str(archive), str(out)).decompress()


def test_write_through_a_symlink_member_is_refused(tmp_path: Path, fake_home: Path) -> None:
    """The classic two-step: create a symlink out of the root, then write through it."""
    target_dir = tmp_path / "target"
    target_dir.mkdir()

    def build(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("escape")
        info.type = tarfile.SYMTYPE
        info.linkname = str(target_dir)
        tar.addfile(info)
        _add_file(tar, "escape/payload.txt")

    archive = _archive(tmp_path, build)
    out = tmp_path / "out"
    out.mkdir()

    with pytest.raises(UnsafeArchiveError):
        TarDecompressor(str(archive), str(out)).decompress()
    assert not (target_dir / "payload.txt").exists()


def test_hardlink_escaping_the_archive_is_refused(tmp_path: Path, fake_home: Path) -> None:
    def build(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("hard")
        info.type = tarfile.LNKTYPE
        info.linkname = "../../../etc/passwd"
        tar.addfile(info)

    archive = _archive(tmp_path, build)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(UnsafeArchiveError, match="hard link"):
        TarDecompressor(str(archive), str(out)).decompress()


def test_device_nodes_are_refused(tmp_path: Path, fake_home: Path) -> None:
    def build(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("dev/null")
        info.type = tarfile.CHRTYPE
        info.devmajor = 1
        info.devminor = 3
        tar.addfile(info)

    archive = _archive(tmp_path, build)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(UnsafeArchiveError, match="device"):
        TarDecompressor(str(archive), str(out)).decompress()


def test_traversal_is_refused_in_absolute_restore_mode(tmp_path: Path, fake_home: Path) -> None:
    """Without --output the tool writes to absolute paths; that is exactly when a
    traversing member would be arbitrary file write."""
    archive = _archive(tmp_path, lambda tar: _add_file(tar, "home/user/../../../etc/pwned.txt"))
    with pytest.raises(UnsafeArchiveError, match="traversal"):
        TarDecompressor(str(archive)).decompress()


def test_setuid_bits_are_not_restored(tmp_path: Path, fake_home: Path) -> None:
    archive = _archive(tmp_path, lambda tar: _add_file(tar, "payload", mode=0o4755))
    out = tmp_path / "out"
    out.mkdir()
    TarDecompressor(str(archive), str(out)).decompress()
    assert not os.stat(out / "payload").st_mode & 0o6000


def test_missing_archive_raises_filenotfound(tmp_path: Path, fake_home: Path) -> None:
    with pytest.raises(FileNotFoundError):
        TarDecompressor(str(tmp_path / "nope.tar.gz")).decompress()


def test_corrupt_archive_raises_archive_error(tmp_path: Path, fake_home: Path) -> None:
    from config_saver.lib.errors import ArchiveError

    corrupt = tmp_path / "corrupt.tar.gz"
    corrupt.write_bytes(b"this is not a gzip stream")
    with pytest.raises(ArchiveError):
        TarDecompressor(str(corrupt)).decompress()


def test_output_directory_is_created_even_for_an_empty_archive(tmp_path: Path, fake_home: Path) -> None:
    """Reporting success into a directory that was never created is a lie."""
    archive = tmp_path / "empty.tar.gz"
    with tarfile.open(archive, "w:gz"):
        pass
    out = tmp_path / "out" / "nested"

    result = TarDecompressor(str(archive), str(out)).decompress()
    assert result.extracted == 0
    assert out.is_dir()
