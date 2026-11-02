import math
import re

import interactions as ipy

from common.constants import COLOR, ELO_BLACKLIST_ROLE, ERROR_EMOJI, FOOTER_IMAGE, FOOTER_TEXT, GUILD_IDS, TOKEN_EMOJI
from common.discord.pagination import build_page_button, build_page_jump_modal, parse_page_number, InvalidPageError
from common.finals.types import FinalError
from common.tokens.wallets import ensure_wallet, wallet_summary
from database_models.token_models import TokenLedgerEntry


class TokenCommands(ipy.Extension):
    tokens = ipy.SlashCommand(name="tokens", description="Your token wallet", scopes=GUILD_IDS)

    async def _wallet(self, ctx):
        if ctx.guild_id not in GUILD_IDS or ctx.author.has_role(ELO_BLACKLIST_ROLE):
            raise FinalError("Use token commands in the Final's server while eligible.")
        return await ensure_wallet(str(ctx.author.id))

    @tokens.subcommand(sub_cmd_name="balance", sub_cmd_description="Show Available Tokens, stakes, and debt")
    async def tokens_balance(self, ctx: ipy.SlashContext):
        await ctx.defer(ephemeral=True)
        try:
            wallet = await self._wallet(ctx)
            available, committed, debt = await wallet_summary(wallet)
            embed = ipy.Embed(title="**Tokens**", color=COLOR)
            embed.add_field(name="**Available**", value=f"{available:,} {TOKEN_EMOJI}")
            embed.add_field(name="**Committed**", value=f"{committed:,} {TOKEN_EMOJI}")
            if debt:
                embed.add_field(name="**Debt**", value=f"{debt:,} {TOKEN_EMOJI}")
            embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
            await ctx.send(embed=embed, ephemeral=True)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    async def _history(self, ctx, page, *, edit=False):
        wallet = await self._wallet(ctx)
        entries = await TokenLedgerEntry.find({"wallet_id": wallet.id}).sort([("created_at", -1), ("_id", -1)]).to_list()
        pages = max(1, math.ceil(len(entries) / 10))
        page = parse_page_number(str(page), pages)
        selected = entries[(page - 1) * 10:page * 10]
        embed = ipy.Embed(title="**Token History**", color=COLOR, description="\n".join(
            f"{entry.kind.title()}: {entry.amount:+,} {TOKEN_EMOJI}" + (f" · {entry.market_id.split(':')[1]}" if entry.market_id else "")
            for entry in selected) or "No token activity.")
        embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
        owner = str(ctx.author.id)
        controls = [ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="◀️️", custom_id=f"tokenpage|{owner}|{page - 1}", disabled=page == 1),
            build_page_button(page, pages, f"tokenjump|{owner}|{pages}"),
            ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="▶️", custom_id=f"tokenpage|{owner}|{page + 1}", disabled=page == pages)]
        if edit:
            if isinstance(ctx, ipy.ModalContext):
                await ctx.edit(ctx.message, embed=embed, components=controls)
            else:
                await ctx.edit_origin(embed=embed, components=controls)
        else:
            await ctx.send(embed=embed, components=controls, ephemeral=True)

    @tokens.subcommand(sub_cmd_name="history", sub_cmd_description="Show grants, bets, payouts, and corrections")
    async def tokens_history(self, ctx: ipy.SlashContext):
        await ctx.defer(ephemeral=True)
        try:
            await self._history(ctx, 1)
        except (FinalError, InvalidPageError) as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.component_callback(re.compile(r"^tokenpage\|.+$"))
    async def token_page(self, ctx: ipy.ComponentContext):
        _, owner, page = ctx.custom_id.split("|")
        try:
            if owner != str(ctx.author.id):
                raise FinalError("This history belongs to another user.")
            await self._history(ctx, page, edit=True)
        except (FinalError, InvalidPageError) as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.component_callback(re.compile(r"^tokenjump\|.+$"))
    async def token_jump(self, ctx: ipy.ComponentContext):
        _, owner, pages = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} This history belongs to another user.", ephemeral=True)
            return
        await ctx.send_modal(build_page_jump_modal(f"tokenjumpmodal|{owner}|{pages}"))

    @ipy.modal_callback(re.compile(r"^tokenjumpmodal\|.+$"))
    async def token_jump_modal(self, ctx: ipy.ModalContext, page: str):
        _, owner, _ = ctx.custom_id.split("|")
        try:
            if owner != str(ctx.author.id):
                raise FinalError("This history belongs to another user.")
            await self._history(ctx, page, edit=ctx.message is not None)
        except (FinalError, InvalidPageError) as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)


def setup(bot):
    TokenCommands(bot)
