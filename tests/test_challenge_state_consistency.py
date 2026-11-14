import unittest
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

from beanie.odm.settings.document import DocumentSettings
from pymongo.errors import OperationFailure

from cogs.challenge_commands import ChallengeCommands
from elobot.db import Player


class TestChallengeStateConsistency(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = patch.object(Player, "_document_settings", DocumentSettings(
            name="players", use_state_management=True,
        ))
        settings.start()
        self.addCleanup(settings.stop)
        self.player = Player.model_construct(
            id="111", user_id="111", tag="#A", elo=1000, highest_elo=1000,
        )
        self.player._save_state()
        self.persisted: list[str] = []
        self.updates = []
        self.failure: OperationFailure | None = None
        self.registered = True
        self.cog = ChallengeCommands(MagicMock())

    def _context(self):
        ctx = MagicMock()
        ctx.author.id = 111
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        return ctx

    @contextmanager
    def _database(self):
        async def update(document: Player, operation, **_kwargs) -> Player:
            self.updates.append(deepcopy(dict(operation)))
            if self.failure is not None:
                raise self.failure
            fields = dict(operation)
            self.assertLessEqual(set(fields), {"$set", "$addToSet", "$pull"})
            if "$set" in fields:
                self.persisted = list(fields["$set"]["blocked_users"])
            if "$addToSet" in fields:
                user_id = fields["$addToSet"]["blocked_users"]
                if user_id not in self.persisted:
                    self.persisted.append(user_id)
            if "$pull" in fields:
                self.persisted = [user_id for user_id in self.persisted if user_id != fields["$pull"]["blocked_users"]]
            return document

        with (patch("cogs.challenge_commands.Player") as model, patch.object(Player, "update", new=update)):
            model.find_one = AsyncMock(
                side_effect=lambda *_args: self.player.model_copy(deep=True) if self.registered else None,
            )
            yield

    async def test_two_block_requests_preserve_both_memberships(self) -> None:
        # Given: two already-loaded player snapshots omit both target users.
        contexts = [self._context(), self._context()]

        # When: each real callback persists its independently requested block.
        with self._database():
            for ctx, user_id in zip(contexts, (333, 444), strict=True):
                await self.cog.challenge_block.callback.func(self.cog, ctx, MagicMock(id=user_id))

        # Then: neither acknowledged membership is replaced by the other snapshot.
        self.assertEqual(self.persisted, ["333", "444"])
        for ctx in contexts:
            ctx.send.assert_awaited_once()

    async def test_two_unblock_requests_preserve_both_removals(self) -> None:
        # Given: two already-loaded snapshots contain both blocked users.
        self.player.blocked_users = ["333", "444"]
        self.player._save_state()
        self.persisted = ["333", "444"]
        contexts = [self._context(), self._context()]

        # When: each real callback persists its independently requested unblock.
        with self._database():
            for ctx, user_id in zip(contexts, (333, 444), strict=True):
                await self.cog.challenge_unblock.callback.func(self.cog, ctx, MagicMock(id=user_id))

        # Then: the second stale snapshot cannot restore the first removed user.
        self.assertEqual(self.persisted, [])
        for ctx in contexts:
            ctx.send.assert_awaited_once()

    async def test_block_applies_when_stale_snapshot_already_contains_the_user(self) -> None:
        # Given: another callback removed the user after this block request loaded its snapshot.
        self.player.blocked_users = ["333"]
        self.player._save_state()
        ctx = self._context()

        # When: the block callback runs against the stale snapshot.
        with self._database():
            await self.cog.challenge_block.callback.func(self.cog, ctx, MagicMock(id=333))

        # Then: success corresponds to an applied idempotent membership operation.
        self.assertEqual(self.persisted, ["333"])
        self.assertEqual(self.updates, [{"$addToSet": {"blocked_users": "333"}}])
        ctx.send.assert_awaited_once()

    async def test_unblock_applies_when_stale_snapshot_omits_the_user(self) -> None:
        # Given: another callback blocked the user after this unblock request loaded its snapshot.
        self.persisted = ["333"]
        ctx = self._context()

        # When: the unblock callback runs against the stale snapshot.
        with self._database():
            await self.cog.challenge_unblock.callback.func(self.cog, ctx, MagicMock(id=333))

        # Then: success corresponds to an applied idempotent removal operation.
        self.assertEqual(self.persisted, [])
        self.assertEqual(self.updates, [{"$pull": {"blocked_users": "333"}}])
        ctx.send.assert_awaited_once()

    async def test_membership_write_failure_prevents_success_response(self) -> None:
        for blocking in (True, False):
            with self.subTest(blocking=blocking):
                # Given: the required Mongo write fails.
                self.player.blocked_users = [] if blocking else ["333"]
                self.player._save_state()
                self.failure = OperationFailure("write failed")
                ctx = self._context()
                command = self.cog.challenge_block if blocking else self.cog.challenge_unblock

                # When: the real callback reaches its membership update.
                with self._database(), self.assertRaises(OperationFailure):
                    await command.callback.func(self.cog, ctx, MagicMock(id=333))

                # Then: it never acknowledges success.
                ctx.send.assert_not_awaited()

    async def test_unregistered_player_has_no_membership_write(self) -> None:
        # Given: the requester has no persisted Player.
        self.registered = False

        # When: either membership command is invoked.
        with self._database():
            for command in (self.cog.challenge_block, self.cog.challenge_unblock):
                ctx = self._context()
                await command.callback.func(self.cog, ctx, MagicMock(id=333))
                ctx.send.assert_awaited_once()

        # Then: the existing registration error path performs no update.
        self.assertEqual(self.updates, [])
