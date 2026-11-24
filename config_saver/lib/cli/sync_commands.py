"""CLI handlers for the multi-machine sync verbs (--sync, --sync-init, --sync-move, --sync-prune, --sync-status, --sync-resolve)."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
from collections.abc import Callable, Mapping

from colorama import Fore

from config_saver.lib.errors import SyncNotSetUpError, SyncStateError, SyncSystemConfigError, SyncTooLargeError
from config_saver.lib.exit_codes import EXIT_OK, EXIT_SYNC_CONFLICT, EXIT_USAGE
from config_saver.lib.models.model import Model
from config_saver.lib.sync import paths as sync_paths
from config_saver.lib.sync.engine import SyncEngine, SyncReport
from config_saver.lib.sync.remote import SyncRemote, load_remote, save_remote

REQUIRED_TOOLS = ("restic", "age")
_HOME_PREFIX = "home/user"

ConfigLoader = Callable[[], Mapping[str, Model]]


def sync_verbs(args: argparse.Namespace) -> list[str]:
    """The sync flags present on the command line."""
    given = []
    if args.sync:
        given.append("--sync")
    if args.sync_init:
        given.append("--sync-init")
    if args.sync_move:
        given.append("--sync-move")
    if args.sync_prune:
        given.append("--sync-prune")
    if args.sync_status:
        given.append("--sync-status")
    if args.sync_resolve:
        given.append("--sync-resolve")
    return given


def show_path(arcname: str) -> str:
    """An archive member name as the user knows it: ``~/.zshrc`` or ``/etc/hosts``."""
    if arcname == _HOME_PREFIX:
        return "~"
    if arcname.startswith(_HOME_PREFIX + "/"):
        return "~/" + arcname[len(_HOME_PREFIX) + 1 :]
    return "/" + arcname


def to_arcname(path: str) -> str:
    """Inverse of show_path, so ``--path`` accepts what the output printed."""
    if path == "~" or path.startswith("~/"):
        return _HOME_PREFIX + path[1:]
    home = os.path.expanduser("~").rstrip(os.sep)
    if path == home or path.startswith(home + os.sep):
        return _HOME_PREFIX + path[len(home) :]
    return path.lstrip("/") if path.startswith("/") else path


def print_conflicts(conflicts: Mapping[str, list[str]]) -> None:
    for cfg, items in conflicts.items():
        for path in items:
            print(Fore.RED + f"{cfg}: {show_path(path)}")
    print("Resolve with: config-saver --sync-resolve local|remote [--path P]")


def _print_report(report: SyncReport, *, dry: bool) -> None:
    if dry:
        if not (report.applied or report.deleted or report.published or report.conflicts):
            print("Nothing to sync.")
        verbs = (
            (report.applied, "take remote"),
            (report.deleted, "delete locally"),
            (report.published, "publish"),
        )
        for table, verb in verbs:
            for cfg, items in table.items():
                for path in items:
                    print(f"{cfg}: {show_path(path)} ({verb})")
    else:
        for cfg in sorted({*report.applied, *report.deleted, *report.published}):
            print(
                Fore.GREEN + f"{cfg}: applied {len(report.applied.get(cfg, []))}, "
                f"deleted {len(report.deleted.get(cfg, []))}, published {len(report.published.get(cfg, []))}"
            )
        if not (report.applied or report.deleted or report.published):
            print("Nothing to sync.")
    if report.skipped_remote:
        print(Fore.YELLOW + "skipped (no local config): " + ", ".join(report.skipped_remote))


def _engine(args: argparse.Namespace, remote: SyncRemote) -> SyncEngine:
    return SyncEngine(
        remote,
        args.identity,
        sync_paths.state_path(),
        sync_paths.sync_dir(),
        sync_paths.lock_path(),
        socket.gethostname(),
    )


def _require_tools(*remotes: SyncRemote) -> None:
    """restic and age always; rclone when any of the given remotes goes through it."""
    needed = list(REQUIRED_TOOLS)
    if any(r.needs_rclone for r in remotes):
        needed.append("rclone")
    missing = [tool for tool in needed if shutil.which(tool) is None]
    if missing:
        raise SyncNotSetUpError(f"sync needs {', '.join(missing)} installed and on PATH")


def _parse_options(given: list[str] | None) -> tuple[str, ...] | None:
    """``--sync-option K=V`` values; None (after saying why) when one is malformed."""
    for option in given or []:
        key, sep, _value = option.partition("=")
        if not sep or not key.strip():
            print(Fore.RED + f"--sync-option takes KEY=VALUE, got {option!r}.")
            return None
    return tuple(given or [])


def _init(args: argparse.Namespace, remote: SyncRemote) -> int:
    path = sync_paths.remote_config_path()
    try:
        current: SyncRemote | None = load_remote(path)
    except SyncNotSetUpError:
        current = None
    if current is not None and current.repository != remote.repository:
        print(
            Fore.RED + f"Sync is already set up against {current.repository}. "
            f"To switch repositories (and carry the history) use: config-saver --sync-move {remote.repository}"
        )
        return EXIT_USAGE
    if current is not None and not args.sync_option:
        remote = current  # the same repository again, no options given: keep what is stored, never wipe it
    _require_tools(remote)
    outcome = _engine(args, remote).init()
    save_remote(path, remote)  # only once the repository is usable
    print(Fore.GREEN + f"Sync repository {outcome}. Mark configurations with 'sync: true'.")
    return EXIT_OK


def _move(args: argparse.Namespace, new: SyncRemote) -> int:
    path = sync_paths.remote_config_path()
    old = load_remote(path)
    if old.repository == new.repository:
        print(Fore.RED + f"Sync already uses {old.repository}: nothing to move.")
        return EXIT_USAGE
    _require_tools(old, new)
    # sync.json switches inside the engine's lock, right after the copy was verified
    copied = _engine(args, old).move(new, on_verified=lambda: save_remote(path, new))
    print(
        Fore.GREEN + f"Moved {copied} sync snapshot(s) to {new.repository}. Every machine runs: "
        f"config-saver --sync-move {new.repository}"
    )
    return EXIT_OK


def handle(args: argparse.Namespace, load_configs: ConfigLoader) -> int:
    """Run the one sync verb requested; exit codes are mapped by CLI.run()."""
    if len(sync_verbs(args)) > 1:
        print(
            Fore.RED + "Use only one of --sync, --sync-init, --sync-move, --sync-prune, --sync-status "
            "and --sync-resolve."
        )
        return EXIT_USAGE
    options = _parse_options(args.sync_option)
    if options is None:
        return EXIT_USAGE
    if args.sync_option and not (args.sync_init or args.sync_move):
        print(Fore.RED + "--sync-option only applies to --sync-init and --sync-move.")
        return EXIT_USAGE
    if args.sync_init:
        return _init(args, SyncRemote(args.sync_init, options))
    if args.sync_move:
        return _move(args, SyncRemote(args.sync_move, options))
    _require_tools()
    remote = load_remote(sync_paths.remote_config_path())
    _require_tools(remote)
    engine = _engine(args, remote)
    if args.sync_prune:
        forgotten = engine.prune(load_configs())
        print(Fore.GREEN + f"Forgot {len(forgotten)} unreferenced sync snapshot(s) and reclaimed their space.")
        return EXIT_OK
    if args.sync_status:
        report = engine.status(load_configs())
        _print_report(report, dry=True)
        if report.conflicts:
            print_conflicts(report.conflicts)
            return EXIT_SYNC_CONFLICT
        return EXIT_OK
    if args.sync_resolve:
        chosen = [to_arcname(p) for p in args.path] if args.path else None
        configs = load_configs()  # outside the try: a ValidationError is a ValueError too
        try:
            report = engine.resolve(configs, args.sync_resolve, chosen)
        except (SyncTooLargeError, SyncSystemConfigError, SyncStateError):
            raise
        except ValueError as exc:  # an unknown --path
            print(Fore.RED + str(exc))
            return EXIT_USAGE
    else:
        report = engine.run(load_configs())  # a conflict raises SyncConflictError
    _print_report(report, dry=False)
    return EXIT_OK
