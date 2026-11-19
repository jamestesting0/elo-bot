from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elo_bot.config.emojis import ERROR_EMOJI, IMPORTANT_EMOJI, SUCCESS_EMOJI
from elo_bot.db import EloFinal, Match
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.features.finals.lifecycle import confirm_bracket, final_for_view, remove_entrant
from elo_bot.features.finals.publication import publish_bracket
from tests.integration._final_ui_fixture import FinalUICase


class TestFinalDashboardControls(FinalUICase):
    async def test_dashboard_rechecks_owner_phase_and_role_before_changing_predictions(self):
        self.final = await confirm_bracket(self.final, "9")
        await publish_bracket(self.bot, self.final)
        for user, manager, expected in ((8, True, "another user's"), (9, False, "permission")):
            ctx = self.context(user, manager=manager, component=True)
            ctx.custom_id = f"finaldash|open|{self.final.id}|9"
            if manager:
                await self.cog.final_dashboard_action.callback.func(self.cog, ctx)
                self.assertIn(expected, ctx.send.await_args.args[0])
            else:
                with self.assertRaises(ipy.errors.CommandCheckFailure):
                    await self.cog.final_dashboard_action(ctx)
            self.assertEqual((await final_for_view()).prediction_state, "NEVER_OPENED")
        ctx = self.context(component=True)
        ctx.custom_id = f"finaldash|open|{self.final.id}|9"
        await self.cog.final_dashboard_action.callback.func(self.cog, ctx)
        self.assertEqual((await final_for_view()).prediction_state, "OPEN")
        self.assertTrue(ctx.edit_origin.await_args.kwargs["content"].startswith(str(SUCCESS_EMOJI)))
        actions = {button.custom_id for row in ctx.edit_origin.await_args.kwargs["components"] for button in row.components}
        self.assertIn(f"finaldash|close|{self.final.id}|9", actions)
        self.assertNotIn(f"finaldash|open|{self.final.id}|9", actions)
        bracket = self.posts[(await final_for_view()).bracket_message_id]
        self.assertIn(f"finalpredict|{self.final.id}", [button.custom_id for row in (bracket.edit.await_args.kwargs if bracket.edit.await_args else bracket.payload)["components"]
                                                        for button in row.components])
        stale = self.context(component=True)
        stale.custom_id = ctx.custom_id
        await self.cog.final_dashboard_action.callback.func(self.cog, stale)
        self.assertEqual(stale.edit_origin.await_args.kwargs["content"],
                         f"{ERROR_EMOJI} This view is outdated. Run `/final status` again.")
        self.assertEqual((await final_for_view()).prediction_state, "OPEN")

    async def test_editor_owns_complete_seeded_draft_and_rejects_stale_or_foreign_swaps(self):
        ctx = self.context()
        await self.cog.final_bracket_create.callback.func(self.cog, ctx)
        draft = await final_for_view()
        menu = ctx.send.await_args.kwargs["components"][0].components[0]
        self.assertEqual(draft.manual_draft["QF1"], ["1", "8"])
        self.assertEqual(draft.draft_owner, "9")
        other = self.context(user=8)
        await self.cog.final_bracket_create.callback.func(self.cog, other)
        self.assertIn("is editing the bracket", other.send.await_args.args[0])
        swap = self.context(component=True)
        swap.custom_id, swap.values = menu.custom_id, ["QF1:0", "QF2:0"]
        await self.cog.final_pairing_action.callback.func(self.cog, swap)
        saved = await final_for_view()
        self.assertEqual(saved.manual_draft["QF1"], ["2", "8"])
        self.assertEqual(saved.manual_draft["QF2"], ["1", "7"])
        self.assertEqual(saved.draft_revision, draft.draft_revision + 1)
        stale = self.context(component=True)
        stale.custom_id, stale.values = menu.custom_id, ["QF1:0", "QF2:0"]
        await self.cog.final_pairing_action.callback.func(self.cog, stale)
        self.assertIn("outdated", stale.send.await_args.args[0])
        self.assertEqual((await final_for_view()).manual_draft, saved.manual_draft)
        confirm = self.context(component=True)
        confirm.custom_id = swap.edit_origin.await_args.kwargs["components"][1].components[1].custom_id
        await self.cog.final_pairing_action.callback.func(self.cog, confirm)
        current = await final_for_view()
        self.assertEqual(current.nodes["QF1"].contestants, ["2", "8"])
        self.assertIsNotNone(current.bracket_message_id)
        self.posts[current.bracket_message_id].pin.assert_awaited_once()
        self.assertIn("Bracket confirmed and published", confirm.edit_origin.await_args.kwargs["content"])
        self.assertEqual([button.label for row in confirm.edit_origin.await_args.kwargs["components"]
                          for button in row.components], ["Open Predictions", "Start Quarterfinals"])

    async def test_round_posts_only_non_bye_markets_and_surfaces_independent_failures(self):
        self.final = await confirm_bracket(self.final, "9")
        attempted = []

        async def confirm(bot, token):
            source = await Match.find_one({"token": token})
            attempted.append(source.final_node_id)
            if source.final_node_id == "QF2":
                raise ipy.errors.Forbidden(MagicMock(status=403, reason="Fixture refusal"))
            await source.set({"thread": str(700 + len(attempted))})
            return source.thread

        ctx = self.context()
        with patch("elo_bot.extensions.final.rounds.lifecycle_module.confirm_match", new=AsyncMock(side_effect=confirm)):
            await self.cog.final_round_start.callback.func(self.cog, ctx)
        current = await final_for_view()
        self.assertEqual(attempted, ["QF1", "QF2", "QF3", "QF4"])
        self.assertIsNone(current.nodes["QF2"].thread_id)
        self.assertTrue(all(current.nodes[key].thread_id for key in ("QF1", "QF3", "QF4")))
        self.assertEqual(await FinalBettingMarket.count(), 3)
        self.assertTrue(all(market.announcement_id for market in await FinalBettingMarket.find_all().to_list()))
        self.assertTrue(ctx.send.await_args.args[0].startswith(f"{IMPORTANT_EMOJI} Quarterfinals partly started."))
        self.assertIn("Fixture refusal", ctx.send.await_args.args[0])
        self.assertEqual(len(self.posts), 3)
        self.assertEqual((await EloFinal.get(current.id)).prediction_state, "NEVER_OPENED")

    async def test_double_bye_swap_keeps_the_draft_and_cancel_discards_it(self):
        for user in ("8", "7", "6"):
            current = await final_for_view()
            await remove_entrant(current, user)
        ctx = self.context()
        await self.cog.final_bracket_create.callback.func(self.cog, ctx)
        draft = await final_for_view()
        payload = ctx.send.await_args.kwargs
        bad = self.context(component=True)
        bad.custom_id = payload["components"][0].components[0].custom_id
        bad.values = ["QF1:0", "QF3:1"]
        await self.cog.final_pairing_action.callback.func(self.cog, bad)
        self.assertEqual(bad.send.await_args.args[0], f"{ERROR_EMOJI} A match needs at least one player.")
        self.assertEqual((await final_for_view()).manual_draft, draft.manual_draft)
        self.assertEqual((await final_for_view()).draft_revision, draft.draft_revision)
        cancelled = self.context(component=True)
        cancelled.custom_id = payload["components"][1].components[2].custom_id
        await self.cog.final_pairing_action.callback.func(self.cog, cancelled)
        current = await final_for_view()
        self.assertEqual(current.manual_draft, {})
        self.assertIsNone(current.draft_owner)
        self.assertEqual(cancelled.edit_origin.await_args.kwargs["content"], f"{SUCCESS_EMOJI} Bracket draft discarded.")
        self.assertEqual(cancelled.edit_origin.await_args.kwargs["components"], [])
