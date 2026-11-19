from typing import Dict, List, Optional, get_args

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import IndexModel

from elo_bot.core.finals import FinalState, NodeState, Outcome, PredictionState

from .player_models import MatchPlayerStats


class DistrictSeasonData(BaseModel):
    builder_id: str
    attacks: int
    average_stars: float
    average_destruction: float


class PlayerSeasonData(BaseModel):
    player_id: str
    elo: int


class Season(Document):
    season_number: int
    start: int
    end: int
    players: List[PlayerSeasonData] = Field(default_factory=dict)
    districts: Dict[str, List[DistrictSeasonData]] = Field(default_factory=dict)

    class Settings:
        name = "seasons"
        use_state_management = True


class EloFinalMatch(BaseModel):
    id: str
    players: Optional[Dict[str, Optional[MatchPlayerStats]]] = Field(default_factory=dict)
    winner: Optional[str] = None
    contestants: list[str | None] = Field(default_factory=lambda: [None, None])
    feeders: list[str] = Field(default_factory=list)
    state: NodeState = "WAITING"
    bye: bool = False
    contestant_revision: int = 1
    attempt: int = 0
    match_id: PydanticObjectId | None = None
    thread_id: str | None = None
    result_id: str | None = None
    result_version: int = 0
    outcome: Outcome | None = None
    first_actual_started_at: int | None = None
    start_claim: str | None = None
    scheduled_times: list[int | None] = Field(default_factory=lambda: [None, None], min_length=2, max_length=2)
    scheduled_is_war: bool | None = None
    actual_started_at: int | None = None
    provisioning: bool = False
    provisioning_error: str | None = None

class FinalResultProgress(BaseModel):
    helper_completed: bool = False
    completed: bool = False
    release_completed: bool = False
    send_attempted: bool = False
    send_nonce: str | None = None
    message_id: str | None = None
    channel_id: str | None = None
    public_send_attempted: bool = False
    public_send_nonce: str | None = None
    public_message_id: str | None = None
    public_published_repair_pending: bool | None = None
    image_error: str | None = None
    published: bool = False
    published_repair_pending: bool | None = None
    thread_deleted: bool = False
    error: str | None = None


class EloFinal(Document):
    season_number: int
    schema_version: int = 1
    guild_id: int | None = None
    channel_id: int | None = None
    active: bool = False
    state: FinalState = "REGISTRATION"
    setup_revision: int = 0
    result_revision: int = 0
    revision: int = 0
    entrant_ids: list[str] = Field(default_factory=list)
    # Each entrant's Elo when they were added; it seeds the bracket.
    seed_elo: dict[str, int | None] = Field(default_factory=dict)
    nodes: dict[str, EloFinalMatch] = Field(default_factory=dict)
    manual_draft: dict[str, list[str | None]] = Field(default_factory=dict)
    draft_owner: str | None = None
    draft_revision: int = 0
    draft_setup_revision: int | None = None
    prediction_state: PredictionState = "NEVER_OPENED"
    predictions_ever_opened: bool = False
    predictions_scoring_revision: int = 0
    champion: str | None = None
    first_actual_started_at: int | None = None
    bracket_message_id: str | None = None
    bracket_send_attempted: bool = False
    bracket_send_nonce: str | None = None
    bracket_signature: str | None = None
    publication_pending: bool = False
    publication_error: str | None = None
    repair_pending: bool = False
    repair_nodes: list[str] = Field(default_factory=list)
    result_progress: dict[str, FinalResultProgress] = Field(default_factory=dict)
    cancel_reason: str | None = None
    completed_at: int | None = None
    # When the archived match threads are deleted; cleared once they are gone.
    thread_cleanup_at: int | None = None
    quarter_finals: List[EloFinalMatch] = Field(default_factory=list)
    semi_finals: List[EloFinalMatch] = Field(default_factory=list)
    final: List[EloFinalMatch] = Field(default_factory=list)
    player_nicknames: Dict[str, str] = Field(default_factory=dict)
    # Entrants whose name was set by a manager; everyone else follows their Discord display name.
    nickname_overrides: list[str] = Field(default_factory=list)

    class Settings:
        name = "elo_finals"
        use_state_management = True
        indexes = [
            # A cancelled Final frees its season, so only the other states claim it.
            IndexModel("season_number", unique=True, name="final_season_claim", partialFilterExpression={
                "state": {"$in": [state for state in get_args(FinalState.__value__) if state != "CANCELLED"]}}),
            IndexModel("active", unique=True, partialFilterExpression={"active": True}, name="final_active_unique"),
        ]


__all__ = ["DistrictSeasonData", "PlayerSeasonData", "Season", "EloFinalMatch", "EloFinal"]
