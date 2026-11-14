from typing import Annotated
from uuid import uuid4

from beanie import Document
from pydantic import Field, StringConstraints
from pymongo import DESCENDING, IndexModel


DiscordId = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9]+$")]


AUDIT_LOG_INDEXES = [
    IndexModel([("recorded_at_ns", DESCENDING)], name="audit_log_recorded_at_desc"),
]


class AuditLogEntry(Document):
    id: str = Field(default_factory=lambda: uuid4().hex, alias="_id")
    recorded_at_ns: int = Field(ge=0)
    command: str
    author_id: DiscordId
    arguments: dict[str, str | int | bool] = Field(default_factory=dict)

    class Settings:
        name = "audit_log_entries"
        indexes = AUDIT_LOG_INDEXES
        use_state_management = True


__all__ = ["DiscordId", "AUDIT_LOG_INDEXES", "AuditLogEntry"]
