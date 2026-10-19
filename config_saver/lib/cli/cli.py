#!/usr/bin/env python3
"""Command line interface for config-saver.

All rendering lives here: the library layers return structured results and this
module turns them into terminal output and stable exit codes.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import os
import re
import shutil
import sys
from collections.abc import Iterator
from datetime import datetime

from colorama import Fore, init
from pydantic import ValidationError
from rich.align import Align
from rich.columns import Columns
from rich.console import Console
from rich.table import Table

from config_saver import __version__
from config_saver.lib import crypto, maintenance, paths
from config_saver.lib.archive_names import FILENAME_PATTERN
from config_saver.lib.backup_manager.backup_manager import BackupManager, BatchResult, PruneReport
from config_saver.lib.errors import ArchiveError, EncryptionError, RootRequiredError, StoreError, UnsafeArchiveError
from config_saver.lib.migrate import MigrationReport, migrate_archives
from config_saver.lib.models.encryption_model import EncryptionModel
from config_saver.lib.parser.parser import Parser
from config_saver.lib.restic_store import NOTICES, ResticStore, Snapshot, StorageMode, select_mode
from config_saver.lib.tar_compressor.tar_compressor import CompressResult
from config_saver.lib.tar_compressor.tar_decompressor import TarDecompressor
from config_saver.lib.tar_prune import TIMESTAMP_FORMAT

init(autoreset=True)

# Stable exit codes (documented in README.md).
EXIT_OK = 0
EXIT_NOT_FOUND = 2
EXIT_VALIDATION = 3
EXIT_PERMISSION = 4
EXIT_RUNTIME = 5
EXIT_USAGE = 6
EXIT_NO_SUCH_CONFIG = 7
EXIT_INCOMPLETE = 8
EXIT_ENCRYPTION = 9
EXIT_IO = 10


class BackupTable:
    """Helper to collect backup archives and render a table of dates."""

    FILENAME_PATTERN = FILENAME_PATTERN

    def __init__(self, saves_dir: str):
        self.saves_dir = saves_dir
        self.user_saves = os.path.expanduser("~/.local/share/config-saver/saves")
        self.manager = BackupManager(saves_dir)

    def _gather_files(self) -> list[str]:
        return self.manager.list_archives()

    def _parse_ts(self, path: str) -> datetime:
        """Return the backup timestamp encoded in the file name.

        The timestamp is group 2 of the pattern; group 1 is the config name.
        Only a name that does not carry a timestamp falls back to the mtime,
        which is "when the file was last touched", not "when the backup ran".
        """
        name = os.path.basename(path)
        m = self.FILENAME_PATTERN.search(name)
        if m:
            try:
                return datetime.strptime(m.group(2), "%Y%m%d-%H%M%S")
            except ValueError:
                print(Fore.YELLOW + f"Warning: unparsable backup timestamp in '{name}', using file mtime.")
        return datetime.fromtimestamp(os.path.getmtime(path))

    def render(self, snapshots: list[Snapshot] | None = None) -> None:
        """Print every saved backup; ``snapshots`` (restic mode) join the tar archives still on disk."""
        files = self._gather_files()
        if not files and not snapshots:
            print(f"No config-saver tar.gz files found in {self.saves_dir} or {self.user_saves}.")
            return

        # config name -> [(when, store kind, description)]
        grouped: dict[str, list[tuple[datetime, str, str | None]]] = {}
        for f in files:
            name = os.path.basename(f)
            m = self.FILENAME_PATTERN.match(name)
            cfgname = m.group(1) if m else os.path.splitext(name)[0]
            desc = self.manager.get_description_for_archive(f)
            grouped.setdefault(cfgname, []).append((self._parse_ts(f), "tar", desc))
        for snap in snapshots or []:
            grouped.setdefault(snap.cfg, []).append((snap.time, "restic", snap.description))

        console = Console()
        tables: list[Table] = []
        mixed = snapshots is not None

        for cfgname, entries in grouped.items():
            table = Table(
                show_header=True,
                header_style="bold bright_blue",
                row_styles=["none", "dim"],
                title=cfgname,
                title_style="bold magenta",
            )
            table.add_column("No.", width=5, justify="center", no_wrap=True)
            table.add_column("Date", overflow="fold", justify="center", no_wrap=True)
            if mixed:
                table.add_column("Store", justify="center", no_wrap=True)
            table.add_column(Align.center("Description"), overflow="fold", justify="left")

            for i, (when, kind, desc) in enumerate(sorted(entries, key=lambda e: e[0], reverse=True), start=1):
                preview = "" if not desc else (desc if len(desc) <= 60 else desc[:57] + "...")
                cells = [str(i), when.strftime("%Y-%m-%d %H:%M:%S")]
                if mixed:
                    cells.append(kind)
                table.add_row(*cells, preview)
            tables.append(table)

        console.rule("Saved configurations")
        console.print(Columns(tables, expand=False, padding=(0, 2), equal=False))
        if mixed and any(kind == "tar" for entries in grouped.values() for _, kind, _ in entries):
            console.print("tar = full archive on disk, not migrated to the restic store yet", style="yellow")
        console.rule()


def default_config_dirs() -> list[str]:
    """The active configuration levels, least specific first.

    System policy and the user's own configurations are merged (see
    config_saver.lib.paths); the shipped examples are deliberately absent, so
    installing a package never decides what a machine backs up.
    """
    return [d for d in (paths.SYSTEM_CONFIG_DIR, paths.user_config_dir()) if os.path.isdir(d)]


class CLI:
    """Orchestrates CLI parsing and actions for config-saver."""

    # Default to the directory containing multiple YAML configs
    DEFAULT_SYSTEM_CONFIG = "/etc/config-saver/configs"

    def __init__(self, argv: list[str] | None = None):
        self.argv = argv

    def parse_args(self) -> argparse.Namespace:
        parser = argparse.ArgumentParser(description="Tar compressor/decompressor utility", prog="config-saver")
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--compress",
            "-c",
            action="store_true",
            help="Compress files/directories from a config",
        )
        group.add_argument("--decompress", "-d", action="store_true", help="Decompress a tar file")
        group.add_argument(
            "--list",
            "-l",
            action="store_true",
            help="List saved config-saver tar.gz files",
        )
        group.add_argument(
            "--export-config",
            "-e",
            type=str,
            metavar="NAME",
            help="Export the latest config archive by name",
        )
        group.add_argument(
            "--export-all-configs",
            action="store_true",
            help="Export the latest archive for every saved configuration",
        )
        group.add_argument(
            "--show-configs",
            action="store_true",
            help="Show available configuration names",
        )
        group.add_argument(
            "--restic-init",
            action="store_true",
            help="Create the deduplicated restic store (needs --encrypt-to <age recipient>)",
        )
        group.add_argument(
            "--migrate",
            action="store_true",
            help="Move existing tar archives into the restic store (see --delete-migrated)",
        )
        group.add_argument(
            "--prune",
            action="store_true",
            help="Apply retention to every configuration now (see --dry-run, --include-legacy)",
        )
        parser.add_argument(
            "--delete-migrated",
            action="store_true",
            help="With --migrate: delete each archive once its snapshot is verified byte for byte",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="With --prune: show what would be removed, delete nothing"
        )
        parser.add_argument(
            "--include-legacy",
            action="store_true",
            help="With --prune: also prune the migrated top-level archives (config 'legacy')",
        )
        parser.add_argument(
            "--input",
            "-i",
            type=str,
            action="append",
            default=None,
            metavar="PATH",
            help=(
                "Input YAML/JSON config or config directory (compress), or tar file (decompress). "
                "Repeatable in directory mode, to combine e.g. the system directory with your own"
            ),
        )
        parser.add_argument(
            "--output",
            "-o",
            type=str,
            default=None,
            help="Output tar file (compress) or extraction directory (decompress, optional)",
        )
        parser.add_argument("--progress", "-P", action="store_true", help="Show a progress bar")
        parser.add_argument(
            "--description",
            "-m",
            type=str,
            default=None,
            help="Optional description saved alongside the archive",
        )
        parser.add_argument(
            "--jobs",
            "-j",
            type=str,
            default="auto",
            metavar="N",
            help=(
                "Compress N configurations in parallel in directory mode. Default 'auto' "
                "(one worker per CPU, capped at the number of configurations); use 1 to force sequential"
            ),
        )
        parser.add_argument(
            "--encrypt-to",
            action="append",
            metavar="RECIPIENT",
            default=None,
            help="Encrypt the archive for this recipient (age public key or gpg key id). Repeatable",
        )
        parser.add_argument(
            "--encrypt-method",
            choices=sorted(crypto.SUFFIXES),
            default=None,
            help="Backend for --encrypt-to (default: age)",
        )
        parser.add_argument(
            "--identity",
            type=str,
            default=None,
            help="Key file used to decrypt an encrypted archive (required for age)",
        )
        parser.add_argument(
            "--include-system-configs",
            action="store_true",
            help=(
                f"Also archive {paths.SYSTEM_CONFIG_DIR} (compress). Off by default: that directory "
                "usually belongs to a declarative installer, which rebuilds it itself"
            ),
        )
        parser.add_argument(
            "--restore-system-configs",
            action="store_true",
            help=(
                f"Restore members that land in {paths.SYSTEM_CONFIG_DIR} (decompress). Off by "
                "default, so a restore cannot make the machine diverge from its installer"
            ),
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help=f"Exit with {EXIT_INCOMPLETE} when a configured path was missing from the backup",
        )
        parser.add_argument(
            "--version",
            "-v",
            action="version",
            version=f"%(prog)s {__version__}",
            help="Show program version and exit",
        )
        return parser.parse_args(self.argv)

    # ------------------------------------------------------------- rendering

    @staticmethod
    def _encryption_override(args: argparse.Namespace) -> EncryptionModel | None:
        """Build the encryption settings the command line asks for, if any.

        Returning None means "whatever the configuration file says", so a config
        that declares `encrypt:` keeps working without any flag.
        """
        if not args.encrypt_to:
            if args.encrypt_method:
                raise ValueError("--encrypt-method needs at least one --encrypt-to RECIPIENT.")
            return None
        return EncryptionModel(method=args.encrypt_method or "age", recipients=list(args.encrypt_to))

    @staticmethod
    def _report_no_configurations() -> int:
        """Explain what to do instead of backing up whatever happens to exist."""
        print(Fore.RED + "No configurations found.")
        print(Fore.YELLOW + f"  system policy : {paths.SYSTEM_CONFIG_DIR}")
        print(Fore.YELLOW + f"  yours         : {paths.user_config_dir()}")
        print(Fore.YELLOW + f"  examples      : {paths.example_config_dir()} (never used on their own)")
        print(
            Fore.YELLOW + "Copy an example to your own directory to get started:\n"
            f"  mkdir -p {paths.user_config_dir()} && "
            f"cp {paths.example_config_dir()}/zsh.yaml {paths.user_config_dir()}/"
        )
        return EXIT_USAGE

    @staticmethod
    def _collect_configs(
        manager: BackupManager,
        directories: list[str],
        *,
        allow_override: bool = False,
    ) -> list[str]:
        """Every configuration across the given directories, in a stable order.

        With allow_override — the layered default, system policy then the user's
        own — a later directory replaces an earlier configuration of the same
        name: that is what makes ~/.config/.../zsh.yaml win over
        /etc/.../zsh.json instead of both running.

        Without it (directories named explicitly on the command line, where no
        precedence is defined) a duplicate name is an error: both would write to
        <saves>/configs/<name>/<timestamp>/ and the second would quietly
        overwrite the first.
        """
        by_name: dict[str, str] = {}
        for directory in directories:
            found = manager.find_config_files(directory)
            if not found and not allow_override:
                raise ValueError(f"No YAML/JSON configuration files found in {directory}.")
            for path in found:
                name = os.path.splitext(os.path.basename(path))[0]
                if name in by_name and not allow_override:
                    raise ValueError(
                        f"Two configurations are both named '{name}': {by_name[name]} and {path}. "
                        "Rename one, or they would overwrite each other's archive."
                    )
                by_name[name] = path
        return [by_name[name] for name in sorted(by_name)]

    @staticmethod
    def _resolve_jobs(raw: str) -> int:
        """Turn the --jobs value into a worker count (the manager caps it again)."""
        if raw == "auto":
            return os.cpu_count() or 1
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"--jobs expects a positive integer or 'auto', got '{raw}'") from exc
        if value < 1:
            raise ValueError(f"--jobs must be >= 1, got {value}")
        return value

    @staticmethod
    def _report_result(result: CompressResult, label: str = "") -> None:
        prefix = f"[{label}] " if label else ""
        if result.encryption is not None:
            recipients = ", ".join(result.encryption.recipients)
            print(Fore.GREEN + f"{prefix}Encrypted with {result.encryption.method} for: {recipients}")
        if result.excluded:
            # Not a warning: excluded is what the configuration asked for. Still
            # printed, because a backup must never skip anything in silence.
            print(Fore.CYAN + f"{prefix}{result.excluded} path(s) excluded by pattern.")
        if result.missing_inputs:
            print(Fore.YELLOW + f"{prefix}⚠ {len(result.missing_inputs)} configured path(s) were missing:")
            for path in result.missing_inputs[:10]:
                print(Fore.YELLOW + f"    - {path}")
            if len(result.missing_inputs) > 10:
                print(Fore.YELLOW + f"    ... and {len(result.missing_inputs) - 10} more")
        if result.skipped_root_owned:
            print(
                Fore.YELLOW + f"{prefix}⚠ {len(result.skipped_root_owned)} root-owned file(s) were skipped because "
                "'only_root_user' is not set to true."
            )
            print(Fore.YELLOW + "  To include these files, either:")
            print(Fore.YELLOW + "  1. Set 'only_root_user: true' in your config and run with sudo")
            print(Fore.YELLOW + "  2. Change ownership of the files to your user")
            for path in result.skipped_root_owned[:10]:
                print(Fore.YELLOW + f"    - {path}")
            if len(result.skipped_root_owned) > 10:
                print(Fore.YELLOW + f"    ... and {len(result.skipped_root_owned) - 10} more")

    @staticmethod
    def _report_batch(batch: BatchResult) -> None:
        for outcome in batch.outcomes:
            if outcome.archive_path or outcome.snapshot:
                if outcome.snapshot:
                    print(
                        Fore.GREEN
                        + f"Stored snapshot {outcome.snapshot.short_id} of {outcome.name} in the restic store."
                    )
                else:
                    print(Fore.GREEN + f"Compression completed successfully. Output: {outcome.archive_path}")
                if outcome.result:
                    CLI._report_result(outcome.result, label=outcome.name)
            elif outcome.error:
                print(Fore.RED + f"[{outcome.name}] failed: {outcome.error}")

        if batch.skipped_root_only:
            print(
                Fore.YELLOW
                + f"\n⚠ Note: {len(batch.skipped_root_only)} configuration(s) skipped because they require root:"
            )
            for cfg in batch.skipped_root_only:
                print(Fore.YELLOW + f"  - {os.path.basename(cfg)}")
            print(Fore.YELLOW + "  To process these configs, run with: sudo config-saver --compress")

    @staticmethod
    def _report_prune(report: PruneReport) -> None:
        for name, removed in report.removed.items():
            print(Fore.CYAN + f"[{name}] pruned {len(removed)} old backup(s).")
        if report.deferred:
            print(
                Fore.YELLOW + f"automatic prune deferred for {', '.join(report.deferred)}: "
                "unmigrated archives remain; run `config-saver --migrate --delete-migrated`"
            )
        for error in report.errors:
            print(Fore.RED + f"Pruning failed: {error}")

    @staticmethod
    def _print_notice(mode: StorageMode) -> None:
        """Say once, on stderr, why backups are not deduplicated (tar modes only)."""
        notice = NOTICES.get(mode)
        if notice:
            print(Fore.YELLOW + notice, file=sys.stderr)

    # ---------------------------------------------------------------- actions

    def run(self) -> int:
        """Execute the requested action and return the process exit code."""
        args = self.parse_args()

        try:
            return self._dispatch(args)
        except RootRequiredError as e:
            print(Fore.RED + str(e))
            return EXIT_PERMISSION
        except FileNotFoundError as e:
            target = getattr(e, "filename", None) or str(e)
            print(Fore.RED + f"Not found: {target}")
            return EXIT_NOT_FOUND
        except ValidationError as e:
            print(Fore.RED + "Validation error in configuration:")
            print(Fore.RED + str(e))
            return EXIT_VALIDATION
        except PermissionError as e:
            print(Fore.RED + f"Permission error: {e}")
            return EXIT_PERMISSION
        except EncryptionError as e:
            print(Fore.RED + f"Encryption error: {e}")
            return EXIT_ENCRYPTION
        except (UnsafeArchiveError, ArchiveError) as e:
            print(Fore.RED + f"Archive error: {e}")
            return EXIT_RUNTIME
        except StoreError as e:
            print(Fore.RED + str(e))
            return EXIT_RUNTIME
        except RuntimeError as e:
            print(Fore.RED + f"Runtime error: {e}")
            return EXIT_RUNTIME
        except OSError as e:
            print(Fore.RED + f"I/O error: {e}")
            return EXIT_IO

    @staticmethod
    def _misplaced_modifier(args: argparse.Namespace) -> str | None:
        """A modifier flag given without the verb it modifies (silently ignoring it would mislead)."""
        if args.delete_migrated and not args.migrate:
            return "--delete-migrated only applies to --migrate."
        if (args.dry_run or args.include_legacy) and not args.prune:
            return "--dry-run and --include-legacy only apply to --prune."
        return None

    def _dispatch(self, args: argparse.Namespace) -> int:
        misplaced = self._misplaced_modifier(args)
        if misplaced:
            print(Fore.RED + misplaced)
            return EXIT_USAGE
        manager = BackupManager()
        saves_dir = manager.ensure_saves_dir()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        if args.show_configs or args.export_config or args.export_all_configs or args.list:
            # A store that exists but cannot be used raises StoreError: never a silent fallback to tars.
            mode = select_mode(saves_dir)
            store = ResticStore(saves_dir, args.identity) if mode is StorageMode.RESTIC else None
            if args.show_configs:
                return self._show_configs(manager, store)
            if args.export_config:
                if store is not None:
                    return self._export_config_restic(manager, store, args)
                return self._export_config(manager, args)
            if args.export_all_configs:
                if store is not None:
                    return self._export_all_restic(manager, store, args)
                return self._export_all_configs(manager, args)
            BackupTable(saves_dir).render(store.snapshots() if store is not None else None)
            return EXIT_OK
        if args.restic_init:
            return self._restic_init(args, saves_dir)
        if args.migrate:
            return self._migrate(manager, args, saves_dir)
        if args.prune:
            return self._prune(manager, args, saves_dir)
        if args.compress:
            return self._compress(manager, args, saves_dir, timestamp)
        if args.decompress:
            return self._decompress(args)
        return EXIT_OK

    def _show_configs(self, manager: BackupManager, store: ResticStore | None = None) -> int:
        config_names = {
            m.group(1) for m in (FILENAME_PATTERN.match(os.path.basename(p)) for p in manager.list_archives()) if m
        }
        if store is not None:
            config_names |= {s.cfg for s in store.snapshots()}
        if config_names:
            print(Fore.GREEN + "Available configurations:")
            for cfg in sorted(config_names):
                print("- " + cfg)
        else:
            print(Fore.YELLOW + "No saved configurations found.")
        return EXIT_OK

    def _export_config(self, manager: BackupManager, args: argparse.Namespace) -> int:
        cfgname = args.export_config
        matching = [p for p in manager.list_archives() if os.path.basename(p).startswith(cfgname + "-")]
        if not matching:
            print(Fore.RED + f"No saved configuration found with the name: {cfgname}")
            return EXIT_NO_SUCH_CONFIG

        def extract_ts(path: str) -> str:
            m = re.search(r"-(\d{8}-\d{6})\.tar\.gz(?:\.age|\.gpg)?$", os.path.basename(path))
            return m.group(1) if m else "00000000-000000"

        latest = sorted(matching, key=extract_ts, reverse=True)[0]
        dest_path = args.output or os.path.join(os.path.expanduser("~"), os.path.basename(latest))
        shutil.copy2(latest, dest_path)
        print(Fore.GREEN + f"Export completed: {dest_path}")
        print(Fore.YELLOW + "Note: archives are not encrypted and may contain secrets.")
        return EXIT_OK

    def _export_all_configs(self, manager: BackupManager, args: argparse.Namespace) -> int:
        cfg_latest: dict[str, tuple[str, str]] = {}
        for p in manager.list_archives():
            name = os.path.basename(p)
            m = FILENAME_PATTERN.match(name)
            cfg, ts = (m.group(1), m.group(2)) if m else (os.path.splitext(name)[0], "00000000-000000")
            if cfg not in cfg_latest or ts > cfg_latest[cfg][0]:
                cfg_latest[cfg] = (ts, p)

        if not cfg_latest:
            print(Fore.YELLOW + "No saved configurations found.")
            return EXIT_OK

        dest_dir = args.output or os.path.expanduser("~")
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except PermissionError:
            print(Fore.RED + f"Cannot create output directory: {dest_dir}")
            return EXIT_USAGE

        exit_code = EXIT_OK
        for _cfg, (_ts, src_path) in sorted(cfg_latest.items()):
            dest_path = os.path.join(dest_dir, os.path.basename(src_path))
            try:
                shutil.copy2(src_path, dest_path)
                print(Fore.GREEN + f"Exported: {dest_path}")
            except PermissionError:
                print(Fore.RED + f"Permission denied copying {src_path} -> {dest_path}")
                exit_code = EXIT_PERMISSION
        print(Fore.YELLOW + "Note: archives are not encrypted and may contain secrets.")
        return exit_code

    # ------------------------------------------------- exports from the restic store

    @staticmethod
    def _newest_tars(manager: BackupManager) -> dict[str, tuple[str, str]]:
        """cfg name -> (timestamp, path) of the newest tar archive still on disk."""
        newest: dict[str, tuple[str, str]] = {}
        for path in manager.list_archives():
            name = os.path.basename(path)
            m = FILENAME_PATTERN.match(name)
            # Same fallback as the archive-mode export: an odd name stands for itself, oldest possible.
            cfg, ts = (m.group(1), m.group(2)) if m else (os.path.splitext(name)[0], "00000000-000000")
            if cfg not in newest or ts > newest[cfg][0]:
                newest[cfg] = (ts, path)
        return newest

    def _export_encryption(
        self, manager: BackupManager, args: argparse.Namespace, cfgname: str
    ) -> tuple[EncryptionModel | None, str | None]:
        """What to encrypt an export of ``cfgname`` with, plus a warning when nothing says.

        --encrypt-to wins; otherwise the ``encrypt:`` block of the configuration
        file of that name (the user's own level overrides the system one).
        """
        override = self._encryption_override(args)
        if override is not None:
            return override, None
        found: str | None = None
        for directory in default_config_dirs():
            for path in manager.find_config_files(directory):
                if os.path.splitext(os.path.basename(path))[0] == cfgname:
                    found = path
        if found is None:
            return None, f"no configuration file named '{cfgname}' was found, so its encrypt: block is unknown."
        return Parser(found).get_model().encrypt, None

    @staticmethod
    @contextlib.contextmanager
    def _temp_files(*paths: str) -> Iterator[None]:
        """Remove every temp path on exit, whatever happened (keyboard interrupt included)."""
        try:
            yield
        finally:
            for path in paths:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path)

    @staticmethod
    def _export_snapshot(store: ResticStore, snap: Snapshot, dest: str, encryption: EncryptionModel | None) -> str:
        """Write snapshot ``snap`` to ``dest`` as a .tar.gz (encrypted when asked); return the final path.

        The plaintext only ever exists in a 0600 temp file next to the destination,
        which is removed on every path; the result is moved into place atomically.
        """
        if encryption is not None:
            suffix = crypto.suffix_for(encryption.method)
            if not dest.endswith(suffix):
                dest += suffix
        directory, base = os.path.split(os.path.abspath(dest))
        plain_tmp = os.path.join(directory, f".{base}.{os.getpid()}.part")
        enc_tmp = plain_tmp + ".enc"
        with CLI._temp_files(plain_tmp, enc_tmp):
            fd = os.open(plain_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as gz:
                store.dump(snap.id, snap.cfg, gz)  # type: ignore[arg-type]
            ready = plain_tmp
            if encryption is not None:
                crypto.encrypt_file(plain_tmp, enc_tmp, encryption)
                os.unlink(plain_tmp)
                ready = enc_tmp
            os.replace(ready, dest)
        return dest

    def _export_one(
        self,
        manager: BackupManager,
        store: ResticStore,
        args: argparse.Namespace,
        cfg: str,
        snap: Snapshot | None,
        tar: tuple[str, str] | None,
        dest: str | None,
        dest_dir: str,
    ) -> bool:
        """Export the newest backup of ``cfg``; return True when the file is not encrypted."""
        if tar is not None and (snap is None or tar[0] > snap.time.strftime(TIMESTAMP_FORMAT)):
            target = dest or os.path.join(dest_dir, os.path.basename(tar[1]))
            if os.path.isdir(target):
                target = os.path.join(target, os.path.basename(tar[1]))
            shutil.copy2(tar[1], target)
            print(Fore.GREEN + f"Exported: {target}")
            if args.encrypt_to:
                print(
                    Fore.YELLOW + f"Warning: {target} is an archive already on disk and was copied, not re-encrypted."
                )
            return crypto.detect_method(tar[1]) is None
        assert snap is not None
        encryption, warning = self._export_encryption(manager, args, cfg)
        if warning:
            print(Fore.YELLOW + f"Warning: {warning}")
        default_name = f"{cfg}-{snap.time.strftime(TIMESTAMP_FORMAT)}.tar.gz"
        target = dest or os.path.join(dest_dir, default_name)
        if os.path.isdir(target):
            target = os.path.join(target, default_name)
        final = self._export_snapshot(store, snap, target, encryption)
        print(Fore.GREEN + f"Exported: {final}")
        return encryption is None

    def _export_config_restic(self, manager: BackupManager, store: ResticStore, args: argparse.Namespace) -> int:
        try:
            self._encryption_override(args)
        except ValueError as e:
            print(Fore.RED + str(e))
            return EXIT_USAGE
        cfg = args.export_config
        snaps = [s for s in store.snapshots() if s.cfg == cfg]
        tar = self._newest_tars(manager).get(cfg)
        if not snaps and tar is None:
            print(Fore.RED + f"No saved configuration found with the name: {cfg}")
            return EXIT_NO_SUCH_CONFIG
        plain = self._export_one(
            manager, store, args, cfg, snaps[-1] if snaps else None, tar, args.output, os.path.expanduser("~")
        )
        if plain:
            print(Fore.YELLOW + "Note: archives are not encrypted and may contain secrets.")
        return EXIT_OK

    def _export_all_restic(self, manager: BackupManager, store: ResticStore, args: argparse.Namespace) -> int:
        try:
            self._encryption_override(args)
        except ValueError as e:
            print(Fore.RED + str(e))
            return EXIT_USAGE
        newest: dict[str, Snapshot] = {}
        for snap in store.snapshots():  # oldest first, so the last one wins
            newest[snap.cfg] = snap
        tars = self._newest_tars(manager)
        names = sorted(set(newest) | set(tars))
        if not names:
            print(Fore.YELLOW + "No saved configurations found.")
            return EXIT_OK
        dest_dir = args.output or os.path.expanduser("~")
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except PermissionError:
            print(Fore.RED + f"Cannot create output directory: {dest_dir}")
            return EXIT_USAGE
        plain = False
        exit_code = EXIT_OK
        for cfg in names:
            # Like the archive-mode export: one failing configuration does not stop the others.
            try:
                plain |= self._export_one(manager, store, args, cfg, newest.get(cfg), tars.get(cfg), None, dest_dir)
            except ValidationError as e:
                print(Fore.RED + f"[{cfg}] validation error in configuration: {e}")
                exit_code = EXIT_VALIDATION
            except PermissionError as e:
                print(Fore.RED + f"[{cfg}] permission error: {e}")
                exit_code = EXIT_PERMISSION
            except EncryptionError as e:
                print(Fore.RED + f"[{cfg}] encryption error: {e}")
                exit_code = EXIT_ENCRYPTION
            except StoreError as e:
                print(Fore.RED + f"[{cfg}] {e}")
                exit_code = EXIT_RUNTIME
            except OSError as e:
                print(Fore.RED + f"[{cfg}] I/O error: {e}")
                exit_code = EXIT_IO
        if plain:
            print(Fore.YELLOW + "Note: archives are not encrypted and may contain secrets.")
        return exit_code

    # ------------------------------------------- store setup, migration, manual prune

    def _restic_init(self, args: argparse.Namespace, saves_dir: str) -> int:
        if not args.encrypt_to:
            print(Fore.RED + "--restic-init needs --encrypt-to <age recipient>: the store password is encrypted to it.")
            return EXIT_USAGE
        if (args.encrypt_method or "age") != "age":
            print(Fore.RED + "The restic store is encrypted with age: --encrypt-method gpg cannot be used here.")
            return EXIT_USAGE
        if len(args.encrypt_to) != 1:
            print(Fore.RED + "--restic-init takes exactly one --encrypt-to recipient.")
            return EXIT_USAGE
        store = ResticStore(saves_dir, args.identity)
        problem = maintenance.check_identity_matches(store.identity, args.encrypt_to[0])
        if problem:
            print(Fore.RED + problem)
            return EXIT_USAGE
        store.init(args.encrypt_to[0])
        print(Fore.GREEN + f"Created the restic store in {saves_dir}.")
        print(
            Fore.YELLOW + "The age private key is now the only thing needed to read this store (the repository "
            f"password is encrypted to it). Keep a copy of {store.identity} off this machine: without it "
            "every backup in the store is unreadable."
        )
        return EXIT_OK

    @staticmethod
    def _print_migration(report: MigrationReport, saves_dir: str) -> None:
        print(
            f"Migrated {len(report.migrated)}, already in the store {len(report.skipped)}, "
            f"failed {len(report.failed)}, deleted {len(report.deleted)} archive(s)."
        )
        print(
            f"Archives before: {report.bytes_before / 1e6:.1f} MB; "
            f"restic store now: {maintenance.store_size(saves_dir) / 1e6:.1f} MB."
        )
        for path, reason in report.failed:
            print(Fore.RED + f"  failed: {path}: {reason} (kept)")
        if report.check_error:
            print(Fore.RED + f"restic check failed: {report.check_error}")
        elif report.checked:
            print(Fore.GREEN + "restic check: ok")

    def _migrate(self, manager: BackupManager, args: argparse.Namespace, saves_dir: str) -> int:
        mode = select_mode(saves_dir)
        if mode is not StorageMode.RESTIC:
            print(Fore.RED + "--migrate needs an initialised restic store.")
            print(Fore.YELLOW + NOTICES[mode])
            return EXIT_USAGE
        store = ResticStore(saves_dir, args.identity)

        def progress(line: str) -> None:
            print((Fore.RED if "FAILED" in line else Fore.CYAN) + line, flush=True)

        report = migrate_archives(manager, store, args.identity, args.delete_migrated, progress=progress)
        self._print_migration(report, saves_dir)
        if not args.delete_migrated and report.migrated:
            print(Fore.YELLOW + "Nothing was deleted. Re-run with --delete-migrated to remove the verified archives.")
        return EXIT_RUNTIME if report.failed or report.check_error else EXIT_OK

    def _prune(self, manager: BackupManager, args: argparse.Namespace, saves_dir: str) -> int:
        mode = select_mode(saves_dir)
        store = ResticStore(saves_dir, args.identity) if mode is StorageMode.RESTIC else None
        known, unreadable = maintenance.retention_by_config(manager, args.input or default_config_dirs())
        outcome = maintenance.prune_all(saves_dir, store, known, unreadable, args.dry_run, args.include_legacy)
        verb = "would remove" if args.dry_run else "removed"
        for note in outcome.notes:
            print(Fore.YELLOW + note)
        for cfg, labels in outcome.removed.items():
            print(Fore.CYAN + f"[{cfg}] {verb} {len(labels)} backup(s):")
            for label in labels:
                print(f"    {label}")
        for error in outcome.errors:
            print(Fore.RED + error)
        print(f"Total: {outcome.total} backup(s) {verb}.")
        return EXIT_RUNTIME if outcome.errors else EXIT_OK

    def _compress(
        self,
        manager: BackupManager,
        args: argparse.Namespace,
        saves_dir: str,
        timestamp: str,
    ) -> int:
        try:
            jobs = self._resolve_jobs(args.jobs)
        except ValueError as e:
            print(Fore.RED + str(e))
            return EXIT_USAGE

        # A per-file progress bar and several workers writing at once are
        # unreadable together, so --progress alone means sequential; asking for
        # both explicitly is allowed and reports config-level progress only.
        if args.progress and args.jobs == "auto":
            jobs = 1

        # A store that exists but cannot be used raises StoreError here: never a silent fallback.
        mode = select_mode(saves_dir)
        store = ResticStore(saves_dir) if mode is StorageMode.RESTIC else None
        if store is not None and (args.encrypt_to or args.encrypt_method):
            print(
                Fore.RED + "--encrypt-to/--encrypt-method cannot be used while the restic store is in use: "
                "the store is always encrypted, and recipients apply to exports (--export-config)."
            )
            return EXIT_USAGE

        try:
            encryption = self._encryption_override(args)
        except ValueError as e:
            print(Fore.RED + str(e))
            return EXIT_USAGE

        extra_directories: tuple[str, ...] = ()
        if args.include_system_configs:
            extra_directories = (paths.SYSTEM_CONFIG_DIR,)

        # With no --input the levels are resolved and merged; with --input the
        # user said exactly what to process, so no precedence is invented.
        inputs = list(args.input or [])
        layered = not inputs
        if layered:
            inputs = default_config_dirs()
            if not inputs:
                return self._report_no_configurations()

        directories = [p for p in inputs if os.path.isdir(p)]
        if len(inputs) > 1 and len(directories) != len(inputs):
            print(Fore.RED + "Several --input paths are only supported when every one is a configuration directory.")
            return EXIT_USAGE

        if directories:
            if args.output is not None:
                print(
                    Fore.RED + "When --input is a directory you may not provide --output. "
                    "Omit --output to create per-config archives."
                )
                return EXIT_USAGE

            try:
                cfg_files = self._collect_configs(manager, directories, allow_override=layered)
            except ValueError as e:
                print(Fore.RED + str(e))
                return EXIT_USAGE

            if not cfg_files:
                return self._report_no_configurations()

            self._print_notice(mode)
            batch = manager.compress_config_files(
                cfg_files,
                timestamp,
                show_progress=args.progress,
                description=args.description,
                jobs=jobs,
                encryption=encryption,
                extra_directories=extra_directories,
                store=store,
            )
            self._report_batch(batch)
            # Only after the report, and never allowed to change a backup's outcome.
            prune_report = manager.prune_after_batch(batch, store)
            self._report_prune(prune_report)
            if batch.failures or prune_report.errors:
                return EXIT_RUNTIME
            if args.strict and batch.missing_inputs:
                return EXIT_INCOMPLETE
            return EXIT_OK

        input_path = inputs[0]
        if args.jobs != "auto" and self._resolve_jobs(args.jobs) > 1:
            print(Fore.RED + "--jobs only applies when --input is a directory of configurations.")
            return EXIT_USAGE

        if args.output and args.description:
            print(
                Fore.RED + "--description stores the archive in a per-config directory, so it cannot be combined "
                "with an explicit --output path."
            )
            return EXIT_USAGE

        self._print_notice(mode)
        if store is not None and not args.output:
            snapshot, result = manager.compress_config_to_store(
                input_path,
                store,
                datetime.strptime(timestamp, TIMESTAMP_FORMAT),
                description=args.description,
                show_progress=args.progress,
                extra_directories=extra_directories,
            )
            print(Fore.GREEN + f"Stored snapshot {snapshot.short_id} of {snapshot.cfg} in the restic store.")
            self._report_result(result)
            if args.strict and result.missing_inputs:
                return EXIT_INCOMPLETE
            return EXIT_OK

        if args.description:
            cfg_basename = os.path.splitext(os.path.basename(input_path))[0]
            cfg_dir = os.path.join(saves_dir, "configs", cfg_basename)
            out_path, result = manager.compress_config_to_timestamp_dir(
                input_path,
                cfg_dir,
                timestamp,
                description=args.description,
                show_progress=args.progress,
                encryption=encryption,
                extra_directories=extra_directories,
            )
        else:
            out_path = args.output or os.path.join(saves_dir, f"config-saver-{timestamp}.tar.gz")
            result = manager.compress_config_file(
                input_path,
                out_path,
                show_progress=args.progress,
                encryption=encryption,
                extra_directories=extra_directories,
            )
            # Encryption appends a suffix, so the archive is not at `out_path`.
            out_path = result.output_path

        print(Fore.GREEN + f"Compression completed successfully. Output: {out_path}")
        self._report_result(result)
        if args.strict and result.missing_inputs:
            return EXIT_INCOMPLETE
        return EXIT_OK

    def _decompress(self, args: argparse.Namespace) -> int:
        inputs = args.input or []
        if not inputs:
            print(Fore.RED + "--decompress requires --input <archive.tar.gz>.")
            return EXIT_USAGE
        if len(inputs) > 1:
            print(Fore.RED + "--decompress takes a single --input archive.")
            return EXIT_USAGE
        decompressor = TarDecompressor(
            inputs[0],
            args.output,
            show_progress=args.progress,
            identity=args.identity,
            restore_system_configs=args.restore_system_configs,
        )
        result = decompressor.decompress()
        if result.decrypted_with:
            print(Fore.GREEN + f"Archive decrypted with {result.decrypted_with}.")
        if result.skipped_system_configs:
            print(
                Fore.YELLOW + f"Skipped {result.skipped_system_configs} system configuration file(s) under "
                f"{paths.SYSTEM_CONFIG_DIR}. Pass --restore-system-configs to write them; leave it "
                "alone if a declarative installer owns that directory."
            )
        if args.output:
            print(Fore.GREEN + f"Extraction completed successfully in '{args.output}' ({result.extracted} members).")
        else:
            print(Fore.GREEN + f"Extraction completed successfully to absolute paths ({result.extracted} members).")
        return EXIT_OK
