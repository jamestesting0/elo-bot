from typing import Literal

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel


type SequentialRunState = Literal["CREATING", "UNCERTAIN", "ACTIVE", "TERMINAL"]

# Live runs host both players at once, so they occupy a side value of their own.
LIVE_SIDE = -1

SEQUENTIAL_RUN_INDEXES = [
    IndexModel(
        [("match_id", ASCENDING), ("side", ASCENDING)],
        name="sequential_run_open_side",
        unique=True,
        partialFilterExpression={"open": True},
    ),
    IndexModel([("host_clan_tag", ASCENDING), ("open", ASCENDING)], name="sequential_run_clan"),
    IndexModel([("thread_id", ASCENDING), ("created_at", DESCENDING)], name="sequential_run_thread"),
    IndexModel([("open", ASCENDING), ("created_at", DESCENDING)], name="sequential_run_open"),
]


class SequentialRunEvent(BaseModel):
    at: float
    kind: str
    detail: str = ""
    actor_id: str | None = None


class HelperSequentialRun(Document):
    match_id: PydanticObjectId
    match_token: str
    thread_id: str
    final_id: str
    final_node_id: str
    final_contestant_revision: int
    final_attempt: int
    side: int
    host_clan_tag: str
    player_tags: list[str]
    districts: list[str]
    create_request: dict
    requested_by: str
    previous_run_id: PydanticObjectId | None = None
    start_message_id: int | None = None
    state: SequentialRunState
    open: bool = True
    job_id: str | None = None
    job_status: str | None = None
    error: str | None = None
    close_reason: str | None = None
    snapshot: dict | None = None
    progress_channel_id: int | None = None
    progress_message_id: int | None = None
    created_at: float
    last_polled_at: float | None = None
    poll_failures: int = 0
    last_poll_error: str | None = None
    events: list[SequentialRunEvent] = Field(default_factory=list)

    class Settings:
        name = "helper_sequential_runs"
        indexes = SEQUENTIAL_RUN_INDEXES
        use_state_management = True


__all__ = ["LIVE_SIDE", "SEQUENTIAL_RUN_INDEXES", "HelperSequentialRun", "SequentialRunEvent"]
