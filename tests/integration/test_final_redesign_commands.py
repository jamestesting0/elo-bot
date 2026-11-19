from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from pymongo.errors import PyMongoError

from elo_bot.config.emojis import IMPORTANT_EMOJI, SUCCESS_EMOJI, TOKEN_EMOJI
from elo_bot.config.ids import LVL1_ROLES
from elo_bot.db import EloFinal, Match
from elo_bot.db.final_models import FinalMatchResult
from elo_bot.db.token_models import TokenLedgerEntry, TokenWallet
from elo_bot.extensions.final import FinalCommands
from elo_bot.extensions.tokens import TokenCommands
from elo_bot.features.finals.bracket import automatic_pairs
from elo_bot.features.finals.bracket_ui import bracket_controls
from elo_bot.features.finals.lifecycle import confirm_bracket, final_for_view, reset_bracket, save_pairing_draft
from elo_bot.features.finals.results import FinalResultSubmission, submit_result
from elo_bot.features.finals.recovery import recover_final
from elo_bot.features.finals.publication import publish_bracket
from elo_bot.features.tokens.bets import BetAction, apply_bet, close_market, open_market
from tests.integration._final_fixture import FinalScenarioCase


class TestFinalRedesignCommands(FinalScenarioCase):
    def context(self, final):
        ctx = MagicMock(guild_id=final.guild_id, id=100, values=[], input_text="", send=AsyncMock(),
                        defer=AsyncMock(), edit_origin=AsyncMock(), send_modal=AsyncMock())
        ctx.author.id = 9
        ctx.author.has_role.side_effect = lambda role: role in LVL1_ROLES
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        return ctx

    def bot(self):
        message = MagicMock(id=800, jump_url="https://discord.com/channels/1/2/800", pin=AsyncMock(), edit=AsyncMock())
        channel = MagicMock(send=AsyncMock(return_value=message), fetch_message=AsyncMock(return_value=message))
        channel.guild.fetch_member = AsyncMock(return_value=None)
        return MagicMock(ext={}, user=SimpleNamespace(id=42), fetch_channel=AsyncMock(return_value=channel)), channel

    async def test_confirm_publishes_and_archived_result_selector_remains_read_only(self):
        final = await self.roster(5)
        bot, channel = self.bot()
        cog = FinalCommands(bot)
        ctx = self.context(final)
        await cog.final_bracket_create.callback.func(cog, ctx)
        self.assertEqual((await final_for_view()).state, "REGISTRATION")
        ctx.custom_id = ctx.send.await_args.kwargs["components"][1].components[1].custom_id
        await cog.final_pairing_action.callback.func(cog, ctx)
        final = await final_for_view()
        self.assertEqual(final.bracket_message_id, "800")
        self.assertFalse(final.publication_pending)
        self.assertTrue(ctx.edit_origin.await_args.kwargs["content"].startswith(str(SUCCESS_EMOJI)))
        channel.send.return_value.pin.assert_awaited_once()
        await self.scheduled_pairing(final, "QF4")
        final = await final_for_view()
        node = final.nodes["QF4"]
        result = await submit_result(FinalResultSubmission(final_id=final.id, node_id=node.id,
            actor_id="9", operation_key="selector", expected_version=0,
            contestant_revision=node.contestant_revision, attempt=node.attempt,
            scores={node.contestants[0]: ["3.100"] * 7, node.contestants[1]: ["2.80"] * 7}))
        final = await final_for_view()
        controls = await bracket_controls(final)
        await final.set({"active": False, "state": "COMPLETED"})
        before = final.model_dump()
        ctx.custom_id = controls[0].components[0].custom_id
        ctx.values = [node.id]
        await cog.final_result_select.callback.func(cog, ctx)
        payload = ctx.send.await_args.kwargs
        self.assertTrue(payload["ephemeral"])
        self.assertIn("embed", payload)
        self.assertEqual(payload["embed"].fields[0].value.count("700%"), 1)
        self.assertEqual((await EloFinal.get(final.id)).model_dump(), before)
        self.assertEqual(await FinalMatchResult.count(), 1)
        self.assertEqual((await FinalMatchResult.get(result.id)).version, 1)

    async def test_reset_publishes_the_entrant_list_without_permanent_image_recovery(self):
        final = await confirm_bracket(await self.roster(5), "9")
        await reset_bracket(final)
        final = await final_for_view()
        bot, channel = self.bot()
        await publish_bracket(bot, final)
        current = await final_for_view()
        self.assertFalse(current.publication_pending)
        self.assertIsNone(current.publication_error)
        self.assertEqual(current.bracket_message_id, "800")
        payload = channel.send.await_args.kwargs
        self.assertIsNone(payload["file"])
        self.assertEqual(payload["components"], [])
        self.assertEqual(payload["embed"].fields, [])

    async def test_manual_confirm_keeps_committed_bracket_when_publication_fails(self):
        final = await self.roster(5)
        final = await save_pairing_draft(final, "9", automatic_pairs(final))
        bot, channel = self.bot()
        channel.send.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        cog = FinalCommands(bot)
        ctx = self.context(final)
        ctx.custom_id = f"finalpair|{final.id}|9|{final.draft_revision}|confirm"
        await cog.final_pairing_action.callback.func(cog, ctx)
        current = await final_for_view()
        self.assertEqual(current.state, "BRACKET_READY")
        self.assertEqual(len(current.nodes), 7)
        self.assertTrue(current.publication_pending)
        self.assertIsNotNone(current.publication_error)
        self.assertTrue(ctx.edit_origin.await_args.kwargs["content"].startswith(str(IMPORTANT_EMOJI)))
        self.assertTrue(ctx.edit_origin.await_args.kwargs["components"])

    async def test_confirm_reports_committed_state_when_publication_metadata_write_fails(self):
        final = await self.roster(5)
        cog = FinalCommands(MagicMock(ext={}))
        ctx = self.context(final)
        await cog.final_bracket_create.callback.func(cog, ctx)
        ctx.custom_id = ctx.send.await_args.kwargs["components"][1].components[1].custom_id
        with patch("elo_bot.extensions.final.bracket.publish_bracket", new=AsyncMock(side_effect=PyMongoError("Metadata write failed"))):
            await cog.final_pairing_action.callback.func(cog, ctx)
        current = await final_for_view()
        self.assertEqual(current.state, "BRACKET_READY")
        self.assertEqual(len(current.nodes), 7)
        self.assertTrue(current.publication_pending)
        self.assertTrue(ctx.edit_origin.await_args.kwargs["content"].startswith(str(IMPORTANT_EMOJI)))
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_autocomplete_filters_current_playable_pairings_and_markets(self):
        final = await confirm_bracket(await self.roster(8), "9")
        for node_id in ("QF1", "QF2", "QF3", "QF4"):
            market = await open_market(final, node_id)
            if node_id == "QF3":
                await close_market(market.id)
            elif node_id in {"QF1", "QF4"}:
                await market.set({"state": "PAID"})
        await EloFinal.get_pymongo_collection().update_one({"_id": final.id},
            {"$set": {"nodes.QF1.winner": "1", "nodes.QF1.state": "DECIDED"}})
        bot, _ = self.bot()
        cog = FinalCommands(bot)
        ctx = self.context(final)
        for callback, expected in (("final_result_autocomplete", ["QF1", "QF2", "QF3", "QF4"]),
                ("final_forfeit_autocomplete", ["QF2", "QF3", "QF4"]),
                ("final_close_autocomplete", ["QF2"]), ("final_void_autocomplete", ["QF2", "QF3"])):
            with self.subTest(callback=callback):
                await getattr(cog, callback)(ctx)
                choices = ctx.send.await_args.kwargs["choices"]
                self.assertEqual([choice.value for choice in choices], expected)
                self.assertTrue(all("vs." in str(choice.name) and len(str(choice.name)) <= 100 for choice in choices))
        ctx.input_text = "Player 4"
        await cog.final_result_autocomplete(ctx)
        self.assertEqual([choice.value for choice in ctx.send.await_args.kwargs["choices"]], ["QF4"])

    async def test_round_resume_skips_results_already_saved_in_the_same_round(self):
        final = await confirm_bracket(await self.roster(8), "9")
        await self.scheduled_pairing(final, "QF1")
        final = await final_for_view()
        node = final.nodes["QF1"]
        await submit_result(FinalResultSubmission(final_id=final.id, node_id=node.id, actor_id="9",
            operation_key="resume", expected_version=0, contestant_revision=node.contestant_revision,
            attempt=node.attempt, scores={node.contestants[0]: ["3.100"] * 7, node.contestants[1]: ["2.80"] * 7}))
        await recover_final(await final_for_view())
        bot, _ = self.bot()
        cog = FinalCommands(bot)
        ctx = self.context(final)
        attempted = []

        async def confirm(bot, token):
            source = await Match.find_one({"token": token})
            attempted.append(source.final_node_id)
            await source.set({"thread": str(700 + len(attempted))})
            return source.thread

        with patch("elo_bot.extensions.final.rounds.lifecycle_module.confirm_match", new=AsyncMock(side_effect=confirm)):
            await cog.final_round_start.callback.func(cog, ctx)
        self.assertEqual(attempted, ["QF2", "QF3", "QF4"])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))

    async def test_wallet_open_bets_and_dated_history_hide_identity_receipts(self):
        final = await confirm_bracket(await self.roster(8), "9")
        market = await open_market(final, "QF2")
        await apply_bet(BetAction(market_id=market.id, user_id="9", contestant="2", amount=1000,
                                 expected_version=0, action_key="open-bet"))
        await TokenLedgerEntry(id="identity", operation_key="identity", wallet_id="9", actor_id="9",
                               amount=0, kind="identity", created_at=2000000000).insert()
        before = (await TokenWallet.get("9")).model_dump()
        cog = TokenCommands(MagicMock(ext={}))
        ctx = self.context(final)
        await cog.tokens_balance.callback.func(cog, ctx)
        embed = ctx.send.await_args.kwargs["embed"]
        self.assertEqual([field.name for field in embed.fields], ["__**Bets**__", "__**Open Bets**__"])
        self.assertIn(f"S7 QF2 · 1,000 {TOKEN_EMOJI}", embed.fields[1].value)
        self.assertIn("https://discordapp.com/users/2?tab=wishlist", embed.fields[1].value)
        await cog.tokens_history.callback.func(cog, ctx)
        payload = ctx.send.await_args.kwargs
        lines = payload["embed"].description.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(payload["components"][1].label, "1/1")
        self.assertTrue(all(":R>" in line for line in lines))
        self.assertFalse(any("identity" in line.casefold() for line in lines))
        self.assertEqual((await TokenWallet.get("9")).model_dump(), before)
        self.assertEqual(await TokenLedgerEntry.find({"kind": "identity"}).count(), 1)
