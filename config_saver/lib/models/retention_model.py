"""Module providing the retention policy model"""

from pydantic import BaseModel, ConfigDict, Field


class RetentionModel(BaseModel):
    """How many snapshots to keep per time bucket (restic's --keep-* semantics)"""

    model_config = ConfigDict(extra="forbid")

    daily: int = Field(default=7, ge=0, description="Keep the last backup of each of N days")
    weekly: int = Field(default=4, ge=0, description="Keep the last backup of each of N ISO weeks")
    monthly: int = Field(default=12, ge=0, description="Keep the last backup of each of N months")
    yearly: int = Field(default=2, ge=0, description="Keep the last backup of each of N years")
