"""Local scan for sync: scope, fingerprints, size limit, and the compressor filters it relies on."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path

import pytest

from config_saver.lib.errors import SyncTooLargeError, SyncUnreadableError
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


def test_too_large_raises(fake_home: Path) -> None:
    f = fake_home / "big"
    f.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(SyncTooLargeError, match="too large"):
        scan(make_model([str(f)], sync_max_mb=1))


def test_exactly_at_limit_is_allowed(fake_home: Path) -> None:
    f = fake_home / "ok"
    f.write_bytes(b"x" * (1024 * 1024))
    assert scan(make_model([str(f)], sync_max_mb=1))[2] == 1024 * 1024


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

    monkeypatch.setattr(mod, "_fingerprint", lambda *_a: pytest.fail("file was read"))
    f = fake_home / "big"
    f.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(SyncTooLargeError):
        scan(make_model([str(f)], sync_max_mb=1))


# ------------------------------------- unreadable is not deleted (final review)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 file")
def test_unreadable_file_in_scope_fails_the_scan(fake_home: Path) -> None:
    from config_saver.lib.errors import SyncUnreadableError

    f = fake_home / "secret.conf"
    f.write_text("x", encoding="utf-8")
    f.chmod(0)
    try:
        with pytest.raises(SyncUnreadableError, match="secret.conf"):
            scan(make_model([str(f)]))
    finally:
        f.chmod(0o600)


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists a 0o000 directory")
def test_unlistable_directory_in_scope_fails_the_scan(fake_home: Path) -> None:
    from config_saver.lib.errors import SyncUnreadableError

    d = fake_home / "tree"
    (d / "locked").mkdir(parents=True)
    (d / "locked" / "inner").write_text("x", encoding="utf-8")
    (d / "locked").chmod(0)
    try:
        with pytest.raises(SyncUnreadableError, match="locked"):
            scan(make_model([str(d)]))
    finally:
        (d / "locked").chmod(0o700)


def test_root_owned_file_in_scope_fails_the_scan(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from config_saver.lib.errors import SyncUnreadableError

    f = fake_home / "owned.conf"
    f.write_text("x", encoding="utf-8")
    monkeypatch.setattr(TarCompressor, "_skips_root_owned", lambda self, path: path == str(f))
    with pytest.raises(SyncUnreadableError, match="owned by root"):
        scan(make_model([str(f)]))


def test_vanished_file_is_still_just_absent(fake_home: Path) -> None:
    scope, side, _ = scan(make_model([str(fake_home / "never")]))
    assert side == {} and scope.contains("home/user/never")


def test_system_config_dir_is_refused(fake_home: Path) -> None:
    from config_saver.lib.errors import SyncSystemConfigError

    for declared in (
        [{"source": "/etc/config-saver/configs", "files": ["a.yaml"]}],
        ["/etc/config-saver/configs"],
        ["/etc/config-saver/configs/sub/../x.yaml"],
    ):
        with pytest.raises(SyncSystemConfigError, match="/etc/config-saver/configs"):
            scan(make_model(declared))
    scan(make_model(["/etc/config-saver/configs-other"]))  # a sibling name is fine


def _behind_locked_parent(fake_home: Path) -> Path:
    d = fake_home / "d"
    d.mkdir()
    (d / "f").write_text("x", encoding="utf-8")
    d.chmod(0)
    return d


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches a 0o000 directory")
@pytest.mark.parametrize("form", ["string", "source_files"])
def test_path_behind_an_unreachable_parent_is_not_an_absence(fake_home: Path, form: str) -> None:
    from config_saver.lib.errors import SyncUnreadableError

    d = _behind_locked_parent(fake_home)
    try:
        directories = [str(d / "f")] if form == "string" else [{"source": str(d), "files": ["f"]}]
        with pytest.raises(SyncUnreadableError, match="d/f"):
            scan(make_model(directories))
    finally:
        d.chmod(0o700)


# ---------------------------------------------------------------- sync_max_mb


def test_limit_comes_from_sync_max_mb(fake_home: Path) -> None:
    f = fake_home / "big"
    f.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(SyncTooLargeError, match=r"over the 1 MiB limit"):
        scan(make_model([str(f)], sync_max_mb=1))
    assert scan(make_model([str(f)], sync_max_mb=2))[2] == 1024 * 1024 + 1


def test_default_limit_message_says_50_mib(fake_home: Path) -> None:
    f = fake_home / "big"
    f.write_bytes(b"x" * (50 * 1024 * 1024 + 1))
    with pytest.raises(SyncTooLargeError, match=r"over the 50 MiB limit"):
        scan(make_model([str(f)]))


# ----------------------------------------------------------------- hash cache


@pytest.fixture
def hashed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Paths the real hashing function was asked to read, in order."""
    import config_saver.lib.sync.scan as mod

    calls: list[str] = []
    real = mod._fingerprint

    def spy(compressor, path, problems):  # type: ignore[no-untyped-def]
        calls.append(path)
        return real(compressor, path, problems)

    monkeypatch.setattr(mod, "_fingerprint", spy)
    return calls


def test_cache_hit_skips_hashing_and_matches_a_cold_scan(fake_home: Path, hashed: list[str]) -> None:
    f = fake_home / "rc"
    f.write_text("abc\n", encoding="utf-8")
    model = make_model([str(f)])
    cache: dict = {}
    _s, first, t1 = scan(model, cache)
    assert hashed == [str(f)]
    _s, second, t2 = scan(model, cache)
    assert hashed == [str(f)]  # no second read
    assert (second, t2) == (first, t1)
    assert second == scan(model)[1]


