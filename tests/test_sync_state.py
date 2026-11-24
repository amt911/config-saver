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
    "content", [b"\xff\xfe", "{not json", "[]", '{"heads": 1, "base": {}}', '{"heads": {}, "base": {"c": {"p": {}}}}']
)
def test_corrupt_state_raises(tmp_path, content):
    p = tmp_path / "s.json"
    p.write_bytes(content if isinstance(content, bytes) else content.encode())
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


def test_save_fsyncs_data_before_the_rename(tmp_path, monkeypatch):
    calls = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b))[1])
    save(str(tmp_path / "s.json"), SyncState())
    assert calls[:2] == ["fsync", "replace"]
    assert calls.count("fsync") == 2  # the file, then the directory


def test_parents_round_trip_and_old_files_load(tmp_path):
    path = str(tmp_path / "s.json")
    save(path, SyncState(heads={"c": "h"}, base={"c": {}}, parents={"c": "p", "d": ""}))
    assert load(path).parents == {"c": "p", "d": ""}
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"heads": {"c": "h"}, "base": {"c": {}}}))
    assert load(str(old)) == SyncState(heads={"c": "h"}, base={"c": {}}, parents={})


def test_last_prune_round_trip_and_old_files_load_as_none(tmp_path):
    path = str(tmp_path / "s.json")
    save(path, SyncState(heads={"c": "h"}, base={"c": {}}, last_prune=1234.5))
    assert load(path).last_prune == 1234.5
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"heads": {"c": "h"}, "base": {"c": {}}}))
    assert load(str(old)).last_prune is None
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"heads": {}, "base": {}, "last_prune": "yesterday"}))
    with pytest.raises(SyncStateError):
        load(str(bad))


def _cache_entry(**kw):
    from config_saver.lib.sync.classify import CacheEntry

    base = {
        "size": 4,
        "mtime_ns": 5,
        "ctime_ns": 6,
        "inode": 7,
        "normalize_content": False,
        "fingerprint": Fingerprint("h", 0o644),
    }
    return CacheEntry(**{**base, **kw})


def test_hash_cache_round_trip(tmp_path):
    path = str(tmp_path / "s.json")
    state = SyncState(
        hash_cache={"/h/a": _cache_entry(), "/h/l": _cache_entry(fingerprint=Fingerprint("t", 0o777, "tg"))},
        hash_cache_home="/h",
    )
    save(path, state)
    assert load(path) == state


def test_old_state_file_has_an_empty_cache(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"heads": {}, "base": {}}))
    s = load(str(p))
    assert s.hash_cache == {} and s.hash_cache_home is None


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"/a": []},
        {
            "/a": {
                "size": "4",
                "mtime_ns": 5,
                "ctime_ns": 6,
                "inode": 7,
                "normalize_content": False,
                "sha256": "h",
                "mode": 1,
            }
        },
        {
            "/a": {
                "size": True,
                "mtime_ns": 5,
                "ctime_ns": 6,
                "inode": 7,
                "normalize_content": False,
                "sha256": "h",
                "mode": 1,
            }
        },
        {"/a": {"size": 4, "mtime_ns": 5, "ctime_ns": 6, "inode": 7, "normalize_content": 0, "sha256": "h", "mode": 1}},
        {
            "/a": {
                "size": 4,
                "mtime_ns": 5,
                "ctime_ns": 6,
                "inode": 7,
                "normalize_content": False,
                "sha256": 3,
                "mode": 1,
            }
        },
        {"/a": {"size": 4, "mtime_ns": 5, "ctime_ns": 6, "inode": 7, "normalize_content": False, "sha256": "h"}},
        {
            "/a": {
                "size": 4,
                "mtime_ns": 5,
                "ctime_ns": 6,
                "inode": 7,
                "normalize_content": False,
                "sha256": "h",
                "mode": 1,
                "link": 9,
            }
        },
    ],
)
def test_malformed_cache_is_dropped_not_fatal(tmp_path, bad):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"heads": {"c": "x"}, "base": {}, "hash_cache": bad, "hash_cache_home": "/h"}))
    s = load(str(p))
    assert s.hash_cache == {}
    assert s.heads == {"c": "x"}


def test_non_string_cache_home_is_dropped(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"heads": {}, "base": {}, "hash_cache": {}, "hash_cache_home": 5}))
    assert load(str(p)).hash_cache_home is None
