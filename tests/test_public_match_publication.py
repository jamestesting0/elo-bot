from copy import deepcopy
import unittest
from unittest.mock import MagicMock

import anyio
import interactions as ipy
from pymongo.errors import PyMongoError

from common.constants import MATCH_CHANNEL
from common.matches.messages import MatchMessagePublicationError, MatchMessageState, publish_match_message
from common.matches.publication_state import restore_match_publication
from common.matches.recovery import _attempt_repair_message
from tests import _match_publication_fixture as publication_tests


class TestPublicMatchPublication(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = publication_tests.MatchPublicationState(self)
        self.fixture.setUp()
        self.fixture.live_request()
        self.channel = self.fixture.bot.cache.place_channel_data({
            "id": str(MATCH_CHANNEL), "type": 0, "name": "public", "guild_id": "50",
        })
        self.fixture.message_data["channel_id"] = str(MATCH_CHANNEL)
        self.controls = [ipy.ActionRow(ipy.Button(style=2, label="Spectate", custom_id="spectate|TOKEN"))]
        self.fixture.message_data["embeds"] = [{"title": "**Match Started**", "description": "Old host"}]
        self.fixture.message_data["components"] = [row.to_dict() for row in self.controls]

    def existing_message(self) -> ipy.Message:
        self.fixture.match.msg = "43"
        self.fixture.persisted["msg"] = "43"
        return self.fixture.bot.cache.place_message_data(deepcopy(self.fixture.message_data))

    async def publish(self, existing: ipy.Message | None = None) -> ipy.Message:
        return await publish_match_message(
            self.fixture.ctx, self.fixture.match, MatchMessageState.capture(self.fixture.match, 0),
            embed=ipy.Embed(title="**Match Started**", description="New host"),
            public_channel=self.channel, existing_message=existing,
        )

    async def test_failed_existing_edit_persists_intent_and_sweep_restores_even_normal_old_title(self) -> None:
        message = self.existing_message()
        self.fixture.bot.http.edit_message.side_effect = ipy.errors.HTTPException(MagicMock(status=503))
        with self.assertRaises(ipy.errors.HTTPException):
            await self.publish(message)
        self.assertEqual(self.fixture.message_data["embeds"][0]["description"], "Old host")
        self.assertEqual(message.embeds[0].title, "**Match Started**")
        self.assertTrue(self.fixture.persisted["public_message_publication"]["pending"])
        self.assertEqual(self.fixture.persisted["msg"], "43")
        self.assertEqual(self.fixture.trace, ["bind"])
        self.fixture.bot.http.edit_message.side_effect = self.fixture.edit_message

        await _attempt_repair_message(
            message, bound=True, ended=False, active=True, match=self.fixture.match, player=None,
        )

        self.assertEqual(self.fixture.message_data["embeds"][0]["description"], "New host")
        self.assertEqual(self.fixture.message_data["components"], [row.to_dict() for row in self.controls])
        self.assertFalse(self.fixture.persisted["public_message_publication"].get("pending", False))
        self.assertFalse(self.fixture.match.public_message_publication.pending)
        self.assertEqual(self.fixture.trace, ["bind", "read", "edit", "bind"])

    async def test_new_binding_failure_leaves_incomplete_without_controls_or_pointer(self) -> None:
        self.fixture.collection.update_one.side_effect = PyMongoError("write failed")
        with self.assertRaises(PyMongoError):
            await self.publish()
        sent = self.fixture.bot.http.create_message.await_args.args[0]
        self.assertEqual(sent["embeds"][0]["description"], "Updating…")
        self.assertEqual(sent["components"], [])
        self.assertEqual(self.fixture.message_data["embeds"][0]["title"], "**Match Update Incomplete**")
        self.assertEqual(self.fixture.message_data["components"], [])
        self.assertIsNone(self.fixture.persisted["msg"])
        self.assertIsNone(self.fixture.match.msg)
        self.assertNotIn("public_message_publication", self.fixture.persisted)

    async def test_changed_durable_run_rejects_existing_publication(self) -> None:
        message = self.existing_message()
        self.fixture.persisted["times.1"] = 2000
        with self.assertRaises(MatchMessagePublicationError):
            await self.publish(message)
        self.assertEqual(self.fixture.persisted["times.1"], 2000)
        self.assertNotIn("public_message_publication", self.fixture.persisted)
        self.assertEqual(self.fixture.message_data["embeds"][0]["title"], "**Match Update Incomplete**")
        self.assertEqual(self.fixture.message_data["components"], [row.to_dict() for row in self.controls])

    async def test_wrong_channel_rejected_before_writes_or_edits(self) -> None:
        message = self.existing_message()
        message._channel_id = 42
        with self.assertRaises(MatchMessagePublicationError):
            await self.publish(message)
        self.fixture.collection.update_one.assert_not_awaited()
        self.fixture.bot.http.edit_message.assert_not_awaited()

    async def test_cache_miss_keeps_valid_existing_message_and_controls(self) -> None:
        message = self.existing_message()
        self.fixture.bot.cache.channel_cache.pop(MATCH_CHANNEL)
        self.assertIsNone(message.channel)
        await self.publish(message)
        self.assertEqual(self.fixture.message_data["embeds"][0]["description"], "New host")
        self.assertEqual(self.fixture.message_data["components"], [row.to_dict() for row in self.controls])
        self.assertNotIn("components", self.fixture.bot.http.edit_message.await_args.args[0])
        self.assertEqual(self.fixture.persisted["msg"], "43")
        self.assertFalse(self.fixture.match.public_message_publication.pending)
        self.fixture.match.hosts[1] = "other-host"
        self.assertEqual(self.fixture.match.get_changes(), {"is_war": False, "hosts.1": "other-host"})

    async def test_recovery_rejects_changed_run_and_wrong_channel(self) -> None:
        message = self.existing_message()
        await self.publish(message)
        self.fixture.bot.http.edit_message.reset_mock()
        self.fixture.persisted["times.1"] = 2000
        self.assertFalse(await restore_match_publication(message, self.fixture.match, None))
        self.fixture.persisted["times.1"] = None
        message._channel_id = 42
        self.assertFalse(await restore_match_publication(message, self.fixture.match, None))
        self.fixture.bot.http.edit_message.assert_not_awaited()

    async def test_cancellation_during_initial_send_leaves_visible_incomplete(self) -> None:
        with anyio.CancelScope() as scope:
            async def cancel_during_send(*args, **kwargs):
                response = await self.fixture.create_message(*args, **kwargs)
                scope.cancel()
                await anyio.lowlevel.checkpoint()
                return response

            async def database_checkpoint(*args, **kwargs):
                await anyio.lowlevel.checkpoint()
                return await self.fixture.update_message_binding(*args, **kwargs)

            self.fixture.bot.http.create_message.side_effect = cancel_during_send
            self.fixture.collection.update_one.side_effect = database_checkpoint
            await self.publish()
            self.fail("Cancelled publication returned success")
        self.assertTrue(scope.cancelled_caught)
        self.assertEqual(self.fixture.message_data["embeds"][0]["title"], "**Match Update Incomplete**")
        self.assertEqual(self.fixture.message_data["components"], [])
        self.assertIsNone(self.fixture.persisted["msg"])
        self.assertNotIn("public_message_publication", self.fixture.persisted)
        self.assertEqual(self.fixture.trace, ["send", "edit"])
