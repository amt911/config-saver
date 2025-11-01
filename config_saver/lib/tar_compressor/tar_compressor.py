"""Module providing a tar compressor based on a YAML configuration with pydantic validation"""
import os
import tarfile
from typing import Optional

from colorama import init
from tqdm import tqdm

from config_saver.lib.models.model import Model

init(autoreset=True)



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
                            file_list.append(file_path)

        with tarfile.open(self.output_path, "w:gz") as tar:
            if self.show_progress:
                for file_path in tqdm(file_list, desc="Compressing files", unit="file"):
                    arcname = self._normalize_path(file_path)
                    tqdm.write(f"Compressing: {file_path} -> {arcname}")
                    tar.add(file_path, arcname=arcname)
            else:
                for file_path in file_list:
                    arcname = self._normalize_path(file_path)
                    tar.add(file_path, arcname=arcname)
