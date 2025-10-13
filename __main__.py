from lib.tar_compressor.tar_compressor import TarCompressor
# Ejemplo de parser para YAML en el main
from lib.parser.parser import Parser
from lib.models.model import Model

def main():
	parser = Parser("configs/default/config.yaml")
	try:
		validated = Model.model_validate(parser.debug())
		print("YAML válido:", validated.model_dump(), type(validated))
		compressor = TarCompressor(validated, "/home/andres/output.tar.gz")
		compressor.compress()
		print("Comprimido generado correctamente.")
	except Exception as e:
		print("Error de validación o compresión:", e)

if __name__ == "__main__":
	main()