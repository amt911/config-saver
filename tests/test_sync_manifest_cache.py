import json
import os
import stat

import pytest

from config_saver.lib.sync import manifest_cache as cache
from config_saver.lib.sync.classify import Fingerprint
from config_saver.lib.sync.manifest import SELF, Manifest
from config_saver.lib.sync.paths import manifest_cache_path

A, B = "a" * 64, "b" * 64
H = "c" * 64


def _m(path: str = "s/x") -> Manifest:
    return Manifest("c", ["s"], {path: (Fingerprint(H, 0o644, None), A)})


def test_round_trip_is_private_and_drops_what_is_not_listed(tmp_path):
    path = str(tmp_path / "state" / "m.json")
    cache.save(path, {A: _m(), B: _m("s/y")}, {A})
    assert cache.load(path) == {A: _m()}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert os.listdir(os.path.dirname(path)) == ["m.json"]


def test_a_missing_file_is_an_empty_cache(tmp_path):
    assert cache.load(str(tmp_path / "nope.json")) == {}


def _write(tmp_path, doc) -> str:
    path = tmp_path / "m.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return str(path)


GOOD = _m().to_json().decode()
HOSTILE = Manifest("c", ["s"], {"s/x": (Fingerprint(H, 0o644, None), A)}).to_json().decode().replace("s/x", "../x")


@pytest.mark.parametrize(
    "doc",
    [
        "not json",
        "[]",
        {"version": 2, "manifests": {}},
        {"version": 1},
        {"version": 1, "manifests": []},
        {"version": 1, "manifests": {"short": GOOD}},
        {"version": 1, "manifests": {A.upper(): GOOD}},
        {"version": 1, "manifests": {A: 7}},
        {"version": 1, "manifests": {A: "{"}},
        {"version": 1, "manifests": {A: HOSTILE}},
        {"version": 1, "manifests": {A: GOOD, B: HOSTILE}},  # one bad entry poisons the whole file
        {"version": 1, "manifests": {A: GOOD}, "extra": 1},
    ],
)
def test_anything_malformed_means_an_empty_cache(tmp_path, doc):
    assert cache.load(_write(tmp_path, doc)) == {}


def test_a_cache_that_is_not_utf8_is_empty(tmp_path):
    path = tmp_path / "m.json"
    path.write_bytes(b"\xff\xfe")
    assert cache.load(str(path)) == {}


def test_a_failing_save_is_silent(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    cache.save(str(blocker / "sub" / "m.json"), {A: _m()}, {A})  # the parent cannot be created: no exception


def test_the_cache_sits_next_to_the_state_file():
    assert manifest_cache_path("/x/y/sync-state.json") == "/x/y/sync-manifests.json"
    assert SELF == "@"
