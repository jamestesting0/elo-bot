from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.config.ids import LVL1_ROLES, LVL2_ROLES
from elo_bot.core.finals import FinalError
from elo_bot.db import BaseStore, EloFinal, Match, Season
from elo_bot.db.final_models import FinalPrediction
from elo_bot.extensions.final import FinalCommands
from elo_bot.extensions.match import MatchCommands
from elo_bot.features.finals.lifecycle import (
    add_entrant, confirm_bracket, confirm_lineup, final_for_view, ongoing_final, request_cancellation,
    save_pairing_draft, set_nickname, start_final,
)
from elo_bot.features.finals.matches import prepare_match
from elo_bot.features.finals.recovery import recover_final, select_financial_work
from elo_bot.features.finals.result_ui import show_source_result_form
from elo_bot.features.hosting.sequential.service import stale_reason
from elo_bot.features.matches.controls import check_match_control
from elo_bot.features.tokens.bets import open_market, void_market
from elo_bot.db.token_models import FinalBettingMarket
from tests.integration._final_fixture import FinalScenarioCase


class TestFinalActiveWrites(FinalScenarioCase):
    def context(self, final):
        ctx = MagicMock(guild_id=final.guild_id, id=100, send=AsyncMock(), defer=AsyncMock(),
                        send_modal=AsyncMock(), edit_origin=AsyncMock(), values=[])
        ctx.author.id = 9
        ctx.author.has_role.side_effect = lambda role: role in (*LVL1_ROLES, *LVL2_ROLES)
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        return ctx

    async def test_manager_writes_reject_archive_without_mutating_it_or_announcing_success(self):
        final = await self.roster(5)
        await final.set({"active": False, "state": "COMPLETED"})
        before = final.model_dump()
        cog = FinalCommands(MagicMock(ext={}))
        player = MagicMock(id=1)
        actions = [("final_end", {}), ("final_cancel", {"reason": "Cancelled"}),
                   ("final_player_add", {"player": player}),
                   ("final_player_nick", {"player": player, "nickname": "Changed"}),
                   ("final_recover", {})]
        for name, arguments in actions:
            with self.subTest(command=name):
                ctx = self.context(final)
                await getattr(cog, name).callback.func(cog, ctx, **arguments)
                self.assertEqual((await EloFinal.get(final.id)).model_dump(), before)
                ctx.send.assert_awaited_once()
                self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
                self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        with self.assertRaises(FinalError):
            await set_nickname(final, "1", "Changed")
        self.assertEqual((await final_for_view()).id, final.id)
        with self.assertRaises(FinalError):
            await ongoing_final()

    async def test_old_controls_cannot_write_to_a_new_ongoing_final(self):
        final = await self.roster(5)
        await final.set({"active": False, "state": "COMPLETED"})
        archived = final.model_dump()
        with self.assertRaises(FinalError) as expired:
            await ongoing_final(final.id)
        await Season(season_number=7, start=2, end=3, players=[]).insert()
        current = await start_final(final.guild_id, final.channel_id)
        before = current.model_dump()
        with self.assertRaises(FinalError) as outdated:
            await ongoing_final(final.id)
        self.assertEqual(str(expired.exception), str(outdated.exception))
        cog = FinalCommands(MagicMock(ext={}))
        controls = [("final_pairing_action", f"finalpair|{final.id}|9|0|0|confirm"),
                    ("final_prediction_action", f"finalpred|{final.id}|9|0|submit|-"),
                    ("final_result_modal", f"finalres|{final.id}|QF1|9|0|1|0|-|0")]
        for name, custom_id in controls:
            with self.subTest(control=name):
                ctx = self.context(final)
                ctx.custom_id = custom_id
                await getattr(cog, name).callback.func(cog, ctx)
                self.assertEqual((await EloFinal.get(final.id)).model_dump(), archived)
                self.assertEqual((await EloFinal.get(current.id)).model_dump(), before)
                ctx.send_modal.assert_not_awaited()
                if ctx.edit_origin.await_count:
                    self.assertIn(str(outdated.exception), str(ctx.edit_origin.await_args.kwargs))
                else:
                    self.assertIn(str(outdated.exception), ctx.send.await_args.args[0])
        self.assertEqual(await FinalPrediction.count(), 0)

    async def test_unrelated_revision_writes_do_not_invalidate_roster_or_pairing_edits(self):
        final = await self.roster(5)
        await EloFinal.get_pymongo_collection().update_one({"_id": final.id}, {"$inc": {"revision": 1}})
        joined = await add_entrant(final, "6", "Six")
        self.assertEqual(joined.entrant_ids, ["1", "2", "3", "4", "5", "6"])
        pairs = {"QF1": ["1", None], "QF2": ["4", "5"], "QF3": ["2", None], "QF4": ["3", "6"]}
        draft = await save_pairing_draft(joined, "manager", pairs)
        await EloFinal.get_pymongo_collection().update_one({"_id": final.id}, {"$inc": {"revision": 1}})
        confirmed = await confirm_bracket(draft, "manager", manual=True)
        self.assertEqual(confirmed.nodes["QF4"].contestants, ["3", "6"])
        self.assertEqual(confirmed.setup_revision, draft.setup_revision + 1)

    async def test_idle_financial_recovery_performs_no_database_writes(self):
        final = await confirm_bracket(await self.roster(5), "manager")
        market = await open_market(final, "QF2")
        await void_market(market.id, "manager", "Fixture refund")
        await recover_final(final)
        final = await final_for_view()
        before = final.model_dump()
        before_market = (await FinalBettingMarket.get(market.id)).model_dump()
        await self.client.admin.command({"configureFailPoint": "failCommand", "mode": "alwaysOn", "data": {
            "failCommands": ["insert", "update", "delete", "findAndModify"], "errorCode": 2}})
        try:
            selected, work = await select_financial_work(final.id)
        finally:
            await self.client.admin.command({"configureFailPoint": "failCommand", "mode": "off"})
        self.assertEqual(work, [])
        self.assertEqual(selected.model_dump(), before)
        self.assertEqual((await FinalBettingMarket.get(market.id)).model_dump(), before_market)

    async def test_cancellation_retires_matches_and_controls_but_retains_base_reservations(self):
        final = await confirm_bracket(await self.roster(5), "manager")
        source = await prepare_match(final, "QF2")
        store = BaseStore(clan_tag="#OWNED", slot="Slot 1", reservation_id="a" * 16, ready=True, message_id=555)
        await source.set({"participants.0.base_store": store.model_dump(),
                          "participants.0.opponent_bases_stored": True, "old_msgs": [888, None]})
        await request_cancellation(final, "Event cancelled")
        await final.set({"repair_pending": False})
        with self.assertRaises(FinalError):
            await prepare_match(final, "QF2")
        self.assertEqual(await Match.count(), 1)
        cancelled = await Match.get(source.id)
        self.assertTrue(cancelled.final_superseded)
        self.assertEqual(cancelled.participants[0].base_store, store)
        self.assertTrue(cancelled.participants[0].opponent_bases_stored)
        run = SimpleNamespace(final_id=str(final.id), final_contestant_revision=source.final_contestant_revision,
                              final_attempt=source.final_attempt)
        self.assertIsNotNone(await stale_reason(run, cancelled))
        ctx = self.context(final)
        ctx.message.id = 888
        self.assertFalse(await check_match_control(ctx, cancelled, 0))
        ctx.send_modal.assert_not_awaited()
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        with self.assertRaises(FinalError):
            await show_source_result_form(ctx, cancelled)
        cog = MatchCommands(MagicMock(ext={}))
        ctx.custom_id = f"submit_bases|{source.token}"
        ctx.channel.id = 777
        await cog.submit_bases_button.callback.func(cog, ctx)
        ctx.send_modal.assert_not_awaited()
        self.assertEqual((await Match.get(source.id)).participants[0].base_store, store)
        await recover_final(final)
        self.assertFalse((await EloFinal.get(final.id)).active)
        await Season(season_number=7, start=2, end=3, players=self.archive.players).insert()
        next_final = await start_final(final.guild_id, final.channel_id)
        for user in range(1, 6):
            next_final = await add_entrant(next_final, str(user), str(user))
        next_final = await confirm_bracket(await confirm_lineup(next_final), "manager")
        new_match = await prepare_match(next_final, "QF2")
        self.assertNotEqual(new_match.id, source.id)
        self.assertEqual((await Match.get(source.id)).participants[0].base_store, store)
