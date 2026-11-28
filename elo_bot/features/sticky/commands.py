import asyncio
import logging

import interactions as ipy
from pymongo.errors import WriteError

from elo_bot.discord import runtime
from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elo_bot.config.ids import GUILD_IDS, LVL1_ROLES
from elo_bot.db import StickyMessage
from elo_bot.features.embeds.static import static_embed

STICKY_WEBHOOK_NAME = "Elo Sticky"


_sticky_locks: dict[str, asyncio.Lock] = {}


# Messages this process posted as stickies. A burst of messages can replace a sticky again before the gateway
# reports the previous repost, which would otherwise look like a new message and repost forever.
_bot_sticky_posts: set[tuple[str, str]] = set()


def sticky_channel_lock(channel_id: str) -> asyncio.Lock:
    return _sticky_locks.setdefault(channel_id, asyncio.Lock())


async def delete_retired_post(channel, message_id: str) -> bool:
    try:
        await channel.delete_message(message_id)
    except ipy.errors.NotFound:
        pass
    except ipy.errors.HTTPException:
        logging.getLogger(__name__).warning("Could not remove old sticky %s in channel %s", message_id, channel.id,
                                            exc_info=True)
        return False
    return True


async def finish_retirement(channel, sticky: StickyMessage) -> bool:
    if sticky.retired_message_id is None:
        return True
    return await delete_retired_post(channel, sticky.retired_message_id) and await sticky.clear_retired()


async def sticky_webhook(channel, webhook_id: str | None):
    thread_id = None
    parent = channel
    if isinstance(channel, ipy.ThreadChannel):
        thread_id = channel.id
        parent = channel.parent_channel or await runtime.bot.fetch_channel(channel.parent_id)

    match = None
    named = None
    for hook in await parent.fetch_webhooks():
        if not isinstance(getattr(hook, "token", None), str) or not hook.token:
            continue
        if webhook_id is not None and str(hook.id) == webhook_id:
            match = hook
            break
        if hook.name == STICKY_WEBHOOK_NAME and named is None:
            named = hook
    if match is None:
        match = named or await parent.create_webhook(STICKY_WEBHOOK_NAME)
    return match, thread_id


async def send_sticky(webhook, content: str, author_name: str, avatar_url: str | None, thread_id):
    kwargs = {"username": author_name, "avatar_url": avatar_url, "wait": True}
    if thread_id is not None:
        kwargs["thread"] = thread_id
    return await webhook.send(content, **kwargs)


@ipy.slash_command(
    name="sticky",
    description="Add a sticky message at the bottom of this channel.",
    scopes=GUILD_IDS,
    dm_permission=False,
)
@ipy.check(ipy.has_any_role(*LVL1_ROLES))
@ipy.slash_option(
    name="message",
    description="The message to keep at the bottom of this channel",
    opt_type=ipy.OptionType.STRING,
    required=True,
    min_length=1,
    max_length=2000,
)
async def sticky(ctx: ipy.SlashContext, message: str) -> None:
    content = message.strip()
    if not content:
        await ctx.send(f"{ERROR_EMOJI} The sticky message cannot be empty.", ephemeral=True)
        return

    await ctx.defer(ephemeral=True)
    author_name = ctx.author.display_name[:80]
    avatar = ctx.author.display_avatar
    avatar_url = None if avatar is None else avatar.url
    channel_id = str(ctx.channel.id)

    async with sticky_channel_lock(channel_id):
        existing = await StickyMessage.for_channel(channel_id)
        webhook, thread_id = await sticky_webhook(
            ctx.channel, existing[0].webhook_id if existing else None,
        )
        sent = await send_sticky(webhook, content, author_name, avatar_url, thread_id)
        try:
            document = StickyMessage(
                id=str(sent.id),
                channel_id=channel_id,
                message_id=str(sent.id),
                webhook_id=str(webhook.id),
                content=content,
                author_name=author_name,
                avatar_url=avatar_url,
            )
            await StickyMessage.get_pymongo_collection().insert_one(document.model_dump(by_alias=True))
        except Exception:
            await sent.delete()
            raise

    await ctx.send(f"{SUCCESS_EMOJI} Sticky message set.", ephemeral=True)


