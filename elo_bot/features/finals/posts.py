import logging
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


@dataclass(frozen=True, slots=True)
class PostContent:
    embed: ipy.Embed
    components: list[ipy.Button | ipy.ActionRow]
    file: ipy.File | None = None


async def publish_post(channel: ipy.GuildText | ipy.ThreadChannel, reference: PostReference,
                       content: PostContent) -> ipy.Message:
    """Reuse a known post, recover a send by nonce, or replace an unlocatable post."""
    message = None
    if reference.message_id:
        try:
            message = await channel.fetch_message(int(reference.message_id))
        except ipy.errors.NotFound:
            message = None
    if message is None and reference.attempted:
        messages = await channel.fetch_messages(limit=100)
        message = next((item for item in messages if str(item.nonce) == reference.nonce
                        and item.author.id == reference.author_id), None)
    if message is None:
        return await channel.send(embed=content.embed, components=content.components, file=content.file,
                                  nonce=reference.nonce, enforce_nonce=True, allowed_mentions=ipy.AllowedMentions.none())
    await edit_channel_message(message, embed=content.embed, components=content.components,
                               attachments=[], file=content.file, allowed_mentions=ipy.AllowedMentions.none())
    return message


async def staff_notice(bot: ipy.Client, content: str) -> None:
    try:
        channel = await bot.fetch_channel(STAFF_CHANNEL)
        await channel.send(content, allowed_mentions=ipy.AllowedMentions.none())
    except ipy.errors.HTTPException:
        logging.exception("Could not post the Final staff notice: %s", content)
