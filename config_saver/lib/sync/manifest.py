"""Sync manifest: per-file fingerprints plus which snapshot holds each content.

The manifest travels inside every encrypted sync snapshot. Its JSON is untrusted
input when read back, so `from_json` validates every type, path and identifier.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from config_saver.lib.errors import ArchiveError
from config_saver.lib.sync.classify import Fingerprint, Side

MANIFEST_MEMBER = ".config-saver-sync-manifest.json"
SELF = "@"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_TOP_KEYS = {"cfg", "scope", "entries"}
_ENTRY_KEYS = {"sha256", "mode", "link", "snapshot"}


def _bad(msg: str) -> ArchiveError:
    return ArchiveError(f"invalid sync manifest: {msg}")


def _check_path(path: object) -> str:
    if not isinstance(path, str) or not path:
        raise _bad("path must be a non-empty string")
    if "\0" in path or "\\" in path:
        raise _bad(f"unsafe path {path!r}")
    if path.startswith("/") or any(part in ("", ".", "..") for part in path.split("/")):
        raise _bad(f"unsafe path {path!r}")
    return path


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _bad(f"duplicate key {key!r}")
        out[key] = value
    return out


def _check_keys(obj: object, keys: set[str], what: str) -> dict[str, Any]:
    if not isinstance(obj, dict) or set(obj) != keys:
        raise _bad(f"{what} must be an object with exactly keys {sorted(keys)}")
    return obj


def _parse_entry(raw: object) -> tuple[Fingerprint, str]:
    e = _check_keys(raw, _ENTRY_KEYS, "entry")
    sha, mode, link, snap = e["sha256"], e["mode"], e["link"], e["snapshot"]
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        raise _bad("sha256 must be 64 lowercase hex characters")
    if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o7777:
        raise _bad("mode must be an integer in 0..0o7777")
    if link is not None and not isinstance(link, str):
        raise _bad("link must be a string or null")
    if not isinstance(snap, str) or not (snap == SELF or _SHA256.fullmatch(snap)):
        raise _bad("snapshot must be 64 lowercase hex characters or the self marker")
    return Fingerprint(sha, mode, link), snap


@dataclass
class Manifest:
    cfg: str
    scope: list[str]
    entries: dict[str, tuple[Fingerprint, str]]

    def to_json(self) -> bytes:
        doc = {
            "cfg": self.cfg,
            "scope": self.scope,
            "entries": {
                p: {"sha256": fp.sha256, "mode": fp.mode, "link": fp.link, "snapshot": snap}
                for p, (fp, snap) in self.entries.items()
            },
        }
        return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()

    @classmethod
    def from_json(cls, data: bytes) -> Manifest:
        try:
            doc = json.loads(data, object_pairs_hook=_no_duplicates)
        except (ValueError, RecursionError) as exc:  # includes UnicodeDecodeError
            raise _bad("not valid JSON") from exc
        top = _check_keys(doc, _TOP_KEYS, "manifest")
        if not isinstance(top["cfg"], str):
            raise _bad("cfg must be a string")
        if not isinstance(top["scope"], list):
            raise _bad("scope must be a list")
        scope = [_check_path(p) for p in top["scope"]]
        if not isinstance(top["entries"], dict):
            raise _bad("entries must be an object")
        entries = {_check_path(p): _parse_entry(e) for p, e in top["entries"].items()}
        return cls(top["cfg"], scope, entries)

    def resolve(self, own_id: str) -> Manifest:
        return Manifest(
            self.cfg,
            list(self.scope),
            {p: (fp, own_id if s == SELF else s) for p, (fp, s) in self.entries.items()},
        )

    def side(self) -> Side:
        return {p: fp for p, (fp, _) in self.entries.items()}


@dataclass(frozen=True)
class SnapRef:
    """One sync snapshot of a single config: its id, the parent it names, its time and generation.

    ``gen`` is 0 for a root and the parent's ``gen + 1`` otherwise (the ``gen:`` tag): it keeps ordering
    snapshots meaningful after forgetting has removed links from the middle of the chain.
    """

    id: str
    parent: str | None
    time: datetime
    gen: int


def _order(snap: SnapRef) -> tuple[datetime, str]:
    return (snap.time, snap.id)


def _fork_losers(snaps: list[SnapRef]) -> set[str]:
    """Siblings share a parent tag (listed or not); in each group only the earliest `(time, id)` wins."""
    groups: dict[str | None, list[SnapRef]] = {}
    for snap in snaps:
        groups.setdefault(snap.parent, []).append(snap)
    out: set[str] = set()
    for group in groups.values():
        winner = min(group, key=_order)
        out |= {s.id for s in group if s.id != winner.id}
    return out


def _subtrees(snaps: list[SnapRef], roots: set[str]) -> set[str]:
    """``roots`` plus every listed snapshot whose chain of listed parents reaches one of them."""
    children: dict[str | None, list[str]] = {}
    for snap in snaps:
        children.setdefault(snap.parent, []).append(snap.id)
    out: set[str] = set()
    stack = list(roots)
    while stack:
        current = stack.pop()
        if current not in out:
            out.add(current)
            stack.extend(children.get(current, []))
    return out


def winning_chain(snaps: list[SnapRef]) -> list[str]:
    """The candidates (everything not a loser) by ascending generation; the head comes last.

    It may have gaps (forgotten snapshots): that is fine, generations still order it. Among candidates of
    one generation the earliest `(time, id)` comes last, so it is the head when that generation is the top.
    """
    lost = losers(snaps)
    candidates = sorted((s for s in snaps if s.id not in lost), key=_order, reverse=True)
    candidates.sort(key=lambda s: s.gen)  # stable: keeps the reversed (time, id) order inside a generation
    return [s.id for s in candidates]


def select_head(snaps: list[SnapRef]) -> str | None:
    """Head of one config's snapshots: the candidate of the highest generation, or None when there are none."""
    chain = winning_chain(snaps)
    return chain[-1] if chain else None


