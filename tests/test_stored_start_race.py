import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import anyio

import elobot.extensions.self_service as self_commands
from elobot.features.base_store.models import SelfBaseStoreRequest
from elobot.features.base_store.publication import BaseStoreMessageConflict
from elobot.features.base_store.storage import store_self_bases
from elobot.features.hosting.war import war_start
from elobot.features.hosting.war import helper_base_store
from elobot.features.hosting.war import result_store_task
from tests._stored_start_fixture import StoredStartFixture, terminal_job
from tests._patch_sites import patch_sites
import elobot.features.matches.resources as resources_module
import elobot.features.base_store.match_adapters as match_adapters_module
import elobot.extensions.match.base_store as match_base_store_module
import elobot.extensions.match.queue as match_queue_module


class TestStoredStartRace(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = StoredStartFixture()
        self.fixture.addCleanup = self.addCleanup
        self.fixture.setUp()
        self.match = self.fixture.make_match()
        self.bot = self.fixture.bot
        self.bot.coc_client.get_player = AsyncMock(return_value=MagicMock(clan=None))
        self.bot.get_ext.return_value = self.fixture.cog
        self.self_cog = self_commands.SelfCommands(self.bot)
        self.ctx = self.fixture.make_slash_context()
        self.ctx.author.id = self.match.participants[0].user_id
        self.enterContext(patch.object(match_adapters_module.Match, "find_one", new=AsyncMock(return_value=self.match)))
        self.enterContext(patch.object(self_commands, "Match", new=MagicMock(
            find_one=AsyncMock(return_value=self.match),
        )))
        self.enterContext(patch_sites(('elobot.features.hosting.live.helper_live.is_match_finished', 'elobot.features.hosting.live.live_start.is_match_finished', 'elobot.features.hosting.war.helper_host.is_match_finished', 'elobot.extensions.match.bases.is_match_finished', 'elobot.extensions.match.base_store.is_match_finished', 'elobot.extensions.match.hosting.is_match_finished', 'elobot.extensions.match.settings.is_match_finished', 'elobot.features.matches.occupancy.is_match_finished'), return_value=False))
        self.enterContext(patch.object(self_commands, "is_match_finished", return_value=False))
        self.enterContext(patch.object(self_commands, "is_raid_weekend", return_value=False))
        self.self_confirm = self.enterContext(patch.object(
            self_commands, "confirm_pending_opponent_bases", new=AsyncMock(return_value=True),
        ))
        self.stored_confirm = self.enterContext(patch_sites(('elobot.extensions.match.base_store.confirm_pending_opponent_bases', 'elobot.extensions.match.hosting.confirm_pending_opponent_bases'), new=AsyncMock(return_value=True)))
        self.enterContext(patch_sites(('elobot.extensions.match.base_store.is_clan_available', 'elobot.extensions.match.hosting.is_clan_available'), new=AsyncMock(return_value=True)))
        self.enterContext(patch_sites(('elobot.extensions.match.sync_base_store_slots', 'elobot.extensions.match.base_store.sync_base_store_slots', 'elobot.extensions.match.dashboards.sync_base_store_slots'), new=AsyncMock(
            side_effect=self.fixture.slot_map,
        )))
        self.enterContext(patch_sites(('elobot.extensions.match.base_store.choose_bases', 'elobot.features.matches.attack_bases.choose_bases'), new=AsyncMock(return_value=self.match)))
        clan = MagicMock(tag=next(iter(self.fixture.clans)), availability_str="Available")
        self.enterContext(patch.object(self_commands, "Clan", new=MagicMock(
            find_all=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[clan]))),
            find_one=AsyncMock(return_value=clan),
        )))
        self.enterContext(patch("elobot.features.hosting.self_hosted.hosting.Player", new=MagicMock(
            find_one=AsyncMock(return_value=MagicMock(tag="#PLAYER")),
        )))
        self.enterContext(patch.object(match_queue_module.Player, "find_one", new=AsyncMock(
            return_value=MagicMock(tag="#PLAYER"),
        )))
        self.native_host = self.enterContext(patch.object(self_commands, "host_war_automatically", new=AsyncMock()))
        self.local_start = self.enterContext(patch.object(war_start, "start_war", new=AsyncMock()))
        self.schedule = self.enterContext(patch.object(result_store_task, "schedule_automatic_result_store"))
        self.helper_start = self.enterContext(patch.object(
            helper_base_store, "host_war_match_on_stored_interactively",
            new=AsyncMock(return_value=terminal_job("host_war_match_on_stored", "failed")),
        ))

    def add_store(self, *, ready: bool) -> None:
        source = self.fixture.make_match(stored=True)
        store = source.participants[0].base_store
        store.stored_at, store.reservation_id, store.message_id, store.ready = None, "HELD", 4242, ready
        self.match.participants[0].base_store = store
        self.match.participants[0].opponent_bases_stored = True

    async def test_pending_storage_blocks_self_host_before_start_or_helper(self) -> None:
        # Given a durable reservation whose Helper store job has not finished.
        self.add_store(ready=False)
        # When the participant requests hosting outside the raid weekend.
        for message_id in (None, 4242):
            with self.subTest(message_id=message_id):
                self.match.participants[0].base_store.message_id = message_id
                await self.self_cog.self_host_war.callback.func(self.self_cog, self.ctx)

        # Then the pending reservation survives without a start or a hosting job.
        self.native_host.assert_not_awaited()
        self.helper_start.assert_not_awaited()
        self.local_start.assert_not_awaited()
        self.assertEqual(self.match.times, [None, None])
        self.assertFalse(self.match.participants[0].base_store.ready)
        self.assertIn("Base storage is incomplete", self.ctx.send.await_args.args[0])
        self.assertTrue(self.ctx.send.await_args.kwargs["ephemeral"])

    async def test_ready_storage_uses_reserved_clan_and_slot_outside_raid_weekend(self) -> None:
        # Given ready storage that must not be replaced by ordinary hosting.
        self.add_store(ready=True)
        store = self.match.participants[0].base_store
        expected = store.model_dump()

        # When Helper declines the start through the shared stored-base path.
        await self.self_cog.self_host_war.callback.func(self.self_cog, self.ctx)

        # Then only the exact stored clan and slot were submitted; no local start committed.
        self.native_host.assert_not_awaited()
        self.helper_start.assert_awaited_once()
        payload = self.helper_start.await_args.args[2]
        self.assertEqual((payload.host_clan_tag, payload.slot), (store.clan_tag, 2))
        self.assertFalse(payload.remove_bases_after)
        self.local_start.assert_not_awaited()
        self.assertEqual(self.match.times, [None, None])
        self.assertEqual(store.model_dump(), expected)
        self.schedule.assert_not_called()

    async def test_stored_self_host_records_start_only_after_helper_success(self) -> None:
        # Given ready storage and a successful Helper start.
        self.add_store(ready=True)
        order = []

        async def helper_succeeds(*_args):
            order.append("helper_succeeded")
            return terminal_job("host_war_match_on_stored", "succeeded")

        async def record_start(_bot, _ctx, start):
            order.append("record_start")
            self.match.times[0] = 1000
            self.match.hosts[0] = str(self.bot.user.id)
            return MagicMock(id=700)

        self.helper_start.side_effect = helper_succeeds
        self.local_start.side_effect = record_start

        # When the slash command starts the held bases.
        await self.self_cog.self_host_war.callback.func(self.self_cog, self.ctx)

        # Then Helper success precedes the committed start and exact reservation binding.
        self.assertEqual(order, ["helper_succeeded", "record_start"])
        self.self_confirm.assert_not_awaited()
        self.stored_confirm.assert_awaited_once()
        self.assertEqual(self.match.times, [1000, None])
        start = self.local_start.await_args.args[2]
        self.assertEqual(start.stored_start.stores[0][1].reservation_id, "HELD")
        self.assertIsNotNone(self.match.participants[0].base_store)
        self.schedule.assert_called_once_with(self.bot, result_store_task.ScheduledResultStoreRequest(
            token=str(self.match.token), player=0, requested_by=str(self.ctx.author.id),
            started_at=1000, submitter_id=str(self.bot.user.id),
        ))

    async def test_store_rejects_a_side_already_reserved_for_start(self) -> None:
        # Given a start already owning this match side.
        lease = await match_base_store_module.try_reserve_resource_match_side(str(self.match.token), 0)
        with patch.object(helper_base_store, "store_bases_interactively", new=AsyncMock(
            return_value=terminal_job("store_bases", "failed"),
        )) as helper_store:
            try:
                # When a concurrent store request tries to claim it.
                await store_self_bases(self.fixture.cog, self.ctx, SelfBaseStoreRequest(self.match, 0))
            finally:
                lease.release()

        # Then no storage or Helper work was started.
        helper_store.assert_not_awaited()
        self.assertIsNone(self.match.participants[0].base_store)
        self.assertIn("already in progress", self.ctx.send.await_args.args[0])

    async def test_store_holds_side_until_helper_and_publication_finish(self) -> None:
        # Given a store job parked after its durable slot claim.
        entered, finish = anyio.Event(), anyio.Event()
        undo_ctx = self.fixture.make_component_context("undo")

        async def parked_store(*_args):
            entered.set()
            await finish.wait()
            return terminal_job("store_bases", "succeeded")

        with patch.object(helper_base_store, "store_bases_interactively", new=AsyncMock(side_effect=parked_store)), \
                patch.object(helper_base_store, "remove_bases_interactively", new=AsyncMock(
                    return_value=terminal_job("remove_bases", "failed"),
                )) as remove:
            with anyio.fail_after(3):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(store_self_bases, self.fixture.cog, self.ctx, SelfBaseStoreRequest(self.match, 0))
                    await entered.wait()
                    undo_ctx.custom_id += "|" + self.match.participants[0].base_store.reservation_id
                    try:
                        # When Undo runs while Helper is still storing these bases.
                        await self.fixture.cog.helper_stored_undo_button.callback.func(self.fixture.cog, undo_ctx)
                    finally:
                        finish.set()

        # Then Undo never touched Helper, and storage completed with its reservation intact.
        remove.assert_not_awaited()
        self.assertTrue(self.match.participants[0].base_store.ready)
        self.assertIn("already in progress", undo_ctx.send.await_args.args[0])
        self.assertFalse(resources_module.RESOURCE_MATCH_SIDE_LOCKS[(str(self.match.token), 0)].locked())

    async def test_stored_message_conflict_reports_committed_start_without_releasing_slot(self) -> None:
        # Given Helper succeeded, and local start committed before a stored-message conflict.
        self.add_store(ready=True)
        self.helper_start.return_value = terminal_job("host_war_match_on_stored", "succeeded")

        async def committed_conflict(*_args):
            self.match.times[0] = 1000
            self.match.hosts[0] = str(self.bot.user.id)
            raise BaseStoreMessageConflict("The match started, but its held base-slot message could not be updated.")

        self.local_start.side_effect = committed_conflict

        # When the message update fails after the committed start.
        with self.assertLogs(level="ERROR"):
            await self.self_cog.self_host_war.callback.func(self.self_cog, self.ctx)

        # Then the response reports partial completion and the committed state remains intact.
        self.assertEqual(self.match.times, [1000, None])
        self.assertEqual(self.match.participants[0].base_store.reservation_id, "HELD")
        self.assertIn("start was saved", self.ctx.send.await_args.args[0])
        self.assertIn("No reservation was released by this start", self.ctx.send.await_args.args[0])
        self.schedule.assert_called_once()

    async def test_declined_local_start_marks_stored_slash_message_incomplete(self) -> None:
        # Given Helper started the bases, but the local conditional start is declined.
        self.add_store(ready=True)
        message = self.fixture.make_component_context("start").message
        self.ctx.channel.fetch_message = AsyncMock(return_value=message)
        self.helper_start.return_value = terminal_job("host_war_match_on_stored", "succeeded")
        self.local_start.return_value = None

        # When the slash command reaches the failed local commit.
        await self.self_cog.self_host_war.callback.func(self.self_cog, self.ctx)

        # Then its bound message explains the failure and only Undo remains available.
        self.assertEqual(self.match.times, [None, None])
        self.assertFalse(self.match.participants[0].base_store.ready)
        self.assertEqual(self.match.participants[0].base_store.reservation_id, "HELD")
        edit = message.edit.await_args.kwargs
        self.assertEqual(edit["embeds"][0].title, "**Match Start Incomplete**")
        self.assertEqual([button.custom_id for button in edit["components"]],
                         [f"helper_stored_undo|{self.match.token}|0|HELD"])
        self.assertIn("did not record the match start", self.ctx.send.await_args.args[0])

    async def test_cancelled_storage_releases_action_lock_but_retains_slot(self) -> None:
        # Given a durable reservation whose Helper job is still running.
        entered = anyio.Event()

        async def parked_store(*_args):
            entered.set()
            await anyio.sleep_forever()

        with patch.object(helper_base_store, "store_bases_interactively", new=AsyncMock(side_effect=parked_store)):
            with anyio.fail_after(3):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(store_self_bases, self.fixture.cog, self.ctx, SelfBaseStoreRequest(self.match, 0))
                    await entered.wait()
                    # When the request is cancelled during Helper work.
                    tasks.cancel_scope.cancel()

        # Then another action can recover the exact pending reservation without a false success.
        self.assertIsNotNone(self.match.participants[0].base_store)
        self.assertFalse(self.match.participants[0].base_store.ready)
        self.assertTrue(self.match.participants[0].opponent_bases_stored)
        self.assertFalse(resources_module.RESOURCE_MATCH_SIDE_LOCKS[(str(self.match.token), 0)].locked())
        self.ctx.channel.send.assert_not_awaited()

    async def test_declined_slash_start_preserves_message_when_reservation_changed(self) -> None:
        self.add_store(ready=True)
        self.ctx.channel.fetch_message = AsyncMock()
        self.match.attach_base_store_message = AsyncMock(return_value=False)
        self.helper_start.return_value = terminal_job("host_war_match_on_stored", "succeeded")
        self.local_start.return_value = None

        await self.self_cog.self_host_war.callback.func(self.self_cog, self.ctx)

        self.ctx.channel.fetch_message.assert_not_awaited()
        self.assertEqual(self.match.times, [None, None])
        self.assertEqual(self.match.participants[0].base_store.reservation_id, "HELD")
        self.assertTrue(self.match.participants[0].base_store.ready)
        self.assertIn("current state could not be confirmed", self.ctx.send.await_args.args[0])
        self.schedule.assert_not_called()
