"""Main module to demonstrate YAML parsing and tar compression"""
from colorama import init, Fore

from lib.tar_compressor.tar_compressor import TarCompressor
from lib.parser.parser import Parser
from lib.models.model import Model
init(autoreset=True)

def main():
    """Main function to demonstrate YAML parsing and tar compression"""
    parser = Parser("configs/default/config.yaml")
    try:
        validated = Model.model_validate(parser.get_data())
    except (ValueError, TypeError) as e:
        print(Fore.RED + "Validation error:", e)
        return
    try:
        compressor = TarCompressor(validated, "/home/andres/output.tar.gz")
        compressor.compress()
        print(Fore.GREEN + "Compression completed successfully.")
    except (OSError, RuntimeError) as e:
        print(Fore.RED + "Compression error:", e)

if __name__ == "__main__":
    main()
