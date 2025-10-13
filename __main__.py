# Ejemplo de parser para YAML en el main
from lib.parser.parser import Parser
from lib.models.model import Model

def main():
	parser = Parser("configs/default/config.yaml")
	try:
		validated = Model.model_validate(parser.debug())
		print("YAML válido:", validated.model_dump())
	except Exception as e:
		print("Error de validación:", e)

if __name__ == "__main__":
	main()