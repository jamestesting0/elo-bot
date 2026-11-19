from unittest.mock import AsyncMock, MagicMock

import interactions as ipy

import elo_bot.extensions.match as match_module
from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.core.finals import FINAL_DISTRICTS, FinalError
from elo_bot.db import Clan, Match
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.features.finals.hosting import accept_final_start
from elo_bot.features.finals.lifecycle import confirm_bracket, final_for_view
from elo_bot.features.finals.matches import prepare_match
from elo_bot.features.hosting.war import result_store_task
from tests.integration._final_fixture import FinalScenarioCase


class TestFinalHosting(FinalScenarioCase):
    async def pairing(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        source = await self.scheduled_pairing(final, "QF1")
        market = await FinalBettingMarket(id="market", final_id=str(final.id), node_id="QF1",
                                         contestant_revision=source.final_contestant_revision,
                                         contestants=[participant.user_id for participant in source.participants]).insert()
        return await final_for_view(), source, market

    async def test_provisioning_reuses_exact_attempt_without_claiming_play(self):
        final, source, market = await self.pairing()
        again = await prepare_match(final, "QF1")
        self.assertEqual(again.id, source.id)
        self.assertEqual(await Match.count(), 1)
        self.assertEqual((await FinalBettingMarket.get(market.id)).state, "OPEN")
        self.assertEqual(source.final_districts, list(FINAL_DISTRICTS))
        self.assertTrue(source.base_submission)

    async def test_open_predictions_reject_start_without_closing_market(self):
        final, source, market = await self.pairing()
        await final.set({"prediction_state": "OPEN"})
        with self.assertRaises(FinalError):
            await accept_final_start(source, "host")
        self.assertEqual((await FinalBettingMarket.get(market.id)).state, "OPEN")
        self.assertIsNone((await final_for_view()).first_actual_started_at)

    async def test_missing_clan_and_stale_source_are_preflight_failures(self):
        final, source, market = await self.pairing()
        with self.assertRaises(FinalError):
            await accept_final_start(source, "host", clan_tag="#MISSING")
        self.assertEqual((await FinalBettingMarket.get(market.id)).state, "OPEN")
        await Clan(tag="#CLAN", name="Test", availability_str="Available").insert()
        with self.assertRaises(FinalError):
            await accept_final_start(source, "host", clan_tag="#CLAN", expected_source={"thread": "stale"})
        self.assertEqual((await FinalBettingMarket.get(market.id)).state, "OPEN")
        self.assertIsNone((await final_for_view()).first_actual_started_at)

    async def test_accepted_start_closes_before_external_work_and_survives_abort_fields(self):
        final, source, market = await self.pairing()
        await accept_final_start(source, "host")
        saved = await Match.get(source.id)
        self.assertEqual((await FinalBettingMarket.get(market.id)).state, "CLOSED")
        self.assertIsNotNone(saved.final_started_at)
        await saved.set({"times": [None, None], "hosts": [None, None]})
        await accept_final_start(saved, "other-host")
        self.assertEqual((await FinalBettingMarket.get(market.id)).version, 1)
        self.assertEqual((await final_for_view()).nodes["QF1"].start_claim, saved.final_start_claim)

    async def test_old_final_submit_bases_button_points_to_the_command_without_a_modal(self):
        _, source, _ = await self.pairing()
        cog = match_module.MatchCommands(MagicMock(ext={}))
        ctx = MagicMock(spec=ipy.ComponentContext, send=AsyncMock(), send_modal=AsyncMock())
        ctx.channel.id = 987
        ctx.guild_id = source.routing_guild_id
        ctx.custom_id = f"submit_bases|{source.token}"
        ctx.author.has_role.return_value = False
        for author in (int(source.participants[0].user_id), 999):
            ctx.author.id = author
            await cog.submit_bases_button.callback.func(cog, ctx)
            ctx.send_modal.assert_not_awaited()
            self.assertEqual(ctx.send.await_args.args[0],
                             f"{ERROR_EMOJI} Submit your bases with `/match base submit` in this thread.")
        self.assertEqual((await Match.get(source.id)).participants[0].bases, [None] * 7)

    async def test_final_helper_start_never_schedules_forty_minute_collection(self):
        _, source, _ = await self.pairing()
        bot = MagicMock()
        result_store_task.schedule_automatic_result_store(bot, result_store_task.ScheduledResultStoreRequest(
            token=source.token, player=0, requested_by="host", started_at=1, is_final=True))
        self.assertFalse(result_store_task._scheduled_result_store_is_eligible(source,
            result_store_task.ScheduledResultStoreRequest(token=source.token, player=0, requested_by="host", started_at=1)))
