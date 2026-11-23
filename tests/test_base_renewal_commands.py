import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie.odm.fields import ExpressionField

from elo_bot.extensions.base import BaseCommands
from elo_bot.config.ids import BASE_RENEWAL_LOGS_CHANNEL
from elo_bot.discord.embeds import embed_color
from tests._patch_sites import patch_sites


BASE_RENEW_MODAL_CALLBACK = BaseCommands.base_renew_modal.callback


class TestBaseRenewalCommands(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.renewal_logs_channel = MagicMock()
        self.renewal_logs_channel.send = AsyncMock()
        self.bot.fetch_channel = AsyncMock(return_value=self.renewal_logs_channel)
        self.cog = object.__new__(BaseCommands)
        BaseCommands.__init__(self.cog, self.bot)

    def test_renewal_commands_use_requested_paths(self) -> None:
        # Given
        command_paths = {
            command.resolved_name
            for klass in BaseCommands.__mro__[:BaseCommands.__mro__.index(ipy.Extension)]
            for command in vars(klass).values()
            if isinstance(command, ipy.SlashCommand)
        }

        # When
        renew_command = BaseCommands.base_renew
        set_link_command = BaseCommands.base_set_link

        # Then
        self.assertEqual(renew_command.resolved_name, "base renew")
        self.assertEqual([str(option.name) for option in renew_command.options], ["base_link"])
        self.assertEqual(set_link_command.resolved_name, "base set link")
        self.assertEqual(
            [str(option.name) for option in set_link_command.options],
            ["old_base_link", "new_base_link"],
        )
        self.assertNotIn("base autorenew", command_paths)

    @patch('elo_bot.extensions.base.manage.utils.misc_utils.disable_components', return_value=[])
    @patch_sites(('elo_bot.extensions.base.extraction.BaseLayout', 'elo_bot.extensions.base.submissions.BaseLayout', 'elo_bot.extensions.base.manage.BaseLayout', 'elo_bot.extensions.base.private.BaseLayout', 'elo_bot.extensions.base.leaderboard.BaseLayout', 'elo_bot.features.bases.layouts.queries.BaseLayout', 'elo_bot.features.bases.layouts.ui.BaseLayout', 'elo_bot.features.bases.layouts.private.BaseLayout'))
    async def test_renewal_modal_logs_success(self, mock_base_layout, _mock_disable_components) -> None:
        # Given
        old_link = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"
        new_link = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3ABBBBB"
        base = MagicMock(district="Barbarian Camp", link=old_link, image_url="https://example.com/base.png")
        base.save_changes = AsyncMock()
        mock_base_layout.link = ExpressionField("link")
        mock_base_layout.find_one = AsyncMock(return_value=base)
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.message.embeds = [ipy.Embed(fields=[ipy.EmbedField(name="Base Link", value=f"[Base Link]({old_link})")])]
        ctx.message.components = []
        ctx.message.edit = AsyncMock()

        # When
        clock = MagicMock()
        clock.now.return_value.timestamp.return_value = 2_000_000_000
        with patch_sites(('elo_bot.extensions.base.submissions.datetime', 'elo_bot.extensions.base.manage.datetime', 'elo_bot.extensions.base.private.datetime'), clock):
            await BASE_RENEW_MODAL_CALLBACK(self.cog, ctx, renewed_link=new_link)

        # Then
        self.assertEqual(base.link, new_link)
        mock_base_layout.find_one.assert_awaited_once_with({"link": old_link})
        self.assertEqual(base.expiration_date, 2_000_000_000 + 15 * 86400)
        base.save_changes.assert_awaited_once_with()
        self.bot.fetch_channel.assert_awaited_once_with(BASE_RENEWAL_LOGS_CHANNEL)
        self.renewal_logs_channel.send.assert_awaited_once()
        renewal_embed = self.renewal_logs_channel.send.await_args.kwargs["embed"].to_dict()
        self.assertEqual(renewal_embed["title"], "Barbarian Camp Base Renewed")
        self.assertEqual(renewal_embed["description"], f"[Base Link]({new_link})")
        self.assertEqual(renewal_embed["image"]["url"], "https://example.com/base.png")
        self.assertEqual(renewal_embed["color"], embed_color())
        self.assertEqual(ctx.message.embeds[0].fields[-1].value, f"[Base Link]({new_link})")

    async def test_renew_button_resolves_raw_and_hyperlinked_warning(self) -> None:
        old_link = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"
        for displayed_link in (old_link, f"[Base Link]({old_link})"):
            with self.subTest(displayed_link=displayed_link):
                ctx = MagicMock(spec=ipy.ComponentContext)
                ctx.message.embeds = [ipy.Embed(fields=[ipy.EmbedField(name="Base Link", value=displayed_link)])]
                ctx.send_modal = AsyncMock()
                with patch("elo_bot.extensions.base.manage.BaseLayout") as model:
                    model.link = ExpressionField("link")
                    model.find_one = AsyncMock(return_value=MagicMock())
                    callback = BaseCommands.base_renew_button.callback
                    await getattr(callback, "func", callback)(self.cog, ctx)
                model.find_one.assert_awaited_once_with({"link": old_link})
                ctx.send_modal.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
