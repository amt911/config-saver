"""Pure three-way classification for multi-machine sync.

Compares base (last synced), local and remote per path and decides whether to
pull, publish or abort on conflict. No IO: everything is a dict of fingerprints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple


@dataclass(frozen=True)
class Fingerprint:
    """Identity of one synced entry.

    For symlinks `sha256` is the hash of the target text and `link` is the target.
    """

    sha256: str
    mode: int
    link: str | None = None


Side = dict[str, Fingerprint]


class CacheEntry(NamedTuple):
    """One remembered hash: the stat tuple it was computed under, and the fingerprint.

    Valid only while every stat field and ``normalize_content`` still match. ``ctime_ns``
    is the one a writer cannot forge (utime sets mtime, never ctime), so a same-size,
    same-mtime edit still invalidates the entry.
    """

    size: int
    mtime_ns: int
    ctime_ns: int
    inode: int
    normalize_content: bool
    fingerprint: Fingerprint


HashCache = dict[str, CacheEntry]


@dataclass
class Plan:
    take_remote: list[str] = field(default_factory=list)
    delete_local: list[str] = field(default_factory=list)
    publish: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def classify(
    base: Side,
    local: Side,
    remote: Side,
    local_scope: set[str],
    remote_scope: set[str],
) -> Plan:
    """Classify every path in `local_scope`; other paths never appear in the plan.

    A remote absence is a deletion only when the path is in `remote_scope`;
    otherwise the remote machine never managed it and `R := B`.
    """
    plan = Plan()
    candidates = (set(base) | set(local) | set(remote)) & local_scope
    for path in sorted(candidates):
        b = base.get(path)
        loc = local.get(path)
        r = remote.get(path)
        if r is None and path not in remote_scope:
            r = b
        if loc == r:
            continue
        if loc == b:
            (plan.delete_local if r is None else plan.take_remote).append(path)
        elif r == b:
            plan.publish.append(path)
        else:
            plan.conflicts.append(path)
    return plan
