from unittest.mock import AsyncMock, MagicMock

from database_models.base_submission_models import SubmissionAcceptance, SubmissionResolution


def configure_pending_resolution(pending: MagicMock) -> None:
    pending.id = "9001"
    pending.resolution = None

    async def claim_resolution(resolution: SubmissionResolution) -> bool:
        if pending.resolution is not None:
            return False
        pending.resolution = resolution
        return True

    async def complete_acceptance(resolution: SubmissionAcceptance) -> bool:
        pending.resolution = resolution.model_copy(update={"phase": "accepted"})
        return True

    pending.claim_resolution = AsyncMock(side_effect=claim_resolution)
    pending.complete_acceptance = AsyncMock(side_effect=complete_acceptance)
