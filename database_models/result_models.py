from beanie import PydanticObjectId
from pydantic import BaseModel, Field

from .player_models import MatchPlayerStats


class ResultRecordSnapshot(BaseModel):
    id: str
    time: int
    matched_time: float | None = None
    is_challenge: bool = False
    players: dict[str, MatchPlayerStats]
    operation_key: str | None = None


class ResultBaseRollback(BaseModel):
    base_id: PydanticObjectId
    record: list[str | int | float]


class MatchResultOperation(BaseModel):
    key: str
    response: dict[str, str]
    created_at: float
    previous_record: ResultRecordSnapshot | None = None
    base_rollbacks: list[ResultBaseRollback] = Field(default_factory=list)
    record: ResultRecordSnapshot | None = None
    summary_message_id: int | None = None
    public_message_id: int | None = None
    completed: bool = False
    published: bool = False
    cancelled: bool = False
