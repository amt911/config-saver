"""Module providing a tar compressor based on a YAML configuration with pydantic validation"""
import os
import tarfile
from typing import Optional

from colorama import Fore, init

from lib.models.model import Model

init(autoreset=True)



class TarCompressor:
    """Class representing a tar compressor"""
    def __init__(self, yaml_data: Model, output_path: str = "output.tar.gz", base_dir: Optional[str] = None):
        self.yaml_data = yaml_data
        self.output_path = output_path
        self.base_dir = base_dir or os.getcwd()

    def compress(self):
        """Compress files and directories based on the provided YAML data, preserving absolute folder structure inside the tar"""
        with tarfile.open(self.output_path, "w:gz") as tar:
            for entry in self.yaml_data.directories:
                if isinstance(entry, str):
                    if not os.path.exists(entry):
                        print(Fore.YELLOW + f"[WARNING] Directory '{entry}' does not exist. Skipping.")
                        continue
                    arcname = os.path.normpath(entry)
                    if arcname.startswith(os.sep):
                        arcname = arcname[1:]
                    tar.add(entry, arcname=arcname)
                else:
                    source = entry.source
                    if not os.path.exists(source):
                        print(Fore.YELLOW + f"[WARNING] Directory '{source}' does not exist. Skipping.")
                        continue
                    for file in entry.files:
                        file_path = os.path.join(source, file)
                        if not os.path.exists(file_path):
                            print(Fore.YELLOW + f"[WARNING] File '{file_path}' does not exist. Skipping.")
                            continue
                        arcname = os.path.normpath(file_path)
                        if arcname.startswith(os.sep):
                            arcname = arcname[1:]
                        tar.add(file_path, arcname=arcname)
