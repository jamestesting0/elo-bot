import math
import re

import interactions as ipy

from elo_bot.config.presentation import FOOTER_TEXT
from elo_bot.discord.embeds import embed_color, footer_image, profile_author
from elo_bot.config.ids import ELO_BLACKLIST_ROLE, GUILD_IDS, LVL1_ROLES
from elo_bot.config.emojis import ERROR_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI, TOKEN_EMOJI
from elo_bot.discord.pagination import build_page_button, build_page_jump_modal, parse_page_number, InvalidPageError
from elo_bot.core.finals import FinalError
from elo_bot.core.token_items import ITEMS, TokenItem
from elo_bot.config.game import DISTRICT_INDEX
from elo_bot.features.tokens import items
from elo_bot.features.tokens.exp import recover_exp, redeem_exp
from elo_bot.features.tokens.item_view import PAGES, active_item_panel, challenge_code_receipt, format_item_price, item_panel
from elo_bot.features.tokens.item_management import ItemAdjustment, adjust_items, deactivate_item
from elo_bot.features.tokens.awards import recover_awards
from elo_bot.features.tokens.stake_publication import recover_stakes
from elo_bot.features.tokens.queue_items import release_orphan_queue_items
from elo_bot.features.tokens.stats import betting_lines, betting_stats
from elo_bot.features.tokens.wallets import adjust_balance, ensure_wallet, wallet_summary
from elo_bot.db.token_models import ParticipantStake, TokenLedgerEntry, TokenWallet
from elo_bot.discord.decorators import ignore_repeated_clicks
from elo_bot.discord.members import fetch_member_otherwise_user, member_guild
from elo_bot.discord.mentions import member_names
from elo_bot.features.tokens.history import ledger_label, market_scopes, open_bet_lines

ITEM_CONTROL = re.compile(
    r"^tokenitems\|(page|pick|buy|district|back)\|\d+\|(shop|inventory)\|[01]\|[a-f0-9]{32}(\|(?:[a-z_]+|\d+))?$"
)


