"""Module providing the base model for the json with the file locations"""

from pydantic import BaseModel

from .specific_files_model import SpecificFilesModel

class JsonModel(BaseModel):
    """Class representing the model itself"""
    directories: list[str | SpecificFilesModel]
