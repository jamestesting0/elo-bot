from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import logging
from typing import Final

import anyio
import interactions as ipy
from beanie import PydanticObjectId
from pydantic import JsonValue
from pymongo.errors import PyMongoError

from elobot.discord.embeds import embed_edit_payload
from elobot.discord.message_edits import edit_channel_message
from elobot.features.matches.message_lock import match_message_lock
from elobot.features.finals.types import is_final_match
from elobot.db import Match
from elobot.db.publication_models import MatchMessagePublication


_PUBLICATION_PLACEHOLDERS: Final = frozenset(("Updating…", "Updating...", "Loading…", "Loading..."))


def match_message_embeds(embeds: Sequence[ipy.Embed]) -> list[ipy.Embed | dict[str, JsonValue]]:
    """Clear legacy publication placeholders when editing committed match messages."""
    result: list[ipy.Embed | dict[str, JsonValue]] = []
    for embed in embeds:
        if embed.description in _PUBLICATION_PLACEHOLDERS:
            payload = embed_edit_payload(embed)
            payloads = payload if isinstance(payload, list) else [payload]
            for item in payloads:
                item["description"] = ""
            result.extend(payloads)
        else:
            result.append(embed)
    return result


async def mark_match_message_ended(message: ipy.Message) -> None:
    if not message.embeds:
        return
    if (message.embeds[0].title != "**Match Ended**"
            or any(embed.description in _PUBLICATION_PLACEHOLDERS for embed in message.embeds)):
        embeds = deepcopy(message.embeds)
        embeds[0].title = "**Match Ended**"
        await edit_channel_message(message, embeds=match_message_embeds(embeds))


@dataclass(frozen=True, slots=True)
class MatchMessageState:
    match_id: PydanticObjectId
    player: int
    is_war: bool
    participants: tuple[str, ...]
    times: tuple[int | None, ...]
    hosts: tuple[str | None, ...]
    host_clans: tuple[str | None, ...]
    old_messages: tuple[int | None, ...]
    final_identity: tuple[str, str, int, int, bool, str | None] | None = None

    @classmethod
    def capture(cls, match: Match, player: int) -> "MatchMessageState":
        """Capture the committed run before any message preparation awaits."""
        assert match.id is not None
        return cls(
            match.id, player, match.is_war,
            tuple(participant.user_id for participant in match.participants),
            tuple(match.times), tuple(match.hosts), tuple(match.host_clans), tuple(match.old_msgs),
            (match.final_id, match.final_node_id, match.final_contestant_revision, match.final_attempt,
             match.final_superseded, match.final_result_id) if is_final_match(match) else None,
        )

    def binding_filter(self) -> Mapping[str, JsonValue | PydanticObjectId]:
        query: dict[str, JsonValue | PydanticObjectId] = {"_id": self.match_id, "is_war": self.is_war}
        if self.final_identity:
            query.update(dict(zip(("final_id", "final_node_id", "final_contestant_revision", "final_attempt",
                                   "final_superseded", "final_result_id"), self.final_identity)))
        for player in (self.player,) if self.is_war else (0, 1):
            query.update({
                f"participants.{player}.user_id": self.participants[player],
                f"times.{player}": self.times[player],
                f"hosts.{player}": self.hosts[player],
                f"host_clans.{player}": self.host_clans[player],
                f"old_msgs.{player}": self.old_messages[player],
            })
        return query


class MatchMessagePublicationError(RuntimeError):
    def __init__(self, match_id: PydanticObjectId, player: int) -> None:
        self.match_id = match_id
        self.player = player
        super().__init__(f"Match {match_id} changed before side {player}'s message finished publishing")


async def _mark_incomplete(message: ipy.Message, embed: ipy.Embed, *, preserve_controls: bool = False) -> None:
    incomplete = deepcopy(embed)
    incomplete.title = "**Match Update Incomplete**"
    incomplete.description = "This match message could not be finalized. Ask a staff member to check the match status."
    try:
        if preserve_controls:
            await edit_channel_message(message, embed=incomplete)
        else:
            await edit_channel_message(message, embed=incomplete, components=[])
    except ipy.errors.HTTPException:
        logging.exception("Unable to mark match message %s incomplete", message.id)


