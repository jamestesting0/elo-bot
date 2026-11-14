import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from pymongo.errors import PyMongoError

from elobot.features.base_store import raid_task
from elobot.extensions.match import MatchCommands
from elobot.config.game import BASE_STORE_SLOTS, HOST_CLAN_CHOICES
from elobot.config.presentation import COLOR
from elobot.config.ids import RAID_AUTOSTORE_LOGS_CHANNEL
from tests._raid_autostore_fixture import make_match


class TestRaidAutostoreTask(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.runtime = MagicMock()
        self.thread = MagicMock(send=AsyncMock())
        self.log = MagicMock(send=AsyncMock())
        self.runtime.bot.fetch_channel = AsyncMock(
            side_effect=lambda channel_id: self.log if channel_id == RAID_AUTOSTORE_LOGS_CHANNEL else self.thread,
        )
        self.slots = {str(choice.value): list(BASE_STORE_SLOTS) for choice in HOST_CLAN_CHOICES[:3]}
        self.runtime._base_store_sync_slots = AsyncMock(side_effect=lambda: self.slots)
        self.matches = []
        model = self.enterContext(patch.object(raid_task, "Match"))
        model.find.return_value.to_list = AsyncMock(side_effect=lambda: self.matches)
        model.find_one = AsyncMock(return_value=None)
        self.model = model
        self.store = self.enterContext(patch.object(raid_task, "store_self_bases", new_callable=AsyncMock))
        self.shuffle = self.enterContext(patch.object(raid_task.random, "shuffle", side_effect=lambda players: None))

    def log_text(self) -> str:
        return "".join(call.kwargs["embed"].description for call in self.log.send.await_args_list)

    async def test_numeric_thread_creation_order_overrides_matchmaking_order(self) -> None:
        self.matches = [make_match("100"), make_match("90")]
        self.matches[0].matched_time, self.matches[1].matched_time = 0, 999

        await raid_task.store_before_raid_weekend(self.runtime)

        requests = [call.args[2] for call in self.store.await_args_list]
        self.assertEqual([(request.match.thread, request.player) for request in requests],
                         [("90", 0), ("90", 1), ("100", 0), ("100", 1)])
        self.assertTrue(all(request.raid_autostore for request in requests))

    async def test_random_tie_break_uses_last_usable_slot_then_stops(self) -> None:
        self.matches = [make_match("100"), make_match("200")]
        self.slots = {tag: ["Slot 1"] for tag in self.slots}
        self.shuffle.side_effect = lambda players: players.reverse()

        async def consume(_runtime, _ctx, _request):
            self.slots[next(iter(self.slots))].clear()

        self.store.side_effect = consume

        await raid_task.store_before_raid_weekend(self.runtime)

        self.store.assert_awaited_once()
        self.assertEqual(self.store.await_args.args[2].player, 1)
        self.assertEqual(sum(map(len, self.slots.values())), 2)
        self.assertEqual(sum(map(bool, self.slots.values())), 2)
        self.assertIn("Stopped: 2 free slot(s) in 2 clan(s)", self.log_text())

    async def test_started_war_side_is_left_alone_while_other_side_is_stored(self) -> None:
        self.matches = [make_match()]
        self.matches[0].times[1] = 123

        await raid_task.store_before_raid_weekend(self.runtime)

        self.assertEqual([call.args[2].player for call in self.store.await_args_list], [0])
        self.assertEqual(self.model.find.call_args.args[0]["$or"], [
            {"times.0": None, "hosts.0": None}, {"times.1": None, "hosts.1": None},
        ])
        self.assertIn("<#100> side 1: no slot reserved", self.log_text())

    async def test_failed_player_does_not_block_remaining_eligible_players(self) -> None:
        self.matches = [make_match("100"), make_match("200")]
        self.store.side_effect = [PyMongoError("write failed"), None, None, None]

        with self.assertLogs(level="ERROR"):
            await raid_task.store_before_raid_weekend(self.runtime)

        self.assertEqual(self.store.await_count, 4)
        self.thread.send.assert_awaited_once()
        self.assertEqual([call.args[2].match.thread for call in self.store.await_args_list],
                         ["100", "100", "200", "200"])

    async def test_existing_storage_and_pending_submission_are_skipped(self) -> None:
        self.matches = [make_match()]
        self.matches[0].participants[0].opponent_bases_stored = True
        self.matches[0].participants[0].submit_bases = True

        await raid_task.store_before_raid_weekend(self.runtime)

        self.store.assert_not_awaited()
        self.runtime.bot.fetch_channel.assert_awaited_once_with(RAID_AUTOSTORE_LOGS_CHANNEL)
        self.assertEqual(self.log_text(), (
            "Checked **1** match(es) with an unstarted side.\n"
            "Skipped <#100> side 1: already stored.\n"
            "Skipped <#100> side 2: opponent bases incomplete.\n"
        ))
        self.assertEqual(self.log.send.await_args.kwargs["embed"].color, COLOR)

    async def test_log_failure_is_logged_without_affecting_storage(self) -> None:
        self.matches = [make_match()]
        self.log.send.side_effect = ipy.errors.HTTPException(MagicMock(status=500, reason="error"))

        with self.assertLogs(level="ERROR"):
            await raid_task.store_before_raid_weekend(self.runtime)

        self.assertEqual(self.store.await_count, 2)

    async def test_registered_task_runs_friday_at_0600_utc(self) -> None:
        cog = MatchCommands(MagicMock())
        trigger = cog.raid_weekend_auto_store.trigger
        trigger.last_call_time = datetime(2026, 10, 8, 12, tzinfo=UTC)

        with patch("elobot.extensions.match.store_before_raid_weekend", new_callable=AsyncMock) as store:
            await cog.raid_weekend_auto_store.callback.func(cog)

        self.assertEqual(trigger.next_fire(), datetime(2026, 10, 9, 6, tzinfo=UTC))
        store.assert_awaited_once_with(cog)
