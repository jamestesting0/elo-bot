import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elobot.extensions.base import BaseCommands
from elobot.config.presentation import LB_ENTRIES_PER_PAGE
from tests._patch_sites import patch_sites


class TestBaseLeaderboardPagination(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.cog = BaseCommands(self.bot)

    async def test_page_button_opens_modal(self):
        # Given: the base-leaderboard owner clicks its current/total control
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.id = 123
        ctx.message.interaction.user.id = 123
        ctx.send = AsyncMock()
        ctx.send_modal = AsyncMock()

        # When: the base-leaderboard page callback handles the click
        await self.cog.base_lb_page_button.callback(ctx)

        # Then: it opens only the base-leaderboard modal
        modal = ctx.send_modal.await_args.args[0]
        self.assertEqual(modal.custom_id, "base_lb_page_modal")
        self.assertEqual(modal.components[0].custom_id, "page")
        ctx.send.assert_not_awaited()

    @patch('elobot.extensions.base.leaderboard.construct_base_leaderboard_string', new_callable=AsyncMock)
    @patch('elobot.extensions.base.leaderboard.sort_bases', new_callable=AsyncMock)
    @patch_sites(('elobot.extensions.base.extraction.BaseLayout', 'elobot.extensions.base.submissions.BaseLayout', 'elobot.extensions.base.manage.BaseLayout', 'elobot.extensions.base.private.BaseLayout', 'elobot.extensions.base.leaderboard.BaseLayout', 'elobot.features.bases.layouts.queries.BaseLayout', 'elobot.features.bases.layouts.ui.BaseLayout', 'elobot.features.bases.layouts.private.BaseLayout'))
    async def test_page_modal_jumps_and_preserves_selects(
            self, mock_base_layout, mock_sort_bases, mock_construct_leaderboard):
        # Given: eleven freshly sorted bases and two existing filter rows
        bases = [MagicMock() for _ in range(LB_ENTRIES_PER_PAGE + 1)]
        mock_sort_bases.return_value = bases
        mock_construct_leaderboard.return_value = "last base"
        district_row = MagicMock()
        quality_row = MagicMock()
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.author.id = 123
        ctx.message.interaction.user.id = 123
        ctx.message.embeds = [SimpleNamespace(
            title="**Best Performing Barbarian Camp Bases (Excellent)**",
            description="first page",
        )]
        ctx.message.components = [district_row, quality_row, MagicMock()]
        ctx.message.edit = AsyncMock()
        ctx.edit = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # When: the owner submits the final page number
        await self.cog.base_lb_page_modal.callback(ctx, page="2")

        # Then: the last offset is rendered and only pagination row two changes
        self.assertEqual(mock_construct_leaderboard.await_args.args[1:], (bases, LB_ENTRIES_PER_PAGE))
        self.assertIs(ctx.message.components[0], district_row)
        self.assertIs(ctx.message.components[1], quality_row)
        page_button = ctx.message.components[2].components[1]
        self.assertEqual(page_button.label, "2/2")
        self.assertEqual(page_button.custom_id, "base_lb_page")
        self.assertFalse(page_button.disabled)
        self.assertTrue(ctx.message.components[2].components[2].disabled)
        ctx.edit.assert_awaited_once()
        ctx.message.edit.assert_not_awaited()
        ctx.defer.assert_not_awaited()
        ctx.send.assert_not_awaited()

    @patch('elobot.extensions.base.leaderboard.sort_bases', new_callable=AsyncMock)
    @patch_sites(('elobot.extensions.base.extraction.BaseLayout', 'elobot.extensions.base.submissions.BaseLayout', 'elobot.extensions.base.manage.BaseLayout', 'elobot.extensions.base.private.BaseLayout', 'elobot.extensions.base.leaderboard.BaseLayout', 'elobot.features.bases.layouts.queries.BaseLayout', 'elobot.features.bases.layouts.ui.BaseLayout', 'elobot.features.bases.layouts.private.BaseLayout'))
    async def test_page_modal_rejects_stale_page(self, mock_base_layout, mock_sort_bases):
        # Given: a modal opened for page two after the dataset shrank to one page
        mock_sort_bases.return_value = [MagicMock()]
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.author.id = 123
        ctx.message.interaction.user.id = 123
        ctx.message.embeds = [SimpleNamespace(
            title="**Best Performing Barbarian Camp Bases (Excellent)**",
            description="unchanged",
        )]
        ctx.message.components = [MagicMock(), MagicMock(), MagicMock()]
        ctx.message.edit = AsyncMock()
        ctx.edit = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # When: the owner submits the stale page number
        await self.cog.base_lb_page_modal.callback(ctx, page="2")

        # Then: the fresh one-page bound is reported without editing
        self.assertIn("between 1 and 1", ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        ctx.edit.assert_not_awaited()
        ctx.message.edit.assert_not_awaited()
        ctx.defer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
