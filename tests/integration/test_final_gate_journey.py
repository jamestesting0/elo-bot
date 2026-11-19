import time
from unittest.mock import AsyncMock, MagicMock, patch

from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elo_bot.db import BaseStore, Match, Season
from elo_bot.db.final_models import FinalPrediction
from elo_bot.db.token_models import FinalBettingMarket, TokenWallet
from elo_bot.features.finals.hosting import accept_final_start
from elo_bot.features.finals.lifecycle import final_for_view
from elo_bot.features.finals.matches import schedule_final_match
from elo_bot.features.tokens.bets import BetAction, apply_bet
from tests.integration._final_ui_fixture import FinalUICase


class TestFinalGateJourney(FinalUICase):
    async def setup_bracket(self):
        start = self.context()
        await self.cog.final_start.callback.func(self.cog, start)
        self.final = await final_for_view()
        for user in range(1, 9):
            await self.cog.final_player_add.callback.func(self.cog, self.context(),
                player=MagicMock(id=user), nickname=f"Player {user}")
        editor = self.context()
        await self.cog.final_bracket_create.callback.func(self.cog, editor)
        swap = self.context(component=True)
        swap.custom_id = editor.send.await_args.kwargs["components"][0].components[0].custom_id
        swap.values = ["QF1:0", "QF2:0"]
        await self.cog.final_pairing_action.callback.func(self.cog, swap)
        reset = self.context(component=True)
        reset.custom_id = swap.edit_origin.await_args.kwargs["components"][1].components[0].custom_id
        await self.cog.final_pairing_action.callback.func(self.cog, reset)
        confirm = self.context(component=True)
        confirm.custom_id = reset.edit_origin.await_args.kwargs["components"][1].components[1].custom_id
        await self.cog.final_pairing_action.callback.func(self.cog, confirm)
        self.final = await final_for_view()
        self.assertEqual(self.final.entrant_ids, [str(user) for user in range(1, 9)])
        self.assertEqual(self.final.nodes["QF1"].contestants, ["1", "8"])
        self.assertTrue(confirm.edit_origin.await_args.kwargs["content"].startswith(str(SUCCESS_EMOJI)))
        self.assertIsNotNone(self.final.bracket_message_id)

    async def record_result(self, node_id):
        edition = await final_for_view()
        source = await Match.get(edition.nodes[node_id].match_id)
        if not all(source.scheduled_times):
            source = await schedule_final_match(source, int(time.time()) + 3600, player=0)
            await schedule_final_match(source, int(time.time()) + 7200, player=1)
        opened = self.context()
        await self.cog.final_match_result.callback.func(self.cog, opened, match=node_id)
        modal = self.context()
        modal.custom_id = opened.send_modal.await_args.args[0].custom_id
        await self.cog.final_result_modal.callback.func(self.cog, modal, player1="\n".join(["3.100"] * 7),
            player2="\n".join(["2.80"] * 7))
        confirm = self.context(component=True)
        confirm.custom_id = modal.send.await_args.kwargs["components"][-1].components[1].custom_id
        await self.cog.final_result_confirmation.callback.func(self.cog, confirm)
        self.assertEqual(confirm.edit_origin.await_args.kwargs["components"], [])
        return confirm

    async def test_complete_final_then_cancel_a_second_final_through_current_callbacks(self):
        self.bot.http.request = AsyncMock(return_value={})
        await self.final.delete()
        await self.setup_bracket()
        print("GATE_JOURNEY setup/editor/reset/confirm: BRACKET_READY, bracket published")
        await self.cog.final_predictions_open.callback.func(self.cog, self.context())
        edition = await final_for_view()
        predict = self.context(component=True)
        bracket = self.posts[edition.bracket_message_id]
        predict.custom_id = (bracket.edit.await_args.kwargs if bracket.edit.await_args else bracket.payload)["components"][0].components[0].custom_id
        self.assertEqual(predict.custom_id, f"finalpredict|{edition.id}")
        await self.cog.final_prediction_open.callback.func(self.cog, predict)
        payload = predict.send.await_args.kwargs["components"]
        picks = {}
        for count in (4, 2, 1, 0):
            for _ in range(count):
                controls = [control for row in payload for control in row.to_dict()["components"]]
                menu = next(control for control in controls if control["type"] == 3
                            and not any(option.get("default") for option in control["options"]))
                action = self.context(component=True)
                action.custom_id = menu["custom_id"]
                action.values = [menu["options"][0]["value"]]
                picks[action.custom_id.split("|")[-1]] = action.values[0]
                await self.cog.final_prediction_action.callback.func(self.cog, action)
                payload = action.edit.await_args.kwargs["components"]
            controls = [control for row in payload for control in row.to_dict()["components"]]
            button = next(control for control in controls if control["type"] == 2
                          and f"|{'next' if count else 'submit'}|" in control["custom_id"])
            action = self.context(component=True)
            action.custom_id = button["custom_id"]
            await self.cog.final_prediction_action.callback.func(self.cog, action)
            payload = action.edit.await_args.kwargs["components"]
        prediction = await FinalPrediction.get(f"{self.final.id}:9")
        self.assertEqual(prediction.submitted, picks)
        self.assertEqual(len(picks), 7)
        print("GATE_JOURNEY predictions: seven picks submitted through public panel and wizard")
        next_thread = 1000

        async def create_thread(bot, token):
            nonlocal next_thread
            source = await Match.find_one({"token": token})
            next_thread += 1
            await source.set({"thread": str(next_thread)})
            return source.thread

        with patch("elo_bot.extensions.final.rounds.lifecycle_module.confirm_match", new=AsyncMock(side_effect=create_thread)):
            await self.cog.final_round_start.callback.func(self.cog, self.context())
            market = await FinalBettingMarket.find_one({"final_id": str(self.final.id), "node_id": "QF1"})
            bet = self.context(component=True)
            bet.custom_id = self.posts[market.announcement_id].payload["components"][0].custom_id
            await self.cog.bet_open.callback.func(self.cog, bet)
            choose = self.context(component=True)
            choose.custom_id = bet.send.await_args.kwargs["components"][0].custom_id
            await self.cog.bet_choose.callback.func(self.cog, choose)
            modal, client = self.library_context(choose.send_modal.await_args.args[0].custom_id, modal=True)
            await self.cog.bet_amount.callback.func(self.cog, modal, amount="1000")
            confirm = self.context(component=True)
            confirm.custom_id = client.http.edit_interaction_message.await_args.args[0]["components"][0]["components"][0]["custom_id"]
            await self.cog.bet_confirm.callback.func(self.cog, confirm)
            self.assertEqual((await TokenWallet.get("9")).balance, 4000)
            print("GATE_JOURNEY round/bet/modal/review/receipt: four markets published, 1000 escrowed")
            await self.cog.final_predictions_close.callback.func(self.cog, self.context())
            source = await Match.get((await final_for_view()).nodes["QF1"].match_id)
            source = await schedule_final_match(source, int(time.time()) + 3600, player=0)
            source = await schedule_final_match(source, int(time.time()) + 7200, player=1)
            await accept_final_start(source, "9")
            self.assertEqual((await FinalBettingMarket.get(market.id)).state, "CLOSED")
            print("GATE_JOURNEY accepted hosting: predictions closed, QF1 market closed")
            tie = self.context()
            await self.cog.final_match_result.callback.func(self.cog, tie, match="QF1")
            tied = self.context()
            tied.custom_id = tie.send_modal.await_args.args[0].custom_id
            await self.cog.final_result_modal.callback.func(self.cog, tied, player1="\n".join(["3.100"] * 7),
                                                           player2="\n".join(["3.100"] * 7))
            self.assertIn(str(ERROR_EMOJI), str(tied.send.await_args))
            self.assertIsNone((await final_for_view()).nodes["QF1"].result_id)
            await self.record_result("QF1")
            self.assertEqual((await FinalBettingMarket.get(market.id)).state, "PAID")
            for node in ("QF2", "QF3", "QF4"):
                await self.record_result(node)
            print("GATE_JOURNEY refused tie/results: Quarterfinals decided, QF1 payout committed")
            for nodes in (("SF1", "SF2"), ("F1",)):
                await self.cog.final_round_start.callback.func(self.cog, self.context())
                for node in nodes:
                    await self.record_result(node)
            self.assertEqual((await final_for_view()).champion, "1")
            prompt = self.context()
            await self.cog.final_end.callback.func(self.cog, prompt)
            end = self.context(component=True)
            end.custom_id = prompt.send.await_args.kwargs["components"][0].custom_id
            await self.cog.final_management_confirmation.callback.func(self.cog, end)
            completed = await final_for_view()
            self.assertEqual((completed.state, completed.active), ("COMPLETED", False))
            print("GATE_JOURNEY Semifinals/Final/champion/end: COMPLETED, champion 1")
            await Season(season_number=7, start=2, end=3, players=self.archive.players).insert()
            await self.setup_bracket()
            await self.cog.final_round_start.callback.func(self.cog, self.context())
            source = await Match.get((await final_for_view()).nodes["QF1"].match_id)
            store = BaseStore(clan_tag="#OWNED", slot="Slot 1", reservation_id="a" * 16, ready=True)
            await source.set({"participants.0.base_store": store.model_dump()})
            market = await FinalBettingMarket.find_one({"final_id": str(self.final.id), "node_id": "QF1"})
            await apply_bet(BetAction(market_id=market.id, user_id="9", contestant="1", amount=500,
                                     expected_version=0, action_key="second-final-stake"))
            self.assertEqual((await TokenWallet.get("9")).balance, 4500)
            prompt = self.context()
            await self.cog.final_cancel.callback.func(self.cog, prompt, reason="Fixture mid-round cancellation")
            cancel = self.context(component=True)
            cancel.custom_id = prompt.send.await_args.kwargs["components"][0].custom_id
            cancel.message.embeds = [prompt.send.await_args.kwargs["embed"]]
            await self.cog.final_management_confirmation.callback.func(self.cog, cancel)
            cancelled = await final_for_view()
            self.assertEqual((cancelled.state, cancelled.active), ("CANCELLED", False))
            self.assertEqual((await Match.get(source.id)).participants[0].base_store, store)
            self.assertEqual((await TokenWallet.get("9")).balance, 5000)
            self.assertEqual({market.state for market in await FinalBettingMarket.find(
                {"final_id": str(cancelled.id)}).to_list()}, {"REFUNDED"})
            self.assertEqual(cancel.edit_origin.await_args.kwargs["components"][0].custom_id,
                             f"finalthreads|{cancelled.id}|9")
            print("GATE_JOURNEY second Final: CANCELLED, four markets refunded, reservation retained")
