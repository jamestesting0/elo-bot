import asyncio

import interactions as ipy

from elobot.app import runtime
from elobot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elobot.config.ids import GUILD_IDS, LVL1_ROLES
from elobot.db import StickyMessage

STICKY_WEBHOOK_NAME = "Elo Sticky"


_sticky_locks: dict[str, asyncio.Lock] = {}


# Message ids this process is deleting while it resticks. The gateway reports those
# deletes too, and they must not clear the stored sticky.
_bot_sticky_deletes: set[tuple[str, str]] = set()


def sticky_channel_lock(channel_id: str) -> asyncio.Lock:
    return _sticky_locks.setdefault(channel_id, asyncio.Lock())


def note_bot_sticky_delete(channel_id: str, message_id: str) -> None:
    _bot_sticky_deletes.add((channel_id, message_id))


def take_bot_sticky_delete(channel_id: str, message_id: str) -> bool:
    key = (channel_id, message_id)
    if key not in _bot_sticky_deletes:
        return False
    _bot_sticky_deletes.remove(key)
    return True


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


@ipy.listen(ipy.events.MessageCreate)
async def on_sticky_message(event: ipy.events.MessageCreate) -> None:
    channel = event.message.channel
    if not isinstance(channel, (ipy.GuildText, ipy.GuildNews, ipy.ThreadChannel)):
        return

    channel_id = str(channel.id)
    if not await StickyMessage.for_channel(channel_id):
        return

    async with sticky_channel_lock(channel_id):
        stickies = await StickyMessage.for_channel(channel_id)
        if not stickies or any(
            sticky.message_id == str(event.message.id)
            or sticky.webhook_id == str(event.message.webhook_id)
            for sticky in stickies
        ):
            return

        webhook, thread_id = await sticky_webhook(channel, stickies[0].webhook_id)
        for sticky in stickies:
            old_message_id = sticky.message_id
            note_bot_sticky_delete(channel_id, old_message_id)
            try:
                await channel.delete_message(old_message_id)
            except ipy.errors.NotFound:
                take_bot_sticky_delete(channel_id, old_message_id)
                await sticky.release()
                continue
            except ipy.errors.HTTPException:
                take_bot_sticky_delete(channel_id, old_message_id)
                raise

            try:
                sent = await send_sticky(webhook, sticky.content, sticky.author_name, sticky.avatar_url, thread_id)
                try:
                    moved = await sticky.move(str(sent.id), str(webhook.id))
                except Exception:
                    await sent.delete()
                    raise
                if not moved:
                    await sent.delete()
            finally:
                take_bot_sticky_delete(channel_id, old_message_id)


async def release_deleted_sticky(channel_id: str, message_id: str) -> None:
    if take_bot_sticky_delete(channel_id, message_id):
        return
    stickies = await StickyMessage.for_channel(channel_id)
    if not any(sticky.message_id == message_id for sticky in stickies):
        return

    async with sticky_channel_lock(channel_id):
        if take_bot_sticky_delete(channel_id, message_id):
            return
        for sticky in await StickyMessage.for_channel(channel_id):
            if sticky.message_id == message_id:
                await sticky.release()
                return


@ipy.listen(ipy.events.MessageDelete)
async def on_sticky_deleted(event: ipy.events.MessageDelete) -> None:
    channel_id = getattr(event.message, "_channel_id", None)
    message_id = getattr(event.message, "id", None)
    if channel_id in (None, ipy.MISSING) or message_id in (None, ipy.MISSING):
        return
    await release_deleted_sticky(str(channel_id), str(message_id))


@ipy.listen(ipy.events.MessageDeleteBulk)
async def on_sticky_bulk_deleted(event: ipy.events.MessageDeleteBulk) -> None:
    channel_id = str(event.channel_id)
    for message_id in event.ids:
        await release_deleted_sticky(channel_id, str(message_id))