async def _publish_match_message(
        ctx: ipy.SlashContext | ipy.ComponentContext,
        match: Match,
        expected: MatchMessageState,
        *,
        embed: ipy.Embed,
        content: str | None = None,
        components: Sequence[ipy.ActionRow] | None = None,
        existing_message: ipy.Message | None = None,
        public_channel: ipy.GuildText | None = None,
) -> ipy.Message:
    """Persist the exact final payload before publishing a new or existing match message."""
    embed = deepcopy(embed)
    if embed.description in _PUBLICATION_PLACEHOLDERS:
        embed.description = ""
    public = public_channel is not None
    replace_controls = components is not None
    channel = public_channel if public else ctx.channel
    old_message_id = match.msg if public else expected.old_messages[expected.player]
    field = "msg" if public else f"old_msgs.{expected.player}"
    publication_field = "public_message_publication" if public else f"message_publications.{expected.player}"
    query = dict(expected.binding_filter())
    if public:
        query[field] = old_message_id
    pending = deepcopy(embed)
    pending.title = "**Match**"
    pending.description = "Updating…"
    message = existing_message
    if message is None:
        with anyio.CancelScope(shield=True):
            message = await channel.send(content, embed=pending, components=[])
    else:
        if (str(message.id) != str(old_message_id)
                or int(message._channel_id) != int(public_channel.id if public else ctx.channel_id)):
            raise MatchMessagePublicationError(expected.match_id, expected.player)
        if not public or components is None:
            components = message.components
    components = components or ()
    final_embeds = embed_edit_payload(embed)
    publication = MatchMessagePublication(
        pending=True, message_id=int(message.id), is_war=expected.is_war, participants=expected.participants,
        times=expected.times, hosts=expected.hosts, host_clans=expected.host_clans,
        embeds=final_embeds if isinstance(final_embeds, list) else [final_embeds],
        components=[row.to_dict() for row in components],
    )
    publication_payload = publication.model_dump(mode="json", exclude_defaults=True)
    collection = match.get_pymongo_collection()
    message_id = str(message.id) if public else int(message.id)

    def remember_publication(value: MatchMessagePublication) -> None:
        if public:
            match.msg = str(message.id)
            match.public_message_publication = value
        else:
            match.old_msgs[expected.player] = int(message.id)
            match.message_publications[expected.player] = value
        saved_state = match.get_saved_state()
        if saved_state is not None:
            if public:
                saved_state["msg"] = str(message.id)
                saved_state["public_message_publication"] = value.model_dump(mode="json")
            else:
                saved_state["old_msgs"][expected.player] = int(message.id)
                saved_state.setdefault("message_publications", [None, None])[expected.player] = value.model_dump(mode="json")

    try:
        if not public:
            await collection.update_one(
                {"_id": expected.match_id, "message_publications": {"$exists": False}},
                {"$set": {"message_publications": [None, None]}},
            )
            match.model_fields_set.add("message_publications")
        result = await collection.update_one(query, {"$set": {
            field: message_id, publication_field: publication_payload,
        }})
        if result.matched_count != 1:
            raise MatchMessagePublicationError(expected.match_id, expected.player)
        remember_publication(publication)
        if existing_message is None or (public and replace_controls):
            await message.edit(embed=final_embeds, components=list(components))
            if not public and existing_message is None:
                await message.pin()
        else:
            await message.edit(embed=final_embeds)
        published = publication.model_copy(update={"pending": False})
        result = await collection.update_one(
            {**query, field: message_id, publication_field: publication_payload},
            {"$set": {publication_field: published.model_dump(mode="json", exclude_defaults=True)}},
        )
        if result.matched_count != 1:
            raise MatchMessagePublicationError(expected.match_id, expected.player)
        remember_publication(published)
    except (PyMongoError, ipy.errors.HTTPException, MatchMessagePublicationError):
        await _mark_incomplete(message, embed, preserve_controls=existing_message is not None)
        raise
    except anyio.get_cancelled_exc_class():
        with anyio.move_on_after(10, shield=True) as cleanup:
            await _mark_incomplete(message, embed, preserve_controls=existing_message is not None)
        if cleanup.cancel_called:
            logging.error("Timed out marking cancelled match message %s incomplete", message.id)
        raise

    if not public and old_message_id and int(old_message_id) != int(message.id):
        try:
            old_message = await ctx.channel.fetch_message(old_message_id)
            if old_message:
                await old_message.delete()
        except ipy.errors.HTTPException:
            logging.exception("Unable to remove previous match message %s", old_message_id)
    return message


async def publish_match_message(
        ctx: ipy.SlashContext | ipy.ComponentContext,
        match: Match,
        expected: MatchMessageState,
        *,
        embed: ipy.Embed,
        content: str | None = None,
        components: Sequence[ipy.ActionRow] | None = None,
        existing_message: ipy.Message | None = None,
        public_channel: ipy.GuildText | None = None,
) -> ipy.Message:
    async with match_message_lock(match.thread or ctx.channel_id):
        return await _publish_match_message(
            ctx, match, expected, embed=embed, content=content, components=components,
            existing_message=existing_message, public_channel=public_channel,
        )
