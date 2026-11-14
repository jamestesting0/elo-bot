import logging
import re

import interactions as ipy

from elo_bot.discord import runtime
from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.config.ids import ELO_BLACKLIST_ROLE
from elo_bot.config.settings import TOKENS
from elo_bot.discord.contexts import ComponentsV2ModalContext

INTENTS = ipy.Intents.new(
    guilds=True,
    guild_emojis_and_stickers=True,
    guild_presences=True,
    guild_members=True,
    guild_moderation=True,
    guild_messages=True,
    direct_messages=True,
    message_content=True,
)


class DiscordApiRouteFilter(logging.Filter):
    _route_pattern = re.compile(r"^[A-Z]+::https://discord\.com/api/v\d+/.+: \d{3}$")

    def filter(self, record: logging.LogRecord) -> bool:
        return not self._route_pattern.match(record.getMessage())


async def dispatch_elo_interaction(event: ipy.events.RawGatewayEvent) -> None:
    data = event.data
    if str(ELO_BLACKLIST_ROLE) in data.get("member", {}).get("roles", []):
        response = {"type": 4, "data": {
            "content": f"{ERROR_EMOJI} You are banned from using Elo commands. " 
            "Please contact the staff team if you have doubts.", 
            "flags": 64,
        }}
        if data["type"] == 4:
            response = {"type": 8, "data": {"choices": []}}
        await runtime.bot.http.post_initial_response(response, token=data["token"], interaction_id=data["id"])
        return
    await ipy.Client._dispatch_interaction.callback(runtime.bot, event)


def create_client(version: str) -> ipy.Client:
    runtime.VERSION = version
    logging.getLogger("interactions").addFilter(DiscordApiRouteFilter())
    runtime.bot = ipy.Client(
        token=TOKENS[runtime.VERSION],
        intents=INTENTS,
        fetch_members=runtime.VERSION == "main",
        sync_interactions=True,
        send_command_tracebacks=False,
        send_not_ready_messages=True,
        show_ratelimit_tracebacks=True,
        modal_context=ComponentsV2ModalContext,
        delete_unused_application_cmds=runtime.VERSION == "main",
        activity=ipy.Activity(
            name="DM your feedback to @jamestesting",
            type=ipy.ActivityType.PLAYING,
        ),
    )
    runtime.bot.add_event_processor("raw_interaction_create")(dispatch_elo_interaction)
    return runtime.bot
