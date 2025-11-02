"""Module providing the base model for the json with the file locations"""

from pydantic import BaseModel, Field

from .specific_files_model import SpecificFilesModel

class Model(BaseModel):
    """Class representing the model itself"""
    directories: list[str | SpecificFilesModel]
    normalize_content: bool = Field(default=False, description="Enable content normalization (replace home paths in text files)")
