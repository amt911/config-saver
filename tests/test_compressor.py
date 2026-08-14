"""TarCompressor unit behaviour: paths, missing inputs, permissions, atomicity."""

from __future__ import annotations

import os
import stat
import tarfile
from pathlib import Path

import pytest

from config_saver.lib.tar_compressor.tar_compressor import METADATA_MEMBER, TarCompressor

from .conftest import archive_names, make_model


def _compressor(directories: list, out: Path, **kwargs) -> TarCompressor:
    return TarCompressor(make_model(directories, **kwargs), str(out))


@pytest.mark.parametrize(
    "relative",
    ["x", "nested/dir/file.txt", ".config/app.conf"],
)
def test_paths_inside_home_are_normalized(fake_home: Path, relative: str) -> None:
    compressor = TarCompressor(make_model([]), "unused.tar.gz")
    assert compressor._normalize_path(str(fake_home / relative)) == os.path.join("home", "user", relative)


def test_sibling_home_is_not_normalized(fake_home: Path) -> None:
    """/home/tester2 must not be rewritten as if it lived in /home/tester."""
    sibling = str(fake_home) + "2"
    compressor = TarCompressor(make_model([]), "unused.tar.gz")
    normalized = compressor._normalize_path(sibling + "/x")
    assert not normalized.startswith(os.path.join("home", "user"))
    assert normalized == (sibling + "/x").lstrip("/")


def test_home_itself_is_normalized(fake_home: Path) -> None:
    compressor = TarCompressor(make_model([]), "unused.tar.gz")
    assert compressor._normalize_path(str(fake_home)) == os.path.join("home", "user")


def test_paths_outside_home_keep_their_absolute_layout(fake_home: Path) -> None:
    compressor = TarCompressor(make_model([]), "unused.tar.gz")
    assert compressor._normalize_path("/etc/fstab") == "etc/fstab"


def test_missing_inputs_are_reported_not_skipped(tmp_path: Path, fake_home: Path) -> None:
    present = fake_home / "present.txt"
    present.write_text("x")
    out = tmp_path / "out.tar.gz"
    result = _compressor([str(present), str(fake_home / "gone.txt")], out).compress()
    assert result.missing_inputs == [str(fake_home / "gone.txt")]
    assert result.complete is False
    assert archive_names(out) == [os.path.join("home", "user", "present.txt")]


def test_missing_files_inside_a_specific_entry_are_reported(tmp_path: Path, fake_home: Path) -> None:
    src = fake_home / "cfg"
    src.mkdir()
    (src / "there").write_text("x")
    out = tmp_path / "out.tar.gz"
    result = _compressor([{"source": str(src), "files": ["there", "missing"]}], out).compress()
    assert result.missing_inputs == [str(src / "missing")]


def test_unexpanded_placeholder_counts_as_missing(tmp_path: Path, fake_home: Path) -> None:
    out = tmp_path / "out.tar.gz"
    raw = str(fake_home / '${BEGINS_WITH="nope"}')
    result = _compressor([raw], out).compress()
    assert result.missing_inputs == [raw]


def test_overlapping_inputs_are_deduplicated(tmp_path: Path, fake_home: Path) -> None:
    root = fake_home / "d"
    root.mkdir()
    (root / "f.txt").write_text("x")
    out = tmp_path / "out.tar.gz"
    result = _compressor([str(root), str(root / "f.txt"), str(root)], out).compress()
    names = archive_names(out)
    assert names.count(os.path.join("home", "user", "d", "f.txt")) == 1
    assert result.added == len(names)


def test_empty_directories_are_archived(tmp_path: Path, fake_home: Path) -> None:
    root = fake_home / "d"
    (root / "empty").mkdir(parents=True)
    out = tmp_path / "out.tar.gz"
    _compressor([str(root)], out).compress()
    assert os.path.join("home", "user", "d", "empty") in archive_names(out)


def test_archive_is_private(tmp_path: Path, fake_home: Path) -> None:
    """Archives hold ssh keys and cloud tokens: never rely on the umask."""
    (fake_home / "f").write_text("x")
    out = tmp_path / "out.tar.gz"
    _compressor([str(fake_home / "f")], out).compress()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_metadata_records_normalization(tmp_path: Path, fake_home: Path) -> None:
    (fake_home / "f").write_text("x")
    out = tmp_path / "out.tar.gz"
    _compressor([str(fake_home / "f")], out, normalize_content=True).compress()
    with tarfile.open(out, "r:gz") as tar:
        handle = tar.extractfile(METADATA_MEMBER)
        assert handle is not None
        assert b'"normalize_content": true' in handle.read()


