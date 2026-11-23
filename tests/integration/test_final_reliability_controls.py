from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elo_bot.config.emojis import ERROR_EMOJI, IMPORTANT_EMOJI, SUCCESS_EMOJI, TOKEN_EMOJI
from elo_bot.config.ids import LVL1_ROLES
from elo_bot.core.finals import FinalError
from elo_bot.db import Match, Participant
from elo_bot.features.finals.matches import prepare_match
from elo_bot.db.token_models import FinalBet, FinalBettingMarket, TokenWallet
from elo_bot.extensions.final import FinalCommands
from elo_bot.features.finals.lifecycle import confirm_bracket, final_for_view
from elo_bot.features.finals.recovery import recover_final
from elo_bot.features.tokens.bets import open_market
from elo_bot.features.tokens.presentation import market_signature
from tests.integration._final_fixture import FinalScenarioCase


class TestFinalReliabilityControls(FinalScenarioCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.final = await confirm_bracket(await self.roster(8), "manager")
        self.message = MagicMock(id=800, pin=AsyncMock(), edit=AsyncMock())
        self.channel = MagicMock(send=AsyncMock(return_value=self.message),
                                 fetch_message=AsyncMock(return_value=self.message))
        self.channel.guild.fetch_member = AsyncMock(return_value=None)
        self.bot = MagicMock(ext={}, fetch_channel=AsyncMock(return_value=self.channel))
        self.bot.user.id = 42
        self.cog = FinalCommands(self.bot)

    def context(self):
        ctx = MagicMock(guild_id=self.final.guild_id, id=100, send=AsyncMock(), defer=AsyncMock(),
                        edit_origin=AsyncMock(), values=[])
        ctx.author.id = 9
        ctx.author.has_role.side_effect = lambda role: role in LVL1_ROLES
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        return ctx

    async def test_committed_bet_refreshes_pool_and_failed_refresh_recovers_without_changing_the_bet(self):
        market = await open_market(self.final, "QF1")
        ctx = self.context()
        ctx.custom_id = f"betconfirm|{market.id}|9|1|0|1000"

        async def send_pool(**payload):
            self.assertEqual((await FinalBet.find_one({"user_id": "9"})).amount, 1000)
            self.assertEqual((await TokenWallet.get("9")).balance, 4000)
            ctx.edit_origin.assert_not_awaited()
            return self.message

        self.channel.send.side_effect = send_pool
        await self.cog.bet_confirm.callback.func(self.cog, ctx)
        saved = await FinalBettingMarket.get(market.id)
        self.assertEqual(saved.pools["1"], 1000)
        self.assertEqual(saved.announcement_signature, market_signature(saved, await final_for_view()))
        self.assertEqual(self.channel.send.await_args.kwargs["embed"].fields[0].value, f"1,000 {TOKEN_EMOJI}")
        self.assertEqual(ctx.edit_origin.await_args.kwargs["components"], [])
        bracket_message = MagicMock(id=801, pin=AsyncMock(), edit=AsyncMock())
        self.channel.send.side_effect = None
        self.channel.send.return_value = bracket_message
        self.message.edit.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        ctx.id = 101
        ctx.custom_id = f"betconfirm|{market.id}|9|1|1|2000"
        await self.cog.bet_confirm.callback.func(self.cog, ctx)
        saved = await FinalBettingMarket.get(market.id)
        self.assertEqual((await TokenWallet.get("9")).balance, 3000)
        self.assertEqual(saved.pools["1"], 2000)
        self.assertIsNotNone(saved.publication_error)
        receipt = ctx.edit_origin.await_args.kwargs
        self.assertIn(str(SUCCESS_EMOJI), receipt["content"])
        self.assertIn(str(IMPORTANT_EMOJI), receipt["content"])
        self.assertEqual(receipt["components"], [])
        self.message.edit.side_effect = None
        await recover_final(await final_for_view(), bot=self.bot)
        saved = await FinalBettingMarket.get(market.id)
        self.assertIsNone(saved.publication_error)
        self.assertEqual(saved.announcement_signature, market_signature(saved, await final_for_view()))
        self.assertEqual(self.message.edit.await_args.kwargs["embed"].fields[0].value, f"2,000 {TOKEN_EMOJI}")
        self.assertEqual(self.channel.send.await_count, 2)
        self.assertNotEqual((await final_for_view()).bracket_message_id, saved.announcement_id)

    async def test_round_attempts_later_matches_after_domain_and_discord_failures(self):
        await Match(token="occupied", matched_time=1,
                    participants=[Participant(user_id="2"), Participant(user_id="9")]).insert()
        with self.assertRaises(FinalError) as occupied:
            await prepare_match(self.final, "QF2")
        denied = ipy.errors.Forbidden(MagicMock(status=403, reason="Fixture refusal"))
        attempted = []

        async def confirm(bot, token):
            source = await Match.find_one({"token": token})
            attempted.append(source.final_node_id)
            if source.final_node_id == "QF1":
                raise denied
            thread = "777" if source.final_node_id == "QF3" else "778"
            await source.set({"thread": thread})
            return thread

        ctx = self.context()
        with patch("elo_bot.extensions.final.rounds.lifecycle_module.confirm_match", new=AsyncMock(side_effect=confirm)):
            await self.cog.final_round_start.callback.func(self.cog, ctx)
        current = await final_for_view()
        self.assertEqual(attempted, ["QF1", "QF3", "QF4"])
        self.assertIsNone(current.nodes["QF1"].thread_id)
        self.assertIsNone(current.nodes["QF2"].match_id)
        self.assertEqual(current.nodes["QF3"].thread_id, "777")
        self.assertEqual(current.nodes["QF4"].thread_id, "778")
        response = ctx.send.await_args.args[0]
        self.assertIn(str(denied), response)
        self.assertIn(str(occupied.exception), response)
        self.assertIn(f"`QF1` {ERROR_EMOJI}", response)
        self.assertIn(f"`QF2` {ERROR_EMOJI}", response)
        self.assertIn("`QF3` <#777>", response)
        self.assertIn("`QF4` <#778>", response)
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
