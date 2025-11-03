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

    def _find_previous_timestamp_dir(self, base_cfg_dir: str, current_timestamp: str) -> Optional[str]:
        """Find the most recent timestamp directory before current_timestamp.
        
        Returns the path to the previous timestamp directory, or None if this is the first backup.
        Only returns directories that contain a .backup-state.json file.
        """
        if not os.path.isdir(base_cfg_dir):
            print(f"[DEBUG] No base_cfg_dir exists: {base_cfg_dir}")
            return None
        
        # List all timestamp directories
        # Format: YYYYMMDD-HHMMSS or YYYYMMDD-HHMMSS-mmm (with milliseconds)
        timestamp_dirs: List[str] = []
        for entry in os.listdir(base_cfg_dir):
            entry_path = os.path.join(base_cfg_dir, entry)
            # Check if it's a directory, before current timestamp, and has state file
            if os.path.isdir(entry_path) and entry < current_timestamp:
                # Check if it looks like a timestamp (basic validation)
                # Accept both old format (15 chars) and new format (21 chars with milliseconds)
                if (len(entry) == 15 or len(entry) == 21) and '-' in entry:
                    # IMPORTANT: Only consider directories that have a backup state file
                    state_file = os.path.join(entry_path, ".backup-state.json")
                    if os.path.exists(state_file):
                        print(f"[DEBUG] Found candidate: {entry} (has state file)")
                        timestamp_dirs.append(entry)
                    else:
                        print(f"[DEBUG] Skipping {entry} (no state file)")
        
        if not timestamp_dirs:
            print(f"[DEBUG] No previous timestamp dirs found before {current_timestamp}")
            return None
        
        # Sort and get the most recent one
        timestamp_dirs.sort(reverse=True)
        prev_dir = os.path.join(base_cfg_dir, timestamp_dirs[0])
        print(f"[DEBUG] Selected previous dir: {timestamp_dirs[0]}")
        return prev_dir

    def compress_yaml_file(
        self, yaml_path: str, out_path: str, show_progress: bool = False, 
        state_dir: Optional[str] = None, prev_state_dir: Optional[str] = None
    ) -> str:
        """Compress a single YAML config into the provided output path and return it.

        This keeps the original behaviour where the caller provides an explicit
        destination archive path.
        
        Args:
            yaml_path: Path to the YAML configuration file
            out_path: Output path for the tar.gz file
            show_progress: Whether to show progress bar
            state_dir: Directory where to store NEW .backup-state.json (for incremental backups)
                      If None, uses the parent directory of out_path
            prev_state_dir: Directory where to load PREVIOUS .backup-state.json from
                           If None, no previous state will be loaded (FULL backup)
        """
        parser = Parser(yaml_path)
        model = parser.get_model()
        
        # If state_dir not provided, use parent directory of output
        if state_dir is None:
            state_dir = os.path.dirname(out_path)
        
        compressor = TarCompressor(model, out_path, show_progress=show_progress, 
                                  state_dir=state_dir, prev_state_dir=prev_state_dir)
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
        # compress into the out_path, using dest_dir as state_dir for incremental backups
        self.compress_yaml_file(yaml_path, out_path, show_progress=show_progress, state_dir=dest_dir)

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
        
        The .backup-state.json will be stored in the timestamp directory.
        The previous state will be loaded from the most recent previous timestamp directory.
        """
        # Check if timestamp directory already exists (collision)
        ts_dir = os.path.join(base_cfg_dir, timestamp)
        if os.path.exists(ts_dir):
            # If directory already exists, it means we're trying to create a backup
            # with the same timestamp. This can happen if backups are created within
            # the same second. We need to ensure unique timestamps.
            print(f"[WARNING] Timestamp directory already exists: {ts_dir}")
            print("[WARNING] This may cause issues with incremental backups.")
            print("[WARNING] Consider waiting 1 second between backups.")
        
        # Find the most recent previous timestamp directory BEFORE creating the new one
        prev_state_dir = self._find_previous_timestamp_dir(base_cfg_dir, timestamp)
        
        # Now create the new timestamp directory
        os.makedirs(ts_dir, exist_ok=True)
        cfg_basename = os.path.splitext(os.path.basename(yaml_path))[0]
        archive_name = f"{cfg_basename}-{timestamp}.tar.gz"
        
        out_path = os.path.join(ts_dir, archive_name)
        parser = Parser(yaml_path)
        model = parser.get_model()
        # Current state will be saved to ts_dir, previous state loaded from prev_state_dir
        compressor = TarCompressor(model, out_path, show_progress=show_progress, 
                                  state_dir=ts_dir, prev_state_dir=prev_state_dir)
        compressor.compress()
        
        # Write description if provided
        if description:
            desc_path = os.path.join(ts_dir, "description.txt")
            try:
                with open(desc_path, "w", encoding="utf-8") as fh:
                    fh.write(description)
            except PermissionError:
                pass
        
        return out_path

    def get_description_for_archive(self, archive_path: str) -> Optional[str]:
        """Return the description text associated with a given archive, if present.

        The description is expected to live in the same timestamp directory as the
        archive under the name `description.txt`.
        """
        if not archive_path:
            return None

        # The archive should be inside a timestamp dir: .../<cfgname>/<timestamp>/<archive>
        archive_dir = os.path.dirname(os.path.abspath(archive_path))
        desc_path = os.path.join(archive_dir, "description.txt")
        if os.path.exists(desc_path) and os.path.isfile(desc_path):
            try:
                with open(desc_path, "r", encoding="utf-8") as fh:
                    return fh.read().strip()
            except (IOError, PermissionError):
                return None
        return None

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
            out_path = os.path.join(ts_dir, archive_name)
            
            # Find previous timestamp directory for loading state
            prev_state_dir = self._find_previous_timestamp_dir(cfg_dir, timestamp)
            
            # Compress with state_dir=ts_dir (current) and prev_state_dir (previous)
            self.compress_yaml_file(cfg, out_path, show_progress=show_progress, 
                                   state_dir=ts_dir, prev_state_dir=prev_state_dir)
            
            # Write description if provided
            if description:
                desc_path = os.path.join(ts_dir, "description.txt")
                try:
                    with open(desc_path, "w", encoding="utf-8") as fh:
                        fh.write(description)
                except PermissionError:
                    pass
            
            results.append(out_path)

        return results
