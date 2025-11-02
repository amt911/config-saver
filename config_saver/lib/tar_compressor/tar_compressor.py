"""Module providing a tar compressor based on a YAML configuration with pydantic validation"""
import io
import os
import tarfile
from typing import Optional

from colorama import init
from tqdm import tqdm

from config_saver.lib.models.model import Model

init(autoreset=True)

# Placeholder for user home directory in file contents
HOME_CONTENT_PLACEHOLDER = "<<<HOME_PLACEHOLDER>>>"



class TarCompressor:
    """Class representing a tar compressor"""
    def __init__(self, yaml_data: Model, output_path: str = "output.tar.gz", base_dir: Optional[str] = None, show_progress: bool = False):
        # yaml_data is expected to be a validated Model instance
        self.yaml_data = yaml_data
        self.output_path = output_path
        self.base_dir = base_dir or os.getcwd()
        self.show_progress = show_progress
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
        """Compress files and directories with a global progress bar for all files, showing current file name."""
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
                            # Check if it's a directory - if so, walk it recursively
                            if os.path.isdir(file_path):
                                for root, _, files in os.walk(file_path):
                                    for f in files:
                                        file_list.append(os.path.join(root, f))
                            else:
                                # It's a file, add it directly
                                file_list.append(file_path)

        with tarfile.open(self.output_path, "w:gz") as tar:
            if self.show_progress:
                for file_path in tqdm(file_list, desc="Compressing files", unit="file"):
                    arcname = self._normalize_path(file_path)
                    
                    # Try to normalize file content (only if enabled in YAML)
                    normalized_content = None
                    if self.yaml_data.normalize_content:
                        normalized_content = self._normalize_file_content(file_path)
                    
                    if normalized_content is not None:
                        # File content was normalized, add from memory
                        tqdm.write(f"Compressing (normalized): {file_path} -> {arcname}")
                        tarinfo = tar.gettarinfo(file_path, arcname=arcname)
                        tarinfo.size = len(normalized_content)
                        tar.addfile(tarinfo, fileobj=io.BytesIO(normalized_content))
                    else:
                        # Add file as-is
                        tqdm.write(f"Compressing: {file_path} -> {arcname}")
                        tar.add(file_path, arcname=arcname)
            else:
                for file_path in file_list:
                    arcname = self._normalize_path(file_path)
                    
                    # Try to normalize file content (only if enabled in YAML)
                    normalized_content = None
                    if self.yaml_data.normalize_content:
                        normalized_content = self._normalize_file_content(file_path)
                    
                    if normalized_content is not None:
                        # File content was normalized, add from memory
                        tarinfo = tar.gettarinfo(file_path, arcname=arcname)
                        tarinfo.size = len(normalized_content)
                        tar.addfile(tarinfo, fileobj=io.BytesIO(normalized_content))
                    else:
                        # Add file as-is
                        tar.add(file_path, arcname=arcname)
