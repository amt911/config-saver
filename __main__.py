"""Main module to demonstrate YAML parsing and tar compression"""
from lib.tar_compressor.tar_compressor import TarCompressor
# Ejemplo de parser para YAML en el main
from lib.parser.parser import Parser
from lib.models.model import Model

def main():
    """Main function to demonstrate YAML parsing and tar compression"""
    parser = Parser("configs/default/config.yaml")
    try:
        validated = Model.model_validate(parser.debug())
        print("YAML válido:", validated.model_dump(), type(validated))
    except (ValueError, TypeError) as e:
        print("Validation error:", e)
        return
    try:
        compressor = TarCompressor(validated, "/home/andres/output.tar.gz")
        compressor.compress()
        print("Compression completed successfully.")
    except (OSError, RuntimeError) as e:
        print("Compression error:", e)

if __name__ == "__main__":
    main()