def test_failure_leaves_no_archive_behind(tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash mid-write must not leave a file that looks like a valid backup."""
    (fake_home / "f").write_text("x")
    out = tmp_path / "out.tar.gz"

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tarfile.TarFile, "add", boom)
    with pytest.raises(OSError, match="disk full"):
        _compressor([str(fake_home / "f")], out).compress()

    assert not out.exists()
    assert list(tmp_path.glob(".*part")) == []


def test_root_owned_files_are_skipped_and_reported(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep = fake_home / "keep.txt"
    keep.write_text("x")
    rooted = fake_home / "rooted.txt"
    rooted.write_text("x")
    monkeypatch.setattr(TarCompressor, "_is_root_owned", lambda _self, path: path.endswith("rooted.txt"))

    out = tmp_path / "out.tar.gz"
    result = _compressor([str(keep), str(rooted)], out).compress()
    assert result.skipped_root_owned == [str(rooted)]
    assert archive_names(out) == [os.path.join("home", "user", "keep.txt")]


def test_binary_extensions_are_not_treated_as_text(tmp_path: Path, fake_home: Path) -> None:
    compressor = TarCompressor(make_model([]), "unused.tar.gz")
    png = fake_home / "img.png"
    png.write_bytes(b"text-looking but named .png")
    assert compressor._is_text_file(str(png)) is False


def test_latin1_content_is_treated_as_text(tmp_path: Path, fake_home: Path) -> None:
    """The latin-1 fallback in normalization is only reachable if detection allows it."""
    compressor = TarCompressor(make_model([]), "unused.tar.gz")
    f = fake_home / "l.txt"
    f.write_bytes("café".encode("latin-1"))
    assert compressor._is_text_file(str(f)) is True


def test_null_bytes_mean_binary(fake_home: Path) -> None:
    compressor = TarCompressor(make_model([]), "unused.tar.gz")
    f = fake_home / "b.dat"
    f.write_bytes(b"abc\0def")
    assert compressor._is_text_file(str(f)) is False


def test_symlinks_are_never_content_normalized(fake_home: Path) -> None:
    target = fake_home / "t.txt"
    target.write_text(f"path={fake_home}\n")
    link = fake_home / "l.txt"
    link.symlink_to(target)
    compressor = TarCompressor(make_model([], normalize_content=True), "unused.tar.gz")
    assert compressor._normalize_file_content(str(link)) is None


BINARY_EXTENSIONS = sorted(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp", ".tiff", ".tif",
        ".ttf", ".otf", ".woff", ".woff2", ".eot",
        ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
        ".so", ".a", ".o", ".pyc", ".pyo", ".exe", ".dll", ".dylib",
        ".db", ".sqlite", ".sqlite3",
        ".mp3", ".mp4", ".avi", ".mkv", ".wav", ".flac", ".ogg",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    }
)  # fmt: skip


@pytest.mark.parametrize("extension", BINARY_EXTENSIONS)
def test_every_binary_extension_is_excluded_from_normalization(fake_home: Path, extension: str) -> None:
    """The extension list is the contract: dropping one silently starts
    rewriting the contents of that file type."""
    compressor = TarCompressor(make_model([], normalize_content=True), "unused.tar.gz")
    target = fake_home / f"file{extension}"
    target.write_text(f"path={fake_home}\n", encoding="utf-8")
    assert compressor._is_text_file(str(target)) is False
    assert compressor._normalize_file_content(str(target)) is None


@pytest.mark.parametrize("extension", [".conf", ".txt", ".xml", ".json", ".yaml", ".sh", ""])
def test_configuration_like_extensions_are_text(fake_home: Path, extension: str) -> None:
    compressor = TarCompressor(make_model([]), "unused.tar.gz")
    target = fake_home / f"file{extension}"
    target.write_text("plain\n", encoding="utf-8")
    assert compressor._is_text_file(str(target)) is True


def test_extension_matching_is_case_insensitive(fake_home: Path) -> None:
    compressor = TarCompressor(make_model([]), "unused.tar.gz")
    target = fake_home / "IMAGE.PNG"
    target.write_text("not really a png", encoding="utf-8")
    assert compressor._is_text_file(str(target)) is False
