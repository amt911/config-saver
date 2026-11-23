"""Local scan for sync: scope, fingerprints, size limit, and the compressor filters it relies on."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path

import pytest

from config_saver.lib.errors import SyncTooLargeError
from config_saver.lib.sync.scan import LocalScope, scan
from config_saver.lib.tar_compressor.tar_compressor import TarCompressor
from tests.conftest import make_model


def _arc(home: Path, path: Path) -> str:
    return "home/user/" + str(path.relative_to(home))


def test_sha_is_home_independent_with_normalize_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shas = []
    for name in ("alice", "bob"):
        home = tmp_path / name
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        (home / "rc").write_text(f"export X={home}/bin\n", encoding="utf-8")
        _scope, side, _total = scan(make_model([str(home / "rc")], normalize_content=True))
        shas.append(side["home/user/rc"].sha256)
    assert shas[0] == shas[1]


def test_sha_without_normalization_is_of_raw_bytes(fake_home: Path) -> None:
    f = fake_home / "rc"
    f.write_text(f"export X={fake_home}\n", encoding="utf-8")
    _scope, side, _ = scan(make_model([str(f)], normalize_content=False))
    assert side["home/user/rc"].sha256 == hashlib.sha256(f.read_bytes()).hexdigest()


def test_symlink_fingerprint_uses_target_and_permission_bits(sample_tree: Path, fake_home: Path) -> None:
    _scope, side, total = scan(make_model([str(sample_tree)]))
    link = side[_arc(fake_home, sample_tree / "link.txt")]
    assert link.link == "plain.txt"
    assert link.sha256 == hashlib.sha256(b"plain.txt").hexdigest()
    assert side[_arc(fake_home, sample_tree / "plain.txt")].mode == 0o640
    assert side[_arc(fake_home, sample_tree / "executable.sh")].mode == 0o755
    # directories are not fingerprinted
    assert _arc(fake_home, sample_tree / "nested") not in side
    assert total == sum(p.stat().st_size for p in sample_tree.rglob("*") if p.is_file() and not p.is_symlink())


def test_too_large_raises(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import config_saver.lib.sync.scan as mod

    monkeypatch.setattr(mod, "MAX_SYNC_BYTES", 10)
    f = fake_home / "big"
    f.write_bytes(b"x" * 11)
    with pytest.raises(SyncTooLargeError, match="too large"):
        scan(make_model([str(f)]))


def test_exactly_at_limit_is_allowed(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import config_saver.lib.sync.scan as mod

    monkeypatch.setattr(mod, "MAX_SYNC_BYTES", 10)
    f = fake_home / "ok"
    f.write_bytes(b"x" * 10)
    assert scan(make_model([str(f)]))[2] == 10


# ---------------------------------------------------------------- LocalScope


def test_scope_declared_but_missing_file_is_in_scope(fake_home: Path) -> None:
    scope, side, _ = scan(make_model([{"source": str(fake_home), "files": [".zshrc"]}]))
    assert side == {}
    assert scope.roots == {"home/user/.zshrc"}
    assert scope.contains("home/user/.zshrc")


def test_scope_file_under_declared_dir(sample_tree: Path, fake_home: Path) -> None:
    scope, _, _ = scan(make_model([str(sample_tree)]))
    assert scope.contains(_arc(fake_home, sample_tree / "nested" / "gone.txt"))


def test_scope_excluded_subdir_is_out(sample_tree: Path, fake_home: Path) -> None:
    scope, _, _ = scan(make_model([str(sample_tree)], exclude=["nested"]))
    assert not scope.contains(_arc(fake_home, sample_tree / "nested" / "deep" / "binary.bin"))
    assert scope.contains(_arc(fake_home, sample_tree / "plain.txt"))


def test_scope_excluded_by_name_pattern(sample_tree: Path, fake_home: Path) -> None:
    scope, side, _ = scan(make_model([str(sample_tree)], exclude=["*.txt"]))
    assert not scope.contains(_arc(fake_home, sample_tree / "plain.txt"))
    assert _arc(fake_home, sample_tree / "plain.txt") not in side


def test_scope_sibling_with_string_prefix_is_out(fake_home: Path) -> None:
    scope, _, _ = scan(make_model([str(fake_home / ".zsh")]))
    assert not scope.contains("home/user/.zshrc")
    assert scope.contains("home/user/.zsh/x")


def test_scope_direct_construction_without_paths() -> None:
    assert LocalScope(frozenset({"a"})).contains("a/b")


# ------------------------------------------------- compressor filters (sync)


def _members(data: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        return {m.name: m for m in tar.getmembers()}


def test_compress_to_stream_only_and_extra_members(sample_tree: Path, fake_home: Path) -> None:
    keep = _arc(fake_home, sample_tree / "nested" / "deep" / "binary.bin")
    buf = io.BytesIO()
    TarCompressor(make_model([str(sample_tree)])).compress_to_stream(
        buf, only={keep}, extra_members={"manifest.json": b"{}"}
    )
    names = set(_members(buf.getvalue()))
    assert names == {
        ".config-saver-metadata.json",
        "manifest.json",
        _arc(fake_home, sample_tree),
        _arc(fake_home, sample_tree / "nested"),
        _arc(fake_home, sample_tree / "nested" / "deep"),
        keep,
    }
    extra = _members(buf.getvalue())["manifest.json"]
    assert extra.mode == 0o600 and extra.isfile()


def test_compress_to_stream_without_only_is_unchanged(sample_tree: Path) -> None:
    buf = io.BytesIO()
    TarCompressor(make_model([str(sample_tree)])).compress_to_stream(buf)
    assert "manifest.json" not in _members(buf.getvalue())
    assert len(_members(buf.getvalue())) > 8


def test_iter_files_yields_files_and_symlinks_only(sample_tree: Path, fake_home: Path) -> None:
    got = {arc: path for path, arc in TarCompressor(make_model([str(sample_tree)])).iter_files()}
    assert _arc(fake_home, sample_tree / "link.txt") in got
    assert _arc(fake_home, sample_tree / "nested") not in got
    assert all(os.path.isfile(p) or os.path.islink(p) for p in got.values())


def test_content_stream_normalizes_only_when_asked(fake_home: Path) -> None:
    f = fake_home / "rc"
    f.write_text(f"p={fake_home}\n", encoding="utf-8")
    with TarCompressor(make_model([str(f)], normalize_content=True)).content_stream(str(f)) as s:
        assert s.read() == b"p=<<<HOME_PLACEHOLDER>>>\n"
    with TarCompressor(make_model([str(f)], normalize_content=False)).content_stream(str(f)) as s:
        assert s.read() == f.read_bytes()


def test_scope_excluded_source_drops_whole_entry(fake_home: Path) -> None:
    src = fake_home / "srcdir"
    src.mkdir()
    (src / "f").write_text("x", encoding="utf-8")
    scope, side, _ = scan(make_model([{"source": str(src), "files": ["f"]}], exclude=["srcdir"]))
    assert side == {}
    assert not scope.contains("home/user/srcdir/f")


def test_too_large_is_refused_before_reading(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import config_saver.lib.sync.scan as mod

    monkeypatch.setattr(mod, "MAX_SYNC_BYTES", 10)
    monkeypatch.setattr(mod, "_fingerprint", lambda *_a: pytest.fail("file was read"))
    f = fake_home / "big"
    f.write_bytes(b"x" * 11)
    with pytest.raises(SyncTooLargeError):
        scan(make_model([str(f)]))
