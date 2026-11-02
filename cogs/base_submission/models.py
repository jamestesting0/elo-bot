from dataclasses import dataclass
from enum import StrEnum


class ReviewStatus(StrEnum):
    PENDING = "Pending"
    ACCEPTED = "Accepted"
    REJECTED = "Rejected"


@dataclass(frozen=True, slots=True)
class SubmissionDraft:
    link: str
    image_url: str
    submitter_id: str
    submitter_display: str
    builder_id: str | None
    builder_display: str
    district: str
    proposed_quality: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class AcceptDecision:
    quality: str
    note: str
    builder_id: str | None = None


@dataclass(frozen=True, slots=True)
class RejectDecision:
    reason: str


type ResolutionDecision = AcceptDecision | RejectDecision
