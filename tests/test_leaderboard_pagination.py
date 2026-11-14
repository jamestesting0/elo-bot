import unittest
from asyncio import wait_for
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elobot.extensions.leaderboard import LeaderboardCommands
from elobot.config.presentation import LB_ENTRIES_PER_PAGE


class TestLeaderboardPagination(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.cog = LeaderboardCommands(self.bot)

    async def test_page_button_opens_modal(self):
        # Given: the original leaderboard owner clicks its current/total control
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.id = 123
        ctx.message.interaction.user.id = 123
        ctx.send = AsyncMock()
        ctx.send_modal = AsyncMock()

        # When: the leaderboard page callback handles the click
        await self.cog.lb_page_button.callback(ctx)

        # Then: it opens the player-leaderboard page modal
        modal = ctx.send_modal.await_args.args[0]
        self.assertEqual(modal.custom_id, "lb_page_modal")
        self.assertEqual(modal.components[0].custom_id, "page")
        ctx.send.assert_not_awaited()

    @patch("elobot.extensions.leaderboard.Player")
    @patch("elobot.extensions.leaderboard.construct_leaderboard_string", new_callable=AsyncMock)
    @patch("elobot.extensions.leaderboard.Season")
    async def test_page_modal_jumps_to_last_page(
            self, mock_season, mock_construct_leaderboard, mock_player):
        # Given: eleven Elo players and two selector rows
        players = [SimpleNamespace(user_id=str(index)) for index in range(LB_ENTRIES_PER_PAGE + 1)]
        page_query = MagicMock()
        page_query.count = AsyncMock(return_value=len(players))
        page_query.sort.return_value.skip.return_value.limit.return_value.to_list = AsyncMock(
            return_value=[players[-1]]
        )
        mock_player.find_all.return_value = page_query
        mock_construct_leaderboard.return_value = "last player"
        mock_season.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=[])
        type_row = MagicMock()
        season_row = MagicMock()
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.author.id = 123
        ctx.guild = MagicMock()
        ctx.message.interaction.user.id = 123
        ctx.message.embeds = [SimpleNamespace(
            title="**Elo Leaderboard**",
            fields=[SimpleNamespace(value="All Seasons")],
            description="first page",
        )]
        ctx.message.components = [type_row, season_row, MagicMock()]
        ctx.message.edit = AsyncMock()
        ctx.edit = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # When: the owner submits the last page number
        await self.cog.lb_page_modal.callback.func(self.cog, ctx, page="2")

        # Then: offset ten is rendered and only row two is replaced
        self.assertEqual(page_query.sort.call_args.args[0], "-elo")
        page_query.sort.return_value.skip.assert_called_once_with(LB_ENTRIES_PER_PAGE)
        page_query.sort.return_value.skip.return_value.limit.assert_called_once_with(LB_ENTRIES_PER_PAGE)
        self.assertEqual(mock_construct_leaderboard.await_args.args[-1], LB_ENTRIES_PER_PAGE)
        self.assertTrue(mock_construct_leaderboard.await_args.kwargs["paged"])
        self.assertIs(ctx.message.components[0], type_row)
        self.assertIs(ctx.message.components[1], season_row)
        page_button = ctx.message.components[2].components[1]
        self.assertEqual(page_button.label, "2/2")
        self.assertEqual(page_button.custom_id, "lb_page")
        self.assertFalse(page_button.disabled)
        self.assertTrue(ctx.message.components[2].components[2].disabled)
        ctx.edit.assert_awaited_once()
        ctx.message.edit.assert_not_awaited()
        ctx.defer.assert_not_awaited()
        ctx.send.assert_not_awaited()

    @patch("elobot.extensions.leaderboard.Player")
    @patch("elobot.extensions.leaderboard.construct_leaderboard_string", new_callable=AsyncMock)
    @patch("elobot.extensions.leaderboard.Season")
    async def test_highest_elo_page_modal_uses_row_one(
            self, mock_season, mock_construct_leaderboard, mock_player):
        # Given: a Highest Elo view with no season selector
        players = [SimpleNamespace(user_id=str(index)) for index in range(LB_ENTRIES_PER_PAGE + 1)]
        page_query = MagicMock()
        page_query.count = AsyncMock(return_value=len(players))
        page_query.sort.return_value.skip.return_value.limit.return_value.to_list = AsyncMock(
            return_value=[players[-1]]
        )
        mock_player.find_all.return_value = page_query
        mock_construct_leaderboard.return_value = "last player"
        mock_season.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=[])
        type_row = MagicMock()
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.author.id = 123
        ctx.guild = MagicMock()
        ctx.message.interaction.user.id = 123
        ctx.message.embeds = [SimpleNamespace(
            title="**Highest Elo Leaderboard**",
            fields=[SimpleNamespace(value="All Seasons")],
            description="first page",
        )]
        ctx.message.components = [type_row, MagicMock()]
        ctx.message.edit = AsyncMock()
        ctx.edit = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # When: the owner submits page two
        await self.cog.lb_page_modal.callback.func(self.cog, ctx, page="2")

        # Then: the paginator remains at row one and the type selector is preserved
        self.assertEqual(page_query.sort.call_args.args[0], "-highest_elo")
        self.assertIs(ctx.message.components[0], type_row)
        self.assertEqual(ctx.message.components[1].components[1].label, "2/2")
        ctx.edit.assert_awaited_once()
        ctx.message.edit.assert_not_awaited()

    @patch("elobot.extensions.leaderboard.Player")
    @patch("elobot.extensions.leaderboard.Season")
    async def test_page_modal_rejects_stale_total(self, mock_season, mock_player):
        # Given: a page-two modal after the leaderboard shrank to one page
        page_query = MagicMock()
        page_query.count = AsyncMock(return_value=1)
        mock_player.find_all.return_value = page_query
        mock_season.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=[])
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.author.id = 123
        ctx.message.interaction.user.id = 123
        ctx.message.embeds = [SimpleNamespace(
            title="**Elo Leaderboard**",
            fields=[SimpleNamespace(value="All Seasons")],
            description="unchanged",
        )]
        ctx.message.components = [MagicMock(), MagicMock(), MagicMock()]
        ctx.message.edit = AsyncMock()
        ctx.edit = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # When: the owner submits the stale page number
        await self.cog.lb_page_modal.callback.func(self.cog, ctx, page="2")

        # Then: the fresh one-page bound is reported without editing
        page_query.sort.assert_not_called()
        self.assertIn("between 1 and 1", ctx.send.await_args.args[0])
        ctx.edit.assert_not_awaited()
        ctx.message.edit.assert_not_awaited()
        ctx.defer.assert_not_awaited()

    @patch("elobot.extensions.leaderboard.sort_players", new_callable=AsyncMock)
    @patch("elobot.extensions.leaderboard.Season")
    async def test_repeat_arrow_reuses_ranking_until_player_cache_changes(self, mock_season, mock_sort_players):
        # Given: a cached Addiction ranking for all seasons
        players = [SimpleNamespace(user_id="1", elo=1500, highest_elo=1600, matches=[])]
        self.cog._player_cache = players
        mock_sort_players.return_value = players
        mock_season.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=[])
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.id = 123
        ctx.guild = MagicMock()
        ctx.custom_id = "lb_right|0"
        ctx.message.interaction.user.id = 123
        ctx.message.embeds = [SimpleNamespace(
            title="**Addiction Leaderboard**",
            fields=[SimpleNamespace(value="All Seasons")],
            description="",
        )]
        ctx.message.components = [MagicMock(), MagicMock(), MagicMock()]
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        ctx.edit_origin = AsyncMock()
        ctx.send = AsyncMock()

        # When: the same page arrow is used twice, then that player's matches change
        await self.cog.lb_pagination.callback.func(self.cog, ctx)
        await self.cog.lb_pagination.callback.func(self.cog, ctx)
        updated = SimpleNamespace(user_id="1", elo=1400, highest_elo=1600, matches=[])
        await self.cog.update_player_cache(updated)
        self.assertEqual(self.cog._rankings, {})
        await wait_for(self.cog._ranking_task, timeout=5)
        await self.cog.lb_pagination.callback.func(self.cog, ctx)

        # Then: both clicks use stored stats, and the update does not sort every match list
        mock_sort_players.assert_not_awaited()
        mock_season.find_all.assert_called_once()
        self.assertIs(self.cog._rankings[("Addiction", "All Seasons", -1)][0], updated)


if __name__ == "__main__":
    unittest.main()
