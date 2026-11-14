from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elobot.features.base_store import storage
from elobot.features.base_store import publication
from elobot.features.base_store import controls
from elobot.features.base_store.presentation import build_helper_store_components, build_manual_store_components
from elobot.features.hosting.war import helper_base_store
from elobot.features.hosting.war import helper_base_store_availability
from elobot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from tests import _match_publication_fixture as recovery_tests
from tests._stored_start_fixture import terminal_job
import elobot.extensions.match as match_module
import elobot.extensions.match.base_store as match_base_store_module
from tests._patch_sites import patch_sites


REAL_CLEAR = storage.clear_participant_base_store


class TestEphemeralBaseStoreUndo(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = recovery_tests.BaseStoreRecoveryState(self)
        self.fixture.setUp()
        self.case = self.fixture.case
        self.bot = self.case.bot
        self.bot._user = ipy.User(client=self.bot, id=999, username="Bot", avatar=None)
        self.bot.fetch_channel = AsyncMock(return_value=self.fixture.ctx.channel)
        self.cog = match_module.MatchCommands(self.bot)
        self.case.match.final_districts = ["Barbarian Camp", "Wizard Valley", "Capital Peak"]
        self.bot.http.post_initial_response = AsyncMock(side_effect=self.initial_response)
        self.bot.http.edit_interaction_message = AsyncMock(side_effect=self.edit_interaction)
        self.enterContext(patch.object(controls, "Match", new=MagicMock(find_one=AsyncMock(return_value=self.case.match))))
        self.enterContext(patch_sites(
            ('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.results.Match'),
            new=MagicMock(find_one=AsyncMock(return_value=self.case.match)),
        ))
        self.enterContext(patch.object(storage, "clear_participant_base_store", new=REAL_CLEAR))
        self.enterContext(patch.object(storage, "sync_base_store_slots", new=AsyncMock(return_value={})))
        self.enterContext(patch.object(helper_base_store_availability, "confirm_remove_bases_availability",
                                      new=AsyncMock(return_value=True)))
        self.remove_bases = self.enterContext(patch.object(helper_base_store, "remove_bases_interactively",
            new=AsyncMock(return_value=terminal_job("remove_bases", "succeeded"))))

    async def initial_response(self, payload, *_args, **_kwargs):
        if payload["type"] == 5:
            return None
        return await self.fixture.post_followup(payload["data"], None, None)

    async def edit_interaction(self, *args, **kwargs):
        payload = kwargs.get("payload", args[0] if args else None)
        message_id = kwargs.get("message_id", "@original")
        if message_id == "@original":
            return await self.fixture.post_followup(payload, None, None)
        return await self.fixture.edit_interaction(payload=payload, message_id=int(message_id))

    async def prepare_recovery(self, *, manual: bool = False) -> ipy.ComponentContext:
        request = replace(self.fixture.request, bases_stored=not manual)
        components = (build_manual_store_components(request, "Slot 1") if manual
                      else build_helper_store_components(request))
        self.bot.http.create_message.side_effect = ipy.errors.HTTPException(MagicMock(status=403))
        with self.assertLogs(level="ERROR"):
            self.assertFalse(await publication.publish_base_store_message(self.fixture.ctx, request, components))
        self.bot.http.get_message = AsyncMock(side_effect=ipy.errors.NotFound(MagicMock(status=404)))
        self.bot.http.edit_message = AsyncMock(side_effect=ipy.errors.NotFound(MagicMock(status=404)))
        ctx = ipy.ComponentContext(self.bot)
        ctx.channel_id, ctx.guild_id, ctx.message_id = 42, 50, 90
        ctx.token, ctx.id = "offline-undo", 701
        ctx.author_id = 100
        self.bot.cache.place_user_data({"id": "100", "username": "Player", "discriminator": "0", "avatar": None})
        ctx.custom_id = "undo_base_storage|TOKEN|0|HELD" if manual else "helper_stored_undo|TOKEN|0|HELD"
        return ctx

    async def assert_undo(self, *, manual: bool = False, stale: bool = False) -> None:
        ctx = await self.prepare_recovery(manual=manual)
        if stale:
            replacement = self.fixture.store.model_copy(update={"reservation_id": "NEW", "message_id": 99})
            self.case.match.participants[0].base_store = replacement
            self.case.persisted.update({"participants.0.base_store.reservation_id": "NEW",
                                        "participants.0.base_store.message_id": 99})
        callback = self.cog.undo_base_storage_button if manual else self.cog.helper_stored_undo_button
        await callback.callback.func(self.cog, ctx)
        clears = [call for call in self.case.collection.update_one.await_args_list
                  if call.args[1]["$set"].get("participants.0.base_store", "unchanged") is None]
        self.assertEqual(len(clears), 0 if stale else 1)
        self.bot.http.edit_message.assert_not_awaited()
        self.assertEqual(self.fixture.messages[90]["components"], [])
        responses = [message["content"] for message in self.fixture.messages.values() if message.get("content")]
        if stale:
            self.assertEqual(self.case.match.participants[0].base_store.reservation_id, "NEW")
            self.assertTrue(responses[-1].startswith(str(ERROR_EMOJI)))
            self.remove_bases.assert_not_awaited()
        else:
            self.assertIsNone(self.case.match.participants[0].base_store)
            self.assertFalse(self.case.match.participants[0].opponent_bases_stored)
            self.assertTrue(responses[-1].startswith(str(SUCCESS_EMOJI)))
            self.assertEqual(self.fixture.messages[90]["embeds"][0]["title"], "**Base Storage Undone**")
            self.assertFalse(self.fixture.messages[90]["embeds"][0].get("description"))
        self.transcript = deepcopy(self.fixture.transcript)

    async def test_helper_undo_from_ephemeral_recovery_clears_once_and_repaints(self) -> None:
        await self.assert_undo()

    async def test_manual_undo_from_ephemeral_recovery_clears_once_and_repaints(self) -> None:
        await self.assert_undo(manual=True)

    async def test_stale_helper_ephemeral_undo_preserves_replacement(self) -> None:
        await self.assert_undo(stale=True)

    async def test_stale_manual_ephemeral_undo_preserves_replacement(self) -> None:
        await self.assert_undo(manual=True, stale=True)

    async def test_manual_undo_defers_before_looking_up_match(self) -> None:
        ctx = await self.prepare_recovery(manual=True)

        async def lookup_after_defer(*_args, **_kwargs):
            self.assertTrue(ctx.deferred)
            return self.case.match

        match_base_store_module.Match.find_one.side_effect = lookup_after_defer
        await self.cog.undo_base_storage_button.callback.func(self.cog, ctx)
