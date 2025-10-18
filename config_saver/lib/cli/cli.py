#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
from datetime import datetime
from typing import Optional

from colorama import Fore, init
from pydantic import ValidationError
import sys
from rich.console import Console
from rich.table import Table
from rich.columns import Columns

from config_saver import __version__
from config_saver.lib.backup_mapager.backup_manager import BackupManager
from config_saver.lib.tar_compressor.tar_decompressor import TarDecompressor

init(autoreset=True)


class BackupTable:
    """Helper to collect backup archives and render a table of dates.

    This class prefers to list per-config archives under <saves_dir>/configs but
    will fall back to top-level archives. It delegates listing to BackupManager.
    """

    # Matches <name>-YYYYMMDD-HHMMSS.tar.gz
    FILENAME_PATTERN = re.compile(r"(.+)-(\d{8}-\d{6})\.tar\.gz$")

    def __init__(self, saves_dir: str):
        self.saves_dir = saves_dir
        self.user_saves = os.path.expanduser("~/.local/share/config-saver/saves")

    def _gather_files(self) -> list[str]:
        manager = BackupManager(self.saves_dir)
        return manager.list_archives()

    def _parse_ts(self, path: str) -> datetime:
        name = os.path.basename(path)
        m = self.FILENAME_PATTERN.search(name)
        if m:
            try:
                return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
            except ValueError:
                pass
        return datetime.fromtimestamp(os.path.getmtime(path))

    def render(self) -> None:
        files = self._gather_files()
        if not files:
            print(f"No config-saver tar.gz files found in {self.saves_dir} or {self.user_saves}.")
            return

        # Group timestamps by config basename
        grouped: dict[str, list[datetime]] = {}
        for f in files:
            name = os.path.basename(f)
            m = self.FILENAME_PATTERN.match(name)
            if m:
                cfgname = m.group(1)
            else:
                cfgname = os.path.splitext(name)[0]
            grouped.setdefault(cfgname, []).append(self._parse_ts(f))

        console = Console()

        # Build a table per config and arrange them left-to-right using Columns
        tables: list[Table] = []
        for cfgname, timestamps in grouped.items():
            table = Table(show_header=True, header_style="bold bright_blue", row_styles=["none", "dim"])
            # Left column: ordinal abbreviation in English (1st, 2nd, 3rd, ...)
            table.add_column("No.", width=5, justify="center", no_wrap=True)
            # Right column: timestamps under the config name (prevent wrapping for consistent row height)
            table.add_column(cfgname, overflow="fold", justify="center", no_wrap=True)

            # sort timestamps descending (newest first)
            timestamps.sort(reverse=True)

            for i, t in enumerate(timestamps, start=1):
                table.add_row(str(i), t.strftime("%Y-%m-%d %H:%M:%S"))
            tables.append(table)

    # Print header then the columns (left-to-right). Use 2 spaces padding between tables and do not expand to terminal width.
        console.rule("Saved configurations")
        console.print(Columns(tables, expand=False, padding=(0, 2), equal=False))
        console.rule()



class CLI:
    """Orchestrates CLI parsing and actions for config-saver."""

    # Default to the directory containing multiple YAML configs
    DEFAULT_SYSTEM_CONFIG = "/etc/config-saver/configs"

    def __init__(self, argv: Optional[list[str]] = None):
        self.argv = argv

    def parse_args(self) -> argparse.Namespace:
        parser = argparse.ArgumentParser(description="Tar compressor/decompressor utility", prog="config-saver")
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--compress', '-c', action='store_true', help='Compress files/directories from YAML config')
        group.add_argument('--decompress', '-d', action='store_true', help='Decompress a tar file')
        group.add_argument('--list', '-l', action='store_true', help='List saved config-saver tar.gz files')
        parser.add_argument('--input', '-i', type=str, default=self.DEFAULT_SYSTEM_CONFIG, help='Input YAML config (for compress) or tar file (for decompress)')
        parser.add_argument('--output', '-o', type=str, default=None, help='Output tar file (for compress) or extraction directory (for decompress, optional)')
        parser.add_argument('--progress', '-P', action='store_true', help='Show progress bar during compression/decompression')
        parser.add_argument('--description', '-m', type=str, default=None, help='Optional description to save alongside the archive')
        parser.add_argument('--version', '-v', action='version', version=f'%(prog)s {__version__}', help='Show program version and exit')
        return parser.parse_args(self.argv)

    def run(self) -> None:
        args = self.parse_args()

        manager = BackupManager()
        saves_dir = manager.ensure_saves_dir()
        try:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

            # Directory-mode compression
            if args.compress and os.path.isdir(args.input):
                if args.output is not None:
                    print(Fore.RED + "When --input is a directory you may not provide --output. Please omit --output to create per-file archives.")
                    sys.exit(6)

                try:
                    created = manager.compress_directory_of_yamls(
                        args.input, timestamp, show_progress=args.progress, description=args.description
                    )
                except FileNotFoundError as e:
                    print(Fore.RED + str(e))
                    sys.exit(2)

                for p in created:
                    print(Fore.GREEN + f"Compression completed successfully. Output: {p}")
                return

            # Ensure saves dir exists for single-file behavior (manager already ensured above)

            # If the user didn't pass an explicit output for single-file compress, set default name using timestamp
            if args.output is None and args.compress:
                args.output = os.path.join(saves_dir, f"config-saver-{timestamp}.tar.gz")

            if args.list:
                # Use BackupTable to render a table of dates for saved archives
                table = BackupTable(saves_dir)
                table.render()
                return

            if args.compress:
                # Single-file compress delegated to BackupManager
                # If the user supplied a description, create a per-config timestamped
                # directory and store both the archive and the description there.
                if args.description:
                    # determine config basename from input yaml path
                    cfg_basename = os.path.splitext(os.path.basename(args.input))[0]
                    cfg_dir = os.path.join(saves_dir, "configs", cfg_basename)
                    out_path = manager.compress_yaml_to_timestamp_dir(
                        args.input, cfg_dir, timestamp, description=args.description, show_progress=args.progress
                    )
                    print(Fore.GREEN + f"Compression completed successfully. Output: {out_path}")
                else:
                    manager.compress_yaml_file(args.input, args.output, show_progress=args.progress)
                    print(Fore.GREEN + f"Compression completed successfully. Output: {args.output}")
                return

            if args.decompress:
                decompressor = TarDecompressor(args.input, args.output, show_progress=args.progress)
                decompressor.decompress()
                return

        except FileNotFoundError as e:
            # e.filename may not be present if FileNotFoundError was raised manually
            if hasattr(e, "filename") and e.filename is not None:
                print(Fore.RED + f"Configuration file not found: {e.filename}")
            else:
                print(Fore.RED + f"Configuration file not found: {str(e)}")
            sys.exit(2)
        except ValidationError as e:
            # pydantic validation error
            print(Fore.RED + "Validation error in configuration:")
            print(Fore.RED + str(e))
            sys.exit(3)
        except PermissionError as e:
            print(Fore.RED + f"Permission error: {e}")
            sys.exit(4)
        except RuntimeError as e:
            print(Fore.RED + f"Runtime error: {e}")
            sys.exit(5)
        except Exception as e:
            print(Fore.RED + f"Unexpected error: {e}")
            sys.exit(10)
