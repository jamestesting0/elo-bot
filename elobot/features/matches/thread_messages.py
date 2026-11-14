import interactions as ipy

from elobot.db import Match


class ThreadMessageBindingError(RuntimeError):
    def __init__(self, token: str) -> None:
        self.token = token
        super().__init__(f"Match {token} changed before its thread announcement could be recorded")


async def attach_thread_message(match: Match, message_id: int) -> bool:
    result = await match.get_pymongo_collection().update_one(
        {"_id": match.id, "thread": match.thread, "thread_message_id": match.thread_message_id},
        {"$set": {"thread_message_id": message_id}},
    )
    if result.matched_count == 0:
        return False
    match.thread_message_id = message_id
    saved_state = match.get_saved_state()
    if saved_state is not None:
        saved_state["thread_message_id"] = message_id
    return True


async def find_match_thread_announcement(
        channel: ipy.MessageableMixin, match: Match, author_id: int,
) -> ipy.Message | None:
    def matches(message: ipy.Message) -> bool:
        return message.author.id == author_id and bool(message.embeds) and any(
            getattr(component, "custom_id", None) == f"submit_button|{match.token}"
            for row in message.components or () for component in row.components
        )

    if match.thread_message_id is not None:
        try:
            message = await channel.fetch_message(match.thread_message_id)
        except ipy.errors.NotFound:
            message = None
        if message is not None and matches(message):
            return message
    for message in await channel.fetch_pinned_messages():
        if matches(message):
            return message
    async for message in channel.history(limit=0):
        if matches(message):
            return message
    return None
