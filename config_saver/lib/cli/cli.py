#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
import shutil

from datetime import datetime
from typing import Optional

from colorama import Fore, init
from pydantic import ValidationError
from rich.align import Align
from rich.columns import Columns
from rich.console import Console
from rich.table import Table

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
        manager = BackupManager(self.saves_dir)

        for cfgname, timestamps in grouped.items():
            table = Table(show_header=True, header_style="bold bright_blue", row_styles=["none", "dim"])
            # Left column: ordinal number
            table.add_column("No.", width=5, justify="center", no_wrap=True)
            # Timestamp column
            table.add_column("Date", overflow="fold", justify="center", no_wrap=True)
            # Description column: header centered, content left-justified
            table.add_column(Align.center("Description"), overflow="fold", justify="left")

            # sort timestamps descending (newest first)
            timestamps.sort(reverse=True)

            # We need to map timestamp back to a specific archive path to fetch its description.
            # The manager.list_archives already returned full paths; we'll rebuild a small lookup
            # by scanning the saves tree for archives that match this cfgname.
            all_archives = manager.list_archives()
            # Filter archives that belong to this cfgname
            cfg_archives = [p for p in all_archives if os.path.basename(p).startswith(cfgname + "-")]
            # Build a mapping from timestamp string (YYYYMMDD-HHMMSS) to archive path
            archive_map: dict[str, str] = {}
            for p in cfg_archives:
                m = self.FILENAME_PATTERN.search(os.path.basename(p))
                if m:
                    archive_map[m.group(2)] = p

            for i, t in enumerate(timestamps, start=1):
                ts_str = t.strftime("%Y%m%d-%H%M%S")
                desc = None
                if ts_str in archive_map:
                    desc = manager.get_description_for_archive(archive_map[ts_str])

                # Truncate description for display
                if desc:
                    preview = desc if len(desc) <= 60 else desc[:57] + "..."
                else:
                    preview = ""

                table.add_row(str(i), t.strftime("%Y-%m-%d %H:%M:%S"), preview)
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
        group.add_argument('--export-config', '-e', type=str, metavar='NAME', help='Export the latest config archive by name')
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

            # Exportar la última configuración por nombre
            if args.export_config:
                cfgname = args.export_config
                # Buscar archivos que coincidan con el nombre
                archives = manager.list_archives()
                # Filtrar por nombre
                matching = [p for p in archives if os.path.basename(p).startswith(cfgname + "-")]
                if not matching:
                    print(Fore.RED + f"No se encontró ninguna configuración guardada con el nombre: {cfgname}")
                    sys.exit(7)
                # Ordenar por timestamp descendente
                def extract_ts(path: str) -> str:
                    m = re.search(r"-(\d{8}-\d{6})\.tar\.gz$", os.path.basename(path))
                    return m.group(1) if m else "00000000-000000"
                matching.sort(key=extract_ts, reverse=True)
                latest = matching[0]
                # Si se especifica --output, copiar el archivo allí
                if args.output:
                    dest_path = args.output
                else:
                    home = os.path.expanduser("~")
                    dest_path = os.path.join(home, os.path.basename(latest))
                shutil.copy2(latest, dest_path)
                print(Fore.GREEN + f"Exportación completada: {dest_path}")
                return

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
        except (OSError, IOError) as e:
            print(Fore.RED + f"I/O error: {e}")
            sys.exit(10)
