from typing import Annotated, Literal

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, ConfigDict, Field

from .auditlog_models import DiscordId
from elobot.features.bases.fingerprints.types import ImageAttachment


class SubmissionAcceptance(BaseModel):
    model_config = ConfigDict(frozen=True)
    phase: Literal["accepting", "accepted"] = "accepting"
    reviewer_id: DiscordId
    base_id: PydanticObjectId
    quality: str
    note: str
    builder_id: DiscordId | None
    accepted_builder_id: DiscordId | None
    expiration_date: float


class SubmissionRejection(BaseModel):
    model_config = ConfigDict(frozen=True)
    phase: Literal["rejected"] = "rejected"
    reviewer_id: DiscordId
    reason: str


type SubmissionResolution = Annotated[
    SubmissionAcceptance | SubmissionRejection, Field(discriminator="phase"),
]


class PendingBaseSubmission(Document):
    id: DiscordId = Field(alias="_id")
    link: str
    image_url: str
    image_attachment: ImageAttachment | None = None
    review_channel_id: DiscordId | None = None
    submitter_id: DiscordId
    submitter_display: str
    builder_id: DiscordId | None = None
    builder_display: str
    district: str
    proposed_quality: str
    reason: str | None = None
    resolution: SubmissionResolution | None = None

    async def claim_resolution(self, resolution: SubmissionResolution) -> bool:
        result = await self.get_pymongo_collection().update_one(
            {"_id": self.id, "resolution": None},
            {"$set": {"resolution": resolution.model_dump()}},
        )
        if result.matched_count == 0:
            return False
        self.resolution = resolution
        return True

    async def complete_acceptance(self, resolution: SubmissionAcceptance) -> bool:
        result = await self.get_pymongo_collection().update_one(
            {"_id": self.id, "resolution.phase": "accepting", "resolution.base_id": resolution.base_id},
            {"$set": {"resolution.phase": "accepted"}},
        )
        if result.matched_count == 0:
            return False
        self.resolution = resolution.model_copy(update={"phase": "accepted"})
        return True

    class Settings:
        name = "pending_base_submissions"
        use_state_management = True


__all__ = ["PendingBaseSubmission", "SubmissionAcceptance", "SubmissionRejection", "SubmissionResolution"]
