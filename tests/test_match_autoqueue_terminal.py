import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from pymongo.results import UpdateResult

from elobot.extensions.match import MatchCommands
from elobot.features.hosting.war import result_store
from elobot.features.hosting.war import result_store_ui
from elobot.features.hosting.war.helper_store import HelperScoreStoreOutcome
from elobot.db import Match, Player
from tests._result_finalization_fixture import VALID_RESULTS, RecordArgument, make_result_match, record_result
from tests._patch_sites import patch_sites
import elobot.extensions.match.results as match_results_module


class TestTerminalAutoqueue(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = MagicMock()
        self.guild = MagicMock()
        self.bot.fetch_guild = AsyncMock(return_value=self.guild)

    @staticmethod
    def _player(expiry: str):
        player = MagicMock(spec=Player)
        player.auto_queue = expiry
        return player

    @staticmethod
    def _stored_match(results: list[list[str | None]]) -> Match:
        match = make_result_match()
        for participant, stored_results in zip(match.participants, results, strict=True):
            participant.result = stored_results.copy()
        return match

    async def test_active_participants_queue_in_match_order_without_priority(self) -> None:
        first = self._player("101")
        second = self._player("102")
        first_member = MagicMock(id=111)
        second_member = MagicMock(id=222)
        self.guild.fetch_member = AsyncMock(side_effect=[first_member, second_member])
        with (
                patch_sites(('elobot.extensions.match.datetime', 'elobot.extensions.match.queue.datetime', 'elobot.extensions.match.lifecycle.datetime', 'elobot.extensions.match.bases.datetime', 'elobot.extensions.match.hosting.datetime', 'elobot.features.queue.state.datetime', 'elobot.features.queue.matching.datetime', 'elobot.features.matches.resources.datetime', 'elobot.features.matches.lifecycle.datetime', 'elobot.features.matches.attack_bases.datetime', 'elobot.features.results.recording.datetime')) as clock,
                patch_sites(('elobot.features.hosting.war.war_start.Player', 'elobot.extensions.match.queue.Player', 'elobot.extensions.match.lifecycle.Player', 'elobot.extensions.match.results.Player', 'elobot.extensions.match.hosting.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player')) as player_model,
                patch_sites(('elobot.extensions.match.queue.queue_player', 'elobot.features.queue.matching.queue_player'), new_callable=AsyncMock) as queue,
        ):
            clock.now.return_value.timestamp.return_value = 100
            player_model.find_one = AsyncMock(side_effect=[first, second])
            await match_results_module.autoqueue_completed_match_participants(
                self.bot, "TOKEN", ("111", "222")
            )

        self.guild.fetch_member.assert_has_awaits([unittest.mock.call("111"), unittest.mock.call("222")])
        self.assertEqual(queue.await_args_list, [
            unittest.mock.call(self.bot, first_member, priority=False),
            unittest.mock.call(self.bot, second_member, priority=False),
        ])

    async def test_disabled_expired_boundary_and_malformed_preferences_skip_queueing(self) -> None:
        players = [self._player("0"), self._player("99"), self._player("100"), self._player("invalid")]
        with (
                patch_sites(('elobot.extensions.match.datetime', 'elobot.extensions.match.queue.datetime', 'elobot.extensions.match.lifecycle.datetime', 'elobot.extensions.match.bases.datetime', 'elobot.extensions.match.hosting.datetime', 'elobot.features.queue.state.datetime', 'elobot.features.queue.matching.datetime', 'elobot.features.matches.resources.datetime', 'elobot.features.matches.lifecycle.datetime', 'elobot.features.matches.attack_bases.datetime', 'elobot.features.results.recording.datetime')) as clock,
                patch_sites(('elobot.features.hosting.war.war_start.Player', 'elobot.extensions.match.queue.Player', 'elobot.extensions.match.lifecycle.Player', 'elobot.extensions.match.results.Player', 'elobot.extensions.match.hosting.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player')) as player_model,
                patch_sites(('elobot.extensions.match.queue.queue_player', 'elobot.features.queue.matching.queue_player'), new_callable=AsyncMock) as queue,
        ):
            clock.now.return_value.timestamp.return_value = 100
            player_model.find_one = AsyncMock(side_effect=players)
            await match_results_module.autoqueue_completed_match_participants(
                self.bot, "TOKEN", ("111", "222")
            )
            await match_results_module.autoqueue_completed_match_participants(
                self.bot, "TOKEN", ("333", "444")
            )

        self.bot.fetch_guild.assert_not_awaited()
        queue.assert_not_awaited()

    async def test_first_player_failure_is_reported_and_second_still_queues(self) -> None:
        first = self._player("101")
        second = self._player("101")
        first_member = MagicMock(id=111)
        second_member = MagicMock(id=222)
        self.guild.fetch_member = AsyncMock(side_effect=[first_member, second_member])
        stderr = io.StringIO()
        with (
                patch_sites(('elobot.extensions.match.datetime', 'elobot.extensions.match.queue.datetime', 'elobot.extensions.match.lifecycle.datetime', 'elobot.extensions.match.bases.datetime', 'elobot.extensions.match.hosting.datetime', 'elobot.features.queue.state.datetime', 'elobot.features.queue.matching.datetime', 'elobot.features.matches.resources.datetime', 'elobot.features.matches.lifecycle.datetime', 'elobot.features.matches.attack_bases.datetime', 'elobot.features.results.recording.datetime')) as clock,
                patch_sites(('elobot.features.hosting.war.war_start.Player', 'elobot.extensions.match.queue.Player', 'elobot.extensions.match.lifecycle.Player', 'elobot.extensions.match.results.Player', 'elobot.extensions.match.hosting.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player')) as player_model,
                patch_sites(('elobot.extensions.match.queue.queue_player', 'elobot.features.queue.matching.queue_player'), new=AsyncMock(side_effect=[RuntimeError("queue failed"), None])) as queue,
                redirect_stderr(stderr),
        ):
            clock.now.return_value.timestamp.return_value = 100
            player_model.find_one = AsyncMock(side_effect=[first, second])
            await match_results_module.autoqueue_completed_match_participants(
                self.bot, "TOKEN", ("111", "222")
            )

        self.assertIn("match TOKEN, participant 111", stderr.getvalue())
        self.assertEqual(queue.await_count, 2)
        self.assertEqual(queue.await_args.args, (self.bot, second_member))

    async def test_successful_finalizer_autoqueues_then_schedules_once(self) -> None:
        self.guild.fetch_member = AsyncMock(side_effect=lambda user_id: MagicMock(
            id=int(user_id), display_name=f"Player {user_id}",
        ))
        cog = object.__new__(MatchCommands)
        cog.bot = self.bot
        cog._pending_match_thread_renames = {}
        match = make_result_match()
        match.hosts = [None, None]
        match.times = [None, None]
        thread = MagicMock(send=AsyncMock())
        thread.name = "🟢 Match TOKEN"
        channel = MagicMock(send=AsyncMock())
        self.bot.fetch_channel = AsyncMock(side_effect=[thread, channel])
        collection = MagicMock(update_one=AsyncMock(
            return_value=UpdateResult({"n": 1, "nModified": 1}, acknowledged=True)
        ))
        events: list[str] = []
        rename_task = MagicMock()

        async def record_success(*_args: RecordArgument) -> tuple[str, str]:
            events.append("record")
            return record_result(match)

        def schedule(_thread: MagicMock, _prefix: str) -> MagicMock:
            assert match.result_operation is not None
            self.assertTrue(match.result_operation.published)
            events.append("rename")
            return rename_task

        async def autoqueue(_bot: ipy.Client, _token: str, _participants: tuple[str, ...]) -> None:
            assert match.result_operation is not None
            self.assertTrue(match.result_operation.completed)
            self.assertFalse(match.result_operation.published)
            events.append("autoqueue")

        record = AsyncMock(side_effect=record_success)
        terminal = AsyncMock(side_effect=autoqueue)
        with (
                patch.object(Match, "save_changes", new_callable=AsyncMock),
                patch.object(Match, "get_pymongo_collection", return_value=collection),
                patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match')) as match_model,
                patch_sites(('elobot.features.hosting.war.war_start.Player', 'elobot.extensions.match.queue.Player', 'elobot.extensions.match.lifecycle.Player', 'elobot.extensions.match.results.Player', 'elobot.extensions.match.hosting.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player')) as player_model,
                patch('elobot.extensions.match.results.record_match', new=record),
                patch_sites(('elobot.extensions.match.results.invite_eligible_players', 'elobot.features.results.recording.invite_eligible_players'), new_callable=AsyncMock) as invite,
                patch('elobot.extensions.match.results.record_base_stats', new_callable=AsyncMock),
                patch_sites(('elobot.extensions.match.lifecycle.clear_match_base_stores', 'elobot.extensions.match.results.clear_match_base_stores'), new_callable=AsyncMock) as clear_stores,
                patch('elobot.extensions.match.results.award_helper_once', new_callable=AsyncMock) as credit,
                patch_sites(('elobot.extensions.match.lifecycle.free_match_clans', 'elobot.extensions.match.results.free_match_clans'), new_callable=AsyncMock),
                patch_sites(('elobot.extensions.match.lifecycle.remove_helper_task', 'elobot.extensions.match.results.remove_helper_task', 'elobot.features.matches.resources.remove_helper_task'), new_callable=AsyncMock),
                patch_sites(('elobot.extensions.match.results.fetch_user_match_id_choices', 'elobot.features.results.recording.fetch_user_match_id_choices'), new_callable=AsyncMock),
                patch_sites(('elobot.extensions.match.results.choose_tip', 'elobot.extensions.match.hosting.choose_tip', 'elobot.features.matches.tips.choose_tip'), new=AsyncMock(return_value="tip")),
                patch_sites(('elobot.extensions.match.results.schedule_channel_name_edit', 'elobot.extensions.match.hosting.schedule_channel_name_edit'), new=schedule),
                patch('elobot.extensions.match.results.autoqueue_completed_match_participants', new=terminal),
        ):
            match_model.find_one = AsyncMock(return_value=match)
            self.assertTrue(await cog._finalize_match_results_locked("TOKEN"))
            failed_cog = object.__new__(MatchCommands)
            failed_cog.bot = self.bot
            failed_cog._pending_match_thread_renames = {}
            failed_match = make_result_match()
            failed_match.hosts = [None, None]
            failed_match.times = [None, None]
            match_model.find_one.return_value = failed_match
            self.bot.fetch_channel = AsyncMock(return_value=thread)
            record.side_effect = RuntimeError("record failed")
            with self.assertRaises(RuntimeError):
                await failed_cog._finalize_match_results_locked("TOKEN")

        terminal.assert_awaited_once_with(self.bot, "TOKEN", ("111", "222"))
        invite.assert_awaited_once_with(self.bot, ("111", "222"), pending_only=False)
        clear_stores.assert_awaited_once_with(match, reset_stored_flags=True, bot=self.bot, reason="finished")
        self.assertEqual(events, ["record", "autoqueue", "rename"])
        self.assertEqual(match.recorded_match_id, "1")
        assert failed_match.result_operation is not None
        self.assertFalse(failed_match.result_operation.completed)
        self.assertFalse(failed_match.result_operation.published)
        credit.assert_not_awaited()
        player_model.find_one.assert_not_called()

    async def test_incomplete_and_duplicate_finalization_never_autoqueue(self) -> None:
        cog = object.__new__(MatchCommands)
        cog.bot = self.bot
        cog._pending_match_thread_renames = {"DUPLICATE": MagicMock()}
        incomplete = make_result_match(complete=False)
        thread = MagicMock()
        thread.name = "🟢 Match TOKEN"
        self.bot.fetch_channel = AsyncMock(return_value=thread)
        with (
                patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match')) as match_model,
                patch('elobot.extensions.match.results.autoqueue_completed_match_participants', new_callable=AsyncMock) as terminal,
        ):
            match_model.find_one = AsyncMock(return_value=incomplete)
            self.assertFalse(await cog._finalize_match_results_locked("TOKEN"))
            self.assertFalse(await cog._finalize_match_results_locked("DUPLICATE"))

        terminal.assert_not_awaited()

    async def test_one_sided_result_storage_autoqueues_player_without_terminal_finalization(self) -> None:
        from beanie import PydanticObjectId
        from beanie.odm.settings.document import DocumentSettings

        source = self._stored_match([[None, None, None], ["3.100", None, None]])
        fresh = self._stored_match([[None, None, None], ["3.100", None, None]])
        for match in (source, fresh):
            match.id = PydanticObjectId("0123456789abcdef01234567")
            match.times = [100, 100]
            match.hosts = ["host-one", "host-two"]
            match.old_msgs = [None, None]
        write = AsyncMock(return_value=MagicMock(matched_count=1))
        runtime = object.__new__(MatchCommands)
        runtime.bot = self.bot
        runtime._finalize_match_results_locked = AsyncMock()
        self.bot.fetch_user = AsyncMock(return_value=MagicMock(mention="<@111>"))
        ctx = MagicMock()
        ctx.author.id = 777
        ctx.send = AsyncMock()
        outcome = HelperScoreStoreOutcome(
            results=tuple(VALID_RESULTS),
            changed=True,
            skipped=(),
            job_id="job",
            status="succeeded",
            summary="stored",
            trusted=tuple(zip(fresh.final_districts, VALID_RESULTS)),
        )
        with (
                patch.object(Match, "_document_settings", DocumentSettings(name="matches", use_state_management=True)),
                patch.object(Match, "get_pymongo_collection", return_value=MagicMock(update_one=write)),
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(return_value=outcome)),
                patch.object(Player, "find_one", new=AsyncMock(return_value=None)),
                patch.object(result_store_ui, "notify_stored_result", new_callable=AsyncMock),
                patch('elobot.extensions.match.base_store.clear_participant_base_store', new_callable=AsyncMock),
                patch_sites(('elobot.extensions.match.results.free_clan', 'elobot.extensions.match.dashboards.free_clan', 'elobot.features.matches.resources.free_clan'), new_callable=AsyncMock),
                patch_sites(('elobot.extensions.match.lifecycle.remove_helper_task', 'elobot.extensions.match.results.remove_helper_task', 'elobot.features.matches.resources.remove_helper_task'), new_callable=AsyncMock),
                patch('elobot.extensions.match.results.autoqueue_completed_match_participants', new_callable=AsyncMock) as autoqueue,
        ):
            match_model.find_one = AsyncMock(side_effect=[source, fresh])
            await result_store.store_match_result_automatically(
                runtime,
                ctx,
                result_store.AutomaticResultStoreRequest(match=source, player=0),
            )

        write.assert_awaited_once()
        self.assertEqual(write.await_args.args[0]["participants.0.result"], [None, None, None])
        self.assertEqual(write.await_args.args[1], {"$set": {
            "participants.0.result": VALID_RESULTS,
            "submitters.0": "777",
        }})
        self.assertEqual(fresh.get_saved_state()["participants"][0]["result"], VALID_RESULTS)
        self.assertEqual(fresh.get_saved_state()["submitters"], ["777", None])
        autoqueue.assert_awaited_once_with(self.bot, "TOKEN", ("111",))
        runtime._finalize_match_results_locked.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
