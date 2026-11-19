from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import anyio
import interactions as ipy

from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elo_bot.core.finals import FinalError
from elo_bot.db.final_draft_models import FinalResultDraft
from elo_bot.db.final_models import FinalMatchResult
from elo_bot.extensions.final import FinalCommands
from elo_bot.features.finals.lifecycle import confirm_bracket, final_for_view, reset_bracket
from elo_bot.features.finals.matches import bind_thread
from elo_bot.features.finals.result_drafts import finish_result_draft
from elo_bot.features.tokens.bets import BetAction, apply_bet, open_market
from elo_bot.db.token_models import FinalBettingMarket, TokenWallet
from tests.integration._final_ui_fixture import FinalUICase


class TestFinalResultDrafts(FinalUICase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.final = await confirm_bracket(self.final, "9")
        source = await self.scheduled_pairing(self.final, "QF1")
        await source.set({"thread": "777"})
        await bind_thread(self.final, source, "777")
        self.final = await final_for_view()

    async def preview(self):
        node = self.final.nodes["QF1"]
        ctx = self.context()
        ctx.custom_id = f"finalres|{self.final.id}|QF1|9|0|{node.contestant_revision}|{node.attempt}|-|0"
        await self.cog.final_result_modal.callback.func(self.cog, ctx, player1="\n".join(["3.100"] * 7),
                                                       player2="\n".join(["2.80"] * 7))
        return ctx, await FinalResultDraft.find_one({"state": "PENDING"})

    async def test_restart_and_duplicate_confirmation_preserve_one_result_and_retire_controls(self):
        preview, draft = await self.preview()
        self.assertEqual(await FinalMatchResult.count(), 0)
        self.assertEqual(draft.snapshot.scores["1"], ["3.100"] * 7)
        indexes = await FinalResultDraft.get_pymongo_collection().index_information()
        self.assertEqual(indexes["final_result_draft_expiry"]["expireAfterSeconds"], 0)
        self.assertEqual(indexes["final_result_draft_expiry"]["key"], [("expires_at", 1)])
        self.assertEqual(preview.send.await_args.kwargs["components"][-1].components[1].custom_id, f"finalconfirm|{draft.id}")
        restarted_bot = MagicMock(ext={}, fetch_channel=self.bot.fetch_channel)
        restarted_bot.user.id = 42
        cog = FinalCommands(restarted_bot)
        ctx = self.context(component=True)
        ctx.custom_id = f"finalconfirm|{draft.id}"
        await cog.final_result_confirmation.callback.func(cog, ctx)
        self.assertEqual(await FinalMatchResult.count(), 1)
        self.assertEqual((await FinalResultDraft.get(draft.id)).state, "CONFIRMED")
        self.assertEqual((await final_for_view()).nodes["QF1"].winner, "1")
        receipt = ctx.edit_origin.await_args.kwargs
        self.assertTrue(receipt["content"].startswith(str(SUCCESS_EMOJI)))
        self.assertIn("https://discordapp.com/users/1", receipt["content"])
        self.assertIn("SF1", receipt["content"])
        self.assertEqual(receipt["components"], [])
        self.assertEqual((await FinalMatchResult.find_all().first_or_none()).snapshot.scores["1"], ["3.100"] * 7)
        second = self.context(component=True)
        second.custom_id = ctx.custom_id
        await cog.final_result_confirmation.callback.func(cog, second)
        self.assertEqual(await FinalMatchResult.count(), 1)
        self.assertEqual((await final_for_view()).nodes["QF1"].result_version, 1)

    def edit_context(self, draft, side, district, user=9):
        ctx = self.context(user)
        ctx.__class__ = ipy.ModalContext
        ctx.custom_id = f"finaleditscore|{draft.id}|{side}|{district}"
        return ctx

    async def test_edited_score_redraws_the_winner_and_is_the_one_confirmed(self):
        preview, draft = await self.preview()
        first, second = draft.snapshot.contestants
        select = preview.send.await_args.kwargs["components"][0].components[0]
        self.assertEqual(len(select.options), 14)
        opener = self.context(component=True)
        opener.custom_id, opener.values = select.custom_id, ["0|6"]
        await self.cog.final_result_edit.callback.func(self.cog, opener)
        modal = opener.send_modal.await_args.args[0]
        self.assertEqual((modal.custom_id, modal.components[0].value), (f"finaleditscore|{draft.id}|0|6", "3.100"))
        for district in range(7):
            await self.cog.final_result_edit_score.callback.func(self.cog, self.edit_context(draft, 0, district),
                                                                 score="0.00")
        edited = await FinalResultDraft.get(draft.id)
        self.assertEqual(edited.snapshot.scores[first], ["0.00"] * 7)
        self.assertEqual(edited.snapshot.scores[second], ["2.80"] * 7)
        self.assertEqual(edited.snapshot.winner, second)
        confirmation = self.context(component=True)
        confirmation.custom_id = f"finalconfirm|{draft.id}"
        await self.cog.final_result_confirmation.callback.func(self.cog, confirmation)
        saved = await FinalMatchResult.find_all().first_or_none()
        self.assertEqual((saved.snapshot.scores[first], saved.snapshot.winner), (["0.00"] * 7, second))

    async def test_invalid_foreign_expired_and_confirmed_edits_never_write(self):
        _, draft = await self.preview()
        first = draft.snapshot.contestants[0]
        invalid = self.edit_context(draft, 0, 0)
        await self.cog.final_result_edit_score.callback.func(self.cog, invalid, score="4.50")
        invalid.defer.assert_not_awaited()
        self.assertTrue(invalid.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        foreign = self.context(8, component=True)
        foreign.custom_id, foreign.values = f"finaledit|{draft.id}", ["0|0"]
        await self.cog.final_result_edit.callback.func(self.cog, foreign)
        foreign.send_modal.assert_not_awaited()
        self.assertTrue(foreign.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        self.assertEqual((await FinalResultDraft.get(draft.id)).snapshot.scores[first], ["3.100"] * 7)
        confirmation = self.context(component=True)
        confirmation.custom_id = f"finalconfirm|{draft.id}"
        await self.cog.final_result_confirmation.callback.func(self.cog, confirmation)
        late = self.edit_context(draft, 0, 0)
        await self.cog.final_result_edit_score.callback.func(self.cog, late, score="0.00")
        self.assertTrue(late.edit.await_args.kwargs["content"].startswith(str(ERROR_EMOJI)))
        saved = await FinalMatchResult.find_all().first_or_none()
        self.assertEqual(saved.snapshot.scores[first], ["3.100"] * 7)
        self.assertEqual((await FinalResultDraft.get(draft.id)).snapshot.scores[first], ["3.100"] * 7)
        await FinalResultDraft.get_pymongo_collection().update_one(
            {"_id": draft.id}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}})
        expired = self.context(component=True)
        expired.custom_id, expired.values = f"finaledit|{draft.id}", ["0|0"]
        await self.cog.final_result_edit.callback.func(self.cog, expired)
        expired.send_modal.assert_not_awaited()
        self.assertEqual(expired.edit_origin.await_args.kwargs["components"], [])

    async def test_foreign_expired_cancelled_and_revoked_drafts_never_save(self):
        _, draft = await self.preview()
        for user, manager in ((8, True), (9, False)):
            ctx = self.context(user, manager=manager, component=True)
            ctx.custom_id = f"finalconfirm|{draft.id}"
            await self.cog.final_result_confirmation.callback.func(self.cog, ctx)
            output = ctx.send.await_args.args[0] if user == 8 else ctx.edit_origin.await_args.kwargs["content"]
            self.assertTrue(output.startswith(str(ERROR_EMOJI)))
            self.assertEqual(await FinalMatchResult.count(), 0)
        ctx = self.context(component=True)
        ctx.custom_id = f"finalcancel|{draft.id}"
        await self.cog.final_result_confirmation.callback.func(self.cog, ctx)
        self.assertTrue(ctx.edit_origin.await_args.kwargs["content"].startswith(str(SUCCESS_EMOJI)))
        self.assertEqual(ctx.edit_origin.await_args.kwargs["components"], [])
        self.assertEqual((await FinalResultDraft.get(draft.id)).state, "CANCELLED")
        ctx = self.context(component=True)
        ctx.custom_id = f"finalconfirm|{draft.id}"
        await self.cog.final_result_confirmation.callback.func(self.cog, ctx)
        self.assertEqual(await FinalMatchResult.count(), 0)
        await draft.set({"state": "PENDING", "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
        expired = self.context(component=True)
        expired.custom_id = ctx.custom_id
        await self.cog.final_result_confirmation.callback.func(self.cog, expired)
        self.assertTrue(expired.edit_origin.await_args.kwargs["content"].startswith(str(ERROR_EMOJI)))
        self.assertEqual(expired.edit_origin.await_args.kwargs["components"], [])
        self.assertEqual(await FinalMatchResult.count(), 0)

    async def test_cancel_racing_confirmation_commits_exactly_one_draft_outcome(self):
        _, draft = await self.preview()
        outcomes = []

        async def finish(cancel):
            try:
                outcomes.append(await finish_result_draft(draft, cancel=cancel))
            except FinalError as error:
                outcomes.append(error)

        async with anyio.create_task_group() as group:
            group.start_soon(finish, True)
            group.start_soon(finish, False)
        current = await FinalResultDraft.get(draft.id)
        count = await FinalMatchResult.count()
        self.assertEqual(sum(isinstance(outcome, FinalError) for outcome in outcomes), 1)
        self.assertEqual(count, int(current.state == "CONFIRMED"))
        self.assertEqual((await final_for_view()).nodes["QF1"].result_version, count)

    async def test_failed_result_insert_keeps_pending_draft_and_has_no_success_receipt(self):
        _, draft = await self.preview()
        ctx = self.context(component=True)
        ctx.custom_id = f"finalconfirm|{draft.id}"
        await self.client.admin.command({"configureFailPoint": "failCommand", "mode": {"times": 1},
            "data": {"failCommands": ["insert"], "errorCode": 2}})
        try:
            await self.cog.final_result_confirmation.callback.func(self.cog, ctx)
        finally:
            await self.client.admin.command({"configureFailPoint": "failCommand", "mode": "off"})
        self.assertEqual(await FinalMatchResult.count(), 0)
        self.assertEqual((await FinalResultDraft.get(draft.id)).state, "PENDING")
        self.assertTrue(ctx.edit_origin.await_args.kwargs["content"].startswith(str(ERROR_EMOJI)))
        self.channel.send.assert_not_awaited()

    async def test_tied_scores_are_refused_before_a_preview_exists(self):
        node = self.final.nodes["QF1"]
        ctx = self.context()
        ctx.custom_id = f"finalres|{self.final.id}|QF1|9|0|{node.contestant_revision}|{node.attempt}|-|0"
        await self.cog.final_result_modal.callback.func(self.cog, ctx, player1="\n".join(["3.100"] * 7),
                                                       player2="\n".join(["3.100"] * 7))
        self.assertIn(str(ERROR_EMOJI), str(ctx.send.await_args))
        self.assertIsNone(await FinalResultDraft.find_one({}))
        self.assertIsNone((await final_for_view()).nodes["QF1"].result_id)

    async def test_forfeit_receipt_follows_the_committed_refund(self):
        market = await open_market(self.final, "QF1")
        await apply_bet(BetAction(market_id=market.id, user_id="9", contestant="1", amount=1000,
                                 expected_version=0, action_key="forfeit-stake"))
        node = self.final.nodes["QF1"]
        ctx = self.context()
        ctx.custom_id = f"finalres|{self.final.id}|QF1|9|0|{node.contestant_revision}|{node.attempt}|1|1"
        await self.cog.final_result_modal.callback.func(self.cog, ctx, player1="\n".join(["0.00"] * 7),
                                                       player2="\n".join(["0.00"] * 7), reason="Fixture forfeit")
        draft = await FinalResultDraft.find_one({"state": "PENDING"})
        confirmation = self.context(component=True)
        confirmation.custom_id = f"finalconfirm|{draft.id}"
        await self.cog.final_result_confirmation.callback.func(self.cog, confirmation)
        self.assertEqual((await FinalBettingMarket.get(market.id)).state, "REFUNDED")
        self.assertEqual((await TokenWallet.get("9")).balance, 5000)
        receipt = confirmation.edit_origin.await_args.kwargs
        self.assertTrue(receipt["content"].startswith(str(SUCCESS_EMOJI)))
        self.assertIn("https://discordapp.com/users/1", receipt["content"])
        self.assertIn("SF1", receipt["content"])
        self.assertIn("QF1", receipt["content"])
        self.assertEqual(receipt["components"], [])
        saved = await FinalMatchResult.find_all().first_or_none()
        self.assertEqual((saved.snapshot.outcome, saved.snapshot.winner), ("Forfeit", "1"))

    async def test_only_the_result_completing_the_round_announces_readiness(self):
        for index, key in enumerate(("QF1", "QF2", "QF3", "QF4")):
            if index:
                source = await self.scheduled_pairing(await final_for_view(), key)
                await source.set({"thread": str(777 + index)})
                await bind_thread(await final_for_view(), source, source.thread)
            current = await final_for_view()
            node = current.nodes[key]
            ctx = self.context()
            ctx.custom_id = f"finalres|{current.id}|{key}|9|0|{node.contestant_revision}|{node.attempt}|-|0"
            await self.cog.final_result_modal.callback.func(self.cog, ctx, player1="\n".join(["3.100"] * 7),
                                                           player2="\n".join(["2.80"] * 7))
            draft = await FinalResultDraft.find_one({"state": "PENDING"})
            confirmation = self.context(component=True)
            confirmation.custom_id = f"finalconfirm|{draft.id}"
            await self.cog.final_result_confirmation.callback.func(self.cog, confirmation)
            self.assertEqual("Semifinals" in confirmation.edit_origin.await_args.kwargs["content"], index == 3)
            current = await final_for_view()
            self.assertEqual(all(current.nodes[match].winner for match in ("QF1", "QF2", "QF3", "QF4")), index == 3)
            self.assertEqual((await FinalResultDraft.get(draft.id)).round_completed, index == 3)


class TestFinalDraftBeforeThread(FinalUICase):
    async def test_bracket_reset_retires_an_unconfirmed_result_without_saving_it(self):
        self.final = await confirm_bracket(self.final, "9")
        node = self.final.nodes["QF1"]
        modal = self.context()
        modal.custom_id = f"finalres|{self.final.id}|QF1|9|0|{node.contestant_revision}|0|1|1"
        await self.cog.final_result_modal.callback.func(self.cog, modal, player1="\n".join(["0.00"] * 7),
                                                       player2="\n".join(["0.00"] * 7), reason="Fixture forfeit")
        draft = await FinalResultDraft.find_one({"state": "PENDING"})
        await reset_bracket(await final_for_view())
        confirm = self.context(component=True)
        confirm.custom_id = f"finalconfirm|{draft.id}"
        await self.cog.final_result_confirmation.callback.func(self.cog, confirm)
        self.assertEqual(await FinalMatchResult.count(), 0)
        self.assertEqual((await final_for_view()).state, "REGISTRATION")
        self.assertIn("match changed", confirm.edit_origin.await_args.kwargs["content"])
        self.assertEqual(confirm.edit_origin.await_args.kwargs["components"], [])
