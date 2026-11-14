import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie.odm.fields import ExpressionField
from pymongo.errors import PyMongoError

from elo_bot.features.hosting.live import live_start
from elo_bot.features.hosting.live import helper_live
from elo_bot.extensions.self_service import SelfCommands
from elo_bot.features.hosting.war import war_start
from elo_bot.features.hosting.war import helper_host
from elo_bot.features.hosting.war import helper_base_store_availability
from elo_bot.features.base_store.start import held_store, ready_store, shared_store_clan
from elo_bot.config.ids import BOT_ID
from tests._live_base_fixture import CLAN, DISTRICTS, PAIR, LiveBaseFixture
from tests._patch_sites import patch_sites
import elo_bot.features.base_store.match_adapters as match_adapters_module
import elo_bot.extensions.match.hosting as match_hosting_module
import elo_bot.extensions.match.lifecycle as match_lifecycle_module


class TestRaidWeekendStarts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.f = LiveBaseFixture()
        await self.f.setup(self)
        for field in ("thread", "token"):
            self.enterContext(patch.object(match_adapters_module.Match, field, ExpressionField(field), create=True))
        async def find(query):
            return await self.f.find({str(key): value for key, value in query.items()})
        match_adapters_module.Match.find_one.side_effect = find
        self.f.bot.user.id = BOT_ID
        self.f.ctx.author = MagicMock(id=333, mention="<@333>")
        original_message = self.f.message

        def message():
            result = original_message()
            result.pin = AsyncMock()
            result.delete = AsyncMock()
            return result

        self.f.message = message
        self.stored_message = self.f.add_pair()
        self.stored_message.embeds = [ipy.Embed(title="**Bases Stored**")]
        self.stored_message.components = []
        public = self.f.message()
        public._channel_id = self.f.channel.id
        public.embeds = [ipy.Embed(fields=[ipy.EmbedField("Players", "111 vs 222"), ipy.EmbedField("Time", "Pending")])]
        self.f.document["msg"] = str(public.id)
        self.f.document["persistent_host_clans"] = [None, None]
        self.enterContext(patch('elo_bot.extensions.match.hosting.is_raid_weekend', return_value=True))
        self.enterContext(patch("elo_bot.extensions.self_service.is_raid_weekend", return_value=True))
        self.enterContext(patch_sites(('elo_bot.extensions.match.base_store.confirm_pending_opponent_bases', 'elo_bot.extensions.match.hosting.confirm_pending_opponent_bases'), new=AsyncMock(return_value=True)))
        self.enterContext(patch("elo_bot.extensions.self_service.confirm_pending_opponent_bases", new=AsyncMock(return_value=True)))
        clan_stub = SimpleNamespace(tag=CLAN, name="Helper Clan", availability_str="Available")
        self.enterContext(patch_sites(('elo_bot.extensions.match.dashboards.Clan', 'elo_bot.features.matches.resources.Clan'), new=MagicMock(find_one=AsyncMock(return_value=clan_stub))))
        self.enterContext(patch("elo_bot.extensions.self_service.Clan", new=MagicMock(find_one=AsyncMock(return_value=clan_stub))))
        self.enterContext(patch('elo_bot.features.matches.resources.occupy_clan', new_callable=AsyncMock))
        self.enterContext(patch_sites(('elo_bot.extensions.match.result_store.free_clan', 'elo_bot.extensions.match.dashboards.free_clan', 'elo_bot.features.matches.resources.free_clan'), new_callable=AsyncMock))
        self.enterContext(patch_sites(('elo_bot.extensions.match.hosting.available_resource_clans_text', 'elo_bot.extensions.match.settings.available_resource_clans_text', 'elo_bot.features.matches.resources.available_resource_clans_text'), new=AsyncMock(return_value="Available")))
        self.enterContext(patch_sites(('elo_bot.extensions.match.bases.send_bases', 'elo_bot.features.matches.attack_bases.send_bases'), new_callable=AsyncMock))
        self.enterContext(patch_sites(('elo_bot.extensions.match.lifecycle.remove_helper_task', 'elo_bot.extensions.match.results.remove_helper_task', 'elo_bot.extensions.match.result_store.remove_helper_task', 'elo_bot.features.matches.resources.remove_helper_task', 'elo_bot.extensions.final.remove_helper_task'), new_callable=AsyncMock))
        self.enterContext(patch_sites(('elo_bot.extensions.match.results.choose_tip', 'elo_bot.extensions.match.hosting.choose_tip', 'elo_bot.extensions.match.settings.choose_tip', 'elo_bot.features.matches.tips.choose_tip'), new=AsyncMock(return_value="tip")))
        self.enterContext(patch('elo_bot.extensions.match.hosting.get_start_base_store_slot_availability', new=AsyncMock(return_value=(["Slot 1", "Slot 2"], {}))))
        self.enterContext(patch.object(live_start, "schedule_channel_name_edit"))
        self.enterContext(patch.object(war_start, "schedule_channel_name_edit"))
        player_model = MagicMock(find_one=AsyncMock(return_value=SimpleNamespace(tag="#PLAYER", elo=1000)))
        self.enterContext(patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player'), new=player_model))
        self.enterContext(patch.object(helper_host, "Player", new=player_model))
        self.enterContext(patch("elo_bot.features.base_store.controls.Player", new=player_model))
        self.f.runtime.coc_client.get_player = AsyncMock(return_value=SimpleNamespace(
            tag="#PLAYER", town_hall=3, share_link="https://player.example",
        ))
        self.choose = self.enterContext(patch_sites(('elo_bot.extensions.match.base_store.choose_bases', 'elo_bot.features.matches.attack_bases.choose_bases'), new_callable=AsyncMock))
        self.empty_slot = self.enterContext(patch.object(helper_host, "get_available_helper_slot", new_callable=AsyncMock))
        self.enterContext(patch.object(helper_host, "confirm_helper_availability", new=AsyncMock(return_value=True)))
        self.enterContext(patch.object(helper_base_store_availability,
                                      "confirm_host_war_match_on_stored_availability", new=AsyncMock(return_value=True)))
        self.schedule = self.enterContext(patch.object(helper_host.result_store_task, "schedule_automatic_result_store"))

    async def war(self, *, automatic=False, player=0, clan=CLAN) -> None:
        command = self.f.runtime.match_war_autostart if automatic else self.f.runtime.match_war_start
        await command.callback.func(self.f.runtime, self.f.ctx, player=player, clan=clan)

    async def live(self, clan=CLAN) -> None:
        await self.f.runtime.match_start.callback.func(self.f.runtime, self.f.ctx, clan=clan)

    def replies(self) -> str:
        return "\n".join(str(call.args[0]) for call in self.f.ctx.send.await_args_list if call.args)

    async def test_manual_war_starts_exact_slot_and_retains_reservation(self) -> None:
        self.f.document["participants"][0]["base_store"]["slot"] = "Slot 2"
        self.f.document["participants"][1]["base_store"] = None
        self.f.document["participants"][1]["opponent_bases_stored"] = False
        await self.war()
        self.assertFalse(self.f.http.create_payloads)
        self.assertIsNotNone(self.f.document["times"][0])
        self.assertEqual(self.f.document["host_clans"][0], CLAN)
        self.assertEqual(self.f.document["participants"][0]["base_store"]["slot"], "Slot 2")
        self.assertEqual(self.stored_message.embeds[0].title, "**Bases Stored**")
        self.assertIn("bases are stored in **slot 2** in **Stored Clan**", self.replies())
        self.assertNotIn("Use **Slot 2**", self.replies())
        self.choose.assert_not_awaited()
        self.empty_slot.assert_not_awaited()
        self.schedule.assert_not_called()

    async def test_unreadied_held_reservation_can_start_during_raid_weekend(self) -> None:
        self.f.document["participants"][0]["base_store"]["ready"] = False
        self.f.document["participants"][1]["base_store"] = None
        self.f.document["participants"][1]["opponent_bases_stored"] = False
        await self.war()
        self.assertFalse(self.f.http.create_payloads)
        self.assertIsNotNone(self.f.document["times"][0])
        self.assertEqual(self.f.document["host_clans"][0], CLAN)
        self.assertEqual(self.f.document["participants"][0]["base_store"]["slot"], "Slot 1")
        self.assertIn("bases are stored in **slot 1** in **Stored Clan**", self.replies())
        self.choose.assert_not_awaited()
        self.empty_slot.assert_not_awaited()
        self.schedule.assert_not_called()

    async def test_manual_war_waits_for_storage_publication_without_claiming_clan_occupied(self) -> None:
        store = self.f.document["participants"][0]["base_store"]
        store["ready"], store["message_id"] = False, None
        self.f.document["participants"][1]["base_store"] = None
        self.f.document["participants"][1]["opponent_bases_stored"] = False

        await self.war()

        self.assertIsNone(self.f.document["times"][0])
        self.assertIsNotNone(self.f.document["participants"][0]["base_store"])
        self.assertFalse(self.f.http.create_payloads)
        self.assertIn("Base storage is incomplete", self.replies())
        self.assertNotIn("currently occupied", self.replies())
        self.choose.assert_not_awaited()
        self.empty_slot.assert_not_awaited()

    async def test_self_host_war_uses_stored_http_job_during_raid_weekend(self) -> None:
        cog = SelfCommands(self.f.bot)
        cog.helper_api_base_url = self.f.runtime.helper_api_base_url
        cog.helper_api_key = self.f.runtime.helper_api_key
        self.f.bot.get_ext.return_value = self.f.runtime
        self.f.jobs(["succeeded"], "host_war_match_on_stored")
        self.f.ctx.author = MagicMock(id=111, mention="<@111>")
        await cog.self_host_war.callback.func(cog, self.f.ctx)
        self.assertEqual(self.f.http.create_payloads, [{
            "type": "host_war_match_on_stored", "requested_by": "111", "payload": {
                "host_clan_tag": CLAN, "player_tag": "#PLAYER", "districts": DISTRICTS,
                "slot": 1, "remove_bases_after": False,
            },
        }])
        self.assertIsNotNone(self.f.document["times"][0])
        self.assertEqual(self.f.document["host_clans"][0], CLAN)
        self.assertTrue(all(p["base_store"] for p in self.f.document["participants"]))
        self.choose.assert_not_awaited()
        self.empty_slot.assert_not_awaited()
        self.schedule.assert_called_once()

    async def test_self_host_war_rejects_without_stored_bases_during_raid_weekend(self) -> None:
        cog = SelfCommands(self.f.bot)
        self.f.bot.get_ext.return_value = self.f.runtime
        self.f.ctx.author = MagicMock(id=111, mention="<@111>")
        self.f.document["participants"][0]["base_store"] = None
        await cog.self_host_war.callback.func(cog, self.f.ctx)
        self.assertFalse(self.f.http.create_payloads)
        self.assertFalse(any(self.f.document["times"]))
        self.assertIn("You can only start a match with ready stored bases during the raid weekend", self.replies())

    async def test_manual_live_starts_both_stored_sides_in_selected_clan(self) -> None:
        await self.live()
        self.assertIsNotNone(self.f.document["times"][0])
        self.assertEqual(self.f.document["host_clans"][0], CLAN)
        self.assertTrue(all(p["base_store"] for p in self.f.document["participants"]))
        self.assertIn("Player 1 attacks the bases in **Slot 1**", self.replies())
        self.assertIn("Player 2 attacks the bases in **Slot 2**", self.replies())
        self.assertNotIn("Please set the players' bases", self.replies())

    async def test_accept_stored_live_request_starts_during_raid_weekend(self) -> None:
        cog = SelfCommands(self.f.bot)
        self.f.bot.get_ext.return_value = self.f.runtime
        self.enterContext(patch.object(cog.live_requests, "schedule_expiry"))
        self.enterContext(patch.object(helper_live, "confirm_helper_availability",
                                      new=AsyncMock(return_value=True)))
        self.f.jobs(["succeeded"], "host_live_match_on_stored")
        async def send_public(*args, **kwargs):
            components = kwargs.get("components")
            if isinstance(components, ipy.Button):
                kwargs["components"] = [components]
            return await self.f.send_public(*args, **kwargs)
        self.f.channel.send.side_effect = send_public
        self.f.ctx.author = MagicMock(id=111, mention="<@111>")
        self.f.ctx.message = self.stored_message
        self.f.ctx.custom_id = f"helper_stored_live_start|LIVE|{PAIR}"
        await cog.self_stored_live_start.callback.func(cog, self.f.ctx)
        message = next(m for m in self.f.messages.values()
                       if m.embeds and m.embeds[0].title == "Live Match Request")
        self.f.ctx.message = message
        self.f.ctx.custom_id = message.components[0].components[1].custom_id
        self.f.ctx.author = MagicMock(id=222, mention="<@222>")

        await cog.self_host_live_accept.callback.func(cog, self.f.ctx)

        self.assertEqual(self.f.http.create_payloads, [{
            "type": "host_live_match_on_stored", "requested_by": "111", "payload": {
                "host_clan_tag": CLAN, "player_1_tag": "#PLAYER", "player_2_tag": "#PLAYER",
                "districts": DISTRICTS, "remove_bases_after": False,
            },
        }])
        self.assertIsNotNone(self.f.document["times"][0])
        self.assertEqual(self.f.document["host_clans"][0], CLAN)
        self.assertTrue(all(p["base_store"] for p in self.f.document["participants"]))
        self.assertEqual(self.stored_message.embeds[0].title, "**Bases Stored**")
        self.assertTrue(all(b.disabled for b in message.components[0].components))
        self.assertNotIn("cannot start a live match", self.replies())
        self.choose.assert_not_awaited()
        await cog.self_host_live_accept.callback.func(cog, self.f.ctx)
        self.assertEqual(len(self.f.http.create_payloads), 1)

    async def test_fresh_automatic_live_start_remains_blocked_during_weekend(self) -> None:
        for participant in self.f.document["participants"]:
            participant["base_store"] = None
            participant["opponent_bases_stored"] = False
        await self.f.runtime._start_live(self.f.ctx, match_hosting_module.LivePreflight(
            clan=CLAN, host=self.f.bot.user, scout=False, automatic=True, requested_by="111",
        ))
        self.assertIn("cannot start a live match", self.replies())
        self.assertFalse(self.f.writes)
        self.assertFalse(self.f.http.create_payloads)

    async def test_automatic_live_without_request_id_uses_ready_stored_pair(self) -> None:
        self.enterContext(patch.object(helper_live, "confirm_helper_availability",
                                      new=AsyncMock(return_value=True)))
        self.f.jobs(["succeeded"], "host_live_match_on_stored")
        async def send_public(*args, **kwargs):
            components = kwargs.get("components")
            if isinstance(components, ipy.Button):
                kwargs["components"] = [components]
            return await self.f.send_public(*args, **kwargs)
        self.f.channel.send.side_effect = send_public
        await self.f.runtime._start_live(self.f.ctx, match_hosting_module.LivePreflight(
            clan=CLAN, host=self.f.bot.user, scout=False, automatic=True, requested_by="111",
        ))
        self.assertEqual(self.f.http.create_payloads, [{
            "type": "host_live_match_on_stored", "requested_by": "111", "payload": {
                "host_clan_tag": CLAN, "player_1_tag": "#PLAYER", "player_2_tag": "#PLAYER",
                "districts": DISTRICTS, "remove_bases_after": False,
            },
        }])
        self.assertIsNotNone(self.f.document["times"][0])
        self.assertNotIn("cannot start a live match", self.replies())
        self.choose.assert_not_awaited()

    async def test_stale_automatic_reservation_id_does_not_use_current_pair(self) -> None:
        await self.f.runtime._start_live(self.f.ctx, match_hosting_module.LivePreflight(
            clan=CLAN, host=self.f.bot.user, scout=False, automatic=True, requested_by="111",
            stored_reservation_id="a" * 16,
        ))
        self.assertIn("no longer ready", self.replies())
        self.assertFalse(self.f.http.create_payloads)
        self.assertFalse(any(self.f.document["times"]))
        self.assertTrue(all(p["base_store"] for p in self.f.document["participants"]))

    async def test_automatic_live_rejects_wrong_clan_when_stored_pair_is_ready(self) -> None:
        await self.f.runtime._start_live(self.f.ctx, match_hosting_module.LivePreflight(
            clan="#OTHER", host=self.f.bot.user, scout=False, automatic=True, requested_by="111",
        ))
        self.assertIn("Start the match in that clan", self.replies())
        self.assertFalse(self.f.http.create_payloads)
        self.assertFalse(any(self.f.document["times"]))
        self.assertTrue(all(p["base_store"] for p in self.f.document["participants"]))

    async def test_war_rejects_missing_expired_and_wrong_clan(self) -> None:
        original = deepcopy(self.f.document)
        for automatic in (False, True):
            for invalid in ("missing", "expired", "wrong_clan"):
                with self.subTest(automatic=automatic, invalid=invalid):
                    self.f.document = deepcopy(original)
                    store = self.f.document["participants"][0]["base_store"]
                    if invalid == "missing":
                        self.f.document["participants"][0]["base_store"] = None
                    elif invalid == "expired":
                        store["stored_at"] = 1
                    await self.war(automatic=automatic, clan="#OTHER" if invalid == "wrong_clan" else CLAN)
                    self.assertFalse(any(self.f.document["times"]))
                    self.assertFalse(self.f.http.create_payloads)
                    self.assertFalse(self.f.writes)
        self.assertIn("Start the match in that clan", self.replies())

    async def test_live_rejects_missing_separate_clans_duplicate_slots_and_wrong_clan(self) -> None:
        original = deepcopy(self.f.document)
        for invalid in ("missing", "separate_clans", "duplicate_slots", "incomplete", "wrong_clan"):
            with self.subTest(invalid=invalid):
                self.f.document = deepcopy(original)
                store = self.f.document["participants"][1]["base_store"]
                if invalid == "missing":
                    self.f.document["participants"][1]["base_store"] = None
                elif invalid == "separate_clans":
                    store["clan_tag"] = "#OTHER"
                elif invalid == "duplicate_slots":
                    store["slot"] = "Slot 1"
                elif invalid == "incomplete":
                    store["ready"] = False
                await self.live(clan="#OTHER" if invalid == "wrong_clan" else CLAN)
                self.assertFalse(any(self.f.document["times"]))
                self.assertFalse(self.f.writes)

    async def test_autostart_uses_stored_http_job_and_never_uploads_layouts(self) -> None:
        self.f.jobs(["succeeded"], "host_war_match_on_stored")
        await self.war(automatic=True, player=1)
        self.assertEqual(self.f.http.create_payloads, [{
            "type": "host_war_match_on_stored", "requested_by": "333", "payload": {
                "host_clan_tag": CLAN, "player_tag": "#PLAYER", "districts": DISTRICTS,
                "slot": 2, "remove_bases_after": False,
            },
        }])
        self.assertIsNotNone(self.f.document["times"][1])
        self.assertEqual(self.f.document["host_clans"][1], CLAN)
        self.assertTrue(all(p["base_store"] for p in self.f.document["participants"]))
        self.choose.assert_not_awaited()
        self.empty_slot.assert_not_awaited()
        self.schedule.assert_called_once()

    async def test_changed_reservation_blocks_commit_and_helper_submission(self) -> None:
        original = deepcopy(self.f.document)
        for kind in ("war", "automatic", "live"):
            with self.subTest(kind=kind):
                self.f.document = deepcopy(original)
                async def change_before_write(query, operation):
                    if "times.0" in operation["$set"]:
                        self.f.document["participants"][0]["base_store"]["reservation_id"] = "newer"
                    return await self.f.update(query, operation)
                self.f.write.side_effect = change_before_write
                if kind == "live":
                    await self.live()
                else:
                    await self.war(automatic=kind == "automatic")
                self.assertFalse(any(self.f.document["times"]))
                self.assertEqual(self.f.document["participants"][0]["base_store"]["reservation_id"], "newer")
                self.assertFalse(self.f.http.create_payloads)

    async def test_persistence_failure_has_no_success_or_helper_request(self) -> None:
        self.f.write.side_effect = PyMongoError("write failed")
        with self.assertRaises(PyMongoError):
            await self.war(automatic=True)
        self.assertFalse(any(self.f.document["times"]))
        self.assertFalse(self.f.http.create_payloads)
        self.assertNotIn("Successfully started", self.replies())
        self.assertTrue(all(p["base_store"] for p in self.f.document["participants"]))

    async def test_helper_failure_and_cancellation_keep_slots_and_abort_start(self) -> None:
        original = deepcopy(self.f.document)
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                self.f.document = deepcopy(original)
                self.f.jobs([status], "host_war_match_on_stored")
                await self.war(automatic=True)
                self.assertFalse(any(self.f.document["times"]))
                self.assertFalse(any(self.f.document["hosts"]))
                self.assertTrue(all(p["base_store"] for p in self.f.document["participants"]))
                self.assertTrue(self.f.document["participants"][0]["base_store"]["ready"])
                self.assertEqual(self.stored_message.embeds[0].title, "**Bases Stored**")
                self.schedule.assert_not_called()

    async def test_publication_failure_retains_committed_start_and_reservations(self) -> None:
        self.f.channel.send.side_effect = ipy.errors.HTTPException(MagicMock(status=503))
        with self.assertRaises(ipy.errors.HTTPException):
            await self.war()
        self.assertIsNotNone(self.f.document["times"][0])
        self.assertEqual(self.f.document["host_clans"][0], CLAN)
        self.assertTrue(all(p["base_store"] for p in self.f.document["participants"]))
        self.assertNotIn("Successfully started", self.replies())

    async def test_live_commit_rejects_opponent_slot_removed_by_cleanup(self) -> None:
        async def remove_before_write(query, operation):
            if "times.0" in operation["$set"]:
                self.assertTrue(match_lifecycle_module.BASE_STORE_RESERVATION_LOCK.locked())
                self.f.document["participants"][1]["base_store"] = None
            return await self.f.update(query, operation)
        self.f.write.side_effect = remove_before_write
        await self.live()
        self.assertFalse(any(self.f.document["times"]))
        self.assertNotIn("Successfully started", self.replies())

    async def test_legacy_store_without_readiness_or_timestamp_remains_usable(self) -> None:
        match = await self.f.find({})
        for participant in match.participants:
            participant.base_store.ready = None
            participant.base_store.stored_at = None
            participant.base_store.reservation_id = None
        self.assertIsNotNone(ready_store(match.participants[0]))
        self.assertIsNotNone(held_store(match.participants[0]))
        self.assertEqual(shared_store_clan(match), CLAN)
