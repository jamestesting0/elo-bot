import re

import interactions as ipy
from interactions import OptionType, SlashContext, slash_option

from elo_bot.config.emojis import ERROR_EMOJI, IMPORTANT_EMOJI, SUCCESS_EMOJI, TOKEN_EMOJI
from elo_bot.config.ids import LVL1_ROLES
from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elo_bot.core.finals import FinalError
from elo_bot.db import EloFinal
from elo_bot.db.token_models import FinalBet, FinalBettingMarket, TokenWallet
from elo_bot.discord.members import member_guild
from elo_bot.discord.mentions import member_names
from elo_bot.extensions.final.checks import final_action
from elo_bot.extensions.final.groups import betting_group, final
from elo_bot.features.tokens.bets import BetAction, apply_bet, close_market, void_market, projected_return
from elo_bot.features.tokens.presentation import bet_review, publish_market
from elo_bot.features.tokens.wallets import ensure_wallet, wallet_summary


class Betting:
    final = final
    betting_group = betting_group

    async def _market(self, ctx, market_id):
        market = await FinalBettingMarket.get(market_id)
        if market is None:
            raise FinalError("This betting market is unavailable.")
        edition = await self._ongoing_edition(ctx, market.final_id, access="player")
        if (market.state != "OPEN" or edition.nodes[market.node_id].contestant_revision != market.contestant_revision
                or edition.state in {"CANCELLING", "CANCELLED"} or edition.repair_pending):
            raise FinalError("Betting is closed for this match.")
        return edition, market

    async def _node_market(self, edition: EloFinal, match: str) -> FinalBettingMarket:
        node = edition.nodes.get(match)
        if node is None:
            raise FinalError("Choose a match from `/final bracket view`.")
        market = await FinalBettingMarket.get(f"{edition.id}:{match}:{node.contestant_revision}")
        if market is None:
            raise FinalError("This pairing has no betting market.")
        return market

    @betting_group.subcommand(sub_cmd_name="close", sub_cmd_description="Close a betting market early")
    @slash_option(name="match", description="Match, e.g. QF1", opt_type=OptionType.STRING, required=True, autocomplete=True)
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_betting_close(self, ctx: SlashContext, match: str):
        await ctx.defer(ephemeral=True)
        edition = await self._ongoing_edition(ctx, access="manager")
        market = await self._node_market(edition, match)
        await close_market(market.id)
        if market.announcement_attempted:
            try:
                await publish_market(self.bot, edition, market)
            except FinalError as error:
                await ctx.send(f"{IMPORTANT_EMOJI} Betting closed. Its post update failed and will retry: {error}",
                               ephemeral=True)
                return
        await ctx.send(f"{SUCCESS_EMOJI} Betting closed.", ephemeral=True)

    @betting_group.subcommand(sub_cmd_name="void", sub_cmd_description="Refund an undecided market")
    @slash_option(name="match", description="Match, e.g. QF1", opt_type=OptionType.STRING, required=True, autocomplete=True)
    @slash_option(name="reason", description="Refund reason", opt_type=OptionType.STRING, required=True)
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_betting_void(self, ctx: SlashContext, match: str, reason: str):
        await ctx.defer(ephemeral=True)
        edition = await self._ongoing_edition(ctx, access="manager")
        market = await self._node_market(edition, match)
        await void_market(market.id, str(ctx.author.id), reason)
        pending = await self._recover(edition)
        await ctx.send(f"{SUCCESS_EMOJI} Refund saved."
                       + (f" {IMPORTANT_EMOJI} " + " ".join(pending) if pending else ""), ephemeral=True)

    @ipy.component_callback(re.compile(r"^betopen\|.+$"))
    @final_action
    async def bet_open(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)
        await self._show_bet(ctx, ctx.custom_id.split("|")[1])

    @ipy.component_callback(re.compile(r"^betback\|.+$"))
    @final_action
    async def bet_back(self, ctx: ipy.ComponentContext):
        _, market_id, owner = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            raise FinalError("You cannot interact with another user's components.")
        await ctx.defer(edit_origin=True)
        await self._show_bet(ctx, market_id, edit=True)

    async def _show_bet(self, ctx, market_id, *, edit=False):
        edition, market = await self._market(ctx, market_id)
        wallet = await ensure_wallet(str(ctx.author.id))
        if set(wallet.identity_claims) & set(market.contestants):
            raise FinalError("Participants cannot bet on their own match.")
        old = await FinalBet.find_one({"market_id": market.id, "wallet_id": wallet.id})
        version = old.version if old else 0
        available, _, debt = await wallet_summary(wallet)
        names = await member_names(await member_guild(ctx), market.contestants)
        active = old is not None and old.active
        current_bet = f"\n{names[old.contestant]} · {old.amount:,} {TOKEN_EMOJI}" if active else ""
        embed = ipy.Embed(title=f"**Season {edition.season_number} Final · {market.node_id} Bet**", color=COLOR,
            description=" vs. ".join(names[user] for user in market.contestants) + current_bet)
        if active:
            _, profit = projected_return(market, old, old.contestant, old.amount)
            embed.add_field(name="**Estimated Profit**", value=f"{profit:,} {TOKEN_EMOJI}")
        embed.add_field(name="**Available Tokens**", value=f"{available:,} {TOKEN_EMOJI}")
        if debt:
            embed.add_field(name="**Debt**", value=f"{debt:,} {TOKEN_EMOJI} · Reduce or cancel your bet to repay.")
        embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
        buttons = [ipy.Button(style=ipy.ButtonStyle.PRIMARY, label=edition.player_nicknames[user][:80],
            custom_id=f"betchoose|{market.id}|{ctx.author.id}|{user}|{version}",
            disabled=debt > 0 and (old is None or not old.active or old.contestant != user)) for user in market.contestants]
        if old and old.active:
            buttons.append(ipy.Button(style=ipy.ButtonStyle.DANGER, label="Cancel Bet",
                custom_id=f"betcancel|{market.id}|{ctx.author.id}|{old.contestant}|{version}"))
        if edit:
            await ctx.edit_origin(content="", embed=embed, components=buttons)
        else:
            await ctx.send(embed=embed, components=buttons, ephemeral=True)

    @ipy.component_callback(re.compile(r"^betchoose\|.+$"))
    @final_action
    async def bet_choose(self, ctx: ipy.ComponentContext):
        _, market_id, owner, contestant, version = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            raise FinalError("You cannot interact with another user's components.")
        try:
            edition, market = await self._market(ctx, market_id)
        except FinalError as error:
            await ctx.edit_origin(content=f"{ERROR_EMOJI} {error}", embeds=[], components=[])
            return
        wallet = await ensure_wallet(owner)
        old = await FinalBet.find_one({"market_id": market.id, "wallet_id": wallet.id})
        if contestant not in market.contestants or (old.version if old else 0) != int(version):
            await ctx.edit_origin(content=f"{ERROR_EMOJI} Your bet changed. Open its current view.", embeds=[], components=[])
            return
        available, _, _ = await wallet_summary(wallet)
        await ctx.send_modal(ipy.Modal(ipy.ShortText(label="Tokens", custom_id="amount", required=True,
            placeholder=f"Available: {available:,}", max_length=12,
            value=str(old.amount) if old and old.active and old.contestant == contestant else None),
            title=f"Bet on {edition.player_nicknames[contestant]}"[:45],
            custom_id=f"betamount|{market_id}|{owner}|{contestant}|{version}"))

    @ipy.modal_callback(re.compile(r"^betamount\|.+$"))
    @final_action
    async def bet_amount(self, ctx: ipy.ModalContext, amount: str):
        _, market_id, owner, contestant, version = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            raise FinalError("You cannot interact with another user's components.")
        await ctx.defer(edit_origin=True)
        _, market = await self._market(ctx, market_id)
        if owner != str(ctx.author.id) or not amount.strip().isascii() or not amount.strip().isdigit() or int(amount) <= 0:
            raise FinalError("Enter a positive whole-token stake in your own bet form.")
        guild = await member_guild(ctx)
        embed, controls = await bet_review(market, owner, contestant, int(amount), int(version), guild=guild)
        await ctx.send(embed=embed, components=controls)

    @ipy.component_callback(re.compile(r"^betcancel\|.+$"))
    @final_action
    async def bet_cancel_review(self, ctx: ipy.ComponentContext):
        _, market_id, owner, contestant, version = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            raise FinalError("You cannot interact with another user's components.")
        await ctx.defer(edit_origin=True)
        _, market = await self._market(ctx, market_id)
        guild = await member_guild(ctx)
        embed, controls = await bet_review(market, owner, contestant, 0, int(version), guild=guild)
        await ctx.edit_origin(content="", embed=embed, components=controls)

    @ipy.component_callback(re.compile(r"^betconfirm\|.+$"))
    @final_action
    async def bet_confirm(self, ctx: ipy.ComponentContext):
        _, market_id, owner, contestant, version, amount = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.", ephemeral=True)
            return
        await ctx.defer(edit_origin=True)
        try:
            edition, market = await self._market(ctx, market_id)
            old = await FinalBet.find_one({"market_id": market_id, "user_id": owner})
            bet = await apply_bet(BetAction(market_id=market_id, user_id=owner, contestant=contestant,
                amount=int(amount), expected_version=int(version), action_key=f"bet-interaction:{ctx.id}"))
            publication_pending = None
            try:
                await publish_market(self.bot, edition, market)
            except FinalError as error:
                publication_pending = str(error)
            available, _, debt = await wallet_summary(await TokenWallet.get(bet.wallet_id))
            names = await member_names(await member_guild(ctx), market.contestants)
            copy = (f"{SUCCESS_EMOJI} Bet updated: {bet.amount:,} {TOKEN_EMOJI} on {names[bet.contestant]}." if bet.active else
                    f"{SUCCESS_EMOJI} Bet cancelled. {old.amount if old else 0:,} {TOKEN_EMOJI} returned.")
            await ctx.edit_origin(content=f"**Season {edition.season_number} Final · {market.node_id}**\n" + copy
                                  + f"\nAvailable Tokens: {available:,} {TOKEN_EMOJI}"
                                  + (f" · Debt: {debt:,} {TOKEN_EMOJI}" if debt else "")
                                  + (f"\n{IMPORTANT_EMOJI} {publication_pending}" if publication_pending else ""),
                                  embeds=[], components=[])
        except FinalError as error:
            await ctx.edit_origin(content=f"{ERROR_EMOJI} {error}", embeds=[], components=[])
