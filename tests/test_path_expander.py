"""PathExpander is pure and deterministic; pin its behaviour."""

from __future__ import annotations

from pathlib import Path

from config_saver.lib.utils.path_expander import PathExpander


def test_expands_custom_variables(fake_home: Path) -> None:
    expander = PathExpander()
    assert expander.expand("$HOME/x") == str(fake_home / "x")
    assert expander.expand("$CONFIG_DIR/y") == str(fake_home / ".config" / "y")
    assert expander.expand("$SHARE_DIR") == str(fake_home / ".local" / "share")


def test_expands_environment_variables(monkeypatch) -> None:
    monkeypatch.setenv("CS_TEST_VAR", "/opt/data")
    assert PathExpander().expand("$CS_TEST_VAR/sub") == "/opt/data/sub"


def test_unknown_variable_is_left_untouched() -> None:
    assert PathExpander().expand("$NOT_A_REAL_VAR_12345/x") == "$NOT_A_REAL_VAR_12345/x"


def test_begins_with_resolves_single_match(tmp_path: Path) -> None:
    (tmp_path / "profile.abcdef").mkdir()
    expanded = PathExpander().expand(str(tmp_path / '${BEGINS_WITH="profile."}'))
    assert expanded == str(tmp_path / "profile.abcdef")


def test_ends_with_resolves_single_match(tmp_path: Path) -> None:
    (tmp_path / "session.default").mkdir()
    expanded = PathExpander().expand(str(tmp_path / '${ENDS_WITH=".default"}'))
    assert expanded == str(tmp_path / "session.default")


def test_multiple_matches_are_deterministic_and_recorded(tmp_path: Path) -> None:
    for name in ("p.zzz", "p.aaa", "p.mmm"):
        (tmp_path / name).mkdir()
    expander = PathExpander()
    expanded = expander.expand(str(tmp_path / '${BEGINS_WITH="p."}'))
    # Sorted, so the filesystem's directory order never becomes behaviour.
    assert expanded == str(tmp_path / "p.aaa")
    assert expander.ambiguities and expander.ambiguities[0][1] == ["p.aaa", "p.mmm", "p.zzz"]


def test_no_match_leaves_the_token_and_is_reported(tmp_path: Path) -> None:
    raw = str(tmp_path / '${BEGINS_WITH="nothing"}' / "sub")
    expander = PathExpander()
    assert expander.expand(raw) == raw
    assert expander.unresolved == [raw]


def test_placeholder_in_the_middle_of_a_path(tmp_path: Path) -> None:
    (tmp_path / "prof.x" / "inner").mkdir(parents=True)
    expanded = PathExpander().expand(str(tmp_path / '${ENDS_WITH=".x"}' / "inner"))
    assert expanded == str(tmp_path / "prof.x" / "inner")


def test_custom_vars_can_be_injected() -> None:
    expander = PathExpander(custom_vars={"HOME": "/srv/user"})
    assert expander.expand("$HOME/.config") == "/srv/user/.config"
