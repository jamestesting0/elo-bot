from typing import Dict, List, Literal, Optional

from beanie import Document, Link
from pydantic import BaseModel, Field, conlist
from pymongo import IndexModel

from elo_bot.db.fingerprint_types import BaseFingerprint, ExtractionState, ImageAttachment
from .fingerprint_models import BASE_FINGERPRINT_INDEXES


class BaseLayout(Document):
    district: str
    link: str = Field(unique=True)
    image_url: str
    records: list = []
    operation_receipts: list[str] = Field(default_factory=list)
    expiration_date: float
    builder_id: Optional[str] = None
    quality: str
    image_revision: int = Field(default=0, ge=0)
    fingerprint: BaseFingerprint | None = None
    fingerprint_extraction: ExtractionState | None = None
    image_attachment: ImageAttachment | None = None

    class Settings:
        name = "base_layouts"
        indexes = [
            "district",
            IndexModel([("link", 1)], unique=True),
            "quality",
            "builder_id",
            [("district", 1), ("quality", 1)],
            [("district", 1), ("quality", 1), ("builder_id", 1)],
            *BASE_FINGERPRINT_INDEXES,
        ]
        use_state_management = True


class MatchDistrictStats(BaseModel):
    stars: int
    destruction: int
    base_quality: str | None = None


class MatchPlayerStats(BaseModel):
    stars: int
    destruction: int
    opponent_stars: int
    opponent_destruction: int
    opponent: str
    elo_change: int
    won: bool
    districts: Dict[str, MatchDistrictStats] = Field(default_factory=dict)
    confirmed_at: float | None = None
    confirmation_method: Literal["ready", "manual", "automatic"] | None = None


class RecordedMatch(Document):
    id: str
    operation_key: str | None = None
    manual_submission_key: str | None = None
    manual_submission_completed: bool = False
    time: int
    matched_time: float | None = None
    is_challenge: bool = Field(default=False)
    players: Dict[str, MatchPlayerStats]

    class Settings:
        name = "recorded_matches"
        indexes = [
            "id",
            "time"
        ]
        use_state_management = True


class Player(Document):
    id: str
    user_id: str
    tag: str
    elo: int
    highest_elo: int
    wins: int = 0
    avg_stars: float = 0.0
    avg_destruction: float = 0.0
    opponent_avg_stars: float = 0.0
    opponent_avg_destruction: float = 0.0

    matches: List[Link[RecordedMatch]] = Field(default_factory=list)
    autowin_receipts: dict[str, int] = Field(default_factory=dict)
    self_host_invitation_attempted: bool = False
    self_host_invitation_message_id: int | None = None

    last_cancel: Optional[float] = None
    priority: bool = False
    challenge: bool = True
    auto_queue: str = "0"
    capital_peak_enabled: bool = True
    blocked_users: List[str] = Field(default_factory=list)
    last_challenged: List[str] = Field(default_factory=list)

    district_presets: List[Optional[str]] = Field(default_factory=lambda: [None, None, None])
    secondary_district_presets: List[Optional[str]] = Field(default_factory=lambda: [None, None, None])
    base_presets: Dict[str, Optional[Link[BaseLayout]]] = Field(default_factory=dict)

    class Settings:
        name = "players"
        indexes = [
            "user_id",
            "tag",
            "elo",
            "wins",
            "avg_stars",
            "avg_destruction"
        ]
        use_state_management = True


class QueueDistrictSelection(BaseModel):
    attempt_id: str
    districts: conlist(str | None, min_length=3, max_length=3)
    confirmed: bool = False


class Queue(Document):
    tier: int
    players: List[Link[Player]] = Field(default_factory=list)
    entered_at: Dict[str, float] = Field(default_factory=dict)
    district_selections: dict[str, QueueDistrictSelection] = Field(default_factory=dict)

    class Settings:
        name = "queue"
        use_state_management = True
        state_management_replace_objects = True


__all__ = ["BaseLayout", "MatchDistrictStats", "MatchPlayerStats", "RecordedMatch", "Player", "Queue"]
