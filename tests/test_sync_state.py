import json
import os
import pathlib
import stat

import pytest

from config_saver.lib.errors import SyncStateError
from config_saver.lib.sync.classify import Fingerprint
from config_saver.lib.sync.paths import state_path, sync_dir
from config_saver.lib.sync.state import SyncState, load, save


def test_missing_file_is_empty(tmp_path):
    state = load(str(tmp_path / "nope.json"))
    assert state == SyncState(heads={}, base={})


def test_round_trip_and_modes(tmp_path):
    path = str(tmp_path / "a" / "b" / "state.json")
    state = SyncState(
        heads={"cfg": "abc"},
        base={"cfg": {"x": Fingerprint("h", 0o644, None), "l": Fingerprint("t", 0o777, "target")}},
    )
    save(path, state)
    assert load(path) == state
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert sorted(os.listdir(os.path.dirname(path))) == ["state.json"]
    assert json.loads(pathlib.Path(path).read_text())["base"]["cfg"]["l"] == {
        "sha256": "t",
        "mode": 0o777,
        "link": "target",
    }


def test_empty_file_is_empty_state(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("")
    assert load(str(p)) == SyncState(heads={}, base={})


@pytest.mark.parametrize(
    "content", ["{not json", "[]", '{"heads": 1, "base": {}}', '{"heads": {}, "base": {"c": {"p": {}}}}']
)
def test_corrupt_state_raises(tmp_path, content):
    p = tmp_path / "s.json"
    p.write_text(content)
    with pytest.raises(SyncStateError) as exc:
        load(str(p))
    assert str(p) in str(exc.value) and "Delete it" in str(exc.value)


def test_paths_follow_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    assert sync_dir() == str(tmp_path / "d" / "config-saver" / "sync")
    assert state_path() == str(tmp_path / "s" / "config-saver" / "sync-state.json")


def test_paths_default_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert sync_dir() == str(tmp_path / ".local/share/config-saver/sync")
    assert state_path() == str(tmp_path / ".local/state/config-saver/sync-state.json")
