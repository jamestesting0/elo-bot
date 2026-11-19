from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from tests.integration._final_fixture import FinalScenarioCase
from elo_bot.features.base_store.controls import start_stored_bases
from elo_bot.features.base_store.models import StoredBaseActionRequest
from elo_bot.features.hosting.live.live_start import LiveStart, ManualLiveStartReply, start_live_match
from elo_bot.features.hosting.war.war_start import SelfHostedWarStartReply, WarStart, start_war
from elo_bot.integrations.helper_api import HelperEta, HelperEtaReader, HelperJobCreator, HelperJobCreateRejected
from elo_bot.features.finals.lifecycle import confirm_bracket, final_for_view
from elo_bot.features.finals.matches import bind_thread
from elo_bot.core.finals import FINAL_DISTRICTS
from elo_bot.features.tokens.bets import open_market
from elo_bot.db import BaseStore, Clan, Match
from elo_bot.db.token_models import FinalBettingMarket
import elo_bot.extensions.match as match_module
import elo_bot.features.hosting.sequential.service as sequential_service
from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.core.finals import FinalError
from elo_bot.db.match_models import BaseLink
from elo_bot.features.finals.hosting import check_final_start
from elo_bot.features.hosting.live.live_start import LiveStartSucceeded
from elo_bot.features.hosting.live.preflight import LivePreflight
from elo_bot.features.matches import attack_bases
from elo_bot.features.matches.timing import hosting_started


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
        market = await open_market(await final_for_view(), "QF1")
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
        self.assertIsNotNone((await final_for_view()).first_actual_started_at)

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
        runtime = match_module.MatchCommands(bot)
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
        self.assertFalse(hosting_started(await Match.get(source.id)))
        self.assertEqual((await Match.get(source.id)).participants[0].base_store.reservation_id, "owned")
        self.assertIn("could not confirm", ctx.send.await_args.args[0])

    async def test_open_predictions_stop_stored_helper_request_and_keep_market_open(self):
        final, source, market = await self.pairing()
        await final.set({"prediction_state": "OPEN"})
        bot, ctx = self.transport(ipy.ComponentContext)
        runtime = match_module.MatchCommands(bot)
        runtime.helper_api_base_url = "http://127.0.0.1:1"
        runtime.helper_api_key = "test-only"
        with patch.object(HelperEtaReader, "read", new=AsyncMock(return_value=HelperEta(
                estimate_start_at=1, estimate_end_at=2))), patch.object(HelperJobCreator, "create", new=AsyncMock()) as create:
            await start_stored_bases(runtime, ctx, StoredBaseActionRequest(token=source.token, player=0,
                reservation_id="owned", message_id=555))
        create.assert_not_awaited()
        self.assertEqual((await FinalBettingMarket.get(market.id)).state, "OPEN")
        self.assertIn("Predictions are still open. Ask a manager to close them before starting.", ctx.send.await_args.args[0])


class TestFinalStartWorkflow(FinalScenarioCase):
    async def ready(self, *, live):
        final = await confirm_bracket(await self.roster(8), "manager")
        source = await self.scheduled_pairing(final, "QF1", is_war=not live)
        await source.set({"thread": "777"})
        await Clan(tag="#TEST", name="Test", availability_str="Available").insert()
        await bind_thread(final, await Match.get(source.id), "777")
        return final, await Match.get(source.id)

    def command(self, author=9):
        bot = MagicMock(ext={})
        bot.user.id = 1
        bot.coc_client.get_clan = AsyncMock(return_value=MagicMock(tag="#TEST", name="Test"))
        bot.coc_client.get_player = AsyncMock(side_effect=lambda tag: MagicMock(tag=tag, town_hall=3))
        bot.fetch_user = AsyncMock(side_effect=lambda user: MagicMock(id=user, mention=f"<@{user}>"))
        ctx = MagicMock(spec=ipy.SlashContext, send=AsyncMock(), defer=AsyncMock())
        ctx.channel.id = 777
        ctx.author.id = author
        return match_module.MatchCommands(bot), ctx

    async def refused_before_bases(self, *, live):
        final, source = await self.ready(live=live)
        await final.set({"prediction_state": "OPEN"})
        cog, ctx = self.command()
        with patch.object(attack_bases, "choose_bases", new=AsyncMock()) as choose, \
                patch.object(HelperEtaReader, "read", new=AsyncMock()) as eta:
            if live:
                await cog._start_live(ctx, LivePreflight(clan="#TEST", scout=False, automatic=True, requested_by="9"))
            else:
                await cog._start_war(ctx, 0, "#TEST", automatic=True)
        choose.assert_not_awaited()
        eta.assert_not_awaited()
        ctx.send.assert_awaited_once()
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        self.assertFalse(hosting_started(await Match.get(source.id)))

    async def test_unready_live_autostart_is_refused_before_bases_or_helper(self):
        await self.refused_before_bases(live=True)

    async def test_unready_war_autostart_is_refused_before_bases_or_helper(self):
        await self.refused_before_bases(live=False)

    async def test_command_that_does_not_fit_the_schedule_names_the_matching_commands(self):
        _, war = await self.ready(live=False)
        with self.assertRaises(FinalError) as live_on_war:
            await check_final_start(war, None)
        self.assertIn("/match war autostart", str(live_on_war.exception))
        await war.set({"is_war": False})
        with self.assertRaises(FinalError) as war_on_live:
            await check_final_start(await Match.get(war.id), 0)
        self.assertIn("/match autostart", str(war_on_live.exception))
        await check_final_start(await Match.get(war.id), None)

    async def test_final_autostart_is_sequential_by_default_and_passes_attacks_first(self):
        _, source = await self.ready(live=True)
        links = {side: [BaseLink(link=f"https://link/{side}/{index}", image="").model_dump() for index in range(7)]
                 for side in (0, 1)}
        await source.set({"participants.0.bases": links[0], "participants.1.bases": links[1]})
        cog, ctx = self.command()
        launched = AsyncMock()
        with patch.object(attack_bases, "choose_bases", new=AsyncMock()), \
                patch("elo_bot.features.hosting.sequential.service.live_eligibility.clan_has_all_base_store_slots",
                      new=AsyncMock(return_value=True)), \
                patch.object(sequential_service.live_start, "start_live_match",
                             new=AsyncMock(return_value=LiveStartSucceeded(message=MagicMock(id=5)))), \
                patch.object(sequential_service, "launch_run", new=launched):
            await cog._start_live(ctx, LivePreflight(clan="#TEST", scout=False, automatic=True, requested_by="9",
                                                     sequential=None, attacks_first=1))
        launch = launched.await_args.args[2]
        self.assertEqual(launch.request.job_type, "host_sequential_live_match")
        player2 = source.participants[1].user_id
        self.assertEqual(launch.request.payload.attacks_first, f"#P{player2}")
        self.assertEqual(launch.request.payload.base_links_for_player_1[0], "https://link/1/0")
        cog, ctx = self.command()
        await cog._start_live(ctx, LivePreflight(clan="#TEST", scout=False, automatic=True, requested_by="9",
                                                 sequential=False, attacks_first=0))
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        self.assertIn("attacks_first", ctx.send.await_args.args[0])
