import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
import logging

import aiohttp
import discord

from elo_bot.config.ids import EXP_GRANT_CHANNEL, EXP_GRANT_COMMAND, MAIN_GUILD
from elo_bot.config.settings import BOT_STAFF_TOKEN


class ExpGrantError(RuntimeError):
    def __init__(self, *, uncertain: bool) -> None:
        self.uncertain = uncertain
        super().__init__("The EXP grant could not be confirmed." if uncertain
                         else "The EXP grant could not be submitted.")


async def grant_exp(user_id: str, before_submit: Callable[[], Awaitable[None]]) -> str:
    if not BOT_STAFF_TOKEN or not BOT_STAFF_TOKEN.strip():
        raise ExpGrantError(uncertain=False)
    submitted = False
    client = discord.Client(chunk_guilds_at_startup=False, guild_subscriptions=False)
    messages: asyncio.Queue[discord.Message] = asyncio.Queue()

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.channel.id == EXP_GRANT_CHANNEL:
            messages.put_nowait(message)

    @client.event
    async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent) -> None:
        await on_message(payload.message)

    gateway = None
    try:
        async with asyncio.timeout(60):
            await client.login(BOT_STAFF_TOKEN)
            gateway = asyncio.create_task(client.connect(reconnect=False))
            await client.wait_until_ready()
            channel = await client.fetch_channel(EXP_GRANT_CHANNEL)
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != MAIN_GUILD:
                raise ExpGrantError(uncertain=False)
            command = next((command for command in await channel.application_commands()
                            if command.id == EXP_GRANT_COMMAND and isinstance(command, discord.SlashCommand)), None)
            add = next((child for child in command.children if child.name == "add"), None) if command else None
            if add is None:
                raise ExpGrantError(uncertain=False)
            member = await channel.guild.fetch_member(int(user_id))
            await before_submit()
            submitted = True
            interaction = await add(channel, member=member, amount=3000)
            while True:
                message = await messages.get()
                if (message.author.id != command.application_id or message.interaction is None
                        or message.interaction.id != interaction.id):
                    continue
                for embed in message.embeds:
                    if (embed.author.name == "Success"
                            and embed.description == f"The user <@{user_id}> has received **3000** exp."):
                        return str(message.id)
                    if embed.author.name == "Error":
                        raise ExpGrantError(uncertain=False)
    except (discord.DiscordException, aiohttp.ClientError, OSError, TimeoutError) as error:
        raise ExpGrantError(uncertain=submitted) from error
    finally:
        if gateway is not None:
            gateway.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await gateway
            except (discord.DiscordException, aiohttp.ClientError, OSError):
                logging.exception("Staff EXP gateway disconnected")
        try:
            await asyncio.wait_for(client.close(), timeout=10)
        except (discord.DiscordException, aiohttp.ClientError, OSError, TimeoutError):
            logging.exception("Staff EXP client could not close cleanly")
