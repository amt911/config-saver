#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import os
from datetime import datetime
from typing import Optional
from colorama import Fore, init
import sys
from pydantic import ValidationError

from config_saver import __version__
from .parser.parser import Parser
from .tar_compressor.tar_compressor import TarCompressor
from .tar_compressor.tar_decompressor import TarDecompressor

init(autoreset=True)


class CLI:
    """Orchestrates CLI parsing and actions for config-saver (internal lib placement)."""

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
        parser.add_argument('--version', '-v', action='version', version=f'%(prog)s {__version__}', help='Show program version and exit')
        return parser.parse_args(self.argv)

    def run(self) -> None:
        args = self.parse_args()

        saves_dir = os.path.expanduser("~/.config/config-saver")
        try:
            # Keep a stable timestamp for this run when creating multiple outputs
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

            # If input is a directory and user requested compress, compress each YAML file
            if args.compress and os.path.isdir(args.input):
                # Ensure saves_dir exists (try primary, fallback on permission errors)
                try:
                    os.makedirs(saves_dir, exist_ok=True)
                except PermissionError:
                    user_saves = os.path.expanduser("~/.local/share/config-saver/saves")
                    try:
                        os.makedirs(user_saves, exist_ok=True)
                    except PermissionError:
                        print(Fore.RED + f"Error: cannot create fallback directory {user_saves} (permission denied). Exiting.")
                        sys.exit(1)
                    print(Fore.YELLOW + f"Warning: cannot create {saves_dir} (permission denied). Using {user_saves} instead.")
                    saves_dir = user_saves

                # If user provided a single --output while input is a directory, that's ambiguous
                if args.output is not None:
                    print(Fore.RED + "When --input is a directory you may not provide --output. Please omit --output to create per-file archives.")
                    sys.exit(6)

                # gather YAML files (non-recursive) and sort for deterministic behavior
                patterns = [os.path.join(args.input, "*.yaml"), os.path.join(args.input, "*.yml")]
                cfg_files: list[str] = []
                for p in patterns:
                    cfg_files.extend(sorted(glob.glob(p)))

                if not cfg_files:
                    print(Fore.RED + f"No YAML configuration files found in {args.input}.")
                    sys.exit(2)

                # Compress each configuration file into its own tar.gz with structure:
                # ~/.config/config-saver/configs/<cfgname>/<cfgname>-<timestamp>.tar.gz
                for cfg in cfg_files:
                    cfg_basename = os.path.splitext(os.path.basename(cfg))[0]
                    cfg_dir = os.path.join(saves_dir, "configs", cfg_basename)
                    try:
                        os.makedirs(cfg_dir, exist_ok=True)
                    except PermissionError:
                        print(Fore.RED + f"Error: cannot create directory {cfg_dir} (permission denied). Skipping {cfg}.")
                        continue

                    out_path = os.path.join(cfg_dir, f"{cfg_basename}-{timestamp}.tar.gz")
                    # Load and validate YAML, then compress
                    yaml_parser = Parser(cfg)
                    model = yaml_parser.get_model()
                    compressor = TarCompressor(model, out_path, show_progress=args.progress)
                    compressor.compress()
                    print(Fore.GREEN + f"Compression completed successfully. Output: {out_path}")
                return

            # Ensure saves_dir exists when compressing single-file behavior. If creation fails, fall back to XDG data dir
            if args.compress:
                try:
                    os.makedirs(saves_dir, exist_ok=True)
                except PermissionError:
                    user_saves = os.path.expanduser("~/.local/share/config-saver/saves")
                    try:
                        os.makedirs(user_saves, exist_ok=True)
                    except PermissionError:
                        print(Fore.RED + f"Error: cannot create fallback directory {user_saves} (permission denied). Exiting.")
                        sys.exit(1)
                    print(Fore.YELLOW + f"Warning: cannot create {saves_dir} (permission denied). Using {user_saves} instead.")
                    if args.output and args.output.startswith(saves_dir):
                        args.output = args.output.replace(saves_dir, user_saves)
                    saves_dir = user_saves

            # If the user didn't pass an explicit output for single-file compress, set default name using timestamp
            if args.output is None and args.compress:
                args.output = f"{saves_dir}/config-saver-{timestamp}.tar.gz"

            if args.list:
                # Prefer listing the per-config archives under saves_dir/configs
                configs_root = os.path.join(saves_dir, "configs")
                files = []
                if os.path.isdir(configs_root):
                    # recursive glob for any tar.gz under configs_root
                    files = sorted(glob.glob(os.path.join(configs_root, "**", "*.tar.gz"), recursive=True))

                # fallback: list top-level tar.gz files in saves_dir
                if not files:
                    files = sorted(glob.glob(f"{saves_dir}/*.tar.gz"))

                if files:
                    print("Available config-saver tar.gz files:")
                    for f in files:
                        print(f"  {f}")
                else:
                    print(f"No config-saver tar.gz files found in {saves_dir} or {configs_root}.")
                return

            if args.compress:
                # Load and validate YAML. Parser now raises exceptions and exposes the Model
                yaml_parser = Parser(args.input)
                model = yaml_parser.get_model()
                compressor = TarCompressor(model, args.output, show_progress=args.progress)
                compressor.compress()
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
