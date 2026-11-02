import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from cogs.match_commands import MatchCommands
from cogs.war_hosting import war_start
from common.discord.concurrency import CHANNEL_USER_CONCURRENCY_BUCKET


class TestMatchWarAutomatic(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = MagicMock()
        self.bot.user = MagicMock(id=9999)
        self.bot.owner = MagicMock(id=9998)
        self.bot.coc_client.get_clan = AsyncMock(return_value=MagicMock(
            tag="#CLAN",
            name="Helper Clan",
            share_link="https://clan.example",
        ))
        self.cog = MatchCommands(self.bot)

    @staticmethod
    def make_context() -> ipy.SlashContext:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=999, mention="<@999>")
        ctx.channel = MagicMock(
            id=12345,
            name="🟡 Match Thread",
            created_at=datetime.now(timezone.utc),
        )
        ctx.channel.fetch_pinned_messages = AsyncMock(
            return_value=[MagicMock(embeds=[MagicMock(fields=[])])],
        )
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        return ctx

    @staticmethod
    def make_match() -> MagicMock:
        participants = [
            MagicMock(user_id="100", result=[None] * 3, base_store=None, opponent_bases_stored=False),
            MagicMock(user_id="200", result=[None] * 3, base_store=None, opponent_bases_stored=False),
        ]
        match = MagicMock(
            token="TOKEN",
            thread="12345",
            is_war=True,
            is_autowin=False,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
            participants=participants,
        )
        match.set = AsyncMock()
        return match

    def test_command_split_exposes_autostart_without_manual_automatic_option(self) -> None:
        manual_options = {str(option.name) for option in self.cog.match_war_start.options}
        autostart = getattr(self.cog, "match_war_autostart", None)

        self.assertNotIn("automatic", manual_options)
        self.assertNotIn("host", manual_options)
        self.assertIsNotNone(autostart)
        self.assertEqual(
            {str(option.name) for option in autostart.options},
            {"player", "clan", "scout", "district"},
        )
        district = next(option for option in autostart.options if str(option.name) == "district")
        self.assertFalse(district.required)
        self.assertEqual(autostart.max_concurrency.concurrent, 1)
        self.assertIs(autostart.max_concurrency.bucket, CHANNEL_USER_CONCURRENCY_BUCKET)

    async def test_manual_default_preserves_occupied_slot_guard(self) -> None:
        ctx = self.make_context()
        match = self.make_match()

        with patch("cogs.match_commands.Match") as match_model, \
                patch("cogs.match_commands.is_match_finished", return_value=False), \
                patch("cogs.match_commands.is_raid_weekend", return_value=False), \
                patch("cogs.match_commands.is_clan_available", new=AsyncMock(return_value=True)), \
                patch(
                    "cogs.match_commands.get_start_base_store_slot_availability",
                    new=AsyncMock(return_value=([], {"Slot 1": ("111", "222")})),
                ) as available_slots, \
                patch(
                    "cogs.war_hosting.helper_host.host_war_automatically",
                    new_callable=AsyncMock,
                    create=True,
                ) as automatic_host:
            match_model.find_one = AsyncMock(return_value=match)

            await self.cog.match_war_start.callback.func(
                self.cog,
                ctx,
                player=0,
                clan="#CLAN",
                scout=False,
            )

        available_slots.assert_awaited_once_with("#CLAN", "12345", bot=self.bot)
        automatic_host.assert_not_awaited()
        self.assertIn("both base slots", ctx.send.await_args.args[0].lower())

    async def test_manual_start_calls_start_war_without_helper(self) -> None:
        ctx = self.make_context()
        match = self.make_match()
        clan = self.bot.coc_client.get_clan.return_value

        with patch("cogs.match_commands.Match") as match_model, \
                patch("cogs.match_commands.is_match_finished", return_value=False), \
                patch("cogs.match_commands.is_raid_weekend", return_value=False), \
                patch("cogs.match_commands.is_clan_available", new=AsyncMock(return_value=True)), \
                patch(
                    "cogs.match_commands.get_start_base_store_slot_availability",
                    new=AsyncMock(return_value=(["Slot 1"], {})),
                ) as available_slots, \
                patch(
                    "cogs.match_commands.confirm_pending_opponent_bases",
                    new=AsyncMock(return_value=True),
                ), \
                patch("cogs.match_commands.war_start.start_war", new_callable=AsyncMock) as start_war, \
                patch(
                    "cogs.war_hosting.helper_host.host_war_automatically",
                    new_callable=AsyncMock,
                    create=True,
                ) as automatic_host:
            match_model.find_one = AsyncMock(return_value=match)

            await self.cog.match_war_start.callback.func(
                self.cog,
                ctx,
                player=0,
                clan="#CLAN",
                scout=True,
            )

        available_slots.assert_awaited_once_with("#CLAN", "12345", bot=self.bot)
        automatic_host.assert_not_awaited()
        start_war.assert_awaited_once()
        runtime, host_ctx, request = start_war.await_args.args
        self.assertIs(runtime, self.bot)
        self.assertIs(host_ctx, ctx)
        self.assertIs(request.match, match)
        self.assertEqual(request.player, 0)
        self.assertIs(request.clan, clan)
        self.assertTrue(request.scout)
        self.assertIsInstance(request.reply, war_start.SlashWarStartReply)

    async def test_autostart_started_side_without_district_stops_before_clan_lookup(self) -> None:
        ctx = self.make_context()
        match = self.make_match()
        match.times[0] = 1000

        with patch("cogs.match_commands.Match") as match_model, \
                patch("cogs.match_commands.is_match_finished", return_value=False), \
                patch("cogs.match_commands.is_clan_available", new_callable=AsyncMock) as clan_available, \
                patch(
                    "cogs.war_hosting.helper_host.host_war_automatically",
                    new_callable=AsyncMock,
                    create=True,
                ) as automatic_host:
            match_model.find_one = AsyncMock(return_value=match)

            await self.cog.match_war_autostart.callback.func(
                self.cog,
                ctx,
                player=0,
                clan="#CLAN",
            )

        clan_available.assert_not_awaited()
        automatic_host.assert_not_awaited()
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertIn("already started", ctx.send.await_args.args[0].lower())

    async def test_autostart_preserves_unavailable_clan_guard(self) -> None:
        ctx = self.make_context()
        match = self.make_match()

        with patch("cogs.match_commands.Match") as match_model, \
                patch("cogs.match_commands.Clan") as clan_model, \
                patch("cogs.match_commands.is_match_finished", return_value=False), \
                patch("cogs.match_commands.is_raid_weekend", return_value=False), \
                patch("cogs.match_commands.is_clan_available", new=AsyncMock(return_value=False)), \
                patch(
                    "cogs.war_hosting.helper_host.host_war_automatically",
                    new_callable=AsyncMock,
                    create=True,
                ) as automatic_host:
            match_model.find_one = AsyncMock(return_value=match)
            clan_model.find_all.return_value.to_list = AsyncMock(return_value=[])

            await self.cog.match_war_autostart.callback.func(
                self.cog,
                ctx,
                player=0,
                clan="#CLAN",
            )

        automatic_host.assert_not_awaited()
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertIn("occupied", ctx.send.await_args.args[0].lower())
        self.assertIn("Available clans:", ctx.send.await_args.args[0])
        self.assertIn("None", ctx.send.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
