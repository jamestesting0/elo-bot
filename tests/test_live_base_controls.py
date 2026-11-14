import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elo_bot.features.base_store import storage
from elo_bot.features.base_store import live_controls
from elo_bot.features.hosting.live.request import parse_live_request_id
from elo_bot.extensions.self_service import SelfCommands
from tests._live_base_fixture import CLAN, PAIR, LiveBaseFixture


class TestLiveBaseControls(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = LiveBaseFixture()
        await self.f.setup(self)
        self.stored = self.f.add_pair()
        self.cog = SelfCommands(self.f.bot)
        self.f.bot.get_ext.return_value = self.f.runtime
        self.f.runtime._start_live = AsyncMock()
        self.enterContext(patch.object(self.cog.live_requests, "schedule_expiry"))
        self.enterContext(patch.object(live_controls, "confirm_remove_bases_availability", new=AsyncMock(return_value=True)))
        self.enterContext(patch.object(storage, "sync_base_store_slots", new=AsyncMock(side_effect=self.f.slots)))
        self.f.ctx.message = self.stored
        self.f.ctx.custom_id = f"helper_stored_live_start|LIVE|{PAIR}"

    async def request(self):
        await self.cog.self_stored_live_start.callback.func(self.cog, self.f.ctx)
        message = next(m for m in self.f.messages.values() if m.embeds and m.embeds[0].title == "Live Match Request")
        self.f.ctx.message = message
        self.f.ctx.custom_id = message.components[0].components[1].custom_id
        return message

    async def test_button_posts_request_and_only_opponent_can_accept_once(self):
        message = await self.request()
        self.assertEqual(message.content, "<@222>")
        parsed = parse_live_request_id(self.f.ctx.custom_id)
        self.assertEqual(parsed.identity.stored_reservation_id, PAIR)
        await self.cog.self_host_live_accept.callback.func(self.cog, self.f.ctx)
        self.f.runtime._start_live.assert_not_awaited()
        self.f.ctx.author.id = 222
        self.f.ctx.author.mention = "<@222>"
        await self.cog.self_host_live_accept.callback.func(self.cog, self.f.ctx)
        request = self.f.runtime._start_live.await_args.args[1]
        self.assertEqual(request.clan, CLAN)
        self.assertEqual(request.stored_reservation_id, PAIR)
        self.assertEqual(request.requested_by, "111")
        self.assertTrue(request.allow_opponent_cancel)
        self.assertTrue(all(button.disabled for button in message.components[0].components))
        await self.cog.self_host_live_accept.callback.func(self.cog, self.f.ctx)
        self.f.runtime._start_live.assert_awaited_once()

    async def test_nickname_mention_does_not_block_opponent_accept(self):
        await self.request()
        self.f.ctx.author.id = 222
        self.f.ctx.author.mention = "<@!222>"
        await self.cog.self_host_live_accept.callback.func(self.cog, self.f.ctx)
        self.f.runtime._start_live.assert_awaited_once()
        self.assertEqual(self.f.runtime._start_live.await_args.args[1].stored_reservation_id, PAIR)

    async def test_replaced_reservation_cannot_be_started_by_old_request(self):
        await self.request()
        self.f.document["participants"][1]["base_store"]["reservation_id"] = "a" * 16
        self.f.ctx.author.id = 222
        self.f.ctx.author.mention = "<@222>"
        await self.cog.self_host_live_accept.callback.func(self.cog, self.f.ctx)
        self.f.runtime._start_live.assert_not_awaited()
        self.assertIn("stored bases changed", self.f.ctx.send.await_args.args[0])

    async def test_rejection_does_not_start_or_release_slots(self):
        message = await self.request()
        self.f.ctx.author.id = 222
        self.f.ctx.author.mention = "<@222>"
        self.f.ctx.custom_id = message.components[0].components[0].custom_id
        await self.cog.self_host_live_reject.callback.func(self.cog, self.f.ctx)
        self.f.runtime._start_live.assert_not_awaited()
        self.assertTrue(all(p["base_store"] for p in self.f.document["participants"]))

    async def test_expired_request_does_not_start_or_release_slots(self):
        await self.request()
        self.f.ctx.author.id = 222
        self.f.ctx.author.mention = "<@222>"
        expiry = parse_live_request_id(self.f.ctx.custom_id).identity.expires_at
        with patch("elo_bot.features.hosting.live.request.time.time", return_value=expiry + 1):
            await self.cog.self_host_live_accept.callback.func(self.cog, self.f.ctx)
        self.f.runtime._start_live.assert_not_awaited()
        self.assertTrue(all(p["base_store"] for p in self.f.document["participants"]))

    async def test_stale_button_and_outsider_do_not_start(self):
        self.f.ctx.author.id = 333
        await self.cog.self_stored_live_start.callback.func(self.cog, self.f.ctx)
        self.f.ctx.author.id = 111
        self.f.ctx.message = MagicMock(id=1)
        await self.cog.self_stored_live_start.callback.func(self.cog, self.f.ctx)
        self.f.runtime._start_live.assert_not_awaited()
        self.f.channel.send.assert_not_awaited()

    async def test_failed_start_can_be_requested_again_while_both_slots_remain(self):
        for participant in self.f.document["participants"]:
            participant["base_store"]["ready"] = False
        await self.cog.self_stored_live_start.callback.func(self.cog, self.f.ctx)
        self.assertTrue(all(participant["base_store"]["ready"] for participant in self.f.document["participants"]))
        self.assertTrue(any(
            message.embeds and message.embeds[0].title == "Live Match Request" for message in self.f.messages.values()
        ))

    async def test_active_match_does_not_report_stored_bases_as_missing(self):
        self.f.document["times"] = [1, None]
        for participant in self.f.document["participants"]:
            participant["base_store"]["ready"] = False
        await self.cog.self_stored_live_start.callback.func(self.cog, self.f.ctx)
        self.assertIn("already started", self.f.ctx.send.await_args.args[0])
        self.assertFalse(any(participant["base_store"]["ready"] for participant in self.f.document["participants"]))

    async def test_partial_undo_retains_other_slot_and_shared_undo_control(self):
        self.f.jobs(["succeeded", "failed"], "remove_bases")
        self.f.ctx.custom_id = f"helper_stored_live_undo|LIVE|{PAIR}"
        await self.cog.self_stored_live_undo.callback.func(self.cog, self.f.ctx)
        self.assertIsNone(self.f.document["participants"][0]["base_store"])
        self.assertIsNotNone(self.f.document["participants"][1]["base_store"])
        self.assertEqual([b.label for b in self.stored.components[0].components], ["Undo"])
        self.assertEqual(self.stored.embeds[0].title, "**Base Storage Incomplete**")
        self.assertFalse(self.stored.embeds[0].description)
        self.assertEqual(self.stored.embeds[0].fields[3].value,
                         "**[Player 111](<https://discordapp.com/users/111>)** — No slot reserved\n"
                         "**[Player 222](<https://discordapp.com/users/222>)** — Slot 2")
        self.assertEqual([p["payload"]["slot"] for p in self.f.http.create_payloads], [1, 2])

    async def test_successful_undo_releases_both_slots(self):
        self.f.jobs(["succeeded", "succeeded"], "remove_bases")
        self.f.ctx.custom_id = f"helper_stored_live_undo|LIVE|{PAIR}"
        await self.cog.self_stored_live_undo.callback.func(self.cog, self.f.ctx)
        self.assertTrue(all(p["base_store"] is None for p in self.f.document["participants"]))
        self.assertEqual(self.stored.components, [])
        self.assertEqual(self.stored.embeds[0].title, "**Base Storage Undone**")
        self.assertFalse(self.stored.embeds[0].description)

    def test_ordinary_live_request_id_still_parses_without_stored_context(self):
        parsed = parse_live_request_id("live_match_request|accept|LIVE|111|1000")
        self.assertIsNone(parsed.identity.stored_reservation_id)
