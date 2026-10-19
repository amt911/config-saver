"""Store setup checks and manual pruning: the work behind ``--restic-init`` and ``--prune``.

Returns structured results; the CLI renders them and picks exit codes.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field

from pydantic import ValidationError

from .backup_manager.backup_manager import LEGACY_NAME, BackupManager
from .errors import StoreError
from .models.retention_model import RetentionModel
from .parser.parser import Parser
from .restic_store import ResticStore, repo_path
from .tar_prune import prune_config_dir

# Plugin recipients (age1yubikey1…, age1tpm1…) have another human-readable part, so a '1'/'b'/'i'/'o'
# (not in the bech32 charset) follows the separator.
_NATIVE_RECIPIENT = re.compile(r"age1[02-9ac-hj-np-z]+")


def check_identity_matches(identity: str, recipient: str) -> str | None:
    """None when ``identity`` is the private half of ``recipient``, else why not.

    Only native age recipients (bech32 prefix exactly ``age``) can be compared; others (ssh keys, plugins) pass.
    """
    if not _NATIVE_RECIPIENT.fullmatch(recipient):
        return None
    if not os.path.isfile(identity):
        return f"identity file not found: {identity} (pass --identity <key file>)"
    try:
        done = subprocess.run(["age-keygen", "-y", identity], capture_output=True, check=False)
    except OSError as exc:
        raise StoreError(f"could not run age-keygen: {exc}") from exc
    if done.returncode != 0:
        return f"{identity} is not a readable age identity: {done.stderr.decode('utf-8', 'replace').strip()}"
    derived = {line.strip() for line in done.stdout.decode("utf-8", "replace").splitlines() if line.strip()}
    if recipient not in derived:
        return (
            f"{identity} is the key for {', '.join(sorted(derived)) or 'nothing'}, not {recipient}: "
            "the store would be unreadable with it. "
            "Pass the matching --identity or the right --encrypt-to."
        )
    return None


def store_size(saves_dir: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(repo_path(saves_dir)):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def retention_by_config(
    manager: BackupManager, inputs: Iterable[str]
) -> tuple[dict[str, RetentionModel | None], dict[str, str]]:
    """name -> its ``retention:`` (None when absent) per configuration found; plus unreadable ones."""
    known: dict[str, RetentionModel | None] = {}
    errors: dict[str, str] = {}
    paths_by_name: dict[str, str] = {}
    for entry in inputs:
        found = manager.find_config_files(entry) if os.path.isdir(entry) else [entry]
        for path in found:
            paths_by_name[os.path.splitext(os.path.basename(path))[0]] = path  # later levels override earlier
    for name, path in sorted(paths_by_name.items()):
        try:
            known[name] = Parser(path).get_model().retention
        except (ValidationError, OSError, ValueError) as exc:
            errors[name] = f"cannot read {path}: {exc}"
    return known, errors


@dataclass
class PruneOutcome:
    # cfg -> what was (or would be) removed, one label per backup
    removed: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.removed.values())


def prune_all(
    saves_dir: str,
    store: ResticStore | None,
    known: dict[str, RetentionModel | None],
    unreadable: dict[str, str],
    dry_run: bool,
    include_legacy: bool,
) -> PruneOutcome:
    """Apply retention to every configuration that has backups (restic) or archive directories (tar)."""
    outcome = PruneOutcome(errors=[f"[{n}] {why}" for n, why in unreadable.items()])
    if store is not None:
        names = sorted({s.cfg for s in store.snapshots()})
    else:
        root = os.path.join(saves_dir, "configs")
        names = sorted(os.listdir(root)) if os.path.isdir(root) else []
    for cfg in names:
        if cfg == LEGACY_NAME and not include_legacy:
            outcome.notes.append(f"[{cfg}] skipped (pass --include-legacy to prune it).")
            continue
        if cfg in unreadable:
            continue
        retention = known.get(cfg)
        try:
            if store is not None:
                if cfg not in known:
                    outcome.notes.append(f"[{cfg}] no configuration file found: the default retention applies.")
                gone = store.forget(cfg, retention or RetentionModel(), dry_run)
                labels = [f"{s.time:%Y-%m-%d %H:%M:%S} ({s.short_id})" for s in gone]
            elif retention is not None:
                dirs = prune_config_dir(os.path.join(saves_dir, "configs", cfg), retention, dry_run)
                labels = [os.path.basename(d) for d in dirs]
            else:
                why = "no retention: block" if cfg in known else "no configuration file"
                outcome.notes.append(f"[{cfg}] left alone: {why} (archive mode only prunes explicit retention).")
                continue
        except (StoreError, OSError) as exc:
            outcome.errors.append(f"[{cfg}] could not prune: {exc}")
            continue
        if labels:
            outcome.removed[cfg] = labels
    if store is not None and outcome.total and not dry_run:
        try:
            store.prune()
        except StoreError as exc:
            outcome.errors.append(f"could not reclaim space (restic prune): {exc}")
    return outcome
