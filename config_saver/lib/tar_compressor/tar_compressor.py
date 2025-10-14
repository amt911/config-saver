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
        self.yaml_data = yaml_data
        self.output_path = output_path
        self.base_dir = base_dir or os.getcwd()
        self.show_progress = show_progress

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
                    arcname = os.path.normpath(file_path)
                    if arcname.startswith(os.sep):
                        arcname = arcname[1:]
                    tqdm.write(f"Compressing: {file_path}")
                    tar.add(file_path, arcname=arcname)
            else:
                for file_path in file_list:
                    arcname = os.path.normpath(file_path)
                    if arcname.startswith(os.sep):
                        arcname = arcname[1:]
                    tar.add(file_path, arcname=arcname)