def test_cache_hit_keeps_symlink_size_out_of_the_total(sample_tree: Path, hashed: list[str]) -> None:
    model = make_model([str(sample_tree)])
    cache: dict = {}
    cold = scan(model, cache)
    n = len(hashed)
    warm = scan(model, cache)
    assert len(hashed) == n
    assert warm[1:] == cold[1:]


def _rewrite_same_size_same_mtime(f: Path, data: bytes) -> None:
    st = f.stat()
    f.write_bytes(data)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_same_size_same_mtime_edit_is_not_stale(fake_home: Path, hashed: list[str]) -> None:
    """ctime still moves, so the entry is rejected even though size and mtime_ns match."""
    f = fake_home / "rc"
    f.write_bytes(b"aaaa")
    model = make_model([str(f)])
    cache: dict = {}
    old = scan(model, cache)[1]["home/user/rc"]
    _rewrite_same_size_same_mtime(f, b"bbbb")
    new = scan(model, cache)[1]["home/user/rc"]
    assert new != old
    assert new.sha256 == hashlib.sha256(b"bbbb").hexdigest()
    assert len(hashed) == 2


def _entry(cache: dict, f: Path) -> tuple:
    return cache[str(f)]


def test_each_field_of_the_validity_tuple_forces_a_rehash(fake_home: Path, hashed: list[str]) -> None:
    f = fake_home / "rc"
    f.write_bytes(b"aaaa")
    model = make_model([str(f)])
    cache: dict = {}
    scan(model, cache)
    base = cache[str(f)]
    st = f.stat()

    def rescan_with(**changes: object) -> None:
        mutated = base._replace(**changes)
        cache[str(f)] = mutated
        before = len(hashed)
        scan(model, cache)
        assert len(hashed) == before + 1, changes
        assert cache[str(f)] == base  # refreshed to the truth

    rescan_with(size=st.st_size + 1)
    rescan_with(mtime_ns=st.st_mtime_ns + 1)
    rescan_with(ctime_ns=st.st_ctime_ns + 1)
    rescan_with(inode=st.st_ino + 1)
    rescan_with(normalize_content=True)
    before = len(hashed)
    scan(model, cache)
    assert len(hashed) == before  # the untouched entry is a hit


def test_chmod_bumps_ctime_and_rehashes(fake_home: Path, hashed: list[str]) -> None:
    f = fake_home / "rc"
    f.write_bytes(b"aaaa")
    model = make_model([str(f)])
    cache: dict = {}
    scan(model, cache)
    os.chmod(f, 0o600)
    side = scan(model, cache)[1]
    assert len(hashed) == 2
    assert side["home/user/rc"].mode == 0o600


def test_replaced_file_with_same_size_and_mtime_rehashes(fake_home: Path, hashed: list[str]) -> None:
    f = fake_home / "rc"
    f.write_bytes(b"aaaa")
    model = make_model([str(f)])
    cache: dict = {}
    scan(model, cache)
    st = f.stat()
    other = fake_home / "other"
    other.write_bytes(b"bbbb")
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(other, f)
    side = scan(model, cache)[1]
    assert len(hashed) == 2
    assert side["home/user/rc"].sha256 == hashlib.sha256(b"bbbb").hexdigest()


def test_normalize_content_flip_rehashes(fake_home: Path, hashed: list[str]) -> None:
    f = fake_home / "rc"
    f.write_text(f"X={fake_home}\n", encoding="utf-8")
    cache: dict = {}
    a = scan(make_model([str(f)], normalize_content=False), cache)[1]["home/user/rc"]
    b = scan(make_model([str(f)], normalize_content=True), cache)[1]["home/user/rc"]
    assert len(hashed) == 2
    assert a.sha256 != b.sha256


def test_entries_for_unseen_paths_are_dropped(fake_home: Path) -> None:
    f, g = fake_home / "f", fake_home / "g"
    f.write_text("1")
    g.write_text("2")
    cache: dict = {}
    scan(make_model([str(f), str(g)]), cache)
    assert set(cache) == {str(f), str(g)}
    g.unlink()
    scan(make_model([str(f), str(g)]), cache)
    assert set(cache) == {str(f)}


def test_a_failed_scan_does_not_cache_unreadable_files(fake_home: Path) -> None:
    f = fake_home / "gone"
    cache: dict = {}
    scan(make_model([str(f)]), cache)
    assert cache == {}


def test_a_warm_cache_never_lets_a_smaller_limit_through(fake_home: Path, hashed: list[str]) -> None:
    f = fake_home / "big"
    f.write_bytes(b"x" * (3 * 1024 * 1024))
    cache: dict = {}
    scan(make_model([str(f)], sync_max_mb=4), cache)
    assert hashed == [str(f)]
    with pytest.raises(SyncTooLargeError):
        scan(make_model([str(f)], sync_max_mb=2), cache)  # served from the cache, still over the limit
    assert hashed == [str(f)]  # and nothing was rehashed to find out


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 file")
def test_an_unreadable_file_is_never_cached(fake_home: Path) -> None:
    f = fake_home / "secret"
    f.write_text("x")
    f.chmod(0)
    cache: dict = {}
    with pytest.raises(SyncUnreadableError):
        scan(make_model([str(f)]), cache)
    assert str(f) not in cache
