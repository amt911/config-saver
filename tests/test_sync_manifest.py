import json
import random
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from config_saver.lib.errors import ArchiveError, StoreError
from config_saver.lib.sync.classify import Fingerprint
from config_saver.lib.sync.manifest import (
    SELF,
    Manifest,
    SnapRef,
    find_head,
    losers,
    select_head,
    unreferenced,
)

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


# Each rejection names the field that failed: the message is how an operator tells which
# check fired, so it is pinned per case (not only the exception type).
@pytest.mark.parametrize(
    ("data", "message"),
    [
        (_raw(scope=[""]), "path must be a non-empty string"),
        (_raw(scope=[5]), "path must be a non-empty string"),
        (_raw(scope=["a/../b"]), "unsafe path 'a/../b'"),
        (_raw(scope=["/abs"]), "unsafe path '/abs'"),
        (_raw(entries={"a\\b": {}}), "unsafe path 'a\\\\b'"),
        (b'{"cfg":"c","cfg":"d","scope":[],"entries":{}}', "duplicate key 'cfg'"),
        (_raw(entries=[]), "entries must be an object"),
        (_raw(entries={"a/f": 3}), "entry must be an object with exactly keys"),
        (_entry(sha256="XYZ"), "sha256 must be 64 lowercase hex characters"),
        (_entry(mode=-1), "mode must be an integer in 0..0o7777"),
        (_entry(mode=0o10000), "mode must be an integer in 0..0o7777"),
        (_entry(link=3), "link must be a string or null"),
        (_entry(snapshot="nope"), "snapshot must be 64 lowercase hex characters or the self marker"),
        (b"not json", "not valid JSON"),
    ],
)
def test_from_json_rejection_messages(data, message):
    with pytest.raises(ArchiveError) as info:
        Manifest.from_json(data)
    text = str(info.value)
    assert text.startswith("invalid sync manifest: ")
    assert message in text


@pytest.mark.parametrize("mode", [0, 0o7777])
def test_mode_bounds_are_inclusive(mode):
    parsed = Manifest.from_json(_entry(mode=mode))
    assert parsed.entries["a/f"][0].mode == mode


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _t(n: int) -> datetime:
    return T0 + timedelta(seconds=n)


def _ref(i: str, parent: str | None, n: int) -> SnapRef:
    return SnapRef(i, parent, _t(n))


def test_select_head_empty() -> None:
    assert select_head([]) is None
    assert losers([]) == set()


def test_select_head_linear_chain_any_order() -> None:
    snaps = [_ref("c", "b", 3), _ref("a", None, 1), _ref("b", "a", 2)]
    assert select_head(snaps) == "c"
    assert losers(snaps) == set()


def test_fork_earliest_wins_regardless_of_order() -> None:
    a = _ref("a", None, 1)
    x = _ref("x", "a", 5)
    y = _ref("y", "a", 4)
    assert select_head([a, x, y]) == "y"
    assert select_head([y, x, a]) == "y"
    assert losers([a, x, y]) == {"x"}


def test_fork_time_tie_smaller_id_wins() -> None:
    a = _ref("a", None, 1)
    assert select_head([a, _ref("q", "a", 4), _ref("p", "a", 4)]) == "p"


def test_losers_descendants_are_losers() -> None:
    snaps = [
        _ref("a", None, 1),
        _ref("w", "a", 2),
        _ref("l", "a", 3),
        _ref("l2", "l", 4),
        _ref("l3", "l2", 5),
        _ref("w2", "w", 9),
    ]
    assert select_head(snaps) == "w2"
    assert losers(snaps) == {"l", "l2", "l3"}


def test_fork_picks_by_time_not_by_descendant_depth() -> None:
    snaps = [_ref("a", None, 1), _ref("w", "a", 2), _ref("l", "a", 3), _ref("l2", "l", 4)]
    assert select_head(snaps) == "w"


def test_unknown_parent_counts_as_root() -> None:
    assert select_head([_ref("b", "gone", 2)]) == "b"


def test_several_roots_earliest_wins() -> None:
    snaps = [_ref("r2", None, 2), _ref("r1", None, 1), _ref("c1", "r1", 3), _ref("c2", "r2", 4)]
    assert select_head(snaps) == "c1"
    assert losers(snaps) == {"r2", "c2"}


def test_cycle_terminates() -> None:
    snaps = [_ref("a", "b", 1), _ref("b", "a", 2)]
    assert select_head(snaps) is None
    assert losers(snaps) == {"a", "b"}


def test_cycle_hanging_off_chain_terminates() -> None:
    snaps = [_ref("r", None, 1), _ref("a", "r", 2), _ref("b", "a", 3)]
    assert select_head(snaps) == "b"


def _m(entries: dict[str, str]) -> Manifest:
    return Manifest("c", ["s"], {p: (Fingerprint(H, 0o644), s) for p, s in entries.items()})


def test_unreferenced_exact() -> None:
    snaps = [_ref(i, None, n) for n, i in enumerate(["h", "r1", "r2", "old", "other"])]
    heads = {"h": _m({"s/a": SELF, "s/b": "r1", "s/c": "r2"})}
    assert unreferenced(snaps, heads) == {"old", "other"}


def test_unreferenced_self_resolves_per_head_across_configs() -> None:
    snaps = [_ref(i, None, n) for n, i in enumerate(["h1", "h2", "x", "y"])]
    heads = {"h1": _m({"s/a": "y"}), "h2": _m({"s/a": SELF})}
    # keys are head snapshot ids: SELF means h2 itself, never some other head
    assert unreferenced(snaps, heads) == {"x"}


def test_unreferenced_without_heads_is_everything() -> None:
    snaps = [_ref("a", None, 1), _ref("b", "a", 2)]
    assert unreferenced(snaps, {}) == {"a", "b"}


@st.composite
def _forests(draw: st.DrawFn) -> list[SnapRef]:
    n = draw(st.integers(0, 10))
    refs: list[SnapRef] = []
    for i in range(n):
        parent = draw(st.one_of(st.none(), st.integers(0, max(i - 1, 0)))) if i else None
        refs.append(SnapRef(f"{i:02d}", None if parent is None else f"{parent:02d}", _t(draw(st.integers(0, 3)))))
    return refs


@given(_forests(), st.randoms())
def test_select_head_invariant_under_permutation(snaps: list[SnapRef], rnd: random.Random) -> None:
    shuffled = list(snaps)
    rnd.shuffle(shuffled)
    assert select_head(shuffled) == select_head(snaps)
    assert losers(shuffled) == losers(snaps)


@given(_forests(), st.randoms())
def test_head_is_never_a_loser(snaps: list[SnapRef], rnd: random.Random) -> None:
    head = select_head(snaps)
    assert (head is None) == (not snaps)
    if head is not None:
        assert head not in losers(snaps)
        assert head in {s.id for s in snaps}


@given(_forests(), st.lists(st.integers(0, 12), max_size=6))
def test_unreferenced_disjoint_from_heads_and_referenced(snaps: list[SnapRef], refs: list[int]) -> None:
    head = select_head(snaps)
    manifest = _m({f"s/{k}": f"{r:02d}" for k, r in enumerate(refs)})
    result = unreferenced(snaps, {} if head is None else {head: manifest})
    assert result.isdisjoint({f"{r:02d}" for r in refs})
    assert head not in result
    assert result == {s.id for s in snaps} - {head} - {f"{r:02d}" for r in refs}
