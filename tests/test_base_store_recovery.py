from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy

from elo_bot.features.base_store import publication
from elo_bot.features.base_store.presentation import build_helper_store_components


from tests._match_publication_fixture import BaseStoreRecoveryStateMixin

class TestBaseStoreRecovery(BaseStoreRecoveryStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_channel_send_failure_recovers_undo_via_real_interaction_sdk(self) -> None:
        self.case.bot.http.create_message.side_effect = ipy.errors.HTTPException(MagicMock(status=403))
        with self.assertLogs(level="ERROR"):
            result = await publication.publish_base_store_message(
                self.ctx, self.request, build_helper_store_components(self.request),
            )
        self.assertFalse(result)
        self.assertIs(self.case.match.participants[0].base_store, self.store)
        self.assertFalse(self.store.ready)
        self.assertEqual(self.store.message_id, 90)
        self.assertEqual(self.store.reservation_id, "HELD")
        self.assertTrue(self.case.match.participants[0].opponent_bases_stored)
        message = self.messages[90]
        self.assertEqual(message["flags"], 64)
        self.assertEqual(message["embeds"][0]["title"], "**Base Storage Incomplete**")
        self.assertFalse(message["embeds"][0].get("description"))
        self.assertEqual([component["custom_id"] for row in message["components"] for component in row["components"]],
                         ["helper_stored_undo|TOKEN|0|HELD"])
        self.case.bot.http.edit_message.assert_not_awaited()
        self.case.bot.http.edit_interaction_message.assert_awaited_once()
        self.assertEqual(self.case.collection.update_one.await_args.args[1], {"$set": {
            "participants.0.base_store.message_id": 90, "participants.0.base_store.ready": False,
        }})

    async def test_retry_reuses_existing_message_and_never_enables_start(self) -> None:
        self.store.message_id = 90
        self.case.persisted["participants.0.base_store.message_id"] = 90
        message = deepcopy(self.case.message_data)
        message.update({"id": "90", "embeds": [{"title": "**Base Storage Unavailable**"}]})
        self.case.bot.http.get_message = AsyncMock(return_value=message)
        result = await publication.present_retained_base_store(self.ctx, self.request)
        self.assertTrue(result)
        self.case.bot.http.create_message.assert_not_awaited()
        self.case.bot.http.post_followup.assert_not_awaited()
        payload = self.case.bot.http.edit_message.await_args.args[0]
        self.assertEqual(payload["embeds"][0]["title"], "**Base Storage Incomplete**")
        self.assertFalse(payload["embeds"][0].get("description"))
        self.assertEqual([component["custom_id"] for row in payload["components"] for component in row["components"]],
                         ["helper_stored_undo|TOKEN|0|HELD"])
        self.assertEqual(self.store.message_id, 90)
        self.assertFalse(self.store.ready)

    async def test_retry_cannot_bind_over_a_replaced_reservation(self) -> None:
        self.case.persisted["participants.0.base_store.reservation_id"] = "NEW"
        result = await publication.present_retained_base_store(self.ctx, self.request)
        self.assertFalse(result)
        self.assertEqual(self.case.persisted["participants.0.base_store.reservation_id"], "NEW")
        self.assertIsNone(self.store.message_id)
        self.assertEqual(self.case.message_data["components"], [])
        self.assertEqual(self.case.message_data["embeds"][0]["title"], "**Base Storage Unavailable**")
        self.assertFalse(self.case.message_data["embeds"][0].get("description"))
