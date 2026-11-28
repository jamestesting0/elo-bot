from typing import Annotated

from beanie import Document
from pydantic import Field, StringConstraints


DiscordId = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9]+$")]


class StickyMessage(Document):
    id: DiscordId = Field(alias="_id")
    # Older records use the channel ID as _id and have no channel_id field.
    channel_id: DiscordId | None = None
    message_id: DiscordId
    retired_message_id: DiscordId | None = None
    # Text stickies are reposted through a webhook; embed stickies are reposted by the bot itself.
    webhook_id: DiscordId | None = None
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    author_name: str | None = Field(default=None, min_length=1, max_length=80)
    avatar_url: str | None = None
    embed_type: str | None = None

    @classmethod
    async def for_channel(cls, channel_id: str) -> list["StickyMessage"]:
        messages = await cls.find({"$or": [
            {"channel_id": channel_id},
            {"_id": channel_id, "channel_id": None},
        ]}).to_list()
        return sorted(messages, key=lambda message: int(message.id))

    async def move(self, message_id: str, webhook_id: str | None) -> bool:
        changes = {"message_id": message_id, "webhook_id": webhook_id, "retired_message_id": self.message_id}
        result = await self.get_pymongo_collection().update_one(
            {"_id": self.id, "message_id": self.message_id},
            {"$set": changes},
        )
        if result.matched_count == 0:
            return False
        self.retired_message_id = self.message_id
        self.message_id = message_id
        self.webhook_id = webhook_id
        saved_state = self.get_saved_state()
        if saved_state is not None:
            saved_state.update(changes)
        return True

    async def clear_retired(self) -> bool:
        result = await self.get_pymongo_collection().update_one(
            {"_id": self.id, "message_id": self.message_id, "retired_message_id": self.retired_message_id},
            {"$set": {"retired_message_id": None}},
        )
        if result.matched_count == 0:
            return False
        self.retired_message_id = None
        saved_state = self.get_saved_state()
        if saved_state is not None:
            saved_state["retired_message_id"] = None
        return True

    async def release(self) -> bool:
        result = await self.get_pymongo_collection().delete_one(
            {"_id": self.id, "message_id": self.message_id},
        )
        return result.deleted_count == 1

    class Settings:
        name = "sticky_messages"
        use_state_management = True
        indexes = ["channel_id"]


__all__ = ["DiscordId", "StickyMessage"]
