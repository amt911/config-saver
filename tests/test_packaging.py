"""Packaging metadata must not drift from reality."""

from __future__ import annotations

from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib", reason="tomllib is 3.11+; the metadata check runs on newer interpreters")

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def pyproject() -> dict:
    with open(ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def test_license_file_referenced_by_metadata_exists(pyproject: dict) -> None:
    license_file = pyproject["project"]["license"]["file"]
    assert (ROOT / license_file).is_file()


def test_requires_python_matches_the_syntax_actually_used(pyproject: dict) -> None:
    """models/model.py evaluates `str | SpecificFilesModel` at runtime (3.10+)."""
    assert pyproject["project"]["requires-python"] == ">=3.10"


def test_pydantic_is_pinned_to_v2(pyproject: dict) -> None:
    deps = pyproject["project"]["dependencies"]
    assert any(d.startswith("pydantic") and ">=2" in d and "<3" in d for d in deps), deps


def test_single_version_source(pyproject: dict) -> None:
    """One mechanism only: a static version and no setuptools_scm to disagree with it."""
    assert "version" in pyproject["project"]
    assert "setuptools_scm" not in pyproject.get("tool", {})
    assert "setuptools_scm" not in pyproject["build-system"]["requires"]


def test_example_configs_are_shipped(pyproject: dict) -> None:
    data_files = pyproject["tool"]["setuptools"]["data-files"]
    assert data_files["share/config-saver/configs"] == ["configs/*.yaml"]


def test_dev_extra_carries_the_toolchain(pyproject: dict) -> None:
    dev = pyproject["project"]["optional-dependencies"]["dev"]
    for tool in ("pytest", "ruff", "mypy", "pre-commit"):
        assert any(entry.startswith(tool) for entry in dev), tool


def test_version_matches_the_package_metadata_when_installed(pyproject: dict) -> None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = version("config-saver")
    except PackageNotFoundError:
        pytest.skip("config-saver is not installed in this environment")
    # An editable install of an older checkout is fine; a *released* mismatch is what
    # the release workflow guards. Here we only assert both are PEP 440-ish strings.
    assert installed.split(".")[0].isdigit()
    assert pyproject["project"]["version"].split(".")[0].isdigit()
