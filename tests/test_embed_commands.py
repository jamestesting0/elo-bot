import asyncio
import unittest
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elo_bot.extensions.embed import EmbedCommands
from elo_bot.db import BaseStore, ServerState
import elo_bot.features.matches.resources as resources_module
import elo_bot.extensions.match.base_store as match_base_store_module
import elo_bot.extensions.match.dashboards as match_dashboards_module


class TestEmbedCommands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        resources_module.RESOURCE_CLAN_LOCKS.clear()

    async def test_clan_availability_embed_shows_slot_thread_mentions(self):
        bot = MagicMock()
        bot.file_locks = defaultdict(asyncio.Lock)
        cog = EmbedCommands(bot)

        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.id = 123
        ctx.channel.send = AsyncMock(return_value=MagicMock(id=456, edit=AsyncMock()))
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.guild.fetch_member = AsyncMock(return_value=MagicMock(id=123, display_name="Player"))

        clan = MagicMock(
            tag="#CLAN",
            name="Test Clan",
            availability_str="Available",
            available_slots=["Slot 2"],
        )
        free_clan = MagicMock(
            tag="#FREE",
            name="Free Clan",
            availability_str="Available",
            available_slots=["Slot 1", "Slot 2"],
        )
        match = MagicMock(thread="987")
        match.participants = [
            MagicMock(
                user_id="123",
                base_store=BaseStore(
                    clan_tag="#CLAN", slot="Slot 1", stored_at=3000000000
                ),
            )
        ]

        with (
                patch("elo_bot.extensions.embed.Clan") as mock_clan,
                patch("elo_bot.extensions.embed.Match") as mock_match,
                patch.object(
                    ServerState,
                    "update_fields",
                    new_callable=AsyncMock,
                ) as mock_update_fields,
        ):
            mock_clan.find_all.return_value.to_list = AsyncMock(return_value=[clan, free_clan])
            mock_match.find.return_value.to_list = AsyncMock(return_value=[match])

            await cog.embed.callback.func(cog, ctx, embed_type="Clan Availability")

        mock_update_fields.assert_awaited_once_with(
            clan_availability_embed={"channel": 123, "message": 456}
        )
        embed = ctx.channel.send.call_args.kwargs["embed"]
        self.assertEqual(
            embed.fields[0].value,
            "Available\nSlot 1: <#987> **[Player](<https://discordapp.com/users/123>)**",
        )
        self.assertEqual(embed.fields[1].value, "Available")

    async def test_manual_free_rejects_locked_clan_with_unlocked_alternatives(self) -> None:
        # Given a manager targeting locked clan A while clan B is still available.
        bot = MagicMock()
        cog = EmbedCommands(bot)
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.has_role.return_value = True
        ctx.values = ["#A"]
        ctx.send = AsyncMock()
        clan_a = MagicMock(tag="#A", name="Alpha", availability_str="<#101> occupied")
        clan_b = MagicMock(tag="#B", availability_str="Available")
        clan_b.name = "Bravo"
        reservation = await match_base_store_module.try_reserve_resource_clan("#A")

        try:
            with patch("elo_bot.extensions.embed.Clan") as clan_model, \
                    patch.object(match_dashboards_module.Clan, "find_all") as find_all:
                clan_model.find_one = AsyncMock(return_value=clan_a)
                find_all.return_value.to_list = AsyncMock(return_value=[clan_a, clan_b])

                # When the manager tries to free clan A.
                await cog.free_clan_select.callback.func(cog, ctx)
        finally:
            reservation.release()

        # Then the conflicting write is rejected and only unlocked clan B is listed.
        response = ctx.send.await_args.args[0]
        self.assertIn("Available clans:", response)
        self.assertIn("- Bravo (#B)", response)
        self.assertNotIn("- Alpha (#A)", response)


if __name__ == "__main__":
    unittest.main()
