"""CLI handlers for the multi-machine sync verbs (--sync, --sync-init, --sync-status, --sync-resolve)."""

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

REQUIRED_TOOLS = ("git", "restic", "age")
_HOME_PREFIX = "home/user"

ConfigLoader = Callable[[], Mapping[str, Model]]


def sync_verbs(args: argparse.Namespace) -> list[str]:
    """The sync flags present on the command line."""
    given = []
    if args.sync:
        given.append("--sync")
    if args.sync_init:
        given.append("--sync-init")
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


def _engine(args: argparse.Namespace) -> SyncEngine:
    return SyncEngine(sync_paths.sync_dir(), sync_paths.state_path(), args.identity, socket.gethostname())


def _require_set_up() -> None:
    missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        raise SyncNotSetUpError(f"sync needs {', '.join(missing)} installed and on PATH")
    if not os.path.isdir(os.path.join(sync_paths.sync_dir(), ".git")):
        raise SyncNotSetUpError(
            "sync is not set up: run config-saver --sync-init <git-url> --encrypt-to <age recipient>"
        )


def handle(args: argparse.Namespace, load_configs: ConfigLoader) -> int:
    """Run the one sync verb requested; exit codes are mapped by CLI.run()."""
    if len(sync_verbs(args)) > 1:
        print(Fore.RED + "Use only one of --sync, --sync-init, --sync-status and --sync-resolve.")
        return EXIT_USAGE
    if args.sync_init:
        recipients = args.encrypt_to or []
        if len(recipients) > 1:
            print(Fore.RED + "--sync-init takes at most one --encrypt-to recipient.")
            return EXIT_USAGE
        _engine(args).init(args.sync_init, recipients[0] if recipients else None)
        print(Fore.GREEN + f"Sync is set up in {sync_paths.sync_dir()}. Mark configurations with 'sync: true'.")
        return EXIT_OK
    _require_set_up()
    engine = _engine(args)
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
