"""Module providing a tar decompressor that extracts files to their original directories"""
import os
import tarfile
from typing import Optional

from colorama import Fore, init
from tqdm import tqdm

init(autoreset=True)



class TarDecompressor:
    """Class representing a tar decompressor"""
    def __init__(self, tar_path: str, output_dir: Optional[str] = None, show_progress: bool = False):
        self.tar_path = tar_path
        self.output_dir = output_dir
        self.show_progress = show_progress

    def decompress(self):
        """Extract all files and folders from the tar archive to their original structure or absolute paths, with optional progress bar"""
        if not os.path.exists(self.tar_path):
            print(Fore.RED + f"[ERROR] Tar file '{self.tar_path}' does not exist.")
            return
        try:
            with tarfile.open(self.tar_path, "r:gz") as tar:
                members = tar.getmembers()
                iterator = tqdm(members, desc="Extracting files", unit="file") if self.show_progress else members
                for member in iterator:
                    # Determine extraction path
                    if self.output_dir:
                        extract_path = self.output_dir
                        display_name = member.name
                    else:
                        abs_path = os.path.join(os.sep, member.name.lstrip(os.sep))
                        abs_dir = os.path.dirname(abs_path)
                        if not os.path.exists(abs_dir):
                            os.makedirs(abs_dir, exist_ok=True)
                        extract_path = os.sep
                        display_name = abs_path
                    # Show progress info if enabled
                    if self.show_progress:
                        tqdm.write(f"Extracting: {display_name}")
                    tar.extract(member, path=extract_path)
                # Success message
                if self.output_dir:
                    print(Fore.GREEN + f"Extraction completed successfully in '{self.output_dir}'.")
                else:
                    print(Fore.GREEN + "Extraction completed successfully to absolute paths.")
        except (tarfile.TarError, OSError, IOError) as e:
            print(Fore.RED + f"[ERROR] Extraction failed: {e}")
