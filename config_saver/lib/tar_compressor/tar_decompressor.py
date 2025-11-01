"""Module providing a tar decompressor that extracts files to their original directories"""
import os
import tarfile
from typing import Optional

from colorama import Fore, init
from tqdm import tqdm

init(autoreset=True)

# Placeholder for user home directory in file contents (must match compressor)
HOME_CONTENT_PLACEHOLDER = "<<<HOME_PLACEHOLDER>>>"



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

    def _is_text_file_content(self, content: bytes) -> bool:
        """Check if content is likely text (not binary)"""
        # Check for null bytes
        if b'\0' in content[:8192]:
            return False
        # Try to decode as UTF-8
        try:
            content[:8192].decode('utf-8')
            return True
        except UnicodeDecodeError:
            return False

    def _denormalize_file_content(self, content: bytes) -> bytes:
        """Replace HOME_CONTENT_PLACEHOLDER with actual user home in file content"""
        if not self._is_text_file_content(content):
            return content
        
        try:
            # Try UTF-8 first
            text_content = content.decode('utf-8')
            if HOME_CONTENT_PLACEHOLDER in text_content:
                text_content = text_content.replace(HOME_CONTENT_PLACEHOLDER, self.user_home)
                return text_content.encode('utf-8')
        except UnicodeDecodeError:
            # Try latin-1
            try:
                text_content = content.decode('latin-1')
                if HOME_CONTENT_PLACEHOLDER in text_content:
                    text_content = text_content.replace(HOME_CONTENT_PLACEHOLDER, self.user_home)
                    return text_content.encode('latin-1')
            except UnicodeDecodeError:
                pass
        
        return content

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
                        
                        # Extract and denormalize content
                        if member.isfile():
                            file_obj = tar.extractfile(member)
                            if file_obj:
                                content = file_obj.read()
                                denormalized_content = self._denormalize_file_content(content)
                                
                                # Write to output directory
                                output_file_path = os.path.join(extract_path, member.name)
                                os.makedirs(os.path.dirname(output_file_path), exist_ok=True)
                                
                                with open(output_file_path, 'wb') as f:
                                    f.write(denormalized_content)
                                
                                # Restore permissions
                                os.chmod(output_file_path, member.mode)
                                
                                if self.show_progress:
                                    tqdm.write(f"Extracting: {display_name}")
                        else:
                            # Directory or link - extract normally
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
                        
                        # Extract file and denormalize content
                        if member.isfile():
                            file_obj = tar.extractfile(member)
                            if file_obj:
                                content = file_obj.read()
                                denormalized_content = self._denormalize_file_content(content)
                                
                                # Write to actual path
                                with open(actual_path, 'wb') as f:
                                    f.write(denormalized_content)
                                
                                # Restore permissions
                                os.chmod(actual_path, member.mode)
                        else:
                            # Directory or link - extract normally
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
