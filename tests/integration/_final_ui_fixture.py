from unittest.mock import AsyncMock, MagicMock

import interactions as ipy

from elo_bot.config.ids import LVL1_ROLES
from elo_bot.extensions.final import FinalCommands
from elo_bot.features.finals.lifecycle import final_for_view
from tests.integration._final_fixture import FinalScenarioCase


def round_select(payload):
    card = payload["components"][0]
    return next((part.components[0] for part in card if isinstance(part, ipy.ActionRow)), None)


class FinalUICase(FinalScenarioCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.posts = {}
        self.next_interaction = 1000

        async def send(**payload):
            message = MagicMock(id=100 + len(self.posts), edit=AsyncMock(), pin=AsyncMock())
            message.author.id = 42
            message.nonce = payload.get("nonce")
            message.embeds = [payload["embed"]] if payload.get("embed") else []
            message.components = payload.get("components") or []
            message.attachments = []
            message.jump_url = f"https://discord.com/channels/1/2/{message.id}"
            message.payload = payload
            self.posts[str(message.id)] = message
            return message

        self.channel = MagicMock(send=AsyncMock(side_effect=send), edit=AsyncMock(),
            fetch_message=AsyncMock(side_effect=lambda message, **_: self.posts[str(message)]),
            fetch_messages=AsyncMock(side_effect=lambda **kwargs: list(self.posts.values())))
        self.channel.guild.fetch_member = AsyncMock(return_value=None)
        self.bot = MagicMock(ext={}, fetch_channel=AsyncMock(return_value=self.channel))
        self.bot.user.id = 42
        self.cog = FinalCommands(self.bot)
        self.final = await self.roster(8)
        self.channel.id = self.final.channel_id

    async def betting_message(self, round_name="Quarterfinals"):
        reference = (await final_for_view()).betting_posts[round_name]
        return self.posts[reference.message_id]

    def context(self, user=9, *, manager=True, component=False):
        self.next_interaction += 1
        ctx = MagicMock(id=self.next_interaction, guild_id=self.final.guild_id, values=[], send=AsyncMock(),
                        edit_origin=AsyncMock(), edit=AsyncMock(), send_modal=AsyncMock(), deferred=False, editing_origin=False)
        ctx.__class__ = ipy.ComponentContext if component else ipy.SlashContext
        ctx.author.id = user
        ctx.author.has_role.side_effect = lambda role: manager and role in LVL1_ROLES
        ctx.guild.fetch_member = AsyncMock(return_value=None)

        async def defer(**kwargs):
            ctx.deferred = True
            ctx.editing_origin = kwargs.get("edit_origin", False)

        ctx.defer = AsyncMock(side_effect=defer)
        return ctx

    def library_context(self, custom_id, *, modal=False, selection=None):
        client = MagicMock()
        client.app.id = 42
        user = MagicMock(id=9)
        user.has_role.return_value = False
        client.cache.get_member.return_value = user
        client.cache.get_user.return_value = user
        client.cache.place_user_data.return_value = user
        client.cache.get_guild.return_value.fetch_member = AsyncMock(return_value=None)
        message = MagicMock(id=9001, flags=ipy.MessageFlags.EPHEMERAL)
        client.cache.place_message_data.return_value = message
        client.cache.get_message.return_value = message
        client.http.post_initial_response = AsyncMock()
        client.http.edit_interaction_message = AsyncMock(return_value={"id": "9001"})
        client.http.get_interaction_message = AsyncMock(return_value={"id": "9001"})
        client.http.post_followup = AsyncMock(return_value={"id": "9002"})
        payload = {"id": "1750000000000000000", "application_id": "42", "type": 5 if modal else 3,
            "token": "offline", "version": 1, "locale": "en-US", "guild_id": str(self.final.guild_id),
            "channel_id": "77", "message": {"id": "9001"},
            "user": {"id": "9", "username": "Player", "discriminator": "0", "avatar": None},
            "data": {"custom_id": custom_id, "component_type": 2}}
        if modal:
            payload["data"] = {"custom_id": custom_id,
                "components": [{"type": 1, "components": [{"type": 4, "custom_id": "amount", "value": "1000"}]}]}
        elif selection is not None:
            payload["data"].update({"component_type": 3, "values": [selection]})
        context = ipy.ModalContext if modal else ipy.ComponentContext
        return context.from_dict(client, payload), client
