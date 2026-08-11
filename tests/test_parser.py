"""Parser + model validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from config_saver.lib.errors import RootRequiredError
from config_saver.lib.parser.parser import Parser


def test_parses_yaml_and_expands_paths(tmp_path: Path, fake_home: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"directories": ["$HOME/data", {"source": "$CONFIG_DIR", "files": ["a"]}]}))
    model = Parser(str(cfg)).get_model()
    assert model.directories[0] == str(fake_home / "data")
    assert model.directories[1].source == str(fake_home / ".config")  # type: ignore[union-attr]


def test_parses_json(tmp_path: Path, fake_home: Path) -> None:
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"directories": ["$HOME/data"], "normalize_content": True}))
    parser = Parser(str(cfg))
    assert parser.get_model().normalize_content is True
    assert parser.get_attr("directories") == [str(fake_home / "data")]


def test_missing_required_field_is_a_validation_error(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"normalize_content": True}))
    with pytest.raises(ValidationError):
        Parser(str(cfg))


def test_typoed_key_is_rejected(tmp_path: Path) -> None:
    """A silently ignored typo means data the user believes is backed up is not."""
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"directories": ["/tmp"], "normalise_content": True}))
    with pytest.raises(ValidationError):
        Parser(str(cfg))


def test_typoed_key_in_nested_entry_is_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"directories": [{"source": "/tmp", "file": ["a"]}]}))
    with pytest.raises(ValidationError):
        Parser(str(cfg))


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("directories: [unclosed\n")
    with pytest.raises(yaml.YAMLError):
        Parser(str(cfg))


def test_only_root_user_raises_typed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"directories": ["/tmp"], "only_root_user": True}))
    monkeypatch.setattr("os.getuid", lambda: 1000)
    with pytest.raises(RootRequiredError) as excinfo:
        Parser(str(cfg))
    # The type carries the meaning; the message is presentation only.
    assert excinfo.value.config_path == str(cfg)
    assert isinstance(excinfo.value, PermissionError)


def test_only_root_user_allows_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"directories": ["/tmp"], "only_root_user": True}))
    monkeypatch.setattr("os.getuid", lambda: 0)
    assert Parser(str(cfg)).get_model().only_root_user is True


def test_unresolved_placeholders_are_reported(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    raw = str(tmp_path / '${BEGINS_WITH="nope"}')
    cfg.write_text(yaml.safe_dump({"directories": [raw]}))
    assert Parser(str(cfg)).unresolved_paths == [raw]


def test_missing_config_file_raises_filenotfound(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Parser(str(tmp_path / "nope.yaml"))
