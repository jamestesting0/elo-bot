from typing import Literal

from beanie import Document, PydanticObjectId
from pymongo import IndexModel

type InvitationState = Literal["pending", "accepted", "declined", "expired", "withdrawn", "full"]


class FinalInvitation(Document):
    final_id: PydanticObjectId
    season_number: int
    user_id: str
    display_name: str
    invited_by: str
    created_at: int
    expires_at: int
    state: InvitationState = "pending"
    dm_channel_id: str | None = None
    dm_message_id: str | None = None
    # The DM still shows a pending invitation that the bot closed (expired or withdrawn) without a click.
    dm_outdated: bool = False

    class Settings:
        name = "final_invitations"
        use_state_management = True
        indexes = [
            IndexModel([("final_id", 1), ("user_id", 1)], unique=True,
                       partialFilterExpression={"state": "pending"}, name="final_invitation_pending"),
            IndexModel([("state", 1), ("expires_at", 1)], name="final_invitation_expiry"),
            IndexModel([("dm_outdated", 1)], partialFilterExpression={"dm_outdated": True},
                       name="final_invitation_dm_outdated"),
        ]
