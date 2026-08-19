"""The ``exclude`` key: prune subtrees a backup must not carry.

A configuration that archives a whole tree (``~/repos``, a VSCode profile) is
mostly regenerable bulk — ``node_modules``, downloaded SDKs, build output. The
key exists so those can be named once instead of the tree being enumerated by
hand, and pruning happens *during* the walk: an excluded directory is never
descended, which is the entire point on a 25 GB tree.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from config_saver.lib.models.model import Model
from config_saver.lib.tar_compressor.tar_compressor import TarCompressor

from .conftest import archive_names, make_model


def _compressor(directories: list, out: Path, **kwargs) -> TarCompressor:
    return TarCompressor(make_model(directories, **kwargs), str(out))


@pytest.fixture
def project(fake_home: Path) -> Path:
    """A tree shaped like a real repository checkout."""
    root = fake_home / "repos" / "app"
    (root / "src").mkdir(parents=True)
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / "src" / "node_modules").mkdir()
    (root / "build").mkdir()
    (root / "src" / "main.py").write_text("x\n", encoding="utf-8")
    (root / "node_modules" / "left-pad" / "index.js").write_text("y\n", encoding="utf-8")
    (root / "src" / "node_modules" / "nested.js").write_text("z\n", encoding="utf-8")
    (root / "build" / "out.o").write_bytes(b"\x00")
    (root / "debug.log").write_text("noise\n", encoding="utf-8")
    (root / "README.md").write_text("doc\n", encoding="utf-8")
    return root


def test_exclude_defaults_to_empty(fake_home: Path) -> None:
    assert Model.model_validate({"directories": []}).exclude == []


def test_exclude_is_a_list_of_patterns(fake_home: Path) -> None:
    assert Model.model_validate({"directories": [], "exclude": ["node_modules"]}).exclude == ["node_modules"]


def test_bare_pattern_prunes_that_directory(tmp_path: Path, project: Path) -> None:
    out = tmp_path / "out.tar.gz"
    _compressor([str(project)], out, exclude=["node_modules"]).compress()
    assert not [name for name in archive_names(out) if "node_modules" in name]
    assert any(name.endswith("src/main.py") for name in archive_names(out))


def test_bare_pattern_matches_at_any_depth(tmp_path: Path, project: Path) -> None:
    """src/node_modules is nested one level deeper and must go too."""
    out = tmp_path / "out.tar.gz"
    _compressor([str(project)], out, exclude=["node_modules"]).compress()
    assert not [name for name in archive_names(out) if name.endswith("nested.js")]


def test_glob_pattern_matches_files(tmp_path: Path, project: Path) -> None:
    out = tmp_path / "out.tar.gz"
    _compressor([str(project)], out, exclude=["*.log"]).compress()
    names = archive_names(out)
    assert not [name for name in names if name.endswith("debug.log")]
    assert any(name.endswith("README.md") for name in names)


def test_pattern_with_a_slash_matches_the_absolute_path(tmp_path: Path, project: Path) -> None:
    """A pattern naming a path excludes that one place, not every 'build'."""
    (project / "src" / "build").mkdir()
    (project / "src" / "build" / "keep.txt").write_text("k\n", encoding="utf-8")
    out = tmp_path / "out.tar.gz"
    _compressor([str(project)], out, exclude=[str(project / "build")]).compress()
    names = archive_names(out)
    assert not [name for name in names if name.endswith("out.o")]
    assert any(name.endswith("keep.txt") for name in names)


def test_path_pattern_accepts_wildcards(tmp_path: Path, project: Path) -> None:
    out = tmp_path / "out.tar.gz"
    _compressor([str(project)], out, exclude=[str(project.parent / "*" / "build")]).compress()
    assert not [name for name in archive_names(out) if name.endswith("out.o")]


def test_trailing_slash_still_names_a_directory(tmp_path: Path, project: Path) -> None:
    """`node_modules/` is how everyone writes it; it must not become a path pattern."""
    out = tmp_path / "out.tar.gz"
    _compressor([str(project)], out, exclude=["node_modules/"]).compress()
    assert not [name for name in archive_names(out) if "node_modules" in name]


def test_excluded_directory_is_never_descended(tmp_path: Path, project: Path, monkeypatch) -> None:
    """The saving is in not walking it: no directory read below an excluded root."""
    scanned: list[str] = []
    real_scandir = os.scandir

    def recording_scandir(path=".", *args, **kwargs):  # type: ignore[no-untyped-def]
        scanned.append(os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", recording_scandir)
    out = tmp_path / "out.tar.gz"
    _compressor([str(project)], out, exclude=["node_modules"]).compress()
    assert str(project / "src") in scanned, "the walk must still descend what was not excluded"
    assert not [path for path in scanned if "node_modules" in path]


def test_excluded_toplevel_entry_is_not_a_missing_input(tmp_path: Path, project: Path) -> None:
    """Excluding is deliberate; reporting it as missing would make --strict lie."""
    out = tmp_path / "out.tar.gz"
    result = _compressor([str(project)], out, exclude=["app"]).compress()
    assert result.missing_inputs == []
    assert result.excluded == 1
    assert archive_names(out) == []


def test_excluded_file_named_in_a_specific_entry_is_not_missing(tmp_path: Path, project: Path) -> None:
    out = tmp_path / "out.tar.gz"
    result = _compressor(
        [{"source": str(project), "files": ["README.md", "debug.log"]}],
        out,
        exclude=["*.log"],
    ).compress()
    assert result.missing_inputs == []
    assert result.excluded == 1
    assert [name.split("/")[-1] for name in archive_names(out)] == ["README.md"]


def test_excluded_entries_are_counted(tmp_path: Path, project: Path) -> None:
    out = tmp_path / "out.tar.gz"
    result = _compressor([str(project)], out, exclude=["node_modules", "*.log"]).compress()
    # build/ stays; two node_modules directories and one log file go.
    assert result.excluded == 3


def test_nothing_is_excluded_without_the_key(tmp_path: Path, project: Path) -> None:
    out = tmp_path / "out.tar.gz"
    result = _compressor([str(project)], out).compress()
    assert result.excluded == 0
    assert [name for name in archive_names(out) if "node_modules" in name]


def test_exclusion_does_not_make_a_backup_incomplete(tmp_path: Path, project: Path) -> None:
    """`complete` means 'everything asked for is in there' — excluded was not asked for."""
    out = tmp_path / "out.tar.gz"
    result = _compressor([str(project)], out, exclude=["node_modules"]).compress()
    assert result.complete is True


def test_patterns_expand_path_variables(tmp_path: Path, project: Path) -> None:
    """`$HOME/repos/*/build` must reach the same place `directories` would."""
    from config_saver.lib.parser.parser import Parser

    from .conftest import write_config

    cfg = write_config(
        tmp_path / "c.yaml",
        ["$HOME/repos"],
        exclude=["$HOME/repos/*/build"],
    )
    model = Parser(str(cfg)).get_model()
    out = tmp_path / "out.tar.gz"
    TarCompressor(model, str(out)).compress()
    assert not [name for name in archive_names(out) if name.endswith("out.o")]
    assert any(name.endswith("main.py") for name in archive_names(out))


def test_cli_reports_how_many_paths_were_excluded(tmp_path: Path, project: Path, capsys) -> None:
    """Never skip in silence: the run says how much it pruned."""
    from config_saver.lib.cli.cli import CLI

    from .conftest import write_config

    cfg = write_config(tmp_path / "c.yaml", [str(project)], exclude=["node_modules"])
    assert CLI(["--compress", "--input", str(cfg), "--output", str(tmp_path / "o.tar.gz")]).run() == 0
    assert "2 path(s) excluded" in capsys.readouterr().out
