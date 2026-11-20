import logging
from datetime import datetime, timedelta
from typing import Final
from weakref import WeakValueDictionary

import interactions as ipy
from aiohttp import ClientError
from anyio import Lock
from interactions.api.http.route import Route
from pymongo.errors import PyMongoError

from elo_bot.db.timestamp_models import TimestampPost


_WEBHOOK_NAME: Final = "Elo Timestamp"
_RETENTION: Final = timedelta(days=7)
_CHECK_CHANNEL: Final = "Check the channel before trying again. I couldn’t confirm the post."
_webhook_locks: WeakValueDictionary[int, Lock] = WeakValueDictionary()
logger = logging.getLogger(__name__)


class TimestampPublicationError(Exception):
    pass


async def _channel_webhook(bot: ipy.Client, channel: ipy.GuildText | ipy.GuildNews | ipy.GuildForum) -> ipy.Webhook:
    key = int(channel.id)
    lock = _webhook_locks.get(key)
    if lock is None:
        lock = _webhook_locks[key] = Lock()
    async with lock:
        for webhook in await channel.fetch_webhooks():
            if (webhook.name == _WEBHOOK_NAME and webhook.type == ipy.WebhookTypes.INCOMING and webhook.token
                    and (webhook.user_id == bot.user.id or str(webhook.application_id) == str(bot.app.id))):
                return webhook
        return await channel.create_webhook(_WEBHOOK_NAME)


async def _remove_unattributed_post(bot: ipy.Client, webhook: ipy.Webhook, message: ipy.Message,
                                    thread_id: ipy.Snowflake | None) -> None:
    if thread_id is None:
        await webhook.delete_message(message.id)
        return
    await bot.http.request(
        Route("DELETE", "/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}",
              webhook_id=webhook.id, webhook_token=webhook.token, message_id=message.id),
        params={"thread_id": thread_id},
    )


async def publish_timestamp(ctx: ipy.InteractionContext, candidate: datetime) -> None:
    bot = ctx.client
    thread_id = None
    try:
        channel = ctx.channel or await bot.fetch_channel(ctx.channel_id)
        if isinstance(channel, ipy.ThreadChannel):
            manage_thread = ipy.Permissions.MANAGE_THREADS | ipy.Permissions.ADMINISTRATOR
            if channel.locked and not (ctx.author_permissions & manage_thread and ctx.app_permissions & manage_thread):
                raise TimestampPublicationError("This thread is locked.")
            thread_id = channel.id
            channel = channel.parent_channel or await bot.fetch_channel(channel.parent_id)
        if not isinstance(channel, (ipy.GuildText, ipy.GuildNews, ipy.GuildForum)):
            raise TimestampPublicationError("Use `/timestamp` in a server channel that supports webhooks.")
        send_permission = ipy.Permissions.SEND_MESSAGES_IN_THREADS if thread_id is not None else ipy.Permissions.SEND_MESSAGES
        send_permissions = send_permission | ipy.Permissions.ADMINISTRATOR
        if not ctx.author_permissions & send_permissions:
            raise TimestampPublicationError("You don’t have permission to send messages here.")
        if not ctx.app_permissions & send_permissions:
            raise TimestampPublicationError("I don’t have permission to send messages here.")
        webhook = await _channel_webhook(bot, channel)
        author = ctx.author
        timestamp = ipy.Timestamp.fromdatetime(candidate).format(ipy.TimestampStyles.LongDateTime)
        message = await webhook.send(timestamp, username=author.display_name, avatar_url=author.display_avatar.url,
                                     allowed_mentions=ipy.AllowedMentions.none(), wait=True, thread=thread_id)
    except (ipy.errors.Forbidden, ipy.errors.NotFound) as error:
        raise TimestampPublicationError("I can’t post a timestamp here. Check my webhook permissions.") from error
    except (ipy.errors.HTTPException, ipy.errors.ForeignWebhookException, ClientError, TimeoutError) as error:
        logger.warning("Could not confirm timestamp webhook delivery (%s)", type(error).__name__)
        raise TimestampPublicationError(_CHECK_CHANNEL) from error
    if message is None:
        logger.warning("Discord did not return the posted timestamp message")
        raise TimestampPublicationError(_CHECK_CHANNEL)

    message_id = ipy.Snowflake(message.id)
    try:
        await TimestampPost(id=int(message_id), user_id=int(author.id), expires_at=message_id.created_at + _RETENTION).insert()
    except PyMongoError as error:
        logger.warning("Could not save timestamp attribution (%s)", type(error).__name__)
        try:
            await _remove_unattributed_post(bot, webhook, message, thread_id)
        except (ipy.errors.HTTPException, ClientError, TimeoutError) as remove_error:
            if not (isinstance(remove_error, ipy.errors.NotFound) and remove_error.code == 10008):
                logger.error("Could not confirm timestamp removal after attribution failed (%s)", type(remove_error).__name__)
                raise TimestampPublicationError(
                    "I couldn’t save who posted the timestamp. Check the channel before trying again."
                ) from None
        raise TimestampPublicationError("The timestamp was removed. I couldn’t save who posted it. Try again.") from None
