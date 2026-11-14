from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LEGACY_PIPELINE_ID = "capital-v1-4d63ddd9"


PREVIOUS_PIPELINE_ID = "capital-v2-relative-4d63ddd9"


PREVIOUS_SAHI_PIPELINE_ID = "capital-v2-sahi448-4d63ddd9"


PIPELINE_ID = "capital-v2-sahi448-hall1-4d63ddd9"


MODEL_SHA256 = "4d63ddd9a95351c6cc9da0fe964e1e715357f268b9efa01ee3874d43d332a271"


PIPELINE_IDENTITIES = {
    LEGACY_PIPELINE_ID: (1, MODEL_SHA256, "elo-1", "hall-relative-image-xy-v1"),
    PREVIOUS_PIPELINE_ID: (2, MODEL_SHA256, "elo-2", "image-normalized-xy-v2"),
    PREVIOUS_SAHI_PIPELINE_ID: (2, MODEL_SHA256, "elo-3", "image-normalized-xy-v2"),
    PIPELINE_ID: (2, MODEL_SHA256, "elo-4", "image-normalized-xy-v2"),
}


HALL_TYPES = frozenset({"capital_peak", "district_hall"})


Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def utcnow() -> datetime:
    return datetime.now(UTC)


class Building(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False, extra="forbid")
    type: Literal[
        "air_bomb", "air_defence", "blast_bow", "bomb_tower", "cannon", "capital_peak", "crusher",
        "district_hall", "gaint_cannon", "hive", "inferno_tower", "motor", "multi_cannon", "post_cannon",
        "post_dragon", "post_gaint", "post_goblin", "rapid_rocket", "reflector", "rocket_artilery", "spear",
        "tesla", "wizard_tower",
    ]
    x: float = Field(ge=-1, le=1)
    y: float = Field(ge=-1, le=1)
    conf: float = Field(ge=0, le=1)


class BaseFingerprint(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False, extra="forbid")
    schema_version: Literal[1, 2] = 1
    pipeline_id: str
    model_sha256: Digest
    extractor_revision: str
    coordinate_system: str
    source_revision: int = Field(ge=0)
    source_image_sha256: Digest
    source_image_url: str
    district_id: int = Field(ge=0, le=8)
    generated_at: datetime
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    anchor_type: Literal["capital_peak", "district_hall"] | None
    anchor_found: bool = True
    total_detections: int = Field(gt=0, le=300)
    confidence_threshold: float = Field(ge=0, le=1)
    inference_runs: int = Field(ge=1, le=3)
    expected_count_match: bool
    warnings: list[str] = Field(default_factory=list)
    buildings: list[Building] = Field(min_length=1, max_length=300)

    @field_validator("warnings")
    @classmethod
    def remove_redundant_count_warning(cls, warnings: list[str]) -> list[str]:
        return [warning for warning in warnings if warning != "Unexpected building count; compare screenshots manually."]

    @model_validator(mode="after")
    def validate_layout(self):
        identity = (self.schema_version, self.model_sha256, self.extractor_revision, self.coordinate_system)
        if PIPELINE_IDENTITIES.get(self.pipeline_id) != identity:
            raise ValueError("Unsupported fingerprint identity")
        if len(self.buildings) != self.total_detections:
            raise ValueError("Detection count is invalid")
        anchors = [b for b in self.buildings if b.type in HALL_TYPES]
        if self.pipeline_id == PIPELINE_ID and len(anchors) > 1:
            raise ValueError("Current extraction permits at most one hall")
        expected = "capital_peak" if self.district_id == 0 else "district_hall"
        found = len(anchors) == 1 and anchors[0].type == expected
        if self.anchor_found != found or self.anchor_type != (expected if found else None):
            raise ValueError("Hall metadata does not match detections")
        match self.schema_version:
            case 1:
                if not found or anchors[0].x != 0 or anchors[0].y != 0:
                    raise ValueError("Hall anchor must define the origin")
            case 2:
                if any(b.x < 0 or b.y < 0 for b in self.buildings):
                    raise ValueError("Image coordinates must be in [0, 1]")
        return self


class ExtractionState(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    status: Literal["pending", "running", "ready", "failed"] = "pending"
    source_revision: int = Field(ge=0)
    pipeline_id: str = LEGACY_PIPELINE_ID
    requested_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    error_code: str | None = None
    claim_token: str | None = None
    lease_expires_at: datetime | None = None
    completion_receipt: str | None = None


class ImageAttachment(BaseModel):
    channel_id: str
    message_id: str
    attachment_id: str
