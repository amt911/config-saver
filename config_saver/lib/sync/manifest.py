"""Sync manifest: per-file fingerprints plus which snapshot holds each content.

The manifest travels inside every encrypted sync snapshot. Its JSON is untrusted
input when read back, so `from_json` validates every type, path and identifier.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from config_saver.lib.errors import ArchiveError, StoreError
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
    if path.startswith("/") or ".." in path.split("/"):
        raise _bad(f"unsafe path {path!r}")
    return path


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
            doc = json.loads(data)
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


def find_head(snaps: list[tuple[str, str | None]], cfg: str) -> str | None:
    """Return the one snapshot id no other snapshot names as parent."""
    if not snaps:
        return None
    parents = {parent for _, parent in snaps if parent is not None}
    heads = [sid for sid, _ in snaps if sid not in parents]
    if len(heads) != 1:
        raise StoreError(f"sync history of {cfg} forked")
    return heads[0]
