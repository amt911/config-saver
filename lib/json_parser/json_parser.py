"""Module providing a json parser"""
import json

from pydantic import ValidationError
from termcolor import colored

from lib.models.json_model import JsonModel


class JsonParser:
    """Class representing a json parser for our model"""
    def __init__(self, filename : str):
        self.filename = filename

        try:
            with open(self.filename, "r") as json_file:
                try:
                    json_data = json.load(json_file)

                    json_validated_data = JsonModel.model_validate(json_data)
                except ValidationError as e:
                    print(e)
                    exit(2)

                self._data = json_validated_data.model_dump()

        except FileNotFoundError:
            print(colored("File does not exist!", "red"))
            exit(1)

    def get_attr(self, attr_name : str):
        if not attr_name in self._data:
            return None

        return self._data[attr_name]

    def debug(self):
        return self._data
