import math
import re

import interactions as ipy

from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elo_bot.config.ids import ELO_BLACKLIST_ROLE, GUILD_IDS, LVL1_ROLES
from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI, TOKEN_EMOJI
from elo_bot.discord.pagination import build_page_button, build_page_jump_modal, parse_page_number, InvalidPageError
from elo_bot.core.finals import FinalError
from elo_bot.core.token_items import ITEMS, TokenItem
from elo_bot.config.game import DISTRICT_INDEX
from elo_bot.features.tokens import items
from elo_bot.features.tokens.item_view import PAGES, item_panel
from elo_bot.features.tokens.awards import recover_awards
from elo_bot.features.tokens.stake_publication import recover_stakes
from elo_bot.features.tokens.queue_items import release_orphan_queue_items
from elo_bot.features.tokens.stats import betting_lines, betting_stats
from elo_bot.features.tokens.wallets import adjust_balance, ensure_wallet, wallet_summary
from elo_bot.db.token_models import ParticipantStake, TokenLedgerEntry
from elo_bot.discord.decorators import ignore_repeated_clicks
from elo_bot.discord.members import member_guild
from elo_bot.discord.mentions import member_names
from elo_bot.features.tokens.history import ledger_label, market_scopes, open_bet_lines

ITEM_CONTROL = re.compile(
    r"^tokenitems\|(page|pick|buy|district|back)\|\d+\|(shop|inventory)\|[01]\|[a-f0-9]{32}(\|[a-z_]+)?$"
)


