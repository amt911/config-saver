"""Module providing the optional encryption settings of a configuration."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EncryptionModel(BaseModel):
    """How to encrypt the archive produced by a configuration.

    Encryption is delegated to `age` or `gpg`; config-saver only calls them.
    """

    model_config = ConfigDict(extra="forbid")

    method: Literal["age", "gpg"] = "age"
    # Public keys (age) or key ids / user ids (gpg). At least one, otherwise the
    # archive would be encrypted to nobody.
    recipients: list[str] = Field(min_length=1)
