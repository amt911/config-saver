"""Module providing a yaml and json parser with pydantic validation"""
import yaml
from pydantic import ValidationError
from termcolor import colored
from lib.models.model import Model

class Parser:
    """Class representing a yaml and json parser for our model"""
    def __init__(self, filename: str):
        self.filename = filename
        try:
            with open(self.filename, "r") as yaml_file:
                try:
                    yaml_data = yaml.safe_load(yaml_file)
                    validated_data = Model.model_validate(yaml_data)
                except ValidationError as e:
                    print(e)
                    exit(2)
                self._data = validated_data.model_dump()
        except FileNotFoundError:
            print(colored("File does not exist!", "red"))
            exit(1)

    def get_attr(self, attr_name: str):
        return self._data.get(attr_name, None)

    def debug(self):
        return self._data
