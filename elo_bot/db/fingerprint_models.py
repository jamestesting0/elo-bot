from datetime import datetime
from typing import Literal
from uuid import uuid4

from beanie import Document, PydanticObjectId
from pydantic import Field
from pymongo import IndexModel

from elo_bot.db.fingerprint_types import LEGACY_PIPELINE_ID, utcnow


BASE_FINGERPRINT_INDEXES = [
    IndexModel([("district", 1), ("fingerprint.pipeline_id", 1)]),
    IndexModel([("fingerprint_extraction.status", 1)]),
]
FINGERPRINT_TASK_INDEXES = [
    IndexModel([("run_id", 1), ("base_id", 1)], unique=True,
               partialFilterExpression={"run_id": {"$type": "string"}}),
    IndexModel([("state", 1), ("next_attempt_at", 1), ("lease_expires_at", 1)]),
    IndexModel([("base_id", 1), ("source_revision", 1)]),
]
EXTRACTION_RUN_INDEXES = [
    IndexModel([("active", 1)], unique=True, partialFilterExpression={"active": True}),
    IndexModel([("status", 1), ("created_at", 1)]),
]


class BaseFingerprintTask(Document):
    id: str = Field(default_factory=lambda: str(uuid4()), alias="_id")
    base_id: PydanticObjectId
    source_revision: int
    source_image_url: str
    district: str
    pipeline_id: str = LEGACY_PIPELINE_ID
    run_id: str | None = None
    predecessor_request_id: str | None = None
    state: Literal["pending", "running", "succeeded", "failed", "superseded", "deleted"] = "pending"
    attempts: int = 0
    next_attempt_at: datetime = Field(default_factory=utcnow)
    lease_owner: str | None = None
    claim_token: str | None = None
    lease_expires_at: datetime = Field(default_factory=utcnow)
    created_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    error_code: str | None = None
    completion_receipt: str | None = None

    class Settings:
        name = "base_fingerprint_tasks"
        indexes = FINGERPRINT_TASK_INDEXES
        use_state_management = True


class BaseExtractionRun(Document):
    id: str = Field(default_factory=lambda: str(uuid4()), alias="_id")
    requester_id: str
    deployment: str
    batch_size: int = Field(ge=1, le=32)
    requested_device: str
    actual_device: str
    pipeline_id: str = LEGACY_PIPELINE_ID
    cutoff: PydanticObjectId
    enrollment_cursor: PydanticObjectId | None = None
    enrollment_complete: bool = False
    active: bool = True
    status: str = "enrolling"
    created_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    notification_channel_id: str | None = None
    notification_message_id: str | None = None
    notification_error: str | None = None

    class Settings:
        name = "base_extraction_runs"
        indexes = EXTRACTION_RUN_INDEXES
        use_state_management = True