def losers(snaps: list[SnapRef]) -> set[str]:
    """Ids of fork losers and of everything listed below them."""
    return _subtrees(snaps, _fork_losers(snaps))


def unreferenced(snaps: list[SnapRef], heads: Mapping[str, Manifest]) -> set[str]:
    """Snapshot ids that are neither a head nor referenced by any head manifest.

    `heads` maps each head snapshot id to its manifest; the self marker in a
    manifest resolves to that head's own id.
    """
    keep = set(heads)
    for head_id, manifest in heads.items():
        keep.update(snap for _, snap in manifest.resolve(head_id).entries.values())
    return {s.id for s in snaps} - keep


def forgettable(
    snaps: list[SnapRef], heads: Mapping[str, Manifest], now: datetime, grace: timedelta, retention: timedelta
) -> set[str]:
    """Ids of one config's snapshots that may be forgotten without changing what `select_head` answers.

    * A superseded candidate nothing references goes once the next generation's snapshot is older than
      ``grace`` (a slower machine may still publish on top of it).
    * A fork loser and everything below it go together, once the loser is older than ``retention``; a
      unit a head manifest references stays whole. Younger ones stay, with the nearest candidate above
      them (the base a machine away for weeks needs) .
    * A fork winner stays while a losing sibling is listed: that sibling is the evidence of the fork.
    * Heads, and anything a head manifest references, never go.
    """
    by_id = {s.id: s for s in snaps}
    forks = _fork_losers(snaps)
    lost = _subtrees(snaps, forks)
    kept = set(by_id) - unreferenced(snaps, heads)
    head = select_head(snaps)
    if head is not None:
        kept.add(head)
    old = {i for i in forks if by_id[i].time < now - retention}
    doomed: set[str] = set()
    for root in old:
        unit = _subtrees(snaps, {root})
        if unit.isdisjoint(kept):
            doomed |= unit
    protected = set(kept)
    for root in forks - doomed:
        ancestor = by_id[root].parent
        while ancestor in by_id and ancestor in lost:
            ancestor = by_id[ancestor].parent
        if ancestor in by_id:
            protected.add(ancestor)
    groups: dict[str | None, list[SnapRef]] = {}
    for snap in snaps:
        groups.setdefault(snap.parent, []).append(snap)
    for group in groups.values():
        if len(group) > 1:
            protected.add(min(group, key=_order).id)
    chain = [by_id[i] for i in winning_chain(snaps)]
    for index, snap in enumerate(chain):
        successor = next((later for later in chain[index + 1 :] if later.gen > snap.gen), None)
        if successor is not None and successor.time < now - grace and snap.id not in protected:
            doomed.add(snap.id)
    return doomed
