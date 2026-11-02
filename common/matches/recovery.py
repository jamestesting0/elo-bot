from common.finals.types import is_final_match
from copy import deepcopy
import logging

import interactions as ipy
from pymongo.errors import PyMongoError

from common.constants import MATCH_CHANNEL
from common.matches.message_lock import match_message_lock
from common.matches.messages import _PUBLICATION_PLACEHOLDERS, mark_match_message_ended, match_message_embeds
from common.matches.publication_state import restore_match_publication
from common.results.validation import is_valid_match_result_entry
from database_models import Match


_MATCH_TITLES = frozenset((
    "**Match**", "**Match Started**", "**Match Scheduled**", "**Match Rescheduled**", "**Match Ended**",
))


def _is_match_message(message: ipy.Message, bot: ipy.Client) -> bool:
    return bool(
        message.author.id == bot.user.id and message.embeds
        and message.embeds[0].title in _MATCH_TITLES
        and (message.embeds[0].description in _PUBLICATION_PLACEHOLDERS
             or any(field.name in ("**Host Clan**", "**Players**") for field in message.embeds[0].fields))
    )


async def _repair_message(message: ipy.Message, *, bound: bool, ended: bool, active: bool) -> None:
    if not message.embeds:
        return
    pending = any(embed.description in _PUBLICATION_PLACEHOLDERS for embed in message.embeds)
    if message.embeds[0].title == "**Match has Ended**":
        if pending:
            await message.edit(embeds=match_message_embeds(message.embeds))
    elif ended or message.embeds[0].title == "**Match Ended**":
        await mark_match_message_ended(message)
    elif bound and active and message.embeds[0].title in (
            "**Match Started**", "**Match Scheduled**", "**Match Rescheduled**",
    ):
        if pending:
            await message.edit(embeds=match_message_embeds(message.embeds))
    elif pending or not bound or not active:
        if message.embeds[0].title == "**Match Aborted**":
            if message.components:
                await message.edit(components=[])
            return
        description = (
            "This message could not be verified against the current match. "
            "Ask a staff member to check the match status."
        )
        if (message.embeds[0].title == "**Match Update Incomplete**"
                and message.embeds[0].description == description and not pending and not message.components):
            return
        embeds = deepcopy(message.embeds)
        embeds[0].title = "**Match Update Incomplete**"
        embeds[0].description = description
        logging.warning(
            "Match message recovery could not restore message %s: bound=%s active=%s title=%r placeholder=%s",
            message.id, bound, active, message.embeds[0].title, pending,
        )
        await message.edit(embeds=match_message_embeds(embeds), components=[])


async def _attempt_repair_message(message: ipy.Message, *, bound: bool, ended: bool, active: bool,
                          match: Match | None = None,
                          player: int | None = None) -> None:
    try:
        publication = (match.public_message_publication if player is None else match.message_publications[player]) \
            if match is not None else None
        interrupted = bool(publication and publication.pending) or bool(message.embeds) and (
            message.embeds[0].title in ("**Match**", "**Match Update Incomplete**")
            or any(embed.description in _PUBLICATION_PLACEHOLDERS for embed in message.embeds)
        )
        if (bound and active and not ended and interrupted and match is not None
                and await restore_match_publication(message, match, player)):
            logging.info("Restored match publication for message %s, side %s", message.id, player)
            return
        await _repair_message(message, bound=bound, ended=ended, active=active)
    except (ipy.errors.HTTPException, PyMongoError):
        logging.exception("Unable to repair match message %s; retrying on the next sweep", message.id)


async def _repair_match(bot: ipy.Client, match: Match) -> None:
    completed = bool(match.result_operation and match.result_operation.completed) or (
        is_final_match(match) and bool(match.final_result_id)
    )
    side_ended = [
        completed or (len(side.result) == len(match.final_districts) and all(
            result is not None and is_valid_match_result_entry(result) for result in side.result
        )) for side in match.participants
    ]
    ended = len(side_ended) == 2 and all(side_ended)
    channel = await bot.fetch_channel(int(match.thread))
    if channel is None:
        return
    bound = {int(message_id): player for player, message_id in enumerate(match.old_msgs) if message_id}
    seen: set[int] = set()
    async for message in channel.history(limit=0):
        message_id = int(message.id)
        if message_id not in bound and not _is_match_message(message, bot):
            continue
        seen.add(message_id)
        player = bound.get(message_id)
        await _attempt_repair_message(
            message, bound=player is not None,
            ended=side_ended[player] if player is not None and match.is_war else ended,
            active=bool(match.times[player]) if player is not None else False,
            match=match, player=player,
        )
    for message_id, player in bound.items():
        if message_id in seen:
            continue
        try:
            message = await channel.fetch_message(message_id)
        except ipy.errors.NotFound:
            continue
        except ipy.errors.HTTPException:
            logging.exception("Unable to fetch match message %s; retrying on the next sweep", message_id)
            continue
        if message is not None:
            await _attempt_repair_message(
                message, bound=True, ended=side_ended[player] if match.is_war else ended,
                active=bool(match.times[player]),
                match=match, player=player,
            )
    if match.msg:
        public = await bot.fetch_channel(match.routing_channel_id if is_final_match(match)
                                        else MATCH_CHANNEL)
        if public is None:
            return
        try:
            message = await public.fetch_message(int(match.msg))
        except ipy.errors.NotFound:
            return
        if message is not None and (ended or match.public_message_publication or _is_match_message(message, bot)):
            await _attempt_repair_message(
                message, bound=True, ended=ended, active=any(match.times), match=match,
            )


async def repair_match_messages(bot: ipy.Client) -> None:
    """Repair interrupted publications without inferring a run's Started/Scheduled status."""
    async for snapshot in Match.find({"thread": {"$ne": None}}):
        try:
            async with match_message_lock(snapshot.thread):
                match = await Match.get(snapshot.id)
                if match is not None and match.thread == snapshot.thread:
                    await _repair_match(bot, match)
        except (ipy.errors.HTTPException, PyMongoError):
            logging.exception("Unable to repair messages for match %s; retrying on the next sweep", snapshot.id)
