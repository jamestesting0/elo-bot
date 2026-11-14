import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from cogs.season_commands import SeasonCommands
from elobot.config.presentation import LB_ENTRIES_PER_PAGE


class TestSeasonPagination(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.cog = SeasonCommands(self.bot)

    @patch("cogs.season_commands.construct_season_leaderboard_string", new_callable=AsyncMock)
    @patch("cogs.season_commands.Season")
    async def test_initial_page_button_is_enabled(self, mock_season, mock_construct_leaderboard):
        # Given: an eleven-entry recorded district leaderboard
        season_doc = SimpleNamespace(
            season_number=3,
            start=100,
            end=200,
            districts={"Barbarian Camp": [MagicMock() for _ in range(11)]},
        )
        mock_season.find_one = AsyncMock(return_value=season_doc)
        mock_construct_leaderboard.return_value = "bases"
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # When: the season base leaderboard is rendered
        await self.cog.season_base_leaderboard.callback(
            ctx, season=3, district="Barbarian Camp", hidden=False
        )

        # Then: its 1/2 middle control is clickable and season-specific
        page_button = ctx.send.await_args.kwargs["components"][0].components[1]
        self.assertEqual(page_button.label, "1/2")
        self.assertEqual(page_button.custom_id, "season_lb_page")
        self.assertFalse(page_button.disabled)

    @patch("cogs.season_commands.construct_season_leaderboard_string", new_callable=AsyncMock)
    @patch("cogs.season_commands.Season")
    async def test_right_navigation_keeps_enabled_page_button(self, mock_season, mock_construct_leaderboard):
        # Given: an owner navigating to the second recorded-season page
        season_doc = SimpleNamespace(districts={"Barbarian Camp": [MagicMock() for _ in range(11)]})
        mock_season.find_one = AsyncMock(return_value=season_doc)
        mock_construct_leaderboard.return_value = "last base"
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.id = 123
        ctx.custom_id = f"season_lb_right|{LB_ENTRIES_PER_PAGE}"
        ctx.message.interaction.user.id = 123
        ctx.message.embeds = [SimpleNamespace(
            title="**Season 3 Best Performing Barbarian Camp Bases**",
            description="Start: old\nEnd: old\n\nfirst page",
        )]
        ctx.message.components = [MagicMock()]
        ctx.edit_origin = AsyncMock()
        ctx.send = AsyncMock()

        # When: the right-arrow callback renders the final page
        await self.cog.season_lb_right_button.callback(ctx)

        # Then: its rebuilt middle control stays enabled at 2/2
        page_button = ctx.message.components[0].components[1]
        self.assertEqual(page_button.label, "2/2")
        self.assertEqual(page_button.custom_id, "season_lb_page")
        self.assertFalse(page_button.disabled)

    async def test_page_button_opens_modal(self):
        # Given: the season-leaderboard owner clicks its current/total control
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.id = 123
        ctx.message.interaction.user.id = 123
        ctx.send = AsyncMock()
        ctx.send_modal = AsyncMock()

        # When: the season page callback handles the click
        await self.cog.season_lb_page_button.callback(ctx)

        # Then: it opens the season page modal
        modal = ctx.send_modal.await_args.args[0]
        self.assertEqual(modal.custom_id, "season_lb_page_modal")
        self.assertEqual(modal.components[0].custom_id, "page")

    @patch("cogs.season_commands.construct_season_leaderboard_string", new_callable=AsyncMock)
    @patch("cogs.season_commands.Season")
    async def test_page_modal_jumps_and_preserves_header(self, mock_season, mock_construct_leaderboard):
        # Given: eleven fresh records and an existing date header
        district_data = [MagicMock() for _ in range(LB_ENTRIES_PER_PAGE + 1)]
        mock_season.find_one = AsyncMock(
            return_value=SimpleNamespace(districts={"Barbarian Camp": district_data})
        )
        mock_construct_leaderboard.return_value = "last base"
        header = "Start: old\nEnd: old"
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.author.id = 123
        ctx.message.interaction.user.id = 123
        ctx.message.embeds = [SimpleNamespace(
            title="**Season 3 Best Performing Barbarian Camp Bases**",
            description=f"{header}\n\nfirst page",
        )]
        ctx.message.components = [MagicMock()]
        ctx.message.edit = AsyncMock()
        ctx.edit = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # When: the owner submits the final page number
        await self.cog.season_lb_page_modal.callback.func(self.cog, ctx, page="2")

        # Then: offset ten is rendered while the date header is unchanged
        self.assertEqual(mock_construct_leaderboard.await_args.args[-1], LB_ENTRIES_PER_PAGE)
        self.assertEqual(ctx.message.embeds[0].description, f"{header}\n\nlast base")
        page_button = ctx.message.components[0].components[1]
        self.assertEqual(page_button.label, "2/2")
        self.assertFalse(page_button.disabled)
        self.assertTrue(ctx.message.components[0].components[2].disabled)
        ctx.edit.assert_awaited_once()
        ctx.message.edit.assert_not_awaited()
        ctx.defer.assert_not_awaited()
        ctx.send.assert_not_awaited()

    @patch("cogs.season_commands.Season")
    async def test_page_modal_rejects_stale_total(self, mock_season):
        # Given: a page-two modal after only one season page remains
        mock_season.find_one = AsyncMock(
            return_value=SimpleNamespace(districts={"Barbarian Camp": [MagicMock()]})
        )
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.author.id = 123
        ctx.message.interaction.user.id = 123
        ctx.message.embeds = [SimpleNamespace(
            title="**Season 3 Best Performing Barbarian Camp Bases**",
            description="unchanged",
        )]
        ctx.message.components = [MagicMock()]
        ctx.message.edit = AsyncMock()
        ctx.edit = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # When: the owner submits the stale page number
        await self.cog.season_lb_page_modal.callback.func(self.cog, ctx, page="2")

        # Then: fresh bounds reject it without editing
        self.assertIn("between 1 and 1", ctx.send.await_args.args[0])
        ctx.edit.assert_not_awaited()
        ctx.message.edit.assert_not_awaited()
        ctx.defer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
