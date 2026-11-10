import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy
from pymongo.errors import OperationFailure
from pymongo.results import UpdateResult

from common.constants import LVL2_ROLES
from common.players import self_hosting
from database_models import Match, Player, RecordedMatch
from database_models.final_models import FinalEditionStats, FinalPlayerProfile
from database_models.match_models import Participant


class TestSelfHostInvitations(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self_hosting.INVITATION_LOCKS.clear()
        self.player = Player.model_construct(
            id="player", user_id="123", tag="#PLAYER", elo=1000, highest_elo=1000,
            matches=[RecordedMatch.model_construct(id="1"), RecordedMatch.model_construct(id="2")],
        )
        self.delivered = []
        self.member = MagicMock(has_role=MagicMock(return_value=False))
        self.bot = MagicMock(
            user=SimpleNamespace(id=7), fetch_channel=AsyncMock(),
            fetch_member=AsyncMock(return_value=self.member),
        )
        self.bot.http.request = AsyncMock(side_effect=ipy.errors.NotFound(
            SimpleNamespace(status=404, reason="Not Found"),
        ))
        self.thread = MagicMock(spec=ipy.GuildPrivateThread, fetch_members=AsyncMock(return_value=[]))
        self.thread.guild = SimpleNamespace(id=456)
        self.bot.fetch_channel.return_value = self.thread
        self.thread.send = AsyncMock(side_effect=self.send)
        self.thread.history.side_effect = self.history
        self.collection = MagicMock(update_one=AsyncMock(side_effect=self.update))
        self.lookup = self.enterContext(patch.object(
            Player, "find_one", new=AsyncMock(side_effect=lambda *_args, **_kwargs: self.player.model_copy(deep=True)),
        ))
        self.enterContext(patch.object(Player, "get_pymongo_collection", return_value=self.collection))
        self.profile = self.enterContext(patch.object(
            FinalPlayerProfile, "find_one", new=AsyncMock(return_value=None),
        ))
        self.enterContext(patch.object(
            Match, "find", return_value=MagicMock(to_list=AsyncMock(return_value=[])),
        ))

    async def send(self, content, **kwargs):
        message = SimpleNamespace(id=900, author=self.bot.user, nonce=kwargs["nonce"], content=content)
        self.delivered.append(message)
        return message

    async def history(self, **_kwargs):
        for message in reversed(self.delivered):
            yield message

    async def update(self, query, operation):
        if self.player.self_host_invitation_message_id is not None:
            return UpdateResult({"n": 0, "nModified": 0}, acknowledged=True)
        for name, value in operation["$set"].items():
            setattr(self.player, name, value)
        return UpdateResult({"n": 1, "nModified": 1}, acknowledged=True)

    async def invite(self):
        await self_hosting.invite_eligible_players(self.bot, ("123",))

    async def test_second_record_invites_once_and_persists_delivery(self):
        await self.invite()
        await self.invite()
        self.thread.send.assert_awaited_once()
        self.assertEqual(self.player.self_host_invitation_message_id, 900)
        self.assertTrue(self.player.self_host_invitation_attempted)
        self.bot.fetch_channel.assert_awaited_once_with(self_hosting.AUTOMATIC_MATCH_THREAD)
        self.assertIn("<@123>", self.delivered[0].content)
        self.assertEqual(self.thread.send.await_args.kwargs["allowed_mentions"].users, [123])
        self.assertTrue(self.thread.send.await_args.kwargs["enforce_nonce"])
        self.bot.fetch_member.assert_awaited_once_with("123", 456, force=True)

    async def test_first_record_never_pings(self):
        self.player.matches = self.player.matches[:1]
        await self.invite()
        self.bot.fetch_channel.assert_not_awaited()
        self.collection.update_one.assert_not_awaited()

    async def test_two_stored_sides_invite_without_opponent_results(self):
        self.player.matches = []
        matches = [
            Match.model_construct(
                id=match_id,
                participants=[
                    Participant(user_id="123", result=["3.100", "2.90", "1.80"]),
                    Participant(user_id="456"),
                ],
            )
            for match_id in ("match-one", "match-two")
        ]
        with patch.object(Match, "find", return_value=MagicMock(to_list=AsyncMock(return_value=matches))):
            await self.invite()
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.player.self_host_invitation_message_id, 900)

    async def test_stored_side_and_record_for_same_match_count_once(self):
        self.player.matches = [RecordedMatch.model_construct(id="1", operation_key="match-one:revision")]
        match = Match.model_construct(
            id="match-one",
            participants=[Participant(user_id="123", result=["3.100", "2.90", "1.80"])],
        )
        with patch.object(Match, "find", return_value=MagicMock(to_list=AsyncMock(return_value=[match]))):
            await self.invite()
        self.assertEqual(self.delivered, [])
        self.assertIsNone(self.player.self_host_invitation_message_id)

    async def test_multiple_record_revisions_for_one_match_count_once(self):
        self.player.matches = [
            RecordedMatch.model_construct(id="1", operation_key="same-match:first"),
            RecordedMatch.model_construct(id="2", operation_key="same-match:corrected"),
        ]
        await self.invite()
        self.assertEqual(self.delivered, [])
        self.assertIsNone(self.player.self_host_invitation_message_id)

    async def test_cleared_or_incomplete_side_does_not_count(self):
        self.player.matches = [RecordedMatch.model_construct(id="1")]
        for result in ([None, None, None], ["3.100", "2.90", None], ["3.100", "2.90", "9.999"]):
            with self.subTest(result=result):
                match = Match.model_construct(id="match-two", participants=[Participant(user_id="123", result=result)])
                with patch.object(Match, "find", return_value=MagicMock(to_list=AsyncMock(return_value=[match]))):
                    await self.invite()
                self.assertEqual(self.delivered, [])
                self.assertIsNone(self.player.self_host_invitation_message_id)

    async def test_seven_complete_final_results_count_as_one_side(self):
        self.player.matches = [RecordedMatch.model_construct(id="1")]
        participant = Participant(
            user_id="123", format="elo_final_seven",
            districts=[None] * 7, bases=[None] * 7, result=["3.100"] * 7,
        )
        match = Match.model_construct(id="final-match", format="elo_final_seven", participants=[participant])
        with patch.object(Match, "find", return_value=MagicMock(to_list=AsyncMock(return_value=[match]))):
            await self.invite()
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.player.self_host_invitation_message_id, 900)

    async def test_existing_thread_member_is_not_invited(self):
        self.bot.http.request.side_effect = None
        self.bot.http.request.return_value = {"user_id": "123"}
        await self.invite()
        self.thread.send.assert_not_awaited()
        self.collection.update_one.assert_not_awaited()
        self.bot.fetch_member.assert_not_awaited()

    async def test_existing_role_access_is_not_invited_or_claimed(self):
        for role_id in LVL2_ROLES:
            with self.subTest(role_id=role_id):
                self.player.self_host_invitation_attempted = False
                self.player.self_host_invitation_message_id = None
                self.thread.send.reset_mock()
                self.collection.update_one.reset_mock()
                self.member.has_role.side_effect = lambda role, allowed=role_id: role == allowed
                await self.invite()
                self.thread.send.assert_not_awaited()
                self.collection.update_one.assert_not_awaited()
                self.assertFalse(self.player.self_host_invitation_attempted)

    async def test_failed_role_lookup_never_pings_or_claims(self):
        self.bot.fetch_member.side_effect = ipy.errors.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable"),
        )
        with self.assertLogs(level="ERROR"):
            await self.invite()
        self.thread.send.assert_not_awaited()
        self.collection.update_one.assert_not_awaited()

    async def test_missing_guild_member_is_not_invited(self):
        self.bot.fetch_member.return_value = None
        await self.invite()
        self.thread.send.assert_not_awaited()
        self.collection.update_one.assert_not_awaited()

    async def test_existing_eligible_player_is_invited_at_next_completion_check(self):
        self.player.matches += [RecordedMatch.model_construct(id=str(index)) for index in range(3, 8)]
        await self.invite()
        self.thread.send.assert_awaited_once()

    async def test_recovery_of_old_completion_does_not_backfill_an_existing_player(self):
        await self_hosting.invite_eligible_players(self.bot, ("123",), pending_only=True)
        self.bot.fetch_channel.assert_not_awaited()
        self.collection.update_one.assert_not_awaited()
        self.player.self_host_invitation_attempted = True
        await self_hosting.invite_eligible_players(self.bot, ("123",), pending_only=True)
        self.thread.send.assert_awaited_once()

    async def test_autowin_without_stored_results_does_not_count(self):
        self.player.matches = self.player.matches[:1]
        self.player.autowin_receipts = {"autowin-match": 1005}
        await self.invite()
        self.thread.send.assert_not_awaited()

    async def test_autowin_with_stored_results_counts_the_stored_side(self):
        self.player.matches = self.player.matches[:1]
        self.player.autowin_receipts = {"autowin-match": 1005}
        match = Match.model_construct(
            id="autowin-match", autowin_completed=True,
            participants=[Participant(user_id="123", result=["3.100", "2.90", "1.80"])],
        )
        with patch.object(Match, "find", return_value=MagicMock(to_list=AsyncMock(return_value=[match]))):
            await self.invite()
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.player.self_host_invitation_message_id, 900)

    async def test_autowin_converted_to_ranked_does_not_count_twice(self):
        self.player.matches = [RecordedMatch.model_construct(id="1", operation_key="same-match:operation")]
        self.player.autowin_receipts = {"same-match": 1005}
        await self.invite()
        self.thread.send.assert_not_awaited()

    async def test_final_editions_and_ranked_matches_combine(self):
        self.player.matches = self.player.matches[:1]
        self.profile.return_value = FinalPlayerProfile.model_construct(
            editions={"final": FinalEditionStats(season_number=1, played=1)},
        )
        await self.invite()
        self.thread.send.assert_awaited_once()

    async def test_official_final_and_its_stored_side_count_once(self):
        self.player.matches = []
        self.profile.return_value = FinalPlayerProfile.model_construct(
            editions={"final": FinalEditionStats(season_number=1, played=1)},
        )
        match = Match.model_construct(
            id="final-match", final_id="final", final_result_id="official",
            participants=[Participant(user_id="123", result=["3.100", "2.90", "1.80"])],
        )
        with patch.object(Match, "find", return_value=MagicMock(to_list=AsyncMock(return_value=[match]))):
            await self.invite()
        self.assertEqual(self.delivered, [])
        self.assertIsNone(self.player.self_host_invitation_message_id)

    async def test_final_byes_and_unplayed_forfeits_do_not_count(self):
        self.player.matches = [RecordedMatch.model_construct(id="1")]
        stats = FinalEditionStats(season_number=1, byes=2, forfeits=2)
        self.profile.return_value = FinalPlayerProfile.model_construct(editions={"final": stats})
        forfeit = Match.model_construct(
            id="forfeit", final_id="final", final_result_id="official-forfeit",
            participants=[Participant(user_id="123", result=["0.00"] * 3)],
        )
        with patch.object(Match, "find", return_value=MagicMock(to_list=AsyncMock(return_value=[forfeit]))):
            await self.invite()
        self.thread.send.assert_not_awaited()

    async def test_concurrent_completion_checks_send_only_once(self):
        started, release = anyio.Event(), anyio.Event()

        async def paused_send(content, **kwargs):
            started.set()
            await release.wait()
            return await self.send(content, **kwargs)

        self.thread.send.side_effect = paused_send
        with anyio.fail_after(2):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(self.invite)
                await started.wait()
                tasks.start_soon(self.invite)
                release.set()
        self.thread.send.assert_awaited_once()
        self.assertEqual(self.player.self_host_invitation_message_id, 900)

    async def test_failed_send_remains_pending_and_next_check_retries(self):
        self.thread.send.side_effect = ipy.errors.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable"),
        )
        with self.assertLogs(level="ERROR"):
            await self.invite()
        self.assertTrue(self.player.self_host_invitation_attempted)
        self.assertIsNone(self.player.self_host_invitation_message_id)
        first_nonce = self.thread.send.await_args.kwargs["nonce"]
        self.thread.send.side_effect = self.send
        await self.invite()
        self.assertEqual(self.thread.send.await_args.kwargs["nonce"], first_nonce)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.player.self_host_invitation_message_id, 900)

    async def test_failed_delivery_marker_recovers_message_without_another_ping(self):
        async def fail_marker(query, operation):
            if "self_host_invitation_message_id" in operation["$set"]:
                raise OperationFailure("marker write failed")
            return await self.update(query, operation)

        self.collection.update_one.side_effect = fail_marker
        with self.assertLogs(level="ERROR"):
            await self.invite()
        self.assertIsNone(self.player.self_host_invitation_message_id)
        self.collection.update_one.side_effect = self.update
        await self.invite()
        self.thread.send.assert_awaited_once()
        self.assertEqual(self.player.self_host_invitation_message_id, 900)

    async def test_failed_history_read_never_repeats_uncertain_delivery(self):
        self.player.self_host_invitation_attempted = True

        async def unavailable_history(**_kwargs):
            raise ipy.errors.HTTPException(SimpleNamespace(status=503, reason="Unavailable"))
            yield

        self.thread.history.side_effect = unavailable_history
        with self.assertLogs(level="ERROR"):
            await self.invite()
        self.thread.send.assert_not_awaited()
        self.assertIsNone(self.player.self_host_invitation_message_id)

    async def test_failed_player_lookup_never_sends(self):
        self.lookup.side_effect = OperationFailure("player read failed")
        with self.assertLogs(level="ERROR"):
            await self.invite()
        self.thread.send.assert_not_awaited()

    async def test_failed_membership_lookup_never_treats_player_as_a_nonmember(self):
        self.bot.http.request.side_effect = ipy.errors.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable"),
        )
        with self.assertLogs(level="ERROR"):
            await self.invite()
        self.thread.send.assert_not_awaited()
        self.collection.update_one.assert_not_awaited()

    async def test_cancellation_after_claim_is_recoverable_without_duplicate_delivery(self):
        started = anyio.Event()

        async def pending_send(_content, **_kwargs):
            started.set()
            await anyio.Event().wait()

        self.thread.send.side_effect = pending_send
        with anyio.fail_after(2):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(self.invite)
                await started.wait()
                tasks.cancel_scope.cancel()
        self.assertTrue(self.player.self_host_invitation_attempted)
        self.assertIsNone(self.player.self_host_invitation_message_id)
        self.thread.send.side_effect = self.send
        await self.invite()
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.player.self_host_invitation_message_id, 900)
