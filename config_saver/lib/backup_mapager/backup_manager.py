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
        """Compress a single YAML config into the provided output path and return it.

        This keeps the original behaviour where the caller provides an explicit
        destination archive path.
        """
        parser = Parser(yaml_path)
        model = parser.get_model()
        compressor = TarCompressor(model, out_path, show_progress=show_progress)
        compressor.compress()
        return out_path

    def _compress_yaml_to_directory(
        self,
        yaml_path: str,
        dest_dir: str,
        archive_name: str,
        description: Optional[str] = None,
        show_progress: bool = False,
    ) -> str:
        """Compress a YAML config into a destination directory and optionally write a description.txt.

        Ensures dest_dir exists, writes description.txt if description is provided,
        creates the tar.gz named archive_name inside dest_dir and returns its path.
        """
        os.makedirs(dest_dir, exist_ok=True)

        out_path = os.path.join(dest_dir, archive_name)
        # compress into the out_path
        self.compress_yaml_file(yaml_path, out_path, show_progress=show_progress)

        if description:
            desc_path = os.path.join(dest_dir, "description.txt")
            try:
                # write the description as UTF-8 text
                with open(desc_path, "w", encoding="utf-8") as fh:
                    fh.write(description)
            except PermissionError:
                # If we cannot write the description, continue silently
                pass

        return out_path

    def compress_yaml_to_timestamp_dir(
        self,
        yaml_path: str,
        base_cfg_dir: str,
        timestamp: str,
        description: Optional[str] = None,
        show_progress: bool = False,
    ) -> str:
        """Public helper to compress a YAML into a per-timestamp directory.

        base_cfg_dir should be the directory for the config (i.e. <saves_dir>/configs/<cfgname>).
        This will create <base_cfg_dir>/<timestamp>/ and place the archive and optional
        description.txt there. Returns the path to the created archive.
        """
        ts_dir = os.path.join(base_cfg_dir, timestamp)
        os.makedirs(ts_dir, exist_ok=True)
        cfg_basename = os.path.splitext(os.path.basename(yaml_path))[0]
        archive_name = f"{cfg_basename}-{timestamp}.tar.gz"
        return self._compress_yaml_to_directory(yaml_path, ts_dir, archive_name, description=description, show_progress=show_progress)

    def compress_directory_of_yamls(
        self,
        input_dir: str,
        timestamp: str,
        show_progress: bool = False,
        description: Optional[str] = None,
    ) -> List[str]:
        """Compress each top-level YAML file inside input_dir into its own archive.

        Produces archives under: <saves_dir>/configs/<cfgname>/<timestamp>/<cfgname>-<timestamp>.tar.gz
        If `description` is provided it will be saved alongside the archive as
        description.txt inside the timestamp directory.
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

            # create a per-timestamp directory
            ts_dir = os.path.join(cfg_dir, timestamp)
            try:
                os.makedirs(ts_dir, exist_ok=True)
            except PermissionError:
                # Skip this config if we cannot create its timestamped directory
                continue

            archive_name = f"{cfg_basename}-{timestamp}.tar.gz"
            out_path = self._compress_yaml_to_directory(
                cfg, ts_dir, archive_name, description=description, show_progress=show_progress
            )
            results.append(out_path)

        return results