class TokenCommands(ipy.Extension):
    tokens = ipy.SlashCommand(name="tokens", description="Your token wallet", scopes=GUILD_IDS)

    @ipy.Task.create(ipy.IntervalTrigger(minutes=1))
    async def recover_token_work(self):
        await recover_awards()
        await recover_stakes(self.bot)
        await items.release_expired_codes()
        await release_orphan_queue_items()

    @ipy.listen(ipy.events.Startup)
    async def start_token_recovery(self):
        self.recover_token_work.start()

    async def _wallet(self, ctx):
        if ctx.guild_id not in GUILD_IDS or ctx.author.has_role(ELO_BLACKLIST_ROLE):
            raise FinalError("Use token commands in the Final's server while eligible.")
        return await ensure_wallet(str(ctx.author.id))

    async def _items(self, ctx: ipy.SlashContext, mode: str, *, hidden: bool) -> None:
        await ctx.defer(ephemeral=hidden)
        try:
            embed, controls = await item_panel(await self._wallet(ctx), mode, 0, hidden=hidden)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await ctx.send(embed=embed, components=controls, ephemeral=hidden)

    @tokens.subcommand(sub_cmd_name="shop", sub_cmd_description="Browse and buy items")
    @ipy.slash_option(name="hidden", description="Make the bot response hidden or not", opt_type=ipy.OptionType.BOOLEAN)
    async def tokens_shop(self, ctx: ipy.SlashContext, hidden: bool = False) -> None:
        await self._items(ctx, "shop", hidden=hidden)

    @tokens.subcommand(sub_cmd_name="inventory", sub_cmd_description="View and use your items")
    @ipy.slash_option(name="hidden", description="Make the bot response hidden or not", opt_type=ipy.OptionType.BOOLEAN)
    async def tokens_inventory(self, ctx: ipy.SlashContext, hidden: bool = False) -> None:
        await self._items(ctx, "inventory", hidden=hidden)

    @ipy.component_callback(ITEM_CONTROL)
    async def token_item_control(self, ctx: ipy.ComponentContext) -> None:
        _, action, owner, mode, raw_page, key, *values = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} This view belongs to another player.", ephemeral=True)
            return
        await ctx.defer(edit_origin=True)
        try:
            wallet = await self._wallet(ctx)
            message = ctx.message
            hidden = message is not None and bool(message.flags & ipy.MessageFlags.EPHEMERAL)
            page = int(raw_page)
            if not 0 <= page < PAGES:
                raise FinalError("This page is unavailable.")
            content = ""
            code_message = None
            district = None
            if action == "pick":
                item = TokenItem(ctx.values[0])
                if mode == "shop":
                    listing = ITEMS[item]
                    embed = ipy.Embed(title=f"**{listing.name}**", description=f"{listing.price:,} {TOKEN_EMOJI}",
                                      color=COLOR, footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE))
                    await ctx.edit_origin(content="", embed=embed, components=[
                        ipy.Button(style=ipy.ButtonStyle.SUCCESS, label="Buy",
                                   custom_id=f"tokenitems|buy|{owner}|{mode}|{page}|{key}|{item}"),
                        ipy.Button(style=ipy.ButtonStyle.SECONDARY, label="Back",
                                   custom_id=f"tokenitems|back|{owner}|{mode}|{page}|{key}"),
                    ])
                    return
                if item is TokenItem.DISTRICT:
                    await ctx.edit_origin(content="", components=[
                        ipy.ActionRow(ipy.StringSelectMenu(
                            *(ipy.StringSelectOption(label=name, value=name) for name in DISTRICT_INDEX),
                            custom_id=f"tokenitems|district|{owner}|{mode}|{page}|{key}",
                            placeholder="Select a district",
                        )),
                        ipy.ActionRow(ipy.Button(style=ipy.ButtonStyle.SECONDARY, label="Back",
                                                custom_id=f"tokenitems|back|{owner}|{mode}|{page}|{key}")),
                    ])
                    return
            if action == "buy":
                item = TokenItem(values[0])
                await items.purchase(wallet, item, key)
                content = f"{SUCCESS_EMOJI} Purchased {ITEMS[item].name}."
            elif action in {"pick", "district"}:
                if action == "district":
                    district = ctx.values[0]
                    if district not in DISTRICT_INDEX:
                        raise FinalError("Select an available district.")
                    item = TokenItem.DISTRICT
                use = await items.activate(wallet, item, key, district=district)
                content = f"{SUCCESS_EMOJI} {ITEMS[item].name} {'ready' if use.state == 'armed' else use.state}."
                if item is TokenItem.CHALLENGE and use.state == "armed":
                    code_message = f"{SUCCESS_EMOJI} Use code `{use.target}` with `/challenge send`."
                    if hidden:
                        content, code_message = code_message, None
            embed, controls = await item_panel(await self._wallet(ctx), mode, page, hidden=hidden)
            await ctx.edit_origin(content=content, embed=embed, components=controls)
            if code_message is not None:
                await ctx.send(code_message, ephemeral=True)
        except FinalError as error:
            await ctx.client.http.post_followup(
                {"content": f"{ERROR_EMOJI} {error}", "flags": int(ipy.MessageFlags.EPHEMERAL)},
                ctx.client.app.id, ctx.token,
            )

    @tokens.subcommand(sub_cmd_name="balance", sub_cmd_description="Show Available Tokens, stakes, and debt")
    @ipy.slash_option(name="hidden", description="Make the bot response hidden or not", opt_type=ipy.OptionType.BOOLEAN)
    async def tokens_balance(self, ctx: ipy.SlashContext, hidden: bool = False):
        await ctx.defer(ephemeral=hidden)
        try:
            wallet = await self._wallet(ctx)
            available, _, debt = await wallet_summary(wallet)
            embed = ipy.Embed(title="**Tokens**", color=COLOR, description=f"**Available:** {available:,} {TOKEN_EMOJI}"
                              + (f"\n**Debt:** {debt:,} {TOKEN_EMOJI}" if debt else ""))
            embed.add_field(name="__**Bets**__", value=betting_lines((await betting_stats()).get(wallet.id)))
            lines = await open_bet_lines(wallet, await member_guild(ctx))
            while lines:
                value = lines.pop(0)
                while lines and len(value) + len(lines[0]) + 1 <= 1024:
                    value += "\n" + lines.pop(0)
                embed.add_field(name="__**Open Bets**__", value=value, inline=False)
            stakes = await ParticipantStake.find({"wallets": wallet.id, "state": "locked"}).to_list()
            if stakes:
                total = sum(stake.committed_amount(user) for stake in stakes
                            for user, wallet_id in zip(stake.users, stake.wallets or ()) if wallet_id == wallet.id)
                embed.add_field(name="**Match Stakes**", value=f"{total:,} {TOKEN_EMOJI}")
            embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
            await ctx.send(embed=embed, ephemeral=hidden)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    async def _history(self, ctx, page, *, edit=False, hidden=True):
        wallet = await self._wallet(ctx)
        entries = await TokenLedgerEntry.find({"wallet_id": wallet.id, "kind": {"$ne": "identity"}}).sort(
            [("created_at", -1), ("_id", -1)]).to_list()
        pages = max(1, math.ceil(len(entries) / 10))
        page = parse_page_number(str(page), pages)
        selected = entries[(page - 1) * 10:page * 10]
        scopes = await market_scopes({entry.market_id for entry in selected if entry.market_id})
        embed = ipy.Embed(title="**Token History**", color=COLOR, description="\n".join(
            f"`{ledger_label(entry)}` {entry.amount:+,} {TOKEN_EMOJI}"
            + (f" · {scopes[entry.market_id]}" if entry.market_id in scopes else "")
            + f" · {ipy.Timestamp.fromtimestamp(entry.created_at).format(ipy.TimestampStyles.RelativeTime)}"
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
            await ctx.send(embed=embed, components=controls, ephemeral=hidden)

    async def _adjust(self, ctx: ipy.SlashContext, player: ipy.User, amount: int, verb: str) -> None:
        await ctx.defer(ephemeral=True)
        try:
            if ctx.guild_id not in GUILD_IDS:
                raise FinalError("Use token commands in the Final's server.")
            wallet = await adjust_balance(str(player.id), amount, str(ctx.author.id), f"adjust:{ctx.id}")
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        name = (await member_names(await member_guild(ctx), [str(player.id)]))[str(player.id)]
        await ctx.send(f"{SUCCESS_EMOJI} {verb} {abs(amount):,} {TOKEN_EMOJI} {'to' if amount > 0 else 'from'} {name}. "
                       f"Available: {max(wallet.balance, 0):,} {TOKEN_EMOJI}", ephemeral=True)

    @tokens.subcommand(sub_cmd_name="add", sub_cmd_description="Add tokens to a player's wallet")
    @ipy.slash_option(name="player", description="The player", opt_type=ipy.OptionType.USER, required=True)
    @ipy.slash_option(name="amount", description="Tokens to add", opt_type=ipy.OptionType.INTEGER, required=True,
                      min_value=1, max_value=1_000_000)
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    async def tokens_add(self, ctx: ipy.SlashContext, player: ipy.User, amount: int):
        await self._adjust(ctx, player, amount, "Added")

    @tokens.subcommand(sub_cmd_name="remove", sub_cmd_description="Remove tokens from a player's wallet")
    @ipy.slash_option(name="player", description="The player", opt_type=ipy.OptionType.USER, required=True)
    @ipy.slash_option(name="amount", description="Tokens to remove", opt_type=ipy.OptionType.INTEGER, required=True,
                      min_value=1, max_value=1_000_000)
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    async def tokens_remove(self, ctx: ipy.SlashContext, player: ipy.User, amount: int):
        await self._adjust(ctx, player, -amount, "Removed")

    @tokens.subcommand(sub_cmd_name="history", sub_cmd_description="Show grants, bets, payouts, and corrections")
    @ipy.slash_option(name="hidden", description="Make the bot response hidden or not", opt_type=ipy.OptionType.BOOLEAN)
    async def tokens_history(self, ctx: ipy.SlashContext, hidden: bool = False):
        await ctx.defer(ephemeral=hidden)
        try:
            await self._history(ctx, 1, hidden=hidden)
        except (FinalError, InvalidPageError) as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.component_callback(re.compile(r"^tokenpage\|.+$"))
    @ignore_repeated_clicks
    async def token_page(self, ctx: ipy.ComponentContext):
        _, owner, page = ctx.custom_id.split("|")
        try:
            if owner != str(ctx.author.id):
                raise FinalError("This history belongs to another user.")
            await self._history(ctx, page, edit=True)
        except (FinalError, InvalidPageError) as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.component_callback(re.compile(r"^tokenjump\|.+$"))
    @ignore_repeated_clicks
    async def token_jump(self, ctx: ipy.ComponentContext):
        _, owner, pages = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} This history belongs to another user.", ephemeral=True)
            return
        await ctx.send_modal(build_page_jump_modal(f"tokenjumpmodal|{owner}|{pages}"))

    @ipy.modal_callback(re.compile(r"^tokenjumpmodal\|.+$"))
    @ignore_repeated_clicks
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
