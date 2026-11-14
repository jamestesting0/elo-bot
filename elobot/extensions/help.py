from inspect import getdoc
from typing import Final, assert_never

import interactions as ipy

from elobot.config.presentation import COLOR


FALLBACK_DESCRIPTION: Final = "No description available."
CONTEXT_MENUS: Final = "Context Menus"


class HelpCommands(ipy.Extension):
    def __init__(self, bot: ipy.Client) -> None:
        self.bot = bot

    def _categories(self) -> dict[str, dict[str, str]]:
        categories: dict[str, dict[str, str]] = {}
        for command in self.bot.application_commands:
            match command:
                case ipy.SlashCommand():
                    root = str(command.name)
                    category = root.replace("-", " ").title()
                    path = f"/{command.resolved_name}"
                    description = str(command.sub_cmd_description if command.is_subcommand else command.description)
                    categories.setdefault(category, {}).setdefault(
                        path, description if description not in {"", "None", "No Description Set"} else FALLBACK_DESCRIPTION
                    )
                case ipy.ContextMenu():
                    category = CONTEXT_MENUS
                    path = f"{command.type.name.title()}: {command.name}"
                    callback = getattr(command.callback, "func", command.callback)
                    description = getdoc(callback) or FALLBACK_DESCRIPTION
                    categories.setdefault(category, {}).setdefault(path, description)
                case unexpected:
                    assert_never(unexpected)

        ordered = sorted(category for category in categories if category not in {"Match", CONTEXT_MENUS})
        if "Match" in categories:
            ordered.insert(0, "Match")
        if CONTEXT_MENUS in categories:
            ordered.append(CONTEXT_MENUS)
        return {category: categories[category] for category in ordered}

    @staticmethod
    def _page(category: str, commands: dict[str, str]) -> ipy.Embed:
        description = "\n".join(f"`{path}` — {command_description}" for path, command_description in commands.items())
        return ipy.Embed(title=category, description=description, color=COLOR)

    @staticmethod
    def _menu(categories: dict[str, dict[str, str]], selected: str) -> ipy.StringSelectMenu:
        return ipy.StringSelectMenu(
            *(ipy.StringSelectOption(label=category, value=category, default=category == selected)
              for category in categories),
            placeholder="Select a command category",
            custom_id="help_category_select",
        )

    @ipy.slash_command(name="help", description="Show the command manual")
    async def help(self, ctx: ipy.SlashContext) -> None:
        categories = self._categories()
        selected = "Match" if "Match" in categories else next(iter(categories))
        await ctx.send(
            embed=self._page(selected, categories[selected]),
            components=self._menu(categories, selected),
            ephemeral=True,
        )

    @ipy.component_callback("help_category_select")
    async def help_category_select(self, ctx: ipy.ComponentContext) -> None:
        if str(ctx.message.interaction.user.id) != str(ctx.author.id):
            await ctx.send("You cannot use another user's help menu.", ephemeral=True)
            return

        categories = self._categories()
        selected = ctx.values[0]
        if selected not in categories:
            await ctx.send("That help category is unavailable.", ephemeral=True)
            return

        await ctx.edit_origin(
            embed=self._page(selected, categories[selected]),
            components=self._menu(categories, selected),
        )
