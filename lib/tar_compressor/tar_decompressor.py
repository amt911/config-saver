"""Module providing a tar decompressor that extracts files to their original directories"""
import tarfile
import os
from colorama import Fore, init
init(autoreset=True)

from typing import Optional

class TarDecompressor:
    """Class representing a tar decompressor"""
    def __init__(self, tar_path: str, output_dir: Optional[str] = None):
        self.tar_path = tar_path
        self.output_dir = output_dir

    def decompress(self):
        """Extract all files and folders from the tar archive to their original structure or absolute paths"""
        if not os.path.exists(self.tar_path):
            print(Fore.RED + f"[ERROR] Tar file '{self.tar_path}' does not exist.")
            return
        with tarfile.open(self.tar_path, "r:gz") as tar:
            try:
                if self.output_dir:
                    tar.extractall(path=self.output_dir)
                    print(Fore.GREEN + f"Extraction completed successfully in '{self.output_dir}'.")
                else:
                    for member in tar.getmembers():
                        # Construir ruta absoluta
                        abs_path = os.path.join(os.sep, member.name.lstrip(os.sep))
                        abs_dir = os.path.dirname(abs_path)
                        if not os.path.exists(abs_dir):
                            os.makedirs(abs_dir, exist_ok=True)
                        tar.extract(member, path=os.sep)
                    print(Fore.GREEN + "Extraction completed successfully to absolute paths.")
            except Exception as e:
                print(Fore.RED + f"[ERROR] Extraction failed: {e}")
