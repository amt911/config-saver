"""Round-trip invariant: compress -> decompress reproduces the tree.

Documented contract (see README "Round-trip semantics"): file contents, file
mode bits (minus setuid/setgid), symlinks and the directory structure —
including empty directories — survive a round trip. Ownership, mtimes and
xattrs are not preserved.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from config_saver.lib.tar_compressor.tar_compressor import HOME_CONTENT_PLACEHOLDER, TarCompressor
from config_saver.lib.tar_compressor.tar_decompressor import TarDecompressor

from .conftest import archive_member_bytes, make_model


def _roundtrip(tree: Path, tmp_path: Path, out_dir: Path, **model_kwargs) -> Path:
    archive = tmp_path / "rt.tar.gz"
    TarCompressor(make_model([str(tree)], **model_kwargs), str(archive)).compress()
    TarDecompressor(str(archive), str(out_dir)).decompress()
    return out_dir / "home" / "user" / tree.name


def test_contents_modes_links_and_empty_dirs_survive(sample_tree: Path, tmp_path: Path) -> None:
    restored = _roundtrip(sample_tree, tmp_path, tmp_path / "out")

    assert (restored / "plain.txt").read_text(encoding="utf-8") == "hello world\n"
    assert (restored / "accented ñame.txt").read_text(encoding="utf-8") == "olé\n"
    assert (restored / "nested" / "deep" / "binary.bin").read_bytes() == bytes(range(256))
    assert (restored / "nested" / "latin1.txt").read_bytes() == "café\n".encode("latin-1")
    assert (restored / "empty").is_dir()
    assert (restored / "link.txt").is_symlink()
    assert os.readlink(restored / "link.txt") == "plain.txt"
    assert stat.S_IMODE((restored / "executable.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((restored / "plain.txt").stat().st_mode) == 0o640


def test_empty_file_survives(fake_home: Path, tmp_path: Path) -> None:
    tree = fake_home / "t"
    tree.mkdir()
    (tree / "empty.txt").write_text("")
    restored = _roundtrip(tree, tmp_path, tmp_path / "out")
    assert (restored / "empty.txt").read_bytes() == b""


def test_normalization_round_trips_across_homes(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = fake_home / "t"
    tree.mkdir()
    (tree / "app.conf").write_text(f"path={fake_home}/data\n", encoding="utf-8")

    archive = tmp_path / "n.tar.gz"
    TarCompressor(make_model([str(tree)], normalize_content=True), str(archive)).compress()
    stored = archive_member_bytes(archive, "home/user/t/app.conf").decode()
    assert HOME_CONTENT_PLACEHOLDER in stored and str(fake_home) not in stored

    other_home = tmp_path / "home" / "other"
    other_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(other_home))
    out = tmp_path / "out"
    TarDecompressor(str(archive), str(out)).decompress()
    assert (out / "home" / "user" / "t" / "app.conf").read_text() == f"path={other_home}/data\n"


def test_literal_placeholder_is_untouched_when_normalization_is_off(fake_home: Path, tmp_path: Path) -> None:
    """Without normalize_content the archive must not rewrite a file that happens
    to contain the placeholder string."""
    tree = fake_home / "t"
    tree.mkdir()
    body = f"literal {HOME_CONTENT_PLACEHOLDER} kept\n"
    (tree / "weird.txt").write_text(body, encoding="utf-8")

    restored = _roundtrip(tree, tmp_path, tmp_path / "out", normalize_content=False)
    assert (restored / "weird.txt").read_text(encoding="utf-8") == body


def test_restore_without_output_dir_targets_the_home_paths(fake_home: Path, tmp_path: Path) -> None:
    tree = fake_home / "restoreme"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f.txt").write_text("data\n", encoding="utf-8")

    archive = tmp_path / "abs.tar.gz"
    TarCompressor(make_model([str(tree)]), str(archive)).compress()

    for path in (tree / "sub" / "f.txt", tree / "sub", tree):
        path.unlink() if path.is_file() else path.rmdir()
    assert not tree.exists()

    result = TarDecompressor(str(archive)).decompress()
    assert result.extracted > 0
    assert (tree / "sub" / "f.txt").read_text(encoding="utf-8") == "data\n"


def test_specific_files_entry_round_trips(fake_home: Path, tmp_path: Path) -> None:
    src = fake_home / ".config"
    (src / "appdir").mkdir(parents=True)
    (src / "appdir" / "inner.conf").write_text("a", encoding="utf-8")
    (src / "single.conf").write_text("b", encoding="utf-8")
    (src / "ignored.conf").write_text("c", encoding="utf-8")

    archive = tmp_path / "s.tar.gz"
    TarCompressor(make_model([{"source": str(src), "files": ["appdir", "single.conf"]}]), str(archive)).compress()
    out = tmp_path / "out"
    TarDecompressor(str(archive), str(out)).decompress()

    base = out / "home" / "user" / ".config"
    assert (base / "appdir" / "inner.conf").read_text() == "a"
    assert (base / "single.conf").read_text() == "b"
    assert not (base / "ignored.conf").exists()
