"""Module providing a tar compressor based on a YAML configuration with pydantic validation"""
import tarfile
import os
from colorama import init, Fore
init(autoreset=True)

from lib.models.model import Model

class TarCompressor:
    """Class representing a tar compressor"""
    def __init__(self, yaml_data: Model, output_path: str = "output.tar.gz"):
        self.yaml_data = yaml_data
        self.output_path = output_path

    def compress(self):
        """Compress files and directories based on the provided YAML data"""
        with tarfile.open(self.output_path, "w:gz") as tar:
            for entry in self.yaml_data.directories:
                if isinstance(entry, str):
                    if not os.path.exists(entry):
                        print(Fore.YELLOW + f"[WARNING] Directory '{entry}' does not exist. Skipping.")
                        continue
                    tar.add(entry, arcname=os.path.basename(entry))
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
                        tar.add(file_path, arcname=os.path.join(os.path.basename(source), file))
