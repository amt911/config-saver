#!/usr/bin/env python3
from __future__ import annotations

import glob
import os
from typing import List, Optional

from config_saver.lib.parser.parser import Parser
from config_saver.lib.tar_compressor.tar_compressor import TarCompressor


class BackupManager:
    """Encapsulates filesystem operations for config-saver CLI.

    Responsibilities:
    - ensure the saves directory exists (with XDG fallback)
    - list existing archives (prefer per-config 'configs' subdir)
    - compress a single YAML-based configuration into a tar.gz
    - compress every YAML in a directory into per-config archives
    """

    def __init__(self, saves_dir: Optional[str] = None):
        self.saves_dir = saves_dir or os.path.expanduser("~/.config/config-saver")

    def ensure_saves_dir(self) -> str:
        """Ensure the base saves dir exists, falling back to XDG data dir on permission errors.

        Returns the actual saves_dir that should be used.
        """
        try:
            os.makedirs(self.saves_dir, exist_ok=True)
            return self.saves_dir
        except PermissionError:
            user_saves = os.path.expanduser("~/.local/share/config-saver/saves")
            os.makedirs(user_saves, exist_ok=True)
            self.saves_dir = user_saves
            return self.saves_dir

    def list_archives(self) -> List[str]:
        """Return list of available archives, preferring the per-config 'configs' tree."""
        configs_root = os.path.join(self.saves_dir, "configs")
        files: List[str] = []
        if os.path.isdir(configs_root):
            files = sorted(glob.glob(os.path.join(configs_root, "**", "*.tar.gz"), recursive=True))

        if not files:
            files = sorted(glob.glob(os.path.join(self.saves_dir, "*.tar.gz")))

        return files

    def compress_yaml_file(self, yaml_path: str, out_path: str, show_progress: bool = False) -> str:
        """Compress a single YAML config into the provided output path and return it."""
        parser = Parser(yaml_path)
        model = parser.get_model()
        compressor = TarCompressor(model, out_path, show_progress=show_progress)
        compressor.compress()
        return out_path

    def compress_directory_of_yamls(self, input_dir: str, timestamp: str, show_progress: bool = False) -> List[str]:
        """Compress each top-level YAML file inside input_dir into its own archive.

        Produces archives under: <saves_dir>/configs/<cfgname>/<cfgname>-<timestamp>.tar.gz
        Returns a list of created archive paths.
        """
        patterns = [os.path.join(input_dir, "*.yaml"), os.path.join(input_dir, "*.yml")]
        cfg_files: List[str] = []
        for p in patterns:
            cfg_files.extend(sorted(glob.glob(p)))

        if not cfg_files:
            raise FileNotFoundError(f"No YAML configuration files found in {input_dir}.")

        # Ensure base saves dir exists or fallback
        self.ensure_saves_dir()

        results: List[str] = []
        for cfg in cfg_files:
            cfg_basename = os.path.splitext(os.path.basename(cfg))[0]
            cfg_dir = os.path.join(self.saves_dir, "configs", cfg_basename)
            try:
                os.makedirs(cfg_dir, exist_ok=True)
            except PermissionError:
                # Skip this config if we cannot create its destination
                continue

            out_path = os.path.join(cfg_dir, f"{cfg_basename}-{timestamp}.tar.gz")
            self.compress_yaml_file(cfg, out_path, show_progress=show_progress)
            results.append(out_path)

        return results
