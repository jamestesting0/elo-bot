import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

from beanie import PydanticObjectId
from beanie.odm.fields import ExpressionField
from beanie.odm.settings.document import DocumentSettings
from bson import DBRef
from pymongo.errors import OperationFailure
from pymongo.results import DeleteResult, UpdateResult

from cogs import data_commands
from tests import _data_reset_fixture as reset_fixture
from elobot.config.game import BASE_STORE_SLOTS
from elobot.config.emojis import SUCCESS_EMOJI
from elobot.db import Match, MatchPlayerStats, Player, RecordedMatch


def context() -> MagicMock:
    return MagicMock(defer=AsyncMock(), send=AsyncMock())


class TestDeleteMatchConsistency(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from tests._final_empty_storage import empty_final_storage
        empty_final_storage(self)
        for model in (Player, RecordedMatch):
            self.enterContext(patch.object(model, "_document_settings", DocumentSettings(
                name=model.Settings.name, use_state_management=True,
            )))
        for model, field in ((Player, "user_id"), (RecordedMatch, "id")):
            self.enterContext(patch.object(model, field, ExpressionField(field), create=True))
        self.recorded = RecordedMatch.model_construct(id="R1", time=1, players={
            user_id: MatchPlayerStats(
                stars=9, destruction=300, opponent_stars=8, opponent_destruction=290,
                opponent=opponent, elo_change=change, won=change > 0,
            ) for user_id, opponent, change in (("111", "222", 20), ("222", "111", -20))
        })
        self.players = {
            user_id: Player.model_construct(
                id=user_id, user_id=user_id, tag="#TAG", elo=1000, highest_elo=1200,
                matches=[self.recorded.model_copy(deep=True)], wins=7,
            ) for user_id in self.recorded.players
        }
        for player in self.players.values():
            player._save_state()
        self.cog = data_commands.DataCommands(MagicMock())
        self.failure_user: str | None = None
        self.fail_delete = False
        self.events: list[str] = []

        async def find_player(query, **_kwargs) -> Player:
            return self.players[query["user_id"]].model_copy(deep=True)

        async def legacy_set(document: Player, updates, **_kwargs) -> Player:
            self.fail_if_requested(document.user_id)
            stored = self.players[document.user_id]
            for field, value in updates.items():
                setattr(stored, field, deepcopy(value))
            stored._save_state()
            self.events.append(document.user_id)
            return document

        async def conditional_update(query, updates, **_kwargs) -> UpdateResult:
            user_id = query["user_id"]
            self.fail_if_requested(user_id)
            player = self.players[user_id]
            references = [match.to_ref() for match in player.matches]
            if query.get("matches") not in references:
                return UpdateResult({"n": 0, "nModified": 0}, acknowledged=True)
            player.elo += updates["$inc"]["elo"]
            player.matches = [match for match in player.matches if match.to_ref() != updates["$pull"]["matches"]]
            player._save_state()
            self.events.append(user_id)
            return UpdateResult({"n": 1, "nModified": 1}, acknowledged=True)

        async def delete_recorded(query, **_kwargs) -> DeleteResult:
            if self.fail_delete:
                self.fail_delete = False
                raise OperationFailure("injected recorded match deletion failure")
            self.assertEqual(query, {"_id": "R1"})
            self.events.append("delete")
            return DeleteResult({"n": 1}, acknowledged=True)

        original_find = RecordedMatch.find_one

        def find_recorded(query, **kwargs):
            if query == {"_id": "R1"}:
                return original_find(query, **kwargs)
            return AsyncMock(return_value=self.recorded.model_copy(deep=True))()

        self.enterContext(patch.object(Player, "find_one", side_effect=find_player))
        self.enterContext(patch.object(Player, "set", new=legacy_set))
        self.collection = MagicMock(update_one=AsyncMock(side_effect=conditional_update))
        self.enterContext(patch.object(Player, "get_pymongo_collection", return_value=self.collection))
        self.enterContext(patch.object(RecordedMatch, "find_one", side_effect=find_recorded))
        recorded_collection = MagicMock(delete_one=AsyncMock(side_effect=delete_recorded))
        recorded_collection.name = "recorded_matches"
        self.enterContext(patch.object(RecordedMatch, "get_pymongo_collection", return_value=recorded_collection))

    def fail_if_requested(self, user_id: str) -> None:
        if user_id == self.failure_user:
            self.failure_user = None
            raise OperationFailure("injected player update failure")

    async def invoke(self, ctx: MagicMock) -> None:
        await self.cog.data_delete_match.callback.func(self.cog, ctx, "R1")

    async def test_retry_only_rolls_back_participants_with_the_match_link(self) -> None:
        # Given: Player 2 fails after Player 1's rollback commits.
        self.failure_user = "222"
        failed = context()
        with self.assertRaises(OperationFailure):
            await self.invoke(failed)
        self.assertEqual([p.elo for p in self.players.values()], [980, 1000])
        failed.send.assert_not_awaited()
        self.assertNotIn("delete", self.events)
        # When: the same deletion is retried.
        succeeded = context()
        await self.invoke(succeeded)
        # Then: each original change is reversed exactly once before deletion succeeds.
        self.assertEqual([p.elo for p in self.players.values()], [980, 1020])
        self.assertEqual(self.events, ["111", "222", "delete"])
        self.assertTrue(succeeded.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
        self.assertEqual([p.matches for p in self.players.values()], [[], []])
        self.assertEqual([p.wins for p in self.players.values()], [7, 7])

    async def test_successful_delete_removes_the_match_from_autocomplete_choices(self) -> None:
        self.cog.bot.match_id_choices = {
            "all": [
                {"name": "Ann vs. Bob (R1)", "value": "R1", "search": "r1"},
                {"name": "Ann vs. Bob (R2)", "value": "R2", "search": "r2"},
            ],
            "111": [{"name": "vs. Bob (R1)", "value": "R1", "search": "r1"}],
            "222": [
                {"name": "vs. Ann (R1)", "value": "R1", "search": "r1"},
                {"name": "vs. Ann (R2)", "value": "R2", "search": "r2"},
            ],
        }
        await self.invoke(context())
        self.assertEqual([choice["value"] for choice in self.cog.bot.match_id_choices["all"]], ["R2"])
        self.assertEqual(self.cog.bot.match_id_choices["111"], [])
        self.assertEqual([choice["value"] for choice in self.cog.bot.match_id_choices["222"]], ["R2"])
        self.assertIn("delete", self.events)

    async def test_retry_after_record_deletion_failure_does_not_repeat_rollbacks(self) -> None:
        self.fail_delete = True
        self.cog.bot.match_id_choices = {
            "all": [{"name": "Ann vs. Bob (R1)", "value": "R1", "search": "r1"}],
            "111": [{"name": "vs. Bob (R1)", "value": "R1", "search": "r1"}],
        }
        failed = context()
        with self.assertRaises(OperationFailure):
            await self.invoke(failed)
        failed.send.assert_not_awaited()
        self.assertEqual([choice["value"] for choice in self.cog.bot.match_id_choices["all"]], ["R1"])
        self.assertEqual([choice["value"] for choice in self.cog.bot.match_id_choices["111"]], ["R1"])
        self.assertNotIn("delete", self.events)
        await self.invoke(context())
        self.assertEqual([p.elo for p in self.players.values()], [980, 1020])
        self.assertEqual(self.events, ["111", "222", "delete"])
        self.assertEqual(self.cog.bot.match_id_choices, {"all": [], "111": []})

    async def test_unlinked_match_does_not_change_a_players_current_elo(self) -> None:
        # Given: this player's rollback was already persisted.
        self.players["111"].matches = []
        # When
        await self.invoke(context())
        # Then: the match link is the atomic condition and is removed with the Elo adjustment.
        self.assertEqual(self.players["111"].elo, 1000)
        query, update = self.collection.update_one.await_args_list[0].args
        self.assertEqual(query, {"user_id": "111", "matches": DBRef("recorded_matches", "R1")})
        self.assertEqual(update, {"$inc": {"elo": -20}, "$pull": {"matches": DBRef("recorded_matches", "R1")}})


class TestResetAllConsistency(reset_fixture.DataResetFixture):


    async def test_reset_with_reserved_bases_requires_undo_and_preserves_all_data(self) -> None:
        # Given: an active reservation has a visible stored-base message.
        stored_message = deepcopy(self.message)
        stored_reservation = self.match.participants[0].base_store.model_copy(deep=True)
        self.bot.match_id_choices = {
            "all": [{"name": "Ann vs. Bob (1)", "value": "1", "search": "ann bob 1"}],
        }
        # When
        ctx = context()
        await self.invoke(ctx)
        # Then: reset cannot make itself an additional release reason.
        self.assertIn("Undo", ctx.send.await_args.args[0])
        self.assertIn("not reset", ctx.send.await_args.args[0])
        self.assertEqual(self.message, stored_message)
        self.assertEqual(self.match.participants[0].base_store, stored_reservation)
        self.assertTrue(self.match.participants[0].opponent_bases_stored)
        self.reset_players.assert_not_awaited()
        self.delete_recorded.assert_not_awaited()
        self.delete_matches.assert_not_awaited()
        self.match_collection.update_one.assert_not_awaited()
        self.bot.http.get_message.assert_not_awaited()
        self.bot.http.edit_message.assert_not_awaited()
        self.assertEqual(self.events, [])
        self.assertEqual(self.bot.match_id_choices["all"][0]["value"], "1")

    async def test_reset_preflights_every_match_before_deleting_an_unreserved_match(self) -> None:
        # Given: the first match is unreserved, while a later match still holds a slot.
        unreserved = self.match.model_copy(deep=True)
        unreserved.id = PydanticObjectId()
        unreserved.token = "UNRESERVED"
        unreserved.participants[0].base_store = None
        unreserved.participants[0].opponent_bases_stored = False
        Match.find_all().to_list.side_effect = lambda: [unreserved, self.match.model_copy(deep=True)]
        # When
        ctx = context()
        await self.invoke(ctx)
        # Then: even the earlier unreserved Match remains intact on refusal.
        self.assertIn("Undo", ctx.send.await_args.args[0])
        self.delete_matches.assert_not_awaited()
        self.reset_players.assert_not_awaited()
        self.delete_recorded.assert_not_awaited()
        self.assertEqual(self.events, [])

    async def test_reset_with_legacy_stored_flag_requires_undo(self) -> None:
        # Given: the legacy stored marker remains without a mapped BaseStore.
        self.match.participants[0].base_store = None
        # When
        ctx = context()
        with patch.object(data_commands, "clear_match_base_stores", new=AsyncMock(), create=True) as clear_stores:
            await self.invoke(ctx)
        # Then: the marker is retained and no opportunistic cleanup is attempted.
        clear_stores.assert_not_awaited()
        self.assertTrue(self.match.participants[0].opponent_bases_stored)
        self.assertIn("Undo", ctx.send.await_args.args[0])
        self.delete_matches.assert_not_awaited()
        self.reset_players.assert_not_awaited()
        self.delete_recorded.assert_not_awaited()

    async def test_unreserved_reset_keeps_the_existing_persistence_order(self) -> None:
        # Given: an allowed lifecycle action already released every reserved slot.
        self.match.participants[0].base_store = None
        self.match.participants[0].opponent_bases_stored = False
        self.clan.available_slots = list(BASE_STORE_SLOTS)
        self.bot.match_id_choices = {
            "all": [{"name": "Ann vs. Bob (1)", "value": "1", "search": "ann bob 1"}],
            "111": [{"name": "vs. Bob (1)", "value": "1", "search": "bob 1"}],
        }
        # When
        ctx = context()
        await self.invoke(ctx)
        # Then: only unreserved Match and player data are reset.
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
        self.assertLess(self.events.index("delete-matches"), self.events.index("free-clan"))
        self.assertLess(self.events.index("free-clan"), self.events.index("reset-players"))
        self.assertNotIn("clear-reservation", self.events)
        self.bot.http.edit_message.assert_not_awaited()
        self.bulk_delete_matches.assert_not_awaited()
        self.assertEqual(self.bot.match_id_choices, {})

    async def test_replacement_after_preflight_prevents_reset_and_resource_release(self) -> None:
        # Given: a reservation appears after preflight but before conditional Match deletion.
        self.match.participants[0].base_store = None
        self.match.participants[0].opponent_bases_stored = False
        self.replace_before_delete = True
        # When
        ctx = context()
        await self.invoke(ctx)
        # Then: the new reservation defeats deletion and retains all global player data.
        self.assertEqual(self.match.participants[0].base_store.reservation_id, "NEW")
        self.assertFalse(self.match_deleted)
        self.reset_players.assert_not_awaited()
        self.delete_recorded.assert_not_awaited()
        self.bulk_delete_matches.assert_not_awaited()
        self.assertNotIn("free-clan", self.events)
        self.assertIn("Undo", ctx.send.await_args.args[0])
        self.assertFalse(ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
