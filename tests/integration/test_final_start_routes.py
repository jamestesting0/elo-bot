from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from tests.integration._final_fixture import FinalScenarioCase
from cogs import match_commands
from elobot.features.base_store.controls import start_stored_bases
from elobot.features.base_store.models import StoredBaseActionRequest
from elobot.features.hosting.live.live_start import LiveStart, ManualLiveStartReply, start_live_match
from elobot.features.hosting.war.war_start import SelfHostedWarStartReply, WarStart, start_war
from elobot.integrations.helper_api import HelperEta, HelperEtaReader, HelperJobCreator, HelperJobCreateRejected
from elobot.features.finals.lifecycle import confirm_bracket, resolve_final
from elobot.features.finals.matches import bind_thread
from elobot.features.finals.types import FINAL_DISTRICTS
from elobot.features.tokens.bets import open_market
from elobot.db import BaseStore, Clan, Match
from elobot.db.token_models import FinalBettingMarket


class TestFinalStartRoutes(FinalScenarioCase):
    async def pairing(self, *, live=False):
        final = await confirm_bracket(await self.roster(8), "manager")
        source = await self.scheduled_pairing(final, "QF1", is_war=not live)
        store = BaseStore(clan_tag="#TEST", slot="Slot 1", reservation_id="owned", message_id=555, ready=True)
        await source.set({"thread": "777", "times": [None, None], "participants.0.base_store": store.model_dump(),
                          "participants.0.opponent_bases_stored": True})
        await Clan(tag="#TEST", name="Test", availability_str="Available").insert()
        source = await Match.get(source.id)
        await bind_thread(final, source, "777")
        market = await open_market(await resolve_final(), "QF1")
        return final, source, market

    def transport(self, kind):
        bot = MagicMock(ext={})
        bot.coc_client.get_clan = AsyncMock(return_value=MagicMock(tag="#TEST", name="Test"))
        bot.fetch_user = AsyncMock(return_value=MagicMock(mention="<@1>"))
        ctx = MagicMock(spec=kind, send=AsyncMock())
        ctx.channel.id = 777
        ctx.channel_id = 777
        ctx.author.id = 9
        ctx.guild.fetch_member = AsyncMock(return_value=MagicMock(mention="<@1>"))
        ctx.channel.fetch_pinned_messages = AsyncMock(return_value=[MagicMock(embeds=[ipy.Embed(title="Match")])])
        return bot, ctx

    async def test_manual_war_start_closes_before_failed_discord_delivery_and_retains_slot(self):
        final, source, market = await self.pairing()
        bot, ctx = self.transport(ipy.SlashContext)
        async def fail(*args, **kwargs):
            self.assertEqual((await FinalBettingMarket.get(market.id)).state, "CLOSED")
            raise ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        ctx.channel.send = AsyncMock(side_effect=fail)
        with self.assertRaises(ipy.errors.Forbidden):
            await start_war(bot, ctx, WarStart(match=source, player=0, host=bot.user,
                clan=MagicMock(tag="#TEST", name="Test"), coc_client=bot.coc_client, reply=SelfHostedWarStartReply()))
        saved = await Match.get(source.id)
        self.assertIsNotNone(saved.times[0])
        self.assertEqual(saved.participants[0].base_store.reservation_id, "owned")
        self.assertIsNotNone((await resolve_final()).first_actual_started_at)

    async def test_manual_live_start_closes_before_failed_discord_delivery_and_retains_slot(self):
        final, source, market = await self.pairing(live=True)
        bot, ctx = self.transport(ipy.SlashContext)
        async def fail(*args, **kwargs):
            self.assertEqual((await FinalBettingMarket.get(market.id)).state, "CLOSED")
            raise ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        ctx.channel.send = AsyncMock(side_effect=fail)
        with self.assertRaises(ipy.errors.Forbidden):
            await start_live_match(bot, ctx, LiveStart(match=source, host=bot.user,
                clan=MagicMock(tag="#TEST", name="Test"), player1=MagicMock(), player2=MagicMock(),
                player1_elo=2000, player2_elo=2000, scout=False, reply=ManualLiveStartReply(slot_instruction="")))
        saved = await Match.get(source.id)
        self.assertFalse(saved.is_war)
        self.assertIsNotNone(saved.times[0])
        self.assertEqual(saved.participants[0].base_store.reservation_id, "owned")

    async def test_stored_helper_request_receives_seven_districts_after_market_closure(self):
        final, source, market = await self.pairing()
        bot, ctx = self.transport(ipy.ComponentContext)
        runtime = match_commands.MatchCommands(bot)
        runtime.helper_api_base_url = "http://127.0.0.1:1"
        runtime.helper_api_key = "test-only"
        async def reject(session, request):
            self.assertEqual((await FinalBettingMarket.get(market.id)).state, "CLOSED")
            self.assertEqual(request.payload.districts, list(FINAL_DISTRICTS))
            self.assertFalse(request.payload.remove_bases_after)
            return HelperJobCreateRejected(error="Test rejection")
        with patch.object(HelperEtaReader, "read", new=AsyncMock(return_value=HelperEta(
                estimate_start_at=1, estimate_end_at=2))), patch.object(HelperJobCreator, "create", new=AsyncMock(side_effect=reject)) as create:
            await start_stored_bases(runtime, ctx, StoredBaseActionRequest(token=source.token, player=0,
                reservation_id="owned", message_id=555))
        create.assert_awaited_once()
        self.assertIsNone((await Match.get(source.id)).times[0])
        self.assertEqual((await Match.get(source.id)).participants[0].base_store.reservation_id, "owned")
        self.assertIn("could not confirm", ctx.send.await_args.args[0])

    async def test_open_predictions_stop_stored_helper_request_and_keep_market_open(self):
        final, source, market = await self.pairing()
        await final.set({"prediction_state": "OPEN"})
        bot, ctx = self.transport(ipy.ComponentContext)
        runtime = match_commands.MatchCommands(bot)
        runtime.helper_api_base_url = "http://127.0.0.1:1"
        runtime.helper_api_key = "test-only"
        with patch.object(HelperEtaReader, "read", new=AsyncMock(return_value=HelperEta(
                estimate_start_at=1, estimate_end_at=2))), patch.object(HelperJobCreator, "create", new=AsyncMock()) as create:
            await start_stored_bases(runtime, ctx, StoredBaseActionRequest(token=source.token, player=0,
                reservation_id="owned", message_id=555))
        create.assert_not_awaited()
        self.assertEqual((await FinalBettingMarket.get(market.id)).state, "OPEN")
        self.assertIn("Predictions are open", ctx.send.await_args.args[0])
