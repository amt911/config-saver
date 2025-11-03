"""Module providing a tar compressor based on a YAML configuration with pydantic validation"""
import io
import json
import os
import tarfile
from typing import Any, Dict, Optional, Set

from colorama import init
from tqdm import tqdm

from config_saver.lib.models.model import Model

init(autoreset=True)

# Placeholder for user home directory in file contents
HOME_CONTENT_PLACEHOLDER = "<<<HOME_PLACEHOLDER>>>"

# Metadata file for incremental backups
INCREMENTAL_METADATA_FILE = ".incremental-metadata.json"



class TarCompressor:
    """Class representing a tar compressor"""
    def __init__(self, yaml_data: Model, output_path: str = "output.tar.gz", base_dir: Optional[str] = None, show_progress: bool = False, state_dir: Optional[str] = None, prev_state_dir: Optional[str] = None):
        # yaml_data is expected to be a validated Model instance
        self.yaml_data = yaml_data
        self.output_path = output_path
        self.base_dir = base_dir or os.getcwd()
        self.show_progress = show_progress
        self.state_dir = state_dir or os.path.dirname(output_path)
        self.prev_state_dir = prev_state_dir  # Where to load previous state from
        # Get current user's home directory for path normalization
        self.user_home = os.path.expanduser("~")

    def _normalize_path(self, file_path: str) -> str:
        """Normalize path by replacing user's home directory with 'home/user/' placeholder"""
        # Ensure we're working with absolute paths
        abs_path = os.path.abspath(file_path)
        
        # If the path starts with the user's home directory, replace it
        if abs_path.startswith(self.user_home):
            # Replace /home/username with home/user
            relative_to_home = os.path.relpath(abs_path, self.user_home)
            normalized = os.path.join("home", "user", relative_to_home)
            return normalized
        
        # For paths outside home, keep them as is but remove leading slash
        arcname = os.path.normpath(abs_path)
        if arcname.startswith(os.sep):
            arcname = arcname[1:]
        return arcname

    def _is_text_file(self, file_path: str) -> bool:
        """Check if a file is likely a text file (not binary)"""
        # Known binary extensions (images, fonts, archives, etc.)
        binary_extensions = {
            # Images
            '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.svg', '.webp', '.tiff', '.tif',
            # Fonts
            '.ttf', '.otf', '.woff', '.woff2', '.eot',
            # Archives
            '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar',
            # Executables and libraries
            '.so', '.a', '.o', '.pyc', '.pyo', '.exe', '.dll', '.dylib',
            # Databases
            '.db', '.sqlite', '.sqlite3',
            # Media
            '.mp3', '.mp4', '.avi', '.mkv', '.wav', '.flac', '.ogg',
            # Documents (binary formats)
            '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
        }
        
        # Check extension first (fast path)
        _, ext = os.path.splitext(file_path.lower())
        if ext in binary_extensions:
            return False
        
        try:
            # Try to read first 8192 bytes and check for null bytes
            with open(file_path, 'rb') as f:
                chunk = f.read(8192)
                # If there are null bytes, it's likely binary
                if b'\0' in chunk:
                    return False
                # Try to decode as UTF-8
                try:
                    chunk.decode('utf-8')
                    return True
                except UnicodeDecodeError:
                    return False
        except (OSError, IOError):
            return False

    def _normalize_file_content(self, file_path: str) -> Optional[bytes]:
        """Read file content and replace user home paths with placeholder. Returns None if file should not be modified."""
        if not self._is_text_file(file_path):
            return None
        
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
            
            # Try to decode and replace
            try:
                text_content = content.decode('utf-8')
                # Replace absolute home path with placeholder
                if self.user_home in text_content:
                    text_content = text_content.replace(self.user_home, HOME_CONTENT_PLACEHOLDER)
                    return text_content.encode('utf-8')
            except UnicodeDecodeError:
                # If UTF-8 fails, try latin-1
                text_content = content.decode('latin-1')
                if self.user_home in text_content:
                    text_content = text_content.replace(self.user_home, HOME_CONTENT_PLACEHOLDER)
                    return text_content.encode('latin-1')
            
            return None  # No replacement needed
        except (OSError, IOError):
            return None

    def compress(self):
        """Compress files and directories with incremental backup support (ALWAYS enabled)."""
        # Import here to avoid circular dependency
        from config_saver.lib.backup_mapager.backup_state import BackupState
        
        # Collect all files
        file_list: list[str] = []
        for entry in self.yaml_data.directories:
            if isinstance(entry, str):
                if os.path.exists(entry):
                    for root, _, files in os.walk(entry):
                        for f in files:
                            file_list.append(os.path.join(root, f))
            else:
                source = entry.source
                if os.path.exists(source):
                    for file in entry.files:
                        file_path = os.path.join(source, file)
                        if os.path.exists(file_path):
                            if os.path.isdir(file_path):
                                for root, _, files in os.walk(file_path):
                                    for f in files:
                                        file_list.append(os.path.join(root, f))
                            else:
                                file_list.append(file_path)

        # Load backup state (incremental backup ALWAYS enabled)
        # Load previous state from prev_state_dir if available, otherwise from state_dir
        load_state_dir = self.prev_state_dir if self.prev_state_dir else self.state_dir
        prev_state = BackupState(load_state_dir)
        is_first_backup = not prev_state.load()
        
        # Create new state object for saving (always to state_dir)
        new_state = BackupState(self.state_dir)
        
        # Determine which files to compress
        if is_first_backup:
            files_to_compress: Set[str] = set(file_list)
            deleted_files: Set[str] = set()
            if self.show_progress:
                print("Creating FULL backup (first time)")
        else:
            files_to_compress = prev_state.get_changed_files(file_list)
            deleted_files = prev_state.get_deleted_files(file_list)
            if self.show_progress:
                print(f"Creating INCREMENTAL backup: {len(files_to_compress)} changed, {len(deleted_files)} deleted")

        # Compress
        with tarfile.open(self.output_path, "w:gz") as tar:
            # Add metadata for incremental backups
            if not is_first_backup:
                metadata: Dict[str, Any] = {
                    "backup_type": "incremental",
                    "changed_files": sorted(list(files_to_compress)),
                    "deleted_files": sorted(list(deleted_files)),
                }
                meta_bytes = json.dumps(metadata, indent=2).encode('utf-8')
                tarinfo = tarfile.TarInfo(name=INCREMENTAL_METADATA_FILE)
                tarinfo.size = len(meta_bytes)
                tar.addfile(tarinfo, fileobj=io.BytesIO(meta_bytes))
            
            if self.show_progress:
                for file_path in tqdm(sorted(files_to_compress), desc="Compressing files", unit="file"):
                    arcname = self._normalize_path(file_path)
                    
                    # Try to normalize file content (only if enabled in YAML)
                    normalized_content = None
                    if self.yaml_data.normalize_content:
                        normalized_content = self._normalize_file_content(file_path)
                    
                    if normalized_content is not None:
                        tqdm.write(f"Compressing (normalized): {file_path} -> {arcname}")
                        tarinfo = tar.gettarinfo(file_path, arcname=arcname)
                        tarinfo.size = len(normalized_content)
                        tar.addfile(tarinfo, fileobj=io.BytesIO(normalized_content))
                    else:
                        tqdm.write(f"Compressing: {file_path} -> {arcname}")
                        tar.add(file_path, arcname=arcname)
                    
                    # Update state
                    
                    # NOTE: state will be updated for all current files after
                    # compression to ensure the saved .backup-state.json reflects
                    # the complete snapshot (not only the changed files).
            else:
                for file_path in sorted(files_to_compress):
                    arcname = self._normalize_path(file_path)
                    
                    # Try to normalize file content (only if enabled in YAML)
                    normalized_content = None
                    if self.yaml_data.normalize_content:
                        normalized_content = self._normalize_file_content(file_path)
                    
                    if normalized_content is not None:
                        tarinfo = tar.gettarinfo(file_path, arcname=arcname)
                        tarinfo.size = len(normalized_content)
                        tar.addfile(tarinfo, fileobj=io.BytesIO(normalized_content))
                    else:
                        tar.add(file_path, arcname=arcname)
                    
                    # Update state
                    
                    # NOTE: state will be updated for all current files after
                    # compression to ensure the saved .backup-state.json reflects
                    # the complete snapshot (not only the changed files).
        
        # Save state after compression
        # Ensure state contains entries for all files currently present so
        # subsequent incremental runs can correctly detect unchanged files.
        for file_path in file_list:
            try:
                new_state.update_file(file_path)
            except (OSError, IOError):
                # Ignore per-file errors when gathering state
                pass

        new_state.save()
