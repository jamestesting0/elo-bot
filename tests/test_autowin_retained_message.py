import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy

from cogs.base_store.models import BaseStorePresentationRequest
from cogs.base_store.presentation import build_helper_store_components
from elobot.config.emojis import SUCCESS_EMOJI
from elobot.db import BaseStore, Match
from tests import _autowin_fixture as autowin_fixture


class TestAutowinRetainedMessage(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.state = autowin_fixture.AutowinState(self)
        self.state.setUp()
        store = BaseStore(clan_tag="#CLAN", slot="Slot 1", reservation_id="HELD", message_id=9001, ready=True)
        self.state.rows[Match][0]["participants"][0].update(base_store=store.model_dump(), opponent_bases_stored=True)
        self.client = ipy.Client()
        channel = self.client.cache.place_channel_data({"id": "321", "type": 0, "name": "match", "guild_id": "50"})
        self.state.bot.fetch_channel = AsyncMock(return_value=channel)
        self.state.bot.cache = self.client.cache
        match = Match.model_validate(deepcopy(self.state.rows[Match][0]))
        controls = build_helper_store_components(BaseStorePresentationRequest(
            match=match, player=0, helper_id="900", clan=MagicMock(),
        ))
        self.original_components = [control.to_dict() for control in controls]
        self.original_undo = controls[1].to_dict()
        self.message = {
            "id": "9001", "channel_id": "321", "guild_id": "50", "type": 0,
            "author": {"id": "60", "username": "EloBot", "discriminator": "0", "avatar": None},
            "content": "", "timestamp": "2026-09-06T00:00:00+00:00",
            "embeds": [{"title": "**Bases Stored**", "description": "Ready to start."}],
            "components": [ipy.ActionRow(*controls).to_dict()],
        }
        self.events: list[str] = []

        async def edit_message(payload, _channel_id, _message_id, **_kwargs):
            self.message.update(deepcopy(payload))
            self.events.append("stored-message:reserved")
            return deepcopy(self.message)

        async def result_sent(message, **_kwargs) -> None:
            if message.startswith(str(SUCCESS_EMOJI)):
                self.events.append("autowin:success")

        self.client.http.get_message = AsyncMock(side_effect=lambda *_args: deepcopy(self.message))
        self.client.http.edit_message = AsyncMock(side_effect=edit_message)
        self.ctx = self.state.context()
        self.ctx.send.side_effect = result_sent

    async def test_autowin_removes_start_and_keeps_the_exact_undo_without_releasing(self) -> None:
        await self.state.invoke(self.ctx)
        participant = self.state.rows[Match][0]["participants"][0]
        self.assertEqual(participant["base_store"]["reservation_id"], "HELD")
        self.assertEqual(participant["base_store"]["slot"], "Slot 1")
        self.assertFalse(participant["base_store"]["ready"])
        self.assertTrue(participant["opponent_bases_stored"])
        retained = self.message["components"][0]["components"]
        self.assertEqual(retained, self.original_components)
        self.assertEqual(self.message["embeds"][0]["title"], "**Bases Stored**")
        self.assertTrue(self.state.rows[Match][0]["autowin_completed"])
        self.assertLess(self.events.index("stored-message:reserved"), self.events.index("autowin:success"))
        self.assertEqual(self.state.elos(), [1005, 965])

    async def test_repaint_failure_is_retryable_without_repeating_ratings(self) -> None:
        original_edit = self.client.http.edit_message.side_effect
        self.client.http.edit_message.side_effect = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
        with self.assertRaises(ipy.errors.HTTPException):
            await self.state.invoke(self.ctx)
        self.ctx.send.assert_not_awaited()
        self.assertFalse(self.state.rows[Match][0]["autowin_completed"])
        self.assertEqual(self.state.elos(), [1005, 965])
        self.assertEqual(len(self.message["components"][0]["components"]), 2)
        self.client.http.edit_message.side_effect = original_edit
        await self.state.invoke(self.ctx)
        self.assertEqual(self.state.elos(), [1005, 965])
        self.assertEqual(self.message["components"][0]["components"], self.original_components)
        self.assertTrue(self.state.rows[Match][0]["autowin_completed"])

    async def test_unavailable_channel_stops_completion_with_explicit_held_slot_guidance(self) -> None:
        self.state.bot.fetch_channel.return_value = None
        await self.state.invoke(self.ctx)
        self.assertFalse(self.state.rows[Match][0]["autowin_completed"])
        self.assertIn("rating changes are saved", self.ctx.send.await_args.args[0])
        self.assertIn("slot remains reserved", self.ctx.send.await_args.args[0])
        self.assertEqual(self.state.elos(), [1005, 965])
        self.client.http.edit_message.assert_not_awaited()
        self.assertEqual(len(self.message["components"][0]["components"]), 2)

    async def test_replacement_during_repaint_preserves_new_slot_and_stops_success(self) -> None:
        original_edit = self.client.http.edit_message.side_effect
        replacement = BaseStore(
            clan_tag="#NEW", slot="Slot 2", reservation_id="NEW", message_id=9002, ready=True,
        ).model_dump()
        replaced = False

        async def replace_during_edit(payload, *args, **kwargs):
            nonlocal replaced
            response = await original_edit(payload, *args, **kwargs)
            if not replaced:
                replaced = True
                self.state.rows[Match][0]["participants"][0]["base_store"] = deepcopy(replacement)
            return response

        self.client.http.edit_message.side_effect = replace_during_edit
        await self.state.invoke(self.ctx)
        self.assertEqual(self.state.rows[Match][0]["participants"][0]["base_store"], replacement)
        self.assertFalse(self.state.rows[Match][0]["autowin_completed"])
        self.assertEqual(self.message["components"], [])
        self.assertEqual(self.message["embeds"][0]["title"], "**Base Storage Unavailable**")
        self.assertFalse(self.message["embeds"][0].get("description"))
        self.assertIn("rating changes are saved", self.ctx.send.await_args.args[0])
        self.ctx.channel.send.assert_not_awaited()
        self.assertEqual(self.state.elos(), [1005, 965])
        collection = Match.get_pymongo_collection()
        query, projection = collection.find_one.await_args.args
        self.assertEqual(query["participants.0.base_store.reservation_id"], "HELD")
        self.assertEqual(projection, {"_id": 1})
