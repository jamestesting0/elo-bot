from elo_bot.config.emojis import ERROR_EMOJI, TOKEN_EMOJI
from elo_bot.db.token_models import FinalBet, TokenWallet
from elo_bot.features.finals.lifecycle import confirm_bracket
from elo_bot.features.tokens.bets import BetAction, apply_bet, close_market, open_market
from tests.integration._final_ui_fixture import FinalUICase


class TestFinalBetPanel(FinalUICase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.final = await confirm_bracket(self.final, "9")
        self.market = await open_market(self.final, "QF1")

    async def test_modal_review_back_and_receipt_edit_one_ephemeral_message(self):
        opened, transport = self.library_context(f"betround|{self.final.id}|Quarterfinals", selection=self.market.id)
        await self.cog.bet_round_open.callback.func(self.cog, opened)
        self.assertEqual(transport.http.post_initial_response.await_args.args[0]["type"], 5)
        self.assertEqual(transport.http.post_initial_response.await_args.args[0]["data"]["flags"], 64)
        chooser = transport.http.edit_interaction_message.await_args.args[0]
        choose_id = chooser["components"][0]["components"][0]["custom_id"]
        choose, modal_transport = self.library_context(choose_id)
        await self.cog.bet_choose.callback.func(self.cog, choose)
        modal = modal_transport.http.post_initial_response.await_args.args[0]
        self.assertEqual(modal["type"], 9)
        self.assertEqual(modal["data"]["title"], "Bet on Player 1")
        self.assertEqual(modal["data"]["components"][0]["components"][0]["placeholder"], "Available: 5,000")
        amount, review_transport = self.library_context(modal["data"]["custom_id"], modal=True)
        await self.cog.bet_amount.callback.func(self.cog, amount, "1000")
        self.assertEqual(review_transport.http.post_initial_response.await_args.args[0]["type"], 6)
        review = review_transport.http.edit_interaction_message.await_args.args[0]
        self.assertEqual(review["embeds"][0]["fields"], [{"name": "**Estimated Profit**", "value": f"0 {TOKEN_EMOJI}", "inline": False}])
        back, back_transport = self.library_context(review["components"][0]["components"][1]["custom_id"])
        await self.cog.bet_back.callback.func(self.cog, back)
        self.assertEqual(back_transport.http.post_initial_response.await_args.args[0]["type"], 6)
        self.assertIn("Available Tokens", back_transport.http.edit_interaction_message.await_args.args[0]["embeds"][0]["fields"][0]["name"])
        self.assertIsNone(await FinalBet.find_one({"user_id": "9"}))
        confirm, receipt_transport = self.library_context(review["components"][0]["components"][0]["custom_id"])
        await self.cog.bet_confirm.callback.func(self.cog, confirm)
        receipt = receipt_transport.http.edit_interaction_message.await_args.args[0]
        self.assertEqual(receipt["components"], [])
        self.assertEqual(receipt["embeds"], [])
        self.assertEqual((await TokenWallet.get("9")).balance, 4000)
        self.assertEqual((await FinalBet.find_one({"user_id": "9"})).amount, 1000)
        for client in (transport, modal_transport, review_transport, back_transport, receipt_transport):
            client.http.post_followup.assert_not_awaited()

    async def test_panel_shows_the_current_bet_with_its_estimated_profit(self):
        side, other = self.market.contestants
        for user, contestant, amount in (("9", side, 500), ("2", other, 1000)):
            await apply_bet(BetAction(market_id=self.market.id, user_id=user, contestant=contestant, amount=amount,
                                      expected_version=0, action_key=f"panel:{user}"))
        panel = self.context(component=True)
        panel.custom_id = f"betback|{self.market.id}|9"
        await self.cog.bet_back.callback.func(self.cog, panel)
        embed = panel.edit_origin.await_args.kwargs["embed"]
        self.assertIn(f"500 {TOKEN_EMOJI} · ×3.00", embed.description)
        self.assertIn(f"1,000 {TOKEN_EMOJI} · ×1.50", embed.description)
        self.assertTrue(embed.description.splitlines()[-1].endswith(f" · 500 {TOKEN_EMOJI}"))
        self.assertEqual([(field.name, field.value) for field in embed.fields][:2],
                         [("**Estimated Profit**", f"1,000 {TOKEN_EMOJI}"), ("**Available Tokens**", f"4,500 {TOKEN_EMOJI}")])

    async def test_market_closing_during_modal_retires_the_origin_without_a_bet(self):
        await close_market(self.market.id)
        ctx, transport = self.library_context(f"betamount|{self.market.id}|9|1|0", modal=True)
        await self.cog.bet_amount.callback.func(self.cog, ctx, "1000")
        payload = transport.http.edit_interaction_message.await_args.kwargs["payload"]
        self.assertEqual(payload["content"], f"{ERROR_EMOJI} Betting is closed for this match.")
        self.assertEqual(payload["components"], [])
        self.assertEqual(payload["embeds"], [])
        transport.http.post_followup.assert_not_awaited()
        self.assertIsNone(await FinalBet.find_one({"user_id": "9"}))

    async def test_cancel_review_and_back_leave_stake_until_cancel_is_confirmed(self):
        ctx = self.context(component=True)
        ctx.custom_id = f"betconfirm|{self.market.id}|9|1|0|500"
        await self.cog.bet_confirm.callback.func(self.cog, ctx)
        review = self.context(component=True)
        review.custom_id = f"betcancel|{self.market.id}|9|1|1"
        await self.cog.bet_cancel_review.callback.func(self.cog, review)
        payload = review.edit_origin.await_args.kwargs
        self.assertEqual(payload["embed"].description, f"Cancel bet · Refund: 500 {TOKEN_EMOJI}")
        self.assertEqual((await TokenWallet.get("9")).balance, 4500)
        confirm = self.context(component=True)
        confirm.custom_id = payload["components"][0].custom_id
        await self.cog.bet_confirm.callback.func(self.cog, confirm)
        self.assertIn(f"Bet cancelled. 500 {TOKEN_EMOJI} returned.", confirm.edit_origin.await_args.kwargs["content"])
        self.assertEqual((await TokenWallet.get("9")).balance, 5000)
        self.assertFalse((await FinalBet.find_one({"user_id": "9"})).active)
