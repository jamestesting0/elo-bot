from typing import Dict, List, Optional

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import IndexModel

from elo_bot.core.finals import FINAL_DISTRICTS, FinalState, NodeState, Outcome, PredictionState

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
    final_summary: dict[str, str | int | None] = Field(default_factory=dict)

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

    class Settings:
        name = "elo_final_matches"
        indexes = [
            "id"
        ]
        use_state_management = True


class FinalResultProgress(BaseModel):
    helper_completed: bool = False
    completed: bool = False
    release_completed: bool = False
    send_attempted: bool = False
    message_id: str | None = None
    channel_id: str | None = None
    published: bool = False
    published_repair_pending: bool | None = None
    thread_deleted: bool = False
    error: str | None = None


class EloFinal(Document):
    season_number: int
    schema_version: int = 1
    season_id: PydanticObjectId | None = None
    guild_id: int | None = None
    channel_id: int | None = None
    active: bool = False
    state: FinalState = "REGISTRATION"
    setup_revision: int = 0
    result_revision: int = 0
    revision: int = 0
    entrant_ids: list[str] = Field(default_factory=list)
    seeds: dict[str, int] = Field(default_factory=dict)
    archived_elo: dict[str, int | None] = Field(default_factory=dict)
    byes: list[str] = Field(default_factory=list)
    nodes: dict[str, EloFinalMatch] = Field(default_factory=dict)
    manual_draft: dict[str, list[str | None]] = Field(default_factory=dict)
    draft_owner: str | None = None
    draft_revision: int = 0
    draft_setup_revision: int | None = None
    districts: list[str] = Field(default_factory=lambda: list(FINAL_DISTRICTS))
    prediction_state: PredictionState = "NEVER_OPENED"
    predictions_ever_opened: bool = False
    predictions_scoring_revision: int = 0
    current_round: str = "Quarterfinals"
    champion: str | None = None
    first_actual_started_at: int | None = None
    bracket_message_id: str | None = None
    bracket_send_attempted: bool = False
    bracket_send_nonce: str | None = None
    bracket_published_revision: int = -1
    publication_pending: bool = False
    publication_error: str | None = None
    repair_pending: bool = False
    repair_nodes: list[str] = Field(default_factory=list)
    result_progress: dict[str, FinalResultProgress] = Field(default_factory=dict)
    reopening_pending: bool = False
    cancel_reason: str | None = None
    completed_at: int | None = None
    quarter_finals: List[EloFinalMatch] = Field(default_factory=list)
    semi_finals: List[EloFinalMatch] = Field(default_factory=list)
    final: List[EloFinalMatch] = Field(default_factory=list)
    player_nicknames: Dict[str, str] = Field(default_factory=dict)

    class Settings:
        name = "elo_finals"
        use_state_management = True
        indexes = [
            IndexModel("season_number", unique=True, name="final_season_unique"),
            IndexModel("active", unique=True, partialFilterExpression={"active": True}, name="final_active_unique"),
        ]


__all__ = ["DistrictSeasonData", "PlayerSeasonData", "Season", "EloFinalMatch", "EloFinal"]
