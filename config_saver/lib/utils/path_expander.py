"""Module providing path expansion utilities"""

from __future__ import annotations

import glob
import os
import re

# ${BEGINS_WITH="..."} / ${ENDS_WITH="..."} placeholders, resolved against the
# directory that precedes them in the path.
_TOKEN_RE = re.compile(r"\$\{(BEGINS_WITH|ENDS_WITH)=['\"](.+?)['\"]\}")


class PathExpander:
    """Class to expand custom and environment variables in paths.

    Resolution of the advanced placeholders is deterministic: candidates are
    sorted, so the filesystem's directory order never becomes behaviour. When a
    placeholder matches nothing it is left untouched, and the caller (the
    compressor) reports the path as a missing input instead of skipping it
    silently.
    """

    def __init__(self, custom_vars: dict[str, str] | None = None):
        # The dictionary can be customised (mostly for tests).
        if custom_vars is None:
            custom_vars = {
                "HOME": os.path.expanduser("~"),
                "ROOT_HOME": os.path.expanduser("~root"),
                "CONFIG_DIR": os.path.expanduser("~/.config"),
                "SHARE_DIR": os.path.expanduser("~/.local/share"),
                "BIN_DIR": os.path.expanduser("~/.local/bin"),
                "LOCALSHARE_DIR": os.path.expanduser("~/.local/share"),
                "ETC_CONFIG_DIR": "/etc/config-saver/configs",
            }
        self.custom_vars: dict[str, str] = custom_vars
        # Placeholders that matched more than one entry, as (path, [candidates]).
        self.ambiguities: list[tuple[str, list[str]]] = []
        # Placeholders that matched nothing.
        self.unresolved: list[str] = []

    def expand(self, path: str) -> str:
        """Expand custom and environment variables in the given path."""
        for key, value in self.custom_vars.items():
            path = path.replace(f"${key}", value)
        path = os.path.expandvars(path)

        if "${" not in path:
            return path
        return self._expand_placeholders(path)

    def _expand_placeholders(self, path: str) -> str:
        """Resolve every BEGINS_WITH/ENDS_WITH placeholder, left to right."""
        parts = path.split(os.sep)
        resolved: list[str] = []
        for part in parts:
            match = _TOKEN_RE.search(part)
            if match is None:
                resolved.append(part)
                continue

            parent = os.sep.join(resolved) or os.sep
            candidate = self._pick_candidate(parent, match.group(1), match.group(2), path)
            resolved.append(part.replace(match.group(0), candidate) if candidate else part)
        return os.sep.join(resolved)

    def _pick_candidate(self, parent: str, kind: str, needle: str, full_path: str) -> str | None:
        """Return the basename of the single deterministic match, if any."""
        entries = sorted(os.path.basename(p) for p in glob.glob(os.path.join(parent, "*")))
        if kind == "BEGINS_WITH":
            candidates = [name for name in entries if name.startswith(needle)]
        else:
            candidates = [name for name in entries if name.endswith(needle)]

        if not candidates:
            self.unresolved.append(full_path)
            return None
        if len(candidates) > 1:
            self.ambiguities.append((full_path, candidates))
        return candidates[0]
