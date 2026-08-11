"""Module providing a SpecificFilesModel"""

from pydantic import BaseModel, ConfigDict


class SpecificFilesModel(BaseModel):
    """Class representing a directory with only some files to export"""

    model_config = ConfigDict(extra="forbid")

    source: str
    files: list[str]
