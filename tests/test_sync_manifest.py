import json

import pytest

from config_saver.lib.errors import ArchiveError, StoreError
from config_saver.lib.sync.classify import Fingerprint
from config_saver.lib.sync.manifest import SELF, Manifest, find_head

H = "a" * 64
SNAP = "b" * 64


def _manifest() -> Manifest:
    return Manifest(
        cfg="ssh",
        scope=["home/u/.ssh"],
        entries={
            "home/u/.ssh/config": (Fingerprint(H, 0o600), SELF),
            "home/u/.ssh/l": (Fingerprint(H, 0o777, "target"), SNAP),
        },
    )


def _raw(**over: object) -> bytes:
    doc: dict[str, object] = {
        "cfg": "c",
        "scope": ["a"],
        "entries": {"a/f": {"sha256": H, "mode": 0o644, "link": None, "snapshot": SELF}},
    }
    doc.update(over)
    return json.dumps(doc).encode()


def _entry(**over: object) -> bytes:
    e: dict[str, object] = {"sha256": H, "mode": 0o644, "link": None, "snapshot": SELF}
    e.update(over)
    return _raw(entries={"a/f": e})


def test_round_trip_and_deterministic():
    m = _manifest()
    assert Manifest.from_json(m.to_json()) == m
    reordered = Manifest(m.cfg, m.scope, dict(reversed(list(m.entries.items()))))
    assert reordered.to_json() == m.to_json()


def test_resolve_and_side():
    m = _manifest().resolve("c" * 64)
    assert m.entries["home/u/.ssh/config"][1] == "c" * 64
    assert m.entries["home/u/.ssh/l"][1] == SNAP
    assert m.side()["home/u/.ssh/l"] == Fingerprint(H, 0o777, "target")


@pytest.mark.parametrize(
    "data",
    [
        b"not json",
        b"[]",
        b"\xff\xfe",
        _raw(extra=1),
        _raw(cfg=1),
        _raw(scope="a"),
        _raw(scope=[1]),
        _raw(scope=["../x"]),
        _raw(scope=["/etc"]),
        _raw(scope=[""]),
        _raw(scope=["a/../b"]),
        _raw(scope=["a//b"]),
        _raw(scope=["."]),
        _raw(scope=["a/"]),
        _raw(entries={"a/./b": {"sha256": H, "mode": 1, "link": None, "snapshot": SELF}}),
        _raw(entries={"a//b": {"sha256": H, "mode": 1, "link": None, "snapshot": SELF}}),
        _raw(entries={"a/": {"sha256": H, "mode": 1, "link": None, "snapshot": SELF}}),
        _raw(entries={"a\0b": {"sha256": H, "mode": 1, "link": None, "snapshot": SELF}}),
        _raw(entries={"a\\b": {"sha256": H, "mode": 1, "link": None, "snapshot": SELF}}),
        b'{"cfg":"c","cfg":"d","scope":[],"entries":{}}',
        b'{"cfg":"c","scope":[],"entries":{"a":{"sha256":"%s","mode":1,"link":null,'
        b'"snapshot":"@"},"a":{"sha256":"%s","mode":1,"link":null,"snapshot":"@"}}}' % (H.encode(), H.encode()),
        b'{"cfg":"c","scope":[],"entries":{"a":{"sha256":"%s","sha256":"%s","mode":1,'
        b'"link":null,"snapshot":"@"}}}' % (H.encode(), H.encode()),
        _raw(entries=[]),
        _raw(entries={"../x": {"sha256": H, "mode": 1, "link": None, "snapshot": SELF}}),
        _raw(entries={"/etc/x": {"sha256": H, "mode": 1, "link": None, "snapshot": SELF}}),
        _raw(entries={"a/../b": {"sha256": H, "mode": 1, "link": None, "snapshot": SELF}}),
        _raw(entries={"": {"sha256": H, "mode": 1, "link": None, "snapshot": SELF}}),
        _raw(entries={"a": 5}),
        _raw(entries={"a": {"sha256": H, "mode": 1, "link": None}}),
        _entry(extra=1),
        _entry(sha256="zz" + "a" * 62),
        _entry(sha256="A" * 64),
        _entry(sha256="a" * 63),
        _entry(sha256=5),
        _entry(mode="x"),
        _entry(mode=True),
        _entry(mode=-1),
        _entry(mode=0o10000),
        _entry(link=5),
        _entry(snapshot="nothex"),
        _entry(snapshot="B" * 64),
        _entry(snapshot="b" * 10),
        _entry(snapshot=None),
    ],
)
def test_from_json_rejects(data):
    with pytest.raises(ArchiveError):
        Manifest.from_json(data)


def test_missing_top_level_key_rejected():
    doc = json.loads(_raw())
    del doc["scope"]
    with pytest.raises(ArchiveError):
        Manifest.from_json(json.dumps(doc).encode())


def test_find_head():
    assert find_head([], "c") is None
    assert find_head([("a", None)], "c") == "a"
    assert find_head([("b", "a"), ("a", None), ("c", "b")], "c") == "c"


def test_find_head_fork():
    with pytest.raises(StoreError, match="sync history of cfg1 forked"):
        find_head([("a", None), ("b", "a"), ("c", "a")], "cfg1")


def test_find_head_cycle():
    with pytest.raises(StoreError, match="sync history of c forked"):
        find_head([("a", "b"), ("b", "a")], "c")
