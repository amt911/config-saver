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
        # Get current user's home directory for path denormalization
        self.user_home = os.path.expanduser("~")

    def _denormalize_path(self, archived_path: str) -> str:
        """Denormalize path by replacing 'home/user/' placeholder with actual user's home directory"""
        # Check if the path starts with our placeholder
        if archived_path.startswith("home/user/") or archived_path.startswith("home/user\\"):
            # Replace home/user with the actual user's home directory
            # Remove 'home/user/' prefix
            relative_part = archived_path[10:]  # len("home/user/") = 10
            # Construct the actual path
            actual_path = os.path.join(self.user_home, relative_part)
            return actual_path
        
        # For other paths, treat as absolute (with leading /)
        return os.path.join(os.sep, archived_path.lstrip(os.sep))

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
                        # User specified an output directory, extract there
                        extract_path = self.output_dir
                        display_name = member.name
                        # Extract with original arcname
                        if self.show_progress:
                            tqdm.write(f"Extracting: {display_name}")
                        tar.extract(member, path=extract_path)
                    else:
                        # Denormalize the path to restore to the correct location
                        actual_path = self._denormalize_path(member.name)
                        actual_dir = os.path.dirname(actual_path)
                        
                        # Create directory structure if needed
                        if not os.path.exists(actual_dir):
                            os.makedirs(actual_dir, exist_ok=True)
                        
                        # Show progress info if enabled
                        if self.show_progress:
                            tqdm.write(f"Extracting: {member.name} -> {actual_path}")
                        
                        # Extract the member
                        # We need to extract to a temp location and then move
                        # OR we can change member.name temporarily
                        original_name = member.name
                        # Remove the archive prefix to get relative path from root
                        if actual_path.startswith(os.sep):
                            member.name = actual_path[1:]  # Remove leading /
                        else:
                            member.name = actual_path
                        
                        tar.extract(member, path=os.sep)
                        
                        # Restore original name for next iteration
                        member.name = original_name
                        
                # Success message
                if self.output_dir:
                    print(Fore.GREEN + f"Extraction completed successfully in '{self.output_dir}'.")
                else:
                    print(Fore.GREEN + "Extraction completed successfully to absolute paths.")
        except (tarfile.TarError, OSError, IOError) as e:
            print(Fore.RED + f"[ERROR] Extraction failed: {e}")
