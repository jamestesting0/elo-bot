import asyncio
import interactions as ipy
import re
import logging
from io import BytesIO
from interactions import Extension, SlashContext, slash_option, OptionType

from elobot.db import Player, EloFinal
from elobot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elobot.config.ids import ELO_BLACKLIST_ROLE, FINAL_CHANNEL, GUILD_IDS, LVL1_ROLES, LVL2_ROLES
from elobot.config.emojis import ERROR_EMOJI, IMPORTANT_EMOJI, SUCCESS_EMOJI, TOKEN_EMOJI
from elobot.features.finals import lifecycle
from elobot.features.finals.types import FinalError
from elobot.features.finals.bracket import seeded_players
from elobot.discord.mentions import mention_user
from elobot.discord.members import member_guild
from elobot.features.matches import lifecycle as lifecycle_module
from elobot.features.queue import policy


async def get_current_elo_final_season():
    return (await lifecycle.resolve_final()).season_number


class FinalCommands(Extension):

    final = ipy.SlashCommand(name="final", description="Elo Final management commands")
    
    bracket_group = final.group(name="bracket", description="Create and view the Final bracket")
    bye_group = final.group(name="bye", description="Assign first-round byes")
    round_group = final.group(name="round", description="Prepare Final rounds")
    match_group = final.group(name="match", description="View and edit Final results")
    predictions_group = final.group(name="predictions", description="Open or manually close predictions")
    prediction_group = final.group(name="prediction", description="View your submitted prediction")
    betting_group = final.group(name="betting", description="Manage Final betting markets")

    async def _recover(self, edition):
        from elobot.features.finals.recovery import recover_final
        from elobot.extensions.match.adapters import clear_match_base_stores
        from elobot.features.matches.resources import free_match_clans, remove_helper_task
        async def release(source):
            await clear_match_base_stores(source, reset_stored_flags=True, bot=self.bot, reason="finished")
            await free_match_clans(source)
        async def deleted(source):
            await clear_match_base_stores(source, reset_stored_flags=True, bot=self.bot, reason="thread_deleted")
            await free_match_clans(source)
            for participant in source.participants:
                await remove_helper_task(source.thread, participant.user_id)
        return await recover_final(edition, bot=self.bot, release_completed=release, release_deleted=deleted)

    @final.subcommand(sub_cmd_name="recover", sub_cmd_description="Resume unfinished Final updates")
    @slash_option(name="season", description="Final season", opt_type=OptionType.INTEGER)
    async def final_recover(self, ctx: SlashContext, season: int = None):
        await ctx.defer(ephemeral=True)
        try:
            pending = await self._recover(await self._edition(ctx, season, manager=True))
            await ctx.send(f"{IMPORTANT_EMOJI} " + "\n".join(pending) if pending else
                           f"{SUCCESS_EMOJI} Final updates are complete.", ephemeral=True)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @final.subcommand(sub_cmd_name="end", sub_cmd_description="Archive the completed Final")
    async def final_end(self, ctx: SlashContext):
        await ctx.defer(ephemeral=True)
        try:
            edition = await self._edition(ctx, manager=True)
            await self._recover(edition)
            await lifecycle.end_final(edition)
            await ctx.send(f"{SUCCESS_EMOJI} Season {edition.season_number} Final archived.", ephemeral=True)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @final.subcommand(sub_cmd_name="cancel", sub_cmd_description="Cancel new work and refund undecided markets")
    @slash_option(name="reason", description="Cancellation reason", opt_type=OptionType.STRING, required=True)
    async def final_cancel(self, ctx: SlashContext, reason: str):
        await ctx.defer(ephemeral=True)
        try:
            edition = await self._edition(ctx, manager=True)
            await self._recover(edition)
            await lifecycle.request_cancellation(edition, reason)
            pending = await self._recover(edition)
            current = await lifecycle.resolve_final(edition.season_number)
            await ctx.send(f"{SUCCESS_EMOJI} Final cancelled." if current.state == "CANCELLED" else
                           f"{IMPORTANT_EMOJI} Cancellation saved. " + "\n".join(pending), ephemeral=True)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @bracket_group.subcommand(sub_cmd_name="reset", sub_cmd_description="Return an unused bracket to registration")
    async def final_bracket_reset(self, ctx: SlashContext):
        try:
            await lifecycle.reset_bracket(await self._edition(ctx, manager=True))
            await ctx.send(f"{SUCCESS_EMOJI} Bracket reset. Registration is open.", ephemeral=True)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.Task.create(ipy.IntervalTrigger(minutes=2))
    async def recover_pending_finals(self):
        from pymongo.errors import PyMongoError
        for edition in await EloFinal.find({"schema_version": 2}).to_list():
            if edition.active or edition.repair_pending or edition.publication_pending or edition.reopening_pending:
                try:
                    pending = await self._recover(edition)
                    if pending:
                        logging.getLogger(__name__).info("Final %s recovery: %s", edition.id, pending)
                except (FinalError, PyMongoError, ipy.errors.HTTPException, RuntimeError, LookupError):
                    logging.getLogger(__name__).exception("Final %s recovery failed", edition.id)

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        from elobot.features.tokens.wallets import backfill_wallets
        try:
            await backfill_wallets()
        except FinalError:
            logging.getLogger(__name__).exception("Token backfill is unavailable; registration and views may retry it")
        await self.recover_pending_finals()
        self.recover_pending_finals.start()

    @final.subcommand(sub_cmd_name="profile", sub_cmd_description="Show a player's Final statistics")
    @slash_option(name="player", description="Player", opt_type=OptionType.USER)
    @slash_option(name="season", description="Final season", opt_type=OptionType.INTEGER)
    async def final_profile(self, ctx: SlashContext, player: ipy.User = None, season: int = None):
        from elobot.features.finals.profile_ui import profile_view
        await ctx.defer()
        try:
            edition = await self._edition(ctx, season)
            guild = await member_guild(ctx)
            embed, controls = await profile_view(edition, player or ctx.author, str(ctx.author.id), guild=guild)
            await ctx.send(embed=embed, components=controls)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.component_callback(re.compile(r"^finalprofile(?:refresh)?\|.+$"))
    async def final_profile_navigation(self, ctx: ipy.ComponentContext):
        from elobot.features.finals.profile_ui import profile_view
        parts = ctx.custom_id.split("|")
        try:
            if parts[1] != str(ctx.author.id):
                raise FinalError("This profile navigation belongs to another user.")
            season = int(parts[3]) if parts[0] == "finalprofilerefresh" else int(ctx.values[0])
            edition = await self._edition(ctx, season)
            user = await self.bot.fetch_user(int(parts[2]))
            embed, controls = await profile_view(edition, user, parts[1], guild=await member_guild(ctx))
            await ctx.edit_origin(embed=embed, components=controls)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    async def _history_view(self, ctx, season, user, page=1, *, edit=False):
        from elobot.discord.pagination import build_page_button, parse_page_number
        from elobot.features.finals.result_ui import result_embed
        from elobot.db.final_models import FinalMatchResult
        from elobot.db.token_models import TokenWallet
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
            members = await asyncio.gather(*(guild.fetch_member(user) for user in result.snapshot.contestants))
            names = {user: mention_user(member, user) for user, member in zip(result.snapshot.contestants, members)}
            embed = result_embed(edition, result, names)
            if edition.nodes[result.node_id].result_id != result.id:
                embed.add_field(name="**History**", value="Replaced by a correction.")
        else:
            embed = ipy.Embed(title=f"**Season {edition.season_number} Final · History**", description="No results.", color=COLOR)
            embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
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

    @final.subcommand(sub_cmd_name="matches", sub_cmd_description="Show a player's Final result history")
    @slash_option(name="player", description="Player", opt_type=OptionType.USER)
    @slash_option(name="season", description="Final season", opt_type=OptionType.INTEGER)
    async def final_matches(self, ctx: SlashContext, player: ipy.User = None, season: int = None):
        await ctx.defer()
        try:
            await self._history_view(ctx, season, str((player or ctx.author).id))
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @final.subcommand(sub_cmd_name="history", sub_cmd_description="Show Final outcomes and corrections")
    @slash_option(name="season", description="Final season", opt_type=OptionType.INTEGER)
    async def final_history(self, ctx: SlashContext, season: int = None):
        await ctx.defer()
        try:
            await self._history_view(ctx, season, "-")
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.component_callback(re.compile(r"^finalhist\|.+$"))
    async def final_history_page(self, ctx: ipy.ComponentContext):
        from elobot.discord.pagination import InvalidPageError
        _, owner, season, user, page = ctx.custom_id.split("|")
        try:
            if owner != str(ctx.author.id):
                raise FinalError("This history navigation belongs to another user.")
            await self._history_view(ctx, int(season), user, page, edit=True)
        except (FinalError, InvalidPageError) as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.component_callback(re.compile(r"^finalhistjump\|.+$"))
    async def final_history_jump(self, ctx: ipy.ComponentContext):
        from elobot.discord.pagination import build_page_jump_modal
        if ctx.custom_id.split("|")[1] != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} This history navigation belongs to another user.", ephemeral=True)
            return
        await ctx.send_modal(build_page_jump_modal(ctx.custom_id.replace("finalhistjump", "finalhistmodal", 1)))

    @ipy.modal_callback(re.compile(r"^finalhistmodal\|.+$"))
    async def final_history_jump_modal(self, ctx: ipy.ModalContext, page: str):
        from elobot.discord.pagination import InvalidPageError
        _, owner, season, user = ctx.custom_id.split("|")
        try:
            if owner != str(ctx.author.id):
                raise FinalError("This history navigation belongs to another user.")
            await self._history_view(ctx, int(season), user, page, edit=ctx.message is not None)
        except (FinalError, InvalidPageError) as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    async def _market(self, ctx, market_id, *, registered=False):
        from beanie import PydanticObjectId
        from elobot.db.token_models import FinalBettingMarket
        market = await FinalBettingMarket.get(market_id)
        if market is None:
            raise FinalError("This betting market is unavailable.")
        edition = await EloFinal.get(PydanticObjectId(market.final_id))
        if edition is None:
            raise FinalError("This Final is unavailable.")
        await self._access(ctx, edition, registered=registered)
        return edition, market

    @betting_group.subcommand(sub_cmd_name="announce", sub_cmd_description="Publish a known pairing's betting market")
    @slash_option(name="match", description="Match, e.g. QF1", opt_type=OptionType.STRING, required=True,
                  autocomplete=True)
    async def final_betting_announce(self, ctx: SlashContext, match: str):
        from elobot.features.tokens.bets import open_market
        from elobot.features.tokens.presentation import publish_market
        await ctx.defer(ephemeral=True)
        try:
            edition = await self._edition(ctx, manager=True)
            market = await open_market(edition, match)
            message = await publish_market(self.bot, edition, market)
            await ctx.send(f"{SUCCESS_EMOJI} Betting announced: {message.jump_url}", ephemeral=True)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    async def _node_market(self, ctx, match, season=None, *, manager=False):
        from elobot.db.token_models import FinalBettingMarket
        edition = await self._edition(ctx, season, manager=manager)
        node = edition.nodes.get(match)
        if node is None:
                raise FinalError("Choose a match from `/final match list`.")
        market = await FinalBettingMarket.get(f"{edition.id}:{match}:{node.contestant_revision}")
        if market is None:
            raise FinalError("This pairing has no betting market.")
        return edition, market

    @betting_group.subcommand(sub_cmd_name="close", sub_cmd_description="Close a betting market early")
    @slash_option(name="match", description="Match, e.g. QF1", opt_type=OptionType.STRING, required=True)
    async def final_betting_close(self, ctx: SlashContext, match: str):
        from elobot.features.tokens.bets import close_market
        from elobot.features.tokens.presentation import publish_market
        await ctx.defer(ephemeral=True)
        try:
            edition, market = await self._node_market(ctx, match, manager=True)
            await close_market(market.id)
            if market.announcement_attempted:
                try:
                    await publish_market(self.bot, edition, market)
                except FinalError as error:
                    await ctx.send(f"{SUCCESS_EMOJI} Betting closed. {ERROR_EMOJI} {error}", ephemeral=True)
                    return
            await ctx.send(f"{SUCCESS_EMOJI} Betting closed.", ephemeral=True)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @betting_group.subcommand(sub_cmd_name="void", sub_cmd_description="Refund an undecided market")
    @slash_option(name="match", description="Match, e.g. QF1", opt_type=OptionType.STRING, required=True)
    @slash_option(name="reason", description="Refund reason", opt_type=OptionType.STRING, required=True)
    async def final_betting_void(self, ctx: SlashContext, match: str, reason: str):
        from elobot.features.tokens.bets import void_market
        await ctx.defer(ephemeral=True)
        try:
            edition, market = await self._node_market(ctx, match, manager=True)
            await void_market(market.id, str(ctx.author.id), reason)
            pending = await self._recover(edition)
            await ctx.send(f"{SUCCESS_EMOJI} Refund saved."
                           + (f" {IMPORTANT_EMOJI} " + " ".join(pending) if pending else ""), ephemeral=True)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @betting_group.subcommand(sub_cmd_name="status", sub_cmd_description="Show the pairing's pool and status")
    @slash_option(name="match", description="Match, e.g. QF1", opt_type=OptionType.STRING, required=True)
    @slash_option(name="season", description="Final season", opt_type=OptionType.INTEGER)
    async def final_betting_status(self, ctx: SlashContext, match: str, season: int = None):
        from elobot.features.tokens.presentation import market_controls, market_embed
        try:
            edition, market = await self._node_market(ctx, match, season)
            guild = await member_guild(ctx)
            members = await asyncio.gather(*(guild.fetch_member(user) for user in market.contestants))
            names = {user: mention_user(member, user) for user, member in zip(market.contestants, members)}
            await ctx.send(embed=market_embed(edition, market, names), components=market_controls(market))
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.component_callback(re.compile(r"^betopen\|.+$"))
    async def bet_open(self, ctx: ipy.ComponentContext):
        from elobot.features.tokens.wallets import ensure_wallet, wallet_summary
        from elobot.db.token_models import FinalBet
        await ctx.defer(ephemeral=True)
        try:
            edition, market = await self._market(ctx, ctx.custom_id.split("|")[1], registered=True)
            wallet = await ensure_wallet(str(ctx.author.id))
            if market.state != "OPEN":
                raise FinalError("Betting is closed for this pairing.")
            if set(wallet.identity_claims) & set(market.contestants):
                raise FinalError("Participants cannot bet on their own match.")
            old = await FinalBet.find_one({"market_id": market.id, "wallet_id": wallet.id})
            version = old.version if old else 0
            available, _, debt = await wallet_summary(wallet)
            current_bet = ""
            if old and old.active:
                guild = await member_guild(ctx)
                member = await guild.fetch_member(old.contestant)
                current_bet = f"\nCurrent bet: {old.amount:,} {TOKEN_EMOJI} on {mention_user(member, old.contestant)}"
            embed = ipy.Embed(title=f"**Season {edition.season_number} Final · {market.node_id} Bet**", color=COLOR,
                description="Choose a player." + current_bet)
            embed.add_field(name="**Available Tokens**", value=f"{available:,} {TOKEN_EMOJI}")
            if debt:
                embed.add_field(name="**Debt**", value=f"{debt:,} {TOKEN_EMOJI} · reduce or cancel your bet to repay")
            embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
            buttons = [ipy.Button(style=ipy.ButtonStyle.PRIMARY, label=edition.player_nicknames[user][:80],
                custom_id=f"betchoose|{market.id}|{ctx.author.id}|{user}|{version}",
                disabled=debt > 0 and (old is None or not old.active or old.contestant != user)) for user in market.contestants]
            if old and old.active:
                buttons.append(ipy.Button(style=ipy.ButtonStyle.DANGER, label="Cancel Bet",
                    custom_id=f"betcancel|{market.id}|{ctx.author.id}|{old.contestant}|{version}"))
            await ctx.send(embed=embed, components=buttons, ephemeral=True)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.component_callback(re.compile(r"^betchoose\|.+$"))
    async def bet_choose(self, ctx: ipy.ComponentContext):
        _, market_id, owner, contestant, version = ctx.custom_id.split("|")
        try:
            await self._market(ctx, market_id, registered=True)
            if owner != str(ctx.author.id):
                raise FinalError("This bet belongs to another user.")
            await ctx.send_modal(ipy.Modal(ipy.ShortText(label="Stake in tokens", custom_id="amount", required=True),
                title="Bet amount", custom_id=f"betamount|{market_id}|{owner}|{contestant}|{version}"))
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.modal_callback(re.compile(r"^betamount\|.+$"))
    async def bet_amount(self, ctx: ipy.ModalContext, amount: str):
        from elobot.features.tokens.presentation import bet_review
        _, market_id, owner, contestant, version = ctx.custom_id.split("|")
        await ctx.defer(ephemeral=True)
        try:
            _, market = await self._market(ctx, market_id, registered=True)
            if owner != str(ctx.author.id) or not amount.strip().isascii() or not amount.strip().isdigit() or int(amount) <= 0:
                raise FinalError("Enter a positive whole-token stake in your own bet form.")
            guild = await member_guild(ctx)
            embed, controls = await bet_review(market, owner, contestant, int(amount), int(version), guild=guild)
            await ctx.send(embed=embed, components=controls, ephemeral=True)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.component_callback(re.compile(r"^betcancel\|.+$"))
    async def bet_cancel_review(self, ctx: ipy.ComponentContext):
        from elobot.features.tokens.presentation import bet_review
        _, market_id, owner, contestant, version = ctx.custom_id.split("|")
        await ctx.defer(ephemeral=True)
        try:
            _, market = await self._market(ctx, market_id, registered=True)
            if owner != str(ctx.author.id):
                raise FinalError("This bet belongs to another user.")
            guild = await member_guild(ctx)
            embed, controls = await bet_review(market, owner, contestant, 0, int(version), guild=guild)
            await ctx.send(embed=embed, components=controls, ephemeral=True)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.component_callback(re.compile(r"^betconfirm\|.+$"))
    async def bet_confirm(self, ctx: ipy.ComponentContext):
        from elobot.features.tokens.bets import BetAction, apply_bet
        from elobot.features.tokens.wallets import wallet_summary
        from elobot.db.token_models import FinalBet, TokenWallet
        _, market_id, owner, contestant, version, amount = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} This bet belongs to another user.", ephemeral=True)
            return
        await ctx.defer(edit_origin=True)
        try:
            edition, market = await self._market(ctx, market_id, registered=True)
            old = await FinalBet.find_one({"market_id": market_id, "user_id": owner})
            bet = await apply_bet(BetAction(market_id=market_id, user_id=owner, contestant=contestant,
                amount=int(amount), expected_version=int(version), action_key=f"bet-interaction:{ctx.id}"))
            available, _, debt = await wallet_summary(await TokenWallet.get(bet.wallet_id))
            copy = (f"{SUCCESS_EMOJI} Bet updated: {bet.amount:,} {TOKEN_EMOJI} on <@{bet.contestant}>." if bet.active else
                    f"{SUCCESS_EMOJI} Bet cancelled. {old.amount if old else 0:,} {TOKEN_EMOJI} returned.")
            await ctx.edit_origin(content=f"**Season {edition.season_number} Final · {market.node_id}**\n" + copy
                                  + f"\nAvailable Tokens: {available:,} {TOKEN_EMOJI}"
                                  + (f" · Debt: {debt:,} {TOKEN_EMOJI}" if debt else ""), embeds=[], components=[])
        except FinalError as error:
            await ctx.edit_origin(content=f"{ERROR_EMOJI} {error}", embeds=[], components=[
                ipy.Button(style=ipy.ButtonStyle.SECONDARY, label="Open Bet", custom_id=f"betopen|{market_id}")])

    async def _access(self, ctx, edition=None, *, manager=False, registered=False):
        if ctx.guild_id not in GUILD_IDS or (edition is not None and edition.guild_id != ctx.guild_id):
            raise FinalError("Use this command in the Final's server.")
        if ctx.author.has_role(ELO_BLACKLIST_ROLE):
            raise FinalError("You cannot use Final commands while blacklisted.")
        if manager and not any(ctx.author.has_role(role) for role in LVL1_ROLES):
            raise FinalError("A manager must perform this action.")
        if registered and await Player.find_one({"user_id": str(ctx.author.id)}) is None:
            raise FinalError("Register before using this action.")

    async def _edition(self, ctx, season=None, *, manager=False, registered=False):
        edition = await lifecycle.resolve_final(season)
        await self._access(ctx, edition, manager=manager, registered=registered)
        return edition

    @final.subcommand(sub_cmd_name="start", sub_cmd_description="Open a Final for a completed season")
    @slash_option(name="season", description="Completed season", opt_type=OptionType.INTEGER)
    async def final_start(self, ctx: SlashContext, season: int = None):
        try:
            await self._access(ctx, manager=True)
            edition = await lifecycle.start_final(season, ctx.guild_id, FINAL_CHANNEL)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await ctx.send(f"{SUCCESS_EMOJI} Season {edition.season_number} Final registration opened.")

    @final.subcommand(
        sub_cmd_name="add",
        sub_cmd_description="Add a player to the Elo Final",
    )
    @slash_option(name="player", description="The player to add", opt_type=OptionType.USER, required=True)
    @slash_option(name="nickname", description="Display nickname", opt_type=OptionType.STRING)
    async def final_player_add(self, ctx: SlashContext, player: ipy.User, nickname: str = None):
        try:
            edition = await self._edition(ctx, manager=True)
            edition = await lifecycle.add_entrant(edition, str(player.id), nickname or str(player))
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await ctx.send(f"{SUCCESS_EMOJI} Added {player.mention} to Season {edition.season_number} Final.")

    @final.subcommand(
        sub_cmd_name="remove",
        sub_cmd_description="Remove a player from the Elo Final",
    )
    @slash_option(name="player", description="The player to remove", opt_type=OptionType.USER, required=True)
    async def final_player_remove(self, ctx: SlashContext, player: ipy.User):
        try:
            edition = await self._edition(ctx, manager=True)
            await lifecycle.remove_entrant(edition, str(player.id))
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await ctx.send(f"{SUCCESS_EMOJI} Removed {player.mention} from Season {edition.season_number} Final.")

    @final.subcommand(
        sub_cmd_name="nick",
        sub_cmd_description="Set nickname for a player in the Elo Final",
    )
    @slash_option(name="player", description="The player", opt_type=OptionType.USER, required=True)
    @slash_option(name="nickname", description="New nickname", opt_type=OptionType.STRING, required=True)
    async def final_player_nick(self, ctx: SlashContext, player: ipy.User, nickname: str):
        try:
            elo_final = await self._edition(ctx, manager=True)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        if not elo_final or str(player.id) not in elo_final.player_nicknames:
            await ctx.send(f"{ERROR_EMOJI} This player is not an entrant.", ephemeral=True)
            return

        old_nick = elo_final.player_nicknames[str(player.id)]
        field = f"player_nicknames.{player.id}"
        result = await elo_final.get_pymongo_collection().update_one(
            {"_id": elo_final.id, field: {"$exists": True}},
            {"$set": {field: nickname.strip()[:80], "publication_pending": True}, "$inc": {"revision": 1}},
        )
        if result.matched_count == 0:
            await ctx.send(f"{ERROR_EMOJI} This player was removed from the final. The nickname was not changed.",
                           ephemeral=True)
            return

        await ctx.send(f"{SUCCESS_EMOJI} Updated {player.mention}'s nickname from **{old_nick}** to **{nickname}**.")
    
    @final.subcommand(
        sub_cmd_name="players",
        sub_cmd_description="List players in the Elo Final",
    )
    @slash_option(name="season", description="Final season", opt_type=OptionType.INTEGER)
    async def final_player_list(self, ctx: SlashContext, season: int = None):
        try:
            elo_final = await self._edition(ctx, season)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        season = elo_final.season_number
        if not elo_final or not elo_final.player_nicknames:
            await ctx.send(f"{ERROR_EMOJI} No players registered for Season {season} Final.", ephemeral=True)
            return
        
        player_ids = seeded_players(elo_final)
        guild = await member_guild(ctx)
        members = await asyncio.gather(*(guild.fetch_member(user_id) for user_id in player_ids))
        msg_lines = []
        for position, (user_id, member) in enumerate(zip(player_ids, members), 1):
            msg_lines.append(f"{elo_final.seeds.get(user_id, position)}. {mention_user(member, user_id)} — "
                             f"{re.sub(r'([*_~|>])', r'\\\1', elo_final.player_nicknames[user_id])}")
        
        embed = ipy.Embed(
            title=f"**Elo Final Season {season}**",
            description=f"**Registered Players ({len(elo_final.player_nicknames)}/8):**\n\n" + "\n".join(msg_lines),
            color=COLOR
        )
        embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
        await ctx.send(embed=embed)

    @final.subcommand(sub_cmd_name="seed", sub_cmd_description="Set an entrant's seed")
    @slash_option(name="player", description="Entrant", opt_type=OptionType.USER, required=True)
    @slash_option(name="position", description="Unused seed 1-8", opt_type=OptionType.INTEGER, required=True,
                  min_value=1, max_value=8)
    async def final_seed(self, ctx: SlashContext, player: ipy.User, position: int):
        try:
            await lifecycle.set_seed(await self._edition(ctx, manager=True), str(player.id), position)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await ctx.send(f"{SUCCESS_EMOJI} Seed {position} assigned to {player.mention}.")

    @bye_group.subcommand(sub_cmd_name="set", sub_cmd_description="Assign an explicit first-round bye")
    @slash_option(name="player", description="Entrant", opt_type=OptionType.USER, required=True)
    async def final_bye_set(self, ctx: SlashContext, player: ipy.User):
        await self._bye(ctx, player, True)

    @bye_group.subcommand(sub_cmd_name="remove", sub_cmd_description="Remove an explicit first-round bye")
    @slash_option(name="player", description="Entrant", opt_type=OptionType.USER, required=True)
    async def final_bye_remove(self, ctx: SlashContext, player: ipy.User):
        await self._bye(ctx, player, False)

    async def _bye(self, ctx, player, enabled):
        try:
            await lifecycle.set_bye(await self._edition(ctx, manager=True), str(player.id), enabled)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await ctx.send(f"{SUCCESS_EMOJI} First-round bye {'assigned' if enabled else 'removed'} for {player.mention}.")

    @final.subcommand(sub_cmd_name="status", sub_cmd_description="Show Final readiness and recovery")
    @slash_option(name="season", description="Final season", opt_type=OptionType.INTEGER)
    async def final_status(self, ctx: SlashContext, season: int = None):
        try:
            edition = await self._edition(ctx, season)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        embed = ipy.Embed(title=f"**Season {edition.season_number} Final**", color=COLOR,
                          description=f"{edition.state} · {edition.current_round}")
        embed.add_field(name="**Predictions**", value=edition.prediction_state)
        embed.add_field(name="**Entrants**", value=f"{len(edition.entrant_ids)}/8")
        if edition.reopening_pending:
            embed.add_field(name="**Corrected play**", value="Reopening is pending until the ongoing Final finishes.")
        if edition.repair_pending or edition.publication_error or edition.publication_pending:
            embed.add_field(name="**Recovery**", value="Pending. A manager can use `/final recover`.")
        embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
        await ctx.send(embed=embed)

    @bracket_group.subcommand(sub_cmd_name="create", sub_cmd_description="Confirm automatic or manual pairing")
    @slash_option(name="mode", description="Pairing mode", opt_type=OptionType.STRING,
                  choices=[ipy.SlashCommandChoice(name="Automatic", value="Automatic"),
                           ipy.SlashCommandChoice(name="Manual", value="Manual")])
    async def final_bracket_create(self, ctx: SlashContext, mode: str = "Automatic"):
        try:
            edition = await self._edition(ctx, manager=True)
            if mode == "Manual":
                from elobot.features.finals.pairing_ui import pairing_view
                edition = await lifecycle.save_pairing_draft(edition, str(ctx.author.id), edition.manual_draft)
                guild = await member_guild(ctx)
                members = await asyncio.gather(*(guild.fetch_member(user) for user in edition.entrant_ids))
                names = {user: mention_user(member, user) for user, member in zip(edition.entrant_ids, members)}
                embed, controls = pairing_view(edition, str(ctx.author.id), 0, names)
                await ctx.send(embed=embed, components=controls, ephemeral=True)
                return
            edition = await lifecycle.confirm_bracket(edition, str(ctx.author.id))
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await ctx.send(f"{SUCCESS_EMOJI} Season {edition.season_number} Final bracket confirmed.")

    @ipy.component_callback(re.compile(r"^finalpair\|.+$"))
    async def final_pairing_action(self, ctx: ipy.ComponentContext):
        from elobot.features.finals.pairing_ui import pairing_view
        _, season, owner, revision, page, action = ctx.custom_id.split("|")
        try:
            edition = await self._edition(ctx, int(season), manager=True)
            if owner != str(ctx.author.id) or edition.draft_owner != owner:
                raise FinalError("This pairing draft belongs to another manager.")
            if int(revision) != edition.draft_revision:
                raise FinalError("The draft changed. Use `/final bracket create mode:Manual` to resume.")
            index = int(page)
            pairs = {key: list(pair) for key, pair in edition.manual_draft.items()}
            node = f"QF{index + 1}"
            if action in {"a", "b"}:
                pair = pairs.setdefault(node, [None, None])
                pair[0 if action == "a" else 1] = None if ctx.values[0] == "BYE" else ctx.values[0]
                edition = await lifecycle.save_pairing_draft(edition, owner, pairs)
            elif action == "clear":
                pairs.pop(node, None)
                edition = await lifecycle.save_pairing_draft(edition, owner, pairs)
            elif action == "back":
                index = max(0, index - 1)
            elif action == "next":
                index = min(3, index + 1)
            elif action == "cancel":
                await lifecycle.update_setup(edition, {"draft_owner": None, "manual_draft": {},
                                                       "draft_revision": edition.draft_revision + 1})
                await ctx.edit_origin(content=f"{SUCCESS_EMOJI} Pairing draft cancelled.", embeds=[], components=[])
                return
            elif action == "confirm":
                await lifecycle.confirm_bracket(edition, owner, manual=True)
                await ctx.edit_origin(content=f"{SUCCESS_EMOJI} Bracket confirmed.", embeds=[], components=[])
                return
            guild = await member_guild(ctx)
            members = await asyncio.gather(*(guild.fetch_member(user) for user in edition.entrant_ids))
            names = {user: mention_user(member, user) for user, member in zip(edition.entrant_ids, members)}
            embed, controls = pairing_view(edition, owner, index, names)
            await ctx.edit_origin(embed=embed, components=controls)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @bracket_group.subcommand(sub_cmd_name="view", sub_cmd_description="View the saved Final bracket")
    @slash_option(name="season", description="Final season", opt_type=OptionType.INTEGER)
    async def final_bracket_view(self, ctx: SlashContext, season: int = None):
        from elobot.features.finals.publication import bracket_embed
        from elobot.features.finals.rendering import render_bracket
        await ctx.defer()
        try:
            edition = await self._edition(ctx, season)
            user_ids = {user for node in edition.nodes.values() for user in (*node.contestants, node.winner) if user}
            guild = await member_guild(ctx)
            members = await asyncio.gather(*(guild.fetch_member(user) for user in user_ids))
            names = {user: mention_user(member, user) for user, member in zip(user_ids, members)}
            embed = bracket_embed(edition, names)
            try:
                rendered = await asyncio.to_thread(render_bracket, edition)
            except FinalError as error:
                embed.add_field(name="**Image**", value=str(error))
                await ctx.send(embed=embed)
                return
            embed.set_image(url="attachment://bracket.png")
            with BytesIO(rendered) as image:
                await ctx.send(embed=embed, file=ipy.File(image, file_name="bracket.png"))
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @bracket_group.subcommand(sub_cmd_name="publish", sub_cmd_description="Publish or refresh the saved bracket")
    async def final_bracket_publish(self, ctx: SlashContext):
        from elobot.features.finals.publication import publish_bracket
        await ctx.defer(ephemeral=True)
        try:
            edition = await self._edition(ctx, manager=True)
            message = await publish_bracket(self.bot, edition)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await ctx.send(f"{SUCCESS_EMOJI} Bracket published: {message.jump_url}", ephemeral=True)

    @round_group.subcommand(sub_cmd_name="create", sub_cmd_description="Check whether a round is ready")
    @slash_option(name="round", description="Round", opt_type=OptionType.STRING, required=True,
                  choices=[ipy.SlashCommandChoice(name=name, value=name) for name in
                           ("Quarterfinals", "Semifinals", "Final")])
    async def final_round_create(self, ctx: SlashContext, round: str):
        from elobot.features.finals.bracket import require_round_ready
        try:
            edition = await self._edition(ctx, manager=True)
            nodes = require_round_ready(edition, round)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await ctx.send(f"{SUCCESS_EMOJI} {round} pairings ready: {', '.join(nodes)}. Use `/final round start`.")

    @round_group.subcommand(sub_cmd_name="start", sub_cmd_description="Create or resume the round's match threads")
    @slash_option(name="round", description="Round", opt_type=OptionType.STRING, required=True,
                  choices=[ipy.SlashCommandChoice(name=name, value=name) for name in
                           ("Quarterfinals", "Semifinals", "Final")])
    async def final_round_start(self, ctx: SlashContext, round: str):
        from elobot.features.finals.bracket import require_round_ready
        from elobot.features.finals.matches import bind_thread, prepare_match
        from pymongo.errors import PyMongoError
        from elobot.features.matches.thread_messages import ThreadMessageBindingError
        await ctx.defer(ephemeral=True)
        completed = []
        try:
            edition = await self._edition(ctx, manager=True)
            for node_id in require_round_ready(edition, round):
                async with policy.QUEUE_OPERATION_LOCK:
                    source = await prepare_match(edition, node_id)
                if source is None:
                    continue
                thread = await lifecycle_module.confirm_match(self.bot, source.token)
                await bind_thread(edition, source, thread)
                completed.append(f"{node_id}: <#{thread}>")
        except (FinalError, ipy.errors.HTTPException, PyMongoError, ThreadMessageBindingError) as error:
            await ctx.send(f"{ERROR_EMOJI} Could not create all match threads. Use `/final recover`.\n"
                           + "\n".join(completed), ephemeral=True)
            return
        await ctx.send(f"{SUCCESS_EMOJI} Threads ready. Hosting has not started.\n" + "\n".join(completed), ephemeral=True)

    async def open_result_form(self, ctx, edition, node_id, *, winner=None, outcome="Played", reason=None):
        from elobot.features.finals.result_ui import result_form
        from elobot.db.final_models import FinalMatchResult
        node = edition.nodes.get(node_id)
        if node is None or node.bye or not all(node.contestants):
            raise FinalError("Choose a playable match from `/final match list`.")
        previous = await FinalMatchResult.get(node.result_id) if node.result_id else None
        await ctx.send_modal(result_form(edition, node, str(ctx.author.id), previous,
                                         winner=winner, outcome=outcome, reason=reason))

    @match_group.subcommand(sub_cmd_name="result", sub_cmd_description="Submit or correct all seven district scores")
    @slash_option(name="match", description="Match, e.g. QF1", opt_type=OptionType.STRING, required=True,
                  autocomplete=True)
    @slash_option(name="winner", description="Explicit tie or forfeit winner", opt_type=OptionType.USER)
    @slash_option(name="outcome", description="Played or unplayed Forfeit", opt_type=OptionType.STRING,
                  choices=[ipy.SlashCommandChoice(name=name, value=name) for name in ("Played", "Forfeit")])
    @slash_option(name="reason", description="Correction, tie or forfeit reason", opt_type=OptionType.STRING)
    @slash_option(name="season", description="Final season", opt_type=OptionType.INTEGER)
    async def final_match_result(self, ctx: SlashContext, match: str, winner: ipy.User = None,
                                 outcome: str = "Played", reason: str = None, season: int = None):
        try:
            edition = await self._edition(ctx, season, manager=True)
            await self.open_result_form(ctx, edition, match, winner=str(winner.id) if winner else None,
                                        outcome=outcome, reason=reason)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @match_group.subcommand(sub_cmd_name="forfeit", sub_cmd_description="Record an unplayed forfeit with zero scores")
    @slash_option(name="match", description="Match, e.g. QF1", opt_type=OptionType.STRING, required=True,
                  autocomplete=True)
    @slash_option(name="winner", description="Advancing entrant", opt_type=OptionType.USER, required=True)
    @slash_option(name="reason", description="Forfeit reason", opt_type=OptionType.STRING, required=True)
    async def final_match_forfeit(self, ctx: SlashContext, match: str, winner: ipy.User, reason: str):
        try:
            edition = await self._edition(ctx, manager=True)
            await self.open_result_form(ctx, edition, match, winner=str(winner.id), outcome="Forfeit", reason=reason)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @ipy.modal_callback(re.compile(r"^finalres\|.+$"))
    async def final_result_modal(self, ctx: ipy.ModalContext, **response):
        from elobot.features.finals.result_ui import result_embed, score_embed
        from elobot.features.finals.results import FinalResultSubmission, submission_snapshot, submit_result
        from pydantic import ValidationError
        from pymongo.errors import PyMongoError
        _, season, node_id, owner, version, revision, attempt, winner, forfeit = ctx.custom_id.split("|")
        await ctx.defer(ephemeral=True)
        try:
            edition = await self._edition(ctx, int(season))
            needs_manager = edition.nodes[node_id].result_id is not None or winner != "-" or forfeit == "1"
            roles = LVL1_ROLES if needs_manager else LVL2_ROLES
            if not any(ctx.author.has_role(role) for role in roles):
                raise FinalError("A manager must correct results; staff may submit an initial result.")
            if owner != str(ctx.author.id):
                raise FinalError("This result form belongs to another user.")
            request = FinalResultSubmission(
                final_id=edition.id, node_id=node_id, actor_id=owner, operation_key=f"final-modal:{ctx.id}",
                expected_version=int(version), contestant_revision=int(revision), attempt=int(attempt),
                scores={user: response[f"player{side + 1}"].splitlines()
                        for side, user in enumerate(edition.nodes[node_id].contestants)},
                winner=None if winner == "-" else winner, outcome="Forfeit" if forfeit == "1" else "Played",
                reason=response.get("reason"),
            )
            snapshot = submission_snapshot(edition, request)
            guild = await member_guild(ctx)
            members = await asyncio.gather(*(guild.fetch_member(user) for user in snapshot.contestants))
            names = {user: mention_user(member, user) for user, member in zip(snapshot.contestants, members)}
            preview = score_embed(edition, node_id, snapshot, names)
            if request.reason:
                preview.add_field(name="**Reason**", value=request.reason)
        except (FinalError, ValidationError) as error:
            await ctx.send(f"{ERROR_EMOJI} " + (str(error) if isinstance(error, FinalError)
                                               else "Enter seven valid stars.destruction lines per player."), ephemeral=True)
            return
        controls = ipy.ActionRow(
            ipy.Button(label="Cancel", style=ipy.ButtonStyle.SECONDARY, custom_id=f"finalcancel|{ctx.id}"),
            ipy.Button(label="Confirm", style=ipy.ButtonStyle.SUCCESS, custom_id=f"finalconfirm|{ctx.id}"),
        )
        message = await ctx.send("Confirm this result?", embed=preview, components=controls, ephemeral=True)
        try:
            confirmation = (await self.bot.wait_for_component(
                components=controls, messages=int(message.id), timeout=180,
                check=lambda event: str(event.ctx.author.id) == owner,
            )).ctx
        except asyncio.TimeoutError:
            await ctx.edit(message, content=f"{ERROR_EMOJI} Confirmation expired. Use `/final match result` to try again.",
                           embeds=[], components=[])
            return
        await confirmation.defer(edit_origin=True)
        if confirmation.custom_id == f"finalcancel|{ctx.id}":
            await confirmation.edit_origin(content=f"{SUCCESS_EMOJI} Submission cancelled.", embeds=[], components=[])
            return
        try:
            edition = await self._edition(confirmation, int(season))
            if not any(confirmation.author.has_role(role) for role in roles):
                raise FinalError("A manager must correct results; staff may submit an initial result.")
            result = await submit_result(request)
        except (FinalError, ValidationError) as error:
            await confirmation.edit_origin(content=f"{ERROR_EMOJI} {error}", embeds=[], components=[])
            return
        except PyMongoError:
            logging.getLogger(__name__).exception("Final result confirmation failed")
            await confirmation.edit_origin(content=f"{ERROR_EMOJI} Could not confirm the save. "
                "Check `/final match view` before retrying.", embeds=[], components=[])
            return
        try:
            pending = await self._recover(edition)
            edition = await lifecycle.resolve_final(int(season))
        except (FinalError, PyMongoError) as error:
            edition.repair_pending = True
            pending = [f"Recovery is pending: {error}. Use `/final recover`."]
        guild = await member_guild(confirmation)
        members = await asyncio.gather(*(guild.fetch_member(user) for user in result.snapshot.contestants))
        names = {user: mention_user(member, user) for user, member in zip(result.snapshot.contestants, members)}
        await confirmation.edit_origin(content=f"{SUCCESS_EMOJI} Result saved."
                                       + (f" {IMPORTANT_EMOJI} " + " ".join(pending) if pending else ""),
                                       embed=result_embed(edition, result, names), components=[])

    @match_group.subcommand(sub_cmd_name="list", sub_cmd_description="List Final matches and threads")
    @slash_option(name="round", description="Round", opt_type=OptionType.STRING)
    @slash_option(name="season", description="Final season", opt_type=OptionType.INTEGER)
    async def final_match_list(self, ctx: SlashContext, round: str = None, season: int = None):
        from elobot.features.finals.types import ROUND_NODES
        try:
            edition = await self._edition(ctx, season)
            nodes = ROUND_NODES.get(round, tuple(edition.nodes))
            lines = [f"{node}: {edition.nodes[node].state} · "
                     + (f"<#{edition.nodes[node].thread_id}>" if edition.nodes[node].thread_id else "No thread")
                     for node in nodes]
            embed = ipy.Embed(title=f"**Season {edition.season_number} Final · Matches**", color=COLOR,
                              description="\n".join(lines) or "Create the bracket first.")
            embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
            await ctx.send(embed=embed)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @match_group.subcommand(sub_cmd_name="view", sub_cmd_description="Show a Final result")
    @slash_option(name="match", description="Match, e.g. QF1", opt_type=OptionType.STRING, required=True,
                  autocomplete=True)
    @slash_option(name="season", description="Final season", opt_type=OptionType.INTEGER)
    async def final_match_view(self, ctx: SlashContext, match: str, season: int = None):
        from elobot.features.finals.result_ui import result_embed
        from elobot.db.final_models import FinalMatchResult
        try:
            edition = await self._edition(ctx, season)
            node = edition.nodes.get(match)
            if node is None:
                raise FinalError("Choose a match from `/final match list`.")
            result = await FinalMatchResult.get(node.result_id) if node.result_id else None
            if result is None:
                await ctx.send(f"Season {edition.season_number} Final · {node.id}: {node.state}. No result.")
                return
            guild = await member_guild(ctx)
            members = await asyncio.gather(*(guild.fetch_member(user) for user in result.snapshot.contestants))
            names = {user: mention_user(member, user) for user, member in zip(result.snapshot.contestants, members)}
            await ctx.send(embed=result_embed(edition, result, names))
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @final_match_result.autocomplete("match")
    @final_match_forfeit.autocomplete("match")
    @final_match_view.autocomplete("match")
    @final_betting_announce.autocomplete("match")
    async def final_match_autocomplete(self, ctx: ipy.AutocompleteContext):
        try:
            edition = await self._edition(ctx, ctx.kwargs.get("season"))
            choices = [ipy.SlashCommandChoice(name=f"{node.id} · {node.state}", value=node.id)
                       for node in edition.nodes.values() if ctx.input_text.lower() in node.id.lower()]
        except FinalError:
            choices = []
        await ctx.send(choices=choices)

    @predictions_group.subcommand(sub_cmd_name="open", sub_cmd_description="Open bracket predictions before play")
    async def final_predictions_open(self, ctx: SlashContext):
        await self._prediction_state(ctx, True)

    @predictions_group.subcommand(sub_cmd_name="close", sub_cmd_description="Manually close prediction submissions")
    async def final_predictions_close(self, ctx: SlashContext):
        await self._prediction_state(ctx, False)

    async def _prediction_state(self, ctx, open_predictions):
        from elobot.features.finals.predictions import change_prediction_state
        try:
            edition = await self._edition(ctx, manager=True)
            edition = await change_prediction_state(edition, open_predictions)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await ctx.send(f"{SUCCESS_EMOJI} Season {edition.season_number} Final predictions "
                       f"{'opened' if open_predictions else 'closed'}.")

    @final.subcommand(sub_cmd_name="predict", sub_cmd_description="Create or resume your full bracket prediction")
    async def final_predict(self, ctx: SlashContext):
        from elobot.features.finals.predictions import begin_prediction
        from elobot.features.finals.prediction_ui import prediction_view
        await ctx.defer(ephemeral=True)
        try:
            edition = await self._edition(ctx, registered=True)
            prediction = await begin_prediction(edition, str(ctx.author.id))
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        guild = await member_guild(ctx)
        members = await asyncio.gather(*(guild.fetch_member(user) for user in edition.entrant_ids))
        names = {user: mention_user(member, user) for user, member in zip(edition.entrant_ids, members)}
        await ctx.send(components=prediction_view(edition, prediction, names), flags=ipy.MessageFlags.IS_COMPONENTS_V2)

    @ipy.component_callback(re.compile(r"^finalpred\|.+$"))
    async def final_prediction_action(self, ctx: ipy.ComponentContext):
        from elobot.features.finals.predictions import PredictionAction, apply_prediction_action
        from elobot.features.finals.prediction_ui import prediction_view
        _, season, owner, revision, action, node = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} This prediction belongs to another player.", ephemeral=True)
            return
        await ctx.defer(edit_origin=True)
        try:
            edition = await self._edition(ctx, int(season), registered=True)
            prediction = await apply_prediction_action(PredictionAction(
                final_id=edition.id, user_id=owner, session_revision=int(revision), action=action,
                node_id=None if node == "-" else node, choice=ctx.values[0] if action == "pick" else None,
            ))
            if action == "submit":
                await ctx.edit_origin(components=[ipy.ContainerComponent(
                    ipy.TextDisplayComponent(content=f"## Season {edition.season_number} Final\n{SUCCESS_EMOJI} Prediction submitted."),
                    accent_color=COLOR,
                )])
                return
            guild = await member_guild(ctx)
            members = await asyncio.gather(*(guild.fetch_member(user) for user in edition.entrant_ids))
            names = {user: mention_user(member, user) for user, member in zip(edition.entrant_ids, members)}
            await ctx.edit_origin(components=prediction_view(edition, prediction, names))
        except FinalError as error:
            await ctx.edit_origin(components=[ipy.ContainerComponent(ipy.TextDisplayComponent(
                content=f"{ERROR_EMOJI} {error}\nOpen `/final predict` to resume."), accent_color=COLOR)])

    @prediction_group.subcommand(sub_cmd_name="view", sub_cmd_description="View your submitted picks and accuracy")
    @slash_option(name="season", description="Final season", opt_type=OptionType.INTEGER)
    async def final_prediction_view(self, ctx: SlashContext, season: int = None):
        from elobot.features.finals.prediction_display import finished_results, prediction_alt_text, prediction_statuses
        from elobot.features.finals.rendering import render_bracket
        from elobot.db.final_models import FinalMatchResult, FinalPrediction
        await ctx.defer(ephemeral=True)
        try:
            edition = await self._edition(ctx, season, registered=True)
            prediction = await FinalPrediction.find_one({"final_id": str(edition.id), "user_id": str(ctx.author.id)})
            if prediction is None or not prediction.submitted:
                raise FinalError("No prediction submitted for this Final. Use `/final predict` while predictions are open.")
            statuses = prediction_statuses(edition, prediction)
            points, eligible = prediction.points, prediction.eligible
            if prediction.scoring_revision != edition.result_revision:
                points = sum(status == "correct" for status in statuses.values())
                eligible = sum(not node.bye and node.outcome == "Played" and node.winner is not None
                               and node.result_id is not None for node in edition.nodes.values())
            accuracy = 100 * points / eligible if eligible else 0
            result_ids = [node.result_id for node in edition.nodes.values() if node.result_id]
            results = await FinalMatchResult.find({"_id": {"$in": result_ids}}).to_list() if result_ids else []
            starts = {}
            for result in results:
                if result.snapshot.outcome != "Played":
                    continue
                started_at = getattr(result.snapshot, "actual_started_at", None)
                if started_at is not None:
                    starts[result.id] = started_at
            user_ids = {user for result in results for user in result.snapshot.contestants}
            guild = await member_guild(ctx)
            members = await asyncio.gather(*(guild.fetch_member(user_id) for user_id in user_ids))
            names = {user_id: mention_user(member, user_id) for user_id, member in zip(user_ids, members)}
            embed = ipy.Embed(title=f"**Season {edition.season_number} Final · Prediction**", color=COLOR,
                              description="✓ Correct  ✕ Incorrect  − Excluded")
            embed.add_field(name="**Predictions**", value=f"{points}/{eligible} → {accuracy:.2f}%")
            result_text = finished_results(edition, results, names, starts)
            if result_text:
                embed.description += f"\n\n**Results**\n{result_text}"
            embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
            try:
                rendered = await asyncio.to_thread(render_bracket, edition, prediction.submitted, statuses)
            except FinalError as error:
                embed.add_field(name="**Image**", value=str(error))
                embed.add_field(name="**Picks**", value="\n".join(
                    f"{node}: {edition.player_nicknames.get(user, user)} · {statuses.get(node, 'pending')}"
                    for node, user in prediction.submitted.items()))
                await ctx.send(embed=embed, ephemeral=True)
                return
            embed.set_image(url="attachment://prediction.png")
            with BytesIO(rendered) as image:
                await ctx.send(embed=embed, file=ipy.File(image, file_name="prediction.png",
                    description=prediction_alt_text(edition, prediction, statuses)), ephemeral=True)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)


def setup(bot):
    FinalCommands(bot)
