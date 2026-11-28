import time
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elo_bot.config.emojis import IMPORTANT_EMOJI, SUCCESS_EMOJI
from elo_bot.core.finals import FINAL_THREAD_RETENTION, NODE_IDS
from elo_bot.db import BaseStore, EloFinal, Match
from elo_bot.db.token_models import FinalBettingMarket, TokenWallet
from elo_bot.extensions.thread import ThreadCommands
from elo_bot.features.finals.lifecycle import confirm_bracket, final_for_view
from elo_bot.features.finals.matches import bind_thread, prepare_match
from elo_bot.features.finals.publication import publish_bracket
from elo_bot.features.finals.results import FinalResultSubmission, submit_result
from elo_bot.features.tokens.bets import BetAction, apply_bet, open_market
from tests.integration._final_ui_fixture import FinalUICase


class TestFinalConfirmations(FinalUICase):
    async def test_cancellation_needs_confirmation_refunds_and_preserves_slots_until_thread_deletion(self):
        self.final = await confirm_bracket(self.final, "9")
        await publish_bracket(self.bot, self.final)
        await self.cog._set_predictions(self.final, True)
        market = await open_market(self.final, "QF1")
        await apply_bet(BetAction(market_id=market.id, user_id="9", contestant="1", amount=1000,
                                 expected_version=0, action_key="cancel-stake"))
        source = await prepare_match(self.final, "QF1")
        store = BaseStore(clan_tag="#OWNED", slot="Slot 1", reservation_id="a" * 16, ready=True)
        await source.set({"thread": "777", "participants.0.base_store": store.model_dump(),
                          "participants.0.opponent_bases_stored": True})
        await bind_thread(self.final, source, "777")
        thread = MagicMock(id=777, delete=AsyncMock(), edit=AsyncMock())
        fetch_public = self.bot.fetch_channel.side_effect

        async def channel(channel_id):
            return thread if channel_id == 777 else self.channel

        self.bot.fetch_channel.side_effect = channel
        ctx = self.context()
        await self.cog.final_cancel.callback.func(self.cog, ctx, "Fixture cancelled")
        self.assertEqual((await final_for_view()).state, "BRACKET_READY")
        self.assertEqual((await TokenWallet.get("9")).balance, 4000)
        prompt = ctx.send.await_args.kwargs
        self.assertIn("1 match thread is", prompt["embed"].description)
        self.assertEqual(prompt["components"][0].style, ipy.ButtonStyle.DANGER)
        confirm = self.context(component=True)
        confirm.custom_id = prompt["components"][0].custom_id
        confirm.message.embeds = [prompt["embed"]]
        await self.cog.final_management_confirmation.callback.func(self.cog, confirm)
        current = await final_for_view()
        self.assertEqual(current.state, "CANCELLED")
        self.assertFalse(current.active)
        self.assertEqual((await FinalBettingMarket.get(market.id)).state, "REFUNDED")
        self.assertEqual((await TokenWallet.get("9")).balance, 5000)
        self.assertEqual((await Match.get(source.id)).participants[0].base_store, store)
        self.assertTrue((await Match.get(source.id)).final_superseded)
        self.assertEqual(confirm.edit_origin.await_args.kwargs["content"],
                         f"{SUCCESS_EMOJI} Final cancelled. 1 market refunded. Match threads are archived and will be "
                         f"deleted <t:{current.thread_cleanup_at}:R>.")
        thread.edit.assert_awaited_once_with(archived=True, locked=True)
        self.assertAlmostEqual(current.thread_cleanup_at, time.time() + FINAL_THREAD_RETENTION, delta=60)
        bracket = self.posts[current.bracket_message_id].edit.await_args.kwargs
        self.assertIn("Cancelled", bracket["embed"].description)
        self.assertEqual(next(field.value for field in bracket["embed"].fields if field.name == "**Predictions**"),
                         "Closed · 0 submitted")
        self.assertEqual(bracket["components"], [])
        self.assertFalse(current.publication_pending)
        delete = self.context(component=True)
        delete.custom_id = confirm.edit_origin.await_args.kwargs["components"][0].custom_id
        await self.cog.final_delete_threads.callback.func(self.cog, delete)
        thread.delete.assert_awaited_once()
        self.assertEqual((await Match.get(source.id)).participants[0].base_store, store)
        self.assertEqual(delete.edit_origin.await_args.kwargs["content"], f"{SUCCESS_EMOJI} Deleted 1 match thread.")
        listener = ThreadCommands(MagicMock(ext={}))

        async def release(match, **kwargs):
            self.assertEqual(kwargs["reason"], "thread_deleted")
            await Match.get_pymongo_collection().update_one({"_id": match.id}, {"$set": {
                "participants.0.base_store": None, "participants.0.opponent_bases_stored": False}})

        with patch("elo_bot.extensions.thread.clear_match_base_stores", new=AsyncMock(side_effect=release)), patch(
                "elo_bot.extensions.thread.close_runs_for_thread", new=AsyncMock()), patch(
                "elo_bot.extensions.thread.free_match_clans", new=AsyncMock()), patch(
                "elo_bot.extensions.thread.remove_helper_task", new=AsyncMock()):
            await listener.on_thread_delete.callback(listener, ipy.events.ThreadDelete(thread=thread))
        self.assertIsNone(await Match.get(source.id))
        self.bot.fetch_channel.side_effect = fetch_public

    async def test_back_and_foreign_cancel_controls_leave_the_edition_unchanged(self):
        ctx = self.context()
        await self.cog.final_cancel.callback.func(self.cog, ctx, "Fixture cancellation")
        prompt = ctx.send.await_args.kwargs
        before = (await EloFinal.get(self.final.id)).model_dump()
        foreign = self.context(8, component=True)
        foreign.custom_id = prompt["components"][0].custom_id
        await self.cog.final_management_confirmation.callback.func(self.cog, foreign)
        self.assertIn("another user's", foreign.send.await_args.args[0])
        back = self.context(component=True)
        back.custom_id = prompt["components"][1].custom_id
        await self.cog.final_management_confirmation.callback.func(self.cog, back)
        self.assertEqual((await EloFinal.get(self.final.id)).model_dump(), before)
        self.assertEqual(back.edit_origin.await_args.kwargs["embed"].title, "**Dashboard**")

    async def test_a_prompt_drawn_before_a_result_changed_returns_to_the_current_dashboard(self):
        ctx = self.context()
        await self.cog.final_cancel.callback.func(self.cog, ctx, "Fixture cancellation")
        prompt = ctx.send.await_args.kwargs
        await (await EloFinal.get(self.final.id)).set({"result_revision": self.final.result_revision + 1})
        for button in prompt["components"]:
            with self.subTest(button=button.label):
                click = self.context(component=True)
                click.custom_id = button.custom_id
                await self.cog.final_management_confirmation.callback.func(self.cog, click)
                shown = click.edit_origin.await_args.kwargs
                self.assertEqual(shown["embed"].title, "**Dashboard**")
                self.assertEqual(shown["content"].startswith(str(IMPORTANT_EMOJI)), button.label == "Cancel Final")
        current = await EloFinal.get(self.final.id)
        self.assertEqual((current.active, current.state), (True, self.final.state))

    async def test_expired_threads_are_deleted_once_and_retried_after_a_failure(self):
        self.final = await confirm_bracket(self.final, "9")
        source = await prepare_match(self.final, "QF1")
        await source.set({"thread": "777"})
        thread = MagicMock(id=777, delete=AsyncMock(side_effect=[ipy.errors.HTTPException(MagicMock(status=500)), None]))
        self.bot.fetch_channel = AsyncMock(return_value=thread)
        final = await EloFinal.get(self.final.id)
        await final.set({"thread_cleanup_at": int(time.time()) + 60})
        await self.cog._delete_expired_threads()
        thread.delete.assert_not_awaited()
        await final.set({"thread_cleanup_at": int(time.time()) - 1})
        await self.cog._delete_expired_threads()
        self.assertIsNotNone((await EloFinal.get(self.final.id)).thread_cleanup_at)
        await self.cog._delete_expired_threads()
        self.assertEqual(thread.delete.await_count, 2)
        self.assertIsNone((await EloFinal.get(self.final.id)).thread_cleanup_at)

    async def test_end_requires_confirmation_then_closes_predictions_and_keeps_champion(self):
        self.final = await confirm_bracket(self.final, "9")
        await publish_bracket(self.bot, self.final)
        for node_id in NODE_IDS:
            current = await final_for_view()
            node = current.nodes[node_id]
            await submit_result(FinalResultSubmission(final_id=current.id, node_id=node_id, actor_id="9",
                operation_key=f"end:{node_id}", expected_version=0, contestant_revision=node.contestant_revision,
                attempt=node.attempt, scores={user: ["0.00"] * 7 for user in node.contestants},
                outcome="Forfeit", winner=node.contestants[0], reason="Fixture forfeit"))
            await self.cog._recover(await final_for_view())
        before = await final_for_view()
        ctx = self.context()
        await self.cog.final_end.callback.func(self.cog, ctx)
        self.assertTrue((await final_for_view()).active)
        prompt = ctx.send.await_args.kwargs
        self.assertIn("Results can't be changed afterwards", prompt["embed"].description)
        confirm = self.context(component=True)
        confirm.custom_id = prompt["components"][0].custom_id
        await self.cog.final_management_confirmation.callback.func(self.cog, confirm)
        current = await final_for_view()
        self.assertEqual(current.state, "COMPLETED")
        self.assertFalse(current.active)
        self.assertEqual(current.champion, before.champion)
        self.assertEqual(current.prediction_state, "CLOSED")
        self.assertIn("ended. Champion:", confirm.edit_origin.await_args.kwargs["content"])

    async def test_failed_thread_delete_retains_its_reservation_and_a_retry_control(self):
        self.final = await confirm_bracket(self.final, "9")
        source = await prepare_match(self.final, "QF1")
        store = BaseStore(clan_tag="#OWNED", slot="Slot 1", reservation_id="b" * 16, ready=True)
        await source.set({"thread": "777", "participants.0.base_store": store.model_dump()})
        await self.final.set({"active": False, "state": "CANCELLED"})
        thread = MagicMock(id=777, delete=AsyncMock(side_effect=ipy.errors.Forbidden(
            MagicMock(status=403, reason="Fixture refusal"))))
        self.bot.fetch_channel.return_value = thread
        ctx = self.context(component=True)
        ctx.custom_id = f"finalthreads|{self.final.id}|9"
        await self.cog.final_delete_threads.callback.func(self.cog, ctx)
        self.assertEqual((await Match.get(source.id)).participants[0].base_store, store)
        payload = ctx.edit_origin.await_args.kwargs
        self.assertIn("Stored bases remain reserved".lower(), payload["content"].lower())
        self.assertEqual(payload["components"][0].custom_id, ctx.custom_id)
