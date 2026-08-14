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

    with pytest.raises(UnsafeArchiveError, match="traversal") as excinfo:
        TarDecompressor(str(archive), str(out)).decompress()
    assert excinfo.value.member_name == "../../escaped.txt"
    assert not (tmp_path / "out" / "escaped.txt").exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_absolute_member_name_is_refused(tmp_path: Path, fake_home: Path) -> None:
    archive = _archive(tmp_path, lambda tar: _add_file(tar, "/tmp/config-saver-absolute-escape.txt"))
    out = tmp_path / "out"
    out.mkdir()

    with pytest.raises(UnsafeArchiveError, match="absolute") as excinfo:
        TarDecompressor(str(archive), str(out)).decompress()
    assert excinfo.value.member_name.endswith("config-saver-absolute-escape.txt")
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

    with pytest.raises(UnsafeArchiveError, match="link target") as excinfo:
        TarDecompressor(str(archive), str(out)).decompress()
    assert excinfo.value.member_name == "link"
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
    with pytest.raises(UnsafeArchiveError, match="hard link") as excinfo:
        TarDecompressor(str(archive), str(out)).decompress()
    assert excinfo.value.member_name == "hard"


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


@pytest.mark.parametrize(
    "member_type",
    [tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE],
    ids=["character-device", "block-device", "fifo"],
)
def test_every_special_member_type_is_refused(tmp_path: Path, fake_home: Path, member_type: bytes) -> None:
    def build(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("special")
        info.type = member_type
        tar.addfile(info)

    archive = _archive(tmp_path, build)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(UnsafeArchiveError, match="device") as excinfo:
        TarDecompressor(str(archive), str(out)).decompress()
    assert excinfo.value.member_name == "special"
    assert not (out / "special").exists()


@pytest.mark.parametrize("name", ["/etc/passwd", "\\windows\\system32", "//double", "\\"])
def test_absolute_or_rooted_member_names_are_refused(tmp_path: Path, fake_home: Path, name: str) -> None:
    """Both POSIX and Windows-style roots: a member name may never start at a root."""
    archive = _archive(tmp_path, lambda tar: _add_file(tar, name))
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(UnsafeArchiveError, match="absolute"):
        TarDecompressor(str(archive), str(out)).decompress()


def test_restore_in_place_writes_absolute_members(tmp_path: Path, fake_home: Path) -> None:
    """Restore without --output targets absolute paths outside the home; that is
    the /etc use case, and it must still work."""
    target = tmp_path / "system" / "conf.d"
    target.mkdir(parents=True)
    member_name = str(target / "app.conf").lstrip("/")

    archive = tmp_path / "abs.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        _add_file(tar, member_name, b"restored\n", mode=0o640)

    result = TarDecompressor(str(archive)).decompress()
    assert result.extracted == 1
    assert (target / "app.conf").read_bytes() == b"restored\n"
    assert (target / "app.conf").stat().st_mode & 0o777 == 0o640


def test_is_within_allows_everything_only_for_the_filesystem_root() -> None:
    """The root=/ shortcut is what makes restore-in-place possible; anything
    narrower must still confine."""
    decompressor = TarDecompressor("unused.tar.gz")
    assert decompressor._is_within("/anywhere/at/all", os.sep) is True
    assert decompressor._is_within("/other/place", "/root/dir") is False
    assert decompressor._is_within("/root/dir", "/root/dir") is True
    assert decompressor._is_within("/root/dir/child", "/root/dir/") is True
    assert decompressor._is_within("/root/dirsibling", "/root/dir") is False


def test_member_landing_in_a_symlinked_directory_is_refused(tmp_path: Path, fake_home: Path) -> None:
    """The destination's parent may not be a symlink out of the root, even when
    the archive itself contains no link member."""
    outside = tmp_path / "outside"
    outside.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    (out / "linkdir").symlink_to(outside)

    archive = _archive(tmp_path, lambda tar: _add_file(tar, "linkdir/payload.txt"))
    with pytest.raises(UnsafeArchiveError, match="parent directory escapes") as excinfo:
        TarDecompressor(str(archive), str(out)).decompress()
    assert excinfo.value.member_name == "linkdir/payload.txt"
    assert not (outside / "payload.txt").exists()


def test_home_lookalike_member_is_refused_in_restore_in_place_mode(tmp_path: Path, fake_home: Path) -> None:
    """`home/username/...` claims the home extraction root but denormalizes
    somewhere else entirely; it must not be written."""
    archive = _archive(tmp_path, lambda tar: _add_file(tar, "home/userland/evil.txt"))
    with pytest.raises(UnsafeArchiveError, match="resolves outside the extraction root") as excinfo:
        TarDecompressor(str(archive)).decompress()
    assert excinfo.value.member_name == "home/userland/evil.txt"
    assert not Path("/home/userland/evil.txt").exists()
