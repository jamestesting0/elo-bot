import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy

import elo_bot.extensions.help as help_module
from elo_bot.extensions.help import HelpCommands
from elo_bot.extensions.raid import RaidCommands


def slash_command(
    name: str,
    description: str,
    group_name: str | None = None,
    subcommand_name: str | None = None,
) -> ipy.SlashCommand:
    return ipy.SlashCommand(
        name=name,
        description=description,
        group_name=group_name,
        group_description="Group commands",
        sub_cmd_name=subcommand_name,
        sub_cmd_description=description,
    )


def command_roster() -> list[ipy.SlashCommand | ipy.ContextMenu]:
    return [
        slash_command("leaderboard", "Show rankings"),
        slash_command("help", "Show the command manual"),
        slash_command("match", "Start a match"),
        slash_command("match", "Store a result", "result", "store"),
        slash_command("queue", ""),
        slash_command("match", "Store a result", "result", "store"),
        RaidCommands.delete_post_message_command,
    ]


def help_context() -> MagicMock:
    context = MagicMock(spec=ipy.SlashContext)
    context.author.id = 41
    context.send = AsyncMock()
    return context


def component_context(user_id: int, category: str) -> MagicMock:
    context = MagicMock(spec=ipy.ComponentContext)
    context.author.id = user_id
    context.values = [category]
    context.message = SimpleNamespace(interaction=SimpleNamespace(user=SimpleNamespace(id=41)))
    context.edit_origin = AsyncMock()
    context.send = AsyncMock()
    return context


def menu_from(context: MagicMock) -> ipy.StringSelectMenu:
    return context.send.await_args.kwargs["components"]


class TestHelpCommands(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = MagicMock()
        self.bot.application_commands = command_roster()
        self.cog = HelpCommands(self.bot)

    async def test_help_renders_match_first_and_every_source_path_once(self) -> None:
        # Given: slash commands from several categories, duplicate scopes, and a context menu
        context = help_context()

        # When: the command manual is opened and every emitted category is selected
        await self.cog.help.callback(context)
        self.assertTrue(context.send.await_args.kwargs["ephemeral"])
        embed = context.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "Match")
        self.assertEqual(embed.color, help_module.COLOR)
        self.assertEqual(
            [option.value for option in menu_from(context).options if option.default], ["Match"],
        )
        categories = [option.value for option in menu_from(context).options]
        pages = []
        for category in categories:
            selection = component_context(41, category)
            await self.cog.help_category_select.callback(selection)
            embed = selection.edit_origin.await_args.kwargs["embed"]
            self.assertEqual(embed.title, category)
            pages.append(embed)

        # Then: Match is first and every source command path appears on exactly one category page
        self.assertEqual(categories[0], "Match")
        self.assertEqual(categories, ["Match", "Help", "Leaderboard", "Queue", "Context Menus"])
        rendered_paths = [
            line.split("`")[1]
            for embed in pages
            for line in embed.description.splitlines()
        ]
        self.assertCountEqual(
            rendered_paths,
            ["/match", "/match result store", "/help", "/leaderboard", "/queue", "Message: Delete Post"],
        )

    async def test_registered_selector_replaces_the_original_category_page(self) -> None:
        # Given: the manual's original author selects Leaderboard
        context = component_context(41, "Leaderboard")

        # When: the registered component callback is invoked directly
        await self.cog.help_category_select.callback(context)

        # Then: the original response is edited with the selected page and one default option
        context.edit_origin.assert_awaited_once()
        self.assertEqual(context.edit_origin.await_args.kwargs["embed"].title, "Leaderboard")
        options = context.edit_origin.await_args.kwargs["components"].options
        self.assertEqual([option.value for option in options if option.default], ["Leaderboard"])
        context.send.assert_not_awaited()

    async def test_selector_rejects_a_different_user_without_editing(self) -> None:
        # Given: a different Discord user selects an existing help category
        context = component_context(99, "Queue")

        # When: the fixed component callback receives the foreign interaction
        await self.cog.help_category_select.callback(context)

        # Then: the denial is ephemeral and the manual remains unchanged
        context.send.assert_awaited_once()
        self.assertTrue(context.send.await_args.kwargs["ephemeral"])
        context.edit_origin.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
