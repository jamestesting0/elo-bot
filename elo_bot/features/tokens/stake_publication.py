import logging
import re
from hashlib import sha256

import interactions as ipy
from beanie import PydanticObjectId
from pymongo.errors import PyMongoError

from elo_bot.config.emojis import TICK_EMOJI, TOKEN_EMOJI
from elo_bot.core.finals import FinalError
from elo_bot.db import Match
from elo_bot.db.token_models import ParticipantStake
from elo_bot.discord.message_edits import edit_channel_message
from elo_bot.features.matches.message_lock import match_message_lock
from elo_bot.features.matches.thread_messages import find_match_thread_announcement
from elo_bot.features.tokens.stakes import reconcile_stake

STAKE_AMOUNT = r"[0-9,]+ <a?:token:\d+>"
STAKE_NOTE = re.compile(
    rf"^- <@!?(\d+)>(?: put {STAKE_AMOUNT} at stake| matches the stake with {STAKE_AMOUNT}"
    rf"| went all in \({STAKE_AMOUNT}\)|[’']s stake was reduced to {STAKE_AMOUNT})\.$"
)
STAKE_REPLY_PATTERN = re.compile(r"^stakereply\|(match|pass)\|[0-9a-f]{24}\|\d+$")


def stake_note_users(stake: ParticipantStake) -> tuple[str, ...]:
    if stake.state == "closed":
        return ()
    return (stake.author,) if stake.state == "offer" else (stake.author, *(user for user in stake.users if user != stake.author))


def stake_lines(stake: ParticipantStake) -> list[str]:
    lines = []
    for user in stake_note_users(stake):
        amount = f"{stake.committed_amount(user):,} {TOKEN_EMOJI}"
        if user in stake.all_in_users:
            lines.append(f"- <@{user}> went all in ({amount}).")
        elif user != stake.author:
            lines.append(f"- <@{user}> matches the stake with {amount}.")
        elif stake.committed_amount(user) < stake.amount:
            lines.append(f"- <@{user}>’s stake was reduced to {amount}.")
        else:
            lines.append(f"- <@{user}> put {amount} at stake.")
    return lines


async def _find_offer_message(channel: ipy.ThreadChannel, stake: ParticipantStake, bot_id: int) -> ipy.Message | None:
    if stake.offer_message_id is not None:
        try:
            return await channel.fetch_message(stake.offer_message_id)
        except ipy.errors.NotFound:
            return None
    if stake.offer_message_revision is not None:
        marker = f"stakereply|match|{stake.id}|{stake.offer_message_revision}"
        async for message in channel.history(limit=0):
            if message.author.id == bot_id and any(
                getattr(button, "custom_id", None) == marker
                for row in message.components or () for button in row.components
            ):
                return message
    return None


async def _publish_offer(channel: ipy.ThreadChannel, state: ParticipantStake, bot_id: int) -> bool:
    message = await _find_offer_message(channel, state, bot_id)
    stakes = ParticipantStake.get_pymongo_collection()
    if message is not None and state.state == "offer" and state.offer_message_revision == state.revision:
        if state.offer_message_id is not None:
            return True
    else:
        if message is not None:
            text = {"offer": "Stake offer updated.", "closed": "Stake offer closed."}.get(state.state, "Stake matched.")
            await edit_channel_message(message, content=text, components=[], allowed_mentions=ipy.AllowedMentions.none())
        if state.state != "offer":
            return True
        claimed = await stakes.update_one(
            {"_id": state.id, "revision": state.revision, "state": "offer"},
            {"$set": {"offer_message_id": None, "offer_message_revision": state.revision}},
        )
        if not claimed.matched_count:
            return False
        opponent = next(user for user in state.users if user != state.author)
        nonce = sha256(f"stake:{state.id}:{state.revision}".encode()).hexdigest()[:24]
        message = await channel.send(
            f"<@{opponent}>, <@{state.author}> put **{state.amount:,}** {TOKEN_EMOJI} at stake. "
            "Match the offer or play without a stake.",
            components=[
                ipy.Button(style=ipy.ButtonStyle.SUCCESS, label="Match Stake", emoji=TICK_EMOJI,
                           custom_id=f"stakereply|match|{state.id}|{state.revision}"),
                ipy.Button(style=ipy.ButtonStyle.SECONDARY, label="Pass",
                           custom_id=f"stakereply|pass|{state.id}|{state.revision}"),
            ],
            allowed_mentions=ipy.AllowedMentions(users=[int(opponent)]),
            nonce=nonce, enforce_nonce=True,
        )
    bound = await stakes.update_one({"_id": state.id, "offer_message_revision": state.revision},
                                    {"$set": {"offer_message_id": int(message.id)}})
    return bound.matched_count == 1


async def publish_stake(bot, match: Match) -> bool:
    if match.thread is None:
        return False
    async with match_message_lock(match.thread):
        state = await ParticipantStake.get(str(match.id))
        if state is None or not state.publication_pending:
            return True
        channel = await bot.fetch_channel(int(match.thread))
        if channel is None:
            return False
        if not await _publish_offer(channel, state, bot.user.id):
            return False
        message = await find_match_thread_announcement(channel, match, bot.user.id)
        if message is None:
            return False
        embed = message.embeds[0]
        notes = next((field for field in embed.fields if field.name == "**Notes**"), None)
        updates = dict(zip(stake_note_users(state), stake_lines(state), strict=True))
        lines = []
        for line in notes.value.splitlines() if notes else ():
            if existing := STAKE_NOTE.fullmatch(line):
                if updated := updates.pop(existing[1], None):
                    lines.append(updated)
            else:
                lines.append(line)
        lines.extend(updates.values())
        if notes and lines:
            notes.value = "\n".join(lines)
        elif notes:
            embed.fields.remove(notes)
        elif lines:
            embed.add_field(name="**Notes**", value="\n".join(lines))
        await edit_channel_message(message, embeds=[embed, *message.embeds[1:]])
        result = await ParticipantStake.get_pymongo_collection().update_one(
            {"_id": state.id, "revision": state.revision, "settlement_key": state.settlement_key},
            {"$set": {"publication_pending": False}},
        )
        return result.matched_count == 1


async def sync_stake_notes(bot, match: Match) -> None:
    if match.match_stake_enabled is not True:
        return
    try:
        await reconcile_stake(str(match.id))
        await publish_stake(bot, match)
    except (FinalError, PyMongoError, ipy.errors.HTTPException):
        logging.exception("Stake Notes for match %s are pending", match.id)


async def recover_stakes(bot) -> None:
    async for stake in ParticipantStake.find({"$or": [
        {"state": {"$in": ["offer", "locked"]}}, {"publication_pending": True},
    ]}):
        try:
            await reconcile_stake(stake.id)
            match = await Match.get(PydanticObjectId(stake.id))
            if match and match.thread:
                await publish_stake(bot, match)
            elif stake.publication_pending:
                await ParticipantStake.get_pymongo_collection().update_one(
                    {"_id": stake.id, "revision": stake.revision}, {"$set": {"publication_pending": False}},
                )
        except (FinalError, PyMongoError, ipy.errors.HTTPException):
            logging.exception("Stake %s recovery is pending", stake.id)
