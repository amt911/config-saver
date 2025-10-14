"""Module providing a SpecificFilesModel"""
from pydantic import BaseModel

class SpecificFilesModel(BaseModel):
    """Class representing a directory with only some files to export"""
    source: str
    files: list[str]