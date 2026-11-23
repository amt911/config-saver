"""Pure three-way classification for multi-machine sync.

Compares base (last synced), local and remote per path and decides whether to
pull, publish or abort on conflict. No IO: everything is a dict of fingerprints.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Fingerprint:
    """Identity of one synced entry. `link` is the symlink target, if any."""

    sha256: str
    mode: int
    link: str | None = None


Side = dict[str, Fingerprint]


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
        l = local.get(path)  # noqa: E741
        r = remote.get(path)
        if r is None and path not in remote_scope:
            r = b
        if l == r:
            continue
        if l == b:
            (plan.delete_local if r is None else plan.take_remote).append(path)
        elif r == b:
            plan.publish.append(path)
        else:
            plan.conflicts.append(path)
    return plan
