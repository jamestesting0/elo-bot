import asyncio
import re
import unittest
from collections import defaultdict
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

import elo_bot.extensions.match.base_store as match_base_store_module
import elo_bot.extensions.match.dashboards as match_dashboards_module
import elo_bot.features.matches.resources as resources_module
from elo_bot.config.ids import GUILD_IDS
from elo_bot.core.season_calendar import season_calendar
from elo_bot.db import BaseStore, ServerState
from elo_bot.extensions.embed import EmbedCommands


class TestEmbedCommands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        resources_module.RESOURCE_CLAN_LOCKS.clear()

    async def test_elo_final_embed_posts_a_details_button_that_answers_privately_with_the_schedule(self):
        cog = EmbedCommands(MagicMock())
        ctx = MagicMock(spec=ipy.SlashContext, defer=AsyncMock(), send=AsyncMock())
        ctx.channel.send = AsyncMock()
        await cog.embed.callback.func(cog, ctx, "Elo Final")
        posted = ctx.channel.send.await_args.kwargs
        button = posted["components"][0].components[0]
        self.assertEqual(button.custom_id, "elo_final_details")
        details = MagicMock(spec=ipy.ComponentContext, send=AsyncMock())
        calendar = season_calendar(datetime(2026, 11, 19, tzinfo=UTC), datetime(2026, 10, 4, tzinfo=UTC), None)
        with patch("elo_bot.extensions.embed.ongoing_season_calendar", new=AsyncMock(return_value=calendar)):
            await cog.elo_final_details.callback.func(cog, details)
        reply = details.send.await_args.kwargs
        self.assertTrue(reply["ephemeral"])
        dates, schedule = reply["embed"].description.split("\n\n", 1)
        self.assertEqual([int(stamp) for stamp in re.findall(r"<t:(\d+):D>", dates)],
                         [int(moment.timestamp()) for moment in (calendar.final_selection, calendar.queue_closes,
                                                                 calendar.season_ends)])
        with open("assets/embeds/elo_final.md") as source:
            self.assertEqual(schedule, source.read())

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
            "Available\nSlot 1: <#987> **[Player](<https://discordapp.com/users/123?tab=wishlist>)**",
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


    async def test_member_update_without_cached_roles_is_ignored(self):
        cog = EmbedCommands(MagicMock())
        event = MagicMock(before=None)
        event.guild.id = GUILD_IDS[0]
        with patch("elo_bot.extensions.embed.Player.find_one", new=AsyncMock()) as lookup:
            await cog.on_member_update.callback(cog, event)
        lookup.assert_not_awaited()
        event.after.fetch_dm.assert_not_called()
