#!/usr/bin/env python3
"""Command line interface for config-saver.

All rendering lives here: the library layers return structured results and this
module turns them into terminal output and stable exit codes.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime

from colorama import Fore, init
from pydantic import ValidationError
from rich.align import Align
from rich.columns import Columns
from rich.console import Console
from rich.table import Table

from config_saver import __version__
from config_saver.lib.backup_manager.backup_manager import BackupManager, BatchResult
from config_saver.lib.errors import ArchiveError, RootRequiredError, UnsafeArchiveError
from config_saver.lib.tar_compressor.tar_compressor import CompressResult
from config_saver.lib.tar_compressor.tar_decompressor import TarDecompressor

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
EXIT_IO = 10

# Matches <name>-YYYYMMDD-HHMMSS.tar.gz
FILENAME_PATTERN = re.compile(r"(.+)-(\d{8}-\d{6})\.tar\.gz$")


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

    def render(self) -> None:
        files = self._gather_files()
        if not files:
            print(f"No config-saver tar.gz files found in {self.saves_dir} or {self.user_saves}.")
            return

        # One pass over the archives: config name -> {timestamp: path}
        grouped: dict[str, dict[str, str]] = {}
        for f in files:
            name = os.path.basename(f)
            m = self.FILENAME_PATTERN.match(name)
            cfgname = m.group(1) if m else os.path.splitext(name)[0]
            ts = m.group(2) if m else self._parse_ts(f).strftime("%Y%m%d-%H%M%S")
            grouped.setdefault(cfgname, {})[ts] = f

        console = Console()
        tables: list[Table] = []

        for cfgname, archives in grouped.items():
            table = Table(
                show_header=True,
                header_style="bold bright_blue",
                row_styles=["none", "dim"],
                title=cfgname,
                title_style="bold magenta",
            )
            table.add_column("No.", width=5, justify="center", no_wrap=True)
            table.add_column("Date", overflow="fold", justify="center", no_wrap=True)
            table.add_column(Align.center("Description"), overflow="fold", justify="left")

            for i, ts in enumerate(sorted(archives, reverse=True), start=1):
                when = self._parse_ts(archives[ts])
                desc = self.manager.get_description_for_archive(archives[ts])
                preview = "" if not desc else (desc if len(desc) <= 60 else desc[:57] + "...")
                table.add_row(str(i), when.strftime("%Y-%m-%d %H:%M:%S"), preview)
            tables.append(table)

        console.rule("Saved configurations")
        console.print(Columns(tables, expand=False, padding=(0, 2), equal=False))
        console.rule()


def default_config_dir() -> str | None:
    """Return the first configuration directory that exists, if any.

    The AUR package installs /etc/config-saver/configs; a pip install ships the
    example configs under <prefix>/share/config-saver/configs.
    """
    for candidate in (
        CLI.DEFAULT_SYSTEM_CONFIG,
        os.path.join(sys.prefix, "share", "config-saver", "configs"),
        os.path.expanduser("~/.config/config-saver/configs.d"),
    ):
        if os.path.isdir(candidate):
            return candidate
    return None


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
        parser.add_argument(
            "--input",
            "-i",
            type=str,
            default=None,
            help="Input YAML/JSON config or config directory (compress), or tar file (decompress)",
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
            if outcome.archive_path:
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
        except (UnsafeArchiveError, ArchiveError) as e:
            print(Fore.RED + f"Archive error: {e}")
            return EXIT_RUNTIME
        except RuntimeError as e:
            print(Fore.RED + f"Runtime error: {e}")
            return EXIT_RUNTIME
        except OSError as e:
            print(Fore.RED + f"I/O error: {e}")
            return EXIT_IO

    def _dispatch(self, args: argparse.Namespace) -> int:
        manager = BackupManager()
        saves_dir = manager.ensure_saves_dir()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        if args.show_configs:
            return self._show_configs(manager)
        if args.export_config:
            return self._export_config(manager, args)
        if args.export_all_configs:
            return self._export_all_configs(manager, args)
        if args.list:
            BackupTable(saves_dir).render()
            return EXIT_OK
        if args.compress:
            return self._compress(manager, args, saves_dir, timestamp)
        if args.decompress:
            return self._decompress(args)
        return EXIT_OK

    def _show_configs(self, manager: BackupManager) -> int:
        config_names = {
            m.group(1) for m in (FILENAME_PATTERN.match(os.path.basename(p)) for p in manager.list_archives()) if m
        }
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
            m = re.search(r"-(\d{8}-\d{6})\.tar\.gz$", os.path.basename(path))
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

        input_path = args.input
        if input_path is None:
            input_path = default_config_dir()
            if input_path is None:
                print(
                    Fore.RED
                    + f"No configuration directory found. Looked in {self.DEFAULT_SYSTEM_CONFIG} and "
                    + os.path.join(sys.prefix, "share", "config-saver", "configs")
                    + "."
                )
                print(Fore.YELLOW + "Pass --input <config.yaml|dir>, or install the 'config-saver' AUR package.")
                return EXIT_USAGE

        if os.path.isdir(input_path):
            if args.output is not None:
                print(
                    Fore.RED + "When --input is a directory you may not provide --output. "
                    "Omit --output to create per-config archives."
                )
                return EXIT_USAGE

            batch = manager.compress_directory_of_configs(
                input_path,
                timestamp,
                show_progress=args.progress,
                description=args.description,
                jobs=jobs,
            )
            self._report_batch(batch)
            if batch.failures:
                return EXIT_RUNTIME
            if args.strict and batch.missing_inputs:
                return EXIT_INCOMPLETE
            return EXIT_OK

        if args.jobs != "auto" and self._resolve_jobs(args.jobs) > 1:
            print(Fore.RED + "--jobs only applies when --input is a directory of configurations.")
            return EXIT_USAGE

        if args.output and args.description:
            print(
                Fore.RED + "--description stores the archive in a per-config directory, so it cannot be combined "
                "with an explicit --output path."
            )
            return EXIT_USAGE

        if args.description:
            cfg_basename = os.path.splitext(os.path.basename(input_path))[0]
            cfg_dir = os.path.join(saves_dir, "configs", cfg_basename)
            out_path, result = manager.compress_config_to_timestamp_dir(
                input_path,
                cfg_dir,
                timestamp,
                description=args.description,
                show_progress=args.progress,
            )
        else:
            out_path = args.output or os.path.join(saves_dir, f"config-saver-{timestamp}.tar.gz")
            result = manager.compress_config_file(input_path, out_path, show_progress=args.progress)

        print(Fore.GREEN + f"Compression completed successfully. Output: {out_path}")
        self._report_result(result)
        if args.strict and result.missing_inputs:
            return EXIT_INCOMPLETE
        return EXIT_OK

    def _decompress(self, args: argparse.Namespace) -> int:
        if args.input is None:
            print(Fore.RED + "--decompress requires --input <archive.tar.gz>.")
            return EXIT_USAGE
        decompressor = TarDecompressor(args.input, args.output, show_progress=args.progress)
        result = decompressor.decompress()
        if args.output:
            print(Fore.GREEN + f"Extraction completed successfully in '{args.output}' ({result.extracted} members).")
        else:
            print(Fore.GREEN + f"Extraction completed successfully to absolute paths ({result.extracted} members).")
        return EXIT_OK
