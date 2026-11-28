import logging
from collections.abc import Callable
from dataclasses import dataclass

import anyio
import interactions as ipy

from elo_bot.config.ids import STAFF_CHANNEL
from elo_bot.discord.message_edits import edit_channel_message

PUBLICATION_LOCKS: dict[str, anyio.Lock] = {}


@dataclass(frozen=True, slots=True)
class PostReference:
    nonce: str
    author_id: int
    message_id: str | None = None
    attempted: bool = False
    match_history: Callable[[ipy.Message], bool] | None = None


@dataclass(frozen=True, slots=True)
class PostContent:
    embed: ipy.Embed
    components: list[ipy.Button | ipy.ActionRow]
    file: ipy.File | None = None


async def locate_post(channel: ipy.GuildText | ipy.GuildNews | ipy.ThreadChannel | ipy.DM,
                      reference: PostReference) -> ipy.Message | None:
    message = None
    if reference.message_id:
        try:
            message = await channel.fetch_message(int(reference.message_id))
        except ipy.errors.NotFound:
            message = None
    if message is None and reference.attempted:
        messages = await channel.fetch_messages(limit=100)
        message = next((item for item in messages if item.author.id == reference.author_id
                        and (str(item.nonce) == reference.nonce
                             or (reference.match_history and reference.match_history(item)))), None)
    return message


async def publish_post(channel: ipy.GuildText | ipy.GuildNews | ipy.ThreadChannel | ipy.DM, reference: PostReference,
                       content: PostContent, *, allow_create: bool = True) -> ipy.Message | None:
    """Reuse a known post, recover a send by nonce, or replace an unlocatable post."""
    message = await locate_post(channel, reference)
    if message is None:
        if not allow_create:
            return None
        return await channel.send(embed=content.embed, components=content.components, file=content.file,
                                  nonce=reference.nonce, enforce_nonce=True, allowed_mentions=ipy.AllowedMentions.none())
    await edit_channel_message(message, embed=content.embed, components=content.components,
                               attachments=[], file=content.file, allowed_mentions=ipy.AllowedMentions.none(),
                               require_confirmation=True)
    return message


async def staff_notice(bot: ipy.Client, content: str) -> None:
    try:
        channel = await bot.fetch_channel(STAFF_CHANNEL)
        await channel.send(content, allowed_mentions=ipy.AllowedMentions.none())
    except ipy.errors.HTTPException:
        logging.exception("Could not post the Final staff notice: %s", content)
