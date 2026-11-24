import json
from datetime import datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from config_saver.lib.errors import ArchiveError
from config_saver.lib.sync.classify import Fingerprint
from config_saver.lib.sync.manifest import (
    SELF,
    Manifest,
    SnapRef,
    forgettable,
    losers,
    select_head,
    unreferenced,
    winning_chain,
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


T0 = datetime(2026, 1, 1)
DAY = timedelta(days=1)
GRACE = timedelta(hours=24)
RETENTION = timedelta(days=30)


def _t(n: int) -> datetime:
    return T0 + timedelta(seconds=n)


def _ref(i: str, parent: str | None, n: int, gen: int = 0) -> SnapRef:
    return SnapRef(i, parent, _t(n), gen)


def test_select_head_empty() -> None:
    assert select_head([]) is None
    assert losers([]) == set()
    assert winning_chain([]) == []


def test_select_head_linear_chain_any_order() -> None:
    snaps = [_ref("c", "b", 3, 2), _ref("a", None, 1, 0), _ref("b", "a", 2, 1)]
    assert select_head(snaps) == "c"
    assert losers(snaps) == set()
    assert winning_chain(snaps) == ["a", "b", "c"]


def test_fork_earliest_wins_regardless_of_order() -> None:
    a = _ref("a", None, 1)
    x = _ref("x", "a", 5, 1)
    y = _ref("y", "a", 4, 1)
    assert select_head([a, x, y]) == "y"
    assert select_head([y, x, a]) == "y"
    assert losers([a, x, y]) == {"x"}
    assert winning_chain([a, x, y]) == ["a", "y"]


def test_fork_time_tie_smaller_id_wins() -> None:
    a = _ref("a", None, 1)
    assert select_head([a, _ref("q", "a", 4, 1), _ref("p", "a", 4, 1)]) == "p"


def test_losers_descendants_are_losers() -> None:
    snaps = [
        _ref("a", None, 1, 0),
        _ref("w", "a", 2, 1),
        _ref("l", "a", 3, 1),
        _ref("l2", "l", 4, 2),
        _ref("l3", "l2", 5, 3),
        _ref("w2", "w", 9, 2),
    ]
    assert select_head(snaps) == "w2"
    assert losers(snaps) == {"l", "l2", "l3"}
    assert winning_chain(snaps) == ["a", "w", "w2"]


def test_fork_picks_by_time_not_by_descendant_depth() -> None:
    snaps = [_ref("a", None, 1, 0), _ref("w", "a", 2, 1), _ref("l", "a", 3, 1), _ref("l2", "l", 4, 2)]
    assert select_head(snaps) == "w"


def test_a_fork_is_seen_even_when_the_shared_parent_is_forgotten() -> None:
    snaps = [_ref("w", "gone", 2, 5), _ref("l", "gone", 3, 5), _ref("l2", "l", 4, 6)]
    assert losers(snaps) == {"l", "l2"}
    assert select_head(snaps) == "w"  # a deeper loser branch does not win


def test_the_highest_generation_wins_across_gaps() -> None:
    snaps = [_ref("old", None, 1, 0), _ref("new", "forgotten", 9, 7)]
    assert select_head(snaps) == "new"  # the old referenced root is not the head
    assert winning_chain(snaps) == ["old", "new"]


def test_same_generation_candidates_the_earliest_is_the_head() -> None:
    snaps = [_ref("p", "x", 5, 3), _ref("q", "y", 4, 3)]
    assert select_head(snaps) == "q"
    assert winning_chain(snaps) == ["p", "q"]


def test_several_roots_earliest_wins() -> None:
    snaps = [_ref("r2", None, 2, 0), _ref("r1", None, 1, 0), _ref("c1", "r1", 3, 1), _ref("c2", "r2", 4, 1)]
    assert select_head(snaps) == "c1"
    assert losers(snaps) == {"r2", "c2"}
    assert winning_chain(snaps) == ["r1", "c1"]


def test_a_cycle_is_all_losers_or_none_but_terminates() -> None:
    snaps = [_ref("r", None, 1, 0), _ref("a", "b", 2, 1), _ref("b", "a", 3, 2)]
    assert losers(snaps) == set()  # a and b are alone in their sibling groups: generations decide
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
    snaps = [_ref("a", None, 1), _ref("b", "a", 2, 1)]
    assert unreferenced(snaps, {}) == {"a", "b"}


# --------------------------------------------------------------------------- forgettable

LONG_AGO = 400 * 86400  # seconds after T0: everything below is far older than any retention


def _forgettable(snaps: list[SnapRef], heads: dict[str, Manifest], days: float) -> set[str]:
    return forgettable(snaps, heads, T0 + timedelta(days=days), GRACE, RETENTION)


def test_forgettable_superseded_unreferenced_candidate_waits_out_the_grace() -> None:
    snaps = [_ref("a", None, 0, 0), _ref("b", "a", 100, 1), _ref("c", "b", 200, 2)]
    heads = {"c": _m({"s/x": SELF})}
    assert _forgettable(snaps, heads, 0.5) == set()
    assert _forgettable(snaps, heads, 1.5) == {"a", "b"}  # b's successor c is 1.5 days old; a's successor b too


def test_forgettable_grace_boundary_is_exclusive_of_the_cutoff() -> None:
    snaps = [_ref("a", None, 0, 0), _ref("b", "a", 100, 1)]
    heads = {"b": _m({"s/x": SELF})}
    just_before = (_t(100) - T0 + GRACE - timedelta(seconds=1)).total_seconds() / 86400
    exactly = (_t(100) - T0 + GRACE).total_seconds() / 86400
    after = (_t(100) - T0 + GRACE + timedelta(seconds=1)).total_seconds() / 86400
    assert _forgettable(snaps, heads, just_before) == set()
    assert _forgettable(snaps, heads, exactly) == set()
    assert _forgettable(snaps, heads, after) == {"a"}


def test_forgettable_keeps_the_head_and_whatever_it_references() -> None:
    snaps = [_ref("a", None, 0, 0), _ref("b", "a", 100, 1), _ref("c", "b", 200, 2)]
    heads = {"c": _m({"s/x": "a", "s/y": SELF})}
    assert _forgettable(snaps, heads, 50) == {"b"}


def test_forgettable_without_the_head_manifest_keeps_the_head_anyway() -> None:
    snaps = [_ref("a", None, 0, 0), _ref("b", "a", 100, 1)]
    assert _forgettable(snaps, {}, 50) == {"a"}


def test_forgettable_empty() -> None:
    assert _forgettable([], {}, 50) == set()


def _fork() -> list[SnapRef]:
    """p <- w (wins, t=100) and p <- l (loses, t=200) <- l2; w <- h."""
    return [
        _ref("p", None, 0, 0),
        _ref("w", "p", 100, 1),
        _ref("l", "p", 200, 1),
        _ref("l2", "l", 300, 2),
        _ref("h", "w", 400, 2),
    ]


def test_forgettable_a_young_loser_subtree_stays_with_its_base_and_the_fork_winner() -> None:
    heads = {"h": _m({"s/x": SELF})}
    # p is the parent of the retained loser, w is the winner of a fork whose loser is listed
    assert _forgettable(_fork(), heads, 29) == set()


def test_forgettable_an_old_loser_subtree_goes_as_one_unit() -> None:
    heads = {"h": _m({"s/x": SELF})}
    days = 30 + 200 / 86400 + 1e-3
    gone = _forgettable(_fork(), heads, days)
    assert {"l", "l2"} <= gone
    exactly = _forgettable(_fork(), heads, 30 + 200 / 86400)
    assert {"l", "l2"}.isdisjoint(exactly)  # the loser is exactly thirty days old: not older


def test_forgettable_the_unit_stays_whole_when_a_head_references_a_member() -> None:
    heads = {"h": _m({"s/x": SELF, "s/y": "l2"})}
    assert {"l", "l2"}.isdisjoint(_forgettable(_fork(), heads, 100))


def test_forgettable_a_fork_winner_is_released_once_its_loser_is_gone() -> None:
    heads = {"h": _m({"s/x": SELF})}
    remaining = [s for s in _fork() if s.id not in {"l", "l2"}]
    assert "w" in _forgettable(remaining, heads, 100)
    assert "p" in _forgettable(remaining, heads, 100)


def test_forgettable_keeps_the_nearest_candidate_above_a_nested_loser() -> None:
    snaps = [
        _ref("p", None, 0, 0),
        _ref("w", "p", 100, 1),
        _ref("l", "p", 200, 1),
        _ref("m", "l", 300, 2),
        _ref("n", "l", 310, 2),  # loses to m inside l's subtree
        _ref("h", "w", 400, 2),
    ]
    heads = {"h": _m({"s/x": SELF})}
    kept_p = _forgettable(snaps, heads, 29)
    assert "p" not in kept_p and "w" not in kept_p
    assert _forgettable(snaps, heads, 31) >= {"l", "m", "n"}


@st.composite
def _histories(draw: st.DrawFn) -> list[SnapRef]:
    """The full DAG of what several machines published: stale views fork it, clock skew reorders siblings."""
    n = draw(st.integers(1, 14))
    snaps: list[SnapRef] = []
    for i in range(n):
        if snaps and draw(st.integers(0, 9)) > 0:
            parent = draw(st.sampled_from(snaps))
            parent_id, gen = parent.id, parent.gen + 1
        else:
            parent_id, gen = None, 0
        when = i * 3600 + draw(st.integers(-7200, 7200))
        snaps.append(SnapRef(f"{i:03d}", parent_id, _t(when), gen))
    return snaps


def _reference_head(snaps: list[SnapRef]) -> str:
    """The parent-walk rule on a complete history: from the roots, the earliest child at every fork."""
    children: dict[str | None, list[SnapRef]] = {}
    for s in snaps:
        children.setdefault(s.parent, []).append(s)
    level = children[None]
    head = ""
    while level:
        winner = min(level, key=lambda s: (s.time, s.id))
        head = winner.id
        level = children.get(winner.id, [])
    return head


@given(_histories(), st.data())
def test_head_survives_any_amount_of_forgetting(history: list[SnapRef], data: st.DataObject) -> None:
    expected = _reference_head(history)
    assert select_head(history) == expected  # generations agree with the parent walk on a full history
    ids = [s.id for s in history]
    remaining = list(history)
    day = data.draw(st.floats(0, 120))
    for _round in range(3):
        head = select_head(remaining)
        assert head == expected
        referenced = data.draw(st.lists(st.sampled_from([s.id for s in remaining]), max_size=4))
        manifest = _m({f"s/{k}": r for k, r in enumerate(referenced)} | {"s/self": SELF})
        gone = forgettable(remaining, {expected: manifest}, T0 + timedelta(days=day), GRACE, RETENTION)
        assert expected not in gone
        assert gone.isdisjoint(referenced)
        remaining = [s for s in remaining if s.id not in gone]
        assert select_head(remaining) == expected
        day += data.draw(st.floats(0, 60))
    assert {s.id for s in remaining} <= set(ids)
