import re

import interactions as ipy
from beanie import PydanticObjectId

import elo_bot.features.hosting.sequential.service as sequential_service
from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.config.game import HOST_CLAN_CHOICES
from elo_bot.config.ids import LVL2_ROLES
from elo_bot.db import Match
from elo_bot.extensions.match.groups import helper_group
from elo_bot.discord.pagination import InvalidPageError, build_page_jump_modal, parse_page_number
from elo_bot.features.hosting.sequential.ui import (
    CANCEL_PREFIX,
    CONTINUE_PREFIX,
    REPLAY_PREFIX,
    LIST_JUMP_PREFIX,
    LIST_LEFT_PREFIX,
    LIST_PAGE_PREFIX,
    LIST_RIGHT_PREFIX,
)

PLAYER_CHOICES = [
    ipy.SlashCommandChoice(name="Player 1", value=0),
    ipy.SlashCommandChoice(name="Player 2", value=1),
]
LIST_SCOPE = r"(open|attention|uncertain|failed|all)\|[^|]+"
STATE_CHOICES = [
    ipy.SlashCommandChoice(name=name, value=value)
    for name, value in (("Open", "open"), ("Needs attention", "attention"), ("Unconfirmed", "uncertain"),
                        ("Failed or cancelled", "failed"), ("All", "all"))
]


async def owns_list(ctx: ipy.ComponentContext | ipy.ModalContext) -> bool:
    message = ctx.message
    if message is None or message.interaction is None or message.interaction.user is None:
        await ctx.send(f"{ERROR_EMOJI} The paginated message is no longer available.", ephemeral=True)
        return False
    owner = getattr(message.interaction.user, "id", message.interaction.user)
    if str(ctx.author.id) != str(owner):
        await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.", ephemeral=True)
        return False
    return True


async def thread_match(ctx: ipy.SlashContext) -> Match | None:
    match = await Match.find_one(Match.thread == str(ctx.channel.id))
    if match is None:
        await ctx.send(f"{ERROR_EMOJI} Use this command in a match thread.", ephemeral=True)
    return match


class MatchSequentialCommands:
    @helper_group.subcommand(sub_cmd_name="status", sub_cmd_description="Show this match's Helper sequential run")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(name="player", description="The war side to show; leave blank for the latest run",
                      opt_type=ipy.OptionType.INTEGER, choices=PLAYER_CHOICES)
    @ipy.slash_option(name="export", description="Attach the full debug file", opt_type=ipy.OptionType.BOOLEAN)
    @ipy.slash_option(name="hidden", description="Only show the result to you", opt_type=ipy.OptionType.BOOLEAN)
    async def match_helper_status(self, ctx: ipy.SlashContext, player: int | None = None, export: bool = False,
                                  hidden: bool = True) -> None:
        await ctx.defer(ephemeral=hidden)
        if match := await thread_match(ctx):
            side = sequential_service.match_side(match, player) if player is not None or not match.is_war else None
            await sequential_service.status(self.bot, ctx, match, side, export=export, hidden=hidden)

    @helper_group.subcommand(sub_cmd_name="list", sub_cmd_description="List Helper sequential runs")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(name="state", description="Which runs to list", opt_type=ipy.OptionType.STRING,
                      choices=STATE_CHOICES)
    @ipy.slash_option(name="clan", description="Only runs in this clan", opt_type=ipy.OptionType.STRING,
                      choices=HOST_CLAN_CHOICES)
    @ipy.slash_option(name="hidden", description="Only show the result to you", opt_type=ipy.OptionType.BOOLEAN)
    async def match_helper_list(self, ctx: ipy.SlashContext, state: str = "open", clan: str | None = None,
                                hidden: bool = True) -> None:
        await ctx.defer(ephemeral=hidden)
        embed, components, _ = await sequential_service.list_page(state, clan, 0)
        await ctx.send(embed=embed, components=components, ephemeral=hidden)

    @ipy.component_callback(re.compile(rf"^({LIST_LEFT_PREFIX}|{LIST_RIGHT_PREFIX})\|\d+\|{LIST_SCOPE}$"))
    async def helper_runs_arrow(self, ctx: ipy.ComponentContext) -> None:
        if not await owns_list(ctx):
            return
        _, page, state, clan = ctx.custom_id.split("|")
        embed, components, _ = await sequential_service.list_page(state, None if clan == "-" else clan, int(page))
        await ctx.edit_origin(embed=embed, components=components)

    @ipy.component_callback(re.compile(rf"^{LIST_PAGE_PREFIX}\|{LIST_SCOPE}$"))
    async def helper_runs_page(self, ctx: ipy.ComponentContext) -> None:
        if not await owns_list(ctx):
            return
        scope = ctx.custom_id.split("|", 1)[1]
        await ctx.send_modal(build_page_jump_modal(f"{LIST_JUMP_PREFIX}|{scope}"))

    @ipy.modal_callback(re.compile(rf"^{LIST_JUMP_PREFIX}\|{LIST_SCOPE}$"))
    async def helper_runs_jump(self, ctx: ipy.ModalContext, **response) -> None:
        if not await owns_list(ctx):
            return
        _, state, clan = ctx.custom_id.split("|")
        clan_tag = None if clan == "-" else clan
        _, _, total_pages = await sequential_service.list_page(state, clan_tag, 0)
        try:
            page = parse_page_number(response["page"], total_pages)
        except InvalidPageError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        embed, components, _ = await sequential_service.list_page(state, clan_tag, page - 1)
        await ctx.edit(ctx.message, embed=embed, components=components)

    @helper_group.subcommand(sub_cmd_name="recover",
                             sub_cmd_description="Resume, adopt or continue this match's Helper sequential run")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(name="player", description="The war side to recover", opt_type=ipy.OptionType.INTEGER,
                      choices=PLAYER_CHOICES)
    async def match_helper_recover(self, ctx: ipy.SlashContext, player: int | None = None) -> None:
        await ctx.defer(ephemeral=True)
        match = await thread_match(ctx)
        if match is None:
            return
        side = sequential_service.match_side(match, player)
        if side is None:
            await ctx.send(f"{ERROR_EMOJI} Choose the player side for a war-style match.", ephemeral=True)
            return
        await sequential_service.recover(self.bot, ctx, match, side)

    @ipy.component_callback(re.compile(rf"^{CANCEL_PREFIX}\|[0-9a-f]{{24}}$"))
    async def helper_sequential_cancel(self, ctx: ipy.ComponentContext) -> None:
        await sequential_service.cancel_run(self.bot, ctx, PydanticObjectId(ctx.custom_id.split("|")[1]))

    @ipy.component_callback(re.compile(rf"^{CONTINUE_PREFIX}\|[0-9a-f]{{24}}$"))
    async def helper_sequential_continue(self, ctx: ipy.ComponentContext) -> None:
        await sequential_service.continue_from_button(self.bot, ctx, PydanticObjectId(ctx.custom_id.split("|")[1]))

    @ipy.component_callback(re.compile(rf"^{REPLAY_PREFIX}\|[0-9a-f]{{24}}$"))
    async def helper_sequential_replay(self, ctx: ipy.ComponentContext) -> None:
        await sequential_service.replay_from_button(self.bot, ctx, PydanticObjectId(ctx.custom_id.split("|")[1]))
