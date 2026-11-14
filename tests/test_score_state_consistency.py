import unittest
from collections.abc import Mapping
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings
from pydantic import JsonValue
from pymongo.errors import OperationFailure

from elobot.features.hosting.war import result_store
from elobot.features.hosting.war import helper_store
from elobot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elobot.db import Match, Participant, Player
from tests import _helper_score_fixture as http_fixture


DISTRICTS = ["Wizard Valley", "Balloon Lagoon", "Dragon Cliffs"]


class TestScoreStateConsistency(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        result_store.RESULT_AUTOSTORE_LOCKS.clear()
        result_store.RESULT_FINALIZATION_LOCKS.clear()
        self.fixture = http_fixture.HelperScoreState(self)
        await self.fixture.asyncSetUp()
        self.fixture.status_responses = [(200, self.fixture.job_document(
            "succeeded", {district: self.fixture.score(3, 100) for district in DISTRICTS},
        ))]
        self.source = Match.model_construct(
            id=PydanticObjectId(), token="score-audit", matched_time=1.0, thread="321", is_war=True,
            times=[100, 100], hosts=["old-host", "other-host"], host_clans=["#OLD", "#OTHER"],
            old_msgs=[111, 222], final_districts=DISTRICTS.copy(),
            participants=[Participant(user_id=http_fixture.REQUESTER_ID), Participant(user_id="222")],
        )
        self.current = self.source.model_copy(deep=True)
        self.ctx = self.fixture.make_context()
        self.events: list[str] = []
        self.writes: list[dict[str, JsonValue]] = []
        self.fail_write = False
        self.restart_on_read = False
        self.restart_on_write = False
        self.read_count = 0
        self.ctx.send.side_effect = self.record_send
        self.ctx.edit.side_effect = self.record_edit
        self.fixture.runtime._finish_stored_result_side = AsyncMock()
        self.fixture.runtime._finalize_match_results_locked = AsyncMock()
        self.fixture.runtime.bot.fetch_user = AsyncMock(return_value=MagicMock(mention="<@player>", send=AsyncMock()))

    async def asyncTearDown(self) -> None:
        await self.fixture.asyncTearDown()

    async def record_send(self, content: str, **_kwargs) -> MagicMock:
        self.record_message(content)
        return MagicMock(id=444)

    async def record_edit(self, _message: MagicMock, *, content: str, **_kwargs) -> None:
        self.record_message(content)

    def record_message(self, content: str) -> None:
        if content.startswith(str(SUCCESS_EMOJI)):
            self.events.append("success-ui")

    def restart(self) -> None:
        self.current.times[0] = 150
        self.current.host_clans[0] = "#NEW"
        self.current.hosts[0] = "new-host"
        self.current.old_msgs[0] = 333

    async def find_match(self, _query: MagicMock) -> Match:
        self.read_count += 1
        if self.read_count == 2 and self.restart_on_read:
            self.restart()
        return self.current.model_copy(deep=True)

    def apply(self, updates: Mapping[str, JsonValue]) -> None:
        self.writes.append(dict(updates))
        self.events.append("write")
        self.current.participants[0].result = list(updates["participants.0.result"])
        if "submitters" in updates:
            self.current.submitters = list(updates["submitters"])
        else:
            self.current.submitters[0] = updates["submitters.0"]

    def matches_query(self, query: Mapping[str, JsonValue | PydanticObjectId]) -> bool:
        stored = self.current.model_dump(by_alias=True)
        for path, expected in query.items():
            if path == "$or":
                assert isinstance(expected, list)
                alternatives = []
                for condition in expected:
                    assert isinstance(condition, dict)
                    alternatives.append(self.matches_query(condition))
                if not any(alternatives):
                    return False
                continue
            value = stored
            for part in path.split("."):
                value = value[int(part)] if isinstance(value, list) else value[part]
            if isinstance(expected, dict) and "$ne" in expected:
                if value == expected["$ne"]:
                    return False
            elif value != expected:
                return False
        return True

    async def conditional_update(
            self,
            query: Mapping[str, JsonValue | PydanticObjectId],
            operation: Mapping[str, Mapping[str, JsonValue]],
    ) -> SimpleNamespace:
        if self.fail_write:
            raise OperationFailure("offline injected write failure")
        if self.restart_on_write:
            self.restart()
        if not self.matches_query(query):
            return SimpleNamespace(matched_count=0)
        self.apply(operation["$set"])
        return SimpleNamespace(matched_count=1)

    async def invoke(self, *, scheduled: bool = False) -> None:
        async def legacy_set(document: Match, updates: Mapping[str, JsonValue]) -> Match:
            if self.fail_write:
                raise OperationFailure("offline injected write failure")
            if self.restart_on_write:
                self.restart()
            self.apply(updates)
            return document

        collection = MagicMock(update_one=AsyncMock(side_effect=self.conditional_update))
        with (
            patch.object(result_store, "Match") as repository,
            patch.object(helper_store, "Player") as players,
            patch.object(Match, "set", new=legacy_set),
            patch.object(Player, "find_one", new=AsyncMock(return_value=None)),
            patch.object(Match, "get_pymongo_collection", return_value=collection),
            patch.object(Match, "_document_settings", DocumentSettings(name="matches", use_state_management=True)),
            patch.object(helper_store, "time", new=MagicMock(time=MagicMock(return_value=200.0))),
        ):
            repository.find_one = AsyncMock(side_effect=self.find_match)
            players.find_one = AsyncMock(return_value=MagicMock(tag="#PLAYER"))
            await result_store.store_match_result_automatically(
                self.fixture.runtime, self.ctx, result_store.AutomaticResultStoreRequest(
                    match=self.source, player=0, interactive=not scheduled, allow_cancellation=False,
                    expected_started_at=100 if scheduled else None,
                ),
            )

    async def test_success_is_reported_only_after_the_result_is_saved(self) -> None:
        # Given: a complete remote result and an unchanged match.
        # When
        await self.invoke()
        # Then
        self.assertEqual(self.events, ["write", "success-ui"])
        self.assertEqual(self.current.participants[0].result, ["3.100"] * 3)

    async def test_write_failure_reports_failure_without_storage_success(self) -> None:
        # Given
        self.fail_write = True
        # When
        with self.assertLogs(result_store.LOGGER, level="ERROR"):
            await self.invoke()
        # Then
        self.assertEqual(self.writes, [])
        self.assertNotIn("success-ui", self.events)
        self.assertTrue(self.ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))

    async def test_interactive_results_do_not_cross_a_restart_while_helper_runs(self) -> None:
        # Given
        self.restart_on_read = True
        # When
        await self.invoke()
        # Then
        self.assertEqual(self.writes, [])
        self.assertEqual(self.current.participants[0].result, [None] * 3)
        self.assertNotIn("success-ui", self.events)
        self.fixture.runtime._finish_stored_result_side.assert_not_awaited()

    async def test_scheduled_results_do_not_cross_a_restart_at_the_write_boundary(self) -> None:
        # Given: the replacement happens after the refreshed match was checked.
        self.restart_on_write = True
        # When
        await self.invoke(scheduled=True)
        # Then
        self.assertEqual(self.writes, [])
        self.assertEqual(self.current.participants[0].result, [None] * 3)
        self.assertNotIn("success-ui", self.events)
        self.fixture.runtime._finish_stored_result_side.assert_not_awaited()
