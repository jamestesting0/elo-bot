import unittest
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy

from elo_bot.extensions.base import BaseCommands
from tests._patch_sites import patch_sites


def _mock_listed_page(mock_base_layout, bases, index):
    query = MagicMock()
    mock_base_layout.find.return_value = query
    query.find.return_value = query
    query.count = AsyncMock(return_value=len(bases))
    page = [bases[index]] if 0 <= index < len(bases) else []
    query.sort.return_value.skip.return_value.limit.return_value.to_list = AsyncMock(return_value=page)
    return query


class TestBasePagination(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.cog = BaseCommands(self.bot)

    @patch_sites(('elo_bot.extensions.base.manage.construct_base_embed', 'elo_bot.extensions.base.listing.construct_base_embed', 'elo_bot.extensions.base.private.construct_base_embed', 'elo_bot.features.bases.layouts.private.construct_base_embed'))
    @patch_sites(('elo_bot.extensions.base.extraction.BaseLayout', 'elo_bot.extensions.base.submissions.BaseLayout', 'elo_bot.extensions.base.manage.BaseLayout', 'elo_bot.extensions.base.private.BaseLayout', 'elo_bot.extensions.base.leaderboard.BaseLayout', 'elo_bot.features.bases.layouts.queries.BaseLayout', 'elo_bot.features.bases.layouts.ui.BaseLayout', 'elo_bot.features.bases.layouts.private.BaseLayout'))
    async def test_navigation_keeps_enabled_page_button(self, mock_base_layout, mock_construct_embed):
        for direction, source, target in (("right", 0, 1), ("left", 1, 0)):
            with self.subTest(direction=direction):
                mock_construct_embed.reset_mock()
                bases = [MagicMock(), MagicMock()]
                for base in bases:
                    base.builder_id = None
                query = _mock_listed_page(mock_base_layout, bases, target)
                ctx = MagicMock(spec=ipy.ComponentContext)
                ctx.author.id = 123
                ctx.message.interaction.user = 123
                ctx.custom_id = f"base_{direction}|Any|{source}|Performance|Any|Any"
                ctx.defer = AsyncMock()
                ctx.edit = AsyncMock()
                ctx.edit_origin = AsyncMock()

                callback = getattr(self.cog, f"base_{direction}_button").callback
                await callback.func(self.cog, ctx)

                query.sort.return_value.skip.assert_called_once_with(target)
                query.sort.return_value.skip.return_value.limit.assert_called_once_with(1)
                ctx.defer.assert_not_awaited()
                ctx.edit.assert_not_awaited()
                ctx.edit_origin.assert_awaited_once()
                components = ctx.edit_origin.await_args.kwargs["components"]
                self.assertEqual(len(components), 3)
                self.assertEqual(components[0].components[0].custom_id, "base_list_district_select")
                self.assertEqual(components[1].components[0].custom_id, "base_list_embed_type_select")
                page_button = components[2].components[1]
                self.assertEqual(page_button.label, f"{target + 1}/2")
                self.assertEqual(page_button.custom_id, "base_page|Any|Performance|Any|Any")
                self.assertFalse(page_button.disabled)
                self.assertEqual(components[2].components[0].disabled, target == 0)
                self.assertEqual(components[2].components[2].disabled, target == 1)
                mock_construct_embed.assert_called_once_with(bases[target], "Performance", names={})

    async def test_page_button_opens_stateful_modal(self):
        # Given: the original command owner clicks the page-count control
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.id = 123
        ctx.message.interaction.user = 123
        ctx.custom_id = "base_page|Barbarian Camp|Performance|Excellent|456"
        ctx.send = AsyncMock()
        ctx.send_modal = AsyncMock()

        # When: the page-count callback handles the click
        await self.cog.base_page_button.callback(ctx)

        # Then: its modal carries the current filters without acknowledging first
        modal = ctx.send_modal.await_args.args[0]
        self.assertEqual(
            modal.custom_id,
            "base_page_modal|Barbarian Camp|Performance|Excellent|456",
        )
        self.assertEqual(modal.components[0].custom_id, "page")
        ctx.send.assert_not_awaited()

    async def test_page_button_rejects_other_user(self):
        # Given: a different user clicks the original command's page-count control
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.id = 999
        ctx.message.interaction.user = 123
        ctx.custom_id = "base_page|Any|Performance|Any|Any"
        ctx.send = AsyncMock()
        ctx.send_modal = AsyncMock()

        # When: the page-count callback checks ownership
        await self.cog.base_page_button.callback(ctx)

        # Then: the click is rejected privately without opening a modal
        ctx.send.assert_awaited_once()
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        ctx.send_modal.assert_not_awaited()

    @patch_sites(('elo_bot.extensions.base.manage.construct_base_embed', 'elo_bot.extensions.base.listing.construct_base_embed', 'elo_bot.extensions.base.private.construct_base_embed', 'elo_bot.features.bases.layouts.private.construct_base_embed'))
    @patch_sites(('elo_bot.extensions.base.extraction.BaseLayout', 'elo_bot.extensions.base.submissions.BaseLayout', 'elo_bot.extensions.base.manage.BaseLayout', 'elo_bot.extensions.base.private.BaseLayout', 'elo_bot.extensions.base.leaderboard.BaseLayout', 'elo_bot.features.bases.layouts.queries.BaseLayout', 'elo_bot.features.bases.layouts.ui.BaseLayout', 'elo_bot.features.bases.layouts.private.BaseLayout'))
    async def test_page_modal_jumps_to_last_fresh_page(self, mock_base_layout, mock_construct_embed):
        # Given: the modal submit sees three freshly queried bases
        bases = [MagicMock(), MagicMock(), MagicMock()]
        for base in bases:
            base.builder_id = None
        query = _mock_listed_page(mock_base_layout, bases, 2)
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.author.id = 123
        ctx.message.interaction.user = 123
        ctx.message.edit = AsyncMock()
        ctx.custom_id = "base_page_modal|Any|Performance|Any|Any"
        ctx.edit = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # When: the owner submits the last visible page number
        await self.cog.base_page_modal.callback(ctx, page="3")

        # Then: the last base and a terminal 3/3 control replace the original message
        mock_construct_embed.assert_called_once_with(bases[2], "Performance", names={})
        query.sort.return_value.skip.assert_called_once_with(2)
        query.sort.return_value.skip.return_value.limit.assert_called_once_with(1)
        self.assertEqual(ctx.edit.await_args.args[0], "@original")
        components = ctx.edit.await_args.kwargs["components"]
        self.assertEqual(len(components), 3)
        self.assertEqual(components[0].components[0].custom_id, "base_list_district_select")
        page_button = components[2].components[1]
        self.assertEqual(page_button.label, "3/3")
        self.assertFalse(page_button.disabled)
        self.assertTrue(components[2].components[2].disabled)
        ctx.edit.assert_awaited_once()
        ctx.message.edit.assert_not_awaited()
        ctx.defer.assert_not_awaited()
        ctx.send.assert_not_awaited()

    @patch_sites(('elo_bot.extensions.base.extraction.BaseLayout', 'elo_bot.extensions.base.submissions.BaseLayout', 'elo_bot.extensions.base.manage.BaseLayout', 'elo_bot.extensions.base.private.BaseLayout', 'elo_bot.extensions.base.leaderboard.BaseLayout', 'elo_bot.features.bases.layouts.queries.BaseLayout', 'elo_bot.features.bases.layouts.ui.BaseLayout', 'elo_bot.features.bases.layouts.private.BaseLayout'))
    async def test_page_modal_rejects_stale_page(self, mock_base_layout):
        # Given: page three existed when opened but only two bases remain at submit time
        _mock_listed_page(mock_base_layout, [MagicMock(), MagicMock()], 2)
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.author.id = 123
        ctx.message.interaction.user = 123
        ctx.message.edit = AsyncMock()
        ctx.custom_id = "base_page_modal|Any|Performance|Any|Any"
        ctx.edit = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # When: the owner submits the now-missing page
        await self.cog.base_page_modal.callback(ctx, page="3")

        # Then: fresh bounds reject it privately without editing the base message
        self.assertIn("between 1 and 2", ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        ctx.edit.assert_not_awaited()
        ctx.message.edit.assert_not_awaited()
        ctx.defer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