async def post_embed_sticky(channel, embed_type: str, bot: ipy.Client) -> None:
    channel_id = str(channel.id)
    async with sticky_channel_lock(channel_id):
        embed, components, file = await static_embed(embed_type, bot)
        sent = await channel.send(embed=embed, components=components, file=file)
        _bot_sticky_posts.add((channel_id, str(sent.id)))
        try:
            document = StickyMessage(id=str(sent.id), channel_id=channel_id, message_id=str(sent.id),
                                     embed_type=embed_type)
            await StickyMessage.get_pymongo_collection().insert_one(document.model_dump(by_alias=True))
        except Exception:
            await sent.delete()
            raise


@ipy.listen(ipy.events.MessageCreate)
async def on_sticky_message(event: ipy.events.MessageCreate) -> None:
    channel = event.message.channel
    if not isinstance(channel, (ipy.GuildText, ipy.GuildNews, ipy.ThreadChannel)):
        return
    # Ephemeral replies are visible to one user only, so they never push a sticky up.
    if ipy.MessageFlags.EPHEMERAL in ipy.MessageFlags(event.message.flags or 0):
        return

    channel_id = str(channel.id)
    post_key = (channel_id, str(event.message.id))
    if post_key not in _bot_sticky_posts and not await StickyMessage.for_channel(channel_id):
        return

    async with sticky_channel_lock(channel_id):
        if post_key in _bot_sticky_posts:
            _bot_sticky_posts.discard(post_key)
            return
        stickies = await StickyMessage.for_channel(channel_id)
        if not stickies or any(
            sticky.message_id == str(event.message.id)
            or sticky.webhook_id == str(event.message.webhook_id)
            for sticky in stickies
        ):
            return

        webhook = thread_id = None
        for sticky in stickies:
            if not await finish_retirement(channel, sticky):
                continue
            if await channel.fetch_message(sticky.message_id, force=True) is None:
                await sticky.release()
                continue

            if sticky.embed_type:
                embed, components, file = await static_embed(sticky.embed_type, event.bot)
                sent = await channel.send(embed=embed, components=components, file=file)
                webhook_id = None
            else:
                if webhook is None:
                    known = next((item.webhook_id for item in stickies if item.webhook_id), None)
                    webhook, thread_id = await sticky_webhook(channel, known)
                sent = await send_sticky(webhook, sticky.content, sticky.author_name, sticky.avatar_url, thread_id)
                webhook_id = str(webhook.id)
            _bot_sticky_posts.add((channel_id, str(sent.id)))
            try:
                moved = await sticky.move(str(sent.id), webhook_id)
            except WriteError:
                await sent.delete()
                raise
            if moved:
                await finish_retirement(channel, sticky)
            else:
                await sent.delete()


async def release_deleted_sticky(channel_id: str, message_id: str, bot: ipy.Client) -> None:
    for sticky in await StickyMessage.for_channel(channel_id):
        if sticky.message_id == message_id:
            if await sticky.release() and sticky.retired_message_id is not None:
                channel = await bot.fetch_channel(channel_id)
                if channel is not None:
                    await delete_retired_post(channel, sticky.retired_message_id)
            return


@ipy.listen(ipy.events.MessageDelete)
async def on_sticky_deleted(event: ipy.events.MessageDelete) -> None:
    channel_id = getattr(event.message, "_channel_id", None)
    message_id = getattr(event.message, "id", None)
    if channel_id in (None, ipy.MISSING) or message_id in (None, ipy.MISSING):
        return
    await release_deleted_sticky(str(channel_id), str(message_id), event.bot)


@ipy.listen(ipy.events.MessageDeleteBulk)
async def on_sticky_bulk_deleted(event: ipy.events.MessageDeleteBulk) -> None:
    channel_id = str(event.channel_id)
    for message_id in event.ids:
        await release_deleted_sticky(channel_id, str(message_id), event.bot)
