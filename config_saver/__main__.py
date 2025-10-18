#!/usr/bin/env python3

"""Main module to demonstrate YAML parsing and tar compression"""
import argparse
import glob
import os
from datetime import datetime
from colorama import Fore, init
from config_saver import __version__

from .lib.models.model import Model
from .lib.parser.parser import Parser
from .lib.tar_compressor.tar_compressor import TarCompressor
from .lib.tar_compressor.tar_decompressor import TarDecompressor

init(autoreset=True)

def main():
    parser = argparse.ArgumentParser(description="Tar compressor/decompressor utility", prog="config-saver")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--compress', '-c', action='store_true', help='Compress files/directories from YAML config')
    group.add_argument('--decompress', '-d', action='store_true', help='Decompress a tar file')
    group.add_argument('--list', '-l', action='store_true', help='List all config-saver .tar.gz files in the current directory')
    parser.add_argument('--input', '-i', type=str, required=False, default='/etc/config-saver/default-config.yaml', help='Input YAML config (for compress, default: /etc/config-saver/default-config.yaml) or tar file (for decompress)')
    parser.add_argument('--output', '-o', type=str, default=None, help='Output tar file (for compress) or extraction directory (for decompress, optional)')
    parser.add_argument('--progress', '-P', action='store_true', help='Show progress bar during compression/decompression')
    parser.add_argument('--version', '-v', action='version', version=f'%(prog)s {__version__}', help='Show program version and exit')
    args = parser.parse_args()

    # Set default output path if not provided (use user config dir by default)
    saves_dir = os.path.expanduser("~/.config/config-saver")
    if args.output is None and args.compress:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        args.output = f"{saves_dir}/config-saver-{timestamp}.tar.gz"
    # Ensure saves_dir exists when compressing. If creation fails, fall back to XDG data dir
    if args.compress:
        try:
            os.makedirs(saves_dir, exist_ok=True)
        except PermissionError:
            user_saves = os.path.expanduser("~/.local/share/config-saver/saves")
            os.makedirs(user_saves, exist_ok=True)
            print(Fore.YELLOW + f"Warning: cannot create {saves_dir} (permission denied). Using {user_saves} instead.")
            # Update args.output to point to the fallback location
            if args.output and args.output.startswith(saves_dir):
                args.output = args.output.replace(saves_dir, user_saves)

    if args.list:
        # List all config-saver .tar.gz files in /etc/config-saver/saves
        files = sorted(glob.glob(f"{saves_dir}/config-saver-*.tar.gz"))
        if files:
            print(f"Available config-saver tar.gz files in {saves_dir}:")
            for f in files:
                print(f"  {f}")
        else:
            print(f"No config-saver tar.gz files found in {saves_dir}.")
        return

    if args.compress:
        try:
            yaml_parser = Parser(args.input)
            validated = Model.model_validate(yaml_parser.get_data())
            print(Fore.GREEN + "YAML validated successfully.")
        except (ValueError, TypeError) as e:
            print(Fore.RED + "Validation error:", e)
            return
        compressor = TarCompressor(validated, args.output, show_progress=args.progress)
        compressor.compress()
        print(Fore.GREEN + f"Compression completed successfully. Output: {args.output}")
    elif args.decompress:
        try:
            if args.output is not None:
                decompressor = TarDecompressor(args.input, args.output, show_progress=args.progress)
            else:
                # If no output, extract to current directory (preserve absolute paths)
                decompressor = TarDecompressor(args.input, None, show_progress=args.progress)
            decompressor.decompress()
        except (OSError, RuntimeError) as e:
            print(Fore.RED + "Decompression error:", e)

if __name__ == "__main__":
    main()
