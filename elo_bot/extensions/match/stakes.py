import logging

import interactions as ipy
from pymongo.errors import PyMongoError

from elo_bot.config.emojis import ERROR_EMOJI, IMPORTANT_EMOJI, SUCCESS_EMOJI, TOKEN_EMOJI
from elo_bot.config.ids import ELO_BLACKLIST_ROLE
from elo_bot.core.finals import FinalError
from elo_bot.db import Match
from elo_bot.db.token_models import ParticipantStake
from elo_bot.extensions.match.groups import base
from elo_bot.features.tokens.stake_publication import STAKE_REPLY_PATTERN, publish_stake
from elo_bot.features.tokens.stakes import StakeReply, parse_stake, set_stake, validate_stake_reply


class MatchStakeCommands:
    @base.subcommand(sub_cmd_name="stake", sub_cmd_description="Put tokens at stake before the match starts")
    @ipy.slash_option(name="amount", description="Tokens to stake, all-in, or 0 to withdraw",
                      opt_type=ipy.OptionType.STRING, max_length=19, required=True)
    async def match_stake(self, ctx: ipy.SlashContext, amount: str) -> None:
        await ctx.defer(ephemeral=True)
        try:
            match = await self._stake_match(ctx)
            tokens = parse_stake(amount)
            user_id = str(ctx.author.id)
            stake = await set_stake(match, user_id, tokens)
            if stake is None or (not tokens and stake.state == "offer" and stake.author != user_id):
                raise FinalError("You don’t have a stake offer to withdraw.")
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await self._stake_receipt(ctx, match, stake)

    @ipy.component_callback(STAKE_REPLY_PATTERN)
    async def match_stake_reply(self, ctx: ipy.ComponentContext) -> None:
        await ctx.defer(ephemeral=True)
        _, action, match_id, revision = ctx.custom_id.split("|")
        try:
            match = await self._stake_match(ctx)
            if str(match.id) != match_id or ctx.message is None:
                raise FinalError("This stake offer is no longer open.")
            reply = StakeReply(int(revision), int(ctx.message.id))
            user_id = str(ctx.author.id)
            offer = validate_stake_reply(await ParticipantStake.get(match_id), user_id, reply)
            stake = await set_stake(match, user_id, offer.amount if action == "match" else 0, reply=reply)
            if stake is None:
                raise FinalError("This stake offer is no longer open.")
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await self._stake_receipt(ctx, match, stake, passed=action == "pass")

    async def _stake_match(self, ctx: ipy.SlashContext | ipy.ComponentContext) -> Match:
        if ctx.author.has_role(ELO_BLACKLIST_ROLE):
            raise FinalError("You can’t place stakes while Elo access is restricted.")
        match = await Match.find_one({"thread": str(ctx.channel_id)})
        if match is None:
            raise FinalError("Use `/match stake` in your match thread.")
        return match

    async def _stake_receipt(self, ctx: ipy.SlashContext | ipy.ComponentContext, match: Match,
                             stake: ParticipantStake, *, passed: bool = False) -> None:
        try:
            published = await publish_stake(self.bot, match)
        except (ipy.errors.HTTPException, PyMongoError):
            logging.exception("Stake announcement for match %s is pending", match.id)
            published = False
        if not published:
            content = f"{IMPORTANT_EMOJI} Stake saved. The thread update is pending."
        elif passed:
            content = f"{SUCCESS_EMOJI} You passed on the stake."
        elif stake.state == "locked":
            user_id = str(ctx.author.id)
            yours = stake.committed_amount(user_id)
            theirs = next(stake.committed_amount(user) for user in stake.users if user != user_id)
            if yours == theirs:
                content = f"{SUCCESS_EMOJI} Stakes fixed at {yours:,} {TOKEN_EMOJI} each."
            else:
                content = f"{SUCCESS_EMOJI} Stakes fixed. Yours {yours:,} {TOKEN_EMOJI} · Opponent’s {theirs:,} {TOKEN_EMOJI}."
        elif stake.state == "closed":
            content = f"{SUCCESS_EMOJI} Stake offer removed."
        else:
            content = f"{SUCCESS_EMOJI} Stake offer sent."
        await ctx.send(content, ephemeral=True)
