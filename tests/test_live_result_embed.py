import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId
from pymongo.errors import OperationFailure
from pymongo.results import UpdateResult

import cogs.match_commands as match_commands
from common.constants import RED_COLOR
from common.results.operations import ResultOperationConflict
from database_models import Match, Participant
from database_models.result_models import MatchResultOperation
from tests._result_retry_store import apply_operations, matches


class TestLiveResultEmbed(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.match = Match.model_construct(
            id=PydanticObjectId(), token="LIVE", thread="321", msg="42", is_war=False,
            participants=[Participant(user_id="111"), Participant(user_id="222")],
            result_operation=MatchResultOperation(
                key="result", response={"player1": "3.100\n3.100\n3.100", "player2": "2.90\n2.90\n2.90"},
                created_at=100, completed=True, summary_message_id=901,
            ),
        )
        self.database = self.match.model_dump(by_alias=True)

        async def update(query, operation) -> UpdateResult:
            matched = matches(self.database, query)
            if matched:
                apply_operations(self.database, operation)
            return UpdateResult({"n": int(matched)}, acknowledged=True)

        self.collection = MagicMock(update_one=AsyncMock(side_effect=update))
        self.enterContext(patch.object(Match, "get_pymongo_collection", return_value=self.collection))
        self.announcement = MagicMock(id=42, edit=AsyncMock())
        self.channel = MagicMock(fetch_message=AsyncMock(return_value=self.announcement),
                                 send=AsyncMock(return_value=MagicMock(id=43, edit=AsyncMock())))
        self.thread = MagicMock(fetch_message=AsyncMock(return_value=MagicMock(id=901, edit=AsyncMock())))
        bot = MagicMock(fetch_channel=AsyncMock(return_value=self.channel))
        self.cog = match_commands.MatchCommands(bot)
        self.embed = ipy.Embed(title="**Match has Ended**", description="111: 9 stars, 300%; 222: 6 stars, 270%")
        self.enterContext(patch.object(match_commands, "choose_tip", new=AsyncMock(return_value="tip")))

    async def publish(self) -> None:
        await self.cog._publish_result_embeds_locked(self.match, self.thread, self.embed)

    async def test_live_result_updates_original_after_binding_and_removes_spectate(self) -> None:
        # Given: the public live announcement is bound to this match.
        async def edit(**payload) -> None:
            self.assertEqual(self.database["result_operation"]["public_message_id"], 42)
            self.assertEqual(payload["embed"].description, self.embed.description)
            self.assertEqual(payload["embed"].color, RED_COLOR)
            self.assertEqual(payload["components"], [])

        self.announcement.edit.side_effect = edit
        # When: its recorded results are published.
        await self.publish()
        # Then: the original message receives the results without a duplicate.
        self.announcement.edit.assert_awaited_once()
        self.channel.fetch_message.assert_awaited_once_with(42)
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.database["msg"], "42")

    async def test_failed_edit_retries_same_bound_public_message(self) -> None:
        # Given: Discord rejects the first edit after the result binding commits.
        self.announcement.edit.side_effect = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
        with self.assertLogs(level="ERROR"), self.assertRaises(ipy.errors.HTTPException):
            await self.publish()
        self.assertEqual(self.database["result_operation"]["public_message_id"], 42)
        self.announcement.edit.side_effect = None
        # When: publication is retried.
        await self.publish()
        # Then: it updates the original rather than posting another result.
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.announcement.edit.await_args.kwargs["components"], [])

    async def test_failed_binding_preserves_original_message(self) -> None:
        # Given: the result message identity cannot be saved.
        self.collection.update_one.side_effect = OperationFailure("injected binding failure")
        # When: publication tries to adopt the announcement.
        with self.assertRaises(OperationFailure):
            await self.publish()
        # Then: no unbound result is published.
        self.announcement.edit.assert_not_awaited()
        self.channel.send.assert_not_awaited()
        self.assertIsNone(self.database["result_operation"]["public_message_id"])

    async def test_newer_public_binding_preserves_both_message_identities(self) -> None:
        # Given: the persisted announcement has changed since this match was read.
        self.database["msg"] = "99"
        # When: a stale result publisher tries to bind message 42.
        with self.assertRaises(ResultOperationConflict):
            await self.publish()
        # Then: it neither overwrites the newer identity nor edits the stale message.
        self.assertEqual(self.database["msg"], "99")
        self.announcement.edit.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_deleted_announcement_gets_a_bound_result_replacement(self) -> None:
        # Given: Discord confirms the original announcement has been deleted.
        self.channel.fetch_message.side_effect = ipy.errors.NotFound(MagicMock(status=404, reason="Not Found"))
        # When: the results are published.
        await self.publish()
        # Then: a replacement is durably bound with the same results and no live controls.
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.channel.send.await_args.kwargs["embed"].description, self.embed.description)
        self.assertEqual(self.channel.send.await_args.kwargs["components"], [])
        self.assertEqual(self.database["msg"], "43")
        self.assertEqual(self.database["result_operation"]["public_message_id"], 43)

    async def test_forbidden_announcement_does_not_create_duplicate(self) -> None:
        # Given: Discord access fails without establishing deletion.
        self.channel.fetch_message.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        # When: the publisher fetches the original announcement.
        with self.assertRaises(ipy.errors.Forbidden):
            await self.publish()
        # Then: it retains the existing message identity for a later retry.
        self.assertEqual(self.database["msg"], "42")
        self.channel.send.assert_not_awaited()
