#!/usr/bin/env python3
"""Module for managing backup state to enable incremental backups."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Set


class BackupState:
    """Manages file state between backups for incremental backups.
    
    Uses hybrid strategy: timestamp+size first, then hash if needed.
    """

    STATE_FILENAME = ".backup-state.json"

    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self.state_path = os.path.join(state_dir, self.STATE_FILENAME)
        self.files: Dict[str, Dict[str, Any]] = {}  # path -> {mtime, size, hash}

    def load(self) -> bool:
        """Load previous backup state."""
        if not os.path.exists(self.state_path):
            print(f"No previous backup state file found: {self.state_path}")
            return False
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.files = data.get("files", {})
            print("Previous backup state loaded successfully.")
            return True
        except (json.JSONDecodeError, IOError):
            print("Failed to load previous backup state.")
            return False

    def save(self) -> None:
        """Save current backup state."""
        os.makedirs(self.state_dir, exist_ok=True)
        data = {"files": self.files}
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except IOError:
            pass

    def _calculate_hash(self, file_path: str) -> str:
        """Calculate SHA256 hash of file."""
        sha256 = hashlib.sha256()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    sha256.update(chunk)
            return sha256.hexdigest()
        except (OSError, IOError):
            return ""

    def has_changed(self, file_path: str) -> bool:
        """Check if file has changed (hybrid strategy)."""
        # New file
        if file_path not in self.files:
            return True

        try:
            stat = os.stat(file_path)
            prev = self.files[file_path]
            
            # Quick check: mtime or size changed
            if stat.st_mtime != prev["mtime"] or stat.st_size != prev["size"]:
                return True
            
            # Slow check: hash (only if mtime/size unchanged)
            current_hash = self._calculate_hash(file_path)
            return current_hash != prev.get("hash", "")
        except (OSError, IOError):
            return True

    def update_file(self, file_path: str) -> None:
        """Update state for a file."""
        try:
            stat = os.stat(file_path)
            self.files[file_path] = {
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "hash": self._calculate_hash(file_path),
            }
        except (OSError, IOError):
            pass

    def get_changed_files(self, file_list: list[str]) -> Set[str]:
        """Get set of files that changed."""
        return {f for f in file_list if self.has_changed(f)}

    def get_deleted_files(self, file_list: list[str]) -> Set[str]:
        """Get files that existed before but not now."""
        current = set(file_list)
        previous = set(self.files.keys())
        return previous - current
