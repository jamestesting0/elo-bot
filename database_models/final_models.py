from typing import Literal

from beanie import Document
from pydantic import BaseModel, Field, model_validator
from pymongo import IndexModel

from common.finals.types import FINAL_DISTRICTS, Outcome
from common.results.validation import is_valid_match_result_entry
from database_models.system_models import HelperHistory


class FinalResultSnapshot(BaseModel):
    contestants: list[str]
    scores: dict[str, list[str]]
    winner: str | None = None
    outcome: Outcome = "Played"
    scheduled_times: list[int | None] = Field(default_factory=lambda: [None, None], min_length=2, max_length=2)
    actual_started_at: int | None = None

    @model_validator(mode="after")
    def validate_scores(self):
        if len(self.contestants) != 2 or len(set(self.contestants)) != 2:
            raise ValueError("A Final result needs two distinct contestants.")
        if set(self.scores) != set(self.contestants) or any(
            len(scores) != len(FINAL_DISTRICTS) or not all(is_valid_match_result_entry(score) for score in scores)
            for scores in self.scores.values()
        ):
            raise ValueError("Each contestant needs seven valid scores.")
        if self.winner is not None and self.winner not in self.contestants:
            raise ValueError("The winner must be a contestant.")
        if self.outcome == "Forfeit" and (self.winner is None or any(
            score not in {"0.00", "0.0"} for scores in self.scores.values() for score in scores
        )):
            raise ValueError("An unplayed forfeit needs zero scores and an explicit winner.")
        return self


class FinalResultOperation(BaseModel):
    operation_key: str
    result_id: str
    version: int
    before: FinalResultSnapshot | None = None
    after: FinalResultSnapshot
    completed: bool = False
    helper_completed: bool = False
    published: bool = False
    publication_error: str | None = None


class FinalHelperCredit(BaseModel):
    user_id: str
    receipt: str
    points: float
    history: HelperHistory | None = None


class FinalMatchResult(Document):
    id: str
    final_id: str
    season_number: int
    node_id: str
    contestant_revision: int
    attempt: int
    version: int
    operation_key: str
    source_match_id: str | None = None
    source_token: str | None = None
    source_thread: str | None = None
    actor_id: str
    reason: str | None = None
    previous_id: str | None = None
    created_at: int
    snapshot: FinalResultSnapshot
    bases: dict[str, list[str | None]] = Field(default_factory=dict)
    helper_credits: list[FinalHelperCredit] = Field(default_factory=list)

    class Settings:
        name = "final_match_results"
        use_state_management = True
        indexes = [
            IndexModel([("final_id", 1), ("node_id", 1), ("version", 1)], unique=True, name="final_result_version"),
            IndexModel("operation_key", unique=True, name="final_result_operation"),
            IndexModel([("season_number", 1), ("snapshot.contestants", 1)], name="final_result_history"),
        ]


class FinalEditionStats(BaseModel):
    season_number: int
    revision: int = 0
    played: int = 0
    wins: int = 0
    losses: int = 0
    byes: int = 0
    forfeits: int = 0
    stars: int = 0
    destruction: int = 0
    district_count: int = 0
    placement: str | None = None


class FinalPlayerProfile(Document):
    id: str
    user_id: str
    player_id: str
    editions: dict[str, FinalEditionStats] = Field(default_factory=dict)

    class Settings:
        name = "final_player_profiles"
        use_state_management = True
        indexes = [IndexModel("user_id", unique=True, name="final_profile_user")]


class FinalPrediction(Document):
    id: str
    final_id: str
    season_number: int
    user_id: str
    setup_revision: int
    session_revision: int = 0
    stage: int = Field(default=0, ge=0, le=3)
    draft: dict[str, str] = Field(default_factory=dict)
    submitted: dict[str, str] = Field(default_factory=dict)
    submitted_at: int | None = None
    scoring_revision: int = 0
    correct: dict[str, bool] = Field(default_factory=dict)
    eligible: int = 0
    points: int = 0

    class Settings:
        name = "final_predictions"
        use_state_management = True
        indexes = [
            IndexModel([("final_id", 1), ("user_id", 1)], unique=True, name="final_prediction_owner"),
            IndexModel([("season_number", 1), ("points", -1)], name="final_prediction_ranking"),
        ]
