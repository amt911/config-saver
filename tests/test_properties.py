"""Property-based tests (Hypothesis).

Coverage says how much code ran; these say whether it is *right* for inputs
nobody would write by hand. Two properties carry the product:

    decompress(compress(tree)) == tree
    denormalize(normalize(path)) == path

Everything here builds a real tree in a temporary directory: mocking `tarfile`
or `os` would prove nothing about a tool whose whole job is filesystem
behaviour.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from config_saver.lib.tar_compressor.tar_compressor import (
    HOME_CONTENT_PLACEHOLDER,
    TarCompressor,
)
from config_saver.lib.tar_compressor.tar_decompressor import TarDecompressor
from config_saver.lib.utils.path_expander import PathExpander

from .conftest import make_model

# Path components: anything a Linux filesystem accepts, minus the separators and
# the two names that mean something else.
_COMPONENT = (
    st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs", "Cc"),
            blacklist_characters="/\x00",
        ),
        min_size=1,
        max_size=12,
    )
    .map(lambda s: s.strip())
    .filter(lambda s: s not in ("", ".", "..") and not s.startswith("."))
)

# Text that survives a UTF-8 round trip and does not already contain the
# placeholder (which is what the normalization property is about).
_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    max_size=200,
).filter(lambda s: HOME_CONTENT_PLACEHOLDER not in s)


@st.composite
def _trees(draw: st.DrawFn) -> list[tuple[tuple[str, ...], bytes]]:
    """A list of (relative path components, file content)."""
    entries = draw(
        st.lists(
            st.tuples(
                st.lists(_COMPONENT, min_size=1, max_size=3).map(tuple),
                st.binary(max_size=256),
            ),
            min_size=1,
            max_size=8,
            unique_by=lambda entry: "/".join(entry[0]),
        )
    )
    return entries


@contextmanager
def _temp_home() -> Iterator[Path]:
    """Run with $HOME pointed at a throwaway directory."""
    previous = os.environ.get("HOME")
    root = Path(tempfile.mkdtemp(prefix="config-saver-prop-"))
    home = root / "home"
    home.mkdir()
    os.environ["HOME"] = str(home)
    try:
        yield home
    finally:
        if previous is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = previous
        shutil.rmtree(root, ignore_errors=True)


def _materialise(root: Path, entries: list[tuple[tuple[str, ...], bytes]]) -> dict[str, bytes]:
    """Write the generated entries under root; return the ones that made it."""
    written: dict[str, bytes] = {}
    for parts, content in entries:
        target = root.joinpath(*parts)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        except (NotADirectoryError, FileExistsError, IsADirectoryError, OSError):
            # A generated entry can claim a path already taken by a directory
            # (or vice versa); that is a property of the generator, not a bug.
            continue
        written["/".join(parts)] = content
    return written


@given(entries=_trees())
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_round_trip_preserves_every_file(entries: list[tuple[tuple[str, ...], bytes]]) -> None:
    """compress -> decompress reproduces exactly the files that went in."""
    with _temp_home() as home:
        tree = home / "tree"
        tree.mkdir()
        written = _materialise(tree, entries)
        assume(written)

        archive = home.parent / "prop.tar.gz"
        TarCompressor(make_model([str(tree)]), str(archive)).compress()

        out = home.parent / "out"
        TarDecompressor(str(archive), str(out)).decompress()

        restored_root = out / "home" / "user" / "tree"
        restored = {
            str(path.relative_to(restored_root)): path.read_bytes()
            for path in restored_root.rglob("*")
            if path.is_file()
        }
        assert restored == written


@given(entries=_trees())
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_round_trip_preserves_the_directory_structure(
    entries: list[tuple[tuple[str, ...], bytes]],
) -> None:
    """Every directory, including the empty ones, comes back."""
    with _temp_home() as home:
        tree = home / "tree"
        tree.mkdir()
        assume(_materialise(tree, entries))
        (tree / "empty-dir").mkdir(exist_ok=True)

        archive = home.parent / "prop.tar.gz"
        TarCompressor(make_model([str(tree)]), str(archive)).compress()
        out = home.parent / "out"
        TarDecompressor(str(archive), str(out)).decompress()

        original = {str(p.relative_to(tree)) for p in tree.rglob("*") if p.is_dir()}
        restored_root = out / "home" / "user" / "tree"
        restored = {str(p.relative_to(restored_root)) for p in restored_root.rglob("*") if p.is_dir()}
        assert restored == original


@given(mode=st.integers(min_value=0, max_value=0o777))
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_round_trip_preserves_permission_bits(mode: int) -> None:
    """Any mode the user can set on a readable file survives the round trip."""
    assume(mode & 0o400)  # the compressor must be able to read it back
    with _temp_home() as home:
        tree = home / "tree"
        tree.mkdir()
        target = tree / "file"
        target.write_bytes(b"data")
        os.chmod(target, mode)

        archive = home.parent / "prop.tar.gz"
        TarCompressor(make_model([str(tree)]), str(archive)).compress()
        out = home.parent / "out"
        TarDecompressor(str(archive), str(out)).decompress()

        restored = out / "home" / "user" / "tree" / "file"
        assert restored.stat().st_mode & 0o777 == mode


@given(parts=st.lists(_COMPONENT, min_size=1, max_size=4))
@settings(max_examples=100, deadline=None)
def test_normalize_and_denormalize_are_inverses_inside_home(parts: list[str]) -> None:
    """The archive path of a home file maps back to that exact file."""
    with _temp_home() as home:
        original = str(home.joinpath(*parts))
        compressor = TarCompressor(make_model([]), "unused.tar.gz")
        decompressor = TarDecompressor("unused.tar.gz")
        assert decompressor._denormalize_path(compressor._normalize_path(original)) == original


@given(parts=st.lists(_COMPONENT, min_size=1, max_size=4))
@settings(max_examples=100, deadline=None)
def test_normalize_and_denormalize_are_inverses_outside_home(parts: list[str]) -> None:
    """Paths outside the home keep their absolute location."""
    with _temp_home():
        original = os.path.join("/etc", *parts)
        compressor = TarCompressor(make_model([]), "unused.tar.gz")
        decompressor = TarDecompressor("unused.tar.gz")
        assert decompressor._denormalize_path(compressor._normalize_path(original)) == original


@given(parts=st.lists(_COMPONENT, min_size=1, max_size=3))
@settings(max_examples=50, deadline=None)
def test_a_sibling_home_is_never_normalized(parts: list[str]) -> None:
    """/home/tester-anything must not be rewritten as if it were /home/tester."""
    with _temp_home() as home:
        sibling = str(home) + "-other"
        compressor = TarCompressor(make_model([]), "unused.tar.gz")
        normalized = compressor._normalize_path(os.path.join(sibling, *parts))
        assert not normalized.startswith(os.path.join("home", "user") + os.sep)


@given(text=_TEXT, tail=st.lists(_COMPONENT, min_size=1, max_size=2))
@settings(max_examples=60, deadline=None)
def test_content_normalization_survives_a_change_of_home(text: str, tail: list[str]) -> None:
    """A file mentioning the old home comes back mentioning the new one."""
    with _temp_home() as home:
        body = f"{text}\nprefix={home}/{'/'.join(tail)}\n"
        source = home / "conf"
        source.write_text(body, encoding="utf-8")

        compressor = TarCompressor(make_model([], normalize_content=True), "unused.tar.gz")
        normalized = compressor._normalize_file_content(str(source))
        assert normalized is not None
        assert str(home).encode() not in normalized

    with _temp_home() as other_home:
        decompressor = TarDecompressor("unused.tar.gz")
        restored = decompressor._denormalize_file_content(normalized).decode("utf-8")
        assert restored == body.replace(str(home), str(other_home))


@given(text=_TEXT)
@settings(max_examples=60, deadline=None)
def test_content_without_the_home_path_is_never_rewritten(text: str) -> None:
    with _temp_home() as home:
        source = home / "conf"
        source.write_bytes(text.encode("utf-8"))
        compressor = TarCompressor(make_model([], normalize_content=True), "unused.tar.gz")
        assume(str(home) not in text)
        assert compressor._normalize_file_content(str(source)) is None


@given(
    path=st.lists(_COMPONENT, min_size=1, max_size=4).map(lambda parts: "/" + "/".join(parts)),
)
@settings(max_examples=100, deadline=None)
def test_expansion_is_idempotent_and_deterministic(path: str) -> None:
    """expand() is a pure function: same input, same output, and a second pass
    changes nothing once every variable is resolved."""
    with _temp_home():
        assume("$" not in path)
        first = PathExpander().expand(path)
        assert first == PathExpander().expand(path)
        assert PathExpander().expand(first) == first


@given(tail=st.lists(_COMPONENT, min_size=0, max_size=3))
@settings(max_examples=60, deadline=None)
def test_home_variable_always_resolves_to_the_current_home(tail: list[str]) -> None:
    with _temp_home() as home:
        raw = "/".join(["$HOME", *tail])
        expanded = PathExpander().expand(raw)
        assert expanded.startswith(str(home))
        assert "$HOME" not in expanded
