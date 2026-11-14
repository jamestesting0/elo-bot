import interactions as ipy
from beanie import PydanticObjectId
from pydantic import JsonValue

from elobot.config.ids import MATCH_CHANNEL
from elobot.features.finals.types import is_final_match
from elobot.db import Match


async def restore_match_publication(message: ipy.Message, match: Match, player: int | None) -> bool:
    """Replay a bound final payload; caller holds the match message lock."""
    public = player is None
    publication = match.public_message_publication if public else match.message_publications[player]
    message_id = match.msg if public else match.old_msgs[player]
    channel_id = (match.routing_channel_id if is_final_match(match) else MATCH_CHANNEL) if public else match.thread
    if (publication is None or publication.message_id != int(message.id)
            or str(message_id) != str(publication.message_id) or match.is_war != publication.is_war
            or str(message._channel_id) != str(channel_id)):
        return False
    field = "public_message_publication" if public else f"message_publications.{player}"
    query: dict[str, JsonValue | PydanticObjectId] = {
        "_id": match.id,
        "is_war": publication.is_war,
        "msg" if public else f"old_msgs.{player}": message_id,
        field: publication.model_dump(mode="json", exclude_defaults=True),
    }
    for side in (player,) if publication.is_war and not public else (0, 1):
        if (match.participants[side].user_id != publication.participants[side]
                or match.times[side] != publication.times[side]
                or match.hosts[side] != publication.hosts[side]
                or match.host_clans[side] != publication.host_clans[side]):
            return False
        query.update({
            f"participants.{side}.user_id": publication.participants[side],
            f"times.{side}": publication.times[side],
            f"hosts.{side}": publication.hosts[side],
            f"host_clans.{side}": publication.host_clans[side],
        })
    collection = match.get_pymongo_collection()
    if await collection.find_one(query, {"_id": 1}) is None:
        return False
    await message.edit(embeds=publication.embeds, components=publication.components)
    if publication.pending:
        published = publication.model_copy(update={"pending": False})
        payload = published.model_dump(mode="json", exclude_defaults=True)
        result = await collection.update_one(query, {"$set": {field: payload}})
        if result.matched_count != 1:
            return False
        if public:
            match.public_message_publication = published
        else:
            match.message_publications[player] = published
        saved_state = match.get_saved_state()
        if saved_state is not None:
            if public:
                saved_state[field] = published.model_dump(mode="json")
            else:
                saved_state["message_publications"][player] = published.model_dump(mode="json")
    return True
