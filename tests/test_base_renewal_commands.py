import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from cogs.base_commands import BaseCommands
from elobot.config.ids import BASE_RENEWAL_LOGS_CHANNEL
from elobot.config.presentation import COLOR


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
            for command in vars(BaseCommands).values()
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

    @patch("cogs.base_commands.utils.misc_utils.disable_components", return_value=[])
    @patch("cogs.base_commands.BaseLayout")
    async def test_renewal_modal_logs_success(self, mock_base_layout, _mock_disable_components) -> None:
        # Given
        old_link = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"
        new_link = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3ABBBBB"
        base = MagicMock(district="Barbarian Camp", link=old_link, image_url="https://example.com/base.png")
        base.save_changes = AsyncMock()
        mock_base_layout.find_one = AsyncMock(return_value=base)
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.message.embeds = [ipy.Embed(fields=[ipy.EmbedField(name="Base Link", value=old_link)])]
        ctx.message.components = []
        ctx.message.edit = AsyncMock()

        # When
        clock = MagicMock()
        clock.now.return_value.timestamp.return_value = 2_000_000_000
        with patch("cogs.base_commands.datetime", clock):
            await BASE_RENEW_MODAL_CALLBACK(self.cog, ctx, renewed_link=new_link)

        # Then
        self.assertEqual(base.link, new_link)
        self.assertEqual(base.expiration_date, 2_000_000_000 + 15 * 86400)
        base.save_changes.assert_awaited_once_with()
        self.bot.fetch_channel.assert_awaited_once_with(BASE_RENEWAL_LOGS_CHANNEL)
        self.renewal_logs_channel.send.assert_awaited_once()
        renewal_embed = self.renewal_logs_channel.send.await_args.kwargs["embed"].to_dict()
        self.assertEqual(renewal_embed["title"], "Barbarian Camp Base Renewed")
        self.assertEqual(renewal_embed["description"], new_link)
        self.assertEqual(renewal_embed["image"]["url"], "https://example.com/base.png")
        self.assertEqual(renewal_embed["color"], COLOR)


if __name__ == "__main__":
    unittest.main()
