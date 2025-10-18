"""Module providing a yaml and json parser with pydantic validation"""
import yaml
from config_saver.lib.models.model import Model

class Parser:
    """Class representing a yaml and json parser for our model"""
    def __init__(self, filename: str):
        self.filename = filename
        # Let exceptions propagate to the caller for handling (no exit()/print here)
        with open(self.filename, "r", encoding="utf-8") as yaml_file:
            yaml_data = yaml.safe_load(yaml_file)
            validated_data = Model.model_validate(yaml_data)
            # store both the pydantic Model instance and a dumped dict
            self._model = validated_data
            self._data = validated_data.model_dump()

    def get_attr(self, attr_name: str):
        """Get an attribute from the parsed data"""
        return self._data.get(attr_name, None)

    def get_data(self):
        """Return the parsed data as a dictionary"""
        return self._data

    def get_model(self) -> Model:
        """Return the validated pydantic Model instance."""
        return self._model