class TokenCommands(ipy.Extension):
    tokens = ipy.SlashCommand(name="tokens", description="Your token wallet", scopes=GUILD_IDS)

    @ipy.Task.create(ipy.IntervalTrigger(minutes=1))
    async def recover_token_work(self):
        await recover_awards()
        await recover_stakes(self.bot)
        await items.release_expired_codes()
        await release_orphan_queue_items()
        await recover_exp()

    @ipy.listen(ipy.events.Startup)
    async def start_token_recovery(self):
        self.recover_token_work.start()

    async def _wallet(self, ctx: ipy.InteractionContext, *, user_id: str | None = None) -> TokenWallet:
        if ctx.guild_id not in GUILD_IDS or ctx.author.has_role(ELO_BLACKLIST_ROLE):
            raise FinalError("Use token commands in the Final's server while eligible.")
        owner = str(ctx.author.id)
        if user_id is None or user_id == owner:
            return await ensure_wallet(owner)
        wallet = await TokenWallet.find_one({"user_id": user_id})
        if wallet is None:
            raise FinalError("This player has no token wallet.")
        return wallet

    async def _wallet_user(self, ctx: ipy.InteractionContext, wallet: TokenWallet) -> ipy.User | ipy.Member | None:
        if wallet.user_id == str(ctx.author.id):
            return ctx.author
        return await fetch_member_otherwise_user(await member_guild(ctx), ctx.client, wallet.user_id)

    async def _item_view(self, ctx: ipy.InteractionContext, wallet: TokenWallet, mode: str, page: int,
                         *, hidden: bool, user: ipy.User | ipy.Member | None = None) -> tuple[ipy.Embed, list[ipy.ActionRow]]:
        embed, controls = await item_panel(wallet, mode, page, hidden=hidden, viewer_id=str(ctx.author.id))
        if mode == "inventory":
            embed.author = profile_author(user or await self._wallet_user(ctx, wallet))
        return embed, controls

    async def _items(self, ctx: ipy.SlashContext, mode: str, *, hidden: bool,
                     user: ipy.User | ipy.Member | None = None) -> None:
        await ctx.defer(ephemeral=hidden)
        try:
            wallet = await self._wallet(ctx, user_id=str(user.id) if user is not None else None)
            embed, controls = await self._item_view(ctx, wallet, mode, 0, hidden=hidden, user=user)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await ctx.send(embed=embed, components=controls, ephemeral=hidden)

    @tokens.subcommand(sub_cmd_name="shop", sub_cmd_description="Browse and buy items")
    @ipy.slash_option(name="hidden", description="Make the bot response hidden or not", opt_type=ipy.OptionType.BOOLEAN)
    async def tokens_shop(self, ctx: ipy.SlashContext, hidden: bool = False) -> None:
        await self._items(ctx, "shop", hidden=hidden)

    @tokens.subcommand(sub_cmd_name="buy", sub_cmd_description="Buy one or more copies of an item")
    @ipy.slash_option(name="item", description="The item", opt_type=ipy.OptionType.STRING, required=True,
                      choices=[ipy.SlashCommandChoice(name=listing.name, value=item.value) for item, listing in ITEMS.items()])
    @ipy.slash_option(name="amount", description="Number of items to buy", opt_type=ipy.OptionType.INTEGER,
                      min_value=1, max_value=1_000_000)
    @ipy.slash_option(name="hidden", description="Make the bot response hidden or not", opt_type=ipy.OptionType.BOOLEAN)
    async def tokens_buy(self, ctx: ipy.SlashContext, item: str, amount: int = 1, hidden: bool = False) -> None:
        await ctx.defer(ephemeral=hidden)
        try:
            selected = TokenItem(item)
            await items.purchase(await self._wallet(ctx), selected, f"command:{ctx.id}", amount=amount)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        listing = ITEMS[selected]
        await ctx.send(f"{SUCCESS_EMOJI} Purchased `{amount:,}` × {listing.name} "
                       f"for {listing.price * amount:,} {TOKEN_EMOJI}.", ephemeral=hidden)

    @tokens.subcommand(sub_cmd_name="use-item", sub_cmd_description="Activate one or more owned items")
    @ipy.slash_option(name="item", description="The item", opt_type=ipy.OptionType.STRING, required=True,
                      choices=[ipy.SlashCommandChoice(name=listing.name, value=item.value) for item, listing in ITEMS.items()])
    @ipy.slash_option(name="amount", description="Number of items to activate", opt_type=ipy.OptionType.INTEGER,
                      min_value=1, max_value=100)
    @ipy.slash_option(name="district", description="District for District Choice", opt_type=ipy.OptionType.STRING,
                      choices=[ipy.SlashCommandChoice(name=name, value=name) for name in DISTRICT_INDEX])
    @ipy.slash_option(name="hidden", description="Make the bot response hidden or not", opt_type=ipy.OptionType.BOOLEAN)
    async def tokens_use(self, ctx: ipy.SlashContext, item: str, amount: int = 1, district: str | None = None,
                         hidden: bool = False) -> None:
        selected = TokenItem(item)
        await ctx.defer(ephemeral=hidden or selected is TokenItem.CHALLENGE)
        try:
            uses = await items.activate_many(await self._wallet(ctx), selected, f"command-use:{ctx.id}",
                                             amount=amount, district=district)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        if selected is TokenItem.CHALLENGE:
            await ctx.send(challenge_code_receipt(uses), ephemeral=True)
            return
        verb = "Queued" if selected is TokenItem.EXP else "Activated"
        await ctx.send(f"{SUCCESS_EMOJI} {verb} `{amount:,}` × {ITEMS[selected].name}. Items will be used in order.",
                       ephemeral=hidden)

    @tokens.subcommand(sub_cmd_name="inventory", sub_cmd_description="View a player's items or use your own")
    @ipy.slash_option(name="user", description="Player whose token inventory to show", opt_type=ipy.OptionType.USER)
    @ipy.slash_option(name="hidden", description="Make the bot response hidden or not", opt_type=ipy.OptionType.BOOLEAN)
    async def tokens_inventory(self, ctx: ipy.SlashContext, hidden: bool = False,
                               user: ipy.User | ipy.Member | None = None) -> None:
        await self._items(ctx, "inventory", hidden=hidden, user=user)

    @tokens.subcommand(sub_cmd_name="items", sub_cmd_description="View and disable your activated items")
    @ipy.slash_option(name="hidden", description="Make the bot response hidden or not", opt_type=ipy.OptionType.BOOLEAN)
    async def tokens_items(self, ctx: ipy.SlashContext, hidden: bool = False) -> None:
        await ctx.defer(ephemeral=hidden)
        try:
            embed, controls = await active_item_panel(await self._wallet(ctx), hidden=hidden)
            embed.author = profile_author(ctx.author)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await ctx.send(embed=embed, components=controls, ephemeral=hidden)

    @ipy.component_callback(re.compile(r"^tokenactive\|(disable|refresh|page)\|\d+(\|\d+)?$"))
    async def token_active_control(self, ctx: ipy.ComponentContext) -> None:
        _, action, owner, *raw_page = ctx.custom_id.split("|")
        page = int(raw_page[0]) if raw_page else 0
        if owner != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} This view belongs to another player.", ephemeral=True)
            return
        await ctx.defer(edit_origin=True)
        try:
            wallet = await self._wallet(ctx)
            content = ""
            if action == "disable":
                try:
                    item = await deactivate_item(wallet, ctx.values[0])
                    content = f"{SUCCESS_EMOJI} Disabled {ITEMS[item].name}. Returned to inventory."
                except FinalError as error:
                    content = f"{ERROR_EMOJI} {error}"
            hidden = ctx.message is not None and bool(ctx.message.flags & ipy.MessageFlags.EPHEMERAL)
            embed, controls = await active_item_panel(wallet, hidden=hidden, page=page)
            embed.author = profile_author(ctx.author)
            await ctx.edit_origin(content=content, embed=embed, components=controls)
        except FinalError as error:
            await ctx.client.http.post_followup(
                {"content": f"{ERROR_EMOJI} {error}", "flags": int(ipy.MessageFlags.EPHEMERAL)},
                ctx.client.app.id, ctx.token)

    @ipy.component_callback(ITEM_CONTROL)
    async def token_item_control(self, ctx: ipy.ComponentContext) -> None:
        _, action, owner, mode, raw_page, key, *values = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} This view belongs to another player.", ephemeral=True)
            return
        await ctx.defer(edit_origin=True)
        try:
            message = ctx.message
            hidden = message is not None and bool(message.flags & ipy.MessageFlags.EPHEMERAL)
            page = int(raw_page)
            if not 0 <= page < PAGES:
                raise FinalError("This page is unavailable.")
            if action == "page":
                wallet = await self._wallet(ctx, user_id=values[0] if values else None)
                embed, controls = await self._item_view(ctx, wallet, mode, page, hidden=hidden)
                await ctx.edit_origin(content="", embed=embed, components=controls)
                return
            wallet = await self._wallet(ctx)
            content = ""
            district = None
            if action == "pick":
                item = TokenItem(ctx.values[0])
                if mode == "shop":
                    listing = ITEMS[item]
                    embed = ipy.Embed(title=f"**{listing.name}**", description=format_item_price(listing.price),
                                      color=embed_color(), footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=footer_image()))
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
                listing = ITEMS[item]
                if not message or not message.embeds or message.embeds[0].description != format_item_price(listing.price):
                    content = f"{ERROR_EMOJI} Price changed. Select the item again to review the current price."
                else:
                    await items.purchase(wallet, item, key)
                    content = f"{SUCCESS_EMOJI} Purchased {listing.name}."
            elif action in {"pick", "district"}:
                if action == "district":
                    district = ctx.values[0]
                    if district not in DISTRICT_INDEX:
                        raise FinalError("Select an available district.")
                    item = TokenItem.DISTRICT
                if item is TokenItem.EXP:
                    await ctx.edit_origin(content=f"{LOADING_EMOJI} Granting 3,000 server EXP...", components=[])
                    try:
                        use = await redeem_exp(wallet, key)
                    except FinalError as error:
                        embed, controls = await self._item_view(ctx, await self._wallet(ctx), mode, page, hidden=hidden)
                        await ctx.edit(ctx.message, content=f"{ERROR_EMOJI} {error}", embed=embed, components=controls)
                        return
                    content = (f"{SUCCESS_EMOJI} EXP Potion queued." if use.state == "queued"
                               else f"{SUCCESS_EMOJI} Granted 3,000 server EXP.")
                else:
                    use = await items.activate(wallet, item, key, district=district)
                    if item is TokenItem.CHALLENGE:
                        await ctx.client.http.post_followup(
                            {"content": challenge_code_receipt([use]), "flags": int(ipy.MessageFlags.EPHEMERAL)},
                            ctx.client.app.id, ctx.token)
                        content = f"{SUCCESS_EMOJI} Used {ITEMS[item].name}."
                    else:
                        content = f"{SUCCESS_EMOJI} {ITEMS[item].name} {'ready' if use.state == 'armed' else use.state}."
            embed, controls = await self._item_view(ctx, await self._wallet(ctx), mode, page, hidden=hidden)
            if ctx.responded:
                await ctx.edit(ctx.message, content=content, embed=embed, components=controls)
            else:
                await ctx.edit_origin(content=content, embed=embed, components=controls)
        except FinalError as error:
            await ctx.client.http.post_followup(
                {"content": f"{ERROR_EMOJI} {error}", "flags": int(ipy.MessageFlags.EPHEMERAL)},
                ctx.client.app.id, ctx.token,
            )

    @tokens.subcommand(sub_cmd_name="balance", sub_cmd_description="Show Available Tokens, stakes, and debt")
    @ipy.slash_option(name="user", description="Player whose token balance to show", opt_type=ipy.OptionType.USER)
    @ipy.slash_option(name="hidden", description="Make the bot response hidden or not", opt_type=ipy.OptionType.BOOLEAN)
    async def tokens_balance(self, ctx: ipy.SlashContext, hidden: bool = False,
                             user: ipy.User | ipy.Member | None = None) -> None:
        await ctx.defer(ephemeral=hidden)
        try:
            wallet = await self._wallet(ctx, user_id=str(user.id) if user is not None else None)
            available, _, debt = await wallet_summary(wallet)
            subject = user or ctx.author
            embed = ipy.Embed(title="**Tokens**", color=embed_color(), author=profile_author(subject),
                              description=f"**Available:** {available:,} {TOKEN_EMOJI}"
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
            embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
            await ctx.send(embed=embed, ephemeral=hidden)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    async def _history(self, ctx: ipy.SlashContext | ipy.ComponentContext | ipy.ModalContext, page: int | str,
                       *, user_id: str | None = None, edit: bool = False, hidden: bool = True) -> None:
        wallet = await self._wallet(ctx, user_id=user_id)
        entries = await TokenLedgerEntry.find({"wallet_id": wallet.id, "kind": {"$ne": "identity"}}).sort(
            [("created_at", -1), ("_id", -1)]).to_list()
        pages = max(1, math.ceil(len(entries) / 10))
        page = parse_page_number(str(page), pages)
        selected = entries[(page - 1) * 10:page * 10]
        scopes = await market_scopes({entry.market_id for entry in selected if entry.market_id})
        subject = await self._wallet_user(ctx, wallet)
        embed = ipy.Embed(title="**Token History**", color=embed_color(), author=profile_author(subject), description="\n".join(
            f"`{ledger_label(entry)}`" + ("" if entry.kind == "item_adjust" else f" {entry.amount:+,} {TOKEN_EMOJI}")
            + (f" · {scopes[entry.market_id]}" if entry.market_id in scopes else "")
            + f" · {ipy.Timestamp.fromtimestamp(entry.created_at).format(ipy.TimestampStyles.RelativeTime)}"
            for entry in selected) or "No token activity.")
        embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
        owner = str(ctx.author.id)
        target_suffix = f"|{wallet.user_id}" if wallet.user_id != owner else ""
        controls = [
            ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="◀️️", disabled=page == 1,
                       custom_id=f"tokenpage|{owner}|{page - 1}{target_suffix}"),
            build_page_button(page, pages, f"tokenjump|{owner}|{pages}{target_suffix}"),
            ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="▶️", disabled=page == pages,
                       custom_id=f"tokenpage|{owner}|{page + 1}{target_suffix}"),
        ]
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

    async def _adjust_item(self, ctx: ipy.SlashContext, change: ItemAdjustment) -> None:
        await ctx.defer(ephemeral=True)
        try:
            if ctx.guild_id not in GUILD_IDS:
                raise FinalError("Use token commands in the Final's server.")
            if not any(ctx.author.has_role(role) for role in LVL1_ROLES):
                raise FinalError("You do not have permission to run this command!")
            wallet = await adjust_items(change)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        verb, direction = ("Gave", "to") if change.quantity > 0 else ("Removed", "from")
        await ctx.send(f"{SUCCESS_EMOJI} {verb} {abs(change.quantity):,} × {ITEMS[change.item].name} "
                       f"{direction} <@{change.user_id}>. In inventory: {wallet.inventory.get(change.item, 0):,}.",
                       ephemeral=True)

    @tokens.subcommand(sub_cmd_name="give-item", sub_cmd_description="Give inventory items to a player")
    @ipy.slash_option(name="player", description="The player", opt_type=ipy.OptionType.USER, required=True)
    @ipy.slash_option(name="item", description="The item", opt_type=ipy.OptionType.STRING, required=True,
                      choices=[ipy.SlashCommandChoice(name=listing.name, value=item.value) for item, listing in ITEMS.items()])
    @ipy.slash_option(name="amount", description="Number of items", opt_type=ipy.OptionType.INTEGER,
                      min_value=1, max_value=1_000_000)
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    async def tokens_give_item(self, ctx: ipy.SlashContext, player: ipy.User, item: str, amount: int = 1) -> None:
        await self._adjust_item(ctx, ItemAdjustment(str(player.id), TokenItem(item), amount,
                                                   str(ctx.author.id), f"item-adjust:{ctx.id}"))

    @tokens.subcommand(sub_cmd_name="remove-item", sub_cmd_description="Remove inventory items from a player")
    @ipy.slash_option(name="player", description="The player", opt_type=ipy.OptionType.USER, required=True)
    @ipy.slash_option(name="item", description="The item", opt_type=ipy.OptionType.STRING, required=True,
                      choices=[ipy.SlashCommandChoice(name=listing.name, value=item.value) for item, listing in ITEMS.items()])
    @ipy.slash_option(name="amount", description="Number of items", opt_type=ipy.OptionType.INTEGER,
                      min_value=1, max_value=1_000_000)
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    async def tokens_remove_item(self, ctx: ipy.SlashContext, player: ipy.User, item: str, amount: int = 1) -> None:
        await self._adjust_item(ctx, ItemAdjustment(str(player.id), TokenItem(item), -amount,
                                                   str(ctx.author.id), f"item-adjust:{ctx.id}"))

    @tokens.subcommand(sub_cmd_name="history", sub_cmd_description="Show grants, bets, payouts, and corrections")
    @ipy.slash_option(name="user", description="Player whose token history to show", opt_type=ipy.OptionType.USER)
    @ipy.slash_option(name="hidden", description="Make the bot response hidden or not", opt_type=ipy.OptionType.BOOLEAN)
    async def tokens_history(self, ctx: ipy.SlashContext, hidden: bool = False,
                             user: ipy.User | ipy.Member | None = None) -> None:
        await ctx.defer(ephemeral=hidden)
        try:
            await self._history(ctx, 1, user_id=str(user.id) if user is not None else None, hidden=hidden)
        except (FinalError, InvalidPageError) as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.component_callback(re.compile(r"^tokenpage\|.+$"))
    @ignore_repeated_clicks
    async def token_page(self, ctx: ipy.ComponentContext):
        _, owner, page, *target_ids = ctx.custom_id.split("|")
        try:
            if owner != str(ctx.author.id):
                raise FinalError("This history belongs to another user.")
            await self._history(ctx, page, user_id=target_ids[0] if target_ids else None, edit=True)
        except (FinalError, InvalidPageError) as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.component_callback(re.compile(r"^tokenjump\|.+$"))
    @ignore_repeated_clicks
    async def token_jump(self, ctx: ipy.ComponentContext):
        _, owner, *_ = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} This history belongs to another user.", ephemeral=True)
            return
        await ctx.send_modal(build_page_jump_modal(ctx.custom_id.replace("tokenjump|", "tokenjumpmodal|", 1)))

    @ipy.modal_callback(re.compile(r"^tokenjumpmodal\|.+$"))
    @ignore_repeated_clicks
    async def token_jump_modal(self, ctx: ipy.ModalContext, page: str):
        _, owner, _, *target_ids = ctx.custom_id.split("|")
        try:
            if owner != str(ctx.author.id):
                raise FinalError("This history belongs to another user.")
            await self._history(ctx, page, user_id=target_ids[0] if target_ids else None, edit=ctx.message is not None)
        except (FinalError, InvalidPageError) as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)


def setup(bot):
    TokenCommands(bot)
