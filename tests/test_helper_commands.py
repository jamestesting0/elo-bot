import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elo_bot.extensions.helper import HelperCommands
from elo_bot.config.presentation import LB_ENTRIES_PER_PAGE
from elo_bot.config.ids import STAFF_ROLE
from elo_bot.config.emojis import SUCCESS_EMOJI


class TestHelperPagination(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.cog = HelperCommands(self.bot)

    @patch("elo_bot.extensions.helper.construct_helper_leaderboard_string", new_callable=AsyncMock)
    @patch("elo_bot.extensions.helper.Helper")
    async def test_initial_page_button_is_enabled(self, mock_helper, mock_construct_leaderboard):
        # Given: eleven helpers in the initial Points leaderboard
        helpers = [SimpleNamespace(user_id=str(index), points=index, history=[]) for index in range(11)]
        mock_helper.find_all.return_value.to_list = AsyncMock(return_value=helpers)
        mock_construct_leaderboard.return_value = "helpers"
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.guild = MagicMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # When: the helper leaderboard is rendered
        await self.cog.helper_leaderboard.callback(ctx, lb_type="Points", hidden=False)

        # Then: its 1/2 middle control is a clickable helper page button
        page_button = ctx.send.await_args.kwargs["components"][0].components[1]
        self.assertEqual(page_button.label, "1/2")
        self.assertEqual(page_button.custom_id, "helper_lb_page")
        self.assertFalse(page_button.disabled)

    @patch("elo_bot.extensions.helper.construct_helper_leaderboard_string", new_callable=AsyncMock)
    @patch("elo_bot.extensions.helper.Helper")
    async def test_right_navigation_keeps_enabled_page_button(self, mock_helper, mock_construct_leaderboard):
        # Given: eleven helpers and an owner navigating to the second page
        helpers = [SimpleNamespace(user_id=str(index), points=index, history=[]) for index in range(11)]
        mock_helper.find_all.return_value.to_list = AsyncMock(return_value=helpers)
        mock_construct_leaderboard.return_value = "last helper"
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.id = 123
        ctx.guild = MagicMock()
        ctx.custom_id = f"helper_lb_right|{LB_ENTRIES_PER_PAGE}"
        ctx.message.interaction.user.id = 123
        ctx.message.embeds = [SimpleNamespace(title="**Helper Points Leaderboard**", description="first")]
        ctx.message.components = [MagicMock()]
        ctx.edit_origin = AsyncMock()
        ctx.send = AsyncMock()

        # When: the right-arrow callback renders the last page
        await self.cog.helper_lb_right_button.callback(ctx)

        # Then: the updated middle control stays enabled at 2/2
        page_button = ctx.message.components[0].components[1]
        self.assertEqual(page_button.label, "2/2")
        self.assertEqual(page_button.custom_id, "helper_lb_page")
        self.assertFalse(page_button.disabled)

    async def test_page_button_opens_modal(self):
        # Given: the helper-leaderboard owner clicks its current/total control
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.id = 123
        ctx.message.interaction.user.id = 123
        ctx.send = AsyncMock()
        ctx.send_modal = AsyncMock()

        # When: the helper page callback handles the click
        await self.cog.helper_lb_page_button.callback(ctx)

        # Then: it opens the helper page modal
        modal = ctx.send_modal.await_args.args[0]
        self.assertEqual(modal.custom_id, "helper_lb_page_modal")
        self.assertEqual(modal.components[0].custom_id, "page")

    @patch("elo_bot.extensions.helper.construct_helper_leaderboard_string", new_callable=AsyncMock)
    @patch("elo_bot.extensions.helper.Helper")
    async def test_page_modal_jumps_to_last_page(self, mock_helper, mock_construct_leaderboard):
        # Given: eleven helpers freshly loaded when the modal is submitted
        helpers = [SimpleNamespace(user_id=str(index), points=index, history=[]) for index in range(11)]
        mock_helper.find_all.return_value.to_list = AsyncMock(return_value=helpers)
        mock_construct_leaderboard.return_value = "last helper"
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.author.id = 123
        ctx.guild = MagicMock()
        ctx.message.interaction.user.id = 123
        ctx.message.embeds = [SimpleNamespace(title="**Helper Points Leaderboard**", description="first")]
        ctx.message.components = [MagicMock()]
        ctx.message.edit = AsyncMock()
        ctx.edit = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # When: the owner submits page two
        await self.cog.helper_lb_page_modal.callback.func(self.cog, ctx, page="2")

        # Then: the renderer receives offset ten and row zero becomes terminal 2/2
        self.assertEqual(mock_construct_leaderboard.await_args.args[-1], LB_ENTRIES_PER_PAGE)
        page_button = ctx.message.components[0].components[1]
        self.assertEqual(page_button.label, "2/2")
        self.assertFalse(page_button.disabled)
        self.assertTrue(ctx.message.components[0].components[2].disabled)
        ctx.edit.assert_awaited_once()
        ctx.message.edit.assert_not_awaited()
        ctx.defer.assert_not_awaited()
        ctx.send.assert_not_awaited()

    @patch("elo_bot.extensions.helper.Helper")
    async def test_page_modal_rejects_stale_total(self, mock_helper):
        # Given: a page-two modal after only one helper page remains
        mock_helper.find_all.return_value.to_list = AsyncMock(
            return_value=[SimpleNamespace(user_id="1", points=1, history=[])]
        )
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.author.id = 123
        ctx.message.interaction.user.id = 123
        ctx.message.embeds = [SimpleNamespace(title="**Helper Points Leaderboard**", description="unchanged")]
        ctx.message.components = [MagicMock()]
        ctx.message.edit = AsyncMock()
        ctx.edit = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # When: the owner submits the stale page number
        await self.cog.helper_lb_page_modal.callback.func(self.cog, ctx, page="2")

        # Then: fresh bounds reject it without editing the leaderboard message
        self.assertIn("between 1 and 1", ctx.send.await_args.args[0])
        ctx.edit.assert_not_awaited()
        ctx.message.edit.assert_not_awaited()
        ctx.defer.assert_not_awaited()


class TestHelperRemove(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.cog = HelperCommands(self.bot)

    def test_command_only_accepts_selected_user(self):
        option_names = [str(option.name) for option in self.cog.helper_remove.options]

        self.assertEqual(option_names, ["user"])

    @patch("elo_bot.extensions.helper.Helper")
    async def test_plain_user_deletes_helper_without_removing_role(self, mock_helper):
        helper_doc = MagicMock()
        helper_doc.delete = AsyncMock()
        mock_helper.user_id.__eq__.return_value = {"user_id": "123456789012345678"}
        mock_helper.find_one = AsyncMock(return_value=helper_doc)
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        user = ipy.User.from_dict(
            {
                "id": "123456789012345678",
                "username": "target",
                "discriminator": "0",
                "avatar": None,
                "global_name": "Target",
                "public_flags": 0,
            },
            self.bot,
        )

        await self.cog.helper_remove.callback(ctx, user=user)

        mock_helper.find_one.assert_awaited_once_with({"user_id": str(user.id)})
        helper_doc.delete.assert_awaited_once_with()
        ctx.send.assert_awaited_once_with(
            f"{SUCCESS_EMOJI} {user.mention} is removed from the helpers.",
            ephemeral=True,
        )

    @patch("elo_bot.extensions.helper.Helper")
    async def test_member_with_helper_role_still_has_role_removed(self, mock_helper):
        helper_doc = MagicMock()
        helper_doc.delete = AsyncMock()
        mock_helper.user_id.__eq__.return_value = {"user_id": "123456789012345678"}
        mock_helper.find_one = AsyncMock(return_value=helper_doc)
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        member = MagicMock(spec=ipy.Member)
        member.id = 123456789012345678
        member.mention = "<@123456789012345678>"
        member.roles = [SimpleNamespace(id=STAFF_ROLE)]
        member.remove_role = AsyncMock()

        await self.cog.helper_remove.callback(ctx, user=member)

        member.remove_role.assert_awaited_once_with(STAFF_ROLE)


if __name__ == "__main__":
    unittest.main()
