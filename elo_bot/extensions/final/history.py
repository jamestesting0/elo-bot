import re

import interactions as ipy
from interactions import OptionType, SlashContext, slash_option

from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.config.presentation import FOOTER_TEXT
from elo_bot.discord.embeds import embed_color, footer_image
from elo_bot.core.finals import FinalError
from elo_bot.db.final_models import FinalMatchResult
from elo_bot.db.token_models import TokenWallet
from elo_bot.discord.members import member_guild
from elo_bot.features.finals.labels import final_member_names
from elo_bot.discord.pagination import InvalidPageError, build_page_button, build_page_jump_modal, parse_page_number
from elo_bot.extensions.final.checks import final_action
from elo_bot.extensions.final.groups import final
from elo_bot.features.finals.result_ui import result_embed


class History:
    final = final

    async def _history_view(self, ctx, season, user, page=1, *, edit=False):
        edition = await self._edition(ctx, season)
        query = {"final_id": str(edition.id)}
        if user != "-":
            wallet = await TokenWallet.find_one({"user_id": user})
            query["snapshot.contestants"] = {"$in": wallet.identity_claims if wallet else [user]}
            query["_id"] = {"$in": [node.result_id for node in edition.nodes.values() if node.result_id]}
        results = await FinalMatchResult.find(query).sort([("created_at", -1), ("version", -1)]).to_list()
        pages = max(1, len(results))
        page = parse_page_number(str(page), pages)
        if results:
            result = results[page - 1]
            guild = await member_guild(ctx)
            names = await final_member_names(edition, guild, result.snapshot.contestants)
            embed = result_embed(edition, result, names)
            if edition.nodes[result.node_id].result_id != result.id:
                embed.add_field(name="**History**", value="Replaced by a correction.")
        else:
            embed = ipy.Embed(title=f"**Season {edition.season_number} Final · History**", description="No results.", color=embed_color())
            embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
        owner = str(ctx.author.id)
        controls = [ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="◀️️", disabled=page == 1,
            custom_id=f"finalhist|{owner}|{edition.season_number}|{user}|{page - 1}"),
            build_page_button(page, pages, f"finalhistjump|{owner}|{edition.season_number}|{user}"),
            ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="▶️", disabled=page == pages,
            custom_id=f"finalhist|{owner}|{edition.season_number}|{user}|{page + 1}")]
        if edit:
            if isinstance(ctx, ipy.ModalContext):
                await ctx.edit(ctx.message, embed=embed, components=controls)
            else:
                await ctx.edit_origin(embed=embed, components=controls)
        else:
            await ctx.send(embed=embed, components=controls)

    @final.subcommand(sub_cmd_name="matches", sub_cmd_description="Show Final results and corrections")
    @slash_option(name="player", description="Player", opt_type=OptionType.USER)
    @slash_option(name="final_season", description="Final season", opt_type=OptionType.INTEGER, autocomplete=True)
    @slash_option(name="hidden", description="Only show this to you", opt_type=OptionType.BOOLEAN)
    @final_action
    async def final_matches(self, ctx: SlashContext, player: ipy.User = None, final_season: int = None,
                            hidden: bool = False):
        await ctx.defer(ephemeral=hidden)
        await self._history_view(ctx, final_season, str(player.id) if player else "-")

    @ipy.component_callback(re.compile(r"^finalhist\|.+$"))
    @final_action
    async def final_history_page(self, ctx: ipy.ComponentContext):
        _, owner, season, user, page = ctx.custom_id.split("|")
        try:
            if owner != str(ctx.author.id):
                raise FinalError("You cannot interact with another user's components.")
            await self._history_view(ctx, int(season), user, page, edit=True)
        except (FinalError, InvalidPageError) as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.component_callback(re.compile(r"^finalhistjump\|.+$"))
    @final_action
    async def final_history_jump(self, ctx: ipy.ComponentContext):
        if ctx.custom_id.split("|")[1] != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.", ephemeral=True)
            return
        await ctx.send_modal(build_page_jump_modal(ctx.custom_id.replace("finalhistjump", "finalhistmodal", 1)))

    @ipy.modal_callback(re.compile(r"^finalhistmodal\|.+$"))
    @final_action
    async def final_history_jump_modal(self, ctx: ipy.ModalContext, page: str):
        _, owner, season, user = ctx.custom_id.split("|")
        try:
            if owner != str(ctx.author.id):
                raise FinalError("You cannot interact with another user's components.")
            await self._history_view(ctx, int(season), user, page, edit=ctx.message is not None)
        except (FinalError, InvalidPageError) as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
