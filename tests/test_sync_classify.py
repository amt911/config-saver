"""Three-way classifier: pure, so examples plus Hypothesis properties."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from config_saver.lib.sync.classify import Fingerprint, Plan, classify


def fp(tag: str, mode: int = 0o644, link: str | None = None) -> Fingerprint:
    return Fingerprint(sha256=tag, mode=mode, link=link)


A, B, C = fp("a"), fp("b"), fp("c")
P = "home/u/.zshrc"
SCOPE = {P}


def run(base, local, remote, local_scope=SCOPE, remote_scope=SCOPE) -> Plan:
    def side(e):
        return {} if e is None else {P: e}

    return classify(side(base), side(local), side(remote), set(local_scope), set(remote_scope))


def test_all_equal_nothing():
    assert run(A, A, A) == Plan([], [], [], [])


def test_both_absent_nothing():
    assert run(None, None, None) == Plan([], [], [], [])


def test_local_equals_remote_both_changed_nothing():
    assert run(A, B, B) == Plan([], [], [], [])


def test_both_deleted_nothing():
    assert run(A, None, None) == Plan([], [], [], [])


def test_remote_changed_takes_remote():
    assert run(A, A, B) == Plan([P], [], [], [])


def test_remote_new_file_takes_remote():
    assert run(None, None, B) == Plan([P], [], [], [])


def test_remote_deleted_deletes_local():
    assert run(A, A, None) == Plan([], [P], [], [])


def test_local_changed_publishes():
    assert run(A, B, A) == Plan([], [], [P], [])


def test_local_new_file_publishes():
    assert run(None, B, None) == Plan([], [], [P], [])


def test_local_deleted_publishes():
    assert run(A, None, A) == Plan([], [], [P], [])


def test_both_changed_differently_conflicts():
    assert run(A, B, C) == Plan([], [], [], [P])


def test_both_added_differently_conflicts():
    assert run(None, B, C) == Plan([], [], [], [P])


def test_local_changed_remote_deleted_conflicts():
    assert run(A, B, None) == Plan([], [], [], [P])


def test_local_deleted_remote_changed_conflicts():
    assert run(A, None, B) == Plan([], [], [], [P])


def test_out_of_scope_remote_path_ignored():
    plan = classify({}, {}, {"home/other/x": A}, SCOPE, {"home/other/x"})
    assert plan == Plan([], [], [], [])


def test_remote_absence_outside_remote_scope_is_not_deletion():
    # Remote machine did not manage P, so its absence says nothing.
    assert run(A, A, None, remote_scope=set()) == Plan([], [], [], [])
    # ...and a local change is then a plain publish, not a conflict.
    assert run(A, B, None, remote_scope=set()) == Plan([], [], [P], [])


def test_file_to_symlink_is_change():
    link = fp("t", mode=0o777, link="/etc/target")
    assert run(A, A, link) == Plan([P], [], [], [])


def test_mode_change_is_change():
    assert run(A, fp("a", 0o600), A) == Plan([], [], [P], [])


def test_plan_lists_sorted():
    names = ["home/u/z", "home/u/a", "home/u/m"]
    base = dict.fromkeys(names, A)
    remote = dict.fromkeys(names, B)
    plan = classify(base, dict(base), remote, set(names), set(names))
    assert plan.take_remote == sorted(names)


fps = st.sampled_from([A, B, C, fp("a", 0o600), fp("d", link="x")])
paths = ["p1", "p2", "p3", "p4"]
sides = st.dictionaries(st.sampled_from(paths), fps)


@given(sides, sides, sides)
def test_conflicts_symmetric(b, lo, r):
    s = set(paths)
    assert classify(b, lo, r, s, s).conflicts == classify(b, r, lo, s, s).conflicts


@given(sides, sides)
def test_equal_sides_no_actions(b, x):
    s = set(paths)
    plan = classify(b, x, dict(x), s, s)
    assert plan == Plan([], [], [], [])


@given(sides, sides)
def test_unchanged_local_never_conflicts_or_publishes(b, r):
    s = set(paths)
    plan = classify(b, dict(b), r, s, s)
    assert plan.conflicts == [] and plan.publish == []


@given(sides, sides, sides, st.sets(st.sampled_from(paths)))
def test_plan_paths_stay_in_local_scope(b, lo, r, local_scope):
    plan = classify(b, lo, r, local_scope, set(paths))
    for lst in (plan.take_remote, plan.delete_local, plan.publish, plan.conflicts):
        assert set(lst) <= local_scope


@given(sides, sides, sides, st.sets(st.sampled_from(paths)))
def test_plan_lists_pairwise_disjoint(b, lo, r, remote_scope):
    plan = classify(b, lo, r, set(paths), remote_scope)
    lists = [plan.take_remote, plan.delete_local, plan.publish, plan.conflicts]
    flat = [p for lst in lists for p in lst]
    assert len(flat) == len(set(flat))
