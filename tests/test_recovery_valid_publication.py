import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy
from interactions.models.discord.message import process_message_payload

from common.matches.messages import MatchMessageState, publish_match_message
from common.matches.recovery import _attempt_repair_message, _repair_message
from tests import _match_publication_fixture as publication_tests


class TestValidPublicationRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_current_final_title_is_preserved_when_clearing_legacy_loading_text(self) -> None:
        for title in ("**Match Started**", "**Match Scheduled**", "**Match Rescheduled**"):
            with self.subTest(title=title):
                message = SimpleNamespace(
                    id=123, embeds=[ipy.Embed(title=title, description="Updating…")],
                    components=["valid controls"], edit=AsyncMock(),
                )

                await _repair_message(message, bound=True, ended=False, active=True)

                payload = message.edit.await_args.kwargs
                serialized = process_message_payload(**payload)
                self.assertEqual(serialized["embeds"][0]["title"], title)
                self.assertEqual(serialized["embeds"][0]["description"], "")
                self.assertNotIn("components", payload)


class TestPublicationRetryRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = publication_tests.MatchPublicationState(self)
        self.fixture.setUp()

    async def test_sweep_restores_failed_final_edit_from_committed_payload(self) -> None:
        fixture = self.fixture
        error = ipy.errors.HTTPException(MagicMock(status=503))
        edit_count = 0

        async def fail_first_edit(*args, **kwargs):
            nonlocal edit_count
            edit_count += 1
            if edit_count == 1:
                raise error
            return await fixture.edit_message(*args, **kwargs)

        fixture.bot.http.edit_message.side_effect = fail_first_edit
        controls = [ipy.ActionRow(ipy.Button(style=2, label="Store Result", custom_id="store|TOKEN|0"))]
        with self.assertRaises(ipy.errors.HTTPException):
            await publish_match_message(
                fixture.ctx, fixture.match, MatchMessageState.capture(fixture.match, 0),
                embed=ipy.Embed(title="**Match Started**"), components=controls,
            )
        self.assertEqual(fixture.message_data["embeds"][0]["title"], "**Match Update Incomplete**")
        writes = fixture.collection.update_one.await_count
        message = fixture.bot.cache.get_message(42, 43)

        await _attempt_repair_message(
            message, bound=True, ended=False, active=True, match=fixture.match, player=0,
        )

        self.assertEqual(fixture.message_data["embeds"][0]["title"], "**Match Started**")
        self.assertEqual(fixture.message_data["embeds"][0]["description"], "")
        self.assertEqual(fixture.message_data["components"][0]["components"][0]["custom_id"], "store|TOKEN|0")
        self.assertEqual(fixture.persisted["old_msgs.0"], 43)
        self.assertEqual(fixture.collection.update_one.await_count, writes + 1)
