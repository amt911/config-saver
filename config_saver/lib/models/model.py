"""Module providing the base model for the json with the file locations"""

from pydantic import BaseModel, ConfigDict, Field

from .encryption_model import EncryptionModel
from .specific_files_model import SpecificFilesModel


class Model(BaseModel):
    """Class representing the model itself"""

    # A backup config must fail fast: a typoed key means data the user believes is
    # being backed up silently is not.
    model_config = ConfigDict(extra="forbid")

    directories: list[str | SpecificFilesModel]
    exclude: list[str] = Field(
        default_factory=list,
        description=(
            "Patterns pruned from every archived tree. A pattern without a '/' matches the "
            "name of any path component at any depth (node_modules, *.log); one with a '/' "
            "is matched against the whole expanded path ($HOME/repos/*/build)."
        ),
    )
    normalize_content: bool = Field(
        default=False,
        description="Enable content normalization (replace home paths in text files)",
    )
    only_root_user: bool = Field(default=False, description="Restrict execution to root user only")
    encrypt: EncryptionModel | None = Field(
        default=None,
        description="Encrypt the resulting archive with age or gpg (a .tar.gz is not confidential)",
    )
