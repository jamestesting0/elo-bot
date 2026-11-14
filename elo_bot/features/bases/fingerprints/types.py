from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from elo_bot.db.fingerprint_types import BaseFingerprint
from elo_bot.db.fingerprint_types import Building
from elo_bot.db.fingerprint_types import LEGACY_PIPELINE_ID


DISTRICTS = {
    "Capital Peak": 0, "Barbarian Camp": 1, "Wizard Valley": 2,
    "Balloon Lagoon": 3, "Builder's Workshop": 4, "Dragon Cliffs": 5,
    "Golem Quarry": 6, "Skeleton Park": 7, "Goblin Mines": 8,
}


class ImageInput(BaseModel):
    path: str
    source_revision: int = Field(ge=0)
    source_image_url: str
    district_id: int = Field(ge=0, le=8)
    pipeline_id: str = LEGACY_PIPELINE_ID


class ExtractionResult(BaseModel):
    fingerprint: BaseFingerprint | None = None
    error: str | None = None
    bounds: list[tuple[float, float, float, float]] = Field(default_factory=list, max_length=300)


class MatchScore(BaseModel):
    base_id: str
    source_revision: int
    similarity: float
    matched: int
    total: int


class LayoutMatch(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False, extra="forbid")
    minimum_matches: ClassVar[int] = 20
    status: Literal["matched", "insufficient", "ambiguous"]
    similarity: float | None = Field(default=None, ge=0, le=100)
    matched: int = Field(default=0, ge=0, le=300)
    total: int = Field(ge=0, le=300)
    residual: float | None = Field(default=None, ge=0)
    proposals: int = Field(default=0, ge=0, le=8192)
    distinct_proposals: int = Field(default=0, ge=0, le=8192)
    verified: int = Field(default=0, ge=0, le=32)
    assignment_rounds: int = Field(default=0, ge=0, le=96)
    truncated: bool = False

    @model_validator(mode="after")
    def validate_score(self):
        if self.matched > self.total or (self.status == "matched") != (self.similarity is not None):
            raise ValueError("Invalid match outcome")
        percentage = round(100 * self.matched / self.total, 1) if self.total else 0
        if self.similarity is not None and ((self.total == 0 and self.minimum_matches > 0) or
                                            self.matched < self.minimum_matches or self.residual is None or
                                            self.similarity != percentage):
            raise ValueError("Invalid match score")
        return self


class ComparisonFingerprint(BaseFingerprint):
    total_detections: int = Field(ge=0, le=300)
    buildings: list[Building] = Field(default_factory=list, max_length=300)


class ComparisonMatch(LayoutMatch):
    minimum_matches: ClassVar[int] = 0


class ComparisonSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: LayoutMatch | ComparisonMatch
    query_unmatched: int = Field(ge=0, le=300)
    candidate_unmatched: int = Field(ge=0, le=300)
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_counts(self):
        if (self.score.status != "matched" or
                self.score.total != self.score.matched + max(self.query_unmatched, self.candidate_unmatched)):
            raise ValueError("Invalid comparison counts")
        return self


class FingerprintError(Exception):
    """A feature failure safe to report without URLs or private layout data."""
